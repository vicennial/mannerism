"""Claude Code transcripts: ~/.claude/projects/<cwd-slug>/<sessionId>.jsonl (or $CLAUDE_CONFIG_DIR/projects).

Own text = user lines (`type=user`, `message.role=user`) that are not meta / sidechain / compaction summaries, text
blocks only.
Tool results, images, attachments, hooks, slash-command wrappers, pasted blocks (`<pasted_content>`), relayed
notifications
and lines whose origin metadata says a human did not type them (task notifications, SDK, peer, scheduled) are dropped.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from .. import hookroots
from ..core import Location, Me, Unit, context_line, hash_ref, home_label
from ._util import (
    claude_dir,
    clip_tail,
    config_dirs,
    is_relay,
    mask_pii,
    own_unit,
    parse_iso,
    project_of,
    read_new_lines,
)

_WRAPPERS = (
    "<command-name>",
    "<command-message>",
    "<command-args>",
    "<local-command-stdout>",
    "<local-command-stderr>",
    "<local-command-caveat>",
    "<system-reminder>",
    "<bash-input>",
    "<bash-stdout>",
    "<bash-stderr>",
    "<task-notification>",
    "<user-prompt-submit-hook>",
    "Caveat:",
    "[Request interrupted",
)
_COMPACT = "This session is being continued from a previous conversation"
_EMBEDDED = re.compile(r"<(system-reminder|ide_opened_file|ide_selection)>.*?(?:</\1>|\Z)", re.DOTALL)
_PASTED = re.compile(r"<pasted_content\b[^>]*>.*?(?:</pasted_content>|\Z)", re.DOTALL)
_NOT_HUMAN_TURN = {"task_notification", "sdk", "peer", "scheduled"}
_NOT_HUMAN_SOURCE = {"system", "sdk"}


def not_human_line(obj: dict) -> bool:
    """True when the line's origin metadata says a human did not type it. Absent fields (older versions) never drop."""
    origin = obj.get("origin")
    if isinstance(origin, dict) and origin.get("kind") not in (None, "human"):
        return True
    return (
        obj.get("turnOrigin") in _NOT_HUMAN_TURN
        or obj.get("promptSource") in _NOT_HUMAN_SOURCE
        or obj.get("entrypoint") == "sdk-cli"
    )


def block_texts(content) -> list[str]:
    """Text of `text` blocks (or a plain string); everything else (tool_result, image, tool_use) is ignored."""
    if isinstance(content, str):
        return [content]
    out = []
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str):
                out.append(b["text"])
            elif isinstance(b, str):
                out.append(b)
    return out


def own_prompt_text(content) -> str:
    parts = []
    for t in block_texts(content):
        t = _PASTED.sub("", _EMBEDDED.sub("", t)).strip()
        if not t or t.startswith(_WRAPPERS) or t.startswith(_COMPACT) or is_relay(t):
            continue
        parts.append(t)
    return "\n\n".join(parts)


_ENTRYPOINT_KINDS = {"cli": "interactive", "claude-vscode": "ide", "claude-jetbrains": "ide"}
PROMPT_KIND_BYTES = 256 * 1024


def session_kind_of(obj: dict) -> str:
    """How the session was driven, from the line's `entrypoint`: the terminal UI is "interactive", an editor
    extension is "ide", any other entrypoint keeps its own name. A line without one (older versions) is "interactive"."""
    ep = obj.get("entrypoint")
    if not isinstance(ep, str) or not ep:
        return "interactive"
    return _ENTRYPOINT_KINDS.get(ep, ep.lower())


