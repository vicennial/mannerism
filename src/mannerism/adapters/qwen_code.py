"""Qwen Code sessions: ~/.qwen/projects/<cwd-slug>/chats/<sessionId>.jsonl.

The directory is $QWEN_RUNTIME_DIR when set, else $QWEN_HOME, else ~/.qwen (`[qwen_code].dirs` adds more). The slug is
the project path with every non-alphanumeric character turned into `-`. Windows and macOS use the same layout under
the user's home directory.

Every line is a `ChatRecord` (`uuid`, `parentUuid`, `timestamp`, `type` user/assistant/tool_result/system, an optional
`subtype`, and `provenance`). Own text = records with `type=user`, no `subtype` (or `mid_turn_user_message`, typed while
a turn was still running) and `provenance=real_user`, so cron / notification / goal / agent-relay records (which have a
subtype and a different provenance) never count. Dropped as well:
- sub-agent rounds (`isSidechain`),
- the prompt a custom slash command expanded into (its parent is a `slash_command` record whose expansion went to the
  model, `sentToModel: true`: the user typed `/tidy x`, not the template). Built-ins that only print something (`/about`,
  `sentToModel: false`) leave the prompt typed right after them alone,
- `@path` expansions: the file contents Qwen appends after the typed words (`--- Content from referenced files ---`),
  and `<system-reminder>` blocks,
- whole sessions whose `session_source` is not an interactive one (channel bots, scheduled tasks, managed gateways).
Slash commands themselves are `system` / `slash_command` records, so they never reach the user filter. Qwen writes nothing
that marks a `qwen -p` (headless) session, so those are kept.

The words the user typed come from the `at_command` record (`systemPayload.userText`, the query as typed) when the prompt
has one; otherwise from the message, where Qwen's @-parser has split `jo.dev@example.org` into `jo.dev @example.org`:
that split is rejoined before masking, so the address is masked instead of leaking. (A word followed by an `@file.ext`
mention that has no at_command record is rejoined the same way and shows as `[email]`: over-masking, on purpose.)

Records form a tree (rewind leaves the abandoned branch in the file); a branch the user typed is still their text, so
every branch is read. Appends go through a per-session lock file next to the transcript, not through the transcript, so
the file stays append-only and a byte-offset cursor is safe.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path

from ..core import Location, Me, Unit, context_line, hash_ref
from ._util import clip_tail, config_dirs, is_relay, mask_pii, own_unit, parse_iso, read_new_lines, resolved_key

_OWN_SUBTYPES = (None, "mid_turn_user_message")
_INTERACTIVE_SOURCES = ("", "default", "cli", "vscode")
# The same blocks Qwen itself removes to find "the user's own words" (core/turn-budget.ts).
_EMBEDDED = (
    re.compile(r"<system-reminder>.*?(?:</system-reminder>|\Z)", re.DOTALL),
    re.compile(r"--- Content from referenced files ---.*?(?:--- End of content ---|\Z)", re.DOTALL),
    re.compile(r"--- Content from MCP resource .*? \[([^\]\n]+)\] ---.*?(?:--- End of MCP resource .*? \[\1\] ---|\Z)",
               re.DOTALL),
)
MAX_SLASH_UUIDS = 8
MAX_AT_COMMANDS = 4
_SPLIT_EMAIL = re.compile(r"(\w) @(?=[\w-]+(?:\.[\w-]+)+)")  # Qwen's @-parser: "jo.dev@example.org" -> "jo.dev @example.org"


def qwen_dir() -> Path:
    env = os.environ.get("QWEN_RUNTIME_DIR") or os.environ.get("QWEN_HOME")
    return Path(env).expanduser() if env else Path.home() / ".qwen"


def part_texts(message, *, thoughts: bool = False) -> list[str]:
    """Text of the `parts` of a Gemini-style Content; function calls / responses and images are ignored."""
    parts = message.get("parts") if isinstance(message, dict) else None
    out = []
    for p in parts if isinstance(parts, list) else []:
        if isinstance(p, dict) and isinstance(p.get("text"), str) and (thoughts or not p.get("thought")):
            out.append(p["text"])
    return out


def own_prompt_text(obj: dict, typed: str | None = None) -> str:
    """The typed words of a user record. `typed` is the `userText` of the at_command record this prompt followed."""
    payload = obj.get("systemPayload") if isinstance(obj.get("systemPayload"), dict) else {}
    shown = payload.get("displayText")
    if isinstance(shown, str) and shown.strip():
        text = shown
    elif typed and typed.strip():
        text = typed  # verbatim: the message below has the @-parser's re-spelling of it
    else:
        text = _SPLIT_EMAIL.sub(r"\1@", "".join(part_texts(obj.get("message"))))
    for rx in _EMBEDDED:
        text = rx.sub(" ", text)
    text = text.strip()
    return "" if is_relay(text) else text


def _expands_to_model(payload: dict, obj: dict, slash: list[str]) -> bool:
    """True for a slash_command record whose expansion was sent to the model, so its children are not typed text.

    `sentToModel` is on the invocation record; the result record that follows carries none, so it counts when its parent
    does. With no flag at all (older builds) an invocation counts: dropping a prompt is the safe error."""
    sent = payload.get("sentToModel")
    if sent is not None:
        return sent is True
    return payload.get("phase") != "result" or obj.get("parentUuid") in slash


class QwenCode:
    name = "qwen_code"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            out = []
            seen: set[str] = set()
            for base in config_dirs(qwen_dir(), Path.home() / ".qwen", (config or {}).get("dirs")):
                key = resolved_key(base)
                if key in seen:
                    continue
                seen.add(key)
                for p in sorted((base / "projects").glob("*/chats/*.jsonl")):
                    out.append(Location(self.name, str(p), p, f"Qwen Code session {p.stem[:8]}"))
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
        skip = bool(cursor.get("skip")) and resumed
        last_agent = cursor.get("last_agent") if resumed else None
        slash = list(cursor.get("slash", [])) if resumed else []
        at_texts = dict(cursor.get("at_texts") or {}) if resumed else {}  # at_command uuid -> masked typed text
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
            if not isinstance(obj, dict) or obj.get("isSidechain"):
                continue
            typ, sub = obj.get("type"), obj.get("subtype")
            if typ == "assistant":
                txt = " ".join(part_texts(obj.get("message"))).strip()
                if txt:
                    last_agent = clip_tail(mask_pii(txt))  # masked before it sits in the cursor row (P1-7)
                continue
            if typ == "system":
                payload = obj.get("systemPayload") if isinstance(obj.get("systemPayload"), dict) else {}
                if sub == "slash_command" and isinstance(obj.get("uuid"), str):
                    if _expands_to_model(payload, obj, slash):
                        slash = [*slash, obj["uuid"]][-MAX_SLASH_UUIDS:]
                elif sub == "at_command" and isinstance(obj.get("uuid"), str) and isinstance(payload.get("userText"), str):
                    # masked before it sits in the cursor row (P1-7); masking again later changes nothing
                    at_texts[obj["uuid"]] = mask_pii(payload["userText"])
                    at_texts = dict(list(at_texts.items())[-MAX_AT_COMMANDS:])
                elif sub == "session_source" and str(payload.get("sourceType") or "") not in _INTERACTIVE_SOURCES:
                    skip = True
                continue
            if typ != "user" or sub not in _OWN_SUBTYPES or obj.get("provenance") not in (None, "real_user"):
                continue
            if obj.get("parentUuid") in slash:
                continue  # the template a custom slash command expanded into
            text = own_prompt_text(obj, at_texts.get(obj.get("parentUuid")))
            if not text:
                continue
            ts = parse_iso(obj.get("timestamp"))
            if ts is None:
                skipped += 1
                continue
            sid = str(obj.get("sessionId") or loc.path.stem)
            ref = str(obj.get("uuid") or f"{sid}:{ts.isoformat()}")
            meta = {"cwd_hash": hash_ref(obj["cwd"])} if isinstance(obj.get("cwd"), str) and obj["cwd"] else {}
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
            )
            last_agent = None
            if u is not None:
                units.append(u)
        new.update(skipped=skipped, last_agent=last_agent, slash=slash, at_texts=at_texts, skip=skip)
        return ([] if skip else units), new
