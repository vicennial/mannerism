"""Regression tests for the code-review findings that cut across several adapters (the per-adapter ones sit in the
adapter's own test file): masking before clipping, relayed text, Cline's new_task hand-off, resolve() failures in
discovery, and the shared SQLite copy / rowid-cursor helpers."""

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from mannerism.adapters import _util
from mannerism.adapters._util import DbCopy, config_dirs, rowid_mark, rowid_resume
from mannerism.adapters.aider import HISTORY_FILE, Aider
from mannerism.adapters.cline import Cline
from mannerism.adapters.continue_dev import Continue
from mannerism.adapters.crush import Crush
from mannerism.adapters.goose import Goose
from mannerism.adapters.kilo import Kilo
from mannerism.adapters.opencode import OpenCode

from . import test_cline as tcl
from . import test_continue_dev as tcd
from . import test_kilo as tk
from .conftest import run

# --- P2-10: mask before clip -----------------------------------------------------------------------------------

VALUE = "hunter2correcthorsebattery"


def leaky(prefix_pad=50):
    """A reply whose last CONTEXT-window starts a few characters into a password VALUE: if the text is clipped first,
    `password=` falls off the front and the value is no longer recognisable; masked first it is gone."""
    head = "x" * prefix_pad + " password=" + VALUE
    return head + " " + "z" * (201 - len(VALUE))


def assert_no_leak(units):
    assert units, "the prompt was not read"
    ctx = units[0].context[0].text
    assert "correcthorse" not in ctx and "battery" not in ctx, ctx


def test_kilo_masks_the_agent_reply_before_clipping_it(home, me):
    con = tk.make_db(tk.kilo_dir(home) / "kilo.db", fixture=False)
    tk.add_session(con, "ses_a")
    tk.add_msg(con, "ses_a", "m1", "assistant", tk.T0, [tk.txt(leaky())])
    tk.add_msg(con, "ses_a", "m2", "user", tk.T0 + 10, [tk.txt("and what next?")])
    con.commit()
    src = Kilo()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert_no_leak(units)


def test_kilo_json_storage_masks_the_agent_reply_before_clipping_it(home, me):
    st = tk.kilo_dir(home) / "storage"
    sid = "ses_old"
    tk.write_json(st / "session" / "p" / f"{sid}.json", {"id": sid, "directory": "/d"})
    tk.write_json(st / "message" / sid / "m1.json", {"id": "m1", "role": "assistant", "time": {"created": tk.T0}})
    tk.write_json(st / "part" / "m1" / "p1.json", {"id": "p1", "type": "text", "text": leaky()})
    tk.write_json(st / "message" / sid / "m2.json", {"id": "m2", "role": "user", "time": {"created": tk.T0 + 5}})
    tk.write_json(st / "part" / "m2" / "p2.json", {"id": "p2", "type": "text", "text": "and what next?"})
    src = Kilo()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert_no_leak(units)


def test_continue_masks_the_agent_reply_before_clipping_it(home, me):
    base = home / ".continue"
    tcd.write_session(base, "s1", [tcd.agent(leaky()), tcd.user("and what next?")])
    src = Continue()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert_no_leak(units)


def test_cline_sdk_masks_the_agent_reply_before_clipping_it(home, me):
    data = home / ".cline" / "data"
    tcl.write_sdk(data, "s1", [tcl.sdk_agent(leaky()), tcl.sdk_user('<user_input mode="act">and what next?</user_input>')])
    src = Cline()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert_no_leak(units)


def test_cline_classic_masks_the_agent_reply_before_clipping_it(home, me):
    rows = [tcl.say("text", leaky(), 1_769_000_000_000), tcl.say("task", "and what next?", 1_769_000_001_000)]
    tcl.write_tasks(home / ".cline" / "data", "t1", rows)
    src = Cline()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert_no_leak(units)


# --- P2-9: relayed text -----------------------------------------------------------------------------------------

RELAY = "<task-notification>build finished</task-notification>"


def test_kilo_drops_relayed_text(home, me):
    con = tk.make_db(tk.kilo_dir(home) / "kilo.db", fixture=False)
    tk.add_session(con, "ses_a")
    tk.add_msg(con, "ses_a", "m1", "user", tk.T0, [tk.txt(RELAY)])
    tk.add_msg(con, "ses_a", "m2", "user", tk.T0 + 5, [tk.txt("a real prompt")])
    con.commit()
    src = Kilo()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["a real prompt"]


