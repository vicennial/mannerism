"""When a source is due: per-source run state, intervals and failure backoff.

The state is a small JSON document per source in the corpus `meta` table (`sched:<source>`): when it last ran
and last succeeded, when it is next due, how many runs in a row failed and the last error (a class name and a
short message; never any text the source read).

A quick (session-start) sync may only *start a background sync* for a due scheduled source whose effective
`run_on_session_start` is true (`[sources.<name>] run_on_session_start`, else the adapter's `session_start_default`,
which is off for plugins and for metered adapters); the quick sync itself never runs one.

A source is *scheduled* when it uses the network or is a plugin. Scheduled sources run only in a full or background
sync, once their due time has passed. Other sources run on every sync, though their state is recorded too, so
`mannerism status` can show the last success and last error of every source.

Intervals: the adapter class may set `default_interval_s` (0, the default, means every sync); the user's
`[sources.<name>] interval_s` overrides it. After a failure the source waits `BACKOFF_BASE_S * 2**(failures - 1)`
seconds (at most `BACKOFF_MAX_S`, and never longer than its normal interval when it has one), so a failing source
neither hammers its service nor blocks the other sources. `sync --force` ignores the due time (not consent, not
budgets).
"""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from . import store

BACKOFF_BASE_S = 60
BACKOFF_MAX_S = 6 * 3600
ERROR_CHARS = 200


def key(source: str) -> str:
    return f"sched:{source}"


def get(con: sqlite3.Connection, source: str) -> dict:
    state = store.get_meta_json(con, key(source), {}) or {}
    return state if isinstance(state, dict) else {}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds")


def parse(value) -> datetime | None:
    """An ISO timestamp (naive means UTC) as an aware datetime, or None."""
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) and value else None
    except ValueError:
        return None
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def interval_s(entry: dict | None, adapter=None) -> int:
    """Seconds between runs: the user's `interval_s`, else the adapter's `default_interval_s`, else 0."""
    for v in ((entry or {}).get("interval_s"), getattr(adapter, "default_interval_s", None)):
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            return v
    return 0


def session_start_default(adapter) -> bool:
    """Whether a due `adapter` may be started by a session-start sync when the user did not say. A free network
    adapter opts in with the class attribute `session_start_default = True`; a missing attribute, a plugin
    (nothing is imported to ask) and any adapter with `estimate()` (a metered, paid service) default to off."""
    if adapter is None or callable(getattr(adapter, "estimate", None)):
        return False
    return getattr(adapter, "session_start_default", False) is True


def run_on_session_start(entry: dict | None, adapter=None) -> bool:
    """The effective `run_on_session_start` of a source: its config value if it is a real boolean, else the default."""
    v = (entry or {}).get("run_on_session_start")
    return v if isinstance(v, bool) else session_start_default(adapter)


def is_due(state: dict, now: datetime | None = None) -> bool:
    nd = parse(state.get("next_due"))
    return nd is None or (now or datetime.now(UTC)) >= nd


def backoff_s(failures: int, interval: int = 0) -> int:
    delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** max(0, min(failures, 30) - 1))
    return min(delay, interval) if interval > 0 else delay


def record_success(con: sqlite3.Connection, source: str, interval: int, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    state = {"last_attempt": _iso(now), "last_success": _iso(now), "failures": 0, "last_error": None,
             "next_due": _iso(now + timedelta(seconds=interval)) if interval > 0 else None}
    store.set_meta_json(con, key(source), state)
    return state


def record_failure(con: sqlite3.Connection, source: str, interval: int, error: str, *, backoff: bool,
                   now: datetime | None = None, retry_after: datetime | None = None) -> dict:
    """Record a failed run. `backoff=False` (a local source) keeps the source due on the next sync.
    `retry_after` (a service's own "try again at") is honoured when it is later than the backoff."""
    now = now or datetime.now(UTC)
    old = get(con, source)
    failures = int(old.get("failures") or 0) + 1
    due = now + timedelta(seconds=backoff_s(failures, interval)) if backoff else None
    if backoff and retry_after and retry_after > due:
        due = retry_after
    next_due = _iso(due) if due else None
    state = {"last_attempt": _iso(now), "last_success": old.get("last_success"), "failures": failures,
             "last_error": error[:ERROR_CHARS], "next_due": next_due}
    store.set_meta_json(con, key(source), state)
    return state
