"""Local own-post and candidate fixtures are entirely synthetic."""
import json
from datetime import UTC, datetime, timedelta

import pytest

from mannerism import bank, capture, published, store
from mannerism.core import Me, Unit

NOW = datetime(2026, 10, 20, 12, tzinfo=UTC)
DRAFT = "The little garden beside our station needs more shade and better paths for everyone."
EDITED = "Our garden needs shade. Wider paths would let everyone use it."
OWN = "Bats find their way through the night by listening to echoes from nearby objects."
ACCEPTED = "The old ferry reaches the island every morning and returns before the evening fog."


def post(pid, text, **kw):
    return {"id": str(pid), "created_at": NOW.isoformat(), "text": text, **kw}


def posts_file(tmp_path, posts, shape="data"):
    path = tmp_path / "own.json"
    path.write_text(json.dumps(posts if shape == "list" else {shape: posts}))
    return path


def candidate_file(tmp_path, rows):
    path = tmp_path / "cards.jsonl"
    path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows))
    return path


def log(con, text, register="public", now=None, **kw):
    return capture.log_draft(text, register, con=con, now=now or NOW - timedelta(hours=1), **kw)


def test_edited_accepted_unmatched_and_duplicate_run(tmp_path):
    con = store.connect()
    log(con, DRAFT)
    log(con, ACCEPTED)
    path = posts_file(tmp_path, [post(1, EDITED), post(2, ACCEPTED), post(3, OWN)])
    result = published.capture_posts(path, con=con)
    assert result == {"edited": 1, "accepted": 1, "unmatched": 1, "ambiguous": 0, "skipped": 0}
    assert [(r["source"], r["source_ref"]) for r in con.execute("SELECT * FROM units ORDER BY source_ref")] == [
        ("x", "1"), ("x", "2"), ("x", "3")]
    edits = con.execute("SELECT * FROM edits").fetchall()
    assert len(edits) == 2 and all(e["unit_id"] for e in edits)
    assert [r[0] for r in con.execute("SELECT source_ref FROM units WHERE unit_id IN (SELECT unit_id FROM bank) "
                                    "ORDER BY source_ref")] == ["1", "3"]
    again = published.capture_posts(path, con=con)
    assert all(v == 0 for v in again.values())
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 2
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 2
    assert json.loads(con.execute("SELECT meta_json FROM units LIMIT 1").fetchone()[0])["public"] is True
    con.close()


@pytest.mark.parametrize("shape", ["data", "items", "list"])
def test_note_tweet_and_legacy_shapes(tmp_path, shape):
    path = posts_file(tmp_path, [post(7, "shortened…", note_tweet={"text": "@friend " + OWN + " &amp; more"})], shape)
    out = published.capture_posts(path)
    assert out["unmatched"] == 1
    con = store.connect()
    row = con.execute("SELECT * FROM units").fetchone()
    assert row["text"] == OWN + " & more" and row["source_ref"] == "7" and row["kind"] == "reply"
    con.close()


def test_structured_candidates_are_drafts_only_and_dedupe_lint_log(tmp_path):
    con = store.connect()
    log(con, ACCEPTED)
    candidates = candidate_file(tmp_path, [
        {"added": (NOW - timedelta(hours=1)).isoformat(), "reply_text": ACCEPTED, "target_id": "42"},
        {"ts": (NOW - timedelta(hours=2)).isoformat(), "text": DRAFT},
        {"ts": NOW.isoformat(), "reply_text": OWN, "register": "work"},
        "broken json", [], {"text": 45}, {"ts": "bad", "text": OWN},
        {"ts": NOW.isoformat(), "text": OWN, "target_id": {}},
    ])
    before = candidates.read_bytes()
    path = posts_file(tmp_path, [post(1, ACCEPTED, conversation_id="42"), post(2, EDITED), post(3, OWN)])
    result = published.capture_posts(path, [candidates, tmp_path / "optional-missing.jsonl"], con=con)
    assert result["accepted"] == 1 and result["edited"] == 1 and result["unmatched"] == 1
    assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM bank WHERE text=?", (ACCEPTED,)).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM bank WHERE source='edit'").fetchone()[0] == 0
    assert candidates.read_bytes() == before
    con.close()


