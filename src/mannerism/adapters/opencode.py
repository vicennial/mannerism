"""OpenCode sessions: `opencode*.db` in ~/.local/share/opencode ($XDG_DATA_HOME/opencode; `$OPENCODE_DB` names a file).

Two layouts live in these databases and both are read:
- v1 (every release up to 1.19, which is what most installs have): tables `session`, `message`, `part`. A prompt is a
  `message` whose JSON says `role: user`; its text is the `text` parts that are neither `synthetic` nor `ignored`
  (injected file reads and tool notes are synthetic).
- v2 (`session_v2` + `session_message`, from the unreleased 2.x line, and the early `session_message` rows 1.19 already
  carries): a prompt is a `session_message` of type `user`, text at `data.text`. Rows of type system / synthetic /
  skill / shell / compaction / switch are not typed by the user.
v2 migrates v1 rows in place and keeps their message ids, so a v1 message whose id already sits in `session_message` is
skipped here (per message: a partly migrated session keeps its other v1 prompts). v2 migration also writes
`[Attachment unavailable after migration: ...]` lines into the text; those lines are removed.
Sub-agent sessions (`parent_id` set) are dropped, and so are sessions started by `opencode run` (the headless/scripted
mode): the 1.x `run` creates its session with a `permission` ruleset that denies the `question` tool, which the TUI, the
desktop app and ACP never set. That mark lives in the v1 `session.permission` column; a session migrated to v2 loses it
in `session_v2`, so a v2 row also asks the v1 `session` row with the same id. A session that v2's own `run` creates is
not marked at all and is kept. The pre-SQLite JSON `storage/` tree is not read: OpenCode imports it into the database on first
start of any release that has the database.
Built-in commands `/init` and `/review` are stored as their expanded template with no marker; text that starts with
the template (from the OpenCode source) is dropped. Custom commands (`opencode.json` `command`, skills, MCP prompts)
are expanded the same way and cannot be told from typed text: what they expand to is kept.

The database is read from a private temp copy, and not copied again while the db and -wal files are unchanged.
Cursor: {"v1": {rowid, id} of the newest part, "v2": {rowid, id} of the newest session_message, "sig": file signatures}.
A cursor row whose id no longer sits at its rowid (TEXT primary keys reuse rowids after the tail was deleted, e.g. by
/undo; a database may be recreated) restarts that table from the top; refs dedupe the re-read.
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
    headless_session,
    is_builtin_template,
    is_relay,
    mask_pii,
    own_unit,
    rowid_mark,
    rowid_resume,
    table_columns,
)

_UNAVAILABLE = re.compile(r"\n*\[Attachment unavailable after migration: [^\n]*\]")


def typed_only(text: str) -> str:
    """Text without migration placeholders; "" for a built-in command's expanded template or a relayed message."""
    text = _UNAVAILABLE.sub("", text).strip()
    return "" if is_builtin_template(text) or is_relay(text) else text


def data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "opencode"


