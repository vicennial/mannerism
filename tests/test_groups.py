"""Groups and the review API: stable ids, held-first listing, "not me" and "it's me", migration from schema 3."""
from __future__ import annotations

import sqlite3

import pytest
from transcripts import BOILERPLATE, world

from mannerism import api, config, groups, store, sync


@pytest.fixture
def synced(_isolated_env):
    paths = world(_isolated_env)
    cfg = config.load_config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": [], "dirs": [str(paths["b"])]}
    cfg.sources["codex"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    sync.sync()
    return paths


def test_group_ids_are_stable_and_distinguish_every_field():
    gid = groups.origin_group_id("claude_code", "~/.claude", "app", "interactive")
    assert gid == "g_0c238a309bc4"  # pinned: ids printed by scan stay valid across versions
    assert gid == groups.origin_group_id("claude_code", "~/.claude", "app", "interactive")
    variants = {groups.origin_group_id("claude_code", "~/.claude", "app", "ide"),
                groups.origin_group_id("claude_code", "~/.claude", "web", "interactive"),
                groups.origin_group_id("claude_code", "~/.other", "app", "interactive"),
                groups.origin_group_id("codex", "~/.claude", "app", "interactive")}
    assert gid not in variants and len(variants) == 4
    assert groups.origin_group_id("s", None, None, None) == groups.origin_group_id("s", "", "", "")
    assert groups.is_group_id(gid) and groups.is_group_id("t_0123456789ab")
    assert not groups.is_group_id("g_0123456789ab\n") and not groups.is_group_id("g_0123456789ab ")
    assert not groups.is_group_id("g_xyz") and not groups.is_group_id("") and not groups.is_group_id("x_0123456789ab")


def test_list_groups_puts_held_first_with_counts_and_masked_samples(synced):
    con = store.connect()
    gs = groups.list_groups(con, samples=3)
    held_flags = [g["held"] > 0 for g in gs]
    assert held_flags == sorted(held_flags, reverse=True) and held_flags[0]
    assert {g["kind"] for g in gs} == {"origin", "template"}
    assert sum(g["units"] for g in gs if g["kind"] == "origin") == con.execute("SELECT count(*) FROM units").fetchone()[0]
    tmpl = next(g for g in gs if g["kind"] == "template")
    assert tmpl["units"] == 14 and tmpl["sessions"] == 14 and tmpl["held"] == 14 and tmpl["decision"] is None
    assert tmpl["samples"] and all(len(s) <= groups.SAMPLE_CHARS for s in tmpl["samples"])
    person = next(g for g in gs if g["project"] == "app" and g["account"] == "~/.claude")
    assert person["held"] == 0 and len(person["samples"]) == 3
    assert "jo.dev@example.org" not in str([g["samples"] for g in gs])
    for g in gs:  # JSON-serialisable plain data
        assert set(g) >= {"group_id", "kind", "source", "account", "project", "session_kind", "units", "held", "sessions",
                          "first", "last", "samples", "decision"}


def test_group_detail_and_review_state(synced):
    con = store.connect()
    tmpl = next(g for g in groups.list_groups(con) if g["kind"] == "template")
    d = groups.group_detail(con, tmpl["group_id"], samples=5)
    assert d["units"] == 14 and len(d["samples"]) == 5
    assert groups.group_detail(con, "g_000000000000") is None and groups.group_detail(con, "junk") is None
    state = groups.review_state(con)
    assert state["held_total"] == con.execute("SELECT count(*) FROM units WHERE held=1").fetchone()[0]
    assert state["decisions"] == [] and state["rules"] == [] and state["groups"][0]["held"] > 0
    assert api.review_groups()["held_total"] == state["held_total"]


def test_not_me_excludes_the_group_deletes_its_texts_and_keeps_it_out(synced):
    con = store.connect()
    tmpl = next(g for g in groups.list_groups(con) if g["kind"] == "template")
    before = con.execute("SELECT count(*) FROM units").fetchone()[0]
    con.close()
    res = groups.not_me(tmpl["group_id"])
    assert res["ok"] and res["removed"] == 14
    con = store.connect()
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == before - 14
    assert con.execute("SELECT grp FROM exclusions").fetchall()[0][0] == tmpl["group_id"]
    assert tmpl["group_id"] not in {g["group_id"] for g in groups.list_groups(con)}
    sync.sync()
    assert store.connect().execute("SELECT count(*) FROM units").fetchone()[0] == before - 14


def test_its_me_releases_held_texts_into_the_bank_and_remembers(synced):
    con = store.connect()
    tmpl = next(g for g in groups.list_groups(con) if g["kind"] == "template")
    assert con.execute("SELECT count(*) FROM bank WHERE text LIKE 'Continue working%'").fetchone()[0] == 0
    con.close()
    res = groups.its_me(tmpl["group_id"])
    assert res["ok"] and res["released"] == 14
    con = store.connect()
    assert con.execute("SELECT count(*) FROM units WHERE text LIKE 'Continue working%' AND held=1").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM bank WHERE text LIKE 'Continue working%'").fetchone()[0] == 14
    assert groups.kept_groups(con) == {tmpl["group_id"]}
    assert next(g for g in groups.list_groups(con) if g["group_id"] == tmpl["group_id"])["decision"] == "keep"
    # later texts of the group are not held, even though they repeat the template again
    from datetime import UTC, datetime

    from transcripts import write_claude
    write_claude(synced["b"], "-srv-orch-run", "ffffffff-0000-0000-0000-000000000000", [BOILERPLATE.format(n=999)],
                 "/srv/orch/runx", start=datetime(2026, 8, 1, tzinfo=UTC))
    sync.sync()
    assert store.connect().execute("SELECT held FROM units WHERE text LIKE '%ABC-999%'").fetchone()[0] == 0
    assert groups.its_me(tmpl["group_id"])["released"] == 0  # idempotent


def test_not_me_after_its_me_replaces_the_decision(synced):
    con = store.connect()
    tmpl = next(g for g in groups.list_groups(con) if g["kind"] == "template")
    con.close()
    groups.its_me(tmpl["group_id"])
    assert groups.not_me(tmpl["group_id"])["ok"]
    con = store.connect()
    assert groups.kept_groups(con) == set()


def test_origin_group_actions(synced):
    con = store.connect()
    g = next(g for g in groups.list_groups(con) if g["project"] == "app" and g["source"] == "codex")
    con.close()
    assert groups.not_me(g["group_id"])["removed"] == g["units"]
    assert api.review_keep("junk")["ok"] is False and groups.not_me("junk")["ok"] is False


def test_review_rescan_scores_old_units_and_holds_them(synced):
    con = store.connect()
    con.execute("UPDATE units SET human_score=NULL, held=0, template=NULL, score_json=NULL")
    con.close()
    res = api.review_rescan()
    assert res["ok"] and res["scored"] > 0 and res["held"] >= 14
    assert api.status()["held"] == res["held"]


# --- migration from the previous schema ----------------------------------------------------------------------------------

OLD_UNITS = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE units (
  unit_id TEXT PRIMARY KEY, source TEXT NOT NULL, source_ref TEXT NOT NULL, thread_id TEXT,
  audience TEXT NOT NULL DEFAULT 'unknown', audience_ref TEXT, ts TEXT NOT NULL, text TEXT NOT NULL,
  parts INTEGER NOT NULL DEFAULT 1, kind TEXT NOT NULL, register TEXT NOT NULL DEFAULT '?', context_json TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}', run_id TEXT, added_at TEXT NOT NULL
);
CREATE INDEX units_source_ts ON units(source, ts);
"""


def test_a_schema_3_store_is_migrated_in_place_and_keeps_its_rows(home):
    home.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(home / "corpus.db")
    raw.executescript(OLD_UNITS)
    raw.execute("INSERT INTO meta VALUES ('schema_version', '3')")
    raw.execute("INSERT INTO units(unit_id, source, source_ref, ts, text, kind, register, added_at) "
                "VALUES ('old1','claude_code','r1','2026-01-01T00:00:00+00:00','an old text from before','prompt',"
                "'agent','2026-01-01T00:00:00+00:00')")
    raw.commit()
    raw.close()
    con = store.connect()
    assert store.get_meta(con, "schema_version") == str(store.SCHEMA_VERSION)
    cols = {r[1] for r in con.execute("PRAGMA table_info(units)")}
    assert {"account", "project", "project_path", "session_kind", "session", "template", "human_score", "held",
            "score_json"} <= cols
    row = con.execute("SELECT text, account, held, human_score FROM units").fetchone()
    assert tuple(row) == ("an old text from before", None, 0, None)
    assert {"exclusions", "group_decisions"} <= {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    idx = {r[1] for r in con.execute("PRAGMA index_list(units)")}
    assert {"units_group", "units_template", "units_session"} <= idx
    # old units match source rules only, are never held, and still reach the bank
    from mannerism import exclude
    assert exclude.matching_unit_ids(con, [exclude.make_rule(project="app")]) == []
    assert exclude.matching_unit_ids(con, [exclude.make_rule(source="claude_code")]) == ["old1"]
    store.connect().close()  # reopening a migrated store is a no-op
