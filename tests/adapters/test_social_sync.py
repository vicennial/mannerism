"""The social sources through the real engine: config -> discover -> read -> store, cursor round-trip as JSON."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from mannerism import store, sync
from mannerism.config import Config, save_config

from .fakenet import ok
from .test_bluesky import DID, HANDLE, Server, post


@pytest.fixture
def engine_home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANNERISM_HOME", str(tmp_path / "mh"))


def test_bluesky_through_sync_stores_units_and_a_resumable_cursor(engine_home, net):
    srv = Server([post(i, f"post number {i} with a handful of words in it") for i in range(1, 6)])
    net.handler = srv
    save_config(Config(sources={"bluesky": {"enabled": True, "handle": HANDLE, "allow_network": True}}))
    rep = sync.sync(sources=["bluesky"])
    assert rep["sources"]["bluesky"]["new_units"] == 5 and rep["sources"]["bluesky"]["errors"] == []
    con = store.connect()
    try:
        rows = con.execute("SELECT kind, register, audience, meta_json FROM units WHERE source='bluesky'").fetchall()
        assert len(rows) == 5 and {(r[0], r[1], r[2]) for r in rows} == {("post", "public", "public")}
        assert all(json.loads(r[3])["public"] is True for r in rows)
        cur = store.get_cursor(con, "bluesky", f"bluesky:{HANDLE}")
        assert cur["identity"]["did"] == DID and cur["high_water"]["uri"].endswith("3k0001")
    finally:
        con.close()
    srv.feed.insert(0, post(0, "a brand new post made after the first sync"))
    assert sync.sync(sources=["bluesky"])["sources"]["bluesky"]["new_units"] == 0  # not due yet: interval
    rep2 = sync.sync(sources=["bluesky"], force=True)
    assert rep2["sources"]["bluesky"]["new_units"] == 1


def test_source_without_allow_network_reads_nothing_even_if_enabled(engine_home, net):
    net.handler = Server([post(1, "should never be fetched at all")])
    save_config(Config(sources={"bluesky": {"enabled": True, "handle": HANDLE}}))
    rep = sync.sync(sources=["bluesky"])
    assert rep["sources"]["bluesky"]["locations"] == 0 and net.calls == []


def test_x_without_token_is_quiet_and_makes_no_request(engine_home, net, monkeypatch):
    monkeypatch.delenv("X_BEARER_TOKEN", raising=False)
    net.handler = lambda u, q, h: ok({})
    save_config(Config(sources={"x": {"enabled": True, "username": "ana_k", "allow_network": True}}))
    rep = sync.sync(sources=["x"])
    assert rep["sources"]["x"]["locations"] == 0 and net.calls == []


def test_deleted_post_is_removed_from_the_store_through_sync(engine_home, net):
    srv = Server([post(i, f"post number {i} with a handful of words") for i in range(1, 6)])
    net.handler = srv
    save_config(Config(sources={"bluesky": {"enabled": True, "handle": HANDLE, "allow_network": True}}))
    assert sync.sync(sources=["bluesky"], force=True)["sources"]["bluesky"]["new_units"] == 5
    con = store.connect()
    try:
        cur = store.get_cursor(con, "bluesky", f"bluesky:{HANDLE}")
        cur["reconciled_at"] = (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")
        con.execute("UPDATE cursors SET cursor_json=? WHERE source='bluesky'", (json.dumps(cur),))
        con.commit()
    finally:
        con.close()
    srv.feed.pop(1)
    rep = sync.sync(sources=["bluesky"], force=True)
    assert rep["sources"]["bluesky"]["deleted_units"] == 1
    con = store.connect()
    try:
        assert con.execute("SELECT count(*) FROM units WHERE source='bluesky'").fetchone()[0] == 4
        assert "_delete_refs" not in store.get_cursor(con, "bluesky", f"bluesky:{HANDLE}")
    finally:
        con.close()
