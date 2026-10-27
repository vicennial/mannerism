"""Registers: who a piece of text is for.

A unit's effective register is the agent's label when there is one, otherwise the adapter's default.
`core.UNKNOWN_REGISTER` ("?") means "not decided": such units stay out of the bank until the agent
labels them (see `packets`). `core.SKIP_REGISTER` ("skip") is never used as style evidence.
"""
from __future__ import annotations

import sqlite3

from .core import REGISTERS, SKIP_REGISTER, UNKNOWN_REGISTER

# SQL for "effective register"; every query that needs it joins `labels l` on unit_id.
EFFECTIVE_SQL = "coalesce(l.register, u.register)"
LABEL_JOIN = "LEFT JOIN labels l ON l.unit_id = u.unit_id"


def effective_register(default: str, label: str | None) -> str:
    return label or default


def is_generation_register(register: str) -> bool:
    return register in REGISTERS


def label_queue(con: sqlite3.Connection, limit: int = 60) -> list[dict]:
    """Units whose effective register is "?", newest first (they are what the user writes now)."""
    rows = con.execute(
        f"SELECT u.unit_id, u.source, u.kind, u.audience, u.ts, u.text FROM units u {LABEL_JOIN} "
        f"WHERE {EFFECTIVE_SQL} = ? AND u.held = 0 ORDER BY u.ts DESC, u.unit_id LIMIT ?", (UNKNOWN_REGISTER, limit)).fetchall()
    # Third-party context lines are stored but never emitted (no packet, card or example carries them).
    return [dict(r) for r in rows]


def unlabelled_count(con: sqlite3.Connection) -> int:
    return con.execute(f"SELECT count(*) FROM units u {LABEL_JOIN} WHERE {EFFECTIVE_SQL} = ? AND u.held = 0",
                       (UNKNOWN_REGISTER,)).fetchone()[0]


def counts_by_register(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute(f"SELECT {EFFECTIVE_SQL} AS reg, count(*) FROM units u {LABEL_JOIN} GROUP BY reg").fetchall()
    return {r[0]: r[1] for r in rows}


def effective_register_of(con: sqlite3.Connection, unit_id: str) -> str | None:
    row = con.execute(f"SELECT {EFFECTIVE_SQL} FROM units u {LABEL_JOIN} WHERE u.unit_id = ?", (unit_id,)).fetchone()
    return row[0] if row else None


__all__ = [
    "REGISTERS",
    "SKIP_REGISTER",
    "UNKNOWN_REGISTER",
    "counts_by_register",
    "effective_register",
    "effective_register_of",
    "is_generation_register",
    "label_queue",
    "unlabelled_count",
]
