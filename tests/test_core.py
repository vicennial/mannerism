import time
from datetime import UTC, datetime

from mannerism.core import Unit, looks_pasted, mask_pii, merge_bursts


def test_mask_pii_secrets_and_contacts():
    text = ("mail ana.ruiz@example.org or call +44 20 7946 0958, key sk-abcdefghijklmnopqrstuv, "
            "card 4111 1111 1111 1111, https://x.example/cb?token=abc123&x=1 on 2026-10-01")
    out = mask_pii(text)
    assert "[email]" in out and "[phone]" in out and "[secret]" in out and "[card]" in out
    assert "abc123" not in out and "2026-10-01" in out
    assert mask_pii(out) == out


def test_merge_bursts_and_unit_validate():
    t = lambda s: datetime(2026, 1, 1, 12, 0, s, tzinfo=UTC)  # noqa: E731
    out = list(merge_bursts([(True, t(0), "a", "hi"), (True, t(5), "b", "there"), (False, t(9), "c", "yo"),
                             (True, t(20), "d", "ok")]))
    assert [o[1] for o in out] == ["hi\nthere", "ok"]
    Unit("git", "abc", t(0), "fix the parser", "commit", register="work").validate()


def test_mask_pii_stays_fast_on_a_large_paste():
    """P2-10: the email/secret/token regexes are bounded (no unbounded `+`), so a 40 KB paste masks in
    well under a second instead of risking catastrophic backtracking."""
    line = "contact ana.ruiz@example.org re the plan, token sk-abcdefghijklmnopqrstuv, see you then. "
    big = line * (40_000 // len(line) + 1)
    assert len(big) > 40_000
    t0 = time.monotonic()
    out = mask_pii(big)
    assert time.monotonic() - t0 < 0.5
    assert "[email]" in out and "ana.ruiz@example.org" not in out


def test_looks_pasted():
    assert looks_pasted("```\ncode\n```\n" + "x\n" * 20)
    assert not looks_pasted("can you make the button blue")


def test_mask_pii_is_linear_on_one_long_token():
    """A long unbroken base64url-like token (the shape of a pasted secret) must not trigger regex backtracking."""
    import random
    import string
    import time

    rng = random.Random(1)
    blob = "".join(rng.choice(string.ascii_letters + string.digits + "-_") for _ in range(120_000))
    t = time.perf_counter()
    mask_pii(blob)
    assert time.perf_counter() - t < 1.0
