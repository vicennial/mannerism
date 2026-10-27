"""Per-register style statistics, saved to `stats/<register>.json`.

These numbers drive three things: the deterministic stats card (`cards`), lint's range checks
(p5..p95 of length, sentence length, comma density ...) and lint's tell calibration (`tells`: the
share of the user's own units in the register that contain each AI tell). Everything is computed
from the bank, i.e. from text the user wrote.

Also here: the private-only entity index used by the public leak check (`leak_index`).
"""
from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from . import bank as bank_mod
from . import core, tells
from .core import REGISTERS, Me

STATS_VERSION = 1
NUMERIC = ("words", "chars", "lines", "words_per_sentence", "avg_word_len", "comma_per100w", "shorthand_per100w",
           "contraction_per100w", "upper_ratio")
RATES = {  # stats key -> (feature, test)
    "starts_lower": ("starts_lower", bool), "all_lower": ("all_lower", bool),
    "ends_with_punct": ("ends_with_punct", bool), "emoji": ("emoji", lambda v: v > 0),
    "exclaim": ("exclaim", lambda v: v > 0), "question": ("question", lambda v: v > 0),
    "ellipsis": ("ellipsis", lambda v: v > 0), "em_dash": ("em_dash", lambda v: v > 0),
    "laugh": ("laugh", lambda v: v > 0), "hedge": ("hedge", lambda v: v > 0), "urls": ("urls", lambda v: v > 0),
}
TELL_SAMPLE = 3000  # newest units per register used to measure the user's own tell rates
TELL_TEXT_CAP = 4000
_WORD = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)
THREAD_SOFTEN = 25  # root finding (a): a unit's stats weight shrinks past this many same-thread units
DISCOURSE_WORDS = frozenset({
    "hey", "hi", "ok", "okay", "so", "also", "yes", "yeah", "yep", "no", "nah", "thanks", "pls", "please",
    "can", "could", "would", "what", "why", "how", "hmm", "lol", "btw", "actually", "just", "quick", "wait",
    "alright", "sure", "cool", "nice", "great", "sorry", "hello", "morning",
})
EDGE_MIN_SHARE = 0.05  # root finding (b): an opener/closer must cover this share of (weighted) units to show


def stats_dir() -> Path:
    return core.home() / "stats"


def percentile(sorted_vals: list[float], p: float) -> float:
    """Linear-interpolated percentile of an ascending list (p in 0..100)."""
    if not sorted_vals:
        return 0.0
    pos = (len(sorted_vals) - 1) * p / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _dist(vals: list[float]) -> dict:
    s = sorted(vals)
    n = len(s)
    mean = sum(s) / n
    var = sum((v - mean) ** 2 for v in s) / n
    return {"p5": round(percentile(s, 5), 3), "p10": round(percentile(s, 10), 3), "median": round(percentile(s, 50), 3),
            "p90": round(percentile(s, 90), 3), "p95": round(percentile(s, 95), 3), "mean": round(mean, 4),
            "std": round(math.sqrt(var), 4)}


def _weights(rows) -> list[float]:
    """Per-row stats weight, aligned with `rows`: 1/max(1, n_thread/THREAD_SOFTEN).

    Root finding (a): on real data, one automated Claude Code paste session contributed 1307 of 2460
    units in a register and skewed "em dashes" to 34%. A unit with no thread_id is its own thread
    (weight 1); a thread of THREAD_SOFTEN or fewer units is also unweighted -- only a thread that keeps
    growing past that loses influence, smoothly rather than being capped outright."""
    counts: Counter = Counter(r["thread_id"] for r in rows if r["thread_id"])
    return [1.0 / max(1.0, counts[r["thread_id"]] / THREAD_SOFTEN) if r["thread_id"] else 1.0 for r in rows]


def percentile_weighted(pairs: list[tuple[float, float]], p: float) -> float:
    """Linear-interpolated weighted percentile of [(value, weight), ...] (p in 0..100)."""
    s = sorted(pairs)
    if not s:
        return 0.0
    total = sum(w for _, w in s)
    if total <= 0:
        return s[-1][0]
    target = total * p / 100
    cum = 0.0
    prev_v, prev_cum = s[0][0], 0.0
    for v, w in s:
        cum += w
        if cum >= target:
            return v if cum == prev_cum else prev_v + (v - prev_v) * (target - prev_cum) / (cum - prev_cum)
        prev_v, prev_cum = v, cum
    return s[-1][0]


