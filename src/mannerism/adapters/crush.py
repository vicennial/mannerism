"""Crush sessions: one SQLite database per project, `<project>/.crush/crush.db` (the data dir can be moved with
`--data-dir` or the `data_directory` option).

Because the databases live inside projects, discovery has three sources, de-duplicated by resolved path:
1. Crush's own project registry, `projects.json` next to its global config ($CRUSH_GLOBAL_DATA, $XDG_DATA_HOME/crush,
   ~/.local/share/crush, %LOCALAPPDATA%/crush), where every run records `{path, data_dir}`. It also finds data dirs
   that were moved.
2. Directories named in `[sources.crush].paths`: a `crush.db` file, a data dir, or a project dir.
3. The working directories of recent Claude Code / Codex sessions (and their parent directories up to ~): a project
   that was opened with another agent is likely to have `.crush/` too.

Own text = `text` parts of `messages` rows with role user whose part is not `hidden` (generated continuations).
Sub-agent sessions (`parent_session_id`), the title helper sessions (`title-...`) and sessions fed by an MCP channel
(`channel` set; the messages come from outside) are dropped. So are sessions still titled `non-interactive`, the title
`crush run` creates its session with. Crush overwrites that title when it generates one for the first prompt (a real
`crush run` database, the test fixture, is titled "Untitled Session"), so this catches almost nothing and `crush run`
sessions are in practice kept: Crush leaves no other mark. Shell commands typed
with `!` are a separate part type and are ignored. Custom commands are stored as their expanded text and cannot be
told apart.

The database is read from a private temp copy, and not copied again while the db and -wal files are unchanged.
Cursor: {"msgs": {rowid, id} of the newest `messages` row, "sig": file signatures}. `messages.id` is a TEXT primary key,
so rowids are reused after tail deletes and a recreated database can grow past the old maximum; when the row at the
saved rowid is no longer the saved one, reading starts over (refs dedupe the re-read).
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from ..core import Location, Me, Unit, context_line, hash_ref
from ._util import (
    DbCopy,
    clip_tail,
    configured_paths,
    db_sig,
    from_epoch,
    is_relay,
    mask_pii,
    own_unit,
    rowid_mark,
    rowid_resume,
    table_columns,
    transcript_cwds,
)

DB_NAME = "crush.db"


def global_data_dir() -> Path:
    if os.environ.get("CRUSH_GLOBAL_DATA"):
        return Path(os.environ["CRUSH_GLOBAL_DATA"])
    if os.environ.get("XDG_DATA_HOME"):
        return Path(os.environ["XDG_DATA_HOME"]) / "crush"
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA") or Path(os.environ.get("USERPROFILE", "")) / "AppData" / "Local"
        return Path(local) / "crush"
    return Path.home() / ".local" / "share" / "crush"


def registered_data_dirs() -> list[Path]:
    """Data dirs from Crush's project registry (empty when it is missing or unreadable)."""
    try:
        obj = json.loads((global_data_dir() / "projects.json").read_text("utf-8"))
        return [Path(p["data_dir"]) for p in obj["projects"] if isinstance(p.get("data_dir"), str) and p["data_dir"]]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []


def _db_in(p: Path) -> Path | None:
    """The database for a file, a data dir or a project dir."""
    for cand in (p, p / DB_NAME, p / ".crush" / DB_NAME):
        try:
            if cand.is_file() and cand.suffix == ".db":
                return cand
        except (OSError, ValueError):  # one unreadable folder (EACCES, a dead mount) must not empty discovery
            continue
    return None


def _json(raw):
    try:
        return json.loads(raw) if isinstance(raw, (str, bytes)) else None
    except ValueError:
        return None


def message_text(parts_json) -> str:
    """Visible text of a message: `text` parts that are not hidden continuations."""
    parts = _json(parts_json)
    if not isinstance(parts, list):
        return ""
    out = []
    for p in parts:
        d = p.get("data") if isinstance(p, dict) else None
        if isinstance(p, dict) and p.get("type") == "text" and isinstance(d, dict) and not d.get("hidden"):
            if isinstance(d.get("text"), str) and d["text"].strip():
                out.append(d["text"])
    return "\n\n".join(out)


class Crush:
    name = "crush"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            home = Path.home()
            found: dict[str, Path] = {}

            def add(db: Path | None):
                if db is not None:
                    try:
                        found.setdefault(str(db.resolve()), db)
                    except (OSError, RuntimeError):  # RuntimeError: a symlink loop on Python 3.11
                        pass

            for d in registered_data_dirs():
                add(_db_in(d))
            for p in configured_paths(config):
                add(_db_in(p))
            for cwd in transcript_cwds(200, 20):
                p = Path(cwd)
                # Crush finds the closest .crush going up, so a session opened in a subdirectory still belongs to the
                # project root. Climb only inside ~: a parent above it is shared, not a project.
                for cand in [p, *p.parents] if home in p.parents else [p]:
                    add(_db_in(cand / ".crush"))
                    if cand == home:
                        break
            return [
                Location(self.name, k, v, f"Crush project {v.parent.parent.name}") for k, v in sorted(found.items())
            ]
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
                last = rowid_resume(con, "messages", "id", cursor.get("msgs"))
                top = rowid_mark(con, "messages", "id")
                where = " AND s.id NOT LIKE 'title-%'"
                if "parent_session_id" in scols:
                    where += " AND s.parent_session_id IS NULL"
                if "channel" in scols:
                    where += " AND (s.channel IS NULL OR s.channel = '')"
                if "title" in scols:
                    where += " AND s.title <> 'non-interactive'"  # `crush run`; the title is usually replaced, see above
                rows = con.execute(
                    "SELECT m.rowid, m.id, m.session_id, m.parts, m.created_at FROM messages m "
                    f"JOIN sessions s ON s.id = m.session_id WHERE m.role = 'user' AND m.rowid > ?{where} "
                    "ORDER BY m.rowid",
                    (last,),
                ).fetchall()
                units: list[Unit] = []
                project = hash_ref(str(loc.path.parent.parent))
                for rowid, mid, sid, parts, created in rows:
                    text, ts = message_text(parts), from_epoch(created)
                    if not text.strip() or ts is None or is_relay(text):
                        continue
                    agent = self._last_agent(con, sid, rowid)
                    ctx = [context_line("agent", clip_tail(mask_pii(agent)))] if agent else []
                    u = own_unit(
                        source=self.name,
                        ref=str(mid),
                        ts=ts,
                        text=text,
                        kind="prompt",
                        register="agent",
                        audience="agent",
                        thread_id=hash_ref(str(sid)),
                        context=ctx,
                        meta={"cwd_hash": project},
                    )
                    if u is not None:
                        units.append(u)
        except (OSError, sqlite3.Error):
            return [], cursor
        return units, {"msgs": top, "sig": copy.sig}

    @staticmethod
    def _last_agent(con, sid: str, before: int) -> str | None:
        """The agent's text reply this prompt answers: the closest earlier assistant text, unless another typed prompt
        comes first. Summary messages are the agent's compaction, not part of the exchange."""
        cols = table_columns(con, "messages")
        skip = " AND is_summary_message = 0" if "is_summary_message" in cols else ""
        for role, parts in con.execute(
            "SELECT role, parts FROM messages WHERE session_id = ? AND rowid < ? "
            f"AND role IN ('user', 'assistant'){skip} ORDER BY rowid DESC LIMIT 12",
            (sid, before),
        ):
            text = message_text(parts)
            if role == "user" and text:
                return None
            if role == "assistant" and text:
                return text
        return None
