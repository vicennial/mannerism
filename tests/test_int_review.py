"""Regression tests for the code review of the setup features (exclusion rules, machine-typed screening, groups, scan,
hooks). One section per finding; the scenarios are the reviewer's, with synthetic data only."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_groups import OLD_UNITS
from transcripts import BOILERPLATE, HUMAN, STATUS, write_claude, write_codex

from mannerism import (
    adapters,
    api,
    capture,
    cli,
    config,
    exclude,
    groups,
    hookroots,
    hooks,
    humanness,
    intake,
    refresh,
    rules,
    scan,
    store,
    sync,
)
from mannerism.adapters.claude_code import ClaudeCode
from mannerism.core import Location, Me, Unit

START = datetime(2026, 9, 1, tzinfo=UTC)
RELAY = ('<x-message from="orchestrator">Lane {n} status: build is green, two reviews are pending, please continue with '
         'the next step of the plan</x-message>')
TMPL = "Please continue with the next step of the plan and report back when you are done here"
STRUCT = "Status update\ntask: build-{n}\nstate: passed\nduration: 31s\nowner: ci-bot\nnext: deploy"
KV = "Status report for the nightly build job:\nbranch: main\nresult: passed\ntests: {n}\nduration: 4m"


def mk(i, text, *, sess=None, account="~/.claude", project="app", kind="interactive", register="agent", source="claude_code",
       gap_s=None, ts=None):
    sess = sess if sess is not None else f"s{i}"
    return Unit(source=source, source_ref=f"r{i}", thread_id=f"th{sess}", audience="agent",
                ts=ts or START + timedelta(minutes=i), text=text, kind="prompt", register=register, account=account,
                project=project, session_kind=kind, session=sess, gap_s=gap_s)


def put(con, units, cfg=None, loc="loc"):
    """What a sync does for one location: screen, write, rescore. Returns (Intake, Intaken)."""
    scr = intake.Intake(con, cfg or config.Config())
    taken = scr.apply(units)
    store.write_units(con, units[0].source, loc, taken.units, {"o": 1})
    scr.after_write(con, taken.units)
    return scr, taken


def rows(con, sql="SELECT unit_id, session, human_score, held, score_json FROM units ORDER BY ts"):
    return [dict(r) for r in con.execute(sql)]


@pytest.fixture
def con(home):
    c = store.connect()
    yield c
    c.close()


# --- P1-1: a stored text below LOW is held, never released; rescoring uses real counts ---------------------------------

def test_a_stored_text_that_falls_below_low_stays_held(con):
    """Three sessions of a status template, five prompts each (the reviewer's rescore2 scenario): when a session becomes
    "structured" the stored texts score below LOW. They must stay held, not enter the bank."""
    i = 0
    for sess in range(3):
        for k in range(5):
            text = "thanks, that looks right to me, go ahead and merge it" if (sess == 2 and k == 4) else KV.format(n=100 + i)
            i += 1
            put(con, [mk(i, text, sess=f"S{sess}")], loc=f"loc{sess}")
    got = rows(con)
    below = [r for r in got if r["human_score"] < humanness.LOW]
    assert below, "the scenario must push stored texts below LOW"
    assert all(r["held"] == 1 for r in below)
    assert [r["held"] for r in got if r["human_score"] >= humanness.REVIEW] == [0]  # the human reply is normal text
    refresh.refresh(con, ["agent"])
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 1


def test_only_ingest_drops_and_a_rescore_never_deletes(con):
    for sess in range(8):
        put(con, [mk(sess, RELAY.format(n=sess), sess=f"S{sess}")], loc=f"loc{sess}")
    n_stored = con.execute("SELECT count(*) FROM units").fetchone()[0]
    assert 0 < n_stored < 8  # the first sessions are stored, later ones were clear enough to drop on arrival
    assert all(r["held"] == 1 for r in rows(con))
    put(con, [mk(100, RELAY.format(n=100), sess="S100")], loc="loc100")
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] <= n_stored + 1


def test_updating_a_stored_unit_recomputes_its_session_and_keeps_the_new_text(con):
    initial = [mk(i, STRUCT.format(n=i), sess="S") for i in range(1, 4)]
    initial += [mk(4, "please explain why the export test failed on the slow runner", sess="S"),
                mk(5, "yes, that plan looks right to me; please continue with it", sess="S")]
    put(con, initial)
    assert con.execute("SELECT count(*) FROM units WHERE held=1").fetchone()[0] == 0

    changed = mk(4, "<status-report>\nstatus: done\nowner: ci-bot\nnext: deploy\nnote: generated\n</status-report>",
                 sess="S")
    _scr, taken = put(con, [changed])
    assert len(taken.units) == 1 and taken.filtered == 0
    row = con.execute("SELECT text, human_score, held FROM units WHERE source_ref='r4'").fetchone()
    assert row["text"] == changed.text and row["human_score"] < humanness.LOW and row["held"] == 1
    assert con.execute("SELECT count(*) FROM units WHERE held=1").fetchone()[0] == 4


def test_a_stored_replacement_changes_counts_before_new_units_are_filtered(con):
    machine = [mk(i, json.dumps({"event": "generated", "item": i, "status": "done"}), sess="S")
               for i in range(1, 5)]
    human = mk(5, "please explain why the export test failed on the slow runner", sess="S")
    for u in [*machine, human]:
        sig = humanness.analyse(u.text)
        u.template = sig.template
        u.human_score, u.score_info = humanness.score(sig)
        u.held = humanness.holds(u.human_score)
    store.write_units(con, "claude_code", "loc", [*machine, human], {})

    replacement = mk(1, "I rewrote this prompt by hand after checking the export failure", sess="S")
    added = mk(6, json.dumps({"event": "generated", "item": 6, "status": "done"}), sess="S")
    _scr, taken = put(con, [replacement, added])
    assert {u.source_ref for u in taken.units} == {"r1", "r6"} and taken.filtered == 0
    row = con.execute("SELECT human_score, held FROM units WHERE source_ref='r6'").fetchone()
    assert row["human_score"] == pytest.approx(0.4) and row["held"] == 1


def test_template_rescoring_keeps_each_texts_real_session_counts(con):
    """The reviewer's rescore3: a template shared by sessions that are not in the current batch. S0 is a structured
    session (4 of 5 prompts); when the template reaches 6 sessions its text is scored again and must keep S0's session
    penalty (the old code only knew the sessions of the current batch)."""
    put(con, [mk(1, TMPL, sess="S1")], loc="a")
    put(con, [mk(2, TMPL, sess="S2")], loc="b")
    for n in range(4):
        put(con, [mk(10 + n, STRUCT.format(n=n), sess="S0")], loc="c")
    put(con, [mk(20, TMPL, sess="S0")], loc="c")
    uid = con.execute("SELECT unit_id FROM units WHERE source_ref='r20'").fetchone()[0]

    def state():
        r = con.execute("SELECT human_score, held, score_json FROM units WHERE unit_id=?", (uid,)).fetchone()
        return r[0], r[1], json.loads(r[2])["reasons"]

    assert "session:4/5 structured" in state()[2]
    for k, s in enumerate(("S3", "S4", "S5")):  # the third makes 6 sessions: the template penalty steps up
        put(con, [mk(30 + k, TMPL, sess=s)], loc=f"d{k}")
    value, held, reasons = state()
    assert "template:6 sessions" in reasons and "session:4/5 structured" in reasons
    assert value == pytest.approx(1 - 0.40 - 0.25) and held == 1


def test_rescan_holds_relay_texts_of_a_store_from_before_screening(con):
    """The reviewer's rescan: eight sessions of one relay wrapper, never scored. Each scores below LOW and must be held."""
    old = [mk(s, RELAY.format(n=s), sess=f"S{s}", account=None, project=None, kind=None) for s in range(8)]
    store.write_units(con, "claude_code", "loc", old, {"o": 1})
    res = humanness.rescan_stored(con, groups.kept_groups(con), groups.group_ids_of_dict)
    assert res["scored"] == 8 and res["held"] == 8
    assert {(r["held"], r["human_score"] < humanness.LOW) for r in rows(con)} == {(1, True)}
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 8  # nothing deleted


def test_the_design_claim_holds_rescoring_happens_when_a_count_crosses_a_threshold(con):
    from mannerism import humanness as h
    assert h.template_tier(2) == 0 and h.template_tier(3) == 1 and h.template_tier(6) == 2 and h.template_tier(12) == 3
    doc = (Path(__file__).resolve().parent.parent / "docs" / "design.md").read_text("utf-8")
    assert "crosses a threshold" in doc or "crossed a threshold" in doc


# --- P2-3: rescore only at thresholds, and the session lookup uses an index ---------------------------------------------

def test_stored_texts_are_rescored_only_when_a_count_crosses_a_threshold(con):
    changed_at = {}
    for s in range(1, 41):
        scr = intake.Intake(con, config.Config())
        u = mk(s, BOILERPLATE.format(n=100 + s), sess=f"S{s}")
        taken = scr.apply([u])
        store.write_units(con, "claude_code", f"loc{s}", taken.units, {"o": s})
        before = con.total_changes
        scr.after_write(con, taken.units)
        changed_at[s] = con.total_changes - before
    assert all(n == 0 for s, n in changed_at.items() if s not in (3, 6, 12)), changed_at
    assert sum(changed_at.values()) <= 3 + 6 + 12 + 3  # the rows of the template at each of the three thresholds


def test_ingesting_many_sessions_stays_fast_and_the_session_lookup_is_indexed(con):
    plan = " ".join(str(r[3]) for r in con.execute(
        "EXPLAIN QUERY PLAN SELECT count(*) FROM units WHERE coalesce(session, thread_id)=?", ("x",)))
    assert "units_sess_key" in plan
    t0 = time.monotonic()
    for s in range(1, 301):  # 300 sessions of one template: the old code rewrote every stored row each time
        put(con, [mk(s, BOILERPLATE.format(n=s), sess=f"S{s}")], loc=f"loc{s}")
    assert time.monotonic() - t0 < 30
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 300


# --- P2-6: a unit of a confirmed ("it's me") group is never dropped ----------------------------------------------------------

def test_a_unit_in_a_confirmed_group_is_stored_as_normal_text_even_below_low(con):
    gid = groups.origin_group_id("claude_code", "~/.claude", "standup", "interactive")
    con.execute("INSERT INTO group_decisions(group_id, decision, decided_at) VALUES (?, 'keep', '2026-10-01')", (gid,))
    text = "<status-report>\nstatus: done with the export fix\nnext: deploy the hotfix\nowner: jo\nnote: fine\n</status-report>"
    u = mk(1, text, project="standup", gap_s=2.0)
    _scr, taken = put(con, [u])
    assert u.human_score < humanness.LOW and len(taken.units) == 1 and taken.filtered == 0
    assert u.held is False
    assert rows(con)[0]["held"] == 0
    other = mk(2, text, project="elsewhere", gap_s=2.0)  # another group is judged as before
    _scr, taken = put(con, [other])
    assert taken.filtered == 1


def test_scan_agrees_a_confirmed_group_is_neither_filtered_nor_held(home, _isolated_env):
    start = datetime(2026, 5, 1, 9, tzinfo=UTC)
    for i in range(14):
        write_claude(_isolated_env / ".claude", "-srv-orch-run", f"cccccccc-0000-0000-0000-{i:012d}",
                     [BOILERPLATE.format(n=100 + i), STATUS.format(n=i)], "/srv/orch/run", start=start + timedelta(days=i),
                     gap_min=0.01)
    cfg = config.load_config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    res = scan.scan(samples=0)
    g = next(x for x in res["groups"] if x["project"] == "run")
    assert g["filtered"] + g["held"] > 0
    con = store.connect()
    con.execute("INSERT INTO group_decisions(group_id, decision, decided_at) VALUES (?, 'keep', '2026-10-01')", (g["group_id"],))
    con.close()
    g2 = next(x for x in scan.scan(samples=0)["groups"] if x["project"] == "run")
    assert g2["filtered"] == 0 and g2["held"] == 0


# --- P1-2: a file or hook sync skips a source that is switched off -----------------------------------------------------------

def _transcript_world(home):
    cfg = config.Config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    return write_claude(home / ".claude", "-home-jo-work-app", "aaaaaaaa-0000-0000-0000-000000000001", HUMAN[:4],
                        str(home / "work/app"), start=datetime(2026, 5, 1, tzinfo=UTC))


def test_a_file_sync_that_loses_the_race_with_forget_source_stores_nothing(_isolated_env):
    p = _transcript_world(_isolated_env)
    assert sync.sync()["new_units"] == 4
    assert api.forget(source="claude_code")["ok"]
    assert "claude_code" not in config.load_config().enabled_sources()
    # the hook child that was waiting on the lock now gets it: it re-reads the config and finds the source off
    res = sync.sync(True, ["claude_code"], None, False, files=[str(p)])
    con = store.connect()
    assert res["new_units"] == 0 and con.execute("SELECT count(*) FROM units").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM cursors").fetchone()[0] == 0
    assert "claude_code" not in res["sources"]


def test_a_file_sync_for_an_enabled_source_still_reads_the_file(_isolated_env):
    p = _transcript_world(_isolated_env)
    res = sync.sync(True, ["claude_code"], None, False, files=[str(p)])
    assert res["new_units"] == 4


# --- P2-1: regular expressions are guarded without signals -----------------------------------------------------------------

SLOW = "a*a*a*a*a*a*a*Z"  # passes the static check (no nesting) and runs for ever on a long run of "a"
SLOW_TEXT = "a" * 200


def test_a_regex_rule_does_not_clobber_another_alarm_or_itimer():
    seen = []
    old = signal.signal(signal.SIGALRM, lambda *a: seen.append("mine"))
    try:
        signal.setitimer(signal.ITIMER_REAL, 30)
        assert exclude._search("nightly", "a nightly text checked against a [[exclude]] text rule") is True
        assert exclude._search(SLOW, SLOW_TEXT) is True  # times out, counts as a match, fail closed
        remaining = signal.getitimer(signal.ITIMER_REAL)[0]
        assert 20 < remaining <= 30  # the caller's timer (the detached sync's 60 s kill) is untouched
        assert signal.getsignal(signal.SIGALRM) is not signal.SIG_DFL and not seen
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def test_the_guard_works_off_the_main_thread():
    out = {}

    def run():
        out["fast"] = exclude._search("needle", "x" * 100 + "needle")
        out["timeout"] = exclude._search(SLOW + "c", SLOW_TEXT, timeout_result=False)

    t = threading.Thread(target=run, daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(20)
    assert not t.is_alive() and time.monotonic() - t0 < 15
    assert out == {"fast": True, "timeout": False}


def test_a_timeout_is_a_match_at_ingest_and_never_deletes_stored_text(home, monkeypatch):
    units = [mk(1, "please keep this prompt about the export fix, " + SLOW_TEXT, sess="S1"),
             mk(2, "a perfectly ordinary prompt about the export fix and the test", sess="S2")]

    class Feed:
        name, tier = "claude_code", "auto"

        def discover(self, me, cfg):
            return [Location("claude_code", "feed", None, "feed")]

        def read(self, loc, cursor, me):
            return list(units), {}

    monkeypatch.setattr(adapters, "load", lambda n: Feed())
    cfg = config.Config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": []}
    cfg.exclude = [{"text": SLOW}]
    config.save_config(cfg)
    before = exclude.STATS["regex_timeouts"]
    res = sync.sync()
    con = store.connect()
    assert [r[0] for r in con.execute("SELECT source_ref FROM units")] == ["r2"]  # r1 timed out: excluded
    assert res["excluded"] == 1 and exclude.STATS["regex_timeouts"] > before and res["exclude_regex_timeouts"] >= 1
    # the retroactive delete treats a timeout as "no match": stored text is never removed because a pattern is slow
    store.write_units(con, "claude_code", "x", [units[0]], {})
    assert exclude.matching_unit_ids(con, [exclude.make_rule(text=SLOW)]) == []


def test_a_timeout_during_reingest_does_not_delete_the_stored_copy(con, monkeypatch):
    original = mk(1, "please keep this prompt about the export fix and its tests", sess="S1")
    store.write_units(con, "claude_code", "loc", [original], {})
    changed = mk(1, "a" * 200, sess="S1")
    pattern = SLOW + "Q"
    exclude._POISONED.pop(pattern, None)
    monkeypatch.setattr(exclude._WORKER, "broken", False)
    monkeypatch.setattr(exclude._WORKER, "search", lambda *_a: None)
    cfg = config.Config()
    cfg.exclude = [{"text": pattern}]
    taken = intake.Intake(con, cfg).apply([changed])
    assert taken.excluded == 1 and taken.excluded_refs == []
    store.write_units(con, "claude_code", "loc", taken.units, {"o": 2}, delete_refs=taken.excluded_refs)
    assert con.execute("SELECT text FROM units WHERE source_ref='r1'").fetchone()[0] == original.text


def test_a_pattern_that_keeps_timing_out_is_not_run_again():
    exclude._POISONED.pop(SLOW + "d", None)
    t0 = time.monotonic()
    for _ in range(exclude.REGEX_POISON_AFTER + 20):
        assert exclude._search(SLOW + "d", SLOW_TEXT) is True
    assert time.monotonic() - t0 < exclude.REGEX_POISON_AFTER * 1.5 + 5


def test_pattern_validity_is_static_and_never_depends_on_timing(monkeypatch):
    monkeypatch.setattr(exclude, "_search", lambda *a, **k: pytest.fail("check_pattern must not run the pattern"))
    monkeypatch.setattr(time, "monotonic", lambda: pytest.fail("no clock in the check"))
    for bad in ("(a+)+$", "(a|aa)+$", "(x*y*)*z", "((a+))*"):
        with pytest.raises(exclude.RuleError):
            exclude.check_pattern(bad)
    for good in (r"^\[nightly\]", r"(?:foo|bar)+x", r"(\d{1,3}\.){3}\d+", "a*b*c*d", "needle"):
        exclude.check_pattern(good)


# --- P3-7: a broken stored regex is skipped with a warning ------------------------------------------------------------------

def test_a_broken_stored_regex_is_skipped_and_reported_not_raised_everywhere(home, monkeypatch):
    con = store.connect()
    con.execute("INSERT INTO exclusions(source, text, created_at) VALUES ('claude_code', '(unclosed', '2026-10-01')")
    con.execute("INSERT INTO exclusions(project, created_at) VALUES ('scratch-*', '2026-10-01')")
    rules_, errors = exclude.store_rules_checked(con)
    assert [r.project for r in rules_] == ["scratch-*"] and len(errors) == 1 and "rule 1" in errors[0]
    scr = intake.Intake(con, config.Config())
    taken = scr.apply([mk(1, "a normal prompt about the export fix and its tests", project="app"),
                       mk(2, "another normal prompt about the loader and its tests", project="scratch-1")])
    assert [u.source_ref for u in taken.units] == ["r1"] and scr.rule_errors == errors
    cfg = config.Config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    assert any("rule 1" in e for e in sync.sync()["exclude_errors"])
    assert scan.scan(samples=0)["exclude_errors"]


# --- P2-2: the Stop hook is cheap; file syncs only ingest and screen ----------------------------------------------------------

def _payload(path, **extra):
    return json.dumps({"session_id": "s", "transcript_path": str(path), "cwd": "/work/demo", **extra}).encode()


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(sync, "sync_in_background", lambda *a, **k: calls.append(k) or {"started": True})
    return calls


def _enable(**dirs):
    cfg = config.Config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": [], **dirs}
    config.save_config(cfg)


def test_stop_does_nothing_for_the_default_or_a_configured_root(_isolated_env, tmp_path, monkeypatch, spawned):
    configured = tmp_path / "work-account"
    _enable(dirs=[str(configured)])
    for root in (_isolated_env / ".claude", configured):
        p = write_claude(root, "-w-demo", "aaaaaaaa-0000-0000-0000-000000000001", HUMAN[:1], "/w/demo", start=START)
        monkeypatch.setattr(hooks, "read_stdin", lambda *a, p=p, **k: _payload(p))
        assert hooks.run("stop") == 0
    assert spawned == []


def test_stop_syncs_a_throwaway_root_and_session_end_adds_one_refresh(_isolated_env, tmp_path, monkeypatch, spawned):
    _enable()
    p = write_claude(tmp_path / "eph", "-w-demo", "aaaaaaaa-0000-0000-0000-000000000001", HUMAN[:1], "/w/demo", start=START)
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: _payload(p))
    assert hooks.run("stop") == 0 and hooks.run("session-end") == 0
    assert [(k["sources"], k["files"], k["refresh"]) for k in spawned] == [
        (["claude_code"], [str(p)], False), (["claude_code"], [str(p)], True)]
    assert hookroots.registered("claude_code") == [tmp_path / "eph"]


def test_session_end_still_syncs_a_default_root(_isolated_env, monkeypatch, spawned):
    _enable()
    p = write_claude(_isolated_env / ".claude", "-w-demo", "aaaaaaaa-0000-0000-0000-000000000001", HUMAN[:1], "/w/demo",
                     start=START)
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: _payload(p))
    assert hooks.run("session-end") == 0 and [k["refresh"] for k in spawned] == [True]


