"""Goose sessions: `sessions/sessions.db` in Goose's data dir (SQLite, WAL).

The data dir comes from the `etcetera` crate with the XDG strategy on Linux and macOS: $XDG_DATA_HOME/goose, else
~/.local/share/goose (not ~/Library/Application Support); Windows uses %APPDATA%\\Block\\goose\\data. An absolute
$GOOSE_PATH_ROOT replaces all of that with <root>/data.

Own text = `text` items of `messages` rows with role user, in sessions of type `user` that have no parent (`hidden`,
`scheduled`, sub-agent and ACP sessions are not typed at a keyboard) and were not started from a recipe or a schedule.
Rows whose metadata says `userVisible: false` (compaction and other injected messages) or `turnContext: true` (the
per-turn clock/cwd note) are dropped, as are built-in slash commands (`/compact`, `/clear`, `/prompts` ...). Steer
interjections typed while the agent runs are kept. `agentVisible: false` rows stay: that is how Goose marks old turns
after a compaction, and the user did type them. The `/skills a b` message ("Use the load_skill tool to load the following
skills: ...") is a template the CLI wrote and is dropped. Not detectable, so kept: the messages an MCP server returns for
`/prompt name args` (stored as ordinary user messages, no marker) and anything else that is expanded before it is
stored. Goose sets nothing on a session that marks `goose run` (headless) apart from an interactive one, so those are
kept too.

The database is read from a private temp copy, and not copied again while the db and -wal files are unchanged.
Cursor: {"msgs": {rowid, id} of the newest `messages` row, "sig": file signatures}. `messages.id` never repeats inside one
database (AUTOINCREMENT), but a deleted and recreated database restarts it; the saved row is checked by its
`message_id`, session and time, and reading starts over (refs dedupe the re-read) when that row is gone or different.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..core import Location, Me, Unit, context_line, hash_ref
from ._util import (
    DbCopy,
    clip_tail,
    config_dirs,
    db_sig,
    from_epoch,
    is_relay,
    mask_pii,
    own_unit,
    rowid_mark,
    rowid_resume,
    table_columns,
)

# Commands handled by the CLI / agent, never sent to the model as prose. `/goal` and `/grind` carry a typed objective.
_COMMANDS = {
    "exit", "quit", "help", "?", "t", "prompts", "prompt", "extension", "builtin", "mode", "model", "clear", "new",
    "compact", "summarize", "edit", "skills", "doctor", "status",
}  # fmt: skip
_WITH_ARGS = {"goal", "grind"}
_SLASH = re.compile(r"^/([\w?-]+)(?:\s+(.*))?$", re.DOTALL)
_SKILLS_TEMPLATE = "Use the load_skill tool to load the following skills:"  # what `/skills a b` stores (session/mod.rs)


def data_dir() -> Path:
    root = os.environ.get("GOOSE_PATH_ROOT")
    if root and Path(root).is_absolute():
        return Path(root) / "data"
    if os.name == "nt" and os.environ.get("APPDATA"):
        return Path(os.environ["APPDATA"]) / "Block" / "goose" / "data"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "goose"


def _json(raw):
    try:
        obj = json.loads(raw) if isinstance(raw, (str, bytes)) else None
    except ValueError:
        return None
    return obj


def message_text(content_json) -> str:
    """The `text` items of a message's content array, joined."""
    items = _json(content_json)
    if not isinstance(items, list):
        return ""
    texts = [
        i["text"] for i in items if isinstance(i, dict) and i.get("type") == "text" and isinstance(i.get("text"), str)
    ]
    return "\n\n".join(t for t in texts if t.strip())


def strip_command(text: str) -> str:
    """'' for a built-in slash command, the typed objective for `/goal ...`, else the text unchanged."""
    if text.lstrip().startswith(_SKILLS_TEMPLATE):
        return ""
    m = _SLASH.match(text.strip())
    if not m:
        return text
    cmd = m.group(1).lower()
    if cmd in _COMMANDS:
        return ""
    if cmd in _WITH_ARGS:
        return (m.group(2) or "").strip()
    return text


