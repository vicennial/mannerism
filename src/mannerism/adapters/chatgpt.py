"""ChatGPT export: `conversations.json` (or `conversations-NNN.json` shards), plain or inside the export .zip.

Only the active branch of each conversation is read: from `current_node` up through `parent`. Own text = user messages
of
content type `text`. Custom-instruction messages are dropped. Cursor: {path: [mtime_ns, size]}; changed files are
re-read
and the engine dedupes by unit_id.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterable

from ..core import Location, Me, Unit, context_line, hash_ref
from ._util import clip_tail, export_candidates, file_sig, from_epoch, own_unit

_NAMES = ("conversations.json",)
MAX_JSON_BYTES = 200 * 1024 * 1024  # P2-11: a single JSON file (plain or zip member) this large is skipped


def _is_conv_member(name: str) -> bool:
    base = name.rsplit("/", 1)[-1].lower()
    return base == "conversations.json" or (base.startswith("conversations-") and base.endswith(".json"))


def load_conversations(path) -> tuple[list, bool]:
    """(conversations, skipped_too_large) -- a file/member over MAX_JSON_BYTES is skipped, not loaded whole."""
    out: list = []
    skipped = False
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not _is_conv_member(info.filename):
                    continue
                if info.file_size > MAX_JSON_BYTES:
                    skipped = True
                    continue
                data = json.loads(z.read(info.filename).decode("utf-8", "replace"))
                out.extend(data if isinstance(data, list) else [])
    elif path.stat().st_size > MAX_JSON_BYTES:
        skipped = True
    else:
        data = json.loads(path.read_text("utf-8", "replace"))
        out = data if isinstance(data, list) else []
    return out, skipped


def _text(msg: dict) -> str:
    c = msg.get("content") or {}
    if not isinstance(c, dict) or c.get("content_type") != "text":
        return ""
    return "\n".join(p for p in c.get("parts") or [] if isinstance(p, str) and p.strip()).strip()


def active_branch(conv: dict) -> list[dict]:
    mapping = conv.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        return []
    node_id = conv.get("current_node")
    if node_id not in mapping:  # fall back to the newest leaf
        leaves = [k for k, v in mapping.items() if isinstance(v, dict) and not v.get("children")]

        def stamp(k):
            return (mapping[k].get("message") or {}).get("create_time") or 0

        node_id = max(leaves, key=stamp) if leaves else None
    chain, seen = [], set()
    while node_id and node_id in mapping and node_id not in seen:
        seen.add(node_id)
        node = mapping[node_id]
        if isinstance(node, dict):
            if isinstance(node.get("message"), dict):
                chain.append({"id": node_id, **node["message"]})
            node_id = node.get("parent")
        else:
            break
    return chain[::-1]


class ChatGPT:
    name = "chatgpt"
    tier = "export"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            files = [
                p
                for p in export_candidates(self.name, config, (".json", ".zip"))
                if p.suffix.lower() == ".zip" or _is_conv_member(p.name)
            ]
            return [Location(self.name, str(p), p, "ChatGPT export") for p in files]
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            sig = file_sig(loc.path)
            if cursor.get(loc.key) == sig:
                return [], cursor
            convs, skipped = load_conversations(loc.path)
        except (OSError, ValueError, zipfile.BadZipFile):
            return [], cursor
        units: list[Unit] = []
        for conv in convs:
            if not isinstance(conv, dict):
                continue
            cid = str(conv.get("id") or conv.get("conversation_id") or conv.get("title") or "")
            last_agent = None
            for m in active_branch(conv):
                role = (m.get("author") or {}).get("role")
                if role == "assistant":
                    t = _text(m)
                    if t:
                        last_agent = clip_tail(t)
                    continue
                if role != "user" or (m.get("metadata") or {}).get("is_user_system_message"):
                    continue
                text = _text(m)
                ts = from_epoch(m.get("create_time")) or from_epoch(conv.get("create_time"))
                if not text or ts is None:
                    continue
                ctx = [context_line("agent", last_agent)] if last_agent else []
                u = own_unit(
                    source=self.name,
                    ref=f"{cid}:{m['id']}",
                    ts=ts,
                    text=text,
                    kind="prompt",
                    register="agent",
                    audience="agent",
                    thread_id=hash_ref(cid),
                    context=ctx,
                )
                last_agent = None
                if u is not None:
                    units.append(u)
        new_cursor = {loc.key: sig}
        if skipped:
            new_cursor["note"] = "skipped: file(s) over the size limit were not read"
        return units, new_cursor