def test_a_registry_error_never_skips_the_file_sync(_isolated_env, tmp_path, monkeypatch, spawned):
    _enable()
    p = write_claude(tmp_path / "eph", "-w-demo", "aaaaaaaa-0000-0000-0000-000000000001", HUMAN[:1], "/w/demo", start=START)
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: _payload(p))
    monkeypatch.setattr(hookroots, "record", lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    assert hooks.run("stop") == 0
    assert len(spawned) == 1 and spawned[0]["files"] == [str(p)]


def test_a_file_sync_ingests_and_screens_but_does_not_refresh_or_queue_work(_isolated_env, monkeypatch):
    p = _transcript_world(_isolated_env)
    boom = lambda *a, **k: pytest.fail("a file sync must not rebuild or queue anything")  # noqa: E731
    monkeypatch.setattr(sync.refresh, "refresh", boom)
    monkeypatch.setattr(sync.packets, "queue_work", boom)
    monkeypatch.setattr(sync.capture, "capture_new_units", boom)
    res = sync.sync(True, ["claude_code"], None, False, files=[str(p)])
    assert res["new_units"] == 4 and res["refresh_pending"] is True and "refresh" not in res
    assert sync.refresh_pending()
    con = store.connect()
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0


def test_the_next_normal_sync_does_the_pending_refresh(_isolated_env):
    p = _transcript_world(_isolated_env)
    sync.sync(True, ["claude_code"], None, False, files=[str(p)])
    assert sync.refresh_pending()
    res = sync.sync(True)  # the SessionStart quick sync
    con = store.connect()
    assert not sync.refresh_pending(con) and "agent" in res["registers_changed"]
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] > 0


