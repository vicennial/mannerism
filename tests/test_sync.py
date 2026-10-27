import fcntl
import os
import time
from datetime import timedelta, timezone

from mannerism import adapters, api, config, store, sync


def enable(*names, tier=None):
    cfg = config.load_config()
    for n in names:
        cfg.sources[n] = {"enabled": True, "paths": []}
    config.save_config(cfg)


def units_in(con=None):
    con = con or store.connect()
    return con.execute("SELECT count(*) FROM units").fetchone()[0]


def test_second_sync_reads_nothing_new(fakes):
    enable("fakechat")
    first = sync.sync()
    assert first["status"] == "ok" and first["new_units"] == 195 and first["sources"]["fakechat"]["locations"] == 1
    assert units_in() == 195
    second = sync.sync()
    assert second["new_units"] == 0 and units_in() == 195
    con = store.connect()
    assert store.get_cursor(con, "fakechat", "fakechat-loc") == {"i": 195}
    assert store.get_meta(con, "last_sync")


def test_source_since_skips_history_but_advances_cursor(fakes):
    enable("fakechat")
    cfg = config.load_config()
    cfg.sources["fakechat"]["since"] = "2099-01-01T00:00:00Z"
    config.save_config(cfg)
    result = sync.sync()
    assert result["new_units"] == 0
    assert store.get_cursor(store.connect(), "fakechat", "fakechat-loc") == {"i": 195}


def test_source_since_requires_timezone_before_read(fakes):
    enable("fakechat")
    cfg = config.load_config()
    cfg.sources["fakechat"]["since"] = "2026-01-01"
    config.save_config(cfg)
    result = sync.sync()
    assert result["sources"]["fakechat"]["status"] == "error"
    assert store.get_cursor(store.connect(), "fakechat", "fakechat-loc") is None


def test_source_since_is_exclusive_with_mixed_units_and_offset(fakes):
    enable("fakechat")
    rows = fakes["fakechat"].units
    boundary = sorted(u.ts for u in rows)[len(rows) // 2]
    cfg = config.load_config()
    cfg.sources["fakechat"]["since"] = boundary.astimezone(timezone(timedelta(hours=5))).isoformat()
    config.save_config(cfg)
    expected = {u.unit_id for u in rows if u.ts > boundary}
    sync.sync()
    con = store.connect()
    assert {r[0] for r in con.execute("SELECT unit_id FROM units")} == expected
    assert store.get_cursor(con, "fakechat", "fakechat-loc") == {"i": len(rows)}


def test_crash_mid_location_does_not_advance_the_cursor(fakes):
    enable("fakechat", "fakepub")
    fakes["fakechat"].crash_after = 50
    res = sync.sync()
    assert res["sources"]["fakechat"]["status"] == "error" and "boom" in res["sources"]["fakechat"]["errors"][0]
    con = store.connect()
    assert store.get_cursor(con, "fakechat", "fakechat-loc") is None
    assert con.execute("SELECT count(*) FROM units WHERE source='fakechat'").fetchone()[0] == 0
    assert res["sources"]["fakepub"]["new_units"] == 14  # other sources are unaffected
    assert con.execute("SELECT error FROM ingest_runs WHERE error IS NOT NULL").fetchone()
    fakes["fakechat"].crash_after = None
    assert sync.sync(sources=["fakechat"])["new_units"] == 195  # the retry re-reads everything: nothing was skipped


def test_missing_and_broken_adapters_are_reported_not_fatal(fakes, monkeypatch):
    enable("fakechat", "ghost", "fakepub")
    real = adapters.load

    def load(name):
        if name == "ghost":
            raise ImportError("no module")
        return real(name)

    monkeypatch.setattr(adapters, "load", load)
    fakes["fakepub"].discover = lambda me, cfg: 1 / 0
    res = sync.sync()
    assert res["sources"]["ghost"]["status"] == "adapter_missing"
    assert res["sources"]["fakepub"]["status"] == "error"
    assert res["sources"]["fakechat"]["new_units"] == 195


def test_quick_only_runs_auto_sources_within_budget(fakes):
    enable("fakechat", "fakepub")
    fakes["fakepub"].tier = "export"
    res = sync.sync(quick=True)
    assert res["sources"]["fakepub"]["status"] == "skipped_not_auto" and res["sources"]["fakechat"]["new_units"] == 195
    res = sync.sync(quick=False, sources=["fakepub"], budget_s=0.0)  # budget spent before the first location starts
    assert res["sources"]["fakepub"]["skipped_locations"] == 1 and res["budget_exhausted"]
    assert sync.sync(sources=["fakepub"])["sources"]["fakepub"]["new_units"] == 14


def test_lock_second_sync_returns_busy(fakes):
    enable("fakechat")
    config.ensure_home()
    fd = os.open(sync.lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        res = sync.sync()
        assert res["status"] == "busy" and units_in() == 0
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    assert sync.sync()["status"] == "ok"


def test_lock_blocks_a_parallel_thread(fakes):
    import threading
    enable("fakechat")
    inside, release, results = threading.Event(), threading.Event(), []
    real_read = fakes["fakechat"].read

    def slow_read(loc, cursor, me):
        inside.set()
        release.wait(5)
        return real_read(loc, cursor, me)

    fakes["fakechat"].read = slow_read
    t = threading.Thread(target=lambda: results.append(sync.sync()))
    t.start()
    assert inside.wait(5)
    assert sync.sync()["status"] == "busy"
    release.set()
    t.join(10)
    assert results[0]["status"] == "ok"


def test_background_sync_returns_at_once_and_finishes(home):
    started = time.monotonic()
    res = sync.sync_in_background(quick=True)
    assert res["started"] and time.monotonic() - started < 2
    con = store.connect()
    for _ in range(100):
        if store.get_meta(con, "last_sync"):
            break
        time.sleep(0.1)
    assert store.get_meta(con, "last_sync")  # the detached worker ran and recorded itself
    assert sync.sync_if_stale() == {"started": False, "reason": "recent"}


def test_setup_does_not_auto_enable_opt_in_sources(fakes, monkeypatch):
    gh = type(fakes["fakechat"])("gh", fakes["fakechat"].units[:3])
    monkeypatch.setattr(adapters, "REGISTRY", {**adapters.REGISTRY, "gh": "x:gh"})
    real = adapters.load
    monkeypatch.setattr(adapters, "load", lambda n: gh if n == "gh" else real(n))
    res = api.setup()
    assert next(s for s in res["sources"] if s["name"] == "gh")["enabled"] is False
    assert api.setup(sources=["gh"])["sources"][-1]["enabled"] is True
