import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mannerism.adapters.kilo import Kilo

from .conftest import run

FIXTURE_SQL = Path(__file__).parent / "fixtures" / "kilo" / "kilo.sql"  # rows of a real `kilo run` 7.8.15 session
REAL_SESSION = "ses_e9f9319eaffe81yrpTVGWrrqmU"
T0 = 1_780_000_000_000


@pytest.fixture(autouse=True)
def _no_xdg(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("KILO_DB", raising=False)


def kilo_dir(home: Path) -> Path:
    d = home / ".local" / "share" / "kilo"
    d.mkdir(parents=True, exist_ok=True)
    return d


def make_db(path: Path, fixture=True, headless=False) -> sqlite3.Connection:
    """The real 7.8.15 schema. The real session came from `kilo run`, which marks its session through the `permission`
    ruleset (headless); that mark is cleared unless `headless`, so the rows read as a typed session."""
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode = WAL")
    con.executescript(FIXTURE_SQL.read_text("utf-8"))
    if not fixture:  # keep the real schema, drop the rows
        con.executescript("DELETE FROM part; DELETE FROM message; DELETE FROM session;")
    elif not headless:
        con.execute("UPDATE session SET permission = NULL")
    con.commit()
    return con


def add_session(con, sid, directory="/home/jo.dev/proj", parent=None):
    con.execute(
        "INSERT INTO session (id, project_id, parent_id, slug, directory, title, version, time_created, time_updated) "
        "VALUES (?, 'global', ?, 's', ?, 't', '7.8.15', ?, ?)",
        (sid, parent, directory, T0, T0),
    )


def add_msg(con, sid, mid, role, t, parts, **extra):
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?)",
        (mid, sid, t, t, json.dumps({"role": role, "time": {"created": t}, **extra})),
    )
    for i, p in enumerate(parts):
        con.execute(
            "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES (?, ?, ?, ?, ?, ?)",
            (f"prt_{mid}_{i}", mid, sid, t + i, t + i, json.dumps(p)),
        )


def txt(s, **kw):
    return {"type": "text", "text": s, **kw}


def texts(units):
    return [u.text for u in units]


def test_kilo_real_session(home, me):
    con = make_db(kilo_dir(home) / "kilo.db")
    con.close()
    src = Kilo()
    locs = src.discover(me, {})
    assert [lc.path.name for lc in locs] == ["kilo.db"]
    units, cur = run(src, locs[0], None, me)
    # stdin adds a trailing newline (stripped); the synthetic Read-tool echoes and the file part are not typed text
    assert texts(units) == [
        "hello there, when does the first ferry leave?",
        "ok thanks, can you look at the attached file and tell me the second sailing? keep it short pls",
        "nice, that is all - cheers",
    ]
    assert all((u.kind, u.audience, u.register, u.source) == ("prompt", "agent", "agent", "kilo") for u in units)
    assert units[0].ts == datetime.fromtimestamp(1792619114.647, UTC)
    assert units[0].context == ()
    assert units[1].context[0].who == "agent" and units[1].context[0].text.startswith("Here is the short version")
    assert units[0].source_ref == f"{REAL_SESSION}:msg_1606cf0970018hOiz0fldyYO6E"
    assert units[0].meta["cwd_hash"]
    again, cur2 = run(src, locs[0], cur, me)
    assert again == [] and cur2 == cur  # database unchanged: nothing read


def test_kilo_headless_run_sessions_are_dropped(home, me):
    """`kilo run` creates its session with the question tool denied; an interactive session never has that rule."""
    con = make_db(kilo_dir(home) / "kilo.db", headless=True)
    assert "question" in con.execute("SELECT permission FROM session").fetchone()[0]
    con.close()
    src = Kilo()
    units, cur = run(src, src.discover(me, {})[0], None, me)
    assert units == [] and cur["sig"]


