"""Lint a draft against the user's own statistics and a list of common AI tells.

Findings are advisory wording for one focused revision ("consider ..."), never a verdict that text is AI. Three groups:

1. **Tells** (`tells.py`), calibrated: a tell is "yours", and so suppressed, when it appears in at least
   `tell_threshold` (default 2%) of your units in the register AND in at least 3 units. Someone who writes
   em dashes is never nagged about them. With fewer than MIN_CALIBRATION (30) units in the register the
   result says "uncalibrated": only universal tells (`strong`: "As an AI", "delve") fire and no personal
   range checks run.
2. **Range**: length, sentence length, casing, punctuation and comma density outside the user's p5..p95.
   Drafts under SHORT_DRAFT_WORDS words skip the sentence-length and punctuation checks.
3. **Public leak** (register `public` only): handles, URLs and capitalised names that occur in the user's
   private text but in nothing they ever published, and any email address.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from . import config as config_mod
from . import stats as stats_mod
from . import tells
from .stylometrics import features

MIN_CALIBRATION = 30
MIN_TELL_UNITS = 3  # a tell is "yours" only if it appears in this many units as well as in >= threshold of them
SHORT_DRAFT_WORDS = 8
LINT_CHAR_CAP = 20_000  # bound the regex work below on a pathologically large draft
_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+")


@dataclass
class Finding:
    code: str
    severity: str  # "warn" | "info"
    message: str
    why: str

    def to_dict(self) -> dict:
        return asdict(self)


def _pct(x: float) -> str:
    return f"{round(100 * x)}%"


def _tell_findings(text: str, register: str, stats: dict | None, threshold: float) -> list[Finding]:
    calibrated = is_calibrated(stats)
    out = []
    for tid, count in tells.count_all(text).items():
        t = tells.BY_ID[tid]
        if calibrated:
            rate = stats["tells"].get(tid, 0.0)
            units = (stats.get("tell_counts") or {}).get(tid, round(rate * stats.get("tells_n", 0)))
            if rate >= threshold and units >= MIN_TELL_UNITS:
                continue  # the user writes this themselves, so it is not a tell for them
            note = (f"You use this in {_pct(rate)} of your {register} messages."
                    if rate else f"You do not use this in your {register} messages.")
        elif t.strong:
            note = "Uncalibrated (too little of your own writing yet); flagged because people rarely write this."
        else:
            continue
        times = f" x{count}" if count > 1 else ""
        out.append(Finding(tid, "warn" if t.strong else "info", f"Consider rewording: {t.label}{times}",
                           f"{t.why} Often seen in AI-generated text. {note}"))
    return out


def is_calibrated(stats: dict | None) -> bool:
    return bool(stats) and stats.get("n", 0) >= MIN_CALIBRATION and stats.get("tells_n", 0) >= MIN_CALIBRATION


def _range(code: str, message: str, why: str) -> Finding:
    return Finding(code, "info", message, why)


def _range_findings(text: str, register: str, stats: dict) -> list[Finding]:
    num, rates = stats["numeric"], stats["rates"]
    f = features(text)
    out: list[Finding] = []
    long_form = register == "public" and f["words"] > 120  # articles: judged per paragraph, not by length
    short = f["words"] < SHORT_DRAFT_WORDS  # sentence and punctuation statistics mean little for a few words
    checks = [] if short else [("words_per_sentence", "range.sentence", "words per sentence", f["words_per_sentence"])]
    if not long_form:
        checks.append(("words", "range.words", "length (words)", f["words"]))
    for key, code, label, val in checks:
        d = num[key]
        if val > d["p95"] and val - d["p95"] >= 2:
            out.append(_range(code, f"{label.capitalize()} is {val:g}, above your usual range",
                              f"In {register} your {label} runs {d['p5']:g} to {d['p95']:g} (median {d['median']:g})."))
        elif val < d["p5"] and d["p5"] - val >= 2:
            out.append(_range(code, f"{label.capitalize()} is {val:g}, below your usual range",
                              f"In {register} your {label} runs {d['p5']:g} to {d['p95']:g} (median {d['median']:g})."))
    if f["words"] >= 15 and f["comma_per100w"] > num["comma_per100w"]["p95"] + 2:
        out.append(_range("range.comma", f"More commas than usual ({f['comma_per100w']:g} per 100 words)",
                          f"Your 95th percentile is {num['comma_per100w']['p95']:g}."))
    if rates["starts_lower"] >= 0.7 and not f["starts_lower"]:
        out.append(_range("range.casing", "Starts with a capital letter",
                          f"You start lowercase in {_pct(rates['starts_lower'])} of {register} messages."))
    elif rates["starts_lower"] <= 0.1 and f["starts_lower"]:
        out.append(_range("range.casing", "Starts lowercase",
                          f"You start with a capital in {_pct(1 - rates['starts_lower'])} of {register} messages."))
    if rates["all_lower"] >= 0.5 and not f["all_lower"] and f["upper_ratio"] > 0.03:
        out.append(_range("range.casing", "Uses capital letters inside the text",
                          f"You write entirely in lowercase in {_pct(rates['all_lower'])} of {register} messages."))
    if short:
        return out
    if rates["ends_with_punct"] <= 0.3 and f["ends_with_punct"]:
        out.append(_range("range.punct", "Ends with final punctuation",
                          f"Only {_pct(rates['ends_with_punct'])} of your {register} messages end with it."))
    elif rates["ends_with_punct"] >= 0.8 and not f["ends_with_punct"] and f["words"] >= 6:
        out.append(_range("range.punct", "No final punctuation",
                          f"{_pct(rates['ends_with_punct'])} of your {register} messages end with it."))
    if rates["exclaim"] < 0.1 and f["exclaim"] >= 2:
        out.append(_range("range.exclaim", f"{f['exclaim']} exclamation marks",
                          f"You use any in {_pct(rates['exclaim'])} of your {register} messages."))
    if rates["emoji"] < 0.05 and f["emoji"]:
        out.append(_range("range.emoji", "Uses emoji", f"You use emoji in {_pct(rates['emoji'])} of {register} messages."))
    return out


def _leak_findings(text: str) -> list[Finding]:
    out = []
    leak = stats_mod.load_leak_index()
    for ent in sorted(e for e in stats_mod.entities(text) if e in leak)[:10]:
        out.append(Finding("leak.entity", "warn", f"'{ent}' may be private",
                           "It appears in your private writing but in nothing you have published."))
    if _EMAIL.search(text):
        out.append(Finding("leak.email", "warn", "Contains an email address",
                           "Email addresses are masked in your stored text, so they cannot be checked against "
                           "what you have published. Confirm you mean to publish it."))
    return out


def _capped(text: str) -> tuple[str, bool]:
    """`text`, truncated to LINT_CHAR_CAP so a huge paste cannot make every regex pass below slow."""
    if len(text) <= LINT_CHAR_CAP:
        return text, False
    return text[:LINT_CHAR_CAP], True


def lint(text: str, register: str, *, stats: dict | None = None, threshold: float | None = None) -> list[Finding]:
    """Findings for `text` as a draft in `register` (see the module docstring)."""
    text, _ = _capped(text)
    stats = stats if stats is not None else stats_mod.load_stats(register)
    if threshold is None:
        threshold = config_mod.load_config().tell_threshold
    out = _tell_findings(text, register, stats, threshold)
    if is_calibrated(stats):
        out += _range_findings(text, register, stats)
    if register == "public":
        out += _leak_findings(text)
    return out


def report(text: str, register: str, *, stats: dict | None = None, threshold: float | None = None) -> dict:
    """lint() plus how well calibrated it was: {"findings", "n", "calibrated", "note"}."""
    text, truncated = _capped(text)
    stats = stats if stats is not None else stats_mod.load_stats(register)
    n = (stats or {}).get("n", 0)
    calibrated = is_calibrated(stats)
    note = (f"calibrated on {n} of your {register} messages" if calibrated else
            f"uncalibrated: only {n} {register} messages so far (need {MIN_CALIBRATION}); showing universal tells only")
    if truncated:
        note += f" (only the first {LINT_CHAR_CAP} characters were checked; the draft is longer)"
    return {"findings": [f.to_dict() for f in lint(text, register, stats=stats, threshold=threshold)],
            "n": n, "calibrated": calibrated, "note": note}


def format_findings(findings: list[Finding]) -> str:
    if not findings:
        return "no suggestions: no common AI-style phrasing, range problems or leaks found"
    return "\n".join(f"- [{f.severity}] {f.message}. {f.why}" for f in findings)
