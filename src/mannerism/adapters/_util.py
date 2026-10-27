"""Helpers shared by the source adapters (standard library only)."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable, Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path

from ..core import (
    CONTEXT_CHARS,
    CONTEXT_LINES,
    ContextLine,
    Location,
    Unit,
    clean_label,
    context_line,
    hash_ref,
    home,
    home_label,
    looks_pasted,
    mask_names,
    mask_pii,
    merge_bursts,
)

_URL = re.compile(r"^(?:https?://|www\.)\S+$", re.IGNORECASE)


# --- paths ---------------------------------------------------------------------------------------------------------


def claude_dir() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def codex_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def config_dirs(env_dir: Path, default: Path, extra) -> list[Path]:
    """The env-configured dir, the default dir and any configured extras, deduped.

    A CLAUDE_CONFIG_DIR / CODEX_HOME set in one shell (a wrapper, a second account) does not mean the default
    directory's history stopped being the user's, so both are read."""
    out, seen = [], set()
    for d in (env_dir, default, *(Path(str(x)).expanduser() for x in extra or [])):
        try:
            key = str(d.resolve())
        except (OSError, RuntimeError):  # RuntimeError: a symlink loop on Python 3.11
            continue
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out


def resolved_key(p: Path) -> str:
    """`str(p.resolve())`, or `str(p)` when resolving fails (OSError, or a RuntimeError symlink loop on Python 3.11)."""
    try:
        return str(p.resolve())
    except (OSError, RuntimeError):
        return str(p)


def inbox_dir(source: str) -> Path:
    return home() / "inbox" / source


def configured_paths(config: dict | None) -> list[Path]:
    out = []
    for p in (config or {}).get("paths", []) or []:
        try:
            out.append(Path(str(p)).expanduser())
        except (TypeError, ValueError):
            continue
    return out


def export_candidates(source: str, config: dict | None, suffixes: Sequence[str]) -> list[Path]:
    """Files an export source may read: configured paths (files, or directories searched two levels deep) plus the
    contents of ~/.mannerism/inbox/<source>/. Creates nothing. Sorted and de-duplicated."""
    roots = configured_paths(config) + [inbox_dir(source)]
    seen: dict[str, Path] = {}
    for root in roots:
        try:
            if root.is_file():
                if root.suffix.lower() in suffixes:
                    seen[str(root.resolve())] = root
            elif root.is_dir():
                for depth in ("*", "*/*"):
                    for p in root.glob(depth):
                        if p.is_file() and p.suffix.lower() in suffixes:
                            seen[str(p.resolve())] = p
        except OSError:
            continue
    return [seen[k] for k in sorted(seen)]


def file_sig(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_mtime_ns, st.st_size]


def db_sig(path: Path) -> list[list[int]]:
    """Signature of a SQLite file plus its -wal (when there is one). Raises OSError if the database itself is gone."""
    sig = [file_sig(path)]
    try:
        sig.append(file_sig(Path(str(path) + "-wal")))
    except OSError:
        pass
    return sig


# --- relay / notification wrappers (not written by the user) ------------------------------------------------------

RELAY_PREFIXES = ("<task-notification>", "<agent-message", "<cross-session-message", "[SYSTEM NOTIFICATION")
RELAY_CONTAINS = ("Subagent hand-back",)
# Harness wrappers are snake/kebab-case tags (<codex_internal_context>, <subagent_notification>, ...). People rarely
# open a typed message with one, so text that starts with such a tag is treated as injected, not written.
_WRAPPER_TAG = re.compile(r"^<[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+(?:\s[^>]*)?>")


# The built-in /init and /review commands of OpenCode and Kilo Code are stored as their expanded template (the typed
# arguments are substituted into it), with no marker. Prefixes of those templates (everything before the first
# argument slot, whitespace collapsed), from opencode core/src/plugin/command/*.txt and kilo
# packages/opencode/src/{command/template,kilocode/review}/*.txt.
BUILTIN_TEMPLATES = tuple(
    " ".join(t.split())
    for t in (
        "Create or update `AGENTS.md` for this repository.\n\nThe goal is a compact instruction file that helps future ",
        "You are a code reviewer. Your job is to review code changes and provide actionable feedback.\n\n---\n\nInput:",
        "You are Kilo Code, an expert code reviewer focused on high-confidence security",
    )
)


def is_builtin_template(text: str) -> bool:
    """True for the expansion of an OpenCode / Kilo built-in command (`/init`, `/review`): not typed text."""
    return " ".join(text.split()).startswith(BUILTIN_TEMPLATES)


def headless_session(permission) -> bool:
    """True when a session's `permission` ruleset is the one `opencode run` sets (the question tool denied)."""
    try:
        rules = json.loads(permission) if isinstance(permission, (str, bytes)) else permission
    except ValueError:
        return False
    return isinstance(rules, list) and any(
        isinstance(r, dict) and r.get("permission") == "question" and r.get("action") == "deny" for r in rules
    )


