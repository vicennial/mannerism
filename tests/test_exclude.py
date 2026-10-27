"""Exclusion rules: every rule type, ingest filtering, retroactive deletion with everything derived, config + store."""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest

from mannerism import adapters, api, capture, config, exclude, groups, rules, store, sync
from mannerism.core import Location, Unit

NOW = datetime(2026, 6, 1, 12, tzinfo=UTC)
HOME = "~"


def mku(i, text=None, *, source="agentx", account="~/.acct-a", project="app", kind="interactive",
        project_path=None, session=None, register="work", ts=None):
    text = text or f"please look at the failing build number {i} and tell me what changed in the config loader"
    return Unit(source, f"u{i}", ts or NOW - timedelta(days=i), text, "prompt", register=register, audience="agent",
                thread_id=session or f"s{i}", session=session or f"s{i}", account=account, project=project,
                project_path=project_path or (f"~/work/{project}" if project else None), session_kind=kind)


class Feed:
    """An in-memory adapter that yields a fixed list of units (the same for every read)."""
    tier = "auto"

    def __init__(self, name, units):
        self.name, self.units = name, units

    def discover(self, me, cfg):
        return [Location(self.name, f"{self.name}-loc", None, self.name)]

    def read(self, loc, cursor, me):
        return list(self.units), {"i": len(self.units)}


@pytest.fixture
def feeds(home, monkeypatch):
    made = {}

    def make(name, units):
        made[name] = Feed(name, units)
        monkeypatch.setattr(adapters, "REGISTRY", {n: f"fake:{n}" for n in made})
        monkeypatch.setattr(adapters, "load", lambda n: made[n])
        cfg = config.load_config()
        cfg.sources[name] = {"enabled": True, "paths": []}
        config.save_config(cfg)
        return made[name]

    return make


def stored(con=None):
    con = con or store.connect()
    return {r[0] for r in con.execute("SELECT source_ref FROM units")}


UNITS = [
    mku(1, account="~/.acct-a", project="app", kind="interactive"),
    mku(2, account="~/.acct-a", project="scratch-1", kind="interactive"),
    mku(3, account="~/.acct-b", project="app", kind="interactive"),
    mku(4, account="~/.acct-b", project="svc", kind="ide"),
    mku(5, text="[nightly] summary of the overnight run for the dashboard people", account="~/.acct-a", project="app"),
    mku(6, source="othersrc", account=None, project=None, kind=None, project_path=None),
]


@pytest.mark.parametrize("rule, gone", [
    ({"source": "othersrc"}, {"u6"}),
    ({"account": "~/.acct-b"}, {"u3", "u4"}),
    ({"account": "~/.acct-*"}, {"u1", "u2", "u3", "u4", "u5"}),
    ({"project": "scratch-*"}, {"u2"}),
    ({"project": "~/work/svc"}, {"u4"}),
    ({"kind": "ide"}, {"u4"}),
    ({"text": r"^\[nightly\]"}, {"u5"}),
    ({"source": "agentx", "account": "~/.acct-b", "kind": "interactive"}, {"u3"}),
    ({"project": "app", "text": "build number [13] "}, {"u1", "u3"}),
])
def test_every_rule_type_filters_at_ingest(feeds, rule, gone):
    for source in ("agentx", "othersrc"):
        feeds(source, [u for u in UNITS if u.source == source])
    cfg = config.load_config()
    cfg.exclude = [rule]
    config.save_config(cfg)
    res = sync.sync()
    everything = {u.source_ref for u in UNITS}
    assert stored() == everything - gone
    assert res["excluded"] == len(gone)


def test_the_account_glob_matches_the_expanded_path_too(feeds, _isolated_env):
    feeds("agentx", [mku(1, account="~/.acct-a")])
    cfg = config.load_config()
    cfg.exclude = [{"account": f"{_isolated_env}/.acct-a"}]
    config.save_config(cfg)
    sync.sync()
    assert stored() == set()


def test_a_unit_without_the_metadata_a_rule_asks_for_does_not_match(feeds):
    feeds("othersrc", [mku(6, source="othersrc", account=None, project=None, kind=None, project_path=None)])
    cfg = config.load_config()
    cfg.exclude = [{"account": "*"}, {"project": "*"}, {"kind": "*"}]
    config.save_config(cfg)
    assert sync.sync()["excluded"] == 0 and stored() == {"u6"}