def test_only_eligible_public_drafts_match(tmp_path):
    con = store.connect()
    log(con, ACCEPTED, now=NOW + timedelta(hours=1))
    log(con, DRAFT, now=NOW - timedelta(days=20))
    log(con, OWN, register="work")
    path = posts_file(tmp_path, [post(1, ACCEPTED), post(2, DRAFT), post(3, OWN)])
    assert published.capture_posts(path, con=con)["unmatched"] == 3
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    con.close()


def test_thread_target_is_bonus_not_proof_of_authorship(tmp_path):
    con = store.connect()
    cards = candidate_file(tmp_path, [{"ts": (NOW - timedelta(hours=1)).isoformat(), "text": ACCEPTED,
                                       "target_id": "42"}])
    path = posts_file(tmp_path, [post(1, OWN, conversation_id="42")])
    out = published.capture_posts(path, [cards], con=con)
    assert out["unmatched"] == 1 and out["edited"] == 0
    con.close()


def test_ambiguous_ai_match_and_case_edit_stay_out_of_bank(tmp_path):
    con = store.connect()
    cards = candidate_file(tmp_path, [
        {"ts": (NOW - timedelta(hours=1)).isoformat(), "text": ACCEPTED + " Often."},
        {"ts": (NOW - timedelta(hours=1)).isoformat(), "text": ACCEPTED + " Rarely."},
    ])
    path = posts_file(tmp_path, [post(1, ACCEPTED)])
    assert published.capture_posts(path, [cards], con=con)["ambiguous"] == 1
    assert con.execute("SELECT count(*) FROM ai_units").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    store.forget_rows(con, drafts_only=True)
    bank.build(con, ["public"])
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    log(con, OWN)
    path = posts_file(tmp_path, [post(2, OWN.lower())])
    assert published.capture_posts(path, con=con)["edited"] == 1
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    con.close()


def test_existing_imported_x_unit_and_edit_pair_are_not_duplicated(tmp_path):
    con = store.connect()
    unit = Unit("x", "7", NOW, EDITED, "post", register="public", audience="public", meta={"public": True})
    store.write_units(con, "x", "legacy-import", [unit], {})
    capture.add_edit(con, "public", DRAFT, EDITED, "legacy", unit_id=unit.unit_id)
    log(con, DRAFT)
    path = posts_file(tmp_path, [post(7, EDITED)])
    assert published.capture_posts(path, con=con)["edited"] == 1
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 1
    con.close()


def test_prompt_hook_edit_attaches_to_published_unit(tmp_path):
    con = store.connect()
    capture.add_edit(con, "public", DRAFT, EDITED, "feedback")
    log(con, DRAFT)
    assert published.capture_posts(posts_file(tmp_path, [post(1, EDITED)]), con=con)["edited"] == 1
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 1
    assert con.execute("SELECT unit_id FROM edits").fetchone()[0] is not None
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 1
    con.close()


def test_malformed_rows_retweets_and_other_authors_are_skipped(tmp_path):
    con = store.connect()
    rows = [None, [], "bad", {}, post(True, OWN), post(1, 45), post(2, OWN, created_at="invalid"),
            post(3, "RT @other: " + OWN), post(4, OWN, isRetweet=True),
            post(5, OWN, referenced_tweets=[{"type": "retweeted", "id": "x"}]),
            post(6, OWN, author={"userName": "someone_else"}), post(7, OWN, author_id="wrong"),
            post(8, OWN, conversation_id={}), post(9, "https://t.co/only"),
            post(12, OWN, author="unknown-author"), post(13, OWN, author={}),
            post(14, OWN, created_at="0001-01-01T00:00:00+01:00"),
            post("1" * 5000, OWN), post(15, "unpaired surrogate \ud800"),
            post(10, OWN, note_tweet=["invalid", "note"]),
            {"id": "11", "text": ACCEPTED, "createdAt": "Tue Oct 20 12:00:00 +0000 2026",
             "author": {"screen_name": "@JO"}}]
    me = Me(handles={"x": "jo", "x_id": "own-id"})
    out = published.capture_posts(posts_file(tmp_path, rows), con=con, me=me)
    assert out["unmatched"] == 2 and out["skipped"] == len(rows) - 2
    con.close()


