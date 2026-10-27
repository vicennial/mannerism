import json
import sqlite3
from pathlib import Path

import pytest

from mannerism.adapters.crush import Crush

from .conftest import run, write_jsonl
from .sqlite_helpers import install_fixture, main_file_tables, wal_snapshot

T0 = 1_792_618_000  # epoch seconds, 2026


@pytest.fixture(autouse=True)
def _no_crush_env(monkeypatch):
    for var in ("XDG_DATA_HOME", "CRUSH_GLOBAL_DATA", "LOCALAPPDATA"):
        monkeypatch.delenv(var, raising=False)


SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, title TEXT NOT NULL, channel TEXT);
CREATE TABLE messages (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, parts TEXT NOT NULL DEFAULT '[]',
    created_at INTEGER NOT NULL, is_summary_message INTEGER DEFAULT 0 NOT NULL);
"""


def session(con, sid, parent=None, channel=None):
    con.execute("INSERT INTO sessions VALUES (?,?,?,?)", (sid, parent, "t", channel))


def txt(t, **kw):
    return {"type": "text", "data": {"text": t, **kw}}


def msg(con, mid, sid, role, parts, at=0, summary=0):
    if isinstance(parts, str):
        parts = [txt(parts)]
    con.execute("INSERT INTO messages VALUES (?,?,?,?,?,?)", (mid, sid, role, json.dumps(parts), T0 + at, summary))


def make_db(path, build):
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    build(con)
    con.commit()
    return con


def test_crush_real_0_98_run(home, me, tmp_path):
    """Database written by `crush run` 0.98.1 (four prompts in one session) against a mock model."""
    proj = home / "work" / "crush"
    db = install_fixture("crush", "crush.db", proj / ".crush" / "crush.db")
    reg = home / ".local" / "share" / "crush" / "projects.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps({"projects": [{"path": str(proj), "data_dir": str(proj / ".crush")}]}), "utf-8")
    src = Crush()
    locs = src.discover(me, {})
    assert [lc.path for lc in locs] == [db] and locs[0].label == "Crush project crush"
    units, cur = run(src, locs[0], None, me)
    assert [u.text for u in units] == [
        "plan a small herb garden for a balcony",
        "make the plan cheaper and favour basil",
        "@notes.txt what do these notes say?",
        "/init",  # `run` does not treat it as a command: the text reached the model as typed
    ]
    assert all((u.kind, u.register, u.audience) == ("prompt", "agent", "agent") for u in units)
    assert units[0].context == ()
    assert all(u.context[0].who == "agent" and u.context[0].text.startswith("Sure, I can help") for u in units[1:])
    assert len({u.thread_id for u in units}) == 1 and units[0].meta["cwd_hash"] and units[0].ts.year == 2026
    assert cur["msgs"]["rowid"] == 8 and cur["sig"]
    assert run(src, locs[0], cur, me)[0] == []


def test_crush_filters_and_incremental(home, me):
    def build(con):
        session(con, "s1")
        session(con, "task", parent="s1")
        session(con, "title-s1", parent="s1")
        session(con, "chan", channel="server:webhook")
        msg(con, "m1", "s1", "user", "wire the retry into the http client", 1)
        msg(con, "m2", "s1", "assistant", [txt("Done. Ping ana@example.org"), {"type": "finish", "data": {}}], 2)
        msg(con, "m3", "s1", "user", [txt("keep going", hidden=True)], 3)  # generated continuation
        msg(con, "m4", "s1", "user", [{"type": "shell_command", "data": {"command": "ls", "output": "x"}}], 4)
        msg(con, "m5", "s1", "tool", [{"type": "tool_result", "data": {"content": "ok"}}], 5)
        msg(
            con,
            "m6",
            "s1",
            "user",
            [txt("first line"), txt("second line"), {"type": "image_url", "data": {"url": "x"}}],
            6,
        )
        msg(con, "m7", "s1", "user", "and a second prompt before any reply", 7)
        msg(con, "m8", "s1", "assistant", "Summary of the conversation so far", 8, summary=1)
        msg(con, "m9", "s1", "user", "   ", 9)
        msg(con, "mt", "task", "user", "sub-agent task text written by the model", 10)
        msg(con, "mg", "title-s1", "user", "Generate a title for this conversation: ...", 10)
        msg(con, "mc", "chan", "user", "a message pushed in by an MCP channel", 10)

    p = home / "work" / "app" / ".crush" / "crush.db"
    con = make_db(p, build)
    src = Crush()
    loc = src.discover(me, {"paths": [str(p)]})[0]
    units, cur = run(src, loc, None, me)
    assert [u.text for u in units] == [
        "wire the retry into the http client",
        "first line\n\nsecond line",
        "and a second prompt before any reply",
    ]
    assert "[email]" in units[1].context[0].text and "ana@" not in repr(cur)
    assert units[2].context == ()  # another typed prompt came first; the summary is not a reply

    msg(con, "m10", "s1", "assistant", "Merged it.", 11)
    msg(con, "m11", "s1", "user", "great, thanks", 12)
    con.commit()
    units, cur2 = run(src, loc, cur, me)
    assert [u.text for u in units] == ["great, thanks"] and units[0].context[0].text == "Merged it."
    assert run(src, loc, cur2, me)[0] == []
    # a cursor from another database (the row at its rowid is different): read from the top
    assert len(run(src, loc, {"msgs": {"rowid": 2, "id": "elsewhere"}}, me)[0]) == 4


def test_crush_wal_only_rows(home, me):
    def build(con):
        con.executescript(SCHEMA)
        session(con, "s1")
        msg(con, "m1", "s1", "user", "written only to the wal so far", 1)

    p = wal_snapshot(build, home / "work" / "app" / ".crush" / "crush.db")
    assert main_file_tables(p) == []
    loc = Crush().discover(me, {"paths": [str(p)]})[0]
    units, _ = run(Crush(), loc, None, me)
    assert [u.text for u in units] == ["written only to the wal so far"]


def test_crush_discover_registry_config_and_transcripts(home, me, tmp_path):
    src = Crush()
    assert src.discover(me, {}) == [] and src.discover(me, None) == []
    (home / ".local" / "share" / "crush").mkdir(parents=True)
    reg = home / ".local" / "share" / "crush" / "projects.json"

    reg.write_text("{broken", "utf-8")
    assert src.discover(me, {}) == []
    reg.write_text(json.dumps({"projects": [{"path": 3}, None, {"data_dir": 5}, {"data_dir": ""}]}), "utf-8")
    assert src.discover(me, {}) == []

    # 1. registry: a default data dir, one moved with --data-dir, and a stale entry whose project was deleted
    a = make_db(home / "code" / "a" / ".crush" / "crush.db", lambda c: None)
    moved = make_db(tmp_path / "elsewhere" / "state" / "crush.db", lambda c: None)
    reg.write_text(
        json.dumps(
            {
                "projects": [
                    {"path": str(home / "code" / "a"), "data_dir": str(home / "code" / "a" / ".crush")},
                    {"path": str(home / "code" / "b"), "data_dir": str(tmp_path / "elsewhere" / "state")},
                    {"path": str(home / "code" / "gone"), "data_dir": str(home / "code" / "gone" / ".crush")},
                ]
            }
        ),
        "utf-8",
    )
    a.close()
    moved.close()
    found = {lc.path for lc in src.discover(me, {})}
    assert found == {home / "code" / "a" / ".crush" / "crush.db", tmp_path / "elsewhere" / "state" / "crush.db"}

    # 2. configured paths: a project dir, a data dir, a database file; duplicates collapse
    for name in ("c", "d", "e"):
        make_db(home / "code" / name / ".crush" / "crush.db", lambda c: None).close()
    cfg = {
        "paths": [
            str(home / "code" / "c"),
            str(home / "code" / "d" / ".crush"),
            str(home / "code" / "e" / ".crush" / "crush.db"),
            str(home / "code" / "a"),
            None,
            "/nonexistent/x",
        ]
    }
    assert len(src.discover(me, cfg)) == 5

    # 3. directories of recent agent sessions: the project root is found from a subdirectory, climbing stops at ~
    make_db(home / "code" / "f" / ".crush" / "crush.db", lambda c: None).close()
    outside = tmp_path / "outside"
    make_db(outside / ".crush" / "crush.db", lambda c: None).close()
    make_db(tmp_path / ".crush" / "crush.db", lambda c: None).close()  # a shared parent above ~ is not a project
    write_jsonl(
        home / ".claude" / "projects" / "p" / "s1.jsonl",
        [
            {
                "type": "user",
                "cwd": str(home / "code" / "f" / "src" / "pkg"),
                "message": {"role": "user", "content": "x"},
            }
        ],
    )
    write_jsonl(
        home / ".claude" / "projects" / "p" / "s2.jsonl",
        [{"type": "user", "cwd": str(outside / "deep"), "message": {"role": "user", "content": "x"}}],
    )
    write_jsonl(
        home / ".claude" / "projects" / "p" / "s3.jsonl",
        [{"type": "user", "cwd": str(outside), "message": {"role": "user", "content": "x"}}],
    )
    locs = src.discover(me, cfg)
    assert home / "code" / "f" / ".crush" / "crush.db" in [lc.path for lc in locs]
    assert outside / ".crush" / "crush.db" in [lc.path for lc in locs]  # the cwd itself, outside ~
    assert tmp_path / ".crush" / "crush.db" not in [lc.path for lc in locs]
    assert len(locs) == 7


def test_crush_global_data_locations(home, me, monkeypatch, tmp_path):
    proj = make_db(home / "code" / "a" / ".crush" / "crush.db", lambda c: None)
    proj.close()

    def registry(root):
        root.mkdir(parents=True, exist_ok=True)
        (root / "projects.json").write_text(
            json.dumps({"projects": [{"path": "x", "data_dir": str(home / "code" / "a" / ".crush")}]}), "utf-8"
        )

    src = Crush()
    registry(tmp_path / "xdg" / "crush")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert len(src.discover(me, {})) == 1
    monkeypatch.delenv("XDG_DATA_HOME")
    assert src.discover(me, {}) == []
    registry(tmp_path / "global")
    monkeypatch.setenv("CRUSH_GLOBAL_DATA", str(tmp_path / "global"))
    assert len(src.discover(me, {})) == 1


def test_crush_unreadable_database_is_not_fatal(home, me):
    p = home / "work" / "app" / ".crush" / "crush.db"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not a database " * 20)
    src = Crush()
    loc = src.discover(me, {"paths": [str(p)]})[0]
    units, cur = src.read(loc, {"max_rowid": 4}, me)
    assert list(units) == [] and cur == {"max_rowid": 4}


def _app(home, build):
    p = home / "work" / "app" / ".crush" / "crush.db"
    con = make_db(p, build)
    return p, con


def test_crush_undo_style_tail_delete_reuses_rowids_and_loses_nothing(home, me):
    """P1-5: messages has a TEXT primary key, so rowids of deleted tail rows are handed out again."""

    def build(con):
        session(con, "s1")
        msg(con, "m1", "s1", "user", "first prompt in the session", 1)
        msg(con, "m2", "s1", "assistant", "reply one", 2)
        msg(con, "m3", "s1", "user", "second prompt in the session", 3)
        msg(con, "m4", "s1", "assistant", "reply two", 4)

    p, con = _app(home, build)
    src = Crush()
    loc = src.discover(me, {"paths": [str(p)]})[0]
    units, cur = run(src, loc, None, me)
    assert len(units) == 2 and cur["msgs"]["rowid"] == 4
    con.execute("DELETE FROM messages WHERE id IN ('m3', 'm4')")  # the tail turn is rolled back ...
    msg(con, "m5", "s1", "user", "the prompt typed after the rollback", 5)  # ... and its rowids come back
    msg(con, "m6", "s1", "assistant", "reply three", 6)
    con.commit()
    assert con.execute("SELECT max(rowid) FROM messages").fetchone()[0] == 4  # the max did not move
    units, cur2 = run(src, loc, cur, me)
    assert "the prompt typed after the rollback" in [u.text for u in units]
    assert {u.source_ref for u in units} <= {"m1", "m5"}  # a re-read of m1 is deduped by the engine
    assert run(src, loc, cur2, me)[0] == []


def test_crush_recreated_database_that_outgrows_the_old_max(home, me):
    def old(con):
        session(con, "s1")
        msg(con, "m1", "s1", "an old prompt", 1)
        msg(con, "m2", "s1", "assistant reply", 2)

    p, con = _app(home, old)
    con.close()
    src = Crush()
    loc = src.discover(me, {"paths": [str(p)]})[0]
    _, cur = run(src, loc, None, me)
    for f in p.parent.glob("crush.db*"):
        f.unlink()

    def new(con):
        session(con, "s9")
        for i in range(3):
            msg(con, f"n{i}", "s9", "user", f"new prompt {i}", 10 + 2 * i)
            msg(con, f"r{i}", "s9", "assistant", "ok", 11 + 2 * i)

    _, con = _app(home, new)  # six rows: past the old maximum of 2
    con.close()
    units, _ = run(src, loc, cur, me)
    assert [u.text for u in units] == [f"new prompt {i}" for i in range(3)]


def test_crush_unchanged_database_is_not_copied_again(home, me, monkeypatch):
    from mannerism.adapters import crush as mod

    def build(con):
        session(con, "s1")
        msg(con, "m1", "s1", "user", "read me once", 1)

    p, con = _app(home, build)
    con.close()
    src = Crush()
    loc = src.discover(me, {"paths": [str(p)]})[0]
    _, cur = run(src, loc, None, me)

    def boom(*a, **k):
        raise AssertionError("an unchanged database must not be copied")

    monkeypatch.setattr(mod, "DbCopy", boom)
    assert run(src, loc, cur, me) == ([], cur)


def test_crush_non_interactive_titled_sessions_and_relays_are_dropped(home, me):
    def build(con):
        con.execute("INSERT INTO sessions VALUES ('run1', NULL, 'non-interactive', NULL)")
        session(con, "s1")
        msg(con, "m0", "run1", "user", "prompt given to crush run", 1)
        msg(con, "m1", "s1", "user", "<task-notification>done</task-notification>", 2)
        msg(con, "m2", "s1", "user", "a prompt typed in the TUI", 3)

    p, con = _app(home, build)
    con.close()
    src = Crush()
    units, _ = run(src, src.discover(me, {"paths": [str(p)]})[0], None, me)
    assert [u.text for u in units] == ["a prompt typed in the TUI"]


def test_crush_one_unreadable_cwd_does_not_empty_discovery(home, me, monkeypatch):
    """P2-6: is_file() raising (EACCES, a dead mount) for one transcript cwd must not lose the other projects."""
    good = home / "good"
    make_db(good / ".crush" / "crush.db", lambda c: None).close()
    reg = home / ".local" / "share" / "crush" / "projects.json"
    reg.parent.mkdir(parents=True)
    reg.write_text(json.dumps({"projects": [{"path": str(good), "data_dir": str(good / ".crush")}]}), "utf-8")
    locked = home / "locked" / "sub"
    locked.mkdir(parents=True)
    write_jsonl(home / ".claude" / "projects" / "-x" / "s.jsonl", [{"type": "user", "cwd": str(locked)}])
    write_jsonl(home / ".claude" / "projects" / "-y" / "s.jsonl", [{"type": "user", "cwd": str(good)}])
    real = Path.is_file

    def is_file(self, *a, **k):
        if "locked" in self.parts:
            raise PermissionError(13, "Permission denied", str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "is_file", is_file)
    assert [lc.path for lc in Crush().discover(me, {})] == [good / ".crush" / "crush.db"]
