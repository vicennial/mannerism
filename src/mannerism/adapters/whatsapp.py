"""WhatsApp chat exports: `.txt` files (iOS `[dd/mm/yyyy, hh:mm:ss] Name: text` and Android
`dd/mm/yyyy, hh:mm - Name: text`), or `.zip` exports holding `_chat.txt` / `WhatsApp Chat*.txt`.

Date order (d/m or m/d) is detected per file (a field above 12 decides; default from `config["date_order"]`, "dmy").
Timestamps carry no zone, so they are taken as UTC (meta tz = "assumed-utc").

Own sender: a name in me.names; else me.handles["whatsapp"]; else `config["self_name"]` (remembered from discover).
Failing that, R3 (P0-1) auto-detects from the chat files the user actually configured (that file's own folder if
the configured path IS a folder, plus the inbox -- never a sibling of a configured single FILE, which would read
outside what the user pointed at): among files with exactly two distinct senders (1:1-looking; a file with more
senders is a group and is never used as a vote), the one name that is a sender in >= 2 such files with >= 2
DISTINCT counterparts qualifies. If more than one name qualifies (or none does) nothing is attributed and the
cursor says {"needs_self_name": true}. A real "own 2 files happen to share the same other person" case (test:
auto_detect_refuses_when_ambiguous) correctly stays undetected this way, since both candidates then have only
ONE distinct counterpart between them.

This auto-detected pick is still USED (mannerism stays a zero-question tool), but every unit from it carries
meta.self_detected=true and the adapter's cursor repeats the guess, so `status` can surface "WhatsApp: you are
'<name>'? confirm with `mannerism sources set whatsapp self_name ...`" -- it is a suggestion, not a silent fact.

A chat with more than one other sender is a group. Cursor: {path: [mtime_ns, size], "self_detected": name?}.
"""

from __future__ import annotations

import re
import zipfile
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from ..core import Location, Me, Unit, hash_ref
from ._util import build_chat_units, export_candidates, file_sig, sha

_INVIS = "\u200e\u200f\u202a\u202b\u202c\ufeff"
_HEAD = re.compile(
    r"^\[?(?P<a>\d{1,4})[/.\-](?P<b>\d{1,2})[/.\-](?P<c>\d{1,4}),?\s+(?P<h>\d{1,2})[:.](?P<m>\d{2})(?:[:.](?P<s>\d{2}))?"
    r"(?:[\s\u202f\u00a0]*(?P<ap>[AaPp]\.?\s?[Mm]\.?))?\]?\s*(?:-\s|–\s)?(?P<rest>.*)$"
)
_DROP = re.compile(
    r"^(?:<media omitted>|<attached:.*>|(?:image|video|audio|sticker|gif|document|contact card|voice message) omitted|"
    r"this message was deleted\.?|you deleted this message\.?|messages and calls are end-to-end encrypted.*|"
    r"missed (?:voice|video) call.*|null|<this message was edited>|location:.*|live location shared)$",
    re.IGNORECASE,
)
_EDITED = re.compile(r"\s*<This message was edited>\s*$")


def _strip(s: str) -> str:
    return s.lstrip(_INVIS)


def parse_chat(text: str, default_order: str = "dmy") -> tuple[list[dict], int]:
    """Parse export text into messages [{ts, sender, text}] (system lines have sender None) and the bad-line count."""
    raw: list[list] = []  # [match groups dict, rest, extra lines]
    for line in text.replace("\r\n", "\n").split("\n"):
        line_s = _strip(line)
        m = _HEAD.match(line_s) if line_s[:1] in ("[", *"0123456789") else None
        if m and (line_s.startswith("[") or re.search(r"\s[-–]\s", line_s[:40])):
            raw.append([m.groupdict(), m.group("rest"), []])
        elif raw:
            raw[-1][2].append(line)
    first_gt, second_gt, ymd = False, False, False
    for g, _, _ in raw:
        if len(g["a"]) == 4:
            ymd = True
        elif int(g["a"]) > 12:
            first_gt = True
        elif int(g["b"]) > 12:
            second_gt = True
    order = "ymd" if ymd else "dmy" if first_gt else "mdy" if second_gt else default_order
    msgs, bad = [], 0
    for g, rest, extra in raw:
        a, b, c = int(g["a"]), int(g["b"]), int(g["c"])
        y, mo, d = (a, b, c) if order == "ymd" else (c, b, a) if order == "dmy" else (c, a, b)
        y = y + 2000 if y < 100 else y
        h, mi, s = int(g["h"]), int(g["m"]), int(g["s"] or 0)
        ap = (g["ap"] or "").replace(".", "").replace(" ", "").lower()
        if ap == "pm" and h < 12:
            h += 12
        elif ap == "am" and h == 12:
            h = 0
        try:
            ts = datetime(y, mo, d, h, mi, s, tzinfo=UTC)
        except ValueError:
            bad += 1
            continue
        rest = _strip(rest)
        sender, sep, body = rest.partition(": ")
        if not sep or '"' in sender or len(sender) > 60:
            msgs.append({"ts": ts, "sender": None, "text": rest})  # system line
            continue
        body = "\n".join([body, *extra]).strip()
        body = _EDITED.sub("", _strip(body)).strip()
        msgs.append({"ts": ts, "sender": sender.strip(_INVIS + " "), "text": body})
    return msgs, bad


