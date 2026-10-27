"""Rebuild everything derived from units for some registers: features, bank, stats, stats card, drift.

Called after a sync ingested units, after labels or edits changed what the bank contains, and after
`forget`. Idempotent and cheap for registers that did not change (they are simply not passed in).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from . import bank, cards, drift, store, stylometrics
from . import stats as stats_mod
from .core import REGISTERS, Me

LEAK_REBUILD_MIN = 10  # minutes


def refresh(con: sqlite3.Connection, registers=None, me: Me | None = None, *, leak: bool = True) -> dict:
    regs = [r for r in (registers or REGISTERS) if r in REGISTERS]
    store.invalidate_public_snapshot(con)
    stylometrics.compute_missing(con)
    counts = bank.build(con, regs)
    for r in regs:
        st = stats_mod.compute(con, r, me)
        stats_mod.save_stats(r, st)
        if st.get("n"):
            cards.write_stats_card(r, st)
        else:
            cards.clear_cards(r)
    if leak:
        maybe_build_leak(con)
    flagged = drift.check_drift(con, regs)
    from . import public
    if store.get_meta(con, "public_snapshot_enabled"):
        try:
            public.build_snapshot(con, allow_dirty=True)
        except Exception:
            # Revocation must fail closed: never keep serving a stale public projection.
            public.snapshot_path().unlink(missing_ok=True)
            raise
    return {"bank": counts, "drift": {r: d["score"] for r, d in flagged.items()}}


def maybe_build_leak(con: sqlite3.Connection, force: bool = False) -> None:
    """Rebuild the private-only entity index (used by the public leak check and card validation)."""
    built = store.get_meta(con, "leak_built_at")
    if not force and built and datetime.fromisoformat(built) > datetime.now(UTC) - timedelta(minutes=LEAK_REBUILD_MIN):
        return
    stats_mod.build_leak_index(con)
    store.set_meta(con, "leak_built_at", store.now_iso())