def prompt_session_kind(path) -> str | None:
    """The most recent user line's session kind for a prompt hook, from a bounded transcript tail."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - PROMPT_KIND_BYTES))
            raw = f.read(PROMPT_KIND_BYTES)
    except OSError:
        return None
    lines = raw.splitlines()
    if size > PROMPT_KIND_BYTES and lines:
        lines = lines[1:]  # the first line can be a partial JSON object
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if (isinstance(obj, dict) and obj.get("type") == "user"
                and isinstance(obj.get("message"), dict) and obj["message"].get("role") == "user"):
            return session_kind_of(obj)
    return None


def account_of(path: Path) -> str | None:
    """The config dir a transcript came from: `<dir>/projects/<slug>/<id>.jsonl` -> `<dir>` (home shown as "~")."""
    return home_label(path.parent.parent.parent) if len(path.parts) > 3 else None


def root_dirs(config: dict | None) -> list[Path]:
    """Every config root to read: $CLAUDE_CONFIG_DIR, ~/.claude, `[claude_code].dirs`, then the roots the hooks
    registered from `transcript_path` (throwaway config dirs). De-duplicated by real path, first spelling wins."""
    extra = [*((config or {}).get("dirs") or []), *hookroots.registered(ClaudeCode.name)]
    return config_dirs(claude_dir(), Path.home() / ".claude", extra)


class ClaudeCode:
    name = "claude_code"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            out = []
            seen: set[str] = set()
            # Root finding (d): a multi-account user runs Claude Code under more than one
            # CLAUDE_CONFIG_DIR; [claude_code].dirs adds the others alongside the normal one.
            for base in root_dirs(config):
                key = str(base.resolve())
                if key in seen:
                    continue
                seen.add(key)
                for p in sorted((base / "projects").glob("*/*.jsonl")):
                    out.append(Location(self.name, str(p), p, f"Claude Code session {p.stem[:8]}"))
            return out
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            lines, new = read_new_lines(loc.path, cursor)
        except OSError:
            return [], cursor
        skipped = int(cursor.get("skipped", 0))
        resumed = bool(cursor) and new["start"] == int(cursor.get("offset", 0))
        last_agent = cursor.get("last_agent") if resumed else None
        # When the previous turn ended: the agent's last line, or the previous human prompt. It is what the typing speed of
        # the next prompt is measured from. Carried in the cursor, so a read that starts in the middle of a session
        # measures the same gap as one that starts at the top; unknown (None) when it is not known, and then nothing
        # is judged as too fast. Other lines (queue markers, attachments, meta and tool-result lines, which can be written
        # the same second as the prompt they belong to) never count as "the previous turn".
        turn_end = cursor.get("turn_end") if resumed and isinstance(cursor.get("turn_end"), int | float) else None
        units: list[Unit] = []
        account = account_of(loc.path)
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(obj, dict) or obj.get("isSidechain"):
                continue
            line_ts = parse_iso(obj.get("timestamp"))
            msg = obj.get("message") if isinstance(obj.get("message"), dict) else {}
            typ = obj.get("type")
            if typ == "assistant":
                if line_ts is not None:
                    turn_end = line_ts.timestamp()
                txt = " ".join(block_texts(msg.get("content"))).strip()
                if txt:
                    # Only (masked, clipped) text ever sits in the cursor row (P1-7): raw agent text could
                    # carry the same PII/secrets own_unit() masks everywhere else, and the cursor row is
                    # never itself passed through mask_pii again on its own -- this is the only chance.
                    last_agent = clip_tail(mask_pii(txt))
                continue
            if typ != "user" or msg.get("role") != "user" or obj.get("isMeta") or obj.get("isCompactSummary"):
                continue
            if not_human_line(obj):
                continue
            text = own_prompt_text(msg.get("content"))
            if not text:
                continue
            ts = parse_iso(obj.get("timestamp"))
            if ts is None:
                skipped += 1
                continue
            sid = str(obj.get("sessionId") or loc.path.stem)
            ref = str(obj.get("uuid") or f"{sid}:{ts.isoformat()}")
            meta = {"cwd_hash": hash_ref(obj["cwd"])} if isinstance(obj.get("cwd"), str) and obj["cwd"] else {}
            project, project_path = project_of(obj.get("cwd"))
            gap = ts.timestamp() - turn_end if turn_end is not None else None
            turn_end = ts.timestamp()
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
                meta=meta,
                account=account,
                project=project,
                project_path=project_path,
                session_kind=session_kind_of(obj),
                session=hash_ref(sid),
                gap_s=gap,
            )
            last_agent = None
            if u is not None:
                units.append(u)
        new["skipped"] = skipped
        new["last_agent"] = last_agent
        new["turn_end"] = turn_end
        return units, new