@pytest.mark.parametrize("raw", ["broken", "{}", '{"data":{}}', "null"])
def test_bad_file_shape_fails_before_store_open(tmp_path, monkeypatch, raw):
    path = tmp_path / "posts.json"
    path.write_text(raw)

    def forbidden():
        pytest.fail("invalid input opened the store")

    monkeypatch.setattr(store, "connect", forbidden)
    with pytest.raises(ValueError):
        published.capture_posts(path)


def test_exclusions_and_held_inputs_do_not_produce_edit_evidence(tmp_path, monkeypatch):
    from mannerism import config

    cfg = config.Config(exclude=[{"source": "x", "text": "^Bats"}])
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    con = store.connect()
    log(con, OWN)
    path = posts_file(tmp_path, [post(1, OWN), post(2, '{"message": "synthetic structured text from a tool"}')])
    assert published.capture_posts(path, con=con)["skipped"] == 2
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    con.close()


@pytest.mark.parametrize("imported", [False, True])
def test_interrupted_match_is_held_and_retry_is_safe(tmp_path, monkeypatch, imported):
    con = store.connect()
    log(con, ACCEPTED)
    path = posts_file(tmp_path, [post(1, ACCEPTED)])
    if imported:
        from mannerism import public

        unit = Unit("x", "1", NOW, ACCEPTED, "post", register="public", audience="public", meta={"public": True})
        store.write_units(con, "x", "legacy-import", [unit], {})
        published.refresh.refresh(con, ["public"])
        assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 1
        public.build_snapshot(con)
        assert public.snapshot_path().exists()
    real = published._match

    def broken(*args):
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(published, "_match", broken)
    with pytest.raises(RuntimeError, match="synthetic interruption"):
        published.capture_posts(path, con=con)
    assert con.execute("SELECT held FROM units").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    metadata = json.loads(con.execute("SELECT meta_json FROM units").fetchone()[0])
    assert metadata[published._QUARANTINE]["prior_held"] is False
    if imported:
        assert not public.snapshot_path().exists()
        assert store.get_meta(con, "public_snapshot_dirty") == "1"
    bank.build(con, ["public"])
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    monkeypatch.setattr(published, "_match", real)
    assert published.capture_posts(path, con=con)["accepted"] == 1
    assert con.execute("SELECT held FROM units").fetchone()[0] == 0
    metadata = json.loads(con.execute("SELECT meta_json FROM units").fetchone()[0])
    assert published._QUARANTINE not in metadata
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    con.close()


def test_unchanged_imported_held_post_stays_excluded(tmp_path):
    from mannerism import public

    con = store.connect()
    unit = Unit("x", "1", NOW, ACCEPTED, "post", register="public", audience="public",
                held=True, meta={"public": True})
    store.write_units(con, "x", "legacy-import", [unit], {})
    log(con, ACCEPTED)
    path = posts_file(tmp_path, [post(1, ACCEPTED)])
    assert published.capture_posts(path, con=con)["skipped"] == 1
    row = con.execute("SELECT held,meta_json FROM units").fetchone()
    assert row["held"] == 1 and published._QUARANTINE not in json.loads(row["meta_json"])
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    assert public.build_snapshot(con)["posts"] == 0
    assert all(v == 0 for v in published.capture_posts(path, con=con).values())
    assert con.execute("SELECT held FROM units").fetchone()[0] == 1
    con.close()


