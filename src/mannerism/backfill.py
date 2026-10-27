"""Fill in where old texts came from.

Texts stored before schema 4 have no origin (account, project, session kind, session): they sit together in one group
per source that cannot be told apart from a person's own writing (`groups.py` marks it `mixed`). `mannerism review
rescan` calls `backfill_origins`: it reads each source that has such texts again from the start, exactly as `scan`
does (in-memory cursors, the same adapters, no network, no plugin code), and for every text it finds that is already
stored without an origin it writes the origin into the existing row. A text that is not stored (it was dropped or
excluded when it was first read) is never added, and no row is ever duplicated: the update is keyed on the unit id and
changes only the origin columns of a row that has none, and only when the text is the same.

Sources that cannot be read again (the files are gone, a network source, a plugin) keep their texts as they are.
"""
from __future__ import annotations

import sqlite3
import time

from . import adapters
from .core import Unit

DEFAULT_BUDGET_S = 120.0
MAX_ROUNDS_PER_LOCATION = 200

_NO_ORIGIN = "account IS NULL AND project IS NULL AND session_kind IS NULL"


def sources_without_origin(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute(f"SELECT DISTINCT source FROM units WHERE {_NO_ORIGIN} ORDER BY source")]


def _fill(con: sqlite3.Connection, units: list[Unit]) -> int:
    rows = [(u.account, u.project, u.project_path, u.session_kind, u.session, u.unit_id, u.text)
            for u in units if u.account or u.project or u.session_kind]
    n = 0
    if rows:
        con.execute("BEGIN IMMEDIATE")
        try:
            for r in rows:
                n += con.execute(
                    f"UPDATE units SET account=?, project=?, project_path=?, session_kind=?, "
                    f"session=coalesce(session, ?) WHERE unit_id=? AND {_NO_ORIGIN} AND text=?", r).rowcount
        except BaseException:
            con.execute("ROLLBACK")
            raise
        con.execute("COMMIT")
    return n


def backfill_origins(con: sqlite3.Connection, cfg, *, budget_s: float | None = DEFAULT_BUDGET_S) -> dict:
    """Returns {"filled": n, "sources": {name: n}, "skipped": {name: reason}, "budget_exhausted": bool}."""
    t0 = time.monotonic()
    out: dict = {"filled": 0, "sources": {}, "skipped": {}, "budget_exhausted": False}
    me = cfg.me

    def out_of_time() -> bool:
        return budget_s is not None and time.monotonic() - t0 >= budget_s

    for name in sources_without_origin(con):
        entry = cfg.sources.get(name) or {}
        if entry.get("plugin"):
            out["skipped"][name] = "a plugin: its code is not run here"
            continue
        try:
            adapter = adapters.load_for(name, entry, warnings=[])
        except Exception as e:  # noqa: BLE001
            out["skipped"][name] = f"adapter not available ({type(e).__name__})"
            continue
        if getattr(adapter, "network", False):
            out["skipped"][name] = "a network source is not read again"
            continue
        filled = 0
        try:
            for loc in adapter.discover(me, cfg.for_adapter(name)):
                if out_of_time():
                    out["budget_exhausted"] = True
                    break
                cursor: dict | None = None
                for _round in range(MAX_ROUNDS_PER_LOCATION):
                    try:
                        units_iter, cursor = adapter.read(loc, cursor, me)
                        units = list(units_iter)
                    except Exception:  # noqa: BLE001 - an unreadable file leaves its texts as they are
                        break
                    filled += _fill(con, [u for u in units if u.source == name])
                    cursor = dict(cursor or {})
                    if not cursor.get("more") or out_of_time():
                        break
        except Exception as e:  # noqa: BLE001
            out["skipped"][name] = f"could not be read again ({type(e).__name__})"
        if filled:
            out["sources"][name] = filled
            out["filled"] += filled
    return out