def test_a_file_sync_with_refresh_is_the_session_end_sync(_isolated_env):
    p = _transcript_world(_isolated_env)
    res = sync.sync(True, ["claude_code"], None, False, files=[str(p)], refresh=True)
    assert not sync.refresh_pending() and "agent" in res["registers_changed"]
    assert store.connect().execute("SELECT count(*) FROM bank").fetchone()[0] > 0


def test_the_file_sync_child_asks_for_its_hard_time_limit(monkeypatch):
    alarms = []
    monkeypatch.setattr(sync.signal, "alarm", alarms.append, raising=False)
    monkeypatch.setattr(sync, "sync", lambda *a, **k: {"status": "ok"})
    sync.main(["--file", "/x", "--source", "claude_code"])
    assert sync.FILE_SYNC_HARD_LIMIT_S == 60 and alarms == [60]


def test_the_hard_time_limit_survives_guarded_regex_matches():
    """Regex guarding used to cancel SIGALRM: the 60 s kill of the detached child must outlive a text rule."""
    signal.alarm(60)
    try:
        exclude._search("x", "text")
        exclude._search(SLOW, SLOW_TEXT)  # times out
        assert signal.alarm(0) > 50
    finally:
        signal.alarm(0)


# --- P2-4: held and excluded units are not captured, distilled, or matched from the prompt hook ------------------------------