def test_interrupted_unheld_own_post_resumes_and_enters_bank(tmp_path, monkeypatch):
    from mannerism import public

    con = store.connect()
    path = posts_file(tmp_path, [post(1, OWN)])
    real = published._match

    def interrupted(*args):
        raise RuntimeError("interrupted unmatched capture")

    monkeypatch.setattr(published, "_match", interrupted)
    with pytest.raises(RuntimeError, match="interrupted unmatched capture"):
        published.capture_posts(path, con=con)
    row = con.execute("SELECT held,meta_json FROM units").fetchone()
    assert row["held"] == 1
    assert json.loads(row["meta_json"])[published._QUARANTINE]["prior_held"] is False
    monkeypatch.setattr(published, "_match", real)
    assert published.capture_posts(path, con=con)["unmatched"] == 1
    row = con.execute("SELECT held,meta_json FROM units").fetchone()
    assert row["held"] == 0 and published._QUARANTINE not in json.loads(row["meta_json"])
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 1
    assert public.build_snapshot(con)["posts"] == 1
    assert all(v == 0 for v in published.capture_posts(path, con=con).values())
    con.close()


def test_imported_ai_flags_and_accepted_edits_survive_draft_forgetting(tmp_path):
    con = store.connect()
    units = [Unit("x", "1", NOW, OWN, "post", register="public", audience="public", meta={"ai": True}),
             Unit("x", "2", NOW, ACCEPTED, "post", register="public", audience="public")]
    store.write_units(con, "x", "legacy-import", units, {})
    capture.add_edit(con, "public", ACCEPTED, ACCEPTED, "legacy", unit_id=units[1].unit_id)
    out = published.capture_posts(posts_file(tmp_path, [post(1, OWN), post(2, ACCEPTED)]), con=con)
    assert out["accepted"] == 1 and out["unmatched"] == 1
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    store.forget_rows(con, drafts_only=True)
    bank.build(con, ["public"])
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    con.close()


def test_repeated_candidate_text_keeps_eligible_timestamp(tmp_path):
    con = store.connect()
    log(con, ACCEPTED, now=NOW + timedelta(hours=1))
    cards = candidate_file(tmp_path, [{"ts": (NOW - timedelta(hours=1)).isoformat(), "text": ACCEPTED}])
    assert published.capture_posts(posts_file(tmp_path, [post(1, ACCEPTED)]), [cards], con=con)["accepted"] == 1
    con.close()


def test_updated_post_replaces_its_edit_and_no_bank_evidence_is_duplicated(tmp_path):
    con = store.connect()
    log(con, DRAFT)
    path = posts_file(tmp_path, [post(1, DRAFT)])
    assert published.capture_posts(path, con=con)["accepted"] == 1
    path = posts_file(tmp_path, [post(1, EDITED)])
    assert published.capture_posts(path, con=con)["edited"] == 1
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM ai_units").fetchone()[0] == 0
    assert published.capture_posts(path, con=con)["edited"] == 0
    con.close()


def test_failed_refresh_can_be_retried_without_duplicate_decisions(tmp_path, monkeypatch):
    con = store.connect()
    path = posts_file(tmp_path, [post(1, OWN)])
    real = published.refresh.refresh

    def broken(*args):
        raise RuntimeError("synthetic refresh failure")

    monkeypatch.setattr(published.refresh, "refresh", broken)
    with pytest.raises(RuntimeError, match="synthetic refresh failure"):
        published.capture_posts(path, con=con)
    assert store.get_meta_json(con, published._PENDING) == ["public"]
    monkeypatch.setattr(published.refresh, "refresh", real)
    assert published.capture_posts(path, con=con)["unmatched"] == 0
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 1
    assert store.get_meta(con, published._PENDING) is None
    con.close()


def test_existing_skip_label_blocks_public_edit_learning(tmp_path):
    con = store.connect()
    unit = Unit("x", "1", NOW, ACCEPTED, "post", register="public", audience="public", meta={"public": True})
    store.write_units(con, "x", "legacy-import", [unit], {})
    con.execute("INSERT INTO labels(unit_id,register,labeller,labelled_at) VALUES (?,?,?,?)",
                (unit.unit_id, "skip", "user", store.now_iso()))
    log(con, ACCEPTED)
    assert published.capture_posts(posts_file(tmp_path, [post(1, ACCEPTED)]), con=con)["skipped"] == 1
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    con.close()


