import json
import sqlite3

import pytest

from mannerism.adapters.opencode import OpenCode

from .conftest import run
from .sqlite_helpers import install_fixture, main_file_tables, wal_snapshot

T0 = 1_792_618_000_000  # epoch ms, 2026


@pytest.fixture(autouse=True)
def _no_xdg(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_DB", raising=False)


def db_path(home, name="opencode.db"):
    return home / ".local" / "share" / "opencode" / name


V1_SCHEMA = """
CREATE TABLE session (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT, title TEXT,
    time_created INTEGER, time_updated INTEGER);
CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, time_created INTEGER NOT NULL,
    time_updated INTEGER NOT NULL, data TEXT NOT NULL);
CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT NOT NULL, session_id TEXT NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""

V2_SCHEMA = """
CREATE TABLE session_v2 (id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT, title TEXT);
CREATE TABLE session_message (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, type TEXT NOT NULL, seq INTEGER NOT NULL,
    time_created INTEGER NOT NULL, time_updated INTEGER NOT NULL, data TEXT NOT NULL);
"""


class V1:
    """Writes v1 rows the way OpenCode 1.x does: message JSON carries the role, part JSON the content."""

    def __init__(self, con):
        self.con, self.n = con, 0

    def session(self, sid, parent=None, directory="/home/jo.dev/work/app"):
        self.con.execute("INSERT INTO session VALUES (?,?,?,?,?,?,?)", (sid, "p1", parent, directory, "t", T0, T0))

    def message(self, sid, role, parts, mid, at):
        self.con.execute(
            "INSERT INTO message VALUES (?,?,?,?,?)", (mid, sid, T0 + at, T0 + at, json.dumps({"role": role}))
        )
        self.parts(sid, mid, parts, at)

    def parts(self, sid, mid, parts, at):
        for p in parts:
            self.n += 1
            self.con.execute(
                "INSERT INTO part VALUES (?,?,?,?,?,?)",
                (f"prt_{self.n:04d}", mid, sid, T0 + at, T0 + at, json.dumps(p)),
            )


def text(t, **kw):
    return {"type": "text", "text": t, **kw}


def install_interactive(path):
    """The real `opencode run` fixture with the `run` mark (the question tool denied) cleared, as a TUI session is."""
    p = install_fixture("opencode", "opencode.db", path)
    con = sqlite3.connect(p)
    con.execute("UPDATE session SET permission = NULL")
    con.commit()
    con.close()
    return p


def test_opencode_headless_run_sessions_are_dropped(home, me):
    """`opencode run` creates its session with a ruleset that denies the question tool; the TUI never sets one."""
    install_fixture("opencode", "opencode.db", db_path(home))
    src = OpenCode()
    units, cur = run(src, src.discover(me, {})[0], None, me)
    assert units == [] and cur["v1"]["rowid"] == 12


def test_opencode_real_1_19_run(home, me):
    """Database written by `opencode run` 1.19.3 (three turns, one via a /command) against a mock model."""
    p = install_interactive(db_path(home))
    src = OpenCode()
    locs = src.discover(me, {})
    assert [lc.path for lc in locs] == [p]
    units, cur = run(src, locs[0], None, me)
    # `opencode run` wraps a multi-word argument in quotes before storing it; the third prompt is /review's template
    assert [u.text for u in units] == [
        '"write me a haiku about gardening"',
        '"now make it shorter and a bit funnier"',
        'Please review this: "the garden notes"',
    ]
    assert all((u.kind, u.register, u.audience) == ("prompt", "agent", "agent") for u in units)
    assert units[0].context == ()
    assert units[1].context[0].who == "agent" and units[1].context[0].text.startswith("Sure, I can help")
    assert units[0].thread_id == units[2].thread_id and units[0].meta["cwd_hash"]
    assert units[0].ts.year == 2026 and len({u.unit_id for u in units}) == 3
    assert cur["v1"]["rowid"] == 12 and cur["v1"]["id"].startswith("prt_16060e4de")
    assert cur["v2"] == {"rowid": 0, "id": ""}  # 1.19 also has the v2 table, still empty
    assert run(src, locs[0], cur, me)[0] == []


def test_opencode_v1_filters_incremental_and_late_parts(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(V1_SCHEMA)
    w = V1(con)
    w.session("ses_a")
    w.session("ses_sub", parent="ses_a")
    w.message("ses_a", "user", [text("fix the flaky retry test in client.py")], "msg_u1", 1)
    w.message("ses_a", "assistant", [{"type": "step-start"}, text("Done, reach me at ana@example.org")], "msg_a1", 2)
    w.message(
        "ses_a",
        "user",
        [
            text("look at @notes.txt please"),
            text("Called the Read tool with the following input: ...", synthetic=True),
            text("hidden note", ignored=True),
            {"type": "file", "url": "file:///x", "mime": "text/plain"},
        ],
        "msg_u2",
        3,
    )
    w.message("ses_a", "user", [text("summarize"), {"type": "compaction", "auto": False}], "msg_c", 4)
    w.message("ses_a", "user", [text("do the thing"), {"type": "subtask", "prompt": "x"}], "msg_s", 5)
    w.message("ses_a", "user", [text("only synthetic", synthetic=True)], "msg_syn", 6)
    w.message("ses_sub", "user", [text("sub-agent prompt written by the model")], "msg_sub", 7)
    con.commit()

    src = OpenCode()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert [u.text for u in units] == ["fix the flaky retry test in client.py", "look at @notes.txt please"]
    assert [u.source_ref for u in units] == ["msg_u1", "msg_u2"]
    assert units[0].context == ()
    assert units[1].context[0].who == "agent" and "[email]" in units[1].context[0].text
    assert "ana@" not in repr(cur)

    # a message row can land before its text part: the part rowid cursor still picks it up
    w.message("ses_a", "user", [], "msg_late", 8)
    con.commit()
    assert run(src, loc, cur, me)[0] == []
    w.parts("ses_a", "msg_late", [text("now add a regression test")], 8)
    w.message("ses_a", "user", [text("and run the suite")], "msg_u4", 9)
    con.commit()
    units, cur2 = run(src, loc, cur, me)
    assert [u.text for u in units] == ["now add a regression test", "and run the suite"]
    assert run(src, loc, cur2, me)[0] == []
    # a cursor from another database (a different row at that rowid) is read from the top
    assert len(run(src, loc, {"v1": {"rowid": 5, "id": "elsewhere"}}, me)[0]) == 4


def test_opencode_v2_session_message(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(V2_SCHEMA)
    con.executemany(
        "INSERT INTO session_v2 VALUES (?,?,?,?,?)",
        [("ses_a", "p", None, "/home/jo.dev/work/app", "t"), ("ses_sub", "p", "ses_a", "/home/jo.dev/work/app", "t")],
    )

    def msg(mid, sid, typ, seq, data):
        con.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (mid, sid, typ, seq, T0 + seq, T0 + seq, json.dumps(data)),
        )

    msg("msg_1", "ses_a", "user", 1, {"text": "port the importer to the new schema"})
    msg(
        "msg_2",
        "ses_a",
        "assistant",
        2,
        {"content": [{"type": "reasoning", "text": "hm"}, {"type": "text", "text": "Ported."}]},
    )
    msg("msg_3", "ses_a", "user", 3, {"text": "now the exporter"})
    for i, typ in enumerate(
        ("system", "synthetic", "skill", "shell", "compaction", "agent-switched", "model-switched")
    ):
        msg(f"msg_x{i}", "ses_a", typ, 4 + i, {"text": f"not typed by the user ({typ})"})
    msg("msg_s", "ses_sub", "user", 1, {"text": "sub-agent task text"})
    msg("msg_e", "ses_a", "user", 20, {"text": "   "})
    con.commit()

    src = OpenCode()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert [u.text for u in units] == ["port the importer to the new schema", "now the exporter"]
    assert units[1].context[0].text == "Ported."
    assert cur["v2"]["rowid"] > 0 and cur["v2"]["id"] == "msg_e" and "v1" not in cur
    msg("msg_4", "ses_a", "user", 21, {"text": "ship it"})
    con.commit()
    assert [u.text for u in run(src, loc, cur, me)[0]] == ["ship it"]


def test_opencode_migrated_v1_sessions_are_not_read_twice(home, me):
    """After the v1 -> v2 migration both layouts hold the session; ids are kept, so it must yield once."""
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(V1_SCHEMA + V2_SCHEMA)
    w = V1(con)
    w.session("ses_old")
    w.session("ses_new")
    w.message("ses_old", "user", [text("migrated prompt")], "msg_old", 1)
    w.message("ses_new", "user", [text("not yet migrated prompt")], "msg_new", 2)
    con.execute("INSERT INTO session_v2 VALUES ('ses_old','p',NULL,'/home/jo.dev/work/app','t')")
    con.execute(
        "INSERT INTO session_message VALUES ('msg_old','ses_old','user',0,?,?,?)",
        (T0 + 1, T0 + 1, json.dumps({"text": "migrated prompt"})),
    )
    con.commit()
    units, cur = run(OpenCode(), OpenCode().discover(me, {})[0], None, me)
    assert sorted(u.text for u in units) == ["migrated prompt", "not yet migrated prompt"]
    assert sorted(u.source_ref for u in units) == ["msg_new", "msg_old"]
    assert set(cur) == {"v1", "v2", "sig"}


def test_opencode_wal_only_rows(home, me):
    def build(con):
        con.executescript(V1_SCHEMA)
        w = V1(con)
        w.session("ses_w")
        w.message("ses_w", "user", [text("written only to the wal so far")], "msg_w", 1)

    p = wal_snapshot(build, db_path(home))
    assert main_file_tables(p) == []  # the main file alone knows nothing
    units, _ = run(OpenCode(), OpenCode().discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["written only to the wal so far"]


def test_opencode_discover_locations(home, me, monkeypatch, tmp_path):
    src = OpenCode()
    assert src.discover(me, {}) == [] and src.discover(me, None) == []
    install_fixture("opencode", "opencode.db", db_path(home))
    install_fixture("opencode", "opencode.db", db_path(home, "opencode-dev.db"))  # other release channel
    (db_path(home).parent / "log").mkdir()
    assert [lc.path.name for lc in src.discover(me, {})] == ["opencode-dev.db", "opencode.db"]
    # XDG_DATA_HOME moves the data dir; the default dir is still read, once
    xdg = tmp_path / "xdg"
    install_fixture("opencode", "opencode.db", xdg / "opencode" / "opencode.db")
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert len(src.discover(me, {})) == 3
    # OPENCODE_DB names one file anywhere; ":memory:" and missing files are ignored
    other = install_fixture("opencode", "opencode.db", tmp_path / "elsewhere" / "custom.db")
    monkeypatch.setenv("OPENCODE_DB", str(other))
    assert other in [lc.path for lc in src.discover(me, {})]
    monkeypatch.setenv("OPENCODE_DB", ":memory:")
    assert len(src.discover(me, {})) == 3
    extra = tmp_path / "second-account"
    install_fixture("opencode", "opencode.db", extra / "opencode.db")
    assert len(src.discover(me, {"dirs": [str(extra), None]})) == 4


def test_opencode_unreadable_database_is_not_fatal(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    p.write_bytes(b"this is not a sqlite database at all" * 10)
    src = OpenCode()
    loc = src.discover(me, {})[0]
    saved = {"v1": {"rowid": 3, "id": "x"}}
    assert list(src.read(loc, saved, me)[0]) == [] and src.read(loc, saved, me)[1] == saved
    con = sqlite3.connect(db_path(home, "other.db"))
    con.execute("CREATE TABLE unrelated (x)")
    con.commit()
    units, cur = run(src, type(loc)("opencode", "k", db_path(home, "other.db")), None, me)
    assert units == [] and set(cur) == {"sig"}


def _v1_db(path, pairs, id_base=0):
    """A v1 database with one session and a (user prompt, assistant reply) pair per entry of `pairs`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.executescript(V1_SCHEMA)
    w = V1(con)
    w.n = id_base  # part ids are unique across databases in real life
    w.session("ses_a")
    for i, (ask, reply, tag) in enumerate(pairs):
        w.message("ses_a", "user", [text(ask)], f"msg_u_{tag}", 2 * i + 1)
        w.message("ses_a", "assistant", [text(reply)], f"msg_a_{tag}", 2 * i + 2)
    con.commit()
    return con, w


def test_opencode_v1_undo_reuses_rowids_and_loses_nothing(home, me):
    """P1-5: /undo deletes the tail turn; the next turn takes the same part rowids (TEXT primary keys)."""
    con, w = _v1_db(db_path(home), [("first prompt here", "reply one", "1"), ("second prompt here", "reply two", "2")])
    src = OpenCode()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert [u.text for u in units] == ["first prompt here", "second prompt here"]
    # /undo of the last turn removes its messages and parts ...
    con.execute("DELETE FROM part WHERE message_id IN ('msg_u_2', 'msg_a_2')")
    con.execute("DELETE FROM message WHERE id IN ('msg_u_2', 'msg_a_2')")
    # ... and the user types something else: two new parts land on the freed rowids, the max rowid does not move
    w.n = 100
    w.message("ses_a", "user", [text("the prompt typed after the undo")], "msg_u_3", 5)
    w.message("ses_a", "assistant", [text("reply three")], "msg_a_3", 6)
    con.commit()
    assert con.execute("SELECT max(rowid) FROM part").fetchone()[0] == cur["v1"]["rowid"]
    units, cur2 = run(src, loc, cur, me)
    # the cursor row was replaced, so reading starts over: the earlier prompt comes again (same ref: the engine
    # dedupes) next to the new one
    assert "the prompt typed after the undo" in [u.text for u in units]
    assert {u.source_ref for u in units} <= {"msg_u_1", "msg_u_3"}
    assert run(src, loc, cur2, me)[0] == []


def test_opencode_v1_recreated_database_that_outgrows_the_old_max(home, me):
    p = db_path(home)
    con, _ = _v1_db(p, [("old prompt number one", "r1", "o1")])
    con.close()
    src = OpenCode()
    loc = src.discover(me, {})[0]
    _, cur = run(src, loc, None, me)
    for f in p.parent.glob("opencode.db*"):
        f.unlink()
    pairs = [(f"new prompt number {i}", f"r{i}", f"n{i}") for i in range(4)]
    con, _ = _v1_db(p, pairs, id_base=500)  # 8 parts > old max 2
    con.close()
    units, _ = run(src, loc, cur, me)
    assert sorted(u.text for u in units) == [f"new prompt number {i}" for i in range(4)]


def test_opencode_v2_undo_reuses_rowids_and_loses_nothing(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(V2_SCHEMA)
    con.execute("INSERT INTO session_v2 VALUES ('ses_a','p',NULL,'/home/jo.dev/work/app','t')")

    def msg(mid, typ, seq, data):
        con.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (mid, "ses_a", typ, seq, T0 + seq, T0 + seq, json.dumps(data)),
        )

    msg("m1", "user", 1, {"text": "first v2 prompt"})
    msg("m2", "assistant", 2, {"content": [{"type": "text", "text": "ok"}]})
    msg("m3", "user", 3, {"text": "second v2 prompt"})
    con.commit()
    src = OpenCode()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert len(units) == 2
    con.execute("DELETE FROM session_message WHERE id = 'm3'")
    msg("m4", "user", 4, {"text": "prompt typed after the undo"})
    con.commit()
    units, _ = run(src, loc, cur, me)
    assert "prompt typed after the undo" in [u.text for u in units]
    assert {u.source_ref for u in units} <= {"m1", "m4"}


def test_opencode_partly_migrated_session_keeps_its_unmigrated_v1_prompts(home, me):
    """P-hybrid: v1 messages are skipped per message id that session_message already has, not per session."""
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(V1_SCHEMA + V2_SCHEMA)
    w = V1(con)
    w.session("ses_a")
    w.message("ses_a", "user", [text("migrated prompt")], "msg_1", 1)
    w.message("ses_a", "user", [text("prompt the migration missed")], "msg_2", 2)
    con.execute("INSERT INTO session_v2 VALUES ('ses_a','p',NULL,'/home/jo.dev/work/app','t')")
    con.execute(
        "INSERT INTO session_message VALUES ('msg_1','ses_a','user',0,?,?,?)",
        (T0 + 1, T0 + 1, json.dumps({"text": "migrated prompt"})),
    )
    con.commit()
    units, _ = run(OpenCode(), OpenCode().discover(me, {})[0], None, me)
    assert sorted(u.source_ref for u in units) == ["msg_1", "msg_2"]
    assert sorted(u.text for u in units) == ["migrated prompt", "prompt the migration missed"]


def test_opencode_migration_attachment_placeholders_are_removed(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(V2_SCHEMA)
    con.execute("INSERT INTO session_v2 VALUES ('ses_a','p',NULL,'/home/jo.dev/work/app','t')")
    note = "[Attachment unavailable after migration: named.txt (text/plain)]"
    other = "[Attachment unavailable after migration: https://example.test/raw (application/octet-stream)]"
    for i, body in enumerate((f"look at this\n\n{note}\n\n{other}", note, f"{note}\n\nthen do the thing")):
        con.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (f"m{i}", "ses_a", "user", i, T0 + i, T0 + i, json.dumps({"text": body})),
        )
    con.commit()
    units, _ = run(OpenCode(), OpenCode().discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["look at this", "then do the thing"]


INIT = (
    "Create or update `AGENTS.md` for this repository.\n\nThe goal is a compact instruction file that helps future "
    "OpenCode sessions avoid mistakes and ramp up quickly.\n\nUser-provided focus or constraints (honor these):\n"
    "focus on the tests\n\n## How to investigate\n"
)
REVIEW = (
    "You are a code reviewer. Your job is to review code changes and provide actionable feedback.\n\n---\n\n"
    "Input: abc1234\n\n---\n\n## Determining What to Review\n"
)


def test_opencode_builtin_init_and_review_templates_are_dropped(home, me):
    con, w = _v1_db(db_path(home), [])
    w.message("ses_a", "user", [text(INIT)], "msg_init", 1)
    w.message("ses_a", "user", [text(REVIEW)], "msg_review", 2)
    w.message("ses_a", "user", [text("please explain the build, I read the Create or update notes")], "msg_ok", 3)
    con.commit()
    units, _ = run(OpenCode(), OpenCode().discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["please explain the build, I read the Create or update notes"]


def test_opencode_relayed_text_is_dropped(home, me):
    con, w = _v1_db(db_path(home), [])
    w.message("ses_a", "user", [text("<task-notification>job done</task-notification>")], "msg_n", 1)
    w.message("ses_a", "user", [text("a real prompt")], "msg_r", 2)
    con.commit()
    units, _ = run(OpenCode(), OpenCode().discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["a real prompt"]


def test_opencode_v2_headless_sessions_are_dropped(home, me):
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(V2_SCHEMA.replace("title TEXT)", "title TEXT, permission TEXT)"))
    rules = json.dumps([{"permission": "question", "pattern": "*", "action": "deny"}])
    con.execute("INSERT INTO session_v2 VALUES ('ses_run','p',NULL,'/d','t',?)", (rules,))
    con.execute("INSERT INTO session_v2 VALUES ('ses_tui','p',NULL,'/d','t',NULL)")
    for sid in ("ses_run", "ses_tui"):
        con.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (f"m_{sid}", sid, "user", 1, T0, T0, json.dumps({"text": f"prompt in {sid}"})),
        )
    con.commit()
    units, _ = run(OpenCode(), OpenCode().discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["prompt in ses_tui"]


def test_opencode_unchanged_database_is_not_copied_again(home, me, monkeypatch):
    from mannerism.adapters import opencode as mod

    _v1_db(db_path(home), [("one prompt", "reply", "1")])[0].close()
    src = OpenCode()
    loc = src.discover(me, {})[0]
    _, cur = run(src, loc, None, me)

    def boom(*a, **k):
        raise AssertionError("an unchanged database must not be copied")

    monkeypatch.setattr(mod, "DbCopy", boom)
    assert run(src, loc, cur, me) == ([], cur)


def test_opencode_migrated_headless_session_is_found_through_the_v1_row(home, me):
    """The v1 -> v2 migration leaves session_v2.permission NULL; the `run` mark is still on the v1 session row."""
    p = db_path(home)
    p.parent.mkdir(parents=True)
    con = sqlite3.connect(p)
    con.executescript(V1_SCHEMA.replace("title TEXT,", "title TEXT, permission TEXT,") + V2_SCHEMA)
    rules = json.dumps([{"permission": "question", "pattern": "*", "action": "deny"}])
    con.execute("INSERT INTO session VALUES ('ses_run','p1',NULL,'/d','t',?,?,?)", (rules, T0, T0))
    con.execute("INSERT INTO session VALUES ('ses_tui','p1',NULL,'/d','t',NULL,?,?)", (T0, T0))
    for sid in ("ses_run", "ses_tui"):
        con.execute("INSERT INTO session_v2 VALUES (?,?,NULL,'/d','t')", (sid, "p"))
        con.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (f"m_{sid}", sid, "user", 1, T0, T0, json.dumps({"text": f"prompt in {sid}"})),
        )
    con.commit()
    units, _ = run(OpenCode(), OpenCode().discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["prompt in ses_tui"]
