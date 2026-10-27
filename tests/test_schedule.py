"""Network sources and scheduling: consent, quick sync, intervals, backoff, --force, status and --watch."""
from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FakeSource, make_corpus

from mannerism import adapters, api, cli, config, schedule, store, sync


class FakeNet(FakeSource):
    """A network adapter as the contract describes it: class attributes `network`, `network_hosts`, `default_interval_s`."""
    network = True
    network_hosts = ("api.example.org",)
    default_interval_s = 900
    session_start_default = True  # a free adapter: a session start may launch its background sync
    tier = "export"

    def __init__(self, name, units):
        super().__init__(name, units)
        self.fail = False

    def read(self, loc, cursor, me):
        if self.fail:
            raise ConnectionError("service down")
        return super().read(loc, cursor, me)


@pytest.fixture
def net(home, monkeypatch):
    corpus = make_corpus()
    corpus["fakepub"] = [replace(u, source="fakenet") for u in corpus["fakepub"]]
    sources = {"fakechat": FakeSource("fakechat", corpus["fakechat"]),
               "fakenet": FakeNet("fakenet", corpus["fakepub"])}
    monkeypatch.setattr(adapters, "REGISTRY", {n: f"fake:{n}" for n in sources})
    monkeypatch.setattr(adapters, "load", lambda name: sources[name])
    return sources


def setup_cfg(net_consent=True, **net_kw):
    cfg = config.load_config()
    cfg.sources["fakechat"] = {"enabled": True, "paths": []}
    cfg.sources["fakenet"] = {"enabled": True, "paths": [], **({"allow_network": True} if net_consent else {}), **net_kw}
    config.save_config(cfg)


def state(name="fakenet"):
    con = store.connect()
    try:
        return schedule.get(con, name)
    finally:
        con.close()


def set_next_due(delta_s, name="fakenet"):
    con = store.connect()
    st = schedule.get(con, name)
    st["next_due"] = (datetime.now(UTC) + timedelta(seconds=delta_s)).isoformat(timespec="seconds")
    store.set_meta_json(con, schedule.key(name), st)
    con.close()


def test_quick_sync_never_runs_a_network_source_but_may_start_a_background_sync(net, monkeypatch):
    setup_cfg()
    started = []
    monkeypatch.setattr(sync, "sync_in_background", lambda **kw: started.append(kw) or {"started": True, "pid": 1})
    res = sync.sync(quick=True)
    assert net["fakenet"].reads == 0 and res["sources"]["fakenet"]["status"] == "skipped_quick"
    assert res["sources"]["fakechat"]["new_units"] == 195  # local sources still run
    assert started == [{"quick": False, "sources": ["fakenet"]}] and res["background"]["started"]
    # not due (it ran recently) -> no background sync either
    sync.sync(sources=["fakenet"])
    started.clear()
    sync.sync(quick=True)
    assert started == [] and net["fakenet"].reads == 1


def test_quick_sync_does_not_start_anything_without_consent(net, monkeypatch):
    setup_cfg(net_consent=False)
    started = []
    monkeypatch.setattr(sync, "sync_in_background", lambda **kw: started.append(kw) or {"started": True})
    res = sync.sync(quick=True, sources=["fakenet"])
    assert res["sources"]["fakenet"]["status"] == "skipped_quick" and started == [] and net["fakenet"].reads == 0


def test_the_background_sync_is_started_after_the_lock_is_released(net, monkeypatch):
    setup_cfg()
    seen = {}

    def fake_bg(**kw):
        seen["free"] = sync.acquire_lock(0.2)  # the lock must be free for the child
        sync.release_lock(seen["free"])
        return {"started": True}

    monkeypatch.setattr(sync, "sync_in_background", fake_bg)
    sync.sync(quick=True)
    assert seen["free"] is not None


def test_a_network_source_needs_consent_in_a_full_sync(net):
    setup_cfg(net_consent=False)
    res = sync.sync()
    rep = res["sources"]["fakenet"]
    assert rep["status"] == "skipped_no_consent" and "sources enable fakenet" in rep["note"] and net["fakenet"].reads == 0
    assert res["sources"]["fakechat"]["new_units"] == 195
    sync.sync(force=True)
    assert net["fakenet"].reads == 0  # --force is not consent
    cfg = config.load_config()
    cfg.sources["fakenet"]["allow_network"] = "yes"  # only a real true counts
    config.save_config(cfg)
    assert sync.sync()["sources"]["fakenet"]["status"] == "skipped_no_consent"