def test_kilo_incremental_reads_and_late_parts(home, me):
    db = kilo_dir(home) / "kilo.db"
    con = make_db(db)
    src = Kilo()
    (loc,) = src.discover(me, {})
    first, cur = run(src, loc, None, me)
    assert len(first) == 3
    # a new prompt arrives with its message row but no part yet (parts land a moment later)
    t = 1792619200000
    add_msg(con, REAL_SESSION, "msg_new", "user", t, [])
    con.commit()
    none, cur2 = run(src, loc, cur, me)
    assert {u.unit_id for u in none} <= {u.unit_id for u in first}  # only the recent prompts again, nothing new
    con.execute(
        "INSERT INTO part (id, message_id, session_id, time_created, time_updated, data) VALUES "
        "('prt_late', 'msg_new', ?, ?, ?, ?)",
        (REAL_SESSION, t + 5, t + 5, json.dumps(txt("one more thing"))),
    )
    con.commit()
    new, cur3 = run(src, loc, cur2, me)
    assert "one more thing" in texts(new)
    # the overlap window re-reads recent prompts; ids are stable so the engine's dedupe holds
    old_ids = {u.unit_id for u in first}
    assert {u.unit_id for u in new if u.text != "one more thing"} <= old_ids
    assert cur3["since"] == t
    # a quiet database is not read again at all
    assert run(src, loc, cur3, me)[0] == []


def test_kilo_part_filters_and_sessions(home, me):
    con = make_db(kilo_dir(home) / "kilo.db", fixture=False)
    add_session(con, "ses_a")
    add_session(con, "ses_sub", parent="ses_a")
    add_msg(
        con, "ses_a", "m1", "assistant", T0, [{"type": "step-start"}, txt("Which module?"), txt("hidden", ignored=True)]
    )
    add_msg(
        con,
        "ses_a",
        "m2",
        "user",
        T0 + 10,
        [
            txt("Called the Read tool with the following input: {}", synthetic=True),
            {"type": "file", "mime": "text/plain", "filename": "a.txt", "url": "file:///x/a.txt"},
            txt(
                "look at ",
            ),
            txt("the parser, mail jo.dev@example.org"),
            txt("<system-reminder>x</system-reminder>", synthetic=True),
            txt("ignored one", ignored=True),
            {"type": "subtask", "prompt": "do things", "description": "d", "agent": "general"},
        ],
    )
    add_msg(con, "ses_a", "m3", "user", T0 + 20, [txt('"fix the flaky test in the parser"\n')])  # `kilo run "..."`
    add_msg(con, "ses_a", "m4", "user", T0 + 30, [txt('"say \\"hi\\" to everyone"')])
    add_msg(con, "ses_a", "m5", "user", T0 + 40, [txt('"oneword"')])  # not added by run (no space): the person's quotes
    add_msg(
        con,
        "ses_a",
        "m6",
        "user",
        T0 + 50,
        [txt("Plan file: /p/plan.md\nRead this file first and treat it as the source of truth.")],
    )
    add_msg(con, "ses_a", "m7", "user", T0 + 60, [txt("/compact")])
    add_msg(con, "ses_a", "m8", "user", T0 + 70, [{"type": "compaction", "auto": True}])
    add_msg(con, "ses_sub", "m9", "user", T0 + 80, [txt("sub-agent prompt written by the parent agent")])
    add_msg(con, "ses_a", "m10", "user", T0 + 90, [txt("   ")])
    add_msg(con, "ghost", "m11", "user", T0 + 95, [txt("session row missing")])
    con.execute(
        "INSERT INTO message (id, session_id, time_created, time_updated, data) VALUES ('bad', 'ses_a', ?, ?, 'not json "
        "role user')",
        (T0 + 100, T0 + 100),
    )
    con.commit()
    src = Kilo()
    (loc,) = src.discover(me, {})
    units, _ = run(src, loc, None, me)
    assert texts(units) == [
        "look at\nthe parser, mail [email]",
        "fix the flaky test in the parser",
        'say "hi" to everyone',
        '"oneword"',
        "session row missing",
    ]
    assert units[0].context[0].text == "Which module?"
    assert units[-1].meta == {}


