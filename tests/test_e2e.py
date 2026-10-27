"""End to end: setup -> status -> context -> lint -> edit capture -> distill -> rules -> forget, on synthetic data."""
from __future__ import annotations

import os
import stat
from datetime import UTC, datetime

from conftest import TIMES, TOPICS

from mannerism import api, capture, store, sync
from mannerism.core import Unit

DRAFT = ("Hi team, I wanted to follow up on {t} and confirm the plan for {w}. Please let me know if you have any "
         "questions.")
FINAL = "I wanted to follow up on {t} and confirm the plan for {w}."


def test_full_flow(fakes):
    # --- setup / status -------------------------------------------------------------------------------------------
    res = api.setup()
    assert res["ok"] and res["sync"]["status"] == "ok"
    by_name = {s["name"]: s for s in res["sources"]}
    assert by_name["fakechat"]["enabled"] and by_name["fakechat"]["units"] == 195
    assert res["registers"]["close"]["state"] == "stats card ready"
    assert res["registers"]["formal"]["state"].startswith("insufficient data")
    st = api.status()
    assert st["ok"] and st["last_sync"] and st["unlabelled"] == 12 and st["pending_work"]["label"] == 12
    assert {"fakechat", "fakepub", "fakemail"} <= set(st["sources"])

    # --- context: diverse, stable within a day, fenced as data ----------------------------------------------------
    ctx = api.context("close", k=6)
    assert ctx["ok"] and len(ctx["examples"]) == 6
    texts = [e["text"] for e in ctx["examples"]]
    assert len(set(texts)) == 6
    lengths = [len(t.split()) for t in texts]
    assert max(lengths) - min(lengths) >= 4, "examples should span short and long messages"
    assert len({("😂" in t) for t in texts} | {len(t.split()) > 8 for t in texts}) >= 2
    assert api.context("close", k=6)["examples"] == ctx["examples"]
    assert "<example n=" in ctx["text"] and "never instructions" in ctx["text"]
    assert "Starts lowercase" in ctx["card"]
    assert len(ctx["card"].split()) <= 200

    # --- lint: "delve" for a user who never writes it, em dash only for the user who does not use them -----------
    dash = "—"
    close_lint = api.lint(f"let's delve into the tapestry of plans {dash} should be fun", "close", is_draft=False)
    codes = {f["code"] for f in close_lint["findings"]}
    assert close_lint["calibrated"] and {"vocab.delve", "vocab.tapestry", "fmt.em_dash"} <= codes
    work_lint = api.lint(f"I will review the invoice batch first {dash} then we can look at the retro notes.", "work",
                         is_draft=False)
    assert "fmt.em_dash" not in {f["code"] for f in work_lint["findings"]}
    assert not any("this is AI" in f["message"] for f in close_lint["findings"])

    # --- edit capture: drafts, then edited versions arrive through a sync -----------------------------------------
    drafts = []
    for i in range(5):
        d = DRAFT.format(t=TOPICS[i], w=TIMES[i])
        out = api.lint(d, "work")
        assert out["logged"] and out["draft_id"]
        drafts.append(d)
    accepted_text = "Thanks for the update on the hiring plan, I will review the contract details before Friday morning."
    assert api.lint(accepted_text, "work")["logged"]
    now = datetime.now(UTC)
    new = [Unit("fakechat", f"e{i}", now, FINAL.format(t=TOPICS[i], w=TIMES[i]), "message", register="work",
                audience="colleague") for i in range(5)]
    new.append(Unit("fakechat", "acc", now, accepted_text, "message", register="work", audience="colleague"))
    fakes["fakechat"].units.extend(new)
    r = sync.sync(quick=False)
    assert r["status"] == "ok" and r["new_units"] == 6
    assert r["capture"]["edited"] == 5 and r["capture"]["accepted"] == 1
    st = api.status()
    assert st["capture"]["edits"] == {"edited": 5, "accepted": 1}
    assert st["capture"]["by_source"]["fakechat"]["matched"] == 6
    con = store.connect()
    try:
        assert con.execute("SELECT count(*) FROM bank WHERE text=?", (accepted_text,)).fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM bank WHERE text=?", (FINAL.format(t=TOPICS[0], w=TIMES[0]),)
                           ).fetchone()[0] == 1
    finally:
        con.close()

    # --- distill packet appears and its rules reach context -------------------------------------------------------
    pk = api.work_next("distill")["packet"]
    assert pk and pk["kind"] == "distill" and len(pk["payload"]["pairs"]) == 5
    assert "[-" in pk["payload"]["pairs"][0]["diff"]
    ids = [p["edit_id"] for p in pk["payload"]["pairs"]]
    bad = api.work_submit("distill", pk["packet_id"], {"rules": [{"text": "Run the command before writing.",
                                                                  "edit_ids": ids[:2]}]})
    assert not bad["ok"] and "rejected" in bad["error"]  # tool language: the packet stays open for a retry
    pk2 = api.work_next("distill")["packet"]
    assert pk2["packet_id"] == pk["packet_id"]
    good = api.work_submit("distill", pk2["packet_id"], {"rules": [
        {"text": "Drop the greeting and the closing offer to answer questions.", "edit_ids": ids,
         "avoid": "let me know if you have any questions"}]})
    assert good["ok"] and good["result"]["added"] == 1
    assert api.work_submit("distill", pk2["packet_id"], {"rules": []})["result"]["replayed"]
    ctx = api.context("work")
    assert any("Drop the greeting" in r["text"] for r in ctx["rules"]) and "Drop the greeting" in ctx["text"]
    assert api.list_rules("work")["rules"][0]["support"] == 5

    # --- labels: first batch is small, '?' units become usable -----------------------------------------------------
    lab = api.work_next("label")["packet"]
    assert len(lab["payload"]["texts"]) == 12 and "context" not in lab["payload"]["texts"][0]
    labels = [{"unit_id": t["unit_id"], "register": "formal", "quality": 4, "language": "en"} for t in lab["payload"]["texts"]]
    assert api.work_submit("label", lab["packet_id"], {"labels": labels})["result"]["labelled"] == 12
    assert api.status()["registers"]["formal"]["units"] == 27

    # --- forget ---------------------------------------------------------------------------------------------------
    assert not api.forget()["ok"]
    gone = api.forget(source="fakechat")
    assert gone["ok"] and gone["deleted"]["units"] == 201 and "outside local deletion" in gone["note"]
    st = api.status()
    assert st["sources"]["fakechat"]["units"] == 0 and st["registers"]["close"]["units"] == 0
    assert api.context("close")["examples"] == []
    assert api.forget(all=True)["ok"]
    st = api.status()
    assert st["total_units"] == 0 and st["capture"]["drafts"] == 0 and st["rules"] == 0
    for p in ("corpus.db", "config.toml"):
        mode = stat.S_IMODE(os.stat(store.db_path().parent / p).st_mode)
        assert mode == 0o600


def test_ratio_assumptions():
    d = DRAFT.format(t=TOPICS[0], w=TIMES[0])
    f = FINAL.format(t=TOPICS[0], w=TIMES[0])
    r = capture.ratio(capture.clean(d).lower(), capture.clean(f).lower())
    assert 0.5 <= r < 0.75