def test_the_interval_gates_a_network_source_and_force_ignores_it(net):
    setup_cfg()
    first = sync.sync()
    assert first["sources"]["fakenet"]["new_units"] == 14 and net["fakenet"].reads == 1
    st = state()
    assert st["failures"] == 0 and st["last_error"] is None
    due = datetime.fromisoformat(st["next_due"])
    assert 800 < (due - datetime.fromisoformat(st["last_success"])).total_seconds() <= 900  # the class default
    again = sync.sync()
    assert again["sources"]["fakenet"]["status"] == "not_due" and again["sources"]["fakenet"]["next_due"] == st["next_due"]
    assert net["fakenet"].reads == 1 and again["sources"]["fakechat"]["status"] == "ok"
    sync.sync(force=True)
    assert net["fakenet"].reads == 2
    set_next_due(-1)  # time passes
    assert sync.sync()["sources"]["fakenet"]["status"] == "ok" and net["fakenet"].reads == 3


def test_interval_s_in_config_overrides_the_class_default(net):
    setup_cfg(interval_s=60)
    sync.sync()
    st = state()
    assert 0 < (datetime.fromisoformat(st["next_due"]) - datetime.fromisoformat(st["last_success"])).total_seconds() <= 60
    cfg = config.load_config()
    cfg.sources["fakenet"]["interval_s"] = 0  # every sync
    config.save_config(cfg)
    sync.sync(force=True)
    assert state()["next_due"] is None
    sync.sync()
    sync.sync()
    assert net["fakenet"].reads >= 4
    assert schedule.interval_s({"interval_s": "x"}, FakeNet("n", [])) == 900
    assert schedule.interval_s({"interval_s": True}, None) == 0 and schedule.interval_s({"interval_s": -5}, None) == 0


def test_a_failing_source_backs_off_and_never_blocks_the_others(net):
    setup_cfg()
    net["fakenet"].fail = True
    res = sync.sync()
    assert res["sources"]["fakenet"]["status"] == "error" and "ConnectionError" in res["sources"]["fakenet"]["errors"][0]
    assert res["sources"]["fakechat"]["new_units"] == 195 and res["status"] == "ok"
    st = state()
    assert st["failures"] == 1 and "ConnectionError" in st["last_error"] and st["last_success"] is None
    wait = (datetime.fromisoformat(st["next_due"]) - datetime.fromisoformat(st["last_attempt"])).total_seconds()
    assert wait == schedule.BACKOFF_BASE_S
    skipped = sync.sync()
    assert skipped["sources"]["fakenet"]["status"] == "not_due" and net["fakenet"].reads == 0
    # a second failure waits longer; --force retries at once
    sync.sync(force=True)
    st2 = state()
    assert st2["failures"] == 2
    assert (datetime.fromisoformat(st2["next_due"]) - datetime.fromisoformat(st2["last_attempt"])).total_seconds() == 2 * schedule.BACKOFF_BASE_S
    # recovery resets everything; the cursor was never advanced by the failures
    net["fakenet"].fail = False
    set_next_due(-1)
    ok = sync.sync()
    assert ok["sources"]["fakenet"]["new_units"] == 14
    st3 = state()
    assert st3["failures"] == 0 and st3["last_error"] is None and st3["last_success"]


def test_a_failed_read_keeps_the_previous_cursor_and_units(net):
    setup_cfg(interval_s=0)
    sync.sync()
    con = store.connect()
    cur = store.get_cursor(con, "fakenet", "fakenet-loc")
    con.close()
    net["fakenet"].units = net["fakenet"].units + [replace(u, source="fakenet") for u in make_corpus(seed=9)["fakepub"][:3]]
    net["fakenet"].fail = True
    sync.sync(force=True)
    con = store.connect()
    assert store.get_cursor(con, "fakenet", "fakenet-loc") == cur
    assert con.execute("SELECT count(*) FROM units WHERE source='fakenet'").fetchone()[0] == 14
    con.close()


