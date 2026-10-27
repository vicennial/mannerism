"""Hooks: silent, always exit 0, fast. The latency tests print the measured numbers (run with -s to see them)."""
from __future__ import annotations

import hashlib
import json
import random
import sqlite3
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta

from mannerism import config, hooks, store, sync

WORDS = ("plan review ship notes budget draft vendor invoice launch update timeline owner risk metric deploy "
         "customer feedback roadmap meeting summary decision next steps follow release checklist").split()
BUDGET_IN_PROCESS_S = 0.3
BUDGET_SUBPROCESS_S = 1.5


def set_up_home() -> None:
    config.ensure_home()
    config.save_config(config.Config())


def seed_drafts(n: int) -> list[str]:
    """n drafts of 25-45 words, all inside the 48 h prompt window, written straight into the log."""
    rng = random.Random(3)
    now = datetime.now(UTC)
    texts, rows = [], []
    for i in range(n):
        text = " ".join(rng.choice(WORDS) for _ in range(rng.randint(25, 45))) + f" {i}"
        texts.append(text)
        ts = store.ts_iso(now - timedelta(seconds=i * 60))
        rows.append((ts, "work", "mcp", hashlib.sha256(text.encode()).hexdigest(), text))
    con = store.connect()
    try:
        with store.tx(con):
            con.executemany("INSERT INTO drafts(ts, register, origin, sha256, text) VALUES (?,?,?,?,?)", rows)
    finally:
        con.close()
    return texts


def payload(prompt: str) -> bytes:
    return json.dumps({"session_id": "s", "hook_event_name": "UserPromptSubmit", "prompt": prompt}).encode()


def edited_prompt(draft: str) -> str:
    words = draft.split()
    words[3] = "changed"
    words[10] = "again"
    return "Here is what I actually sent:\n\n" + " ".join(words) + "\n\nplease learn from it"


def run_hook(event: str, stdin: bytes, env: dict, timeout=10) -> tuple[subprocess.CompletedProcess, float]:
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, "-m", "mannerism", "hook", event], input=stdin, capture_output=True,
                       env={**env, "PATH": "/usr/bin:/bin"}, timeout=timeout)
    return p, time.perf_counter() - t0


# --- behaviour -------------------------------------------------------------------------------------------------

def test_prompt_hook_records_an_edit_and_is_silent(iso, monkeypatch, capsys):
    set_up_home()
    drafts = seed_drafts(5)
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload(edited_prompt(drafts[2])))
    assert hooks.run("prompt") == 0
    assert capsys.readouterr().out == ""
    con = store.connect()
    try:
        assert con.execute("SELECT count(*) FROM edits WHERE origin='prompt'").fetchone()[0] == 1
    finally:
        con.close()


def test_prompt_hook_survives_bad_input(iso, monkeypatch, capsys):
    set_up_home()
    for raw in (b"", b"not json", b"[1,2]", b'{"prompt": 5}', b'{"prompt": ""}', b"\xff\xfe\x00", b'{"other": 1}'):
        monkeypatch.setattr(hooks, "read_stdin", lambda *a, _r=raw, **k: _r)
        assert hooks.run("prompt") == 0
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err == ""


def test_hooks_do_nothing_before_setup(iso, monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "sync_in_background", lambda *a, **k: calls.append(1))
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload("some prompt with enough words to match anything"))
    assert hooks.run("session-start") == 0 and hooks.run("prompt") == 0
    assert not calls
    assert not (iso["MANNERISM_HOME"] and __import__("pathlib").Path(iso["MANNERISM_HOME"]).exists())


def test_session_start_starts_one_quick_background_sync(iso, monkeypatch, capsys):
    set_up_home()
    calls = []
    monkeypatch.setattr(sync, "sync_in_background", lambda *a, **k: calls.append((a, k)) or {})
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: b'{"source": "startup"}')
    assert hooks.run("session-start") == 0
    assert calls == [((), {"quick": True})]
    assert capsys.readouterr().out == ""


