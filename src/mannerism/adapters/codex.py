"""Codex CLI rollouts: ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl (or $CODEX_HOME/sessions).

Lines are `{"timestamp", "type", "payload"}`. Two user-message variants exist:
`response_item` / `message` / role user (content[].input_text) and `event_msg` / `user_message` (payload.message).
Both can appear for the same prompt; they are de-duplicated (same text within 2 s -> one unit, event_msg preferred).
Injected instructions (AGENTS.md, environment context, ...) are dropped.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC
from pathlib import Path

from .. import hookroots
from ..core import Location, Me, Unit, clean_label, context_line, hash_ref, home_label
from ._util import (
    clip_tail,
    codex_dir,
    config_dirs,
    is_relay,
    mask_pii,
    own_unit,
    parse_iso,
    project_of,
    read_new_lines,
    sha,
)

_INJECTED = (
    "<user_instructions>",
    "<environment_context>",
    "<permissions instructions>",
    "# AGENTS.md instructions",
    "<INSTRUCTIONS>",
    "<turn_aborted>",
)
DUP_WINDOW_S = 2.0


def _is_injected(text: str) -> bool:
    return text.lstrip().startswith(_INJECTED) or "<user_instructions>" in text or is_relay(text)


_INTERACTIVE_SOURCES = {"cli", "vscode"}
_KINDS = {"cli": "interactive", "vscode": "ide"}  # session_kind of the sessions that are read at all
PEEK_LINES = 20


def headless_session(payload: dict, exclude_originators: frozenset[str] = frozenset()) -> bool:
    """True when a session_meta payload says the session was not driven by a human at the keyboard:
    `source` present but not a plain "cli"/"vscode" string (headless "exec", or a subagent object), `originator`
    codex_exec,
    or a parent thread / agent role (spawned subagents and guardians)."""
    if "source" in payload and not (isinstance(payload["source"], str) and payload["source"] in _INTERACTIVE_SOURCES):
        return True
    if payload.get("originator") in exclude_originators:
        return True  # config `exclude_originators`: front-ends that drive Codex from scripts
    return payload.get("originator") == "codex_exec" or bool(
        payload.get("parent_thread_id") or payload.get("agent_role")
    )


def account_of(path: Path) -> str | None:
    """The CODEX_HOME a rollout came from: `<home>/sessions/Y/M/D/rollout-*.jsonl` -> `<home>` (home shown as "~")."""
    return home_label(path.parents[4]) if len(path.parents) > 4 else None


def peek_session_meta(path) -> dict | None:
    """The session_meta payload from the first few lines, or None when there is none."""
    try:
        with open(path, "rb") as f:
            for i, raw in enumerate(f):
                if i >= PEEK_LINES:
                    break
                try:
                    obj = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "session_meta" and isinstance(obj.get("payload"), dict):
                    return obj["payload"]
    except OSError:
        return None
    return None


def prompt_session_kind(path) -> str | None:
    """The interactive session kind for a prompt hook, from the rollout's bounded session metadata peek."""
    payload = peek_session_meta(path)
    if payload is None or headless_session(payload):
        return None
    source = payload.get("source")
    return _KINDS.get(source, "interactive") if isinstance(source, str) else "interactive"


def _items_text(content, kinds: tuple[str, ...]) -> str:
    if isinstance(content, str):
        return content
    parts = []
    if isinstance(content, list):
        for it in content:
            if isinstance(it, dict) and it.get("type") in kinds and isinstance(it.get("text"), str):
                parts.append(it["text"])
    return "\n".join(p for p in parts if not _is_injected(p) and p.strip())


