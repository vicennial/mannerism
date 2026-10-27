"""The hook-root registry and the end-of-session single-file sync. Synthetic data only; fake HOME (conftest)."""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time

import pytest

from mannerism import config, core, hookroots, hooks, store, sync
from mannerism.adapters.claude_code import ClaudeCode
from mannerism.adapters.codex import Codex
from mannerism.core import Me

SID = "11111111-aaaa-bbbb-cccc-222222222222"
PROMPT = "please rename the helper in the demo module and keep the public names the same, thanks"


def transcript(root, slug="-work-demo", sid=SID, text=PROMPT):
    """A one-prompt Claude Code transcript under `root`/projects."""
    p = root / "projects" / slug / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    line = {"type": "user", "message": {"role": "user", "content": text}, "timestamp": "2026-03-01T10:00:00.000Z",
            "sessionId": sid, "uuid": f"u-{sid[:4]}", "cwd": "/work/demo"}
    p.write_text(json.dumps(line) + "\n", "utf-8")
    return p


def rollout(root, name="rollout-2026-03-01T10-00-00-aaaa.jsonl"):
    p = root / "sessions" / "2026" / "03" / "01" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{}\n", "utf-8")
    return p


def enable(*names):
    config.ensure_home()
    cfg = config.Config()
    for n in names:
        cfg.sources[n] = {"enabled": True, "paths": []}
    config.save_config(cfg)


def stored_texts():
    con = store.connect()
    try:
        return [r[0] for r in con.execute("SELECT text FROM units")]
    finally:
        con.close()


# --- paths -------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("harness,path,root", [
    ("claude_code", "/tmp/e/projects/-w-x/abc.jsonl", "/tmp/e"),
    ("claude_code", "/projects/-w-x/abc.jsonl", None),  # the root would be "/"
    ("claude_code", "/tmp/e/projects/abc.jsonl", None),  # too shallow
    ("claude_code", "/tmp/e/other/-w-x/abc.jsonl", None),
    ("claude_code", "/tmp/e/projects/-w-x/abc.txt", None),
    ("claude_code", "relative/projects/-w-x/abc.jsonl", None),
    ("codex", "/tmp/c/sessions/2026/03/01/rollout-1.jsonl", "/tmp/c"),
    ("codex", "/tmp/c/sessions/2026/03/01/notes.jsonl", None),
    ("codex", "/tmp/c/sessions/2026/rollout-1.jsonl", None),
    ("claude_code", None, None),
    ("claude_code", 5, None),
    ("claude_code", "/tmp/e/projects/-w-x/a\x00.jsonl", None),
    ("claude_code", "/" + "x" * 2000 + "/projects/-w-x/abc.jsonl", None),
    ("nope", "/tmp/e/projects/-w-x/abc.jsonl", None),
])
def test_root_from_transcript(harness, path, root):
    got = hookroots.root_from_transcript(harness, path)
    assert (str(got) if got else None) == root


def test_identify_tells_the_harnesses_apart():
    assert hookroots.identify("/a/projects/s/x.jsonl")[0] == "claude_code"
    assert hookroots.identify("/a/sessions/2026/03/01/rollout-x.jsonl")[0] == "codex"
    assert hookroots.identify("/a/whatever.jsonl") is None
    assert hookroots.identify(None) is None


# --- registry ----------------------------------------------------------------------------------------------------

def test_record_writes_a_private_file_and_leaves_no_temp(tmp_path):
    root = tmp_path / "eph"
    p = transcript(root)
    assert hookroots.record("claude_code", str(p)) == root
    reg = hookroots.registry_path()
    assert stat.S_IMODE(reg.stat().st_mode) == 0o600
    assert json.loads(reg.read_text())["roots"]["claude_code"][str(root)] > 0
    assert [f.name for f in reg.parent.iterdir() if f.name.endswith(".tmp")] == []
    assert hookroots.registered("claude_code") == [root]
    assert hookroots.registered("codex") == []


def test_record_rejects_roots_that_do_not_exist_and_bad_paths(tmp_path):
    assert hookroots.record("claude_code", str(tmp_path / "gone" / "projects" / "s" / "x.jsonl")) is None
    assert hookroots.record("claude_code", "garbage") is None
    assert not hookroots.registry_path().exists()