def test_continue_drops_relayed_text(home, me):
    tcd.write_session(home / ".continue", "s1", [tcd.user(RELAY), tcd.user("a real prompt")])
    src = Continue()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["a real prompt"]


def test_cline_drops_relayed_text(home, me):
    data = home / ".cline" / "data"
    tcl.write_sdk(data, "s1", [tcl.sdk_user(RELAY, 1, "u1"), tcl.sdk_user("a real prompt", 2, "u2")])
    tcl.write_tasks(data, "t1", [tcl.say("task", RELAY, 1_769_000_000_000), tcl.say("user_feedback", "ok go", 1_769_000_001_000)])
    src = Cline()
    got = sorted(t for lc in src.discover(me, {}) for t in tcl.texts(run(src, lc, None, me)[0]))
    assert got == ["a real prompt", "ok go"]


# --- P2-8: Kilo built-in command templates ---------------------------------------------------------------------


def test_kilo_builtin_init_and_review_templates_are_dropped(home, me):
    init = ("Create or update `AGENTS.md` for this repository.\n\nThe goal is a compact instruction file that helps "
            "future Kilo sessions avoid mistakes.\n\nUser-provided focus or constraints (honor these):\nthe api\n")
    review = "You are Kilo Code, an expert code reviewer focused on high-confidence security, performance, business logic."
    old_review = ("You are a code reviewer. Your job is to review code changes and provide actionable feedback.\n\n---"
                  "\n\nInput: HEAD~1\n\n---\n")
    con = tk.make_db(tk.kilo_dir(home) / "kilo.db", fixture=False)
    tk.add_session(con, "ses_a")
    for i, body in enumerate((init, review, old_review, "please review my plan for the init sequence")):
        tk.add_msg(con, "ses_a", f"m{i}", "user", tk.T0 + 10 * i, [tk.txt(body)])
    con.commit()
    src = Kilo()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["please review my plan for the init sequence"]


# --- P1-4: Cline new_task hand-off ------------------------------------------------------------------------------

HANDOFF = (
    "<task>\nContinue the parser refactor.\n</task>\n<context>Step 1 is done; src/parser.py is half migrated. "
    "Key concepts: the tokenizer yields spans, the parser builds nodes from spans, and the printer walks nodes. "
    "Pending: move the error recovery into the tokenizer and update the three call sites in src/cli.py.</context>"
)


def test_cline_classic_new_task_context_is_not_a_typed_first_prompt(home, me):
    data = home / ".cline" / "data"
    t0 = 1_769_000_000_000
    tcl.write_tasks(data, "t1", [
        tcl.say("task", "refactor the parser", t0),
        tcl.say("text", "Half way. Start a new task?", t0 + 1000),
        {"ts": t0 + 2000, "type": "ask", "ask": "new_task", "text": HANDOFF},
    ])
    # the accepted hand-off becomes the next task's first prompt (whitespace may differ); the user then types on
    tcl.write_tasks(data, "t2", [
        tcl.say("task", HANDOFF.replace("\n", "\n  "), t0 + 10_000),
        tcl.say("user_feedback", "also keep the old API", t0 + 11_000),
    ])
    tcl.write_tasks(data, "t3", [tcl.say("task", "an unrelated task I typed", t0 + 20_000)])
    src = Cline()
    got = {lc.path.parent.name: tcl.texts(run(src, lc, None, me)[0]) for lc in src.discover(me, {})}
    assert got == {"t1": ["refactor the parser"], "t2": ["also keep the old API"], "t3": ["an unrelated task I typed"]}


def test_cline_task_whose_first_prompt_equals_its_own_new_task_ask_is_kept(home, me):
    t0 = 1_769_000_000_000
    tcl.write_tasks(home / ".cline" / "data", "t1", [
        tcl.say("task", HANDOFF, t0),
        {"ts": t0 + 2000, "type": "ask", "ask": "new_task", "text": HANDOFF},
    ])
    src = Cline()
    (loc,) = src.discover(me, {})
    assert tcl.texts(run(src, loc, None, me)[0]) == [HANDOFF]


