"""Wispr Flow dictation history: `flow.sqlite`, table `History` (macOS `~/Library/Application Support/Wispr Flow/`,
Windows `%APPDATA%/Wispr Flow/`).

Columns are introspected: text is `editedText` > `formattedText` > `asrText` (first non-empty); `timestamp`, `app` and
`conversationId` are used when present. The register comes from the `app` bundle id (chat apps -> close / work, IDEs and
agent apps -> agent, mail and unknown -> `?`). The database is read from a private temp copy. Cursor: {"max_rowid": n}.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..core import Location, Me, Unit, hash_ref
from ._util import DbCopy, from_epoch, own_unit, parse_iso, table_columns

_AGENT = (
    "cursor",
    "claude",
    "codex",
    "vscode",
    "visualstudio",
    "jetbrains",
    "intellij",
    "pycharm",
    "terminal",
    "iterm",
    "warp",
    "ghostty",
    "kitty",
    "alacritty",
    "xcode",
    "zed",
    "windsurf",
    "chatgpt",
    "openai",
    "gemini",
)
_CLOSE = ("whatsapp", "keepcoder", "telegram", "mobilesms", "imessage", "signal", "messenger", "facebook")
_WORK = ("slack", "tinyspeck", "teams", "linear", "notion", "asana", "jira", "atlassian")


def register_for(app) -> str:
    a = (app or "").lower()
    if not a:
        return "?"
    if any(k in a for k in _AGENT):
        return "agent"
    if any(k in a for k in _CLOSE):
        return "close"
    if any(k in a for k in _WORK):
        return "work"
    return "?"


def _paths() -> list[Path]:
    out = [Path.home() / "Library/Application Support/Wispr Flow/flow.sqlite"]
    if os.environ.get("APPDATA"):
        out.append(Path(os.environ["APPDATA"]) / "Wispr Flow" / "flow.sqlite")
    return out


class Wispr:
    name = "wispr"
    tier = "macos"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            return [Location(self.name, str(p), p, "Wispr Flow dictation history") for p in _paths() if p.is_file()]
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        last = int(cursor.get("max_rowid") or 0)
        try:
            with DbCopy(loc.path) as con:
                cols = table_columns(con, "History")
                text_cols = [c for c in ("editedText", "formattedText", "asrText") if c in cols]
                if not text_cols:
                    return [], cursor
                extra = [c for c in ("timestamp", "app", "conversationId") if c in cols]
                sel = ", ".join(f'"{c}"' for c in [*text_cols, *extra])
                rows = con.execute(
                    f'SELECT rowid, {sel} FROM "History" WHERE rowid > ? ORDER BY rowid', (last,)
                ).fetchall()
        except (OSError, sqlite3.Error):
            return [], cursor
        units: list[Unit] = []
        max_rowid, skipped = last, 0
        names = [*text_cols, *extra]
        for r in rows:
            rowid, rec = r[0], dict(zip(names, r[1:], strict=False))
            max_rowid = max(max_rowid, rowid)
            text = next((rec[c] for c in text_cols if isinstance(rec.get(c), str) and rec[c].strip()), "")
            raw_ts = rec.get("timestamp")
            ts = (
                from_epoch(raw_ts)
                if isinstance(raw_ts, (int, float)) or str(raw_ts).replace(".", "", 1).isdigit()
                else parse_iso(raw_ts if isinstance(raw_ts, str) else "")
            )
            if not text:
                continue
            if ts is None:
                skipped += 1
                continue
            app = rec.get("app") if isinstance(rec.get("app"), str) else None
            conv = rec.get("conversationId")
            u = own_unit(
                source=self.name,
                ref=str(rowid),
                ts=ts,
                text=text,
                kind="dictation",
                register=register_for(app),
                audience="unknown",
                thread_id=hash_ref(str(conv)) if conv else None,
                meta={"app": app} if app else {},
            )
            if u is not None:
                units.append(u)
        return units, {"max_rowid": max_rowid, "skipped": int(cursor.get("skipped", 0)) + skipped}