class Codex:
    name = "codex"
    tier = "auto"

    exclude_originators: frozenset[str] = frozenset()

    def discover(self, me: Me, config: dict) -> list[Location]:
        self.exclude_originators = frozenset((config or {}).get("exclude_originators") or ())
        try:
            out = []
            seen: set[str] = set()
            # Root finding (d): [codex].dirs adds extra CODEX_HOME directories for a multi-account user.
            extra = [*((config or {}).get("dirs") or []), *hookroots.registered(self.name)]  # + hook-registered roots
            for base in config_dirs(codex_dir(), Path.home() / ".codex", extra):
                key = str(base.resolve())
                if key in seen:
                    continue
                seen.add(key)
                for p in sorted((base / "sessions").glob("*/*/*/rollout-*.jsonl")):
                    out.append(Location(self.name, str(p), p, f"Codex session {p.stem[-8:]}"))
            return out
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            ino = loc.path.stat().st_ino
        except OSError:
            return [], cursor
        if cursor.get("skip") and cursor.get("inode") == ino:
            return [], cursor  # decided on an earlier sync: not a human-driven session
        if not cursor.get("sid"):
            head = peek_session_meta(loc.path)
            if head is not None and headless_session(head, self.exclude_originators):
                return [], {"skip": True, "inode": ino}
        try:
            lines, new = read_new_lines(loc.path, cursor)
        except OSError:
            return [], cursor
        resumed = bool(cursor) and new["start"] == int(cursor.get("offset", 0))
        sid = cursor.get("sid") if resumed else None
        # P1-7: only the HASH of cwd is ever carried in the cursor. It is computed fresh, once, the moment
        # a session_meta line hands us the raw path (just below); nothing here ever re-hashes an
        # already-hashed value, which would silently change cwd_hash between reads of the same session.
        cwd_hash = cursor.get("cwd_hash") if resumed else None
        # The same goes for the (masked) project labels and the session kind: they come from the session_meta line.
        project = cursor.get("project") if resumed else None
        project_path = cursor.get("project_path") if resumed else None
        kind = cursor.get("kind") if resumed else None
        # When the previous turn ended (the agent's last message, or the previous prompt); carried in the cursor, None
        # when unknown, so a prompt is never judged "too fast" on a guess (see adapters/claude_code.py).
        turn_end = cursor.get("turn_end") if resumed and isinstance(cursor.get("turn_end"), int | float) else None
        last_agent = cursor.get("last_agent") if resumed else None
        recent = [tuple(x) for x in cursor.get("recent", [])] if resumed else []
        skipped = int(cursor.get("skipped", 0))
        mtime_ts = None
        cands: list[dict] = []
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except ValueError:
                skipped += 1
                continue
            if not isinstance(obj, dict):
                skipped += 1
                continue
            line_ts = parse_iso(obj.get("timestamp"))
            typ = obj.get("type")
            pl = obj.get("payload") if isinstance(obj.get("payload"), dict) else None
            if pl is None and typ in ("message", "user_message"):  # older rollouts without the payload envelope
                pl, typ = obj, ("event_msg" if typ == "user_message" else "response_item")
            if typ == "session_meta" and pl is not None:
                if headless_session(pl, self.exclude_originators):
                    return [], {"skip": True, "inode": new["inode"]}
                sid = str(pl.get("id") or sid or "") or sid
                raw_cwd = pl.get("cwd")
                if isinstance(raw_cwd, str) and raw_cwd:
                    cwd_hash = hash_ref(raw_cwd)
                    project, project_path = project_of(raw_cwd)
                    project, project_path = clean_label(project), home_label(project_path)
                src = pl.get("source")
                kind = _KINDS.get(src, "interactive") if isinstance(src, str) else "interactive"
                if line_ts is not None:
                    turn_end = line_ts.timestamp()  # the session opened: the first prompt is measured from here
                continue
            if pl is None:
                continue
            ptype = pl.get("type", "message" if typ == "response_item" else None)
            text, via = "", ""
            if typ == "response_item" and ptype == "message":
                if pl.get("role") == "assistant":
                    if line_ts is not None:
                        turn_end = line_ts.timestamp()
                    t = _items_text(pl.get("content"), ("output_text", "text"))
                    if t.strip():
                        last_agent = clip_tail(mask_pii(t))  # masked+clipped before it ever sits in the cursor (P1-7)
                    continue
                if pl.get("role") != "user":
                    continue
                text, via = _items_text(pl.get("content"), ("input_text", "text")), "response"
            elif typ == "event_msg" and ptype == "user_message" and isinstance(pl.get("message"), str):
                text, via = ("" if _is_injected(pl["message"]) else pl["message"]), "event"
            else:
                continue
            text = text.strip()
            if not text:
                continue
            ts = parse_iso(obj.get("timestamp")) or mtime_ts
            if ts is None:
                try:
                    from datetime import datetime

                    mtime_ts = ts = datetime.fromtimestamp(loc.path.stat().st_mtime, tz=UTC)
                except OSError:
                    skipped += 1
                    continue
            norm = " ".join(text.split())
            h, epoch = sha(norm), ts.timestamp()
            dup = next(
                (c for c in reversed(cands[-6:]) if c["h"] == h and abs(c["epoch"] - epoch) <= DUP_WINDOW_S), None
            )
            if dup is not None:
                if via == "event" and dup["via"] == "response":
                    dup.update(via="event", text=text, ts=ts, epoch=epoch)
                continue
            if any(r[1] == h and abs(r[0] - epoch) <= DUP_WINDOW_S for r in recent):
                continue
            gap = ts.timestamp() - turn_end if turn_end is not None else None
            turn_end = ts.timestamp()
            cands.append({"h": h, "epoch": epoch, "via": via, "text": text, "ts": ts, "agent": last_agent, "gap": gap})
            last_agent = None
        sid = sid or loc.path.stem
        account = account_of(loc.path)
        units = []
        for c in cands:
            ref = f"{sid}:{c['h']}:{int(c['epoch']) // 60}"
            ctx = [context_line("agent", c["agent"])] if c["agent"] else []
            meta = {"cwd_hash": cwd_hash} if cwd_hash else {}
            u = own_unit(
                source=self.name,
                ref=ref,
                ts=c["ts"],
                text=c["text"],
                kind="prompt",
                register="agent",
                audience="agent",
                thread_id=hash_ref(sid),
                context=ctx,
                meta=meta,
                account=account,
                project=project,
                project_path=project_path,
                session_kind=kind or "interactive",
                session=hash_ref(sid),
                gap_s=c["gap"],
            )
            if u is not None:
                units.append(u)
        recent = (recent + [(c["epoch"], c["h"]) for c in cands])[-8:]
        new.update(sid=sid, cwd_hash=cwd_hash, last_agent=last_agent, turn_end=turn_end, skipped=skipped, recent=[list(r) for r in recent],
                   project=project, project_path=project_path, kind=kind)
        return units, new