def test_backoff_is_exponential_capped_and_never_longer_than_the_interval():
    assert [schedule.backoff_s(n) for n in (1, 2, 3, 4)] == [60, 120, 240, 480]
    assert schedule.backoff_s(40) == schedule.BACKOFF_MAX_S
    assert schedule.backoff_s(10, interval=900) == 900 and schedule.backoff_s(1, interval=900) == 60


def test_local_sources_do_not_back_off_but_their_state_is_recorded(net):
    setup_cfg()
    net["fakechat"].crash_after = 5
    res = sync.sync()
    assert res["sources"]["fakechat"]["status"] == "error"
    st = state("fakechat")
    assert st["failures"] == 1 and st["next_due"] is None and "boom" in st["last_error"]
    net["fakechat"].crash_after = None
    assert sync.sync()["sources"]["fakechat"]["new_units"] == 195  # retried on the very next sync
    assert state("fakechat")["failures"] == 0 and state("fakechat")["last_success"]


def test_status_shows_last_success_next_due_and_last_error_without_text(net, capsys):
    setup_cfg()
    sync.sync()
    net["fakenet"].fail = True
    set_next_due(-1)
    sync.sync()
    res = api.status()
    s = res["sources"]["fakenet"]
    assert s["last_success"] and s["next_due"] and "ConnectionError" in s["last_error"] and s["failures"] == 1
    assert res["sources"]["fakechat"]["last_error"] is None and res["sources"]["fakechat"]["next_due"] is None
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "last success" in out and "next due" in out and "ConnectionError" in out
    row = next(ln for ln in out.splitlines() if ln.startswith("fakenet"))
    assert " in " in row  # a future due time


def test_forget_source_drops_its_schedule_state(net):
    setup_cfg()
    sync.sync()
    assert state()["last_success"]
    api.forget(source="fakenet")
    assert state() == {}


def test_watch_runs_the_scheduler_in_a_loop(net):
    setup_cfg()
    reports, sleeps = [], []
    n = sync.watch(5, iterations=1, sleep=sleeps.append, report=reports.append)
    assert n == 1 and sleeps == [] and len(reports) == 1 and reports[0]["sources"]["fakenet"]["new_units"] == 14
    n = sync.watch(5, iterations=3, sleep=sleeps.append, report=reports.append)
    assert n == 3 and sleeps == [5, 5]
    assert reports[-1]["sources"]["fakenet"]["status"] == "not_due"  # the loop respects intervals


def test_cli_watch_stops_on_ctrl_c_and_rejects_conflicting_flags(net, monkeypatch, capsys):
    setup_cfg()

    def interrupted(*a, **kw):
        kw["sleep"] = lambda s: (_ for _ in ()).throw(KeyboardInterrupt())
        return real(*a, **kw)

    real = sync.watch
    monkeypatch.setattr(sync, "watch", interrupted)
    assert cli.main(["sync", "--watch", "--every", "2"]) == 0
    out = capsys.readouterr().out
    assert "watching: a full sync every 2 s" in out and "stopped" in out and "new texts" in out
    for bad in (["--quick"], ["--force"], ["--background"]):
        assert cli.main(["sync", "--watch", *bad]) == 2
        assert "do not combine" in capsys.readouterr().err
    assert cli.main(["sync", "--watch", "--every", "0"]) == 2


def test_cli_sync_force_flag_and_table(net, capsys):
    setup_cfg()
    assert cli.main(["sync"]) == 0
    capsys.readouterr()
    assert cli.main(["sync"]) == 0
    assert "not_due" in capsys.readouterr().out
    assert cli.main(["sync", "--force", "--source", "fakenet"]) == 0
    assert net["fakenet"].reads == 2


def test_enabling_a_network_source_records_consent_and_prints_destinations(net, capsys):
    assert cli.main(["sources", "enable", "fakenet", "--interval", "120"]) == 0
    out = capsys.readouterr().out
    assert "api.example.org" in out and "uses the network" in out
    entry = config.load_config().sources["fakenet"]
    assert entry["enabled"] is True and entry["allow_network"] is True and entry["interval_s"] == 120
    assert cli.main(["sources", "enable", "fakechat"]) == 0
    assert "allow_network" not in config.load_config().sources["fakechat"]
    assert cli.main(["sources", "disable", "fakenet"]) == 0
    entry = config.load_config().sources["fakenet"]
    assert entry["enabled"] is False and "allow_network" not in entry
    assert cli.main(["sources", "enable", "fakenet", "--interval", "-3"]) == 2


