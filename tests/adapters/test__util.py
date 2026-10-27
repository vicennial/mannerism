from mannerism.adapters._util import read_new_lines


def test_read_new_lines_respects_a_byte_budget_and_resumes_cleanly(tmp_path):
    """P2-11: a transcript that grew huge between syncs must not be read into memory in one call. A
    budget-limited read still lands on a complete line, and the next call (same budget) picks up
    exactly where the first left off, eventually catching up to the real end of the file."""
    p = tmp_path / "big.jsonl"
    lines = [f'{{"i": {i}, "text": "line number {i} with some padding to take up space"}}' for i in range(2000)]
    p.write_text("\n".join(lines) + "\n")
    full_size = p.stat().st_size
    assert full_size > 20_000

    budget = 10_000  # deliberately small, to force more than one call
    seen: list[str] = []
    cursor = None
    calls = 0
    while True:
        calls += 1
        got, cursor = read_new_lines(p, cursor, budget=budget)
        seen += got
        if cursor["offset"] >= full_size:
            break
        assert calls < 50  # sanity: must actually be making progress, not stuck
    assert calls > 1  # the budget really did force multiple reads
    assert seen == lines
    assert cursor["offset"] == full_size

    # a further call (file unchanged) returns nothing new
    more, cursor2 = read_new_lines(p, cursor, budget=budget)
    assert more == [] and cursor2["offset"] == full_size
