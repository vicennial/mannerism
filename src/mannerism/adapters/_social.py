"""Shared machinery for the continuous social-post sources (Bluesky, Mastodon, X): config gate, status in the cursor,
text hygiene and the paged newest-first scan.

The scan keeps two marks apart. `high_water` is the newest item a COMPLETED scan has seen. While a scan runs, its
state lives in `scan` = {token, candidate, fetched}: the continuation token for the next page, the newest item seen
so far (it becomes the high-water mark only when the scan finishes) and how many items were fetched. A timeout or
error in the middle returns the units gathered so far together with the `scan` continuation, so the next read resumes
at the same page: nothing is lost (the units and the cursor are stored in one transaction) and nothing is read twice.
A first run (no high-water mark yet) stops after `initial` items instead of reading a whole history.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..core import Unit
from ._http import NetError

CURSOR_VERSION = 1
MAX_PAGES_PER_READ = 5  # a read never makes more page requests than this; the rest continues next time
DEFAULT_INITIAL_POSTS = 200  # free sources: how much history the first run reads
DEFAULT_RECONCILE_INTERVAL_S = 7 * 24 * 3600  # free sources: how often deletes and edits are looked for
RECONCILE_ITEMS = 200  # the recent window a reconcile re-reads
RECONCILE_CHECKS = 50  # at most this many vanished-looking posts are confirmed one by one per reconcile
MAX_SEEN = 400  # refs remembered in the cursor (newest first): enough to cover the reconcile window
REJECTED_TOKEN_STATUSES = (400, 404, 410)  # a stored continuation token the service no longer accepts


def utcnow() -> datetime:
    return datetime.now(UTC)


def network_enabled(config: dict | None) -> bool:
    """`allow_network` must be exactly true; anything else (missing, "yes", 1) keeps the source off."""
    return isinstance(config, dict) and config.get("allow_network") is True


def int_option(config: dict | None, key: str, default: int, low: int = 1) -> int:
    try:
        v = int((config or {}).get(key, default))
    except (TypeError, ValueError):
        return default
    return v if v >= low else default


# --- status in the cursor -----------------------------------------------------------------------------------------


def with_status(cursor: dict, message: str, retry_after_s: float | None = None) -> dict:
    """`cursor` plus a `status` entry; everything else in the cursor is untouched. The engine stores it as is, so the
    scheduler can read `status.error` / `status.retry_after` to back off."""
    now = utcnow()
    status = {"error": message[:300], "at": now.isoformat(timespec="seconds")}
    if retry_after_s is not None:
        status["retry_after"] = (now + timedelta(seconds=retry_after_s)).isoformat(timespec="seconds")
    return {**cursor, "status": status}


def backoff_active(cursor: dict) -> bool:
    """True while a stored `status.retry_after` is still in the future (no request should be made)."""
    st = cursor.get("status")
    if not isinstance(st, dict) or not st.get("retry_after"):
        return False
    try:
        return datetime.fromisoformat(st["retry_after"]) > utcnow()
    except ValueError:
        return False


def clean_cursor(cursor: dict | None) -> dict:
    """The cursor if it has this version's shape, else a fresh one (an unknown shape is re-read from scratch; units
    dedupe by id). Always carries `v`."""
    if isinstance(cursor, dict) and cursor.get("v") == CURSOR_VERSION:
        return dict(cursor)
    return {"v": CURSOR_VERSION}


# --- text hygiene -------------------------------------------------------------------------------------------------

_HANDLE = r"@\w+(?:[.-]\w+)*(?:@\w+(?:[.-]\w+)*)?"
_LEAD_MENTIONS = re.compile(rf"^(?:\s*{_HANDLE})+\s*")
_MENTION = re.compile(rf"(?<![\w/@]){_HANDLE}")
MENTION_MARK = ""  # an adapter-inserted placeholder for a structured mention (facet / mention link)
_LEAD_MARKS = re.compile(rf"^(?:\s*(?:{MENTION_MARK}|{_HANDLE}))+\s*")


def finish_text(text: str, reply: bool) -> str:
    """Reply: drop the leading @mentions. Everywhere else a mention of another person becomes `@[user]`, so a third
    party's handle is not stored. Collapses stray spacing."""
    if reply:
        text = _LEAD_MARKS.sub("", text)
    text = text.replace(MENTION_MARK, "@[user]")
    text = _MENTION.sub("@[user]", text)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