def test_the_group_rule_uses_the_ids_scan_and_review_show(feeds):
    feeds("agentx", UNITS[:5])
    sync.sync()
    con = store.connect()
    gid = next(g["group_id"] for g in groups.list_groups(con) if g["project"] == "scratch-1")
    assert gid == groups.origin_group_id("agentx", "~/.acct-a", "scratch-1", "interactive")
    con.close()
    res = api.exclude_add(group=gid)
    assert res["ok"] and res["removed"] == 1
    assert "u2" not in stored()
    sync.sync()  # the same text is read again by the next sync: still not stored
    assert "u2" not in stored()


def test_config_and_store_rules_both_apply(feeds):
    feeds("agentx", UNITS[:5])
    cfg = config.load_config()
    cfg.exclude = [{"project": "svc"}]
    config.save_config(cfg)
    assert api.exclude_add(kind="ide")["ok"]
    sync.sync()
    assert stored() == {"u1", "u2", "u3", "u5"}  # svc is also ide, so u4 goes either way
    api.exclude_add(project="scratch-*")
    sync.sync()
    assert "u2" not in stored()
    listed = api.exclude_list()["rules"]
    assert {(r["origin"], r["rule"]) for r in listed} == {("config", "project='svc'"), ("store", "kind='ide'"),
                                                           ("store", "project='scratch-*'")}


