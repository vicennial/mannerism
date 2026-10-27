"""Configured Hermes Telegram databases, opened read-only. No home-directory discovery.

Message row ids and 120-second burst references match the legacy Hermes source.
Compaction aliases and per-unit hashes live in the engine cursor, not the source.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..core import Location, Me, Unit, context_line, hash_ref, merge_bursts
from ._util import db_sig, from_epoch, is_relay, own_unit

MAX_ROWS = 100_000
MAX_CONTENT = 256_000
MAX_SNAPSHOT_CHARS = 32 * 1024 * 1024
# Agent replies only supply a masked context line of CONTEXT_CHARS after whitespace collapse, so read
# a bounded prefix. Long replies then neither dominate the snapshot budget nor hold memory.
AGENT_CONTEXT_READ = 8192
_MACHINE = re.compile(
    r"^\s*(?:\[(?:ASYNC DELEGATION|CONTEXT COMPACTION|Your active task list|IMPORTANT|System note|System[:\]]|"
    r"Note:|STILL IN PROGRESS|Cron delivery)|You've reached the maximum n|Automatic provider fallback|"
    r"⚕\ufe0f? ?\*?Hermes|<system(?:>|-reminder>))", re.I
)
_REPLY = re.compile(r'^\[Replying to: "(.*?)"\]\s*\n\n?', re.S)
_MEDIA = re.compile(
    r"^\[(?:The user (?:sent|shared) [^\]]*|voice message could not be transcribed[^\]]*|"
    r"(?:Image|File|Video|Audio) attached at: [^\]]*|Telegram [^\]]*skipped[^\]]*|screenshot)\]\s*", re.S
)
_INLINE_MEDIA = re.compile(r"\n*\[(?:(?:Image|File|Video|Audio) attached at: [^\]]*|Replied-to [^\]]*|screenshot)\]")
_TRAILING_MACHINE = re.compile(
    r"\n+(?:\[(?:ASYNC DELEGATION|CONTEXT COMPACTION|IMPORTANT|System note|STILL IN PROGRESS)|⚕\ufe0f? ?\*?Hermes).*", re.S
)
_VOICE = re.compile(r'^"(.*?)"(?:\n\n|$)', re.S)
_URL = re.compile(r"https?://\S+")


def clean(content: str) -> tuple[str, bool, str | None]:
    """Strip gateway media/reply wrappers; keep quoted dictation and bounded reply context."""
    if not isinstance(content, str) or len(content) > MAX_CONTENT:
        return "", False, None
    if content.startswith("\x00json:"):
        try:
            parts = json.loads(content[6:])
        except ValueError:
            return "", False, None
        if not isinstance(parts, list):
            return "", False, None
        content = "\n".join(p["text"] for p in parts if isinstance(p, dict) and p.get("type") == "text"
                            and isinstance(p.get("text"), str))
    rest, pieces, voice, replied = content, [], False, None
    while rest:
        rest = rest.lstrip()
        if _MACHINE.match(rest) or is_relay(rest):
            return "", False, None
        if m := _REPLY.match(rest):
            replied, rest = m.group(1), rest[m.end():]
        elif m := _MEDIA.match(rest):
            rest = rest[m.end():]
        elif m := _VOICE.match(rest):
            if _MACHINE.match(m.group(1)) or is_relay(m.group(1)):
                return "", False, None
            voice = True
            pieces.append(m.group(1).strip())
            rest = rest[m.end():]
        else:
            break
    rest = _TRAILING_MACHINE.sub("", _INLINE_MEDIA.sub("", rest)).strip()
    text = "\n".join(p for p in [*pieces, rest] if p)
    return (text, voice, replied) if _URL.sub("", text).strip() else ("", False, None)


def _columns(con, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def _roots(sessions: dict) -> dict[str, str]:
    """Only attributable, non-delegated ancestry is usable; parents also represent compaction."""
    roots = {}
    for sid in sessions:
        current, seen = sid, set()
        while current in sessions and current not in seen:
            seen.add(current)
            row = sessions[current]
            # A shared group session's owner does not identify each message's sender.
            if row["chat_type"] not in (None, "", "dm"):
                break
            try:
                cfg = json.loads(row["model_config"] or "{}")
            except (TypeError, ValueError):
                break
            if not isinstance(cfg, dict) or cfg.get("_delegate_from") is not None:
                break
            parent = row["parent_session_id"]
            if not parent:
                roots[sid] = current
                break
            current = parent
    return roots


class Hermes:
    name = "hermes"
    tier = "export"

    def __init__(self):
        self._owners: dict[str, str] = {}

    def discover(self, me: Me, config: dict | None) -> list[Location]:
        self._owners.clear()
        config = config if isinstance(config, dict) else {}
        user = config.get("user_id") or me.handles.get("hermes") or me.handles.get("telegram")
        paths = config.get("paths", [])
        if not user or not isinstance(user, (str, int)) or isinstance(user, bool):
            return []
        if not isinstance(paths, list):
            return []
        out = {}
        for raw in paths:
            if not isinstance(raw, (str, Path)):
                continue
            try:
                path = Path(raw).expanduser().resolve()
                if not path.is_file():
                    continue
            except (OSError, RuntimeError, ValueError):
                continue
            key = f"{path}#{hash_ref(str(user))}"
            self._owners[key] = str(user)
            out[key] = Location(self.name, key, path, "Hermes Telegram database")
        return [out[k] for k in sorted(out)]

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        user = self._owners.get(loc.key)
        if not user or loc.path is None:
            return [], {**cursor, "_error": "Hermes requires configured paths and an explicit user_id"}
        try:
            sig = db_sig(loc.path)
            if cursor.get("sig") == sig:
                return [], cursor
            # A real SQLite read transaction coordinates with writers and
            # checkpoints. WAL/SHM read coordination is allowed; logical writes
            # are denied by both mode=ro and query_only.
            con = sqlite3.connect(loc.path.as_uri() + "?mode=ro", uri=True, timeout=2)
            try:
                con.row_factory = sqlite3.Row
                con.execute("PRAGMA query_only=ON")
                con.execute("BEGIN")
                units, aliases = self._extract(con, user, cursor, loc.path)
            finally:
                con.close()
        except (OSError, sqlite3.Error, ValueError) as exc:
            # Do not advance or delete anything after an incomplete snapshot.
            return [], {**cursor, "_error": f"Hermes read failed: {type(exc).__name__}: {exc}"}
        old = cursor.get("units") or {}
        fingerprints = {u.source_ref: hashlib.sha256(json.dumps(
            [u.text, u.ts.isoformat(), u.thread_id, u.meta, [(c.who, c.text) for c in u.context]],
            sort_keys=True).encode()).hexdigest() for u in units}
        out = [u for u in units if old.get(u.source_ref) != fingerprints[u.source_ref]]
        new = {"sig": sig, "units": fingerprints, "aliases": aliases}
        deleted = sorted(set(old) - set(fingerprints))
        if deleted:
            new["_delete_refs"] = deleted
        return out, new

    def _extract(self, con, user: str, cursor: dict, path: Path) -> tuple[list[Unit], dict]:
        sc, mc = _columns(con, "sessions"), _columns(con, "messages")
        if not {"id", "source", "user_id", "parent_session_id", "model_config"} <= sc or not {
            "id", "session_id", "role", "content", "timestamp", "platform_message_id", "display_kind"
        } <= mc:
            raise ValueError("unsupported Hermes schema")
        optional = ", model_config"
        optional += ", chat_id" if "chat_id" in sc else ", NULL AS chat_id"
        optional += ", chat_type" if "chat_type" in sc else ", NULL AS chat_type"
        sessions = {r["id"]: r for r in con.execute(
            "SELECT id, parent_session_id" + optional + " FROM sessions WHERE source='telegram' AND user_id=?",
            (user,))}
        roots = _roots(sessions)
        con.create_function("mannerism_own_session", 1, lambda sid: sid in roots, deterministic=True)
        extra = ", " + ", ".join(f"m.{c}" if c in mc else f"NULL AS {c}"
                                  for c in ("_compressed_summary", "observed", "tool_call_id", "tool_calls", "tool_name"))
        rows = con.execute(
            "SELECT m.id, m.session_id, m.role, substr(m.content,1,CASE WHEN m.role='assistant' "
            f"THEN {AGENT_CONTEXT_READ} ELSE {MAX_CONTENT + 1} END) AS content, "
            "m.timestamp, m.platform_message_id, m.display_kind" + extra +
            " FROM messages m JOIN sessions s ON s.id=m.session_id WHERE s.source='telegram' AND s.user_id=? "
            "AND mannerism_own_session(m.session_id) AND m.role IN ('user','assistant') "
            "ORDER BY m.timestamp, m.id LIMIT ?", (user, MAX_ROWS + 1))
        threads: dict[str, list] = {}
        own, aliases, chars = {}, {}, 0
        for n, row in enumerate(rows):
            if n == MAX_ROWS:
                raise ValueError("Hermes snapshot exceeds row limit")
            root = roots.get(row["session_id"])
            ts = from_epoch(row["timestamp"])
            content = row["content"]
            if root is None or ts is None or not isinstance(content, str):
                continue
            chars += len(content)
            if chars > MAX_SNAPSHOT_CHARS:
                raise ValueError("Hermes snapshot exceeds text budget")
            if row["role"] == "assistant":
                if content.strip():
                    threads.setdefault(root, []).append((False, ts, str(row["id"]), content[:MAX_CONTENT], None))
                continue
            if row["display_kind"] is not None or any(row[c] for c in
                    ("_compressed_summary", "observed", "tool_call_id", "tool_calls", "tool_name")):
                continue
            text, voice, replied = clean(content)
            if not text:
                continue
            scope = str(sessions[root]["chat_id"] or root)
            key = hash_ref(json.dumps([scope, "p", str(row["platform_message_id"])]) if row["platform_message_id"]
                           else json.dumps([scope, "t", content, int(ts.timestamp())]))
            ref = (cursor.get("aliases") or {}).get(key) or aliases.get(key) or str(row["id"])
            aliases[key] = ref
            # Latest copy supplies edits; earliest/cursor row id remains the native reference.
            own[key] = (root, True, ts, ref, text, (voice, replied))
        for root, *event in own.values():
            threads.setdefault(root, []).append(tuple(event))
        units = []
        for root, events in threads.items():
            flat, info, last_agent = [], {}, None
            for is_own, ts, ref, text, extra in sorted(events, key=lambda e: (e[1], int(e[2]))):
                if not is_own:
                    if text != last_agent:
                        flat.append((False, ts, "", text))
                        last_agent = text
                    continue
                info[ref] = (extra, last_agent)
                flat.append((True, ts, ref, text))
            for ts, text, refs in merge_bursts(flat):
                (voice, replied), previous = info[refs[0]]
                context = replied if replied is not None else previous
                ctx = context_line("agent", context) if context else None
                u = own_unit(source=self.name, ref=refs[0] if len(refs) == 1 else f"{refs[0]}..{refs[-1]}",
                             ts=ts, text=text, kind="prompt", register="agent", audience="agent",
                             thread_id=hash_ref(root), audience_ref=hash_ref("hermes"), parts=len(refs),
                             context=[ctx] if ctx else [], account=str(path.parent), session_kind="interactive",
                             session=hash_ref(root), meta={"replying": replied is not None,
                                 "voice_transcribed": voice or any(info[r][0][0] for r in refs)})
                if u:
                    units.append(u)
        return units, aliases