@pytest.mark.parametrize("privacy_flag", ["private_fact", "third_party_detail"])
def test_unchanged_imported_post_keeps_privacy_denial(tmp_path, privacy_flag):
    from mannerism import public

    con = store.connect()
    metadata = {"public": False, privacy_flag: True,
                "legacy_voicekit": {"unit": {"text": ACCEPTED},
                                    "selected_label": {"labels_json": json.dumps({privacy_flag: True})}}}
    unit = Unit("x", "1", NOW, ACCEPTED, "post", register="public", audience="public", meta=metadata)
    store.write_units(con, "x", "legacy-import", [unit], {})
    log(con, ACCEPTED)
    assert published.capture_posts(posts_file(tmp_path, [post(1, ACCEPTED)]), con=con)["skipped"] == 1
    saved = json.loads(con.execute("SELECT meta_json FROM units").fetchone()[0])
    assert saved["public"] is False and saved[privacy_flag] is True
    assert saved["legacy_voicekit"] == metadata["legacy_voicekit"]
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert public.build_snapshot(con)["posts"] == 0
    assert all(v == 0 for v in published.capture_posts(posts_file(tmp_path, [post(1, ACCEPTED)]), con=con).values())
    con.close()


@pytest.mark.parametrize("ai", [False, True])
def test_changed_imported_post_discards_old_raw_metadata_and_keeps_ai_denial(tmp_path, ai):
    from mannerism import public

    con = store.connect()
    metadata = {"public": False, "private_fact": True, "third_party_detail": True,
                "ai": ai, "old_content_flag": "ObsoleteSyntheticCanary",
                "legacy_voicekit": {"unit": {"text": DRAFT}, "selected_label": {"detail": "obsolete"}}}
    unit = Unit("x", "1", NOW, DRAFT, "post", register="public", audience="public", meta=metadata)
    store.write_units(con, "x", "legacy-import", [unit], {})
    # The integrated store erases migration audit rows when their source text changes.
    con.execute("CREATE TABLE legacy_voicekit_rows(source_db TEXT,source_table TEXT,source_key TEXT,"
                "payload_json TEXT,target_id TEXT)")
    con.execute("CREATE TABLE legacy_voicekit_cards(name TEXT)")
    con.execute("INSERT INTO legacy_voicekit_rows VALUES ('corpus','units','old',?,?)",
                (json.dumps(metadata), unit.unit_id))
    assert published.capture_posts(posts_file(tmp_path, [post(1, OWN)]), con=con)["unmatched"] == 1
    row = con.execute("SELECT text,meta_json FROM units").fetchone()
    saved = json.loads(row["meta_json"])
    assert row["text"] == OWN and saved["public"] is True
    assert not {"legacy_voicekit", "private_fact", "third_party_detail", "old_content_flag"} & saved.keys()
    assert "ObsoleteSyntheticCanary" not in row["meta_json"] and DRAFT not in row["meta_json"]
    assert saved.get("ai", False) is ai
    assert con.execute("SELECT count(*) FROM legacy_voicekit_rows").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM bank").fetchone()[0] == int(not ai)
    assert public.build_snapshot(con)["posts"] == int(not ai)
    con.close()


def test_projection_is_revoked_before_existing_post_write_can_interrupt(tmp_path, monkeypatch):
    from mannerism import public

    con = store.connect()
    unit = Unit("x", "1", NOW, OWN, "post", register="public", audience="public", meta={"public": True})
    store.write_units(con, "x", "legacy-import", [unit], {})
    published.refresh.refresh(con, ["public"])
    public.build_snapshot(con)
    assert public.snapshot_path().exists()

    def interrupted_write(*args, **kwargs):
        assert not public.snapshot_path().exists()
        assert con.execute("SELECT held FROM units").fetchone()[0] == 0
        raise RuntimeError("interrupted before held mutation")

    monkeypatch.setattr(store, "write_units", interrupted_write)
    with pytest.raises(RuntimeError, match="interrupted before held mutation"):
        published.capture_posts(posts_file(tmp_path, [post(1, OWN)]), con=con)
    assert not public.snapshot_path().exists()
    assert con.execute("SELECT held FROM units").fetchone()[0] == 0
    assert store.get_meta(con, "public_snapshot_dirty") == "1"
    with pytest.raises(ValueError, match="successful corpus refresh"):
        public.build_snapshot(con)
    assert not public.snapshot_path().exists()
    con.close()
