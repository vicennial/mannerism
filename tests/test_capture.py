from datetime import UTC, datetime, timedelta

from conftest import ingest, unit

from mannerism import capture, store

DRAFT = ("Hi team, I wanted to follow up on the invoice batch and confirm the plan for Friday. Please let me know if "
         "you have any questions.")
FINAL = "I wanted to follow up on the invoice batch and confirm the plan for Friday."


def con_with_draft(register="work", text=DRAFT, **kw):
    con = store.connect()
    capture.log_draft(text, register, "mcp", con=con, now=datetime.now(UTC) - timedelta(hours=1), **kw)
    return con


def new_unit(con, text, register="work", **kw):
    return ingest(con, [unit(text, register, days=0, **kw)], refresh_all=False)


def test_log_draft_masks_caps_and_returns_id(home):
    con = store.connect()
    did = capture.log_draft("please mail jo@example.org about the plan for friday", "work", con=con)
    assert did and "jo@example.org" not in con.execute("SELECT text FROM drafts").fetchone()[0]
    assert capture.log_draft("please mail jo@example.org about the plan for friday", "work", con=con) == did  # refresh, not a new row
    assert capture.log_draft("too short", "work", con=con) is None
    assert capture.log_draft("word " * 2000, "work", con=con) is None  # over 8 KB
    assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1


def test_draft_log_row_and_byte_caps(home, monkeypatch):
    con = store.connect()
    monkeypatch.setattr(capture, "MAX_ROWS", 5)
    topics = ["the invoice batch export", "the onboarding flow redesign", "the staging deploy freeze",
              "the quarterly review deck", "the vendor contract renewal", "the search latency regression",
              "the release checklist cleanup", "the retro notes summary", "the hiring plan update"]
    for i, topic in enumerate(topics):  # distinct topics: must not trigger the near-duplicate collapse
        capture.log_draft(f"draft about {topic} that still needs a decision from the team", "work", con=con,
                          now=datetime.now(UTC) - timedelta(minutes=10 - i))
    assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 5
    monkeypatch.setattr(capture, "MAX_BYTES", 400)
    capture.log_draft("another draft with words to ship soon enough please", "work", con=con)
    assert con.execute("SELECT coalesce(sum(length(text)), 0) FROM drafts").fetchone()[0] <= 400
    old = datetime.now(UTC) - timedelta(days=50)
    capture.log_draft("an ancient draft nobody remembers anymore ok", "work", con=con, now=old)
    assert con.execute("SELECT count(*) FROM drafts WHERE text LIKE 'an ancient%'").fetchone()[0] == 0


def test_edited_and_accepted_statuses(home):
    con = con_with_draft()
    ids = new_unit(con, FINAL)
    out = capture.capture_new_units(con, ids)
    assert out["edited"] == 1 and out["registers"] == ["work"]
    row = con.execute("SELECT status, ratio, unit_id FROM edits").fetchone()
    assert row["status"] == "edited" and 0.5 <= row["ratio"] < 0.9 and row["unit_id"] == ids[0]
    again = capture.capture_new_units(con, ids)  # a unit is captured once
    assert again["edited"] == 0
    con2 = con_with_draft(text=FINAL + " Thanks")
    ids2 = new_unit(con2, FINAL + " Thanks.", ref="z")
    assert capture.capture_new_units(con2, ids2)["accepted"] == 1


def test_case_only_edit_counts_as_edited(home):
    # ratios compare lowercased text, so "starts lowercase" used to read as `accepted` and never became a rule
    assert capture.case_edited(FINAL, FINAL[0].lower() + FINAL[1:])
    assert not capture.case_edited(FINAL, FINAL + " Thanks.")
    assert capture.edit_status(FINAL, FINAL.lower()) == "edited"
    assert capture.edit_status(FINAL, FINAL) == "accepted"
    con = con_with_draft(text=FINAL)
    ids = new_unit(con, "i" + FINAL[1:], ref="lc")
    assert capture.capture_new_units(con, ids)["edited"] == 1


def test_pasted_back_case_only_edit_is_recorded(home):
    con = con_with_draft()
    lowered = DRAFT[0].lower() + DRAFT[1:]
    hit = capture.match_prompt("here's what i sent:\n\n" + lowered, con=con)
    assert hit and hit["status"] == "edited"
    assert capture.match_prompt("here's what i sent:\n\n" + DRAFT, con=con) is None  # unchanged: no signal


