"""Pi coding agent sessions: ~/.pi/agent/sessions/--<cwd-slug>--/<timestamp>_<sessionId>.jsonl.

The agent directory is $PI_CODING_AGENT_DIR when set, else ~/.pi/agent (`[pi].dirs` adds more agent directories). A
custom session directory ($PI_CODING_AGENT_SESSION_DIR, or `sessionDir` in the agent directory's settings.json) holds the
files directly or one folder down. Same layout on every OS, under the user's home directory.

The first line is a header (`type=session`: id, cwd, version, and `parentSession` for a fork). Every other line is an
entry with `id`, `parentId` and an ISO `timestamp`; `type=message` entries carry an `AgentMessage` with a `role`.
Own text = `role=user` messages (a string or `text` blocks). Dropped: `custom` / `bashExecution` / system messages,
extension entries, `/skill:name` expansions (the skill body between `<skill ...>` tags; the words typed after it stay),
and a leading `/word` that Pi did not recognise as a command. A prompt-template command (`/review file`) is expanded
before it is stored, and the record does not say so; that text cannot be told from typed text.

A session file only appears with its first reply and is appended to after that. `/tree` branches live in the same file
and every branch is the user's own text, so all are read. `/fork` and `/clone` copy the earlier entries into a new file
with the same ids and timestamps, so a unit's ref is built from the entry id and timestamp only (not the session): the
copies dedupe. Pi has no built-in sub-agents; extensions that spawn them run with `--no-session` and leave no file.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

from ..core import Location, Me, Unit, context_line, hash_ref
from ._util import clip_tail, config_dirs, from_epoch, is_relay, mask_pii, own_unit, parse_iso, read_new_lines

_SKILL = re.compile(r'^<skill name="[^"]+" location="[^"]+">\n.*?\n</skill>(?:\n\n(.+))?\Z', re.DOTALL)
_UNKNOWN_COMMAND = re.compile(r"^/[A-Za-z][\w:.-]*(?:\s|\Z)")


def agent_dir() -> Path:
    env = os.environ.get("PI_CODING_AGENT_DIR")
    return Path(env).expanduser() if env else Path.home() / ".pi" / "agent"


def session_files(base: Path) -> list[Path]:
    """Session files of one agent directory: its sessions/ folder, plus a custom session directory if one is set."""
    roots = [base / "sessions"]
    custom = os.environ.get("PI_CODING_AGENT_SESSION_DIR")
    if not custom:
        try:
            custom = json.loads((base / "settings.json").read_text("utf-8")).get("sessionDir")
        except (OSError, ValueError, AttributeError):
            custom = None
    if isinstance(custom, str) and custom.strip():
        roots.append(Path(custom).expanduser())
    out: dict[str, Path] = {}
    for root in roots:
        for pat in ("*/*.jsonl", "*.jsonl"):
            for p in root.glob(pat):
                out[str(p.resolve())] = p
    return [out[k] for k in sorted(out)]


def block_texts(content) -> list[str]:
    """Text of `text` blocks (or a plain string); images and tool calls are ignored."""
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)]


def own_prompt_text(content) -> str:
    text = "\n\n".join(t for t in block_texts(content) if t.strip()).strip()
    m = _SKILL.match(text)
    if m:
        text = (m.group(1) or "").strip()
    if is_relay(text):
        return ""
    # `/word ...` that reached the model is a command Pi did not know; a path ("/etc/hosts is wrong") is prose.
    if _UNKNOWN_COMMAND.match(text) and "/" not in text.split()[0][1:]:
        return ""
    return text


class Pi:
    name = "pi"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            out = []
            seen: set[str] = set()
            for base in config_dirs(agent_dir(), Path.home() / ".pi" / "agent", (config or {}).get("dirs")):
                for p in session_files(base):
                    key = str(p.resolve())
                    if key not in seen:
                        seen.add(key)
                        out.append(Location(self.name, str(p), p, f"Pi session {p.stem.rsplit('_', 1)[-1][:8]}"))
            return out
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            lines, new = read_new_lines(loc.path, cursor)
        except OSError:
            return [], cursor
        resumed = bool(cursor) and new["start"] == int(cursor.get("offset", 0))
        sid = cursor.get("sid") if resumed else None
        cwd_hash = cursor.get("cwd_hash") if resumed else None
        last_agent = cursor.get("last_agent") if resumed else None
        skipped = int(cursor.get("skipped", 0))
        units: list[Unit] = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                continue
            if obj.get("type") == "session":
                sid = str(obj.get("id") or sid or "") or sid
                if isinstance(obj.get("cwd"), str) and obj["cwd"]:
                    cwd_hash = hash_ref(obj["cwd"])
                continue
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            if obj.get("type") != "message":
                continue
            if msg.get("role") == "assistant":
                txt = " ".join(block_texts(msg.get("content"))).strip()
                if txt:
                    last_agent = clip_tail(mask_pii(txt))  # masked before it sits in the cursor row (P1-7)
                continue
            if msg.get("role") != "user":
                continue
            text = own_prompt_text(msg.get("content"))
            if not text:
                continue
            ts = parse_iso(obj.get("timestamp")) or from_epoch(msg.get("timestamp"))
            if ts is None:
                skipped += 1
                continue
            sid = sid or loc.path.stem.rsplit("_", 1)[-1]
            ref = f"{obj['id']}@{obj.get('timestamp')}" if obj.get("id") else f"{sid}:{ts.isoformat()}"
            ctx = [context_line("agent", last_agent)] if last_agent else []
            u = own_unit(
                source=self.name,
                ref=ref,
                ts=ts,
                text=text,
                kind="prompt",
                register="agent",
                audience="agent",
                thread_id=hash_ref(sid),
                context=ctx,
                meta={"cwd_hash": cwd_hash} if cwd_hash else {},
            )
            last_agent = None
            if u is not None:
                units.append(u)
        new.update(sid=sid, cwd_hash=cwd_hash, last_agent=last_agent, skipped=skipped)
        return units, new