def senders_of(text: str, default_order: str = "dmy") -> set[str]:
    return {m["sender"] for m in parse_chat(text, default_order)[0] if m["sender"]}


MAX_CHAT_BYTES = 200 * 1024 * 1024  # P2-11: a single chat file this large is skipped, not loaded whole


def chat_texts(path: Path) -> list[tuple[str, str]]:
    """[(chat name, text)] for a .txt file or each chat file in a .zip. A member/file over MAX_CHAT_BYTES is
    silently skipped (P2-11) -- a single WhatsApp export is ordinarily tiny text, so this only guards
    against a pathological file."""
    if path.suffix.lower() == ".zip":
        out = []
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                base = info.filename.rsplit("/", 1)[-1]
                if base == "_chat.txt" or (base.startswith("WhatsApp Chat") and base.endswith(".txt")):
                    if info.file_size > MAX_CHAT_BYTES:
                        continue
                    out.append((f"{path.name}/{info.filename}", z.read(info.filename).decode("utf-8", "replace")))
        return out
    if path.stat().st_size > MAX_CHAT_BYTES:
        return []
    return [(path.name, path.read_text("utf-8", "replace"))]


class WhatsApp:
    name = "whatsapp"
    tier = "export"

    def __init__(self):
        self._config: dict = {}

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            self._config = dict(config or {})
            return [
                Location(self.name, str(p), p, "WhatsApp chat export")
                for p in export_candidates(self.name, config, (".txt", ".zip"))
            ]
        except Exception:  # noqa: BLE001
            return []

    def _self_name(self, me: Me, own_senders: set[str], order: str) -> tuple[str | None, bool]:
        """(name, auto_detected). A direct name/handle/configured-name match wins and is never flagged as a
        guess. Otherwise, P0-1: vote from this source's OWN files only (export_candidates: the user's
        configured paths plus the inbox -- never a sibling file the user never pointed at)."""
        for n in own_senders:
            if me.has_name(n):
                return n, False
        for cand in (me.handles.get("whatsapp"), self._config.get("self_name")):
            if cand:
                for n in own_senders:
                    if n.casefold() == str(cand).casefold():
                        return n, False
        # Per-candidate voting: a file counts only when it looks like a real 1:1 (exactly two senders) --
        # a group file (3+ senders) never votes, so a lurking non-sender in a group can no longer poison
        # the result the way a single global set-intersection across ALL files used to. A candidate
        # qualifies once it has >= 2 DISTINCT counterparts (which, since one 1:1 file contributes exactly
        # one counterpart, also means it showed up in >= 2 such files).
        counterparts: dict[str, set[str]] = {}
        for p in export_candidates(self.name, self._config, (".txt", ".zip")):
            try:
                for _, t in chat_texts(p):
                    s = senders_of(t, order)
                    if len(s) == 2:
                        a, b = sorted(s)
                        counterparts.setdefault(a, set()).add(b)
                        counterparts.setdefault(b, set()).add(a)
            except (OSError, zipfile.BadZipFile):
                continue
        qualified = [n for n, cps in counterparts.items() if len(cps) >= 2]
        if len(qualified) == 1:
            return qualified[0], True
        return None, False

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            sig = file_sig(loc.path)
            if cursor.get(loc.key) == sig:
                return [], cursor
            chats = chat_texts(loc.path)
        except (OSError, zipfile.BadZipFile):
            return [], cursor
        order = str(self._config.get("date_order", "dmy")).lower()
        units: list[Unit] = []
        detected: str | None = None
        for _cname, text in chats:
            msgs, _ = parse_chat(text, order)
            senders = {m["sender"] for m in msgs if m["sender"]}
            me_name, auto = self._self_name(me, senders, order)
            if me_name is None:
                return [], {"needs_self_name": True}
            if auto:
                detected = me_name
            others = sorted(senders - {me_name})
            group = len(others) > 1
            # Thread identity is the participant set, not the export's file name: re-exporting the same
            # chat under a new name (WhatsApp gives no stable chat id) must land on the same thread and
            # the same per-message refs below, so the engine's unit_id dedup catches it as unchanged
            # rather than re-ingesting it as a second copy (P2-17).
            thread = hash_ref(":".join(sorted(senders)))
            rows = []
            for m in msgs:
                if not m["sender"] or not m["text"] or _DROP.match(m["text"].strip().strip(_INVIS)):
                    continue
                own = m["sender"] == me_name
                rows.append(
                    (
                        m["ts"],
                        m["sender"],
                        own,
                        f"{thread}:{int(m['ts'].timestamp())}:{sha(m['sender'] + m['text'], 6)}",
                        m["text"].strip(_INVIS + " \n"),
                        m["sender"],
                    )
                )
            unit_meta = {"is_group": group, "tz": "assumed-utc"}
            if auto:
                # A guess, not a fact: `status` reads this back off the cursor to ask the user to confirm it.
                unit_meta["self_detected"] = True
            units.extend(
                build_chat_units(
                    source=self.name,
                    thread=thread,
                    messages=rows,
                    audience="group" if group else "close",
                    register="group" if group else "close",
                    meta=unit_meta,
                )
            )
        new_cursor = {loc.key: sig}
        if detected:
            new_cursor["self_detected"] = detected
        return units, new_cursor
