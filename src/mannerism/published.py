"""Capture trusted local own-post JSON. No fetching, posting, or candidate publication.

The legacy identity is (source='x', source_ref=str(post_id)). Candidate JSONL rows
are drafts only. A capture links its edit to the post unit so the bank has one
piece of evidence and accepted/mostly-AI text stays out of human examples.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import capture, config, intake, refresh, store, sync
from .adapters._util import own_unit, parse_iso
from .adapters.x_archive import clean_tweet_text
from .core import Me, Unit, hash_ref

MAX_JSON_BYTES = 32 * 1024 * 1024
MAX_POSTS = 10_000
MAX_CANDIDATES = 5_000
_PENDING = "published_refresh_pending"
_QUARANTINE = "_published_capture_quarantine"


def _timestamp(value) -> datetime | None:
    dt = parse_iso(value)
    if dt is None and isinstance(value, str):
        try:
            dt = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
        except ValueError:
            pass
    try:
        return dt.astimezone(UTC) if dt else None
    except (ValueError, OverflowError):
        return None


def _posts(path: Path) -> list:
    if path.stat().st_size > MAX_JSON_BYTES:
        raise ValueError("own-post JSON exceeds the size limit")
    data = json.loads(path.read_text("utf-8"))
    if isinstance(data, dict):
        data = data.get("data", data.get("items"))
    if not isinstance(data, list):
        raise ValueError("expected own-post JSON array, data array, or items array")
    if len(data) > MAX_POSTS:
        raise ValueError("too many posts in one capture")
    return data


def load_candidates(paths) -> list[dict]:
    """Read only explicitly supplied local JSONL: ts/added, text/reply_text, target_id."""
    out = []
    for raw in paths:
        path = Path(raw)
        if not path.exists():
            continue  # an optional reply-card file may not exist yet
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("candidate file exceeds the size limit")
        with path.open(encoding="utf-8") as fh:
            for line in fh:
                if len(line) > capture.MAX_ITEM_BYTES * 8:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(row, dict) or row.get("register", "public") != "public":
                    continue
                ts = _timestamp(row.get("ts") or row.get("added"))
                text = row.get("text") or row.get("reply_text")
                if ts is None or not isinstance(text, str) or not text.strip():
                    continue
                try:
                    size = len(text.encode("utf-8"))
                except UnicodeError:
                    continue
                if size > capture.MAX_ITEM_BYTES or not capture.segments(text):
                    continue
                target = row.get("target_id")
                if target is not None and (isinstance(target, bool) or not isinstance(target, (str, int))):
                    continue
                out.append({"ts": ts, "text": text, "target": str(target) if target else None})
                if len(out) > MAX_CANDIDATES:
                    raise ValueError("too many candidate rows in one capture")
    return out


def _unit(item, me: Me) -> Unit | None:
    if not isinstance(item, dict):
        return None
    pid = item.get("id")
    if (isinstance(pid, bool) or not isinstance(pid, (str, int)) or len(str(pid)) > 32 or not str(pid).isascii()
            or not str(pid).isdigit() or int(pid) <= 0):
        return None
    author = item.get("author")
    if author is not None and not isinstance(author, dict):
        return None
    if isinstance(author, dict):
        handle = author.get("userName") or author.get("screen_name")
        own = me.handles.get("x")
        if (not isinstance(handle, str) or not own or handle.lstrip("@").casefold() !=
                own.lstrip("@").casefold()):
            return None
    if item.get("author_id") and me.handles.get("x_id") and str(item["author_id"]) != str(me.handles["x_id"]):
        return None
    note = item.get("note_tweet")
    text = note.get("text") if isinstance(note, dict) else None
    text = text or item.get("text")
    refs = item.get("referenced_tweets")
    retweet = isinstance(refs, list) and any(isinstance(r, dict) and r.get("type") == "retweeted" for r in refs)
    if not isinstance(text, str) or text.startswith("RT @") or item.get("isRetweet") or retweet or item.get("type") == "retweet":
        return None
    try:
        if len(text.encode("utf-8")) > 256_000:
            return None
    except UnicodeError:
        return None
    ts = _timestamp(item.get("created_at") or item.get("createdAt"))
    if ts is None:
        return None
    reply = bool(item.get("isReply") or text.startswith("@") or item.get("in_reply_to_user_id"))
    text = clean_tweet_text(text, reply)
    conv = item.get("conversation_id", item.get("conversationId"))
    if conv is None or conv == "":
        conv = pid
    if isinstance(conv, bool) or not isinstance(conv, (str, int)):
        return None
    return own_unit(source="x", ref=str(pid), ts=ts, text=text, kind="reply" if reply else "post",
                    register="public", audience="public", thread_id=hash_ref(f"x:{conv}"),
                    audience_ref=hash_ref("x:public"), meta={"public": True, "conversation_id": str(conv)})


def _pool(con, candidates: list[dict]) -> list[dict]:
    rows = [{"id": r["draft_id"], "ts": _timestamp(r["ts"]), "text": r["text"], "target": r["target_id"]}
            for r in con.execute("SELECT draft_id, ts, text, target_id FROM drafts WHERE register='public'")]
    # The same candidate may already be logged by lint. It must not create false ambiguity.
    seen, pool = set(), []
    for row in [*rows, *candidates]:
        if row["ts"] is None:
            continue
        segs = capture.segments(row["text"])
        key = (row["ts"], row["target"], tuple(s.lower() for s in segs))
        if not segs or key in seen:
            continue
        seen.add(key)
        pool.append({**row, "segs": segs})
    return pool


def _queue_refresh(con, *registers) -> None:
    pending = set(store.get_meta_json(con, _PENDING, []) or [])
    store.set_meta_json(con, _PENDING, sorted(pending | {"public"} | {r for r in registers if r and r != "?"}))


def _match(con, unit: Unit, pool: list[dict]) -> str:
    existing = con.execute("SELECT status, ratio FROM edits WHERE unit_id=? ORDER BY edit_id LIMIT 1",
                           (unit.unit_id,)).fetchone()
    if existing:
        if existing["status"] == "accepted" or (existing["ratio"] or 0) >= capture.AI_RATIO:
            con.execute("INSERT OR IGNORE INTO ai_units(unit_id, ratio, detected_at) VALUES (?,?,?)",
                        (unit.unit_id, existing["ratio"] or 1.0, store.now_iso()))
        return existing["status"]
    final = capture.clean(unit.text)
    if len(final.split()) < capture.MIN_WORDS:
        return "unmatched"
    distinct = {}
    for row in pool:
        if not unit.ts - timedelta(days=capture.WINDOW_DAYS) <= row["ts"] <= unit.ts:
            continue
        target = row["target"]
        threaded = bool(target and target in (unit.meta["conversation_id"], unit.thread_id))
        floor = capture.MATCH_MIN - (capture.THREAD_BONUS if threaded else 0)
        raw, segment = max(((capture.ratio(s.lower(), final.lower(), floor), s) for s in row["segs"]),
                           default=(0.0, ""))
        score = min(1.0, raw + (capture.THREAD_BONUS if threaded else 0))
        if raw > 0 and score >= capture.MATCH_MIN:
            key = tuple(s.lower() for s in row["segs"])
            if key not in distinct or score > distinct[key][0]:
                distinct[key] = (score, raw, segment, row)
    scored = list(distinct.values())
    scored.sort(key=lambda entry: -entry[0])
    if not scored:
        return "unmatched"
    best_raw = max(r[1] for r in scored)
    if best_raw >= capture.AI_RATIO and len(final.split()) >= capture.AI_MIN_WORDS:
        con.execute("INSERT INTO ai_units(unit_id, ratio, detected_at) VALUES (?,?,?) "
                    "ON CONFLICT(unit_id) DO UPDATE SET ratio=excluded.ratio, detected_at=excluded.detected_at",
                    (unit.unit_id, round(best_raw, 3), store.now_iso()))
    if len(scored) > 1 and scored[0][0] - scored[1][0] < capture.AMBIGUOUS_GAP:
        return "ambiguous"
    _, raw, segment, row = scored[0]
    _, status, _ = capture.add_edit(con, "public", segment, unit.text, "x-capture", unit_id=unit.unit_id,
                                   draft_id=row.get("id"), score=raw, thread_key=unit.thread_id, now=unit.ts)
    return status


def capture_posts(posts_path: str | Path, candidates_paths=(), *, con: sqlite3.Connection | None = None,
                  me: Me | None = None) -> dict:
    """Import local own posts and return edited/accepted/unmatched/ambiguous/skipped counts.

    Already decided, unchanged posts return zero counts. Input errors raise before
    opening the store. With an injected connection the caller owns the sync lock.
    The normal entry point takes that lock itself. No network calls are made.
    """
    posts = _posts(Path(posts_path))
    candidates = load_candidates(candidates_paths)
    cfg = config.load_config()
    me = me if me is not None else cfg.me
    # Last occurrence wins, as in the host's own-post archive.
    units, skipped = {}, 0
    for item in posts:
        unit = _unit(item, me)
        if unit is None:
            skipped += 1
        else:
            units[unit.unit_id] = unit
    counts = Counter({"edited": 0, "accepted": 0, "unmatched": 0, "ambiguous": 0, "skipped": skipped})
    owned = con is None
    lock = None
    try:
        if owned:
            lock = sync.acquire_lock()
            if lock is None:
                raise RuntimeError("Mannerism sync is busy; retry capture-posts later")
            con = store.connect()
        pool = _pool(con, candidates)
        screen = intake.Intake(con, cfg)
        for unit in sorted(units.values(), key=lambda u: (u.ts, u.source_ref)):
            fingerprint = hashlib.sha256(json.dumps(
                [unit.text, unit.ts.isoformat(), unit.thread_id], ensure_ascii=False).encode()).hexdigest()
            taken = screen.apply([unit])
            if not taken.units:
                if taken.excluded_refs:
                    result = store.write_units(con, "x", "published-posts", [], None,
                                               delete_refs=taken.excluded_refs)
                    _queue_refresh(con, *result.deleted_registers)
                counts["skipped"] += 1
                continue
            prior_held = False
            old = con.execute("SELECT text, held, meta_json FROM units WHERE unit_id=?", (unit.unit_id,)).fetchone()
            if old:
                try:
                    meta = json.loads(old["meta_json"])
                except (TypeError, ValueError):
                    meta = {}
                meta = meta if isinstance(meta, dict) else {}
                unchanged = old["text"] == unit.text
                decision = meta.get("published_capture")
                if (unchanged and _QUARANTINE not in meta and isinstance(decision, dict)
                        and decision.get("sha256") == fingerprint):
                    continue
                if unchanged:
                    prior_held = bool(old["held"])
                    quarantine = meta.get(_QUARANTINE)
                    if (isinstance(quarantine, dict) and quarantine.get("sha256") == fingerprint
                            and type(quarantine.get("prior_held")) is bool):
                        # Resume only our matching quarantine, not an imported/user hold.
                        prior_held = quarantine["prior_held"]
                    unit.meta = {**meta, **unit.meta}
                    if "public" in meta:
                        # Publication provenance does not undo the importer's privacy review.
                        unit.meta["public"] = meta["public"] is True
                # Changed text gets fresh content metadata. In particular, do not
                # restore legacy_voicekit's obsolete raw text after store audit erasure.
                if meta.get("ai"):
                    unit.meta["ai"] = True
            held = bool(unit.held or prior_held)
            # An interrupted capture must not admit an undecided AI post to the bank.
            unit.meta[_QUARANTINE] = {"sha256": fingerprint, "prior_held": prior_held}
            unit.held = True
            store.invalidate_public_snapshot(con)
            result = store.write_units(con, "x", "published-posts", [unit], None)
            effective = con.execute("SELECT coalesce(l.register,u.register) FROM units u "
                                    "LEFT JOIN labels l ON l.unit_id=u.unit_id WHERE u.unit_id=?",
                                    (unit.unit_id,)).fetchone()[0]
            with store.tx(con):
                con.execute("DELETE FROM bank WHERE unit_id=?", (unit.unit_id,))
                _queue_refresh(con, effective, *result.deleted_registers)
            ineligible = held or effective not in ("public", "?") or unit.meta.get("public") is not True
            status = "held" if ineligible else _match(con, unit, pool)
            unit.meta["published_capture"] = {"sha256": fingerprint, "status": status}
            unit.meta.pop(_QUARANTINE)
            store.invalidate_public_snapshot(con)
            with store.tx(con):
                con.execute("UPDATE units SET held=?, meta_json=? WHERE unit_id=?",
                            (int(held), json.dumps(unit.meta, sort_keys=True), unit.unit_id))
            counts[status if status != "held" else "skipped"] += 1
        pending = store.get_meta_json(con, _PENDING, [])
        if pending:
            refresh.refresh(con, pending)
            store.set_meta(con, _PENDING, None)
        return dict(counts)
    finally:
        if owned and con is not None:
            con.close()
        if owned:
            sync.release_lock(lock)