def test_cline_sdk_new_task_tool_call_context_is_not_typed(home, me):
    data = home / ".cline" / "data"
    call = {"id": "a1", "role": "assistant", "ts": 5,
            "content": [{"type": "tool_use", "id": "c1", "name": "new_task", "input": {"context": HANDOFF}}]}
    tcl.write_sdk(data, "s1", [tcl.sdk_user('<user_input mode="act">refactor the parser</user_input>', 1, "u1"), call])
    tcl.write_sdk(data, "s2", [tcl.sdk_user(f'<user_input mode="act">{HANDOFF}</user_input>', 10, "u1"),
                               tcl.sdk_user("and keep the old API", 11, "u2")])
    src = Cline()
    got = {lc.path.parent.name: tcl.texts(run(src, lc, None, me)[0]) for lc in src.discover(me, {})}
    assert got == {"s1": ["refactor the parser"], "s2": ["and keep the old API"]}


def test_cline_handoff_index_is_bounded_and_cached(home, me, monkeypatch):
    from mannerism.adapters import cline as mod

    data = home / ".cline" / "data"
    for i in range(6):
        tcl.write_tasks(data, f"t{i}", [tcl.say("task", f"unrelated prompt {i} " + "long " * 50, 1_769_000_000_000 + i)])
    src = Cline()
    locs = src.discover(me, {})
    monkeypatch.setattr(mod, "MAX_HANDOFF_FILES", 3)
    loads = []
    real = mod._load
    monkeypatch.setattr(mod, "_load", lambda p: loads.append(p) or real(p))
    mod._HANDOFFS.clear()
    for lc in locs:
        run(src, lc, None, me)
    # each task is loaded once to be read, plus at most MAX_HANDOFF_FILES sibling loads per first prompt, cached
    assert len(loads) <= len(locs) + 3 * 2


# --- P2-6: resolve() failing in discovery -----------------------------------------------------------------------


@pytest.fixture
def looping(monkeypatch):
    """Path.resolve raises RuntimeError (a symlink loop on Python 3.11) for anything under a folder named 'loop'."""
    real = Path.resolve

    def resolve(self, *a, **k):
        if "loop" in self.parts:
            raise RuntimeError("Symlink loop from " + str(self))
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", resolve)


def test_config_dirs_survives_a_symlink_loop(home, looping):
    good = home / "good"
    assert config_dirs(home / "loop" / "x", good, [home / "loop" / "y"]) == [good]


def test_aider_discovery_survives_a_symlink_loop(home, me, looping, tmp_path):
    (home / "loop").mkdir()
    (home / "loop" / HISTORY_FILE).write_text("\n# aider chat started at 2026-03-01 10:00:00\n\n#### hi there  \n")
    good = home / HISTORY_FILE
    good.write_text("\n# aider chat started at 2026-03-01 10:00:00\n\n#### hi there  \n")
    found = [lc.path for lc in Aider().discover(me, {"paths": [str(home / "loop" / HISTORY_FILE)]})]
    assert good in found


def test_crush_opencode_goose_discovery_survives_a_symlink_loop(home, me, looping):
    from .sqlite_helpers import install_fixture
    from .test_crush import make_db as crush_db

    crush_db(home / "good" / ".crush" / "crush.db", lambda c: None).close()
    crush_db(home / "loop" / ".crush" / "crush.db", lambda c: None).close()
    cfg = {"paths": [str(home / "loop"), str(home / "good")]}
    assert [lc.path for lc in Crush().discover(me, cfg)] == [home / "good" / ".crush" / "crush.db"]

    install_fixture("opencode", "opencode.db", home / ".local" / "share" / "opencode" / "opencode.db")
    install_fixture("opencode", "opencode.db", home / "loop" / "opencode.db")
    got = OpenCode().discover(me, {"dirs": [str(home / "loop")]})
    assert [lc.path.name for lc in got] == ["opencode.db"] and "loop" not in str(got[0].path)

    install_fixture("goose", "sessions.db", home / ".local" / "share" / "goose" / "sessions" / "sessions.db")
    install_fixture("goose", "sessions.db", home / "loop" / "sessions" / "sessions.db")
    got = Goose().discover(me, {"dirs": [str(home / "loop")]})
    assert len(got) == 1 and "loop" not in str(got[0].path)


# --- P2-11: the shared SQLite copy -----------------------------------------------------------------------------


