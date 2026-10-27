"""Deterministic per-unit style features. No models, no network.

Features are cheap surface measures that a style card can quote as frequencies ("starts lowercase
82% of the time") and that lint and retrieval compare. `vector()` turns the numeric ones into a
small normalised vector so retrieval can pick examples that span the register's range.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import unicodedata

from .store import tx

FEATURE_VERSION = 1
FEATURE_BATCH = 500  # rows per write transaction, so a big backlog does not hold the write lock in one go

_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
_SENT_END = re.compile(r"[.!?。！？]+(?:\s|$)")
_URL = re.compile(r"https?://\S+")
_LAUGH = re.compile(r"(?i)\b(?:ha(?:ha)+h?|he(?:he)+|lol+|lmao+|rofl)\b|[😂🤣]")
_SHORTHAND = re.compile(r"(?i)\b(?:u|ur|pls|plz|btw|tbh|idk|imo|imho|ngl|rn|w/|thx|ty|np|omw|gonna|wanna|gotta|kinda|"
                        r"sorta|ya|yeah|yep|nope|ok|okay|k)\b")
_CONTRACTION = re.compile(r"(?i)\b\w+['’](?:s|t|re|ve|ll|d|m)\b")
_HEDGE = re.compile(r"(?i)\b(?:i think|i guess|maybe|probably|kinda|sort of|i feel like|not sure)\b")
_END_PUNCT = {".", "!", "?", "。", "！", "？"}


def _is_emoji(ch: str) -> bool:
    cp = ord(ch)
    return (0x1F300 <= cp <= 0x1FAFF) or (0x2600 <= cp <= 0x27BF) or (0x1F1E6 <= cp <= 0x1F1FF)


def script_mix(text: str) -> dict[str, float]:
    """Share of letters per Unicode script, keyed by the first word of the character name
    ("LATIN SMALL LETTER A" -> "latin", "CJK UNIFIED IDEOGRAPH-4E2D" -> "cjk"). Generic: no script list."""
    counts: dict[str, int] = {}
    total = 0
    for ch in text:
        if not ch.isalpha():
            continue
        name = unicodedata.name(ch, "")
        key = name.split(" ", 1)[0].split("-", 1)[0].lower() if name else "other"
        counts[key] = counts.get(key, 0) + 1
        total += 1
    return {k: round(v / total, 3) for k, v in sorted(counts.items(), key=lambda kv: -kv[1])} if total else {}


def features(text: str) -> dict:
    words = _WORD.findall(text)
    n_words = len(words)
    letters = [c for c in text if c.isalpha()]
    stripped = text.strip()
    trailing = 0 if stripped[-1:] in _END_PUNCT else 1
    sentences = max(1, len(_SENT_END.findall(stripped)) + trailing)
    first_alpha = next((c for c in stripped if c.isalpha()), "")
    per100 = (lambda n: round(100 * n / n_words, 2)) if n_words else (lambda n: 0.0)
    return {
        "chars": len(text),
        "words": n_words,
        "lines": text.count("\n") + 1,
        "words_per_sentence": round(n_words / sentences, 2),
        "avg_word_len": round(sum(map(len, words)) / n_words, 2) if n_words else 0.0,
        "starts_lower": bool(first_alpha) and first_alpha.islower(),
        "all_lower": bool(letters) and all(not c.isupper() for c in letters),
        "upper_ratio": round(sum(c.isupper() for c in letters) / len(letters), 3) if letters else 0.0,
        "ends_with_punct": stripped[-1:] in _END_PUNCT,
        "emoji": sum(_is_emoji(c) for c in text),
        "exclaim": text.count("!"),
        "question": text.count("?"),
        "ellipsis": text.count("...") + text.count("…"),
        "em_dash": text.count("—"),
        "comma_per100w": per100(text.count(",")),
        "laugh": len(_LAUGH.findall(text)),
        "shorthand_per100w": per100(len(_SHORTHAND.findall(text))),
        "contraction_per100w": per100(len(_CONTRACTION.findall(text))),
        "hedge": len(_HEDGE.findall(text)),
        "urls": len(_URL.findall(text)),
        "script": script_mix(text),
    }


# Numeric/boolean features that describe a unit's "shape" for retrieval diversity, each squashed to ~0..1.
_VECTOR_SPEC: tuple[tuple[str, float], ...] = (
    ("words", 0.0), ("lines", 0.0), ("words_per_sentence", 40.0), ("avg_word_len", 10.0),
    ("starts_lower", 1.0), ("all_lower", 1.0), ("upper_ratio", 0.3), ("ends_with_punct", 1.0),
    ("emoji", 0.0), ("exclaim", 0.0), ("question", 0.0), ("ellipsis", 0.0), ("em_dash", 0.0),
    ("comma_per100w", 20.0), ("laugh", 0.0), ("shorthand_per100w", 20.0), ("contraction_per100w", 10.0),
    ("hedge", 0.0), ("urls", 0.0),
)
_LOG_SCALE = {"words": math.log1p(400), "lines": math.log1p(20)}
_PRESENCE = {"emoji", "exclaim", "question", "ellipsis", "em_dash", "laugh", "hedge", "urls"}  # count > 0


def vector(feats: dict) -> list[float]:
    """Normalised numeric vector of a unit's features; Euclidean distance between two is the style distance."""
    out: list[float] = []
    for key, scale in _VECTOR_SPEC:
        v = feats.get(key, 0)
        if key in _LOG_SCALE:
            x = math.log1p(max(float(v), 0)) / _LOG_SCALE[key]
        elif key in _PRESENCE:
            x = 1.0 if v else 0.0
        else:
            x = float(v) / scale if scale else float(v)
        out.append(min(max(x, 0.0), 1.5))
    return out


def distance(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))


def compute_missing(con: sqlite3.Connection) -> int:
    """Compute features for every unit that has none for the current FEATURE_VERSION.

    The (pure, CPU-only) feature computation runs OUTSIDE any write transaction, and the inserts commit
    in batches of FEATURE_BATCH: a large backlog must not hold the write lock for the whole computation,
    which would starve `lint`/`feedback`/`context` reads for however long that takes."""
    rows = con.execute(
        "SELECT u.unit_id, u.text FROM units u LEFT JOIN unit_features f "
        "ON f.unit_id = u.unit_id AND f.version = ? WHERE f.unit_id IS NULL", (FEATURE_VERSION,)).fetchall()
    computed = [(r[0], FEATURE_VERSION, json.dumps(features(r[1]), ensure_ascii=False)) for r in rows]
    for i in range(0, len(computed), FEATURE_BATCH):
        batch = computed[i:i + FEATURE_BATCH]
        with tx(con):
            con.executemany("INSERT OR REPLACE INTO unit_features VALUES (?,?,?)", batch)
    return len(rows)
