"""Telegram Desktop export (`result.json`, whole-account or single-chat).

Own id: `personal_information.user_id` (-> "user<ID>"); else `me.handles["telegram"]`; else the single `from_id`
present in EVERY personal chat
(needs >= 2 personal chats and exactly one such id; the most frequent sender is never used). A single-chat export
without a known id cannot
be attributed (both sides look alike), so it yields nothing and the cursor says {"needs_self_id": true}.

Chat types: personal_chat -> close, private_group/private_supergroup -> group, public channels/supergroups -> an
audience of "public" but register "?" (R3 decision 2: Telegram gives NO public provenance -- a channel/supergroup
post never sets meta.public and is never admitted to the public register just for having been posted there; it
still needs an explicit `voice_feedback(register="public")` or a real published source like x_archive/markdown).
Everything else (saved messages, bot chats) is skipped. Forwarded and service messages are dropped; bursts are
merged. Cursor: {"sig": [...], "chats": {hash(chat id): max message id}}.
"""

from __future__ import annotations

import json
from collections.abc import Iterable

from ..core import Location, Me, Unit, hash_ref
from ._util import build_chat_units, export_candidates, file_sig, from_epoch, parse_iso

MAX_JSON_BYTES = 200 * 1024 * 1024  # P2-11: a single export file this large is skipped, not loaded whole

_CLOSE = {"personal_chat"}
_GROUP = {"private_group", "private_supergroup"}
_PUBLIC = {"public_supergroup", "public_group", "public_channel", "private_channel"}


def flatten_text(text) -> str:
    if isinstance(text, str):
        return text
    if isinstance(text, list):
        return "".join(p if isinstance(p, str) else str(p.get("text", "")) for p in text if isinstance(p, (str, dict)))
    return ""


def _norm_id(value) -> str | None:
    if value is None or value == "":
        return None
    s = str(value).strip()
    return f"user{s}" if s.isdigit() else s


def chats_of(data) -> list[dict]:
    if isinstance(data, dict):
        chats = data.get("chats")
        if isinstance(chats, dict) and isinstance(chats.get("list"), list):
            return [c for c in chats["list"] if isinstance(c, dict)]
        if isinstance(data.get("messages"), list):
            return [data]
    return []


def own_id(data, chats: list[dict], me: Me) -> str | None:
    pi = data.get("personal_information") if isinstance(data, dict) else None
    if isinstance(pi, dict) and pi.get("user_id"):
        return _norm_id(pi["user_id"])
    if me.handles.get("telegram"):
        return _norm_id(me.handles["telegram"])
    personal = [c for c in chats if c.get("type") in _CLOSE]
    if len(personal) < 2:
        return None
    sets = []
    for c in personal:
        sets.append(
            {
                m.get("from_id")
                for m in c.get("messages", [])
                if isinstance(m, dict) and m.get("type") == "message" and isinstance(m.get("from_id"), str)
            }
        )
    common = set.intersection(*sets)  # the one account that is a participant of every personal chat
    return next(iter(common)) if len(common) == 1 else None


class Telegram:
    name = "telegram"
    tier = "export"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            return [
                Location(self.name, str(p), p, "Telegram export")
                for p in export_candidates(self.name, config, (".json",))
            ]
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            sig = file_sig(loc.path)
            if cursor.get("sig") == sig:
                return [], cursor
            if loc.path.stat().st_size > MAX_JSON_BYTES:
                return [], {**cursor, "note": "skipped: file over the size limit was not read"}
            data = json.loads(loc.path.read_text("utf-8", "replace"))
        except (OSError, ValueError):
            return [], cursor
        chats = chats_of(data)
        if not chats:
            return [], cursor
        me_id = own_id(data, chats, me)
        if me_id is None:
            return [], {"needs_self_id": True}
        seen = dict(cursor.get("chats") or {})
        units: list[Unit] = []
        for chat in chats:
            ctype = chat.get("type")
            if ctype in _CLOSE:
                audience, register = "close", "close"
            elif ctype in _GROUP:
                audience, register = "group", "group"
            elif ctype in _PUBLIC:
                audience, register = "public", "?"  # decision 2: no public provenance from Telegram
            else:
                continue
            ckey = hash_ref(str(chat.get("id", chat.get("name", ""))))
            last = int(seen.get(ckey, 0))
            msgs, max_id = [], last
            for m in chat.get("messages", []):
                if not isinstance(m, dict) or m.get("type") != "message" or m.get("forwarded_from"):
                    continue
                text = flatten_text(m.get("text")).strip()
                if not text:
                    continue
                ts = from_epoch(m.get("date_unixtime")) or parse_iso(m.get("date"))
                if ts is None:
                    continue
                sender = str(m.get("from_id") or m.get("from") or "")
                is_own = sender == me_id
                if audience == "public" and not is_own:
                    continue
                mid = int(m.get("id") or 0)
                max_id = max(max_id, mid) if is_own else max_id
                msgs.append((ts, sender, is_own, f"{ckey}:{mid}", text, str(m.get("from") or "")))

            def old(first_ref: str, last=last) -> bool:
                return int(first_ref.rsplit(":", 1)[1]) <= last

            meta = {
                "chat_type": ctype,
                "tz": "utc" if chat.get("messages") and "date_unixtime" in chat["messages"][0] else "assumed-utc",
            }
            # decision 2: meta.public is never set here -- a Telegram channel/supergroup post is not treated
            # as a verified publication just because it was posted somewhere visible to others.
            units.extend(
                build_chat_units(
                    source=self.name,
                    thread=ckey,
                    messages=msgs,
                    audience=audience,
                    register=register,
                    meta=meta,
                    old=old,
                )
            )
            seen[ckey] = max_id
        return units, {"sig": sig, "chats": seen}