class Goose:
    name = "goose"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            out, seen = [], set()
            config = config or {}
            for base in config_dirs(data_dir(), Path.home() / ".local" / "share" / "goose", config.get("dirs")):
                p = base / "sessions" / "sessions.db"
                try:
                    key = str(p.resolve())
                except (OSError, RuntimeError):  # RuntimeError: a symlink loop on Python 3.11
                    continue
                if key not in seen and p.is_file():
                    seen.add(key)
                    out.append(Location(self.name, str(p), p, "Goose sessions"))
            return out
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            if cursor.get("sig") == db_sig(loc.path):
                return [], cursor
            copy = DbCopy(loc.path)
            with copy as con:
                scols = table_columns(con, "sessions")
                mcols = table_columns(con, "messages")
                mid_col = "m.message_id" if "message_id" in mcols else "NULL"
                meta_col = "metadata_json" if "metadata_json" in mcols else "NULL"
                meta_sel = f"m.{meta_col}" if meta_col != "NULL" else "NULL"
                first = "message_id" if "message_id" in mcols else "NULL"
                ident = f"COALESCE({first}, '') || '|' || session_id || '|' || created_timestamp"
                last = rowid_resume(con, "messages", ident, cursor.get("msgs"))
                top = rowid_mark(con, "messages", ident)
                where = ""
                if "session_type" in scols:
                    where += " AND s.session_type = 'user'"
                if "parent_session_id" in scols:
                    where += " AND s.parent_session_id IS NULL"
                if "recipe_json" in scols:
                    where += " AND (s.recipe_json IS NULL OR s.recipe_json = '')"
                if "schedule_id" in scols:
                    where += " AND s.schedule_id IS NULL"
                cwd = "s.working_dir" if "working_dir" in scols else "NULL"
                rows = con.execute(
                    f"SELECT m.id, {mid_col}, m.session_id, m.content_json, m.created_timestamp, {meta_sel}, "
                    f"{cwd} FROM messages m JOIN sessions s ON s.id = m.session_id "
                    f"WHERE m.role = 'user' AND m.id > ?{where} ORDER BY m.id",
                    (last,),
                ).fetchall()
                units: list[Unit] = []
                for rowid, mid, sid, content, created, meta_json, working_dir in rows:
                    meta = _json(meta_json) if meta_json else {}
                    if isinstance(meta, dict) and (meta.get("userVisible") is False or meta.get("turnContext")):
                        continue
                    text = strip_command(message_text(content))
                    ts = from_epoch(created)
                    if not text.strip() or ts is None or is_relay(text):
                        continue
                    agent = self._last_agent(con, sid, rowid, meta_col)
                    ctx = [context_line("agent", clip_tail(mask_pii(agent)))] if agent else []
                    u = own_unit(
                        source=self.name,
                        ref=str(mid or f"{sid}:{rowid}"),
                        ts=ts,
                        text=text,
                        kind="prompt",
                        register="agent",
                        audience="agent",
                        thread_id=hash_ref(str(sid)),
                        context=ctx,
                        meta={"cwd_hash": hash_ref(working_dir)}
                        if isinstance(working_dir, str) and working_dir
                        else {},
                    )
                    if u is not None:
                        units.append(u)
        except (OSError, sqlite3.Error):
            return [], cursor
        return units, {"msgs": top, "sig": copy.sig}

    @staticmethod
    def _last_agent(con, sid: str, before: int, meta_col: str) -> str | None:
        """The agent's text reply this prompt answers: walking back, the first visible assistant text, unless another
        typed prompt comes first (a steer, or a second message before the agent replied)."""
        for _id, role, content, meta_json in con.execute(
            f"SELECT id, role, content_json, {meta_col} FROM messages WHERE session_id = ? AND id < ? "
            "AND role IN ('user', 'assistant') ORDER BY id DESC LIMIT 12",
            (sid, before),
        ):
            meta = _json(meta_json) if meta_json else {}
            meta = meta if isinstance(meta, dict) else {}
            if meta.get("userVisible") is False or meta.get("turnContext"):
                continue  # injected context and the compaction summary are not part of the visible exchange
            text = message_text(content)
            if role == "user":
                if strip_command(text).strip():
                    return None
                continue
            if text:
                return text
        return None
