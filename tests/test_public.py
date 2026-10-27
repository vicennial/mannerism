from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

import pytest
from conftest import ingest, unit

from mannerism import api, core, public, public_server, refresh, remote, store


@pytest.fixture
def projected():
    con = store.connect()
    ingest(con, [unit("a published observation about making small tools useful every single day", "public", ref="p",
                      source="x", kind="post", meta={"public": True}),
                 unit("SecretCanary is our private project and must never be an example outside this machine",
                      "close", ref="secret"),
                 unit("this unreleased public draft is not published and must stay private forever", "public", ref="draft")])
    public.build_snapshot(con)
    yield con
    con.close()


def test_projection_has_only_published_evidence(projected):
    ctx = public.context()
    assert len(ctx["examples"]) == 1
    assert "published observation" in ctx["text"]
    assert "SecretCanary" not in ctx["text"] and "unreleased" not in ctx["text"]
    con = public.open_snapshot()
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert not ({"units", "drafts", "edits", "rules", "rejections"} & tables)
        assert json.loads(store.get_meta(con, "stats"))["n"] == 1
    finally:
        con.close()
    assert public.snapshot_path().stat().st_mode & 0o777 == 0o600


def test_public_reads_never_open_private_store_or_leak_index(projected, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("private read")
    monkeypatch.setattr(store, "connect", forbidden)
    monkeypatch.setattr("mannerism.stats.load_leak_index", forbidden)
    assert public.context()["ok"]
    assert public.status()["posts"] == 1
    assert public.check("SecretCanary is something I wrote myself today", is_draft=False)["ok"]


def test_private_changes_do_not_change_public_results(projected):
    before = public.context()
    check = public.check("SecretCanary is mentioned in this draft", is_draft=False)
    ingest(projected, [unit("SecretCanary private text with completely different writing styles", "work", ref="more")])
    assert public.context() == before
    assert public.check("SecretCanary is mentioned in this draft", is_draft=False) == check


def test_snapshot_revocation_and_forget(projected):
    projected.execute("UPDATE units SET held=1 WHERE source='x'")
    refresh.refresh(projected)
    assert public.status()["posts"] == 0
    assert api.forget(all=True)["ok"]
    assert not public.snapshot_path().exists()


def test_forget_revokes_snapshot_even_if_early_refresh_fails(projected, monkeypatch):
    def fail(*args):
        raise RuntimeError("synthetic feature failure")
    with monkeypatch.context() as patch:
        patch.setattr("mannerism.stylometrics.compute_missing", fail)
        with pytest.raises(RuntimeError):
            api.forget(source="x")
    assert not public.snapshot_path().exists()
    with pytest.raises(ValueError, match="successful corpus refresh"):
        public.build_snapshot(projected)
    refresh.refresh(projected)
    assert public.status()["posts"] == 0


def test_unit_update_revokes_before_refresh_can_start(projected):
    changed = unit("a changed published observation about making better small tools today", "public", ref="p",
                   source="x", kind="post", meta={"public": True})
    store.write_units(projected, "x", "synthetic", [changed], None)
    assert not public.snapshot_path().exists()
    with pytest.raises(ValueError):
        public.build_snapshot(projected)
    refresh.refresh(projected)
    assert "changed published" in public.context()["text"]


def test_source_forget_erases_unlinked_feedback_and_rejections(projected):
    result = api.feedback("An old draft with a rather long synthetic phrase", "A completely different final text", "public")
    eid = projected.execute("SELECT edit_id FROM edits").fetchone()[0]
    assert result["ok"]
    api.feedback("A rejected old draft", "No thank you", "public", rejected=True)
    rid = projected.execute("SELECT rejection_id FROM rejections").fetchone()[0]
    projected.executemany("INSERT INTO feedback_sources VALUES (?,?,?,?,?,?)",
                         [("edit", eid, "x", "missing-post", None, 1), ("rejection", rid, "x", "missing-post", None, 0)])
    assert api.forget(source="x")["ok"]
    assert projected.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert projected.execute("SELECT count(*) FROM rejections").fetchone()[0] == 0
    assert projected.execute("SELECT count(*) FROM feedback_sources").fetchone()[0] == 0


def test_ai_and_unpublished_edits_are_not_exported(projected):
    uid = projected.execute("SELECT unit_id FROM units WHERE source='x'").fetchone()[0]
    projected.execute("INSERT INTO ai_units VALUES (?, 1, ?)", (uid, store.now_iso()))
    public.build_snapshot(projected)
    assert public.status()["posts"] == 0


@pytest.mark.parametrize("fn,args", [(public.context, ("close",)),
                                     (public.check, ("some text", "work")),
                                     (public.feedback, ("draft", "final", "agent"))])
def test_other_registers_refused(projected, fn, args):
    with pytest.raises(ValueError):
        fn(*args)


def test_signatures_bind_method_arguments_and_survive_restart(projected):
    path = public.ensure_token()
    token = public.read_token(path)
    args = {"text": "a public draft with a few words", "register": "public", "is_draft": True}
    headers = {k.lower(): v for k, v in public.signed_headers(token, "voice_lint", args).items()}
    with pytest.raises(PermissionError):
        public.authorize(headers, "voice_feedback", args)
    with pytest.raises(PermissionError):
        public.authorize(headers, "voice_lint", {**args, "text": "changed"})
    public.authorize(headers, "voice_lint", args)
    with pytest.raises(PermissionError):
        public.authorize(headers, "voice_lint", args)
    code = """import json,sys
from mannerism import public
headers,args=json.load(sys.stdin)
try:
    public.authorize(headers,'voice_lint',args)
except PermissionError:
    sys.exit(0)
sys.exit(1)
"""
    replay = subprocess.run([sys.executable, "-c", code], input=json.dumps([headers, args]),
                            text=True, capture_output=True, timeout=15)
    assert replay.returncode == 0


def test_concurrent_signed_replay_has_exactly_one_winner(projected):
    from concurrent.futures import ThreadPoolExecutor
    public.ensure_token()
    args = {"text": "a synthetic draft", "register": "public", "is_draft": True}
    headers = {k.lower(): v for k, v in public.signed_headers(public.read_token(public.token_path()), "voice_lint", args).items()}
    def attempt():
        try:
            public.authorize(headers, "voice_lint", args)
            return True
        except PermissionError:
            return False
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _: attempt(), range(4))).count(True) == 1
    headers["x-mannerism-ts"] = "1"
    with pytest.raises(PermissionError):
        public.authorize(headers, "voice_lint", args)


