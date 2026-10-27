"""Kilo Code (the OpenCode-based CLI and VS Code extension, v7+): ~/.local/share/kilo/kilo.db.

Current releases keep everything in one SQLite database in the XDG data directory (`$XDG_DATA_HOME/kilo`, else
`~/.local/share/kilo`, on Linux, macOS and Windows alike: Kilo uses xdg-basedir everywhere). `$KILO_DB` can point at
another file and non-stable channels use `kilo-<channel>.db`. The VS Code extension runs the same CLI as its backend,
so both write here. Tables: `session` (id, parent_id, directory), `message` (id, session_id, time_created, data JSON
with `role`) and `part` (id, message_id, data JSON with `type`). The database is copied before it is read, never
opened.

Own text = the `text` parts of `role: user` messages, in part order, unless the part is `synthetic` (tool read-backs,
attached file contents, compaction continuations) or `ignored`. File, subtask and compaction parts carry no typed
text. Sessions with a `parent_id` are sub-agent runs whose "user" prompt was written by the parent agent: skipped. So
are sessions started by `kilo run` (headless/scripted): `run` creates its session with a `permission` ruleset that denies
the `question` tool, which the TUI, the VS Code extension and ACP never set. A prompt typed at the TUI is unaffected. The
plan hand-off text Kilo writes into a new session is dropped, and so is text that starts with the expanded template of
the built-in `/init` or `/review` command (from the Kilo source; stored with no marker). Custom commands, skills and MCP
prompts are expanded the same way and cannot be told from typed text: what they expand to is kept.
Timestamps: `message.time_created` (ms).

Older releases (before the SQLite move) wrote one JSON file per message and per part under `<data>/storage/`:
`message/<sessionId>/<messageId>.json`, `part/<messageId>/<partId>.json`, `session/<projectId>/<sessionId>.json`.
Current Kilo imports these into kilo.db on first start, so that layout is read only where no database sits next to it.
Same rules, same refs (`<sessionId>:<messageId>`).

Cursor (database): file signatures plus the newest message time already read; each sync starts five minutes before it,
because a message row lands a moment before its parts. The engine dedupes by unit_id. Cursor (legacy): directory
signature.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
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
    resolved_key,
    table_columns,
)

OVERLAP_MS = 5 * 60 * 1000
_QUOTED_ARGV = re.compile(r'"((?:[^"\\]|\\.)*)"', re.DOTALL)
_NOT_TYPED = ("Plan file: ",)  # the hand-off into a new planning/implementation session
_SLASH_ONLY = re.compile(r"^/[\w:.-]+$")


def kilo_dirs(extra=None) -> list[Path]:
    """Kilo data directories: $XDG_DATA_HOME/kilo or ~/.local/share/kilo, plus configured extras."""
    xdg = (os.environ.get("XDG_DATA_HOME") or "").strip()
    default = Path.home() / ".local" / "share" / "kilo"
    primary = Path(xdg) / "kilo" if xdg else default
    return config_dirs(primary.expanduser(), default, extra)


def typed_text(parts: Iterable[dict]) -> str:
    """Typed text of one user message from its parts (already in order)."""
    out = []
    for p in parts:
        if p.get("type") != "text" or p.get("synthetic") or p.get("ignored") or not isinstance(p.get("text"), str):
            continue
        if p["text"].strip():
            out.append(p["text"].strip())
    text = "\n".join(out)
    m = _QUOTED_ARGV.fullmatch(text)
    if m and " " in m.group(1):
        text = m.group(1).replace('\\"', '"')
    if text.startswith(_NOT_TYPED) or _SLASH_ONLY.match(text) or is_builtin_template(text) or is_relay(text):
        return ""
    return text


def agent_text(parts: Iterable[dict]) -> str:
    return "\n".join(
        p["text"].strip()
        for p in parts
        if p.get("type") == "text"
        and not p.get("synthetic")
        and not p.get("ignored")
        and isinstance(p.get("text"), str)
    ).strip()


def _json(raw) -> dict:
    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


class Kilo:
    name = "kilo"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            out: dict[str, Location] = {}
            dbs = []
            if (os.environ.get("KILO_DB") or "").strip() and Path(os.environ["KILO_DB"]).is_absolute():
                dbs.append(Path(os.environ["KILO_DB"]))
            for base in kilo_dirs((config or {}).get("dirs")):
                found = sorted(p for p in base.glob("kilo*.db") if p.is_file())
                dbs += found
                messages = base / "storage" / "message"
                if not found and messages.is_dir():
                    for d in sorted(messages.iterdir()):
                        if d.is_dir():
                            out[resolved_key(d)] = Location(self.name, str(d), d, f"Kilo session {d.name[-8:]} (JSON)")
            for p in dbs:
                if p.is_file():
                    out.setdefault(resolved_key(p), Location(self.name, str(p), p, f"Kilo database {p.name}"))
            return list(out.values())
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            return self._read_db(loc, cursor) if loc.path.is_file() else self._read_json(loc, cursor)
        except (OSError, sqlite3.Error):
            return [], cursor  # locked, mid-checkpoint or an unfamiliar schema: keep the cursor, retry next sync

    # --- SQLite (current releases) ---------------------------------------------------------------------------------

    def _read_db(self, loc: Location, cursor: dict) -> tuple[Iterable[Unit], dict]:
        if cursor.get("sig") == db_sig(loc.path):
            return [], cursor
        since = int(cursor.get("since") or 0)
        start = max(0, since - OVERLAP_MS) if since else 0
        units: list[Unit] = []
        newest = since
        copy = DbCopy(loc.path)
        with copy as con:
            permission = "permission" if "permission" in table_columns(con, "session") else "NULL"
            sessions = {
                sid: (parent or headless_session(perm), directory)
                for sid, parent, directory, perm in con.execute(
                    f"SELECT id, parent_id, directory, {permission} FROM session"
                )
            }
            rows = con.execute(
                "SELECT id, session_id, time_created, data FROM message "
                "WHERE time_created >= ? AND data LIKE '%\"user\"%' ORDER BY time_created, id",
                (start,),
            ).fetchall()
            for mid, sid, created, raw in rows:
                parent, directory = sessions.get(sid, (None, None))
                if parent or _json(raw).get("role") != "user":
                    continue
                parts = [
                    _json(d) for (d,) in con.execute("SELECT data FROM part WHERE message_id = ? ORDER BY id", (mid,))
                ]
                text = typed_text(parts)
                ts = from_epoch(created)
                if not text or ts is None:
                    continue
                last_agent = self._last_agent(con, sid, created)
                u = own_unit(
                    source=self.name,
                    ref=f"{sid}:{mid}",
                    ts=ts,
                    text=text,
                    kind="prompt",
                    register="agent",
                    audience="agent",
                    thread_id=hash_ref(sid),
                    context=[context_line("agent", last_agent)] if last_agent else [],
                    meta={"cwd_hash": hash_ref(directory)} if directory else {},
                )
                if u is not None:
                    units.append(u)
                    newest = max(newest, int(created))
        return units, {"sig": copy.sig, "since": newest}

    @staticmethod
    def _last_agent(con, sid: str, before: int) -> str | None:
        """Text of the newest assistant reply in the session before `before` (masking happens in context_line)."""
        for mid, raw in con.execute(
            "SELECT id, data FROM message WHERE session_id = ? AND time_created < ? ORDER BY time_created DESC LIMIT 6",
            (sid, before),
        ):
            if _json(raw).get("role") != "assistant":
                continue
            parts = [_json(d) for (d,) in con.execute("SELECT data FROM part WHERE message_id = ? ORDER BY id", (mid,))]
            text = agent_text(parts)
            if text:
                return clip_tail(mask_pii(text))  # mask first: a secret cut by the clip would no longer match
        return None

    # --- JSON files (older releases) -------------------------------------------------------------------------------

    def _read_json(self, loc: Location, cursor: dict) -> tuple[Iterable[Unit], dict]:
        storage = loc.path.parent.parent  # <data>/storage
        files = list(loc.path.glob("*.json"))
        parts_root = storage / "part"
        sig = [loc.path.stat().st_mtime_ns, parts_root.stat().st_mtime_ns if parts_root.is_dir() else 0, len(files)]
        if cursor.get(loc.key) == sig:
            return [], cursor
        sid = loc.path.name
        directory, parent = None, None
        for info in storage.glob(f"session/*/{sid}.json"):
            meta = _json(info.read_text("utf-8", "replace"))
            directory, parent = meta.get("directory"), meta.get("parentID") or headless_session(meta.get("permission"))
            break
        return list(self._json_units(files, parts_root, sid, directory)) if not parent else [], {loc.key: sig}

    def _json_units(self, files: list[Path], parts_root: Path, sid: str, directory) -> Iterator[Unit]:
        msgs = []
        for f in files:
            info = _json(f.read_text("utf-8", "replace"))
            if info.get("role") in ("user", "assistant"):
                created = (info.get("time") or {}).get("created") if isinstance(info.get("time"), dict) else None
                msgs.append((from_epoch(created), f.stem, info["role"]))
        last_agent = None
        for ts, mid, role in sorted(msgs, key=lambda m: (m[0] is None, m[0], m[1])):
            parts = []
            for pf in sorted((parts_root / mid).glob("*.json")):
                parts.append(_json(pf.read_text("utf-8", "replace")))
            if role == "assistant":
                text = agent_text(parts)
                if text:
                    last_agent = clip_tail(mask_pii(text))
                continue
            text = typed_text(parts)
            if not text or ts is None:
                continue
            u = own_unit(
                source=self.name,
                ref=f"{sid}:{mid}",
                ts=ts,
                text=text,
                kind="prompt",
                register="agent",
                audience="agent",
                thread_id=hash_ref(sid),
                context=[context_line("agent", last_agent)] if last_agent else [],
                meta={"cwd_hash": hash_ref(directory)} if isinstance(directory, str) and directory else {},
            )
            last_agent = None
            if u is not None:
                yield u