def test_register_and_length_gates(home):
    con = con_with_draft(register="formal")
    ids = new_unit(con, FINAL, register="work")
    out = capture.capture_new_units(con, ids)
    assert out["edited"] == 0 and out["unmatched"] == 0  # no draft in that register: not even a candidate
    ids = new_unit(con, FINAL, register="?", ref="q")
    assert capture.capture_new_units(con, ids)["edited"] == 1  # '?' units may match any register
    con.execute("DELETE FROM drafts")
    con = con_with_draft(text=DRAFT + " " + "More padding words to stretch the draft far beyond twice the length. " * 3)
    ids = new_unit(con, FINAL, ref="len")
    assert capture.capture_new_units(con, ids)["unmatched"] == 1  # length ratio outside 0.5 to 2.0


def test_thread_bonus_never_matches_alone_and_ambiguity_is_skipped(home):
    con = con_with_draft(target_id="thr1")
    ids = new_unit(con, "something completely different about lunch and the weather outside today", thread_id="thr1")
    assert capture.capture_new_units(con, ids)["unmatched"] == 1
    borderline = "I wanted to follow up on the invoice batch, can you confirm"  # ratio a bit under 0.5 alone
    r = capture.ratio(capture.clean(DRAFT).lower(), capture.clean(borderline).lower())
    ids = new_unit(con, borderline, thread_id="thr1", ref="b")
    got = capture.capture_new_units(con, ids)
    assert (got["edited"] == 1) == (r + 0.1 >= 0.5 > r) or r >= 0.5
    con = con_with_draft()
    # A second, genuinely different draft (not a near-duplicate of DRAFT, which R3's draft-log dedup
    # would otherwise collapse into one row) that happens to score about as well against FINAL.
    second = ("Hey everyone, I wanted to follow up on the invoice batch and confirm the plan for Friday. "
              "Appreciate you taking a look soon, thanks.")
    capture.log_draft(second, "work", con=con, now=datetime.now(UTC) - timedelta(hours=2))
    ids = new_unit(con, FINAL, ref="amb")
    out = capture.capture_new_units(con, ids)
    assert out["ambiguous"] == 1 and out["edited"] == 0
    assert store.get_meta_json(con, "capture_stats")["s1"]["ambiguous"] == 1


def test_public_capture_requires_meta_public_on_the_unit(home):
    """P1-5: a draft logged in the public register must only match a unit the adapter itself flagged
    meta.public=true (a verified publication) -- never a unit that merely carries the "public" register
    (e.g. from an agent label), which decision 3 never treats as public provenance either."""
    con = con_with_draft(register="public", text=DRAFT)
    ids = new_unit(con, FINAL, register="public", meta={}, ref="notpub")
    out = capture.capture_new_units(con, ids)
    # Same as an ordinary register mismatch elsewhere in this file: not even a capture candidate, so no
    # draft gets consumed and nothing is counted either way.
    assert out["unmatched"] == 0 and out["edited"] == 0 and out["accepted"] == 0
    con2 = con_with_draft(register="public", text=DRAFT)
    ids2 = new_unit(con2, FINAL, register="public", meta={"public": True}, ref="ispub")
    out2 = capture.capture_new_units(con2, ids2)
    assert out2["edited"] == 1


def test_prompt_hook(home):
    con = con_with_draft()
    assert capture.match_prompt("too short", con=con) is None
    assert capture.match_prompt("totally unrelated words in a longer prompt about something else entirely", con=con) is None
    prompt = f"Here is my version, please check the tone:\n\n{FINAL}\n\nThanks"
    hit = capture.match_prompt(prompt, con=con)
    assert hit and hit["status"] == "edited" and hit["ratio"] >= 0.6
    edit = con.execute("SELECT origin, final FROM edits").fetchone()
    assert edit["origin"] == "prompt" and edit["final"] == FINAL
    assert capture.match_prompt(prompt, con=con)["created"] is False  # same pair twice is one edit
    unchanged = capture.match_prompt("use this exactly: \n\n" + DRAFT, con=con)
    assert unchanged is None  # a draft pasted back unchanged carries no edit signal


def test_prompt_and_ingest_sightings_are_one_edit(home):
    con = con_with_draft()
    capture.match_prompt(f"my version:\n\n{FINAL}\n\nplease review it", con=con)
    ids = new_unit(con, FINAL)
    capture.capture_new_units(con, ids)
    rows = con.execute("SELECT unit_id FROM edits").fetchall()
    assert len(rows) == 1 and rows[0]["unit_id"] == ids[0]


def test_record_feedback_masks_and_dedupes(home):
    con = store.connect()
    r = capture.record_feedback("mail me at jo@example.org about the plan please", "mail me about the plan", "work", con=con)
    assert r["status"] == "edited" and r["created"]
    assert "example.org" not in con.execute("SELECT draft FROM edits").fetchone()[0]
    assert not capture.record_feedback("mail me at jo@example.org about the plan please", "mail me about the plan", "work", con=con)["created"]