def test_record_is_atomic_when_the_write_fails(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    hookroots.record("claude_code", str(transcript(a)))
    before = hookroots.registry_path().read_bytes()
    def boom(*_a):
        raise OSError("disk")

    with monkeypatch.context() as m:
        m.setattr(os, "replace", boom)
        with pytest.raises(OSError):
            hookroots.record("claude_code", str(transcript(b)))
    assert hookroots.registry_path().read_bytes() == before  # the old file is intact
    assert [f.name for f in hookroots.registry_path().parent.iterdir() if f.name.endswith(".tmp")] == []


def test_record_skips_the_write_for_a_recently_seen_root(tmp_path):
    p = transcript(tmp_path / "a")
    hookroots.record("claude_code", str(p), now=1_000_000.0)
    mtime = hookroots.registry_path().stat().st_mtime_ns
    hookroots.record("claude_code", str(p), now=1_000_100.0)
    assert hookroots.registry_path().stat().st_mtime_ns == mtime
    hookroots.record("claude_code", str(p), now=1_000_000.0 + 2 * 86400)
    assert json.loads(hookroots.registry_path().read_text())["roots"]["claude_code"][str(tmp_path / "a")] \
        == 1_000_000 + 2 * 86400


def test_prune_drops_roots_missing_for_a_long_time_only(tmp_path):
    now = 5_000_000.0
    gone_old, gone_recent, alive_old = tmp_path / "go", tmp_path / "gr", tmp_path / "ao"
    for r in (gone_old, gone_recent, alive_old):
        hookroots.record("claude_code", str(transcript(r)), now=now - 100 * 86400)
    hookroots.record("claude_code", str(transcript(gone_recent)), now=now - 2 * 86400)
    import shutil
    shutil.rmtree(gone_old)
    shutil.rmtree(gone_recent)
    hookroots.record("claude_code", str(transcript(tmp_path / "new")), now=now)
    kept = set(json.loads(hookroots.registry_path().read_text())["roots"]["claude_code"])
    assert kept == {str(gone_recent), str(alive_old), str(tmp_path / "new")}
    assert hookroots.registered("claude_code") == [tmp_path / "new", alive_old]  # a missing root is skipped silently


def test_registry_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(hookroots, "MAX_ROOTS", 5)
    for i in range(9):
        hookroots.record("claude_code", str(transcript(tmp_path / f"r{i}")), now=1_000_000.0 + i)
    roots = json.loads(hookroots.registry_path().read_text())["roots"]["claude_code"]
    assert sorted(roots) == [str(tmp_path / f"r{i}") for i in range(4, 9)]  # the newest five


@pytest.mark.parametrize("junk", [b"", b"not json", b"[1]", b'{"roots": 5}', b'{"roots": {"claude_code": []}}',
                                  b'{"roots": {"claude_code": {"/x": "y", "": 1, "/ok": 3}, "evil": {"/z": 1}}}'])
def test_garbage_registry_is_harmless(tmp_path, junk):
    core.home().mkdir(parents=True, exist_ok=True)
    hookroots.registry_path().write_bytes(junk)
    assert hookroots.registered("claude_code") == []
    p = transcript(tmp_path / "r")
    assert hookroots.record("claude_code", str(p)) == tmp_path / "r"  # rewritten cleanly
    assert hookroots.registered("claude_code") == [tmp_path / "r"]


def test_oversized_registry_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(hookroots, "MAX_FILE_BYTES", 50)
    core.home().mkdir(parents=True, exist_ok=True)
    hookroots.registry_path().write_text(json.dumps({"roots": {"claude_code": {str(tmp_path): 1}}}) + " " * 100)
    assert hookroots.registered("claude_code") == []


# --- adapters ----------------------------------------------------------------------------------------------------

def test_claude_adapter_reads_a_registered_root_outside_the_defaults(tmp_path, home):
    eph = tmp_path / "ephemeral-cfg"
    p = transcript(eph)
    me = Me(names=["Jo Dev"])
    assert ClaudeCode().discover(me, {}) == []  # not known yet
    hookroots.record("claude_code", str(p))
    locs = ClaudeCode().discover(me, {})
    assert [str(lc.path) for lc in locs] == [str(p)]
    units, _ = ClaudeCode().read(locs[0], None, me)
    assert [u.text for u in units] == [PROMPT]


def test_claude_adapter_dedupes_by_real_path_and_skips_vanished_roots(tmp_path, home, monkeypatch):
    real = tmp_path / "real-cfg"
    transcript(real)
    link = tmp_path / "link-cfg"
    link.symlink_to(real)
    hookroots.record("claude_code", str(transcript(link)))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(real))
    me = Me(names=["Jo Dev"])
    dirs = [str(lc.path) for lc in ClaudeCode().discover(me, {"dirs": [str(link)]})]
    assert len(dirs) == 1  # env dir, [claude_code].dirs and the registered root are one directory
    # a registered root that is gone is skipped silently, and so is a default that does not exist
    import shutil
    link.unlink()
    shutil.rmtree(real)
    assert ClaudeCode().discover(me, {}) == []