def test_setup_and_listing_never_call_discover_on_a_network_adapter(net, capsys):
    net["fakenet"].discover = lambda me, cfg: 1 / 0  # would raise (and a real one would touch the network)
    called = []
    net["fakenet"].discover = lambda me, cfg: called.append(1) or []
    api.setup()
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    assert called == [] and "network" in out


def test_hook_session_start_only_starts_a_quick_background_sync(net, monkeypatch):
    from mannerism import hooks
    setup_cfg()
    calls = []
    monkeypatch.setattr(sync, "sync_in_background", lambda **kw: calls.append(kw) or {"started": True})
    hooks.session_start()
    assert calls == [{"quick": True}] and net["fakenet"].reads == 0


class SoftNet(FakeNet):
    """A network adapter that never raises: a failed read returns its cursor with status.error (the social adapters)."""
    default_interval_s = 0
    retry_after = None

    def read(self, loc, cursor, me):
        if self.fail:
            st = {"error": "HTTP 429", "at": "2026-10-21T10:00:00+00:00"}
            if self.retry_after:
                st["retry_after"] = self.retry_after
            return [], {**(cursor or {}), "status": st}
        units, cur = FakeSource.read(self, loc, cursor, me)
        return units, cur


@pytest.fixture
def soft(home, monkeypatch):
    sources = {"softnet": SoftNet("softnet", [replace(u, source="softnet") for u in make_corpus()["fakepub"]])}
    monkeypatch.setattr(adapters, "REGISTRY", {n: f"fake:{n}" for n in sources})
    monkeypatch.setattr(adapters, "load", lambda name: sources[name])
    cfg = config.load_config()
    cfg.sources["softnet"] = {"enabled": True, "allow_network": True}
    config.save_config(cfg)
    return sources["softnet"]


def test_a_returned_status_error_counts_as_a_failed_run_and_honours_retry_after(soft):
    soft.fail = True
    res = sync.sync()
    assert res["sources"]["softnet"]["status"] == "error" and res["sources"]["softnet"]["errors"] == ["HTTP 429"]
    st = state("softnet")
    assert st["failures"] == 1 and st["last_error"] == "HTTP 429"
    assert (datetime.fromisoformat(st["next_due"]) - datetime.fromisoformat(st["last_attempt"])).total_seconds() == 60
    later = (datetime.now(UTC) + timedelta(hours=2)).isoformat(timespec="seconds")
    soft.retry_after = later
    sync.sync(force=True)
    st = state("softnet")
    assert st["failures"] == 2 and datetime.fromisoformat(st["next_due"]) == datetime.fromisoformat(later)
    assert api.status()["sources"]["softnet"]["last_error"] == "HTTP 429"
    soft.fail = False
    sync.sync(force=True)
    assert state("softnet")["failures"] == 0


class Paid(FakeNet):
    def hosts(self, config):
        return (f"{config.get('instance', 'unset')}.example.social",)

    def status_message(self, config):
        return "set X_BEARER_TOKEN first"

    def estimate(self, config):
        return 1200, 0.6

    def monthly_ceiling(self, config):
        return 5.0


def test_enable_prints_hosts_estimate_and_ceiling_and_asks_first(home, monkeypatch, capsys):
    sources = {"paid": Paid("paid", [])}
    monkeypatch.setattr(adapters, "REGISTRY", {"paid": "fake:paid"})
    monkeypatch.setattr(adapters, "load", lambda name: sources[name])
    cfg = config.load_config()
    cfg.sources["paid"] = {"instance": "mastodon"}
    config.save_config(cfg)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    assert cli.main(["sources", "enable", "paid"]) == 2
    cap = capsys.readouterr()
    assert "mastodon.example.social" in cap.out and "1200 posts" in cap.out and "$5.00" in cap.out
    assert "X_BEARER_TOKEN" in cap.out and "not enabled" in cap.err
    assert not config.load_config().sources["paid"].get("enabled")
    assert cli.main(["sources", "enable", "paid", "--yes"]) == 0
    entry = config.load_config().sources["paid"]
    assert entry["enabled"] and entry["allow_network"] is True and entry["instance"] == "mastodon"


