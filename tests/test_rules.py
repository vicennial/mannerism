import pytest

from mannerism import capture, packets, rules, store


def add_pairs(con, n, threads, register="work", tag="a"):
    ids = []
    for i in range(n):
        eid, _, _ = capture.add_edit(con, register, f"Hi team, please review item {tag}{i} soon. Thanks so much!",
                                     f"please review item {tag}{i} soon", "feedback", thread_key=f"t{i % threads}")
        ids.append(eid)
    return ids


def packet(con, register="work"):
    spec = rules.distill_packet(con, register)
    return packets._store(con, spec)


def test_distill_needs_five_pairs_from_three_threads(home):
    con = store.connect()
    add_pairs(con, 4, 4)
    assert rules.distill_packet(con, "work") is None and rules.distill_registers(con) == []
    add_pairs(con, 5, 2)  # five pairs but a single noisy conversation pair
    con.execute("DELETE FROM edits")
    add_pairs(con, 6, 2)
    assert rules.distill_packet(con, "work") is None  # 6 pairs, 2 threads
    con.execute("DELETE FROM edits")
    ids = add_pairs(con, 6, 3)
    spec = rules.distill_packet(con, "work")
    assert spec and len(spec["payload"]["pairs"]) == 6 and set(spec["schema"]["properties"]) == {"rules"}
    assert [p["edit_id"] for p in spec["payload"]["pairs"]] == sorted(ids, reverse=True)
    assert "[-Hi" in spec["payload"]["pairs"][0]["diff"] or "[-" in spec["payload"]["pairs"][0]["diff"]


def test_submit_merges_rejects_and_marks_distilled(home):
    con = store.connect()
    ids = add_pairs(con, 6, 3)
    pk = packet(con)
    ok = rules.submit_rules(con, pk["packet_id"], {"rules": [
        {"text": "Drop greetings and thank-you closers.", "edit_ids": ids[:4], "avoid": "thanks so much"},
        {"text": "see https://example.org for style", "edit_ids": ids[:1]},
        {"text": "Run the linter on every draft first.", "edit_ids": ids[:1]},
        {"text": "x" * 150 + " ```code```", "edit_ids": ids[:1]},
    ]})
    assert ok["added"] == 1 and len(ok["rejected"]) == 3
    assert con.execute("SELECT count(*) FROM edits WHERE distilled=0").fetchone()[0] == 0
    row = con.execute("SELECT support FROM rules").fetchone()
    assert row["support"] == 4
    # second round: same meaning merges and support is the number of unique cited edits
    ids2 = add_pairs(con, 6, 3, tag="b")
    pk2 = packet(con)
    out = rules.submit_rules(con, pk2["packet_id"], {"rules": [
        {"text": "  drop greetings and thank-you closers  ", "edit_ids": ids2[:2]},
        {"text": "Drop greetings and thank you closers!", "edit_ids": ids2[2:4]}]})
    assert out["merged"] == 2 and out["added"] == 0
    assert con.execute("SELECT support FROM rules").fetchone()["support"] == 8


def test_edit_ids_must_come_from_the_packet(home):
    con = store.connect()
    add_pairs(con, 6, 3)
    pk = packet(con)
    with pytest.raises(ValueError, match="not in this packet"):
        rules.submit_rules(con, pk["packet_id"], {"rules": [{"text": "Keep it short and plain.", "edit_ids": [9999]}]})
    assert con.execute("SELECT count(*) FROM edits WHERE distilled=1").fetchone()[0] == 0
    with pytest.raises(ValueError):  # over-long text violates the schema
        rules.submit_rules(con, pk["packet_id"], {"rules": [{"text": "x" * 201, "edit_ids": [1]}]})


def test_contradicted_rules_are_hidden_and_listable_and_deletable(home):
    con = store.connect()
    ids = add_pairs(con, 6, 3)
    pk = packet(con)
    rules.submit_rules(con, pk["packet_id"], {"rules": [
        {"text": "Never write the word leverage.", "edit_ids": ids[:1], "avoid": "leverage"},
        {"text": "Prefer short sentences always.", "edit_ids": ids[:3]}]})
    assert [r["text"] for r in rules.top_rules("work", con=con)] == ["Prefer short sentences always.",
                                                                    "Never write the word leverage."]
    capture.add_edit(con, "work", "we could use it a lot here", "we should leverage it here", "feedback")
    assert [r["text"] for r in rules.top_rules("work", con=con)] == ["Prefer short sentences always."]
    listed = rules.list_rules(con, "work")
    assert {r["text"]: r["hidden"] for r in listed}["Never write the word leverage."] is True
    assert rules.delete_rule(con, listed[0]["rule_id"]) and not rules.delete_rule(con, 9999)
    assert rules.top_rules("work", con=con) == []