def _tables(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _json(raw):
    try:
        obj = json.loads(raw) if isinstance(raw, (str, bytes)) else None
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def _text_of(items) -> str:
    """Text of v2 assistant content items / v1 text parts, in order."""
    out = []
    for it in items:
        if isinstance(it, dict) and it.get("type") == "text" and isinstance(it.get("text"), str) and it["text"].strip():
            if not it.get("synthetic") and not it.get("ignored"):
                out.append(it["text"])
    return "\n\n".join(out)


class OpenCode:
    name = "opencode"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            out, seen = [], set()
            config = config or {}

            def add(p: Path):
                try:
                    key = str(p.resolve())
                except (OSError, RuntimeError):  # RuntimeError: a symlink loop on Python 3.11
                    return
                if key not in seen and p.is_file():
                    seen.add(key)
                    out.append(Location(self.name, str(p), p, f"OpenCode database {p.stem}"))

            for base in config_dirs(data_dir(), Path.home() / ".local" / "share" / "opencode", config.get("dirs")):
                for p in sorted(base.glob("opencode*.db")):
                    add(p)
            env_db = os.environ.get("OPENCODE_DB")
            if env_db and env_db != ":memory:":
                add(Path(env_db).expanduser())
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
                tables = _tables(con)
                units: list[Unit] = []
                new = dict(cursor, sig=copy.sig)
                if "session_message" in tables:
                    got, new["v2"] = self._read_v2(con, tables, cursor.get("v2"))
                    units += got
                if {"session", "message", "part"} <= tables:
                    got, new["v1"] = self._read_v1(con, tables, cursor.get("v1"))
                    units += got
        except (OSError, sqlite3.Error):
            return [], cursor
        return units, new

    # -- v2: session_message ---------------------------------------------------------------------------------------

    def _read_v2(self, con, tables: set[str], saved) -> tuple[list[Unit], dict]:
        last = rowid_resume(con, "session_message", "id", saved)
        top = rowid_mark(con, "session_message", "id")
        sessions = "session_v2" if "session_v2" in tables else "session"
        if sessions not in tables:
            return [], top
        cols = table_columns(con, sessions)
        directory = "s.directory" if "directory" in cols else "NULL"
        permission = "s.permission" if "permission" in cols else "NULL"
        old, old_permission = "", "NULL"
        if sessions != "session" and "session" in tables and "permission" in table_columns(con, "session"):
            old = "LEFT JOIN session v1 ON v1.id = s.id"
            old_permission = "v1.permission"
        where = "AND s.parent_id IS NULL" if "parent_id" in cols else ""
        rows = con.execute(
            f"SELECT m.rowid, m.id, m.session_id, m.time_created, m.data, {directory}, {permission}, {old_permission} "
            f"FROM session_message m JOIN {sessions} s ON s.id = m.session_id {old} "
            f"WHERE m.type = 'user' AND m.rowid > ? {where} ORDER BY m.rowid",
            (last,),
        ).fetchall()
        units = []
        for rowid, mid, sid, created, data, directory, permission, v1_permission in rows:
            obj = _json(data) or {}
            text = obj.get("text")
            ts = from_epoch(created)
            if not isinstance(text, str) or ts is None or headless_session(permission) or headless_session(v1_permission):
                continue
            text = typed_only(text)
            if not text:
                continue
            ctx = self._v2_agent(con, sid, rowid)
            u = self._unit(mid, sid, ts, text, ctx, directory)
            if u is not None:
                units.append(u)
        return units, top

    @staticmethod
    def _v2_agent(con, sid: str, before: int) -> str | None:
        """The agent's text reply this prompt answers, unless another typed prompt comes first."""
        for typ, data in con.execute(
            "SELECT type, data FROM session_message WHERE session_id = ? AND type IN ('user', 'assistant') "
            "AND rowid < ? ORDER BY rowid DESC LIMIT 12",
            (sid, before),
        ):
            obj = _json(data) or {}
            if typ == "user":
                if str(obj.get("text") or "").strip():
                    return None
                continue
            text = _text_of(obj.get("content") or [])
            if text:
                return text
        return None

    # -- v1: message + part ----------------------------------------------------------------------------------------

    def _read_v1(self, con, tables: set[str], saved) -> tuple[list[Unit], dict]:
        last = rowid_resume(con, "part", "id", saved)
        top = rowid_mark(con, "part", "id")
        cols = table_columns(con, "session")
        skip = ""
        if "session_message" in tables:
            # v2 migration keeps message ids: skip exactly the messages it already carries, so a partly migrated
            # session still gives its remaining v1 prompts.
            skip += " AND m.id NOT IN (SELECT id FROM session_message)"
        parent = " AND s.parent_id IS NULL" if "parent_id" in cols else ""
        directory = "s.directory" if "directory" in cols else "NULL"
        permission = "s.permission" if "permission" in cols else "NULL"
        # Candidate user messages: those that got a new text part since the last read. Reading by part rowid means a
        # message whose parts land a moment after the message row is never skipped.
        msgs = con.execute(
            f"SELECT DISTINCT m.id, m.session_id, m.time_created, {directory}, {permission} FROM part p "
            "JOIN message m ON m.id = p.message_id JOIN session s ON s.id = m.session_id "
            f'WHERE p.rowid > ? AND p.data LIKE \'%"text"%\' AND m.data LIKE \'%"role"%"user"%\'{parent}{skip} '
            "ORDER BY m.time_created, m.id",
            (last,),
        ).fetchall()
        units = []
        for mid, sid, created, cwd, perm in msgs:
            if headless_session(perm):
                continue
            msg = _json(con.execute("SELECT data FROM message WHERE id = ?", (mid,)).fetchone()[0])
            if not msg or msg.get("role") != "user":
                continue
            parts = [
                _json(d) or {} for (d,) in con.execute("SELECT data FROM part WHERE message_id = ? ORDER BY id", (mid,))
            ]
            if any(p.get("type") in ("compaction", "subtask") for p in parts):
                continue  # compaction requests and sub-agent dispatches are not typed prose
            text = typed_only(_text_of(parts))
            ts = from_epoch(created)
            if not text or ts is None:
                continue
            u = self._unit(mid, sid, ts, text, self._v1_agent(con, sid, created), cwd)
            if u is not None:
                units.append(u)
        return units, top

    @staticmethod
    def _v1_agent(con, sid: str, before) -> str | None:
        """The agent's text reply this prompt answers, unless another typed prompt comes first."""
        for mid, data in con.execute(
            "SELECT id, data FROM message WHERE session_id = ? AND time_created < ? ORDER BY time_created DESC LIMIT 12",
            (sid, before),
        ):
            role = (_json(data) or {}).get("role")
            text = _text_of(
                [
                    _json(d) or {}
                    for (d,) in con.execute("SELECT data FROM part WHERE message_id = ? ORDER BY id", (mid,))
                ]
            )
            if role == "user" and text:
                return None
            if role == "assistant" and text:
                return text
        return None

    def _unit(self, mid, sid, ts, text, agent_text, directory) -> Unit | None:
        # Only masked, clipped text is ever kept from the agent's reply.
        ctx = [context_line("agent", clip_tail(mask_pii(agent_text)))] if agent_text else []
        meta = {"cwd_hash": hash_ref(directory)} if isinstance(directory, str) and directory else {}
        return own_unit(
            source=self.name,
            ref=str(mid),
            ts=ts,
            text=text,
            kind="prompt",
            register="agent",
            audience="agent",
            thread_id=hash_ref(str(sid)),
            context=ctx,
            meta=meta,
        )