def _dist_weighted(pairs: list[tuple[float, float]]) -> dict:
    total = sum(w for _, w in pairs) or 1.0
    mean = sum(v * w for v, w in pairs) / total
    var = sum(w * (v - mean) ** 2 for v, w in pairs) / total
    return {"p5": round(percentile_weighted(pairs, 5), 3), "p10": round(percentile_weighted(pairs, 10), 3),
            "median": round(percentile_weighted(pairs, 50), 3), "p90": round(percentile_weighted(pairs, 90), 3),
            "p95": round(percentile_weighted(pairs, 95), 3), "mean": round(mean, 4), "std": round(math.sqrt(var), 4)}


def compute(con: sqlite3.Connection, register: str, me: Me | None = None, holdout: bool = False) -> dict:
    """Statistics of the bank rows for `register` (all written units; edited finals included)."""
    where, args = "register = ?", [register]
    hl = bank_mod.holdout_limit(con) if holdout else None
    if hl:
        where += " AND ts < ?"
        args.append(hl[1])
    rows = con.execute(f"SELECT unit_id, ts, text, thread_id, features_json FROM bank WHERE {where} "
                       f"ORDER BY ts DESC", args).fetchall()
    n = len(rows)
    out: dict = {"version": STATS_VERSION, "register": register, "n": n,
                 "computed_at": datetime.now(UTC).isoformat(timespec="seconds")}
    if not n:
        return out
    feats = [json.loads(r["features_json"]) for r in rows]
    weights = _weights(rows)  # root finding (a): thread-balanced, see _weights()
    wsum = sum(weights) or 1.0
    out["numeric"] = {k: _dist_weighted([(float(f.get(k, 0)), w) for f, w in zip(feats, weights, strict=True)])
                      for k in NUMERIC}
    out["rates"] = {k: round(sum(w for f, w in zip(feats, weights, strict=True) if test(f.get(feat, 0))) / wsum, 4)
                    for k, (feat, test) in RATES.items()}
    scripts: Counter = Counter()
    for f, w in zip(feats, weights, strict=True):
        for k, v in (f.get("script") or {}).items():
            scripts[k] += v * w
    out["scripts"] = {k: round(v / wsum, 3) for k, v in scripts.most_common(6)}
    out["languages"] = _languages(con, [r["unit_id"] for r in rows])
    out["openers"], out["closers"] = _edges([(r["text"], w) for r, w in zip(rows, weights, strict=True)], me)
    sample = list(zip(rows[:TELL_SAMPLE], weights[:TELL_SAMPLE], strict=True))
    wsample = sum(w for _, w in sample) or 1.0
    present: Counter = Counter()
    for r, w in sample:
        for tid in tells.count_all(r["text"][:TELL_TEXT_CAP]):
            present[tid] += w
    out["tells_n"] = len(sample)
    out["tells"] = {t.id: round(present[t.id] / wsample, 4) for t in tells.TELLS}
    # Weighted, like everything above (a big automated thread must not manufacture tell "support" either);
    # lint still suppresses a tell only once this clears MIN_TELL_UNITS.
    out["tell_counts"] = {t.id: round(present[t.id], 2) for t in tells.TELLS}
    return out


def _languages(con: sqlite3.Connection, unit_ids: list[str]) -> dict[str, float]:
    """Share of labelled units per language (labels come from the agent; empty when nothing is labelled)."""
    langs: Counter = Counter()
    total = 0
    for i in range(0, len(unit_ids), 500):
        part = [u for u in unit_ids[i:i + 500] if not u.startswith("edit:")]
        if not part:
            continue
        for r in con.execute(f"SELECT language FROM labels WHERE language IS NOT NULL AND language <> '' "
                             f"AND unit_id IN ({','.join('?' * len(part))})", part):
            langs[r[0].strip().lower()] += 1
            total += 1
    return {k: round(v / total, 3) for k, v in langs.most_common(5)} if total else {}