def test_codex_adapter_reads_a_registered_root(tmp_path, home):
    eph = tmp_path / "ephemeral-codex"
    p = rollout(eph)
    assert Codex().discover(Me(), {}) == []
    assert hookroots.record("codex", str(p)) == eph
    assert [str(lc.path) for lc in Codex().discover(Me(), {})] == [str(p)]


# --- the single-file sync ----------------------------------------------------------------------------------------

def test_sync_with_files_reads_only_those_files_and_keeps_last_sync_untouched(tmp_path, home):
    enable("claude_code")
    root = tmp_path / "home" / ".claude"  # the fake HOME's default root
    one = transcript(root, sid=SID)
    transcript(root, slug="-other", sid="22222222-aaaa-bbbb-cccc-333333333333", text="a second, different prompt here")
    res = sync.sync(quick=True, sources=["claude_code"], files=[str(one)])
    assert res["sources"]["claude_code"]["locations"] == 1 and res["new_units"] == 1
    assert stored_texts() == [PROMPT]
    con = store.connect()
    try:
        assert store.get_meta(con, "last_sync") is None  # a partial sync never makes a full one look recent
    finally:
        con.close()
    full = sync.sync(sources=["claude_code"])
    assert full["new_units"] == 1 and len(stored_texts()) == 2


def test_sync_with_files_stays_quiet_after_forget_all(tmp_path, home):
    enable("claude_code")
    p = transcript(tmp_path / "home" / ".claude")
    con = store.connect()
    try:
        store.set_meta(con, "forget_all_at", "2026-01-01T00:00:00+00:00")
    finally:
        con.close()
    res = sync.sync(sources=["claude_code"], files=[str(p)])
    assert res["status"] == "skipped" and stored_texts() == []
    assert sync.forget_all_pending()  # not cleared by the partial sync


def test_file_sync_process_waits_for_the_lock_but_not_forever(monkeypatch):
    alarms, sleeps, calls = [], [], []
    monkeypatch.setattr(sync.signal, "alarm", alarms.append, raising=False)
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)
    monkeypatch.setattr(sync, "sync", lambda *a, **k: calls.append(k) or {"status": "busy"})
    monkeypatch.setattr(sync, "FILE_SYNC_LOCK_WAIT_S", 0.0)
    assert sync.main(["--file", "/x", "--source", "claude_code"]) == 0
    assert alarms == [sync.FILE_SYNC_HARD_LIMIT_S] and len(calls) == 1 and calls[0]["files"] == ["/x"]
    calls.clear()
    results = iter([{"status": "busy"}, {"status": "busy"}, {"status": "ok"}])
    monkeypatch.setattr(sync, "sync", lambda *a, **k: calls.append(k) or next(results))
    monkeypatch.setattr(sync, "FILE_SYNC_LOCK_WAIT_S", 30.0)
    sync.main(["--file", "/x"])
    assert len(calls) == 3 and sleeps  # retried while busy, stopped when it ran


# --- the hooks ---------------------------------------------------------------------------------------------------

def payload(path, event="Stop", **extra):
    return json.dumps({"session_id": "s", "transcript_path": str(path) if path else None, "cwd": "/work/demo",
                       "hook_event_name": event, **extra}).encode()


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "sync_in_background", lambda *a, **k: calls.append((a, k)) or {"started": True})
    return calls


@pytest.mark.parametrize("event", ["stop", "session-end"])
def test_end_hook_registers_the_root_and_syncs_that_one_file(iso, tmp_path, monkeypatch, spawned, capsys, event):
    enable("claude_code")
    p = transcript(tmp_path / "eph")
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload(p))
    assert hooks.run(event) == 0
    assert spawned == [((), {"quick": True, "sources": ["claude_code"], "files": [str(p)],
                             "refresh": event == "session-end"})]
    assert hookroots.registered("claude_code") == [tmp_path / "eph"]
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_end_hook_handles_codex_rollouts(iso, tmp_path, monkeypatch, spawned):
    enable("codex")
    p = rollout(tmp_path / "eph-codex")
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload(p))
    assert hooks.run("stop") == 0
    assert spawned == [((), {"quick": True, "sources": ["codex"], "files": [str(p)], "refresh": False})]
    assert hookroots.registered("codex") == [tmp_path / "eph-codex"]


def test_end_hook_is_a_noop_when_not_set_up_or_the_source_is_off_or_forgotten(iso, tmp_path, monkeypatch, spawned):
    p = transcript(tmp_path / "eph")
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload(p))
    assert hooks.run("stop") == 0 and not spawned
    assert not core.home().exists()  # nothing created before `init`
    enable("codex")  # set up, but claude_code is not enabled
    assert hooks.run("stop") == 0 and not spawned and not hookroots.registry_path().exists()
    enable("claude_code")
    con = store.connect()
    try:
        store.set_meta(con, "forget_all_at", "2026-01-01T00:00:00+00:00")
    finally:
        con.close()
    assert hooks.run("stop") == 0 and not spawned and not hookroots.registry_path().exists()