DRAFT = "Here is the weekly summary of the export work: the loader was rewritten and the tests were added to the suite"


def test_capture_does_not_pair_a_held_unit_with_a_draft_until_it_is_released(con):
    capture.log_draft(DRAFT, "agent", con=con, now=START - timedelta(hours=1))
    held = mk(1, DRAFT.replace("weekly summary", "daily summary"), sess="S1")
    held.held = True
    store.write_units(con, "claude_code", "loc", [held], {})
    assert capture.capture_new_units(con, [held.unit_id])["unmatched"] == 0
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    con.execute("UPDATE units SET held=0")
    capture.capture_new_units(con, [held.unit_id])
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 1


def test_its_me_pairs_the_units_it_releases(home):
    con = store.connect()
    capture.log_draft(DRAFT, "agent", con=con, now=START - timedelta(hours=1))
    u = mk(1, DRAFT.replace("weekly summary", "daily summary"), sess="S1")
    u.held = True
    store.write_units(con, "claude_code", "loc", [u], {})
    gid = groups.origin_group_id("claude_code", "~/.claude", "app", "interactive")
    con.close()
    assert groups.its_me(gid)["released"] == 1
    con = store.connect()
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 1


def test_distill_skips_edits_whose_unit_is_held(con):
    ids = []
    for i in range(6):
        u = mk(i, f"please keep the summary short and skip the greeting, item {i}", sess=f"S{i}")
        u.held = i < 3
        store.write_units(con, "claude_code", f"l{i}", [u], {})
        cur = con.execute("INSERT INTO edits(ts, register, draft, final, status, origin, unit_id, final_sha, thread_key) "
                          "VALUES (?, 'agent', 'Hi! Here is a long summary ...', ?, 'edited', 'capture', ?, ?, ?)",
                          ("2026-09-02T00:00:00+00:00", u.text, u.unit_id, u.unit_id, f"t{i}"))
        ids.append(cur.lastrowid)
    pending = [r["edit_id"] for r in rules.pending_edits(con, "agent")]
    assert sorted(pending) == sorted(ids[3:])
    assert "agent" not in rules.distill_registers(con) or len(rules.pending_edits(con, "agent")) == 3