def test_hooks_swallow_internal_errors(iso, monkeypatch):
    set_up_home()
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: payload("a prompt with plenty of words in it, really"))
    monkeypatch.setattr("mannerism.capture.match_prompt", lambda *a, **k: 1 / 0)
    assert hooks.run("prompt") == 0
    monkeypatch.setattr(sync, "sync_in_background", lambda *a, **k: 1 / 0)
    assert hooks.run("session-start") == 0


def test_read_stdin_is_bounded(tmp_path, monkeypatch):
    big = tmp_path / "big"
    big.write_bytes(b"x" * 5000)
    monkeypatch.setattr(sys, "stdin", open(big, "rb"))
    assert len(hooks.read_stdin(max_bytes=1000)) == 1000


# --- subprocess behaviour ----------------------------------------------------------------------------------------

def test_subprocess_hooks_exit_zero_silently(iso):
    for stdin in (b"", b"garbage", payload("hello")):  # not set up yet
        for event in ("prompt", "session-start"):
            p, _ = run_hook(event, stdin, iso)
            assert (p.returncode, p.stdout, p.stderr) == (0, b"", b"")
    set_up_home()
    for event in ("prompt", "session-start"):
        p, _ = run_hook(event, b"garbage", iso)
        assert (p.returncode, p.stdout, p.stderr) == (0, b"", b"")
    assert hooks.is_set_up()


def test_prompt_hook_with_locked_database_exits_zero_quickly(iso):
    set_up_home()
    drafts = seed_drafts(20)
    lock = sqlite3.connect(iso["MANNERISM_HOME"] + "/corpus.db", timeout=1, isolation_level=None)
    lock.execute("BEGIN IMMEDIATE")  # another process holds the write lock
    try:
        p, took = run_hook("prompt", payload(edited_prompt(drafts[0])), iso)
    finally:
        lock.execute("ROLLBACK")
        lock.close()
    assert (p.returncode, p.stdout) == (0, b"")
    assert took < BUDGET_SUBPROCESS_S, took


# --- latency -----------------------------------------------------------------------------------------------------

def test_prompt_hook_latency_in_process_with_2000_drafts(iso, monkeypatch):
    set_up_home()
    drafts = seed_drafts(2000)
    unmatched = " ".join(WORDS[i % len(WORDS)] for i in range(120)) + "\n\n" + " ".join(WORDS[::-1] * 3)
    cases = {"matching an old draft": edited_prompt(drafts[110]), "no match": unmatched, "short": "ok thanks"}
    worst = 0.0
    for name, prompt in cases.items():
        monkeypatch.setattr(hooks, "read_stdin", lambda *a, _p=prompt, **k: payload(_p))
        runs = []
        for _ in range(3):  # best of three: this measures our work, not a noisy CI neighbour
            t0 = time.perf_counter()
            assert hooks.run("prompt") == 0
            runs.append(time.perf_counter() - t0)
        took = min(runs)
        print(f"hook prompt in-process, 2000 drafts, {name}: {took * 1000:.0f} ms")
        worst = max(worst, took)
    assert worst < BUDGET_IN_PROCESS_S, worst


def test_prompt_hook_latency_as_subprocess_with_2000_drafts(iso):
    set_up_home()
    drafts = seed_drafts(2000)
    run_hook("prompt", payload("warm the file cache with a prompt"), iso)  # python's own import cache, not ours
    times = []
    for prompt in (edited_prompt(drafts[50]), "something unrelated, but long enough to be compared with the drafts"):
        p, took = run_hook("prompt", payload(prompt), iso)
        assert (p.returncode, p.stdout, p.stderr) == (0, b"", b"")
        times.append(took)
    print(f"hook prompt subprocess, 2000 drafts: {[round(t * 1000) for t in times]} ms")
    assert max(times) < BUDGET_SUBPROCESS_S, times
