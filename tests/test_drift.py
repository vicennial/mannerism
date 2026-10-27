from conftest import ingest, unit

from mannerism import drift, packets, registers, store


def test_drift_flags_card_refresh_and_acknowledge(home):
    con = store.connect()
    old = [unit(f"ok sounds good see you then number {i}", "close", ref=f"o{i}", days=60 + i * 2, thread_id=f"o{i}")
           for i in range(70)]
    recent = [unit(f"Dear colleague, I would like to formally confirm arrangement {i}, "
                   f"as discussed previously in our long correspondence.", "close", ref=f"r{i}", days=1 + i % 25,
                   thread_id=f"r{i}") for i in range(30)]
    ingest(con, old + recent)
    d = drift.drift_score(con, "close")
    assert d and d["score"] > drift.THRESHOLD and d["recent_n"] >= 15
    assert "close" in drift.flagged(con)  # refresh() flagged it
    assert "close" in packets.card_candidates(con)
    assert packets.queue_work(con) and packets.next_packet(con=con)["kind"] == "card"
    drift.acknowledge(con, "close")
    assert drift.check_drift(con, ["close"]) == {} and "close" not in drift.flagged(con)


def test_drift_needs_enough_data(home):
    con = store.connect()
    ingest(con, [unit(f"ok message number {i} here", "close", ref=f"o{i}", days=i + 1) for i in range(20)])
    assert drift.drift_score(con, "close") is None


def test_label_queue_is_newest_first_and_context_free(home):
    con = store.connect()
    ingest(con, [unit(f"unknown note {i} about something", "?", ref=f"q{i}", days=i + 1) for i in range(5)])
    q = registers.label_queue(con, 3)
    assert [r["text"] for r in q] == [f"unknown note {i} about something" for i in range(3)]
    assert all("context" not in r and "context_json" not in r for r in q)
    assert registers.unlabelled_count(con) == 5