def test_the_prompt_hook_applies_the_exclusion_rules_with_the_payload_cwd(home, tmp_path):
    con = store.connect()
    capture.log_draft(DRAFT, "agent", con=con)
    edited = DRAFT.replace("weekly summary", "daily summary")
    cfg = config.Config()
    cfg.exclude = [{"project": "scratch-*"}]
    config.save_config(cfg)
    assert capture.match_prompt(edited, con=con, cwd="/work/scratch-9") is None
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert capture.match_prompt(edited, con=con, cwd="/work/app")["status"] == "edited"
    root = tmp_path / "acct"
    cfg.exclude = [{"source": "claude_code", "account": str(root)}]
    config.save_config(cfg)
    transcript = root / "projects" / "-w-x" / "abc.jsonl"
    other = DRAFT.replace("weekly summary", "monthly summary")
    assert capture.match_prompt(other, con=con, cwd="/work/app", transcript_path=str(transcript)) is None
    assert capture.match_prompt(other, con=con, cwd="/work/app", transcript_path="/elsewhere/projects/-w/abc.jsonl")
    capture.log_draft(DRAFT.replace("weekly", "quarterly"), "agent", con=con)
    third = DRAFT.replace("weekly summary", "quarterly report")
    gid = groups.origin_group_id("claude_code", str(root), "app", "interactive")
    cfg.exclude = [{"group": gid}]
    config.save_config(cfg)
    assert capture.match_prompt(third, con=con, cwd="/work/app", transcript_path=str(transcript)) is None


def test_prompt_hook_uses_claude_ide_session_metadata_for_group_rules(home, tmp_path):
    con = store.connect()
    capture.log_draft(DRAFT, "agent", con=con)
    edited = DRAFT.replace("weekly summary", "daily summary")
    root = tmp_path / "claude-ide"
    transcript = write_claude(root, "-work-app", "a" * 36, ["an earlier IDE prompt about this project"],
                              "/work/app", start=START, entrypoint="claude-vscode")
    cfg = config.Config()
    cfg.exclude = [{"group": groups.origin_group_id("claude_code", str(root), "app", "ide")}]
    config.save_config(cfg)
    assert capture.match_prompt(edited, con=con, cwd="/work/app", transcript_path=str(transcript)) is None
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0


def test_prompt_hook_uses_codex_ide_session_metadata_for_group_rules(home, tmp_path):
    con = store.connect()
    capture.log_draft(DRAFT, "agent", con=con)
    edited = DRAFT.replace("weekly summary", "daily summary")
    root = tmp_path / "codex-ide"
    transcript = write_codex(root, "b" * 36, ["an earlier IDE prompt about this project"], "/work/app",
                             start=START, source="vscode")
    cfg = config.Config()
    cfg.exclude = [{"group": groups.origin_group_id("codex", str(root), "app", "ide")}]
    config.save_config(cfg)
    assert capture.match_prompt(edited, con=con, cwd="/work/app", transcript_path=str(transcript)) is None
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0


def test_prompt_hook_defers_kind_rules_when_the_transcript_path_is_missing(home):
    con = store.connect()
    capture.log_draft(DRAFT, "agent", con=con)
    cfg = config.Config()
    cfg.exclude = [{"kind": "ide"}]
    config.save_config(cfg)
    edited = DRAFT.replace("weekly summary", "daily summary")
    assert capture.match_prompt(edited, con=con, cwd="/work/app", transcript_path=None) is None
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0


def test_the_prompt_hook_entry_point_passes_cwd_and_transcript(home, monkeypatch):
    cfg = config.Config()
    cfg.exclude = [{"project": "scratch-*"}]
    config.save_config(cfg)
    store.connect().close()
    con = store.connect()
    capture.log_draft(DRAFT, "agent", con=con)
    con.close()
    edited = DRAFT.replace("weekly summary", "daily summary")
    body = json.dumps({"prompt": edited, "cwd": "/work/scratch-1"}).encode()
    monkeypatch.setattr(hooks, "read_stdin", lambda *a, **k: body)
    assert hooks.run("prompt") == 0
    assert store.connect().execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    body = json.dumps({"prompt": edited, "cwd": "/work/app"}).encode()
    assert hooks.run("prompt") == 0
    assert store.connect().execute("SELECT count(*) FROM edits").fetchone()[0] == 1


# --- P2-5: retroactive delete keeps the rule evidence that survives ---------------------------------------------------------------

def test_a_retroactive_delete_removes_only_the_deleted_edits_from_a_rule(home):
    config.save_config(config.Config())
    con = store.connect()
    units = [mk(i, f"please keep the summary short and skip the greeting, item {i}", project="mine") for i in range(9)]
    units.append(mk(9, "please keep the summary short and skip the greeting, item 9", project="relay-scratch"))
    store.write_units(con, "claude_code", "loc", units, {})
    ids = []
    for x in units:
        cur = con.execute("INSERT INTO edits(ts, register, draft, final, status, origin, unit_id, final_sha, distilled, "
                          "thread_key) VALUES (?, 'agent', 'Hi! Here is a long summary ...', ?, 'edited', 'capture', ?, ?, 1, ?)",
                          ("2026-09-02T00:00:00+00:00", x.text, x.unit_id, x.unit_id, x.thread_id))
        ids.append(cur.lastrowid)
    # Simulate a rule written by the old merge logic: ten citations but support drifted down to two.
    con.execute("INSERT INTO rules(register, text, support, created_at, last_seen, edit_ids_json) VALUES "
                "('agent', 'Skip greetings; keep summaries short', 2, '2026-09-03', '2026-09-03', ?)", (json.dumps(ids),))
    con.execute("INSERT INTO rules(register, text, support, created_at, last_seen, edit_ids_json) VALUES "
                "('agent', 'Only from the relay', 1, '2026-09-03', '2026-09-03', ?)", (json.dumps([ids[9]]),))
    con.close()
    assert api.exclude_add(project="relay-*")["removed"] == 1
    con = store.connect()
    left = con.execute("SELECT text, support, edit_ids_json FROM rules").fetchall()
    assert [r["text"] for r in left] == ["Skip greetings; keep summaries short"]  # the rule with no support left is gone
    assert left[0]["support"] == 9 and json.loads(left[0]["edit_ids_json"]) == ids[:9]
    assert con.execute("SELECT count(*) FROM edits WHERE distilled=1").fetchone()[0] == 9  # kept edits keep their state


def test_group_expected_count_is_checked_inside_each_write_lock(home):
    config.save_config(config.Config())
    con = store.connect()
    drop = [mk(101 + i, f"a normal prompt about export test number {i}", project="drop") for i in range(2)]
    keep = [mk(201 + i, RELAY.format(n=i), project="keep") for i in range(2)]
    for u in keep:
        u.held = True
    store.write_units(con, "claude_code", "drop", drop, {})
    store.write_units(con, "claude_code", "keep", keep, {})
    con.close()

    drop_gid = groups.origin_group_id("claude_code", "~/.claude", "drop", "interactive")
    keep_gid = groups.origin_group_id("claude_code", "~/.claude", "keep", "interactive")
    assert groups.not_me(drop_gid, expected_units=1) == {"ok": False, "error": "this group changed, refresh"}
    assert groups.its_me(keep_gid, expected_units=1) == {"ok": False, "error": "this group changed, refresh"}
    con = store.connect()
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 4
    assert con.execute("SELECT count(*) FROM exclusions").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM units WHERE held=1").fetchone()[0] == 2


