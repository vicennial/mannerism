import json
import sqlite3

import pytest

from mannerism.adapters.goose import Goose, strip_command

from .conftest import run
from .sqlite_helpers import install_fixture, main_file_tables, wal_snapshot

T0 = 1_792_618_000  # epoch seconds, 2026


@pytest.fixture(autouse=True)
def _no_goose_env(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("GOOSE_PATH_ROOT", raising=False)


def db_path(home):
    return home / ".local" / "share" / "goose" / "sessions" / "sessions.db"


SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', session_type TEXT NOT NULL DEFAULT 'user',
    working_dir TEXT NOT NULL, schedule_id TEXT, recipe_json TEXT, parent_session_id TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT, session_id TEXT NOT NULL,
    role TEXT NOT NULL, content_json TEXT NOT NULL, created_timestamp INTEGER NOT NULL, metadata_json TEXT);
"""
VISIBLE = {"userVisible": True, "agentVisible": True}


def session(con, sid, typ="user", **kw):
    con.execute(
        "INSERT INTO sessions (id, session_type, working_dir, schedule_id, recipe_json, parent_session_id) "
        "VALUES (?,?,?,?,?,?)",
        (sid, typ, "/home/jo.dev/work/app", kw.get("schedule"), kw.get("recipe"), kw.get("parent")),
    )


def msg(con, sid, role, content, at=0, meta=None, mid=None):
    if isinstance(content, str):
        content = [{"type": "text", "text": content}]
    con.execute(
        "INSERT INTO messages (message_id, session_id, role, content_json, created_timestamp, metadata_json) "
        "VALUES (?,?,?,?,?,?)",
        (mid, sid, role, json.dumps(content), T0 + at, json.dumps(VISIBLE if meta is None else meta)),
    )


def test_goose_real_1_55_run(home, me):
    """Database written by goose 1.55.0 (`goose run`, three turns plus /compact, and one --no-session run)."""
    p = install_fixture("goose", "sessions.db", db_path(home))
    src = Goose()
    locs = src.discover(me, {})
    assert [lc.path for lc in locs] == [p]
    units, cur = run(src, locs[0], None, me)
    # the --no-session run is a `hidden` session, /compact is a command, the <turn-context> rows are injected
    assert [u.text for u in units] == [
        "draft a short note thanking the garden club",
        "make it warmer and mention the tulips",
        "ok thanks, now list three tulip colours",
    ]
    assert all((u.kind, u.register, u.audience) == ("prompt", "agent", "agent") for u in units)
    assert units[0].context == ()
    assert units[1].context[0].who == "agent" and units[1].context[0].text.startswith("Sure, I can help")
    assert units[0].source_ref.startswith("msg_") and units[0].meta["cwd_hash"]
    assert len({u.thread_id for u in units}) == 1 and units[0].ts.year == 2026
    assert cur["msgs"]["rowid"] == 15 and cur["sig"]
    assert run(src, locs[0], cur, me)[0] == []


def test_goose_filters_and_incremental(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    session(con, "s_user")
    session(con, "s_hidden", "hidden")
    session(con, "s_sched", "scheduled")
    session(con, "s_sub", "sub_agent", parent="s_user")
    session(con, "s_parent", "user", parent="s_user")
    session(con, "s_recipe", recipe='{"title": "r"}')
    session(con, "s_cron", schedule="nightly")
    msg(con, "s_user", "user", "rename the config loader to something clearer", 1, mid="msg_1")
    msg(con, "s_user", "assistant", "Renamed it. Mail me at ana@example.org", 2)
    msg(
        con,
        "s_user",
        "user",
        "<turn-context>\n<current-time>x</current-time>\n</turn-context>",
        3,
        {"userVisible": False, "agentVisible": True, "turnContext": True},
    )
    msg(con, "s_user", "user", "injected context the user never saw", 3, {"userVisible": False, "agentVisible": True})
    msg(con, "s_user", "user", [{"type": "toolResponse", "id": "t1", "toolResult": {"status": "success"}}], 4)
    msg(con, "s_user", "user", [{"type": "image", "data": "x", "mimeType": "image/png"}], 4)
    msg(con, "s_user", "user", "and keep the tests green", 5, {"userVisible": True, "agentVisible": False})
    msg(con, "s_user", "user", "actually, stop and use sqlite instead", 6, {**VISIBLE, "steer": True}, mid="msg_steer")
    msg(con, "s_user", "user", "/compact", 7)
    msg(con, "s_user", "user", "/clear", 7)
    msg(con, "s_user", "user", "/goal make the importer idempotent", 8)
    msg(con, "s_user", "user", "/usr/bin/env is where it looks, why?", 9)
    msg(con, "s_user", "user", "   ", 9)
    for sid in ("s_hidden", "s_sched", "s_sub", "s_parent", "s_recipe", "s_cron"):
        msg(con, sid, "user", f"not typed at a keyboard ({sid})", 10)
    con.commit()

    src = Goose()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert [u.text for u in units] == [
        "rename the config loader to something clearer",
        "and keep the tests green",
        "actually, stop and use sqlite instead",
        "make the importer idempotent",
        "/usr/bin/env is where it looks, why?",
    ]
    assert units[0].source_ref == "msg_1" and units[2].source_ref == "msg_steer"
    assert units[1].context[0].who == "agent" and "[email]" in units[1].context[0].text
    assert units[2].context == ()  # the agent has not answered since
    assert units[3].source_ref.startswith("s_user:")  # no message_id stored: session + rowid
    assert "ana@" not in repr(cur)

    msg(con, "s_user", "user", "one more thing", 11)
    con.commit()
    units, cur2 = run(src, loc, cur, me)
    assert [u.text for u in units] == ["one more thing"]
    assert run(src, loc, cur2, me)[0] == []
    # a cursor whose row is not the one at that rowid (another database) is read from the top
    assert len(run(src, loc, {"msgs": {"rowid": 3, "id": "elsewhere"}}, me)[0]) == 6


def test_goose_millisecond_timestamps_and_older_schema(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, working_dir TEXT NOT NULL);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,"
        " content_json TEXT NOT NULL, created_timestamp INTEGER NOT NULL);"
        "INSERT INTO sessions VALUES ('a', '/home/jo.dev/work/app');"
    )
    con.execute(
        "INSERT INTO messages (session_id, role, content_json, created_timestamp) VALUES ('a','user',?,?)",
        (json.dumps([{"type": "text", "text": "an older database without metadata"}]), (T0 + 5) * 1000),
    )
    con.commit()
    units, _ = run(Goose(), Goose().discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["an older database without metadata"] and units[0].ts.year == 2026


def test_goose_wal_only_rows(home, me):
    def build(con):
        con.executescript(SCHEMA)
        session(con, "s_w")
        msg(con, "s_w", "user", "written only to the wal so far", 1)

    p = wal_snapshot(build, db_path(home))
    assert main_file_tables(p) == []
    units, _ = run(Goose(), Goose().discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["written only to the wal so far"]


def test_goose_strip_command():
    assert strip_command("/compact") == "" and strip_command("  /prompts --extension x ") == ""
    assert strip_command("/goal ship the importer") == "ship the importer" and strip_command("/grind off") == "off"
    assert strip_command("/usr/lib is empty") == "/usr/lib is empty" and strip_command("no command") == "no command"
    assert strip_command("//comment") == "//comment"


def test_goose_discover_locations(home, me, monkeypatch, tmp_path):
    src = Goose()
    assert src.discover(me, {}) == [] and src.discover(me, None) == []
    install_fixture("goose", "sessions.db", db_path(home))
    assert [lc.path for lc in src.discover(me, {})] == [db_path(home)]
    # XDG_DATA_HOME wins as the primary dir but the default dir is still read
    xdg = tmp_path / "xdg"
    install_fixture("goose", "sessions.db", xdg / "goose" / "sessions" / "sessions.db")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert len(src.discover(me, {})) == 2
    # GOOSE_PATH_ROOT (absolute only) moves everything under <root>/data
    root = tmp_path / "root"
    install_fixture("goose", "sessions.db", root / "data" / "sessions" / "sessions.db")
    monkeypatch.setenv("GOOSE_PATH_ROOT", str(root))
    paths = [lc.path for lc in src.discover(me, {})]
    assert root / "data" / "sessions" / "sessions.db" in paths and len(paths) == 2  # + the default dir
    monkeypatch.setenv("GOOSE_PATH_ROOT", "relative/root")
    assert len(src.discover(me, {})) == 2  # ignored: back to XDG + default
    extra = tmp_path / "second"
    install_fixture("goose", "sessions.db", extra / "sessions" / "sessions.db")
    assert len(src.discover(me, {"dirs": [str(extra), None]})) == 3


def test_goose_unreadable_database_is_not_fatal(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not a database " * 20)
    src = Goose()
    loc = src.discover(me, {})[0]
    saved = {"msgs": {"rowid": 4, "id": "x"}}
    units, cur = src.read(loc, saved, me)
    assert list(units) == [] and cur == saved


def _make(p, prompts, start=0):
    """A fresh goose database with one user session and one typed prompt per entry of `prompts`."""
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(p)
    con.executescript(SCHEMA)
    session(con, "s_user")
    for i, text in enumerate(prompts):
        msg(con, "s_user", "user", text, start + 2 * i, mid=f"msg_{text[:3]}_{start}_{i}")
        msg(con, "s_user", "assistant", "ok", start + 2 * i + 1)
    con.commit()
    return con


def test_goose_recreated_database_that_outgrows_the_old_max_is_read_in_full(home, me):
    """P1-5: the deleted-and-recreated database restarts ids at 1; it grows past the old max before the next sync."""
    p = db_path(home)
    con = _make(p, ["first old prompt", "second old prompt"])
    con.close()
    src = Goose()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert len(units) == 2 and cur["msgs"]["rowid"] == 4
    for f in p.parent.glob("sessions.db*"):
        f.unlink()
    con = _make(p, [f"brand new prompt {i}" for i in range(4)], start=100)  # ids 1..8 > old max 4
    con.close()
    units, cur2 = run(src, loc, cur, me)
    assert [u.text for u in units] == [f"brand new prompt {i}" for i in range(4)]  # none lost to the stale cursor
    assert cur2["msgs"]["rowid"] == 8
    assert run(src, loc, cur2, me)[0] == []


def test_goose_unchanged_database_is_not_copied_again(home, me, monkeypatch):
    from mannerism.adapters import goose as goose_mod

    _make(db_path(home), ["a prompt to read once"]).close()
    src = Goose()
    loc = src.discover(me, {})[0]
    _, cur = run(src, loc, None, me)

    def boom(*a, **k):
        raise AssertionError("an unchanged database must not be copied")

    monkeypatch.setattr(goose_mod, "DbCopy", boom)
    assert run(src, loc, cur, me) == ([], cur)


def test_goose_skills_template_is_not_typed(home, me):
    """`/skills a b` stores a sentence the CLI wrote ("Use the load_skill tool ..."), not the user's words."""
    p = db_path(home)
    con = _make(p, [])
    msg(con, "s_user", "user", 'Use the load_skill tool to load the following skills: "pdf", "docx".', 1)
    msg(con, "s_user", "assistant", "Loaded.", 2)
    msg(con, "s_user", "user", "now summarise the attached report", 3)
    con.commit()
    con.close()
    src = Goose()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["now summarise the attached report"]
    assert strip_command('Use the load_skill tool to load the following skills: "x".') == ""


def test_goose_relayed_text_is_dropped(home, me):
    p = db_path(home)
    con = _make(p, [])
    msg(con, "s_user", "user", "<task-notification>job done</task-notification>", 1)
    msg(con, "s_user", "user", "a real prompt", 2)
    con.commit()
    con.close()
    src = Goose()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["a real prompt"]
