import os
import stat

from conftest import ingest, unit

from mannerism import api, capture, packets, store

CARD = "Writes short plain messages in lowercase, rarely ends them with punctuation and keeps greetings out. " * 2


def corpus_two_sources():
    rows = [unit(f"message {i} with some varied words {'x' * (i % 9)}", "close", ref=f"a{i}", days=i + 1,
                 source="s1", thread_id=f"t{i}") for i in range(40)]
    rows += [unit(f"other message {i} that is a bit different", "close", ref=f"b{i}", days=i + 1, source="s2",
                  thread_id=f"u{i}") for i in range(10)]
    return rows


def test_forget_source_marks_agent_cards_stale_and_queues_a_new_one(home):
    con = store.connect()
    ingest(con, corpus_two_sources())
    pk = packets.next_packet("card", con=con)
    assert packets.submit("card", pk["packet_id"], {"register": "close", "card": CARD}, con=con)["saved"]
    assert api.status()["registers"]["close"]["state"] == "agent card ready"
    open_label = packets.next_packet("label", con=con)  # nothing to label: no packet
    assert open_label is None
    out = api.forget(source="s2")
    assert out["ok"] and out["stale_cards"] == ["close"] and "outside local deletion" in out["note"]
    st = api.status()
    assert st["registers"]["close"]["card_source"] == "stats" and st["registers"]["close"]["units"] == 40
    nxt = api.work_next("card")["packet"]
    assert nxt["register"] == "close" and "s2" not in str(nxt["payload"]["examples"])


def test_forget_invalidates_open_packets_and_drafts_option(home):
    con = store.connect()
    ingest(con, [unit(f"quick note {i} about the plan for people", "?", ref=f"q{i}", days=i + 1) for i in range(5)])
    pk = packets.next_packet("label", con=con)
    capture.log_draft("a draft that should vanish when forgotten ok", "work", con=con)
    capture.record_feedback("Hi, please look at the thing soon thanks", "please look at the thing", "work", con=con)
    api.forget(source="s1")
    assert not api.work_submit("label", pk["packet_id"], {"labels": []})["ok"]
    ingest(con, [unit("another note to keep around for later use", "close", ref="keep")])
    out = api.forget(drafts=True)
    assert out["ok"] and out["deleted"]["drafts"] >= 1
    st = api.status()
    assert st["capture"]["drafts"] == 0 and st["capture"]["edits"] == {} and st["total_units"] == 1


def test_lint_returns_draft_id_and_respects_is_draft(home):
    # chat.hope_helps is still a "strong" tell (R3 demoted let_me_know/certainly/absolutely_right, which
    # are common enough in ordinary agreement that they used to fire noisily on too little data).
    a = api.lint("I hope this helps with the plan for Friday", "work")
    b = api.lint("I hope this helps with the plan for Friday", "work")
    assert a["logged"] and a["draft_id"] == b["draft_id"] and "chat.hope_helps" in {f["code"] for f in a["findings"]}
    assert a["calibrated"] is False and "uncalibrated" in a["note"] and a["n"] == 0
    c = api.lint("something else entirely different from the other one", "work", is_draft=False)
    assert not c["logged"] and c["draft_id"] is None
    assert not api.lint("x", "nope")["ok"] and not api.context("nope")["ok"]
    assert api.feedback("Hi, can you look at this soon please", "can you look at this", "work")["status"] == "edited"


def test_everything_on_disk_is_private(fakes):
    api.setup()
    pk = api.work_next("blind")["packet"]
    assert pk
    for root, dirs, files in os.walk(store.db_path().parent):
        for d in dirs:
            assert stat.S_IMODE(os.stat(os.path.join(root, d)).st_mode) == 0o700, d
        for f in files:
            assert stat.S_IMODE(os.stat(os.path.join(root, f)).st_mode) == 0o600, f


def test_public_context_only_shows_published_text(fakes):
    api.setup()
    ctx = api.context("public", k=6)
    assert ctx["ok"] and ctx["examples"] and all(e["text"].startswith("Shipped") for e in ctx["examples"])
    assert "Let me know" not in ctx["text"]
    st = api.status()
    assert st["registers"]["public"]["units"] == 14
    assert all(isinstance(v["state"], str) for v in st["registers"].values())