# --- P2-7: mixed groups, the refusal, the hint and the backfill ----------------------------------------------------------------

def _old_store(home, n=6):
    """Units stored by a version without origin tracking: no metadata, never scored."""
    con = store.connect()
    old = [Unit(source="claude_code", source_ref=f"r{i}", thread_id=f"th{i}", audience="agent", ts=START + timedelta(hours=i),
                text=HUMAN[i % len(HUMAN)] + f" ({i})", kind="prompt", register="agent") for i in range(n)]
    store.write_units(con, "claude_code", "loc", old, {"o": 1})
    return con, groups.origin_group_id("claude_code", "", "", "")


def test_groups_of_texts_with_no_origin_are_marked_mixed(home):
    con, gid = _old_store(home)
    g = next(x for x in groups.list_groups(con) if x["group_id"] == gid)
    assert g["mixed"] is True and groups.group_detail(con, gid)["mixed"] is True
    assert next(x for x in groups.review_state(con)["groups"] if x["group_id"] == gid)["mixed"] is True
    assert all(x["mixed"] is False for x in groups.list_groups(con) if x["group_id"] != gid)


def test_a_normal_group_and_a_source_without_origin_support_are_not_mixed(con):
    put(con, [mk(1, "an ordinary prompt about the loader and the tests that fail")])
    assert all(g["mixed"] is False for g in groups.list_groups(con))
    put(con, [mk(2, "a post from a source that never has an origin", source="x_archive", account=None, project=None, kind=None)],
        loc="other")
    assert all(g["mixed"] is False for g in groups.list_groups(con))


def test_not_me_refuses_a_mixed_group_unless_forced(home):
    con, gid = _old_store(home)
    con.close()
    res = groups.not_me(gid)
    assert res["ok"] is False and "review rescan" in res["error"]
    assert store.connect().execute("SELECT count(*) FROM units").fetchone()[0] == 6
    assert store.connect().execute("SELECT count(*) FROM exclusions").fetchone()[0] == 0
    assert api.exclude_add(group=gid)["ok"] is False
    assert api.forget_where(group=gid)["ok"] is False
    dry = api.exclude_add(group=gid, dry_run=True)
    assert dry["ok"] and dry["mixed"] and dry["would_remove"] == 6
    forced = groups.not_me(gid, force=True)
    assert forced["ok"] and forced["removed"] == 6


def test_the_cli_refuses_a_mixed_group_and_prints_no_exclude_hint_for_it(home, capsys):
    con, gid = _old_store(home)
    con.execute("UPDATE units SET held=1")  # shown by `review`
    con.close()
    assert cli.main(["exclude", "add", "--group", gid, "--yes"]) == 2
    assert "review rescan" in capsys.readouterr().err
    assert cli.main(["review"]) == 0
    out = capsys.readouterr().out
    assert gid in out and "exclude add --group" not in out and "review rescan" in out
    assert cli.main(["exclude", "add", "--group", gid, "--yes", "--force"]) == 0
    assert store.connect().execute("SELECT count(*) FROM units").fetchone()[0] == 0


def test_review_rescan_fills_in_origins_from_the_sources_without_duplicating_units(_isolated_env):
    p = _transcript_world(_isolated_env)
    assert sync.sync()["new_units"] == 4
    con = store.connect()
    con.execute("UPDATE units SET account=NULL, project=NULL, project_path=NULL, session_kind=NULL, human_score=NULL, "
                "held=0, score_json=NULL, template=NULL")  # what the pre-v4 store looked like
    gid = groups.origin_group_id("claude_code", "", "", "")
    assert next(g for g in groups.list_groups(con) if g["group_id"] == gid)["mixed"]
    con.close()
    res = api.review_rescan()
    assert res["ok"] and res["origins"]["filled"] == 4 and res["origins"]["sources"] == {"claude_code": 4}
    con = store.connect()
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 4
    got = {tuple(r) for r in con.execute("SELECT account, project, session_kind FROM units")}
    assert got == {("~/.claude", "app", "interactive")}
    assert not any(g["mixed"] for g in groups.list_groups(con))
    assert p.exists()


def test_review_rescan_marks_what_it_could_not_fill_so_the_group_stays_mixed(home):
    con, gid = _old_store(home)
    con.close()
    res = api.review_rescan()
    assert res["ok"] and res["origins"]["filled"] == 0 and res["scored"] == 6
    con = store.connect()
    assert next(g for g in groups.list_groups(con) if g["group_id"] == gid)["mixed"] is True


# --- P3-1 / P3-2: the hook-root registry ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("junk", ['{"roots": {"claude_code": {"/x": NaN}}}', '{"roots": {"claude_code": {"/x": 1e400}}}',
                                  '{"roots": {"claude_code": {"/x": 1, "/y": NaN, "/z": "t"}}}'])
def test_a_bad_registry_entry_is_dropped_and_never_raises(home, tmp_path, junk):
    p = hookroots.registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(junk)
    root = tmp_path / "eph"
    (root / "projects" / "-w" ).mkdir(parents=True)
    assert hookroots.record("claude_code", str(root / "projects" / "-w" / "s.jsonl")) == root
    assert hookroots.registered("claude_code") == [root]
    assert all(isinstance(v, int) for v in json.loads(p.read_text())["roots"]["claude_code"].values())


def test_a_registered_root_must_be_owned_and_not_writable_by_others(home, tmp_path):
    ok = tmp_path / "ok"
    shared = tmp_path / "shared"
    for d in (ok, shared):
        (d / "projects" / "-w").mkdir(parents=True)
    os.chmod(shared, 0o777)
    hookroots.record("claude_code", str(ok / "projects" / "-w" / "s.jsonl"))
    hookroots.record("claude_code", str(shared / "projects" / "-w" / "s.jsonl"))
    assert hookroots.registered("claude_code") == [ok]
    os.chmod(shared, 0o770)  # group-writable is refused too
    assert hookroots.registered("claude_code") == [ok]
    os.chmod(shared, 0o750)
    assert set(hookroots.registered("claude_code")) == {ok, shared}


def test_a_root_owned_by_someone_else_is_not_used(home, tmp_path, monkeypatch):
    root = tmp_path / "theirs"
    (root / "projects" / "-w").mkdir(parents=True)
    hookroots.record("claude_code", str(root / "projects" / "-w" / "s.jsonl"))
    assert hookroots.registered("claude_code") == [root]
    monkeypatch.setattr(os, "getuid", lambda: os.stat(root).st_uid + 1)
    assert hookroots.registered("claude_code") == []