def _edges(weighted_texts: list[tuple[str, float]], me: Me | None) -> tuple[list[list], list[list]]:
    """Most common first and last words as [word, rate], weighted the same way as the rest of stats
    (root finding (a)).

    Root finding (b): on real data, "most frequent word type anywhere in the text" let through topic
    jargon from one heavy thread ("lane", "fabric") as if it were a stylistic opener/closer. Only words
    on a small, fixed discourse/function-word list (greetings, acknowledgements, fillers) can qualify
    now, and only if they cover at least EDGE_MIN_SHARE of the (weighted) units -- a card must never
    quote a name or a topic word here."""
    firsts: Counter = Counter()
    lasts: Counter = Counter()
    wsum = 0.0
    for t, w in weighted_texts:
        ws = [x.lower() for x in _WORD.findall(t)]
        if not ws:
            continue
        wsum += w
        firsts[ws[0]] += w
        lasts[ws[-1]] += w
    banned = {w.lower() for name in (me.names if me else []) for w in re.split(r"\W+", name) if w}
    wsum = wsum or 1.0

    def top(c: Counter) -> list[list]:
        return [[w, round(k / wsum, 3)] for w, k in c.most_common(12)
                if w in DISCOURSE_WORDS and w not in banned and k / wsum >= EDGE_MIN_SHARE][:5]

    return top(firsts), top(lasts)


# --- persistence ----------------------------------------------------------------------------------------------------

def save_stats(register: str, stats: dict) -> Path:
    d = stats_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    p = d / f"{register}.json"
    _write_private(p, json.dumps(stats, ensure_ascii=False, indent=1, sort_keys=True))
    return p


def load_stats(register: str) -> dict | None:
    try:
        return json.loads((stats_dir() / f"{register}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_private(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def build_stats(con: sqlite3.Connection, registers=None, me: Me | None = None) -> dict[str, dict]:
    out = {}
    for r in registers or REGISTERS:
        st = compute(con, r, me)
        save_stats(r, st)
        out[r] = st
    return out


# --- public leak index ----------------------------------------------------------------------------------------------

_HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9_][A-Za-z0-9_.]{1,30}")
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.IGNORECASE)
_ENTITY = re.compile(r"\b[A-Z][a-z]{1,20}(?:[ \t]+[A-Z][a-z]{1,20})+\b")
LEAK_SCAN_CAP = 50000
LEAK_KEEP = 20000


def entities(text: str) -> set[str]:
    """Handles, URLs and capitalised multi-word names in `text`, normalised for comparison."""
    out = {m.group(0).lower() for m in _HANDLE.finditer(text)}
    out |= {re.sub(r"^https?://(?:www\.)?", "", m.group(0).rstrip(".,;:!?")).lower() for m in _URL.finditer(text)}
    out |= {" ".join(m.group(0).split()) for m in _ENTITY.finditer(text)}
    return out


def build_leak_index(con: sqlite3.Connection) -> dict:
    """Entities that occur in the user's private units but in no public unit. Saved to stats/leak.json."""
    public: set[str] = set()
    private: Counter = Counter()
    rows = con.execute("SELECT source, text, meta_json FROM units ORDER BY ts DESC LIMIT ?", (LEAK_SCAN_CAP,))
    for r in rows:
        try:
            meta = json.loads(r["meta_json"] or "{}")
        except ValueError:
            meta = {}
        ents = entities(r["text"])
        if bank_mod.is_public(meta):
            public |= ents
        else:
            private.update(ents)
    leak = [e for e, _ in private.most_common() if e not in public][:LEAK_KEEP]
    idx = {"version": 1, "entities": leak, "computed_at": datetime.now(UTC).isoformat(timespec="seconds")}
    d = stats_dir()
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    _write_private(d / "leak.json", json.dumps(idx, ensure_ascii=False))
    return idx


def load_leak_index() -> set[str]:
    try:
        return set(json.loads((stats_dir() / "leak.json").read_text(encoding="utf-8")).get("entities", []))
    except (OSError, ValueError):
        return set()