def test_config_exclude_tables_round_trip_through_save(home):
    cfg = config.load_config()
    cfg.exclude = [{"project": "scratch-*", "kind": "ide"}, {"text": r"^\[nightly\]"}]
    cfg.sources["claude_code"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    back = config.load_config()
    assert back.exclude == cfg.exclude and back.sources["claude_code"]["enabled"] is True
    text = (home / "config.toml").read_text()
    assert text.count("[[exclude]]") == 2


def test_invalid_config_rules_are_reported_not_applied(feeds):
    feeds("agentx", UNITS[:2])
    cfg = config.load_config()
    cfg.exclude = [{}, {"text": "(unclosed"}, {"bogus": "x"}, {"group": "nope"}, {"project": "scratch-*"}]
    config.save_config(cfg)
    res = sync.sync()
    assert len(res["exclude_errors"]) == 4 and stored() == {"u1"}


@pytest.mark.parametrize("fields, message", [
    ({}, "at least one"),
    ({"project": "   "}, "at least one"),
    ({"text": "(unclosed"}, "not a valid regular expression"),
    ({"text": "(a+)+$"}, "nested quantifiers"),
    ({"text": "x" * 600}, "longer than"),
    ({"group": "nope"}, "not a group id"),
    ({"kind": "a", "session_kind": "b"}, "not both"),
])
def test_bad_rules_are_refused(fields, message):
    with pytest.raises(exclude.RuleError, match=message):
        exclude.make_rule(**fields)


def test_a_slow_pattern_is_refused_by_a_static_check_and_a_runaway_match_is_cut_off():
    for bad in ("(a|aa)+$", "(a+)+$", r"(\s*\w+)*x"):  # the same answer on every machine: no timing involved
        with pytest.raises(exclude.RuleError, match="run for ever"):
            exclude.make_rule(text=bad)
    exclude.make_rule(text=r"(?:foo|bar)+$")  # branches that start differently are fine
    exclude.make_rule(text=r"(\d{1,3}\.){3}\d+")  # bounded repeats are fine
    before = exclude.STATS["regex_timeouts"]
    pattern = "(a|aa)+$"  # bypasses the static check on purpose (a stored rule from before it existed)
    t0 = time.monotonic()
    assert exclude._search(pattern, "a" * 60 + "b") is True  # cut off by the time limit: counts as a match (fails closed)
    assert exclude._search(pattern, "a" * 60 + "b", timeout_result=False) is False  # the retroactive delete never removes
    assert exclude.STATS["regex_timeouts"] == before + 2
    assert time.monotonic() - t0 < 5


def test_regex_matching_never_falls_back_to_the_caller_process(monkeypatch):
    pattern = "a*a*a*a*a*a*a*Q"
    exclude._POISONED.pop(pattern, None)
    monkeypatch.setattr(exclude._WORKER, "broken", True)
    monkeypatch.setattr(exclude, "_compile", lambda _p: pytest.fail("must not run regex in the caller"))
    assert exclude._search(pattern, "a" * 200) is True
    assert exclude._search(pattern, "a" * 200, timeout_result=False) is False


def test_a_worker_created_in_a_temporary_thread_is_reused_without_a_false_timeout():
    exclude._WORKER.stop()
    exclude._WORKER.broken = False
    pattern = "temporary-thread-needle"
    exclude._POISONED.pop(pattern, None)
    before = exclude.STATS["regex_timeouts"]
    got = []
    thread = threading.Thread(target=lambda: got.append(exclude._search(pattern, "ordinary text")))
    thread.start()
    thread.join(5)
    assert not thread.is_alive() and got == [False]
    assert exclude._search(pattern, "ordinary text") is False
    assert exclude.STATS["regex_timeouts"] == before


def test_text_rules_only_look_at_the_start_of_a_huge_text():
    rule = exclude.make_rule(text="needle")
    assert rule.matches(exclude.Facts("s", "needle " + "x" * 100))
    assert not rule.matches(exclude.Facts("s", "x" * (exclude.TEXT_SCAN_CHARS + 10) + "needle"))


def test_an_updated_unit_that_now_matches_is_deleted_from_the_store(feeds):
    feed = feeds("agentx", [mku(1)])
    sync.sync()
    assert stored() == {"u1"}
    con = store.connect()
    exclude.add_store_rule(con, exclude.make_rule(text=r"^\[nightly\]"))  # a rule, without the retroactive delete
    con.close()
    feed.units = [mku(1, text="[nightly] this text changed and is now the kind of thing a rule drops quietly")]
    res = sync.sync()
    assert stored() == set() and res["deleted_units"] == 1 and res["excluded"] == 1


# --- retroactive deletion removes everything derived ---------------------------------------------------------------------

def build_derived(con):
    """Units from two projects with bank rows, features, a label, a draft + edit pair and a rule built from them."""
    topics = ["the invoice batch export", "the onboarding flow redesign", "the staging deploy freeze",
              "the quarterly review deck", "the vendor contract renewal", "the search latency regression"]
    for i, topic in enumerate(topics):
        capture.log_draft(f"Hi team, I wanted to follow up on {topic} and confirm the plan for this week. Thanks so much!",
                          "work", con=con, now=NOW - timedelta(hours=3))
        u = mku(i, f"following up on {topic}, confirm the plan for this week?", project="gone-proj",
                register="work", ts=NOW - timedelta(hours=1))
        res = store.write_units(con, "agentx", "loc", [u], {})
        capture.capture_new_units(con, res.new_ids)
    keep = [mku(100 + i, f"a different note number {i} to keep about the roadmap for the next quarter, thanks",
                project="keep-proj") for i in range(8)]
    store.write_units(con, "agentx", "loc", keep, {})
    from mannerism import refresh
    refresh.refresh(con, None)
    pk = rules.distill_packet(con, "work")
    assert pk
    con.execute("INSERT INTO packets(packet_id, kind, register, created_at, payload_json, schema_json, expires_at) "
                "VALUES ('pk_x','distill','work','2026-01-01T00:00:00+00:00',?,?, '2999-01-01T00:00:00+00:00')",
                (json.dumps({"instructions": "", "payload": pk["payload"]}), json.dumps(pk["schema"])))
    ids = [p["edit_id"] for p in pk["payload"]["pairs"]]
    rules.submit_rules(con, "pk_x", {"rules": [{"text": "Leave out greetings and sign-offs", "edit_ids": ids}]})


def counts(con):
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("units", "unit_features", "bank", "edits", "rules", "drafts", "packets")}