# --- P3-3: scan -------------------------------------------------------------------------------------------------------------------

def _files(h: Path) -> dict:
    return {p.name: hashlib.sha1(p.read_bytes()).hexdigest() for p in sorted(h.iterdir()) if p.is_file()}


CRASH = """
import os
from mannerism import store, config
config.save_config(config.Config())
con = store.connect()
con.execute("PRAGMA wal_autocheckpoint=0")
con.execute("INSERT INTO exclusions(project, created_at) VALUES ('scratch-*', '2026-01-01')")
os._exit(0)  # a crash: the -wal stays behind
"""


def test_scan_creates_no_file_when_the_wal_has_no_index_file(_isolated_env):
    import subprocess
    subprocess.run([sys.executable, "-c", CRASH], check=True, env=dict(os.environ), timeout=60)
    h = config.ensure_home()
    (h / "corpus.db-shm").unlink(missing_ok=True)
    assert (h / "corpus.db-wal").stat().st_size > 0 and not (h / "corpus.db-shm").exists()
    before = _files(h)
    res = scan.scan(samples=1, budget_s=10)
    assert res["ok"] and _files(h) == before and not (h / "corpus.db-shm").exists()
    assert "write-ahead log without its index file" in res["note"]
    assert "write-ahead log" in scan.format_text(res)


def test_scan_with_a_live_store_creates_no_new_file(_isolated_env):
    config.save_config(config.Config())
    h = config.ensure_home()
    con = store.connect()
    con.execute("INSERT INTO exclusions(source, created_at) VALUES ('nonexistent', '2026-01-01')")  # frames in the WAL
    names = {p.name for p in h.iterdir()}
    scan.scan(samples=1, budget_s=10)
    assert {p.name for p in h.iterdir()} == names
    con.close()


def test_scan_never_calls_the_paid_x_api_even_with_network(_isolated_env, monkeypatch):
    calls = []

    class Paid:
        tier, network = "auto", True

        def discover(self, me, c):
            calls.append("discover")
            return []

        def read(self, loc, cur, me):
            calls.append("read")
            return [], {}

    real = adapters.load
    monkeypatch.setattr(adapters, "load", lambda n: Paid() if n == "x" else real(n))
    cfg = config.Config()
    cfg.sources["x"] = {"enabled": True, "allow_network": True, "username": "jo.dev"}
    config.save_config(cfg)
    for kwargs in ({"network": True}, {"network": True, "sources": ["x"]}):
        res = scan.scan(**kwargs)
        assert res["sources"]["x"]["status"] == "skipped_paid" and calls == []
        assert "X was skipped" in scan.format_text(res)


def test_scan_skips_sources_the_config_switches_off_unless_they_are_named(_isolated_env):
    h = _isolated_env
    write_claude(h / ".claude", "-w-app", "aaaaaaaa-0000-0000-0000-000000000001", HUMAN[:2], "/w/app", start=START)
    cfg = config.Config()
    cfg.sources["claude_code"] = {"enabled": False, "paths": []}  # e.g. after `forget --source claude_code`
    config.save_config(cfg)
    res = scan.scan(samples=0)
    assert "claude_code" not in res["sources"]
    res = scan.scan(samples=0, sources=["claude_code"])
    assert res["sources"]["claude_code"]["texts"] == 2
    config.save_config(config.Config())  # never configured: zero-setup sources are still scanned before `init`
    assert scan.scan(samples=0)["sources"]["claude_code"]["texts"] == 2


def test_scan_cleans_plugin_labels_so_group_ids_match_sync(_isolated_env, monkeypatch):
    secret = "AKIAIOSFODNN7EXAMPLE1234567890abcd"

    class Plug:
        tier, network = "auto", False
        name = "plug"

        def discover(self, me, c):
            return [Location("plug", "plug-loc", None, "plug")]

        def read(self, loc, cur, me):
            u = Unit(source="plug", source_ref="p1", ts=START, text="a plugin text about the export fix and its tests",
                     kind="prompt", register="agent", audience="agent", thread_id="t",
                     account=f"~/acct-{secret}", project=f"proj  {secret}", session_kind="inter  active")
            return [u], {}

    monkeypatch.setattr(adapters, "load_for", lambda name, entry=None, **k: Plug())
    cfg = config.Config()
    cfg.sources["plug"] = {"enabled": True, "plugin": "x"}
    config.save_config(cfg)
    res = scan.scan(samples=0, sources=["plug"])
    (g,) = res["groups"]
    assert secret not in json.dumps(res) and g["session_kind"] == "inter active"
    sync.sync(sources=["plug"], force=True)
    con = store.connect()
    r = con.execute("SELECT source, account, project, session_kind FROM units").fetchone()
    assert g["group_id"] == groups.origin_group_id(*tuple(r))


# --- P3-4 / P3-5 / P3-11: CLI -----------------------------------------------------------------------------------------------------

def test_exclude_rm_without_an_id_prints_usage_not_a_traceback(home, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["exclude", "rm"])
    assert e.value.code == 2
    assert "exclude rm needs the rule id" in capsys.readouterr().err


def test_masked_labels_are_copy_safe_as_globs(home):
    label = "~/.claude-[secret]"
    f = exclude.Facts("claude_code", "hi", label, "app", None, "interactive")
    assert exclude.make_rule(account=label).matches(f)  # pasted as shown
    assert exclude.make_rule(account=exclude.glob_escape(label)).matches(f)  # and as the scan prints it
    assert exclude.glob_escape(label) == "~/.claude-[[]secret]"
    assert not exclude.make_rule(account=exclude.glob_escape("~/.claude-[secret]")).matches(
        exclude.Facts("claude_code", "hi", "~/.claude-s", "app", None, "interactive"))


def test_scan_suggestions_escape_a_masked_account_label():
    g = [{"group_id": f"g_{i:012x}", "source": "claude_code", "account": "~/.claude-[secret]", "project": f"p{i}",
          "session_kind": None, "texts": 0, "filtered": 6, "held": 0, "sessions": 6} for i in range(2)]
    (s,) = scan._suggestions(g, [])
    assert s["command"] == "mannerism exclude add --source claude_code --account '~/.claude-[[]secret]'"


