"""Machine-typed detection: clearly human text is never held or dropped; clearly machine text is, in both directions."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from mannerism import groups, humanness, refresh, store
from mannerism.core import Unit

HUMAN_SHORT = ["lgtm", "fix the test", "ok go ahead", "yes", "continue", "commit and push", "why is this failing?",
               "no, the other one", "thanks", "revert that", "try again but keep the old name"]
HUMAN_LONG = [
    "can you look at why the login test is flaky on CI? it fails about one in five runs and only on the slow runner, "
    "so I suspect a timeout rather than a real bug",
    "I think we should split this module in two. the parsing part is fine, but the caching bit keeps growing and "
    "nobody wants to touch it anymore. start with the interface, don't move any code yet",
    "hm, that is not what I meant. I want the retry to wrap the whole call, not just the send. also please keep "
    "the logging as it is, I like seeing the attempt number",
]
RELAY = ('<agent-message from="worker-3" id="12">\nTask 41 finished.\nStatus: done\nNext: waiting for review\n'
         '</agent-message>')
KEY_VALUE = "Status update\ntask: build-42\nstate: passed\nduration: 31s\nowner: ci-bot\nnext: deploy"
JSON_BLOB = json.dumps({"event": "task_done", "id": 42, "result": {"ok": True, "items": [1, 2, 3]}, "by": "worker"})
FRONT = "---\ntitle: nightly report\nowner: bot\nschedule: daily\n---\nSummary of the run follows below in detail."
STATUS = "- build: done\n- tests: passed\n- deploy: pending\n- docs: blocked"
MARKER = "[SYSTEM NOTICE] The session budget is nearly used up; wrap up the current task and report back."
BOILERPLATE = ("Continue working on ticket ABC-{n} following the plan in the ledger and report status when finished, "
               "keeping the commit small")


def test_thresholds_are_documented_and_ordered():
    assert 0 < humanness.LOW < humanness.REVIEW < 1
    assert humanness.LOW == 0.20 and humanness.REVIEW == 0.60
    assert humanness.is_dropped(0.19) and not humanness.is_dropped(0.20)
    assert humanness.is_held(0.20) and humanness.is_held(0.59) and not humanness.is_held(0.60)


@pytest.mark.parametrize("text", HUMAN_SHORT)
def test_clearly_human_short_prompts_are_never_judged(text):
    value, info = humanness.score_text(text, gap_s=0.1)
    assert value == 1.0 and info["reasons"] == []


@pytest.mark.parametrize("text", HUMAN_SHORT)
def test_a_short_prompt_repeated_in_every_session_is_still_human(text):
    value, _ = humanness.score_text(text, template_sessions=200, session_total=50, session_structured=0)
    assert value == 1.0


@pytest.mark.parametrize("text", HUMAN_LONG)
def test_clearly_human_prose_scores_normal(text):
    value, info = humanness.score_text(text, gap_s=45.0)
    assert value >= humanness.REVIEW and info["reasons"] == []


@pytest.mark.parametrize("text", [RELAY, JSON_BLOB, FRONT, MARKER, STATUS])
def test_clearly_machine_text_is_held(text):
    value, info = humanness.score_text(text)
    assert humanness.is_held(value) and info["reasons"], text  # one strong signal holds, never drops


@pytest.mark.parametrize("text", [KEY_VALUE])
def test_format_signals_alone_lose_points_but_stay_normal_until_the_session_agrees(text):
    value, info = humanness.score_text(text)
    assert humanness.REVIEW <= value < 1.0 and info["reasons"]
    inside, _ = humanness.score_text(text, session_total=10, session_structured=9)
    assert humanness.is_held(inside)


def test_each_structural_signal_is_named():
    codes = {name: humanness.analyse(text).codes for name, text in
             (("markup", RELAY), ("key-value", KEY_VALUE), ("json", JSON_BLOB), ("front-matter", FRONT),
              ("status", STATUS), ("marker", MARKER))}
    for name, got in codes.items():
        assert name in got, (name, got)


def test_a_human_pasting_html_is_not_dropped():
    value, _ = humanness.score_text("<p>why does this render badly in the narrow layout of my phone screen</p>")
    assert value >= humanness.REVIEW - 0.01  # weak markup alone never reaches the hold threshold by much
    assert not humanness.is_dropped(value)


def test_repetition_across_sessions_grows_with_the_count():
    text = BOILERPLATE.format(n=1)
    sg = humanness.analyse(text)
    assert sg.template
    values = [humanness.score(sg, n)[0] for n in (1, 2, 3, 6, 12)]
    assert values[0] == values[1] == 1.0
    assert values[2] > values[3] > values[4]
    assert values[3] >= humanness.REVIEW and humanness.is_held(values[4])


def test_the_same_prompt_with_other_numbers_is_one_template():
    a = humanness.template_of(BOILERPLATE.format(n=1))[0]
    b = humanness.template_of(BOILERPLATE.format(n=977))[0]
    c = humanness.template_of("Please rename the config loader so the names match the docs we wrote last week, thanks")[0]
    assert a and a == b and a != c


def test_fast_arrival_needs_a_long_text_and_a_known_gap():
    long_text = "word " * 100  # 500 chars
    assert "fast" in humanness.analyse(long_text, gap_s=1.0).codes
    assert "fast" not in humanness.analyse(long_text, gap_s=300.0).codes
    assert "fast" not in humanness.analyse(long_text, gap_s=None).codes
    assert "fast" not in humanness.analyse("quick note", gap_s=0.0).codes
    value, _ = humanness.score_text(long_text, gap_s=1.0)
    assert value >= humanness.REVIEW  # speed alone never holds


def test_a_session_where_nearly_every_prompt_is_structured_costs_points():
    sg = humanness.analyse(KEY_VALUE)
    alone, _ = humanness.score(sg)
    inside, info = humanness.score(sg, session_total=10, session_structured=9)
    assert inside < alone and any(r.startswith("session:") for r in info["reasons"])
    few, _ = humanness.score(sg, session_total=4, session_structured=4)
    assert few == alone  # too few prompts to say


def test_several_signals_agreeing_drop_the_unit():
    sg = humanness.analyse(RELAY)
    value, _ = humanness.score(sg, template_sessions=14, session_total=8, session_structured=8)
    assert humanness.is_dropped(value)


def test_one_strong_signal_alone_holds_but_does_not_drop():
    for text in (RELAY, JSON_BLOB, MARKER, FRONT):
        value, _ = humanness.score_text(text)
        assert humanness.is_held(value), text


# --- Screener (ingest) and rescoring -----------------------------------------------------------------------------------

def mk(text, i, session, source="s1", **kw):
    return Unit(source, f"r{i}", datetime(2026, 5, 1, tzinfo=UTC) + timedelta(minutes=i), text, "prompt",
                register="agent", audience="agent", thread_id=session, session=session, **kw)


def screen(con, units, kept=frozenset()):
    sc = humanness.Screener(con, kept)
    return sc.apply(units, lambda u: groups.group_ids(u.source, u.account, u.project, u.session_kind, u.template))


def test_screener_counts_sessions_in_the_batch(home):
    con = store.connect()
    units = [mk(BOILERPLATE.format(n=i), i, f"sess{i}") for i in range(14)]
    screen(con, units)
    assert all(u.template == units[0].template for u in units)
    assert all(u.human_score == units[0].human_score for u in units)
    assert units[0].held and "template:14 sessions" in units[0].score_info["reasons"]


def test_template_aggregation_scans_each_batch_counter_once(monkeypatch):
    from collections import Counter

    class CountingCounter(Counter):
        visits = 0

        def __iter__(self):
            type(self).visits += len(self)
            return super().__iter__()

    def alpha(n: int) -> str:
        out = ""
        while True:
            out = chr(ord("a") + n % 26) + out
            n = n // 26 - 1
            if n < 0:
                return out

    monkeypatch.setattr(humanness, "Counter", CountingCounter)
    units = [mk(f"unique{alpha(i)} alpha beta gamma delta epsilon zeta eta theta", i, f"session{i}")
             for i in range(400)]
    screen(None, units)
    assert CountingCounter.visits <= len(units) * 4


def test_screener_uses_what_the_store_already_holds(home):
    con = store.connect()
    first = [mk(BOILERPLATE.format(n=i), i, f"sess{i}") for i in range(10)]
    screen(con, first)
    store.write_units(con, "s1", "loc", first, {})
    later = [mk(BOILERPLATE.format(n=50), 50, "sess50")]
    screen(con, later)
    assert "template:11 sessions" in later[0].score_info["reasons"]


def test_screener_never_holds_confirmed_groups(home):
    con = store.connect()
    units = [mk(BOILERPLATE.format(n=i), i, f"sess{i}") for i in range(14)]
    screen(con, units, kept={groups.template_group_id(humanness.template_of(units[0].text)[0])})
    assert units[0].human_score < humanness.REVIEW and not any(u.held for u in units)


def test_rescoring_holds_earlier_units_when_the_count_grows_and_refreshes_the_bank(home):
    con = store.connect()
    early = [mk(BOILERPLATE.format(n=i), i, f"sess{i}") for i in range(2)]
    screen(con, early)
    store.write_units(con, "s1", "loc", early, {})
    refresh.refresh(con, None)
    assert not any(u.held for u in early) and con.execute("SELECT count(*) FROM bank").fetchone()[0] == 2
    late = [mk(BOILERPLATE.format(n=i), i, f"sess{i}") for i in range(2, 14)]
    screen(con, late)
    store.write_units(con, "s1", "loc", late, {})
    changed = humanness.rescore_after_write(con, {late[0].template}, {u.session for u in late}, set(),
                                            groups.group_ids_of_row)
    assert changed == {"agent"}
    assert con.execute("SELECT count(*) FROM units WHERE held=1").fetchone()[0] == 14
    refresh.refresh(con, ["agent"])
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0  # held units are never examples


def test_held_units_stay_out_of_the_bank_and_the_label_queue(home):
    con = store.connect()
    human = [mk(t, 100 + i, f"h{i}") for i, t in enumerate(HUMAN_LONG)]
    machine = [mk(RELAY.replace("41", str(i)), i, f"m{i}") for i in range(3)]
    for u in (*human, *machine):
        u.register = "agent"
    screen(con, [*human, *machine])
    assert all(u.held for u in machine) and not any(u.held for u in human)
    store.write_units(con, "s1", "loc", [*human, *machine], {})
    refresh.refresh(con, ["agent"])
    texts = {r[0] for r in con.execute("SELECT text FROM bank")}
    assert texts == {u.text for u in human}
    from mannerism import registers
    unk = mk(RELAY.replace("41", "999"), 999, "mx")
    unk.register = "?"
    screen(con, [unk])
    assert unk.held
    store.write_units(con, "s1", "loc", [unk], {})
    assert registers.unlabelled_count(con) == 0 and registers.label_queue(con) == []


def test_rescan_scores_units_stored_before_the_feature_existed(home):
    con = store.connect()
    units = [mk(RELAY.replace("41", str(i)), i, f"m{i}") for i in range(3)] + [mk(HUMAN_LONG[0], 50, "h")]
    store.write_units(con, "s1", "loc", units, {})  # no score: as if read by an older version
    assert con.execute("SELECT count(*) FROM units WHERE human_score IS NULL").fetchone()[0] == 4
    res = humanness.rescan_stored(con, set(), groups.group_ids_of_row)
    assert res["scored"] == 4 and res["held"] == 3 and res["registers"] == {"agent"}
    assert con.execute("SELECT count(*) FROM units WHERE human_score IS NULL").fetchone()[0] == 0