def is_relay(text: str) -> bool:
    """True for text that agents or the harness relayed into a prompt (notifications, hand-backs, peer messages)."""
    head = text.lstrip()
    return head.startswith(RELAY_PREFIXES) or bool(_WRAPPER_TAG.match(head)) or any(s in text for s in RELAY_CONTAINS)


# --- text hygiene --------------------------------------------------------------------------------------------------


def is_pure_url(text: str) -> bool:
    toks = text.split()
    return bool(toks) and all(_URL.match(t) for t in toks)


def clip_tail(text: str, limit: int = CONTEXT_CHARS) -> str:
    """Whitespace-collapsed text, keeping the END when it is too long (the question is usually last)."""
    text = " ".join(text.split())
    return text if len(text) <= limit else "…" + text[-(limit - 1) :]


def own_unit(
    *,
    source: str,
    ref: str,
    ts: datetime,
    text: str,
    kind: str,
    register: str,
    audience: str,
    thread_id: str | None = None,
    audience_ref: str | None = None,
    context: Sequence[ContextLine] = (),
    meta: dict | None = None,
    parts: int = 1,
    account: str | None = None,
    project: str | None = None,
    project_path: str | None = None,
    session_kind: str | None = None,
    session: str | None = None,
    gap_s: float | None = None,
) -> Unit | None:
    """Build a masked Unit from own text, or None for empty / pure-URL text. Pastes get register `skip`.

    `account` / `project_path` are paths ("~" for the home directory is applied here), `project` the working
    directory's basename, `session_kind` the adapter's label for how the session was driven. All are masked."""
    if not text or not text.strip() or is_pure_url(text):
        return None
    if looks_pasted(text):
        register = "skip"
    masked = mask_pii(text.strip())
    if not masked.strip():
        return None
    return Unit(
        source=source,
        source_ref=ref,
        ts=ts,
        text=masked,
        kind=kind,
        register=register,
        thread_id=thread_id,
        audience=audience,
        audience_ref=audience_ref,
        context=tuple(context[-CONTEXT_LINES:]),
        meta=meta or {},
        parts=parts,
        account=home_label(account),
        project=clean_label(project),
        project_path=home_label(project_path),
        session_kind=clean_label(session_kind),
        session=session,
        gap_s=gap_s,
    )


def project_of(cwd) -> tuple[str | None, str | None]:
    """(basename, path) labels for a working directory; (None, None) when there is none."""
    if not isinstance(cwd, str) or not cwd.strip():
        return None, None
    name = cwd.rstrip("/\\").replace("\\", "/").rsplit("/", 1)[-1] or cwd
    return name, cwd


def parse_iso(value) -> datetime | None:
    """ISO-8601 (with `Z`) -> aware datetime; naive values are taken as UTC."""
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def from_epoch(value) -> datetime | None:
    """Epoch seconds or milliseconds (number or numeric string) -> aware UTC datetime."""
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if x > 1e12:
        x /= 1000.0
    if x <= 0:
        return None
    try:
        return datetime.fromtimestamp(x, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def sha(text: str, n: int = 12) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:n]


# --- append-only files ---------------------------------------------------------------------------------------------


def _head(path: Path, upto: int) -> str:
    with open(path, "rb") as f:
        return hashlib.sha1(f.read(min(upto, 64))).hexdigest()


READ_BUDGET = 8 * 1024 * 1024  # P2-11: a single location reads at most this many new bytes per sync call