def test_forget_all_message_says_what_is_kept_and_clears_the_hook_registry(_isolated_env, tmp_path, capsys):
    _enable()
    root = tmp_path / "eph"
    p = write_claude(root, "-w-demo", "aaaaaaaa-0000-0000-0000-000000000001", HUMAN[:1], "/w/demo", start=START)
    hookroots.record("claude_code", str(p))
    assert hookroots.registry_path().exists()
    api.exclude_add(project="scratch-*")
    res = api.forget(all=True)
    assert res["ok"] and not hookroots.registry_path().exists()
    assert "exclusion rules" in res["note"] and "config.toml" in res["note"] and "hooks had learned" in res["note"]
    con = store.connect()
    assert con.execute("SELECT count(*) FROM exclusions").fetchone()[0] == 1  # kept, and the note says so
    assert cli.main(["forget", "--all", "--yes"]) == 0
    assert "Kept on purpose" in capsys.readouterr().out


# --- P3-6: migration is race-safe -------------------------------------------------------------------------------------------------

def test_two_processes_opening_an_old_store_at_once_both_succeed(home):
    home.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(home / "corpus.db")
    raw.executescript(OLD_UNITS)
    raw.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    raw.commit()
    raw.close()
    errors, barrier = [], threading.Barrier(4)

    def open_store():
        try:
            barrier.wait(10)
            store.connect().close()
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    threads = [threading.Thread(target=open_store) for _ in range(4)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == []
    con = store.connect()
    cols = [r[1] for r in con.execute("PRAGMA table_info(units)")]
    assert len(cols) == len(set(cols)) and "score_json" in cols
    assert store.get_meta(con, "schema_version") == str(store.SCHEMA_VERSION)


def test_migration_runs_in_one_transaction_and_rechecks_the_version(home, monkeypatch):
    home.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(home / "corpus.db")
    raw.executescript(OLD_UNITS)
    raw.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    raw.commit()
    raw.close()
    seen = []
    real = store._migrate_v4
    monkeypatch.setitem(store.MIGRATIONS, 4, lambda c: (seen.append(c.in_transaction), real(c)))
    store.connect().close()
    assert seen == [True]
    store.connect().close()
    assert seen == [True]  # an up-to-date store runs no migration


# --- P3-8: the "fast" signal never false-flags ----------------------------------------------------------------------------------------

LONG = ("I would like you to go through the whole loader module and explain how the retry works, then list every place "
        "where a timeout can be swallowed, and finally propose a small patch for the worst one without touching the tests "
        "or the public names; keep the answer short and use plain words please, thank you very much for the help here.")


def _claude_lines(p: Path, lines: list[dict]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", "utf-8")


def _user(text, ts, uuid):
    return {"type": "user", "message": {"role": "user", "content": text}, "timestamp": ts, "sessionId": "s1", "uuid": uuid,
            "cwd": "/w/app", "entrypoint": "cli"}


def _assistant(ts):
    return {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            "timestamp": ts, "sessionId": "s1", "uuid": "a" + ts}


def _read_all(p: Path, steps: int):
    """Read the file in `steps` growing prefixes through the cursor, like successive syncs."""
    src, me = ClaudeCode(), Me()
    loc = Location("claude_code", str(p), p, "x")
    text = p.read_text("utf-8").splitlines(keepends=True)
    bounds = [round(len(text) * (i + 1) / steps) for i in range(steps)]
    cursor, units, final = None, [], p.read_text("utf-8")
    try:
        for b in bounds:
            p.write_text("".join(text[:b]), "utf-8")
            got, cursor = src.read(loc, cursor, me)
            units += list(got)
    finally:
        p.write_text(final, "utf-8")
    return units


def test_the_gap_is_the_same_whether_the_file_is_read_at_once_or_in_pieces(tmp_path):
    p = tmp_path / ".claude" / "projects" / "-w-app" / "s1.jsonl"
    _claude_lines(p, [_assistant("2026-09-01T10:00:00Z"), _user("short one for the test run", "2026-09-01T10:00:20Z", "u1"),
                      _assistant("2026-09-01T10:00:30Z"), {"type": "queue-operation", "timestamp": "2026-09-01T10:05:00Z"},
                      _user(LONG, "2026-09-01T10:05:00Z", "u2"), _user(LONG + " again", "2026-09-01T10:05:01Z", "u3")])
    whole = {u.source_ref: u.gap_s for u in _read_all(p, 1)}
    pieces = {u.source_ref: u.gap_s for u in _read_all(p, 6)}
    assert whole["u2"] == 270.0 and whole["u3"] == 1.0  # the queue marker is not the previous turn
    assert pieces == whole


def test_a_read_that_starts_mid_session_without_a_known_turn_end_never_flags_a_prompt_as_fast(tmp_path):
    p = tmp_path / ".claude" / "projects" / "-w-app" / "s1.jsonl"
    _claude_lines(p, [_user(LONG, "2026-09-01T10:05:00Z", "u2")])
    (u,) = list(ClaudeCode().read(Location("claude_code", str(p), p, "x"), None, Me())[0])
    assert u.gap_s is None and "fast" not in humanness.analyse(u.text, u.gap_s).codes
    # and an old cursor without the new key resumes with the gap unknown, not zero
    old_cursor = {"offset": 0, "last_agent": None}
    assert list(ClaudeCode().read(Location("claude_code", str(p), p, "x"), old_cursor, Me())[0])[0].gap_s is None


def test_codex_prompt_next_to_its_duplicate_event_line_is_not_fast(tmp_path):
    from mannerism.adapters.codex import Codex
    p = tmp_path / ".codex" / "sessions" / "2026" / "09" / "01" / "rollout-2026-09-01T10-00-00-abc.jsonl"
    p.parent.mkdir(parents=True)
    lines = [{"timestamp": "2026-09-01T10:00:00Z", "type": "session_meta", "payload": {"id": "abc", "cwd": "/w/app", "source": "cli"}},
             {"timestamp": "2026-09-01T10:06:00Z", "type": "response_item",
              "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "ok"}]}},
             {"timestamp": "2026-09-01T10:06:40Z", "type": "response_item",
              "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": LONG}]}},
             {"timestamp": "2026-09-01T10:06:40Z", "type": "event_msg", "payload": {"type": "user_message", "message": LONG}}]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", "utf-8")
    (u,) = list(Codex().read(Location("codex", str(p), p, "x"), None, Me())[0])
    assert u.gap_s == 40.0 and "fast" not in humanness.analyse(u.text, u.gap_s).codes


# --- docs -----------------------------------------------------------------------------------------------------------------------------

DOCS = Path(__file__).resolve().parent.parent


def test_readme_and_design_say_exactly_which_codex_hooks_are_installed():
    readme = (DOCS / "README.md").read_text("utf-8")
    design = (DOCS / "docs" / "design.md").read_text("utf-8")
    for text in (readme, design):
        assert "no Codex Stop or SessionEnd hook is installed" in " ".join(text.split())


def test_readme_does_not_promise_that_it_never_leaves_the_machine():
    readme = (DOCS / "README.md").read_text("utf-8")
    assert "never leaves your machine" not in readme and "stays on your machine" not in readme and "runs local" not in readme
    assert "stored on your machine" in readme