def context_text(text: str) -> str:
    """Another person's text on its way into one masked context line: handles masked, spacing collapsed."""
    return _MENTION.sub("@[user]", " ".join(str(text).replace(MENTION_MARK, "@[user]").split()))


def text_hash(text: str) -> str:
    """Short fingerprint of the cleaned text (a reconcile compares it to see an edit)."""
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:10]


def reconcile_due(cursor: dict, interval_s: int, now: datetime | None = None) -> bool:
    """True when the last reconcile is at least `interval_s` old. A cursor that never had one is NOT due: the first
    completed scan only stamps `reconciled_at` (it has just read the recent window anyway)."""
    raw = cursor.get("reconciled_at")
    try:
        last = datetime.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        last = None
    return last is not None and ((now or utcnow()) - last).total_seconds() >= interval_s


def month_key(now: datetime) -> str:
    return now.strftime("%Y-%m")


def next_month_start(now: datetime) -> datetime:
    y, m = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    return datetime(y, m, 1, tzinfo=UTC)


# --- the scan -----------------------------------------------------------------------------------------------------


@dataclass
class ScanResult:
    units: list[Unit]
    cursor: dict  # the state fields (high_water / scan) advanced; no status set
    error: NetError | None = None
    completed: bool = False
    fetched: int = 0
    dropped: bool = False  # a stored continuation token was rejected: the scan state was dropped


def scan(
    cursor: dict,
    *,
    fetch: Callable[[str | None, int], tuple[list, str | None]],
    marker: Callable[[object], object | None],
    at_high_water: Callable[[object, object], bool],
    convert: Callable[[object], list[Unit]],
    page_size: int,
    initial: int,
    max_pages: int | None = None,
    promote_on_drop: bool = False,
) -> ScanResult:
    """Walk a newest-first feed page by page.

    `fetch(token, limit)` -> (items, next_token) or raises NetError. `marker(item)` -> the item's ordering mark, or
    None for items that carry none (a pinned post). `at_high_water(item, hw)` -> True when the item is not newer than the
    stored high-water mark (the scan is complete). `convert(item)` -> units. With no stored high-water mark the scan
    stops after `initial` items.

    A fetch that fails with 400/404/410 while it carried a continuation token means the service no longer accepts that
    token: the `scan` state is dropped (the `high_water` mark is kept) so the next read starts a fresh scan instead of
    retrying the same token forever. `promote_on_drop` (the paid source): on a first run that already read something,
    the newest item seen becomes the high-water mark, so a restart does not read, and pay for, the same posts again."""
    hw = cursor.get("high_water")
    state = cursor.get("scan") if isinstance(cursor.get("scan"), dict) else {}
    token = state.get("token")
    candidate = state.get("candidate")
    fetched = int(state.get("fetched") or 0)
    max_pages = MAX_PAGES_PER_READ if max_pages is None else max_pages
    units: list[Unit] = []
    error: NetError | None = None
    complete = False
    dropped = False
    pages = 0
    while not complete:
        if pages >= max_pages:
            break
        limit = page_size
        if hw is None:
            if fetched >= initial:
                complete = True
                break
            limit = min(page_size, initial - fetched)
        try:
            items, nxt = fetch(token, limit)
        except NetError as e:
            error = e
            dropped = token is not None and e.kind == "http" and e.status in REJECTED_TOKEN_STATUSES
            break
        pages += 1
        for item in items:
            m = marker(item)
            if m is not None and hw is not None and at_high_water(item, hw):
                complete = True
                break
            if hw is None and fetched >= initial:
                complete = True
                break
            fetched += 1
            if m is not None and candidate is None:
                candidate = m
            units.extend(convert(item))
        if complete:
            break
        if not items or nxt is None:
            complete = True
            break
        token = nxt
    new = {k: v for k, v in cursor.items() if k not in ("scan", "status")}
    if complete:
        if candidate is not None:
            new["high_water"] = candidate
    elif dropped:
        if promote_on_drop and hw is None and candidate is not None:
            new["high_water"] = candidate
    elif token is not None or candidate is not None or fetched:
        new["scan"] = {"token": token, "candidate": candidate, "fetched": fetched}
    return ScanResult(units, new, error, complete, fetched, dropped)


def cap_seen(seen: dict, order_key: Callable[[str, object], object], limit: int = MAX_SEEN) -> dict:
    """The `limit` newest entries of a {ref: value} map, newest by `order_key(ref, value)`."""
    if len(seen) <= limit:
        return seen
    keep = sorted(seen.items(), key=lambda kv: order_key(*kv), reverse=True)[:limit]
    return dict(keep)
