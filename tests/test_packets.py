import json
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ingest, unit

from mannerism import packets, store, validate


def make_unlabelled(con, n, **kw):
    return ingest(con, [unit(f"quick note number {i} about the plan for {i + 3} people", "?", ref=f"q{i}",
                             days=i + 1, **kw) for i in range(n)])


def test_validator_subset():
    schema = {"type": "object", "required": ["a"], "additionalProperties": False, "properties": {
        "a": {"type": "integer", "minimum": 1, "maximum": 5}, "b": {"type": "array", "maxItems": 2,
              "items": {"type": "string", "enum": ["x", "y"]}}}}
    assert validate.validate({"a": 3, "b": ["x"]}, schema) == []
    errs = validate.validate({"a": 9, "b": ["z", "x", "y"], "c": 1}, schema)
    assert len(errs) == 4
    assert validate.validate({"a": True}, schema)  # bool is not an integer
    assert validate.validate([], schema)


def test_first_label_batch_is_small_and_context_free(home):
    con = store.connect()
    make_unlabelled(con, 70)
    with store.tx(con):
        con.execute("UPDATE units SET context_json='[{\"who\": \"other1\", \"text\": \"secret reply\"}]'")
    pk = packets.next_packet("label", con=con)
    assert len(pk["payload"]["texts"]) == packets.LABEL_FIRST == 40
    assert "secret reply" not in json.dumps(pk) and "context" not in pk["payload"]["texts"][0]
    assert pk["payload"]["texts"][0]["text"].startswith("quick note number 0")  # newest first
    assert pk["expires_at"] > pk["created_at"] and "data" in pk["data_notice"]
    assert packets.next_packet("label", con=con)["packet_id"] == pk["packet_id"]  # still valid: handed out again


def test_label_submit_rules(home):
    con = store.connect()
    make_unlabelled(con, 10)
    pk = packets.next_packet("label", con=con)
    ids = [t["unit_id"] for t in pk["payload"]["texts"]]
    with pytest.raises(packets.PacketError, match="not in this packet"):
        packets.submit("label", pk["packet_id"], {"labels": [{"unit_id": "nope", "register": "close", "quality": 3,
                                                               "language": "en"}]}, con=con)
    with pytest.raises(packets.PacketError, match="schema"):
        packets.submit("label", pk["packet_id"], {"labels": [{"unit_id": ids[0], "register": "weird", "quality": 3,
                                                               "language": "en"}]}, con=con)
    res = packets.submit("label", pk["packet_id"], {"labels": [
        {"unit_id": ids[0], "register": "public", "quality": 5, "language": "en"},   # private text can never be public
        {"unit_id": ids[1], "register": "work", "quality": 4, "language": "en"},
        {"unit_id": ids[2], "register": "skip", "quality": 1, "language": "en"}]}, con=con)
    assert res["labelled"] == 2 and res["rejected"][0]["unit_id"] == ids[0] and res["remaining"] == 8
    assert con.execute("SELECT count(*) FROM bank WHERE register='work'").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM bank WHERE register='public'").fetchone()[0] == 0


def test_submit_is_idempotent_and_rejects_expired_and_invalidated(home):
    con = store.connect()
    make_unlabelled(con, 5)
    pk = packets.next_packet("label", con=con)
    first = packets.submit("label", pk["packet_id"], {"labels": []}, con=con)
    again = packets.submit("label", pk["packet_id"], {"labels": [{"unit_id": "zzz", "register": "close",
                                                                   "quality": 3, "language": "en"}]}, con=con)
    assert again["replayed"] and again["labelled"] == first["labelled"]
    pk2 = packets.next_packet("label", con=con)
    with store.tx(con):
        con.execute("UPDATE packets SET expires_at=? WHERE packet_id=?",
                    (store.ts_iso(datetime.now(UTC) - timedelta(minutes=1)), pk2["packet_id"]))
    with pytest.raises(packets.PacketError, match="expired"):
        packets.submit("label", pk2["packet_id"], {"labels": []}, con=con)
    pk3 = packets.next_packet("label", con=con)
    assert pk3["packet_id"] != pk2["packet_id"]
    with store.tx(con):
        con.execute("UPDATE packets SET invalidated=1 WHERE packet_id=?", (pk3["packet_id"],))
    with pytest.raises(packets.PacketError, match="stale"):
        packets.submit("label", pk3["packet_id"], {"labels": []}, con=con)
    with pytest.raises(packets.PacketError, match="unknown packet"):
        packets.submit("label", "pk_nope", {"labels": []}, con=con)
    with pytest.raises(packets.PacketError, match="not 'card'"):
        packets.submit("card", packets.next_packet("label", con=con)["packet_id"], {}, con=con)


def test_card_packet_flow_and_staleness(home):
    con = store.connect()
    ingest(con, [unit(f"message {i} about stuff with some varied words {'x' * (i % 9)}", "close", ref=f"c{i}",
                      days=i + 1, thread_id=f"t{i}") for i in range(35)])
    pk = packets.next_packet(con=con)
    assert pk["kind"] == "card" and pk["register"] == "close"
    text = "Writes short plain messages in lowercase, rarely ends them with punctuation and keeps greetings out. " * 2
    ingest(con, [unit("a brand new message arrives here today", "close", ref="newest")])  # bank changes
    with pytest.raises(packets.PacketError, match="changed"):
        packets.submit("card", pk["packet_id"], {"register": "close", "card": text}, con=con)
    pk = packets.next_packet("card", con=con)
    with pytest.raises(packets.PacketError, match="rejected"):
        packets.submit("card", pk["packet_id"], {"register": "close", "card": text + " mail jo@example.org"}, con=con)
    out = packets.submit("card", pk["packet_id"], {"register": "close", "card": text}, con=con)
    assert out["saved"] and packets.next_packet("card", con=con) is None


def test_distill_packet_is_offered_first(home):
    con = store.connect()
    make_unlabelled(con, 3)
    from mannerism import capture
    for i in range(5):
        capture.add_edit(con, "work", f"Hi, please look at {i}. Thanks!", f"please look at {i}", "feedback",
                         thread_key=f"t{i}")
    assert packets.next_packet(con=con)["kind"] == "distill"
    assert packets.pending_summary(con)["distill"] == ["work"] and packets.queue_work(con) == []