def test_kilo_db_discovery_and_robustness(home, me, tmp_path, monkeypatch):
    base = kilo_dir(home)
    make_db(base / "kilo.db", fixture=False).close()
    (base / "kilo-beta.db").write_bytes(b"")
    (base / "notes.txt").write_text("x")
    src = Kilo()
    assert sorted(lc.path.name for lc in src.discover(me, {})) == ["kilo-beta.db", "kilo.db"]
    by = {lc.path.name: lc for lc in src.discover(me, {})}
    assert list(src.read(by["kilo.db"], None, me)[0]) == []  # right schema, no rows
    assert list(src.read(by["kilo-beta.db"], {"since": 5}, me)[0]) == []  # an empty file has no tables
    assert src.read(by["kilo-beta.db"], {"since": 5}, me)[1] == {"since": 5}  # ... the cursor is kept for a retry
    (base / "kilo-beta.db").write_bytes(b"this is not a database at all" * 100)
    assert list(src.read(Kilo().discover(me, {})[0], None, me)[0]) == []
    # relocation: $KILO_DB (absolute) and $XDG_DATA_HOME
    other = tmp_path / "other.db"
    make_db(other).close()
    monkeypatch.setenv("KILO_DB", str(other))
    assert str(other) in [str(lc.path) for lc in src.discover(me, {})]
    monkeypatch.delenv("KILO_DB")
    xdg = tmp_path / "xdg"
    (xdg / "kilo").mkdir(parents=True)
    make_db(xdg / "kilo" / "kilo.db").close()
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert str(xdg / "kilo" / "kilo.db") in [str(lc.path) for lc in src.discover(me, {})]
    extra = tmp_path / "extra"
    extra.mkdir()
    make_db(extra / "kilo.db").close()
    assert str(extra / "kilo.db") in [str(lc.path) for lc in src.discover(me, {"dirs": [str(extra), None]})]


def write_json(path: Path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))


def test_kilo_legacy_json_storage(home, me):
    st = kilo_dir(home) / "storage"
    sid = "ses_old"
    write_json(st / "session" / "proj1" / f"{sid}.json", {"id": sid, "directory": "/home/jo.dev/proj"})
    write_json(
        st / "message" / sid / "msg_1.json", {"id": "msg_1", "sessionID": sid, "role": "user", "time": {"created": T0}}
    )
    write_json(st / "part" / "msg_1" / "prt_1.json", {"id": "prt_1", "type": "text", "text": "first old prompt"})
    write_json(st / "message" / sid / "msg_2.json", {"id": "msg_2", "role": "assistant", "time": {"created": T0 + 5}})
    write_json(st / "part" / "msg_2" / "prt_2.json", {"id": "prt_2", "type": "text", "text": "Sure."})
    write_json(st / "message" / sid / "msg_3.json", {"id": "msg_3", "role": "user", "time": {"created": T0 + 9}})
    write_json(
        st / "part" / "msg_3" / "prt_a.json", {"id": "prt_a", "type": "text", "text": "tool echo", "synthetic": True}
    )
    write_json(st / "part" / "msg_3" / "prt_b.json", {"id": "prt_b", "type": "text", "text": "second old prompt"})
    write_json(st / "message" / "ses_child" / "msg_9.json", {"id": "msg_9", "role": "user", "time": {"created": T0}})
    write_json(st / "part" / "msg_9" / "prt_9.json", {"id": "prt_9", "type": "text", "text": "child prompt"})
    write_json(st / "session" / "proj1" / "ses_child.json", {"id": "ses_child", "parentID": sid})
    (st / "message" / "ses_broken").mkdir()
    (st / "message" / "ses_broken" / "msg_x.json").write_text("{torn")
    src = Kilo()
    locs = src.discover(me, {})
    assert sorted(lc.path.name for lc in locs) == ["ses_broken", "ses_child", sid]
    by = {lc.path.name: run(src, lc, None, me) for lc in locs}
    units, cur = by[sid]
    assert texts(units) == ["first old prompt", "second old prompt"]
    assert units[1].context[0].text == "Sure." and units[0].meta["cwd_hash"]
    assert units[0].source_ref == f"{sid}:msg_1"
    assert by["ses_child"][0] == [] and by["ses_broken"][0] == []
    (loc,) = [lc for lc in locs if lc.path.name == sid]
    assert run(src, loc, cur, me)[0] == []
    # a new message and part appear: the signature changes and the session is read again
    write_json(st / "message" / sid / "msg_4.json", {"id": "msg_4", "role": "user", "time": {"created": T0 + 20}})
    write_json(st / "part" / "msg_4" / "prt_4.json", {"id": "prt_4", "type": "text", "text": "third old prompt"})
    more, _ = run(src, loc, cur, me)
    assert texts(more) == ["first old prompt", "second old prompt", "third old prompt"]
    assert [u.unit_id for u in more[:2]] == [u.unit_id for u in units]
    # once the database exists next to it (Kilo imports the JSON on first start), the JSON tree is no longer read
    make_db(kilo_dir(home) / "kilo.db", fixture=False).close()
    assert [lc.path.name for lc in src.discover(me, {})] == ["kilo.db"]