# --- run_on_session_start ----------------------------------------------------------------------------------------------


def started_by_quick(monkeypatch):
    started = []
    monkeypatch.setattr(sync, "sync_in_background", lambda **kw: started.append(kw) or {"started": True})
    return started


def test_run_on_session_start_defaults_by_adapter_kind():
    assert schedule.session_start_default(FakeNet("n", [])) is True
    assert schedule.session_start_default(FakeSource("n", [])) is False  # attribute missing: off
    assert schedule.session_start_default(None) is False  # a plugin is never imported to ask
    assert schedule.session_start_default(Paid("n", [])) is False  # estimate(): a paid service stays off
    paid_opted_in = Paid("n", [])
    paid_opted_in.session_start_default = True
    assert schedule.session_start_default(paid_opted_in) is False  # even if the class says true
    assert schedule.run_on_session_start({"run_on_session_start": True}, Paid("n", [])) is True  # the user decides
    assert schedule.run_on_session_start({"run_on_session_start": False}, FakeNet("n", [])) is False
    assert schedule.run_on_session_start({"run_on_session_start": "yes"}, FakeNet("n", [])) is True  # not a bool: default
    assert schedule.run_on_session_start({}, None) is False and schedule.run_on_session_start({"run_on_session_start": True}) is True


def test_a_session_start_sync_only_launches_sources_that_run_on_session_start(net, monkeypatch):
    started = started_by_quick(monkeypatch)
    setup_cfg(run_on_session_start=False)
    sync.sync(quick=True)
    assert started == [] and net["fakenet"].reads == 0  # due, consented, but the user turned the launch off
    setup_cfg()
    sync.sync(quick=True)
    assert started == [{"quick": False, "sources": ["fakenet"]}]  # the adapter default applies


def test_a_paid_adapter_is_never_launched_at_session_start_unless_configured(home, monkeypatch):
    sources = {"paid": Paid("paid", [])}
    monkeypatch.setattr(adapters, "REGISTRY", {"paid": "fake:paid"})
    monkeypatch.setattr(adapters, "load", lambda name: sources[name])
    started = started_by_quick(monkeypatch)
    cfg = config.load_config()
    cfg.sources["paid"] = {"enabled": True, "allow_network": True}
    config.save_config(cfg)
    sync.sync(quick=True)
    assert started == []
    cfg.sources["paid"]["run_on_session_start"] = True
    config.save_config(cfg)
    sync.sync(quick=True)
    assert started == [{"quick": False, "sources": ["paid"]}]


def test_the_mcp_lazy_path_follows_the_same_rule(net, monkeypatch):
    """sync_if_stale starts a quick sync; that sync decides what else to launch."""
    setup_cfg(run_on_session_start=False)
    calls = []
    real = sync.sync_in_background
    monkeypatch.setattr(sync, "sync_in_background", lambda **kw: calls.append(kw) or {"started": True})
    assert sync.sync_if_stale()["started"] and calls == [{"quick": True}]
    sync.sync(quick=True)
    assert calls == [{"quick": True}]  # nothing more: the source does not run on session start
    assert real is not None


def test_enable_prints_whether_a_session_start_may_launch_it(net, capsys):
    assert cli.main(["sources", "enable", "fakenet"]) == 0
    assert "a session start may launch a background sync" in capsys.readouterr().out
    cfg = config.load_config()
    cfg.sources["fakenet"]["run_on_session_start"] = False
    config.save_config(cfg)
    assert cli.main(["sources", "enable", "fakenet"]) == 0
    out = capsys.readouterr().out
    assert "never launches it" in out and "run_on_session_start = true in [sources.fakenet]" in out


class Caveat(Paid):
    rate_caveat = "rates change; check the developer console"

    def monthly_ceiling(self, config):
        return 7.5


def test_enable_prints_the_rate_caveat_and_a_float_ceiling(home, monkeypatch, capsys):
    sources = {"paid": Caveat("paid", [])}
    monkeypatch.setattr(adapters, "REGISTRY", {"paid": "fake:paid"})
    monkeypatch.setattr(adapters, "load", lambda name: sources[name])
    assert cli.main(["sources", "enable", "paid", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "monthly ceiling about $7.50" in out and "rates change; check the developer console" in out
    assert "set allow_network" not in out
