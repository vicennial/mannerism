"""Drift: has the user's writing moved away from what the card describes?

For each register, compare the last DRIFT_DAYS days of the bank with the whole bank on a handful of
features (length, sentence length, casing, punctuation, emoji, contractions ...): the score is the mean
absolute z of the recent feature means (distance in units of the whole bank's standard deviation). Above
THRESHOLD the register is flagged; flagged registers get a `card` packet ("refresh the card"). After an
agent card is written the flag is acknowledged and not raised again for DRIFT_DAYS days.
"""
from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime, timedelta

from . import store
from .core import REGISTERS

DRIFT_DAYS = 30
MIN_RECENT = 15
MIN_TOTAL = 60
THRESHOLD = 0.75

_FEATURES = (
    ("words", lambda f: math.log1p(f["words"])), ("words_per_sentence", lambda f: f["words_per_sentence"]),
    ("avg_word_len", lambda f: f["avg_word_len"]), ("comma_per100w", lambda f: f["comma_per100w"]),
    ("starts_lower", lambda f: float(f["starts_lower"])), ("all_lower", lambda f: float(f["all_lower"])),
    ("ends_with_punct", lambda f: float(f["ends_with_punct"])), ("emoji", lambda f: float(f["emoji"] > 0)),
    ("exclaim", lambda f: float(f["exclaim"] > 0)), ("question", lambda f: float(f["question"] > 0)),
    ("contraction_per100w", lambda f: f["contraction_per100w"]),
    ("shorthand_per100w", lambda f: f["shorthand_per100w"]),
)


def drift_score(con: sqlite3.Connection, register: str, now: datetime | None = None) -> dict | None:
    """{"score", "n", "recent_n", "top": [[feature, z], ...]} or None without enough data."""
    now = now or datetime.now(UTC)
    cut = store.ts_iso(now - timedelta(days=DRIFT_DAYS))
    rows = con.execute("SELECT ts, features_json FROM bank WHERE register=? AND tier='written'", (register,)).fetchall()
    if len(rows) < MIN_TOTAL:
        return None
    feats = [(r["ts"], json.loads(r["features_json"])) for r in rows]
    recent = [f for ts, f in feats if ts >= cut]
    if len(recent) < MIN_RECENT:
        return None
    zs = []
    for name, fn in _FEATURES:
        allv = [fn(f) for _, f in feats]
        mean = sum(allv) / len(allv)
        std = math.sqrt(sum((v - mean) ** 2 for v in allv) / len(allv))
        if std < 1e-9:
            continue
        rmean = sum(fn(f) for f in recent) / len(recent)
        zs.append((name, abs(rmean - mean) / std))
    if not zs:
        return None
    return {"score": round(sum(z for _, z in zs) / len(zs), 3), "n": len(feats), "recent_n": len(recent),
            "top": [[n, round(z, 2)] for n, z in sorted(zs, key=lambda x: -x[1])[:3]]}


def check_drift(con: sqlite3.Connection, registers=None, now: datetime | None = None) -> dict[str, dict]:
    """Update the drift flags and return the flagged registers with their scores."""
    now = now or datetime.now(UTC)
    flagged = {}
    for reg in registers or REGISTERS:
        ack = store.get_meta(con, f"drift_ack:{reg}")
        if ack and datetime.fromisoformat(ack) > now - timedelta(days=DRIFT_DAYS):
            store.set_meta(con, f"drift:{reg}", None)
            continue
        d = drift_score(con, reg, now)
        if d and d["score"] > THRESHOLD:
            store.set_meta_json(con, f"drift:{reg}", {**d, "flagged_at": now.isoformat(timespec="seconds")})
            flagged[reg] = d
        else:
            store.set_meta(con, f"drift:{reg}", None)
    return flagged


def flagged(con: sqlite3.Connection) -> dict[str, dict]:
    out = {}
    for reg in REGISTERS:
        d = store.get_meta_json(con, f"drift:{reg}")
        if d:
            out[reg] = d
    return out


def acknowledge(con: sqlite3.Connection, register: str) -> None:
    """A fresh card was written: clear the flag and do not raise it again for DRIFT_DAYS days."""
    store.set_meta(con, f"drift:{register}", None)
    store.set_meta(con, f"drift_ack:{register}", store.now_iso())
