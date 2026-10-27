"""Cline, in both storage formats it has used.

SDK era (CLI 3.x, current VS Code extension): `<data>/sessions/<id>/<id>.messages.json`, where <data> is
$CLINE_DATA_DIR, $CLINE_DIR/data or ~/.cline/data (sessions: $CLINE_SESSION_DATA_DIR). One JSON file per session,
`{messages: [{id, role, content: [blocks], ts (ms), metadata?}], origin, ...}`, rewritten on save. User turns are
wrapped: `<user_input mode="act">text</user_input>`, optionally led by a `<mode_notice>`; a `/command` becomes a
`<user_command slash="x">instructions</user_command>`; an @-mention becomes `[file: name]` (contents ride in separate
file blocks); the runtime also injects `[SYSTEM]` nudges and compaction summaries as user-role messages (those carry
`metadata`). Own text = the unwrapped text blocks of the rest. Sub-agent files (stem different from the session id),
sessions whose `origin.mode` is automation / subagent / team and sessions with a parent are skipped. The manifest's
`interactive` flag is NOT used to drop headless runs: a real multi-turn TUI session (the test fixture) carries
`interactive: false` there, so the flag does not mark `cline -p`. Nothing else in the files does; headless runs are kept.

Classic era (before the SDK): `<data>/tasks/<taskId>/ui_messages.json` and, in older VS Code installs,
`<globalStorage>/saoudrizwan.claude-dev/tasks/<taskId>/ui_messages.json` (Code, Insiders, VSCodium, Cursor, Windsurf,
remote servers). The file is a JSON array of UI rows `{ts (ms), type, say|ask, text}`. Own text = `say: task` (the
first prompt) and `say: user_feedback` rows, kept as typed (mentions are not expanded there). Editing a message
truncates the array and appends again; rows are identified by their `ts`, so the rewrite never double counts.
Sub-agent rows are never read. When the user accepts a `new_task` ask ("start a new task with this context?"), the
agent-written context becomes the next task's first prompt (`say: task`); a first prompt that equals (whitespace
collapsed) the text of a `new_task` ask in an OLDER task of the same folder (scanned newest first) is dropped; the
decision is kept in the cursor. Only first prompts of at least
HANDOFF_MIN_CHARS characters are checked (a hand-off is a long summary), so most reads scan nothing; the index of those
asks is built from the sibling files, hashed, cached per file signature and capped at MAX_HANDOFF_FILES files. The SDK builds do not
have `new_task`; a `new_task` tool call in a sibling session is indexed the same way.

Cursor: {path: [mtime_ns, size]}; a changed file is re-read whole and the engine dedupes by unit_id. The last agent
reply (masked, clipped) is the context line.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from ..core import Location, Me, Unit, context_line, hash_ref
from ._util import (
    clip_tail,
    config_dirs,
    file_sig,
    from_epoch,
    is_relay,
    mask_pii,
    own_unit,
    parse_iso,
    resolved_key,
    sha,
)

MAX_JSON_BYTES = 200 * 1024 * 1024  # a single file this large is skipped, not loaded whole
MAX_HANDOFF_FILES = 400  # sibling files scanned for `new_task` asks
HANDOFF_MIN_CHARS = 200  # a hand-off context is a long summary: shorter first prompts are never checked (no scan)
EXTENSION_ID = "saoudrizwan.claude-dev"
_EDITORS = ("Code", "Code - Insiders", "VSCodium", "Code - OSS", "Cursor", "Windsurf", "Windsurf - Next")
_SERVER_STORAGE = (
    ".vscode-server/data",
    ".vscode-server-insiders/data",
    ".cursor-server/data",
    ".local/share/code-server",
)

_NOT_USER_MODES = {"automation", "subagent", "team"}
_SYNTHETIC_KINDS = {
    "auto_compaction",
    "compaction",
    "compaction_budget_emergency",
    "compaction_summary",
    "completion_reminder",
    "loop_detection_notice",
    "manual_compaction",
    "mistake_stop_notice",
    "recovery_notice",
}
_NOT_TYPED = ("[SYSTEM]", "[TASK RESUMPTION]", "The user approved switching to act mode.")
_MODE_NOTICE = re.compile(r"<mode_notice>.*?</mode_notice>", re.DOTALL)
_USER_COMMAND = re.compile(r"""<user_command\b[^>]*\bslash=["']([^"']+)["'][^>]*>.*?</user_command>""", re.DOTALL)
_USER_INPUT = re.compile(r"<user_input\b[^>]*>(.*?)</user_input>", re.DOTALL)
_FILE_BLOCK = re.compile(r"<file_content\b[^>]*>.*?</file_content>", re.DOTALL)
_FILE_MENTION = re.compile(r"\[file: ([^\]\n]+)\]")
_IMAGE_MENTION = re.compile(r"\[image: [^\]\n]*\]")
_SLASH_ONLY = re.compile(r"^/[\w:.-]+$")


# --- paths ---------------------------------------------------------------------------------------------------------


def data_dirs(extra=None) -> list[Path]:
    """Cline data directories: $CLINE_DATA_DIR, else $CLINE_DIR/data; plus ~/.cline/data and any configured extras."""
    default = Path.home() / ".cline" / "data"
    env_data = (os.environ.get("CLINE_DATA_DIR") or "").strip()
    env_dir = (os.environ.get("CLINE_DIR") or "").strip()
    if env_data:
        primary = Path(env_data)
    elif env_dir:
        primary = Path(env_dir) / "data"
    else:
        primary = default
    return config_dirs(primary.expanduser(), default, extra)


def global_storage_dirs(platform: str) -> list[Path]:
    """VS Code-family `User/globalStorage` directories for an OS (`sys.platform` value), plus remote-server layouts."""
    home = Path.home()
    if platform == "darwin":
        base = home / "Library" / "Application Support"
    elif platform.startswith("win"):
        base = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    out = [base / e / "User" / "globalStorage" for e in _EDITORS]
    out += [home / s / "User" / "globalStorage" for s in _SERVER_STORAGE]
    return out


# --- text ----------------------------------------------------------------------------------------------------------


def sdk_text(msg: dict) -> str:
    """Typed text of one SDK-era user message ("" for runtime-injected ones)."""
    meta = msg.get("metadata") if isinstance(msg.get("metadata"), dict) else {}
    if meta.get("kind") in _SYNTHETIC_KINDS or meta.get("displayRole") in ("system", "status", "error"):
        return ""
    span = meta.get("userRunSpan")
    if span == 0 and not isinstance(span, bool):
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        raw = content
    elif isinstance(content, list):
        raw = "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        )
    else:
        return ""
    text = _MODE_NOTICE.sub("", raw)
    text = _USER_COMMAND.sub(lambda m: "/" + m.group(1), text)
    text = _USER_INPUT.sub(lambda m: m.group(1), text)
    text = _FILE_BLOCK.sub("", text)
    text = _IMAGE_MENTION.sub("", _FILE_MENTION.sub(lambda m: "@" + m.group(1).strip(), text)).strip()
    return "" if text.startswith(_NOT_TYPED) else text


def agent_text(msg: dict) -> str:
    content = msg.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "\n".join(
            b["text"]
            for b in content
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
        ).strip()
    return ""


def _question(text: str) -> str:
    """The question of a classic `ask: followup` row (its text is JSON)."""
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    return obj.get("question", "") if isinstance(obj, dict) and isinstance(obj.get("question"), str) else ""


# --- the adapter ---------------------------------------------------------------------------------------------------


def _load(path: Path):
    if path.stat().st_size > MAX_JSON_BYTES:
        return None
    return json.loads(path.read_text("utf-8", "replace"))


def _flat(text: str) -> str:
    return " ".join(text.split())


_HANDOFFS: dict[str, tuple[list[int], frozenset[str]]] = {}  # file -> (signature, hashes of its new_task texts)


def _file_handoffs(path: Path) -> frozenset[str]:
    """Hashes of the `new_task` texts in one task / session file (cached per signature)."""
    try:
        sig = file_sig(path)
        cached = _HANDOFFS.get(str(path))
        if cached and cached[0] == sig:
            return cached[1]
        if sig[1] > MAX_JSON_BYTES or b"new_task" not in path.read_bytes():
            _HANDOFFS[str(path)] = (sig, frozenset())  # no new_task anywhere in the file: nothing to parse
            return frozenset()
        data = _load(path)
    except (OSError, ValueError):
        return frozenset()
    texts: list[str] = []
    if isinstance(data, list):  # classic: {type: ask, ask: new_task, text}
        for row in data:
            if isinstance(row, dict) and row.get("type") == "ask" and row.get("ask") == "new_task":
                if isinstance(row.get("text"), str):
                    texts.append(row["text"])
    elif isinstance(data, dict):  # SDK: an assistant `new_task` tool call
        for msg in data.get("messages") if isinstance(data.get("messages"), list) else []:
            blocks = msg.get("content") if isinstance(msg, dict) else None
            for b in blocks if isinstance(blocks, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "new_task":
                    args = b.get("input") if isinstance(b.get("input"), dict) else {}
                    texts += [v for v in args.values() if isinstance(v, str)]
    out = set()
    for t in texts:
        out.add(sha(_flat(t)))
        try:  # the ask text may be JSON ({"context": ...}); the context is what the new task starts with
            obj = json.loads(t)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.update(sha(_flat(v)) for v in obj.values() if isinstance(v, str))
    found = frozenset(out)
    _HANDOFFS[str(path)] = (sig, found)
    return found


def _order(path: Path) -> tuple[int, str]:
    """Creation order of a task / session folder: both id formats start with the start time in milliseconds."""
    m = re.match(r"\d+", path.parent.name)
    return (int(m.group()) if m else -1, path.parent.name)


def _is_handoff(path: Path, first_prompt: str, memo: dict) -> bool:
    """True when `first_prompt` is the context a `new_task` ask of an OLDER sibling task / session carried.

    Siblings are scanned newest first (the source is almost always the task right before), stopping at the first
    match, at most MAX_HANDOFF_FILES of them. The answer is kept in `memo["handoff"]` (it goes into the cursor): the
    first prompt of a task never changes, so a later sync does not scan again."""
    if len(first_prompt) < HANDOFF_MIN_CHARS:
        return False
    digest = sha(_flat(first_prompt))
    known = memo.get("handoff")
    if isinstance(known, list) and len(known) == 2 and known[0] == digest:
        return bool(known[1])
    pattern = "*/ui_messages.json" if path.name == "ui_messages.json" else "*/*.messages.json"
    found = False
    try:
        me = _order(path)
        older = sorted((p for p in path.parent.parent.glob(pattern) if p != path and _order(p) < me), key=_order)
        for other in reversed(older[-MAX_HANDOFF_FILES:]):
            if digest in _file_handoffs(other):
                found = True
                break
    except OSError:
        return False  # could not look: not remembered, the next sync tries again
    memo["handoff"] = [digest, found]
    return found


def _sdk_session_files(sessions: Path) -> Iterator[Path]:
    for p in sorted(sessions.glob("*/*.messages.json")):
        if p.name == f"{p.parent.name}.messages.json":  # sub-agent / team files carry another stem
            yield p


def _manifest_is_user_session(messages_path: Path) -> bool:
    """False for sessions the manifest marks as child or automated runs. A missing/odd manifest does not exclude."""
    manifest = messages_path.with_name(messages_path.name.removesuffix(".messages.json") + ".json")
    try:
        data = json.loads(manifest.read_text("utf-8", "replace"))
    except (OSError, ValueError):
        return True
    if not isinstance(data, dict):
        return True
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    origin = meta.get("sessionHistoryOrigin") if isinstance(meta.get("sessionHistoryOrigin"), dict) else {}
    if data.get("parent_session_id") or origin.get("trigger") or origin.get("mode") in _NOT_USER_MODES:
        return False
    return True


class Cline:
    name = "cline"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            files: dict[
                str, tuple[Path, str]
            ] = {}  # resolved path -> (path, label); a task copied between stores is read once
            session_roots = [d / "sessions" for d in data_dirs((config or {}).get("dirs"))]
            env_sessions = (os.environ.get("CLINE_SESSION_DATA_DIR") or "").strip()
            if env_sessions:
                session_roots.insert(0, Path(env_sessions).expanduser())
            for root in session_roots:
                for p in _sdk_session_files(root):
                    files.setdefault(resolved_key(p), (p, f"Cline session {p.parent.name[-8:]}"))
            task_roots = [d / "tasks" for d in data_dirs((config or {}).get("dirs"))]
            task_roots += [g / EXTENSION_ID / "tasks" for g in global_storage_dirs(sys.platform)]
            for root in task_roots:
                for p in sorted(root.glob("*/ui_messages.json")):
                    files.setdefault(resolved_key(p), (p, f"Cline task {p.parent.name[-8:]}"))
            return [Location(self.name, key, p, label) for key, (p, label) in files.items()]
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            sig = file_sig(loc.path)
            if cursor.get(loc.key) == sig:
                return [], cursor
            data = _load(loc.path)
        except (OSError, ValueError):
            return [], cursor  # missing or half-written: the cursor stays, the next sync tries again
        if data is None:
            return [], {loc.key: sig, "note": "skipped: file over the size limit was not read"}
        memo = {"handoff": cursor["handoff"]} if cursor.get("handoff") else {}
        if loc.path.name == "ui_messages.json":
            units = list(self._classic(loc, data, memo))
        elif _manifest_is_user_session(loc.path):
            units = list(self._sdk(loc, data, memo))
        else:
            units = []
        return units, {loc.key: sig, **({"handoff": memo["handoff"]} if "handoff" in memo else {})}

    def _sdk(self, loc: Location, data, memo: dict) -> Iterator[Unit]:
        if not isinstance(data, dict):
            return
        origin = data.get("origin") if isinstance(data.get("origin"), dict) else {}
        if origin.get("mode") in _NOT_USER_MODES:
            return
        messages = data.get("messages")
        if not isinstance(messages, list):
            return
        sid = str(data.get("sessionId") or loc.path.parent.name)
        fallback = parse_iso(data.get("updated_at")) or datetime.fromtimestamp(loc.path.stat().st_mtime, UTC)
        last_agent = None
        first = True
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "assistant":
                text = agent_text(msg)
                if text:
                    last_agent = clip_tail(mask_pii(text))
                continue
            if msg.get("role") != "user":
                continue
            text = sdk_text(msg)
            if not text or _SLASH_ONLY.match(text) or is_relay(text):
                continue
            if first:
                first = False
                if _is_handoff(loc.path, text, memo):
                    continue  # the context a `new_task` call handed over, not typed
            mid = msg.get("id")
            u = own_unit(
                source=self.name,
                ref=f"{sid}:{mid}" if isinstance(mid, str) and mid else f"{sid}:{i}",
                ts=from_epoch(msg.get("ts")) or fallback,
                text=text,
                kind="prompt",
                register="agent",
                audience="agent",
                thread_id=hash_ref(sid),
                context=[context_line("agent", last_agent)] if last_agent else [],
            )
            last_agent = None
            if u is not None:
                yield u

    def _classic(self, loc: Location, data, memo: dict) -> Iterator[Unit]:
        if not isinstance(data, list):
            return
        tid = loc.path.parent.name
        last_agent = None
        first = True
        for row in data:
            if not isinstance(row, dict) or row.get("partial") or not isinstance(row.get("text"), str):
                continue
            kind, text = row.get("type"), row["text"].strip()
            if kind == "say" and row.get("say") in ("text", "completion_result") and text:
                last_agent = clip_tail(mask_pii(text))
            elif kind == "ask" and row.get("ask") == "followup" and _question(text):
                last_agent = clip_tail(mask_pii(_question(text)))
            if not (kind == "say" and row.get("say") in ("task", "user_feedback")) or not text or is_relay(text):
                continue
            ts = from_epoch(row.get("ts"))
            if ts is None:
                continue
            if first and row.get("say") == "task":
                first = False
                if _is_handoff(loc.path, text, memo):
                    continue  # the context a `new_task` ask handed over, not typed
            u = own_unit(
                source=self.name,
                ref=f"{tid}:{row['ts']}",
                ts=ts,
                text=text,
                kind="prompt",
                register="agent",
                audience="agent",
                thread_id=hash_ref(tid),
                context=[context_line("agent", last_agent)] if last_agent else [],
            )
            last_agent = None
            if u is not None:
                yield u