def test_end_hook_skips_a_transcript_that_is_already_gone(iso, tmp_path, monkeypatch, spawned):
    enable("claude_code")
    p = transcript(tmp_path / "eph")
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload(p.with_name("missing.jsonl")))
    assert hooks.run("stop") == 0 and not spawned


GARBAGE = [b"", b"not json", b"[1,2]", b"null", b'{"transcript_path": 5}', b'{"transcript_path": null}',
           b'{"transcript_path": ""}', b'{"transcript_path": "/etc/passwd"}', b"\xff\xfe\x00",
           b'{"transcript_path": "/a/projects/s/\\u0000.jsonl"}', b'{"other": 1}']


def test_end_hooks_survive_garbage_payloads(iso, monkeypatch, spawned, capsys):
    enable("claude_code", "codex")
    for raw in GARBAGE:
        monkeypatch.setattr(hooks, "read_stdin", lambda *a, _r=raw, **k: _r)
        for event in ("stop", "session-end", "session-start"):
            assert hooks.run(event) == 0
    assert not hookroots.registry_path().exists()
    assert [k for _a, k in spawned if "files" in k] == []  # only session-start's plain quick syncs
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_end_hook_swallows_internal_errors(iso, tmp_path, monkeypatch):
    enable("claude_code")
    p = transcript(tmp_path / "eph")
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload(p))
    monkeypatch.setattr(sync, "sync_in_background", lambda *a, **k: 1 / 0)
    assert hooks.run("stop") == 0
    monkeypatch.setattr(hookroots, "record", lambda *a, **k: 1 / 0)
    assert hooks.run("stop") == 0


def test_session_start_registers_the_root_before_starting_the_sync(iso, tmp_path, monkeypatch):
    enable("claude_code")
    p = transcript(tmp_path / "eph")
    seen = []
    monkeypatch.setattr(sync, "sync_in_background",
                        lambda *a, **k: seen.append((k, hookroots.registered("claude_code"))) or {})
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload(p, "SessionStart", source="startup"))
    assert hooks.run("session-start") == 0
    assert seen == [({"quick": True}, [tmp_path / "eph"])]


def test_session_start_without_a_transcript_path_still_syncs(iso, monkeypatch, spawned):
    enable("claude_code")
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: b'{"source": "startup"}')
    assert hooks.run("session-start") == 0
    assert spawned == [((), {"quick": True})]


def test_prompt_hook_does_not_touch_the_registry(iso, tmp_path, monkeypatch):
    enable("claude_code")
    p = transcript(tmp_path / "eph")
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload(p, "UserPromptSubmit", prompt="hello there"))
    assert hooks.run("prompt") == 0
    assert not hookroots.registry_path().exists()


# --- end to end: a throwaway config dir that vanishes right after the hook -----------------------------------------

def test_stop_hook_stores_the_prompts_of_a_throwaway_config_dir(iso, tmp_path):
    import shutil
    enable("claude_code")
    eph = tmp_path / "ephemeral-cfg"
    p = transcript(eph)
    env = {**iso, "PATH": "/usr/bin:/bin"}
    t0 = time.perf_counter()
    r = subprocess.run([sys.executable, "-m", "mannerism", "hook", "stop"], input=payload(p), capture_output=True,
                       env=env, timeout=20)
    took = time.perf_counter() - t0
    assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert took < 5, took  # the hook returns at once; the sync runs detached
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and PROMPT not in stored_texts():
        time.sleep(0.2)
    shutil.rmtree(eph)  # the session is over and its config dir with it
    assert stored_texts() == [PROMPT]
    assert hookroots.registered("claude_code") == []  # the vanished root is skipped, not an error
    assert ClaudeCode().discover(Me(), {}) == []


def test_subprocess_end_hooks_exit_zero_silently_on_garbage(iso):
    env = {**iso, "PATH": "/usr/bin:/bin"}
    for event in ("stop", "session-end"):
        for raw in (b"", b"garbage", b'{"transcript_path": "/x/projects/s/a.jsonl"}'):
            r = subprocess.run([sys.executable, "-m", "mannerism", "hook", event], input=raw, capture_output=True,
                               env=env, timeout=20)  # not set up yet
            assert (r.returncode, r.stdout, r.stderr) == (0, b"", b"")
    assert not core.home().exists()