def _db(path: Path, rows=3):
    con = sqlite3.connect(path)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE t (id TEXT PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t VALUES (?,?)", [(f"id{i}", f"value {i}") for i in range(rows)])
    con.commit()
    return con


def test_dbcopy_repeats_the_copy_when_a_checkpoint_happened_underneath(tmp_path, monkeypatch):
    """A checkpoint during the copy moves the main file and restarts the WAL; the torn copy must not be used."""
    p = tmp_path / "a.db"
    con = _db(p)
    real, calls = shutil.copyfile, []

    def copyfile(src, dst, *a, **k):
        out = real(src, dst, *a, **k)
        calls.append(str(src))
        if len(calls) == 1:  # the app checkpoints and writes again while the first copy is being made
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("INSERT INTO t VALUES ('late', 'written mid copy')")
            con.commit()
        return out

    monkeypatch.setattr(_util.shutil, "copyfile", copyfile)
    with DbCopy(p) as c:
        ids = {r[0] for r in c.execute("SELECT id FROM t")}
    assert "late" in ids  # the second attempt saw the write
    assert len(calls) > 2  # more than one pass over db + -wal
    assert DbCopy(p).ATTEMPTS == 3


def test_dbcopy_does_not_retry_for_wal_appends_alone(tmp_path, monkeypatch):
    """A live writer appends to the WAL all the time; that must not triple the copy cost."""
    p = tmp_path / "a.db"
    con = _db(p)
    real, calls = shutil.copyfile, []

    def copyfile(src, dst, *a, **k):
        out = real(src, dst, *a, **k)
        calls.append(str(src))
        con.execute("INSERT INTO t VALUES (?, 'appended')", (f"w{len(calls)}",))
        con.commit()  # appends WAL frames only
        return out

    monkeypatch.setattr(_util.shutil, "copyfile", copyfile)
    with DbCopy(p) as c:
        c.execute("SELECT count(*) FROM t").fetchone()
    assert calls.count(str(p)) == 1


def test_dbcopy_gives_up_after_three_attempts_and_reports_the_older_signature(tmp_path, monkeypatch):
    p = tmp_path / "a.db"
    con = _db(p)
    n = {"i": 0}
    real = shutil.copyfile

    def copyfile(src, dst, *a, **k):
        out = real(src, dst, *a, **k)
        if str(src) == str(p):
            n["i"] += 1
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.execute("INSERT INTO t VALUES (?, 'x')", (f"w{n['i']}",))
            con.commit()
        return out

    monkeypatch.setattr(_util.shutil, "copyfile", copyfile)
    copy = DbCopy(p)
    with copy as c:
        c.execute("SELECT count(*) FROM t").fetchone()
    assert n["i"] == 3  # bounded
    assert copy.sig != _util.db_sig(p)  # stale on purpose: the next sync sees a change and reads again


def test_db_sig_includes_the_wal(tmp_path):
    p = tmp_path / "a.db"
    con = _db(p)
    sig = _util.db_sig(p)
    assert len(sig) == 2
    con.execute("INSERT INTO t VALUES ('more', 'rows')")
    con.commit()
    assert _util.db_sig(p) != sig


def test_rowid_cursor_helpers(tmp_path):
    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE t (id TEXT PRIMARY KEY, v TEXT)")
    assert rowid_mark(con, "t", "id") == {"rowid": 0, "id": ""} and rowid_resume(con, "t", "id", {"rowid": 5}) == 0
    con.executemany("INSERT INTO t VALUES (?,?)", [("a", "1"), ("b", "2"), ("c", "3")])
    mark = rowid_mark(con, "t", "id")
    assert mark == {"rowid": 3, "id": "c"} and rowid_resume(con, "t", "id", mark) == 3
    con.execute("DELETE FROM t WHERE id = 'c'")
    assert rowid_resume(con, "t", "id", mark) == 0  # the row is gone
    con.execute("INSERT INTO t VALUES ('d', '4')")  # takes rowid 3 again
    assert rowid_resume(con, "t", "id", mark) == 0  # ... but it is another row
    for junk in (None, 7, {"rowid": "x"}, {"rowid": -1}, {}):
        assert rowid_resume(con, "t", "id", junk) == 0


def test_cline_short_first_prompts_do_not_scan_siblings(home, me, monkeypatch):
    from mannerism.adapters import cline as mod

    data = home / ".cline" / "data"
    for i in range(4):
        tcl.write_tasks(data, f"t{i}", [tcl.say("task", f"short prompt {i}", 1_769_000_000_000 + i)])
    loads = []
    real = mod._load
    monkeypatch.setattr(mod, "_load", lambda p: loads.append(p) or real(p))
    src = Cline()
    locs = src.discover(me, {})
    for lc in locs:
        run(src, lc, None, me)
    assert len(loads) == len(locs)  # one load per task, none for the index


def test_cline_handoff_source_is_found_beyond_the_scan_cap(home, me, monkeypatch):
    """Follow-up P1: the scan runs over OLDER tasks, newest first, so a cap does not hide the task right before."""
    from mannerism.adapters import cline as mod

    monkeypatch.setattr(mod, "MAX_HANDOFF_FILES", 5)
    data = home / ".cline" / "data"
    t0 = 1_769_000_000_000
    for i in range(30):  # many older tasks without a hand-off ...
        tcl.write_tasks(data, str(t0 + i), [tcl.say("task", f"old task {i}", t0 + i)])
    tcl.write_tasks(data, str(t0 + 100), [tcl.say("task", "the source task", t0 + 100),
                                         {"ts": t0 + 101, "type": "ask", "ask": "new_task", "text": HANDOFF}])
    tcl.write_tasks(data, str(t0 + 200), [tcl.say("task", HANDOFF, t0 + 200), tcl.say("user_feedback", "go on", t0 + 201)])
    tcl.write_tasks(data, str(t0 + 300), [tcl.say("task", "a newer task, after the hand-off", t0 + 300)])
    src = Cline()
    got = {lc.path.parent.name: tcl.texts(run(src, lc, None, me)[0]) for lc in src.discover(me, {})}
    assert got[str(t0 + 200)] == ["go on"]
    # a task is never compared with tasks created after it
    tcl.write_tasks(data, str(t0 + 50), [tcl.say("task", HANDOFF, t0 + 50)])
    (loc,) = [lc for lc in src.discover(me, {}) if lc.path.parent.name == str(t0 + 50)]
    assert tcl.texts(run(src, loc, None, me)[0]) == [HANDOFF]


def test_cline_handoff_decision_is_kept_in_the_cursor(home, me, monkeypatch):
    from mannerism.adapters import cline as mod

    data = home / ".cline" / "data"
    t0 = 1_769_000_000_000
    long_prompt = ("a typed prompt that is long enough to be checked " * 6).strip()
    tcl.write_tasks(data, str(t0), [tcl.say("task", "earlier", t0)])
    path = tcl.write_tasks(data, str(t0 + 10), [tcl.say("task", long_prompt, t0 + 10)])
    src = Cline()
    (loc,) = [lc for lc in src.discover(me, {}) if lc.path == path]
    units, cur = run(src, loc, None, me)
    assert tcl.texts(units) == [long_prompt] and cur["handoff"][1] is False
    path.write_text(json.dumps([tcl.say("task", long_prompt, t0 + 10), tcl.say("user_feedback", "and more", t0 + 11)]))
    calls = []
    real = mod._file_handoffs
    monkeypatch.setattr(mod, "_file_handoffs", lambda p: calls.append(p) or real(p))
    units, cur2 = run(src, loc, cur, me)  # the file changed, the task is read again ...
    assert tcl.texts(units) == [long_prompt, "and more"] and calls == []  # ... but no sibling is scanned again
    assert cur2["handoff"] == cur["handoff"]


def test_cline_files_without_new_task_are_not_parsed(home, me, monkeypatch):
    from mannerism.adapters import cline as mod

    data = home / ".cline" / "data"
    t0 = 1_769_000_000_000
    for i in range(5):
        tcl.write_tasks(data, str(t0 + i), [tcl.say("task", f"old {i}", t0 + i)])
    path = tcl.write_tasks(data, str(t0 + 50), [tcl.say("task", "x" * 300, t0 + 50)])
    loads = []
    real = mod._load
    monkeypatch.setattr(mod, "_load", lambda p: loads.append(p) or real(p))
    mod._HANDOFFS.clear()
    src = Cline()
    (loc,) = [lc for lc in src.discover(me, {}) if lc.path == path]
    run(src, loc, None, me)
    assert loads == [path]
