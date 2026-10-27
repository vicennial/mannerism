"""iMessage on macOS: `~/Library/Messages/chat.db` (needs Full Disk Access for the terminal / agent app).

The database is never opened in place: a private temp copy (with its -wal / -shm) is read, so the WAL is applied and
the app's
file is never locked or written. Own = `is_from_me = 1`. Tapbacks (`associated_message_type != 0`) and system items
(`item_type != 0`) are dropped. When `text` is NULL the `attributedBody` typedstream is decoded best-effort
(bytes after `NSString`: 5 header bytes, a length -- 1 byte, or 0x81 + 2 bytes LE -- then UTF-8 text).
1:1 chats -> close, group chats (chat.style 43) -> group. Cursor: {"max_rowid": n}.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..core import Location, Me, Unit, hash_ref
from ._util import DbCopy, build_chat_units, table_columns

_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
HINT = "iMessage (macOS Messages database; grant Full Disk Access to your terminal or agent app if reading fails)"


def decode_attributed(blob: bytes | None) -> str:
    """Best-effort text of a typedstream `attributedBody`; "" when nothing plausible is found."""
    if not blob:
        return ""
    blob = bytes(blob)
    i = blob.find(b"NSString")
    if i < 0:
        return ""
    p = i + len(b"NSString") + 5
    if p >= len(blob):
        return ""
    n = blob[p]
    p += 1
    if n == 0x81:
        n, p = int.from_bytes(blob[p : p + 2], "little"), p + 2
    elif n == 0x82:
        n, p = int.from_bytes(blob[p : p + 4], "little"), p + 4
    if n <= 0 or p + n > len(blob):
        return ""
    return blob[p : p + n].decode("utf-8", "replace")


def apple_time(v) -> datetime | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    try:
        return _APPLE_EPOCH + timedelta(seconds=v / 1e9 if v > 1e12 else v)
    except OverflowError:
        return None


def db_path() -> Path:
    return Path.home() / "Library" / "Messages" / "chat.db"


class IMessage:
    name = "imessage"
    tier = "macos"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            p = db_path()
            if sys.platform != "darwin" or not p.exists():
                return []
            return [Location(self.name, str(p), p, HINT)]
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        last = int(cursor.get("max_rowid") or 0)
        try:
            with DbCopy(loc.path) as con:
                cols = set(table_columns(con, "message"))
                if not {"ROWID", "is_from_me", "date"} <= cols | {"ROWID"}:
                    return [], cursor
                sel_text = "m.text" if "text" in cols else "NULL"
                sel_ab = "m.attributedBody" if "attributedBody" in cols else "NULL"
                filt = ["1=1"]
                if "associated_message_type" in cols:
                    filt.append("COALESCE(m.associated_message_type,0)=0")
                if "item_type" in cols:
                    filt.append("COALESCE(m.item_type,0)=0")
                handle = "m.handle_id" if "handle_id" in cols else "0"
                sql = f"""SELECT m.ROWID, {sel_text}, {sel_ab}, m.is_from_me, m.date, {handle}, c.ROWID,
                c.chat_identifier, c.style
                          FROM message m JOIN chat_message_join j ON j.message_id = m.ROWID JOIN chat c ON c.ROWID =
                          j.chat_id
                          WHERE {" AND ".join(filt)}
                            AND c.ROWID IN (SELECT chat_id FROM chat_message_join WHERE message_id > ?)
                          ORDER BY c.ROWID, m.date, m.ROWID"""
                rows = con.execute(sql, (last,)).fetchall()
                max_rowid = con.execute("SELECT COALESCE(MAX(ROWID),0) FROM message").fetchone()[0]
        except (OSError, sqlite3.Error):
            return [], cursor
        chats: dict[int, dict] = {}
        for rowid, text, body, from_me, date, handle_id, chat_id, ident, style in rows:
            ts = apple_time(date)
            text = (
                (text if isinstance(text, str) and text.strip() else decode_attributed(body))
                .replace("\ufffc", "")
                .strip()
            )
            if ts is None or not text:
                continue
            chat = chats.setdefault(chat_id, {"ident": str(ident), "group": style == 43, "msgs": []})
            chat["msgs"].append((ts, str(handle_id), bool(from_me), str(rowid), text, ""))
        units: list[Unit] = []
        for chat in chats.values():
            thread = hash_ref(chat["ident"])
            aud = "group" if chat["group"] else "close"
            units.extend(
                build_chat_units(
                    source=self.name,
                    thread=thread,
                    messages=chat["msgs"],
                    audience=aud,
                    register=aud,
                    meta={"is_group": chat["group"]},
                    old=lambda ref: int(ref) <= last,
                )
            )
        return units, {"max_rowid": max(last, int(max_rowid))}
