import time

from mannerism import lint, stats, tells


def fake_stats(n=100, **over):
    base = {"register": "close", "n": n, "tells_n": n,
            "numeric": {k: {"p5": 2, "p10": 3, "median": 8, "p90": 16, "p95": 20, "mean": 9, "std": 5}
                        for k in stats.NUMERIC},
            "rates": {k: 0.0 for k in stats.RATES}, "tells": {t.id: 0.0 for t in tells.TELLS},
            "tell_counts": {t.id: 0 for t in tells.TELLS}}
    base["rates"].update(starts_lower=0.9, all_lower=0.8, ends_with_punct=0.1)
    base.update(over)
    return base


def codes(findings):
    return {f.code for f in findings}


def test_tell_list_shape():
    assert len(tells.TELLS) >= 60 and len({t.id for t in tells.TELLS}) == len(tells.TELLS)
    assert {t.origin for t in tells.TELLS} == {"humanizer", "slop-forensics", "original"}
    assert all(t.why and t.label for t in tells.TELLS)
    found = tells.count_all("It's worth noting that, not just fast but also cheap, we delve. I hope this helps!")
    assert {"phrase.worth_noting", "phrase.not_just_but", "vocab.delve", "chat.hope_helps"} <= set(found)


def test_tell_is_suppressed_only_when_yours():
    text = "we should delve into this"
    base = fake_stats()
    assert "vocab.delve" in codes(lint.lint(text, "close", stats=base, threshold=0.02))
    mine = fake_stats(tells={**base["tells"], "vocab.delve": 0.05}, tell_counts={**base["tell_counts"], "vocab.delve": 5})
    assert "vocab.delve" not in codes(lint.lint(text, "close", stats=mine, threshold=0.02))
    # 2% of units but fewer than 3 units: not yet "yours"
    few = fake_stats(n=100, tells={**base["tells"], "vocab.delve": 0.02}, tell_counts={**base["tell_counts"], "vocab.delve": 2})
    assert "vocab.delve" in codes(lint.lint(text, "close", stats=few, threshold=0.02))


def test_uncalibrated_runs_only_universal_tells():
    small = fake_stats(n=12)
    out = lint.lint("Let's delve in — this is a very long sentence that goes on and on and on and on and on", "close", stats=small)
    assert codes(out) == {"vocab.delve"}  # em dash is not universal; no range checks
    rep = lint.report("delve", "close", stats=small)
    assert not rep["calibrated"] and "uncalibrated" in rep["note"] and rep["n"] == 12
    assert "uncalibrated" in lint.report("hi", "close", stats=None)["note"]


def test_range_checks_and_short_drafts():
    st = fake_stats()
    long_text = "Here is a long and rather formal sentence that rambles on far beyond what you usually write in chat. " * 3
    out = lint.lint(long_text, "close", stats=st)
    assert {"range.words", "range.casing", "range.punct"} <= codes(out)
    short = lint.lint("Ok.", "close", stats=st)
    assert "range.punct" not in codes(short) and "range.sentence" not in codes(short)
    assert "range.casing" in codes(short)  # casing is still checked for short drafts


def test_wording_is_advisory():
    out = lint.lint("I hope this helps", "close", stats=fake_stats())
    assert out
    assert all(not f.message.lower().startswith(("this is ai", "ai tell")) for f in out)
    assert "no suggestions" in lint.format_findings([])


def test_report_caps_pathological_input_and_stays_fast():
    """P2-10: a huge paste must not make the regex passes in lint()/report() slow -- report() caps the
    text at LINT_CHAR_CAP before any of them run and says so."""
    huge = ("The quick brown fox jumps over the lazy dog, and then writes a rather long and winding "
            "sentence about absolutely nothing in particular, over and over. ") * 700  # ~ 70 KB
    assert len(huge) > 40_000
    t0 = time.monotonic()
    rep = lint.report(huge, "close", stats=fake_stats())
    assert time.monotonic() - t0 < 0.5
    assert rep["note"].endswith("the draft is longer)")
    assert all(f for f in rep["findings"])  # still produces ordinary findings, just bounded input


def test_public_leak_check(home):
    stats.stats_dir().mkdir(parents=True, exist_ok=True)
    (stats.stats_dir() / "leak.json").write_text('{"entities": ["@sam.dev", "Rosa Delgado", "private.example/plan"]}')
    out = lint.lint("Thanks @sam.dev and Rosa Delgado, see https://private.example/plan or mail jo@example.org",
                    "public", stats=fake_stats(register="public"))
    leaks = [f.message for f in out if f.code == "leak.entity"]
    assert len(leaks) == 3 and any(f.code == "leak.email" for f in out)
    assert not any(f.code.startswith("leak") for f in lint.lint("Thanks Rosa Delgado", "work", stats=fake_stats()))