def test_rejected_feedback_never_becomes_evidence(projected):
    args = ("Here is a draft I do not want to use at all", "no thanks", "public")
    assert api.feedback(*args, rejected=True)["created"]
    assert not api.feedback(*args, rejected=True)["created"]
    assert projected.execute("SELECT count(*) FROM rejections").fetchone()[0] == 1
    assert projected.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert api.forget(drafts=True)["ok"]
    assert projected.execute("SELECT count(*) FROM rejections").fetchone()[0] == 0


def test_public_tools_are_narrow_and_unsigned_writes_fail(projected):
    mcp = public_server.build()
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == {"voice_context", "voice_status", "voice_lint", "voice_feedback"}
    async def call():
        return await mcp.call_tool("voice_lint", {"text": "this draft has enough words for the store"})
    result = asyncio.run(call())
    assert "authorized" in str(result)
    assert projected.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0


@pytest.mark.parametrize("value", ["https://example.org/mcp", "http://example.org/mcp", "http://user@localhost/mcp",
                                   "file:///tmp/mcp", "http://127.0.0.1:3345/mcp?redirect=1"])
def test_remote_refuses_non_tunnel_endpoints(monkeypatch, value):
    monkeypatch.setenv("MANNERISM_REMOTE_URL", value)
    with pytest.raises(ValueError):
        remote.url()


def test_remote_mode_cannot_initialize_local_corpus(monkeypatch):
    from mannerism.cli import main
    monkeypatch.setenv("MANNERISM_REMOTE_URL", "http://127.0.0.1:3345/mcp")
    assert main(["init"]) == 2
    assert not (core.home() / "corpus.db").exists()


def test_remote_hooks_never_capture(monkeypatch):
    from mannerism.cli import HOOK_EVENTS, main
    monkeypatch.setenv("MANNERISM_REMOTE_URL", "http://127.0.0.1:3345/mcp")
    def forbidden(*args):
        raise AssertionError("remote hook ran")
    monkeypatch.setattr("mannerism.hooks.run", forbidden)
    for event in HOOK_EVENTS:
        assert main(["hook", event]) == 0
    assert not (core.home() / "corpus.db").exists()


def test_token_rejects_non_regular_or_shared_files(tmp_path):
    token = tmp_path / "token"
    public.ensure_token(token)
    link = tmp_path / "link"
    link.symlink_to(token)
    with pytest.raises(PermissionError):
        public.read_token(link)
    with pytest.raises(PermissionError):
        public.read_token(tmp_path)
    token.chmod(0o644)
    with pytest.raises(PermissionError):
        public.read_token(token)


@pytest.mark.smoke
def test_real_http_signed_client_and_public_only_readback(projected, monkeypatch, tmp_path):
    public.ensure_token()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    monkeypatch.setenv("MANNERISM_REMOTE_URL", f"http://127.0.0.1:{port}/mcp")
    env = {k: v for k, v in os.environ.items() if k != "MANNERISM_REMOTE_URL"}
    with (tmp_path / "server.log").open("w+") as log:
        proc = subprocess.Popen([sys.executable, "-m", "mannerism", "public", "serve", "--port", str(port)],
                                env=env, stdout=log, stderr=log)
        try:
            for _ in range(100):
                if proc.poll() is not None:
                    log.seek(0)
                    pytest.fail(log.read())
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        break
                except OSError:
                    time.sleep(0.05)
            assert remote.call("voice_status")["posts"] == 1
            assert remote.call("voice_context", {"register": "public", "task": "tools", "k": 3})["ok"]
            args = {"text": "This is a public draft that I might publish after revising it", "register": "public", "is_draft": True}
            assert remote.call("voice_lint", args)["logged"]
            assert projected.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
            assert remote.call("voice_lint", {"text": "A second synthetic draft checks signed default arguments"})["logged"]
            feedback = {"draft": args["text"], "final": "no thanks", "register": "public", "rejected": True}
            assert remote.call("voice_feedback", feedback)["status"] == "rejected"
            with pytest.raises(ValueError):
                remote.call("voice_context", {"register": "work"})
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
