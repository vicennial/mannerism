"""Explicit, already-decrypted Android msgstore snapshots. Never decrypt or discover backups.

Native key_id refs and pre-masking burst boundaries match the legacy WhatsApp
importer. A content-digest cursor reconciles edits, exclusions and disappearing
bursts, including changes below the previous maximum row id.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict
from datetime import UTC, datetime
from itertools import groupby
from pathlib import Path

from ..core import (
    CONTEXT_CHARS,
    CONTEXT_LINES,
    ContextLine,
    Location,
    Me,
    Unit,
    hash_ref,
    mask_names,
    mask_pii,
    merge_bursts,
)

_TEXT_TYPES = {0, 1, 3, 9, 13, 42, 43}
_BOT = re.compile(r"⚕\ufe0f? ?\*?Hermes")
_JID = re.compile(r"\b\d{5,}(?:-\d+)?@(?:s\.whatsapp\.net|lid|g\.us)\b")
_MENTION = re.compile(r"@\d{6,}")
_CURSOR_VERSION = 1


def _mask(text: str, names: dict[str, str]) -> str:
    # Apply native-id protections before the generic email/credential masks.
    text = _JID.sub("[phone]", text)
    text = _MENTION.sub("@[user]", text)
    text = mask_pii(text)
    # The core name helper skips one-character names and uses word boundaries.
    # Group subjects can instead be one character, bracketed, or emoji-delimited.
    for name in sorted(names, key=len, reverse=True):
        if name.strip() and (len(name) < 2 or not name[0].isalnum() or not name[-1].isalnum()):
            text = re.sub(rf"(?i)(?<!\w){re.escape(name)}(?!\w)", f"[{names[name]}]", text)
    return mask_names(text, names)


def _columns(con: sqlite3.Connection, table: str) -> set[str]:
    # Table names are constants from this module, never configuration/input.
    return {row[1] for row in con.execute(f'PRAGMA table_info("{table}")')}


def _timestamp(value) -> datetime | None:
    try:
        return datetime.fromtimestamp(value / 1000, UTC) if value > 0 else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


class WhatsAppDB:
    name = "whatsapp_db"
    tier = "export"

    def __init__(self):
        self._paths: set[Path] = set()
        self._names: dict[str, str] = {}

    def discover(self, me: Me, config: dict) -> list[Location]:
        self._paths = set()
        self._names = {}
        config = config or {}
        paths = config.get("paths", [])
        if isinstance(paths, (str, Path)):
            paths = [paths]
        if not isinstance(paths, (list, tuple)):
            return []
        names = config.get("redact_names", [])
        if isinstance(names, list):
            self._names = {n: "person" for n in names if isinstance(n, str) and n.strip()}
        locations = []
        for value in paths:
            if not isinstance(value, (str, Path)) or not str(value).strip():
                continue
            try:
                path = Path(value).expanduser().resolve()
                if path in self._paths:
                    continue
                self._paths.add(path)
                if path.is_file():
                    locations.append(Location(self.name, str(path), path, "Decrypted Android WhatsApp database"))
            except (OSError, RuntimeError, ValueError):
                continue
        return locations

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        if loc.source != self.name or loc.path is None or loc.path.resolve() not in self._paths:
            raise ValueError("WhatsApp database location must be explicitly configured")
        path = loc.path.resolve()
        if not path.is_file():
            raise FileNotFoundError("Configured WhatsApp database is unavailable")
        # as_uri escapes ?, # and other characters that otherwise change SQLite URI semantics.
        con = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
        try:
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")  # one consistent snapshot, including committed WAL changes
            units = self._extract(con, me)
        finally:
            con.close()
        current = {
            u.source_ref: hashlib.sha256(
                json.dumps(asdict(u), sort_keys=True, default=str, ensure_ascii=False).encode()
            ).hexdigest()
            for u in units
        }
        previous = (cursor or {}).get("units", {}) if (cursor or {}).get("version") == _CURSOR_VERSION else {}
        changed = [u for u in units if previous.get(u.source_ref) != current[u.source_ref]]
        new_cursor = {"version": _CURSOR_VERSION, "units": current}
        deleted = sorted(set(previous) - set(current))
        if deleted:
            new_cursor["_delete_refs"] = deleted
        return changed, new_cursor

    def _extract(self, con: sqlite3.Connection, me: Me) -> list[Unit]:
        required = {
            "message": {"_id", "chat_row_id", "from_me", "key_id", "timestamp", "message_type", "text_data"},
            "chat": {"_id", "jid_row_id"},
            "jid": {"_id", "raw_string", "server"},
        }
        cols = {table: _columns(con, table) for table in required}
        if any(not fields <= cols[table] for table, fields in required.items()):
            raise ValueError("Unsupported Android WhatsApp database schema")
        flags = []
        for table in ("message_forwarded", "message_system", "message_revoked"):
            if "message_row_id" in _columns(con, table):
                flags.append(f"EXISTS(SELECT 1 FROM {table} x WHERE x.message_row_id=m._id)")
            else:
                flags.append("0")
        sender = "m.sender_jid_row_id" if "sender_jid_row_id" in cols["message"] else "NULL"
        members = "c.group_member_count" if "group_member_count" in cols["chat"] else "NULL"
        volume = dict(con.execute("SELECT chat_row_id, count(*) FROM message GROUP BY chat_row_id"))
        participants = {}
        if "group_jid_row_id" in _columns(con, "group_participant_user"):
            participants = dict(con.execute(
                "SELECT group_jid_row_id, count(*) FROM group_participant_user GROUP BY group_jid_row_id"
            ))
        names = {**self._names, **{n: "me" for n in me.names if n}}
        if "subject" in cols["chat"]:
            names.update({s: "group" for (s,) in con.execute(
                "SELECT c.subject FROM chat c JOIN jid j ON j._id=c.jid_row_id WHERE j.server='g.us'"
            ) if isinstance(s, str) and s})
        rows = con.execute(f"""
            SELECT m._id, m.chat_row_id, m.from_me, m.key_id, m.timestamp, m.message_type,
                   m.text_data, {sender}, j.raw_string, j.server, {members}, c.jid_row_id,
                   {', '.join(flags)}
            FROM message m JOIN chat c ON c._id=m.chat_row_id JOIN jid j ON j._id=c.jid_row_id
            ORDER BY m.chat_row_id, m.timestamp, m._id
        """)
        units = []
        for chat_id, chat_rows in groupby(rows, key=lambda r: r[1]):
            units.extend(self._chat(chat_rows, names, volume[chat_id], participants))
        return units

    def _chat(self, rows, names: dict[str, str], volume: int, participants: dict) -> list[Unit]:
        context: deque[ContextLine] = deque(maxlen=CONTEXT_LINES)
        contexts: dict[str, tuple[ContextLine, ...]] = {}
        events = []
        others: dict[int, str] = {}
        voice_notes = 0
        bot = False
        thread = None
        is_group = False
        member_count = None

        def remember(who: str, text: str) -> None:
            context.append(ContextLine(who, _mask(" ".join(text.split()), names)[:CONTEXT_CHARS]))

        for row_id, _, from_me, key, timestamp, mtype, text, sender, jid, server, members, jid_row, fwd, system, revoked in rows:
            if thread is None:
                # Preserve the legacy pseudonym too; no dependency on the old package.
                thread = hash_ref(str(jid), salt="voice-kit")
                is_group = server == "g.us"
                member_count = (participants.get(jid_row) or members or None) if is_group else 2
            own = from_me == 1
            if system or mtype == 7 or revoked or mtype == 15:
                continue
            if own and mtype == 2:
                voice_notes += 1
                continue
            ts = _timestamp(timestamp)
            if ts is None:
                continue
            has_text = isinstance(text, str) and bool(text.strip())
            text_type = mtype in _TEXT_TYPES
            ref = str(key) if key else f"row:{thread}:{row_id}"
            if not own:
                who = others.setdefault(sender or jid_row, f"other{len(others) + 1}")
                if has_text and text_type:
                    remember(who, text)
                events.append((False, ts, ref, ""))
                continue
            if fwd:
                if has_text and text_type:
                    remember("me", text)
                continue  # legacy: forwarded own text is context, not a burst boundary
            if not has_text or not text_type:
                continue
            if _BOT.search(text):
                bot = True
                remember("agent", text)
                events.append((False, ts, ref, ""))
                continue
            contexts[ref] = tuple(context)
            remember("me", text)
            # Legacy removes numeric mentions before merging; all other masking follows merging.
            events.append((True, ts, ref, _MENTION.sub("@[user]", text)))

        audience = "agent" if bot and not others else "group" if is_group else "close"
        return [Unit(
            source=self.name,
            source_ref=refs[0] if len(refs) == 1 else f"{refs[0]}..{refs[-1]}",
            ts=ts,
            text=_mask(text, names),
            kind="message",
            register=audience,
            audience=audience,
            thread_id=thread,
            audience_ref=thread,
            parts=len(refs),
            context=contexts[refs[0]],
            meta={"is_group": is_group, "participant_count": member_count,
                  "chat_message_volume": volume, "thread_voice_notes": voice_notes},
        ) for ts, text, refs in merge_bursts(events)]