def test_adding_a_rule_deletes_matching_units_and_everything_derived(home):
    con = store.connect()
    build_derived(con)
    before = counts(con)
    assert before["edits"] and before["rules"] and before["bank"] and before["drafts"]
    con.close()
    res = api.exclude_add(project="gone-proj")
    assert res["ok"] and res["removed"] == 6 and res["by_source"] == {"agentx": 6}
    con = store.connect()
    after = counts(con)
    assert after["units"] == before["units"] - 6
    assert after["edits"] == 0 and after["rules"] == 0 and after["drafts"] == 0 and after["packets"] == 0
    assert con.execute("SELECT count(*) FROM bank WHERE text LIKE '%follow%'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM unit_features").fetchone()[0] == after["units"]
    assert con.execute("SELECT count(*) FROM units WHERE project='keep-proj'").fetchone()[0] == 8
    assert con.execute("SELECT count(*) FROM exclusions").fetchone()[0] == 1


def test_forget_where_deletes_the_same_but_stores_no_rule(home):
    con = store.connect()
    build_derived(con)
    con.close()
    res = api.forget_where(project="gone-proj")
    assert res["ok"] and res["removed"] == 6 and "outside local deletion" in res["note"]
    con = store.connect()
    assert con.execute("SELECT count(*) FROM exclusions").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0


def test_dry_run_counts_and_changes_nothing(home):
    con = store.connect()
    build_derived(con)
    before = counts(con)
    con.close()
    for call in (api.exclude_add, api.forget_where):
        res = call(project="gone-proj", dry_run=True)
        assert res["ok"] and res["would_remove"] == 6 and res["by_source"] == {"agentx": 6}
    con = store.connect()
    assert counts(con) == before and con.execute("SELECT count(*) FROM exclusions").fetchone()[0] == 0


def test_dry_run_on_a_machine_with_no_store_creates_nothing(home):
    res = api.exclude_add(source="x", dry_run=True)
    assert res["ok"] and res["would_remove"] == 0
    assert not (home / "corpus.db").exists()


def test_adding_a_rule_drops_an_agent_card_built_on_the_removed_source(home):
    from conftest import ingest

    from mannerism import packets
    con = store.connect()
    ingest(con, [mku(i, f"message {i} with some varied words {'x' * (i % 9)}", register="close", project="p1")
                 for i in range(25)] +
           [mku(100 + i, f"other message {i} that is a bit different from those", register="close", project="p2",
                source="agent2") for i in range(10)])
    pk = packets.next_packet("card", con=con)
    assert packets.submit("card", pk["packet_id"], {"register": "close", "card": "Writes short plain messages. " * 8},
                          con=con)["saved"]
    con.close()
    res = api.exclude_add(source="agent2")
    assert res["ok"] and res["stale_cards"] == ["close"]


def test_exclude_rm_and_list(home):
    first = api.exclude_add(project="a*")
    assert first["created"] and not api.exclude_add(project="a*")["created"]  # identical rule: stored once
    assert api.exclude_add(project="a*", kind="ide")["rule_id"] != first["rule_id"]
    assert len(api.exclude_list()["rules"]) == 2
    assert api.exclude_rm(first["rule_id"])["ok"]
    assert not api.exclude_rm(first["rule_id"])["ok"]
    assert len(api.exclude_list()["rules"]) == 1


def test_a_changed_config_rule_deletes_what_is_stored_once(feeds):
    feeds("agentx", UNITS[:5])
    sync.sync()
    assert len(stored()) == 5
    cfg = config.load_config()
    cfg.exclude = [{"account": "~/.acct-b"}]
    config.save_config(cfg)
    res = sync.sync()
    assert res["deleted_units"] == 2 and stored() == {"u1", "u2", "u5"}
    con = store.connect()
    assert store.get_meta(con, "exclude_config_sig")
    assert sync.sync()["deleted_units"] == 0  # not again
    cfg.exclude = []
    config.save_config(cfg)
    sync.sync()
    assert store.get_meta(store.connect(), "exclude_config_sig") is None


def test_forget_all_keeps_the_rules(home):
    api.exclude_add(project="keepme")
    api.forget(all=True)
    assert len(api.exclude_list()["rules"]) == 1


def test_a_rule_that_matches_nothing_removes_nothing(home):
    res = api.exclude_add(project="nothing-here")
    assert res["ok"] and res["removed"] == 0
    assert os.path.exists(home / "corpus.db")