def read_new_lines(path: Path, cursor: dict | None, budget: int = READ_BUDGET) -> tuple[list[str], dict]:
    """New COMPLETE lines of an append-only file since `cursor`, up to `budget` new bytes, and the new cursor.

    The cursor holds byte offset + inode + size + a hash of the first bytes. If the inode changed, the file shrank or
    its head changed, reading restarts at 0 (the engine dedupes by unit_id). A trailing partial line is not consumed.
    A file that grew by more than `budget` since the last sync is simply not fully caught up in one call: the new
    offset still lands on a complete line, so the next sync's read continues cleanly from there (P2-11 -- a
    transcript that grew huge between syncs must not force one call to hold it all in memory at once)."""
    st = path.stat()
    cursor = cursor or {}
    off = 0
    try:
        prev = int(cursor.get("offset") or 0)
        if prev and cursor.get("inode") == st.st_ino and st.st_size >= prev and cursor.get("head") == _head(path, prev):
            off = prev
    except (OSError, TypeError, ValueError):
        off = 0
    oversized = 0
    with open(path, "rb") as f:
        f.seek(off)
        data = f.read(budget)
        cut = data.rfind(b"\n")
        if cut < 0 and len(data) == budget:
            # One line longer than the whole budget (a giant tool result): skip past it, or the file never advances.
            pos = off + len(data)
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    break
                i = chunk.find(b"\n")
                if i >= 0:
                    pos += i + 1
                    oversized = 1
                    break
                pos += len(chunk)
            if oversized:
                data, cut = b"", -1
                off_after = pos
    consumed = data[: cut + 1] if cut >= 0 else b""
    lines = [ln.decode("utf-8", "replace") for ln in consumed.split(b"\n")[:-1]] if consumed else []
    new_off = off_after if oversized else off + len(consumed)
    new = {
        "offset": new_off,
        "start": off,
        "inode": st.st_ino,
        "size": st.st_size,
        "head": _head(path, new_off) if new_off else "",
        # More complete lines may follow: the read stopped at the byte budget, not at the end of the file.
        "more": bool(oversized or (len(data) == budget and new_off < st.st_size)),
    }
    if oversized:
        new["oversized_lines"] = int(cursor.get("oversized_lines") or 0) + 1
    return lines, new


# --- agent transcripts ---------------------------------------------------------------------------------------------


def newest_transcripts(limit: int = 200) -> list[Path]:
    """Newest Claude Code and Codex transcript files (by mtime), at most `limit` in total."""
    files: list[tuple[float, Path]] = []
    pats = [*((d / "projects", "*/*.jsonl") for d in config_dirs(claude_dir(), Path.home() / ".claude", ())),
            *((d / "sessions", "*/*/*/rollout-*.jsonl") for d in config_dirs(codex_dir(), Path.home() / ".codex", ()))]
    for base, pat in pats:
        try:
            for p in base.glob(pat):
                try:
                    files.append((p.stat().st_mtime, p))
                except OSError:
                    continue
        except OSError:
            continue
    files.sort(key=lambda t: t[0], reverse=True)
    return [p for _, p in files[:limit]]


def transcript_cwds(limit: int = 200, first_lines: int = 20) -> list[str]:
    """Distinct cwd values from the first lines of the newest transcripts (newest first)."""
    out: list[str] = []
    seen: set[str] = set()
    for p in newest_transcripts(limit):
        try:
            with open(p, "rb") as f:
                for i, raw in enumerate(f):
                    if i >= first_lines:
                        break
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    pl = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    cwd = obj.get("cwd") or pl.get("cwd")
                    if isinstance(cwd, str) and cwd and cwd not in seen:
                        seen.add(cwd)
                        out.append(cwd)
                        break
        except OSError:
            continue
    return out


# --- chats ---------------------------------------------------------------------------------------------------------


def build_chat_units(
    *,
    source: str,
    thread: str,
    messages: Iterable[tuple[datetime, str, bool, str, str, str]],
    audience: str,
    register: str,
    kind: str = "message",
    names: dict[str, str] | None = None,
    meta: dict | None = None,
    old=None,
) -> Iterator[Unit]:
    """Own-text units from one chat.

    `messages`: (ts, sender_key, is_own, ref, text, sender_name) in time order. Text-less messages should be left out.
    Other parties become `other1`, `other2`… in context; their names are masked. Own bursts are merged
    (core.merge_bursts).
    `old(ref)` -> True marks an already-ingested message (incremental reads): it stays available as context and ends any
    burst, but never becomes part of a new unit."""
    roles: dict[str, str] = {}
    namemap: dict[str, str] = dict(names or {})
    ctx: list[tuple[str, str]] = []
    own_pos: dict[str, int] = {}
    events: list[tuple[bool, datetime, str, str]] = []
    for ts, sender, is_own, ref, text, sname in messages:
        if is_own:
            own_pos[ref] = len(ctx)
            ctx.append(("me", text))
            events.append((not (old and old(ref)), ts, ref, text))
        else:
            role = roles.setdefault(sender, f"other{len(roles) + 1}")
            if sname:
                namemap[sname] = role
            ctx.append((role, text))
            events.append((False, ts, ref, ""))
    for first_ts, text, refs in merge_bursts(events):
        pos = own_pos[refs[0]]
        context = [context_line(who, t, namemap) for who, t in ctx[max(0, pos - CONTEXT_LINES) : pos]]
        # own earlier lines are plain "me" context; mask names there too
        ref = refs[0] if len(refs) == 1 else f"{refs[0]}..{refs[-1]}"
        u = own_unit(
            source=source,
            ref=ref,
            ts=first_ts,
            text=text,
            kind=kind,
            register=register,
            audience=audience,
            thread_id=thread,
            audience_ref=thread,
            context=context,
            meta=dict(meta or {}),
            parts=len(refs),
        )
        if u is not None:
            yield u


