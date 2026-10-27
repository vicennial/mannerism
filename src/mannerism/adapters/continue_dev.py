"""Continue sessions: ~/.continue/sessions/<sessionId>.json (or $CONTINUE_GLOBAL_DIR/sessions).

The VS Code / JetBrains extension and the `cn` CLI write the same files: `{sessionId, title, workspaceDirectory,
history: [{message: {role, content}, contextItems, editorState?}]}`, rewritten whole on every save (no lock, so a read
can land on a half-written file; that is a skipped read, retried next sync). `sessions.json` next to them indexes the
sessions. Own text = `role: user` items. The extension keeps what was typed in `editorState` (a TipTap document: text,
@-mention labels and slash-command names; selected code and expanded prompt files are separate nodes and dropped);
`cn` stores the plain string there. `message.content` is only the fallback, because the extension fills it with the
expanded prompt. Bare slash commands ("/help") and pastes are dropped. Timestamps: Continue records none per message,
so every turn takes the session's `dateCreated` from `sessions.json` (the file's mtime if the index has no row).
Nothing in a session file says whether it came from `cn -p` (headless) or an interactive run, so both are kept.
Cursor: {path: [mtime_ns, size]}; a changed file is re-read in full and the engine dedupes by unit_id (refs are the
message id, or a text hash).
"""

from __future__ import annotations

import json
import os
import re
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from ..core import Location, Me, Unit, context_line, hash_ref
from ._util import clip_tail, config_dirs, file_sig, from_epoch, is_relay, mask_pii, own_unit, sha

MAX_JSON_BYTES = 200 * 1024 * 1024  # a session file this large is skipped, not loaded whole
_SLASH_ONLY = re.compile(r"^/[\w:.-]+$")


def continue_dir() -> Path:
    """$CONTINUE_GLOBAL_DIR (relative paths are taken from the working directory, as Continue does) or ~/.continue."""
    env = os.environ.get("CONTINUE_GLOBAL_DIR")
    return Path(env).expanduser().absolute() if env else Path.home() / ".continue"


def _text_parts(content) -> str:
    """Text of a message content: a plain string, or the `text` parts of a multimodal list (images are dropped)."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    out = []
    for p in content:
        if isinstance(p, str):
            out.append(p)
        elif isinstance(p, dict) and p.get("type") == "text" and isinstance(p.get("text"), str):
            out.append(p["text"])
    return "\n".join(out)


def _doc_text(doc: dict) -> str:
    """Typed text of a TipTap editor document: paragraphs, with @-mentions and slash commands as the user saw them."""
    lines: list[str] = []
    for block in doc.get("content") or []:
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        attrs = block.get("attrs") if isinstance(block.get("attrs"), dict) else {}
        if kind == "paragraph":
            parts = []
            for node in block.get("content") or []:
                if not isinstance(node, dict):
                    continue
                nattrs = node.get("attrs") if isinstance(node.get("attrs"), dict) else {}
                if node.get("type") == "text" and isinstance(node.get("text"), str):
                    parts.append(node["text"])
                elif node.get("type") == "mention":
                    label = nattrs.get("renderInlineAs") or nattrs.get("label") or nattrs.get("id")
                    if isinstance(label, str) and label:
                        parts.append("@" + label.lstrip("@"))
                elif node.get("type") == "hardBreak":
                    parts.append("\n")
            lines.append("".join(parts))
        elif kind == "prompt-block":  # a chosen slash command / prompt file: only its name is the user's
            item = attrs.get("item") if isinstance(attrs.get("item"), dict) else {}
            if isinstance(item.get("name"), str) and item["name"]:
                lines.append("/" + item["name"].lstrip("/"))
        # code-block (selected code), image and anything else: not typed text
    return "\n".join(lines).strip()


def typed_text(item: dict) -> str:
    es = item.get("editorState")
    if isinstance(es, dict):
        return _doc_text(es)  # an empty document stays empty: `content` would be the expanded prompt
    if isinstance(es, (str, list)):
        text = _text_parts(es)
        if text.strip():
            return text
    msg = item.get("message") if isinstance(item.get("message"), dict) else {}
    return _text_parts(msg.get("content"))


def _created_by_session(sessions_dir: Path) -> dict[str, datetime]:
    try:
        rows = json.loads((sessions_dir / "sessions.json").read_text("utf-8", "replace"))
    except (OSError, ValueError):
        return {}
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get("sessionId"), str):
            ts = from_epoch(row.get("dateCreated"))
            if ts is not None:
                out[row["sessionId"]] = ts
    return out


class Continue:
    name = "continue"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            out = []
            for base in config_dirs(continue_dir(), Path.home() / ".continue", (config or {}).get("dirs")):
                for p in sorted((base / "sessions").glob("*.json")):
                    if p.name != "sessions.json":
                        out.append(Location(self.name, str(p), p, f"Continue session {p.stem[:8]}"))
            return out
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            sig = file_sig(loc.path)
            if cursor.get(loc.key) == sig:
                return [], cursor
            if loc.path.stat().st_size > MAX_JSON_BYTES:
                return [], {loc.key: sig, "note": "skipped: file over the size limit was not read"}
            data = json.loads(loc.path.read_text("utf-8", "replace"))
        except (OSError, ValueError):
            return [], cursor  # missing, or caught mid-write: the cursor stays and the next sync tries again
        history = data.get("history") if isinstance(data, dict) else None
        if not isinstance(history, list):
            return [], {loc.key: sig}
        sid = str(data.get("sessionId") or loc.path.stem)
        ts = _created_by_session(loc.path.parent).get(sid) or datetime.fromtimestamp(loc.path.stat().st_mtime, UTC)
        cwd = data.get("workspaceDirectory")
        meta = {"cwd_hash": hash_ref(cwd)} if isinstance(cwd, str) and cwd else {}
        seen: Counter[str] = Counter()
        units: list[Unit] = []
        last_agent = None
        for item in history:
            msg = item.get("message") if isinstance(item, dict) else None
            if not isinstance(msg, dict):
                continue
            if msg.get("role") == "assistant":
                text = _text_parts(msg.get("content")).strip()
                if text:
                    last_agent = clip_tail(mask_pii(text))  # mask first: a secret cut by the clip would no longer match
                continue
            if msg.get("role") != "user":
                continue
            text = typed_text(item).strip()
            if not text or _SLASH_ONLY.match(text) or is_relay(text):
                continue
            digest = sha(text)
            seen[digest] += 1
            mid = msg.get("id")
            ref = f"{sid}:{mid}" if isinstance(mid, str) and mid else f"{sid}:{digest}:{seen[digest]}"
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
        return units, {loc.key: sig}