# --- sqlite --------------------------------------------------------------------------------------------------------


def _copy_stamp(path: Path):
    """What a consistent copy must not see change: the main file's size and mtime and the first 32 bytes of the -wal.

    New WAL frames are only appended (a harmless difference: SQLite ignores frames it cannot validate), so the WAL's size
    is left out. A checkpoint changes the main file, and a restarted WAL gets new salt values in its header."""
    try:
        with open(str(path) + "-wal", "rb") as f:
            head = f.read(32)
    except OSError:
        head = b""
    return file_sig(path), head


class DbCopy:
    """Context manager: a private temp copy of a SQLite file (+ -wal/-shm) opened for reading only by us.

    The app's own database is never opened, written or locked. The main-file signature and the WAL header are taken
    before and after the copy; if they moved (the app checkpointed mid-copy) the copy is repeated, up to 3 times, so a
    torn copy is not read. `sig` is the db + -wal signature (`db_sig`) from before the last copy: a cursor that stores
    it re-reads whatever changed after it."""

    ATTEMPTS = 3

    def __init__(self, path: Path):
        self.path = path
        self._tmp = None
        self.con = None
        self.sig: list = []

    def __enter__(self):
        import sqlite3

        self._tmp = tempfile.mkdtemp(prefix="mannerism-")
        dst = Path(self._tmp) / "copy.db"
        for _ in range(self.ATTEMPTS):
            before = _copy_stamp(self.path)
            self.sig = db_sig(self.path)
            for ext in ("-wal", "-shm"):
                Path(str(dst) + ext).unlink(missing_ok=True)
            shutil.copyfile(self.path, dst)
            for ext in ("-wal", "-shm"):
                side = Path(str(self.path) + ext)
                if side.exists():
                    shutil.copyfile(side, Path(str(dst) + ext))
            if _copy_stamp(self.path) == before:
                break
        self.con = sqlite3.connect(str(dst))
        self.con.execute("PRAGMA query_only=1")
        return self.con

    def __exit__(self, *exc):
        try:
            if self.con is not None:
                self.con.close()
        finally:
            if self._tmp:
                shutil.rmtree(self._tmp, ignore_errors=True)


# --- rowid cursors -------------------------------------------------------------------------------------------------
# A plain "last rowid" cursor loses rows twice over: tables with a TEXT primary key reuse rowids once the tail is
# deleted (OpenCode /undo), and a database that was deleted and recreated can grow past the old maximum. So the cursor
# keeps the identity of the row at that rowid and starts over (refs dedupe re-reads) when the row there changed.


def rowid_mark(con, table: str, ident_sql: str) -> dict:
    """Cursor value for `table`: the newest rowid and the identity (an SQL expression) of the row that holds it."""
    row = con.execute(f'SELECT rowid, {ident_sql} FROM "{table}" ORDER BY rowid DESC LIMIT 1').fetchone()
    return {"rowid": int(row[0]), "id": str(row[1])} if row else {"rowid": 0, "id": ""}


def rowid_resume(con, table: str, ident_sql: str, saved) -> int:
    """The rowid to read after: the saved one if the same row still sits there, else 0 (read everything again)."""
    if not isinstance(saved, dict):
        return 0
    try:
        rid = int(saved.get("rowid") or 0)
    except (TypeError, ValueError):
        return 0
    if rid <= 0:
        return 0
    row = con.execute(f'SELECT {ident_sql} FROM "{table}" WHERE rowid = ?', (rid,)).fetchone()
    return rid if row is not None and str(row[0]) == str(saved.get("id")) else 0


def table_columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


__all__ = ["Location", "hash_ref", "mask_names", "mask_pii"]
