"""X (Twitter) archive: a zip or the unpacked folder, reading `data/tweets.js`
(`tweets-part1.js` ...) and `data/note-tweet.js`.

The `window.YTD.<name>.part<N> = ` prefix is stripped and the JSON list parsed. Retweets are skipped, t.co links
removed,
leading @mentions of replies stripped. When a tweet is a truncated prefix of a long-form note tweet, the note is used.
Everything is public writing (register public, audience public). Cursor: {path: [mtime_ns, size]} (zip/folder: newest
file
mtime and total size of the data files).
"""

from __future__ import annotations

import html
import json
import re
import zipfile
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ..core import Location, Me, Unit, hash_ref
from ._util import configured_paths, export_candidates, inbox_dir, own_unit, parse_iso

_TWEET_FILE = re.compile(r"(?:^|/)(?:data/)?(tweets?(?:-part\d+)?|note-tweet(?:-part\d+)?)\.js$")
_PREFIX = re.compile(r"^\s*window\.YTD\.[\w.]+\s*=\s*")
_TCO = re.compile(r"https?://t\.co/\w+")
_LEAD_MENTIONS = re.compile(r"^(?:\s*@[\w.]+)+\s*")
MAX_JS_BYTES = 200 * 1024 * 1024  # P2-11: a single data file this large is skipped, not loaded whole


def parse_js(raw: str) -> list:
    try:
        data = json.loads(_PREFIX.sub("", raw, count=1))
    except ValueError:
        return []
    return data if isinstance(data, list) else []


def clean_tweet_text(t: str, reply: bool) -> str:
    t = html.unescape(_TCO.sub("", t))
    if reply:
        t = _LEAD_MENTIONS.sub("", t)
    return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", t)).strip()


def _norm(t: str) -> str:
    return " ".join(t.split())


def _sources(path: Path) -> tuple[list[tuple[str, str]], list[int], bool]:
    """[(member name, js text)], a [newest mtime_ns, total size] signature, and whether a data file was
    skipped for being over MAX_JS_BYTES (P2-11: a single file is never loaded whole past that size)."""
    out, sig, skipped = [], [0, 0], False
    if path.is_dir():
        for p in sorted(path.rglob("*.js")):
            if _TWEET_FILE.search(p.relative_to(path).as_posix()):
                st = p.stat()
                sig = [max(sig[0], st.st_mtime_ns), sig[1] + st.st_size]
                if st.st_size > MAX_JS_BYTES:
                    skipped = True
                    continue
                out.append((p.name, p.read_text("utf-8", "replace")))
    elif path.suffix.lower() == ".zip":
        st = path.stat()
        sig = [st.st_mtime_ns, st.st_size]
        with zipfile.ZipFile(path) as z:
            for info in z.infolist():
                if not _TWEET_FILE.search(info.filename):
                    continue
                if info.file_size > MAX_JS_BYTES:
                    skipped = True
                    continue
                out.append((info.filename.rsplit("/", 1)[-1], z.read(info.filename).decode("utf-8", "replace")))
    else:
        st = path.stat()
        sig = [st.st_mtime_ns, st.st_size]
        if st.st_size > MAX_JS_BYTES:
            skipped = True
        else:
            out.append((path.name, path.read_text("utf-8", "replace")))
    return out, sig, skipped


def _tweet_dt(s) -> datetime | None:
    try:
        return datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
    except (TypeError, ValueError):
        return None


class XArchive:
    name = "x_archive"
    tier = "export"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            found: dict[str, Path] = {}
            for p in export_candidates(self.name, config, (".zip",)):
                found[str(p.resolve())] = p
            for p in configured_paths(config):
                if p.is_file() and _TWEET_FILE.search(p.name):
                    found[str(p.resolve())] = p
            for root in configured_paths(config) + [inbox_dir(self.name)]:
                if root.is_dir():
                    for d in [root, *[c for c in root.iterdir() if c.is_dir()]]:
                        if (d / "data").is_dir() and any((d / "data").glob("tweet*.js")):
                            found[str(d.resolve())] = d
            return [Location(self.name, k, v, "X archive") for k, v in sorted(found.items())]
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            files, sig, skipped = _sources(loc.path)
        except (OSError, zipfile.BadZipFile):
            return [], cursor
        if cursor.get(loc.key) == sig:
            return [], cursor
        tweets, notes = [], []
        for _name, raw in files:
            for item in parse_js(raw):
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("tweet"), dict):
                    tweets.append(item["tweet"])
                elif isinstance(item.get("noteTweet"), dict):
                    notes.append(item["noteTweet"])
        note_items = []
        for n in notes:
            text = (n.get("core") or {}).get("text") or ""
            ts = parse_iso(n.get("createdAt"))
            if text.strip() and ts is not None:
                note_items.append(
                    {"id": str(n.get("noteTweetId")), "text": html.unescape(_TCO.sub("", text)).strip(), "ts": ts}
                )
        used_notes: set[str] = set()
        units: list[Unit] = []
        for t in tweets:
            full = t.get("full_text") or t.get("text") or ""
            if not isinstance(full, str) or full.startswith("RT @"):
                continue
            reply_to = t.get("in_reply_to_status_id_str")
            text = clean_tweet_text(full, bool(reply_to))
            ts = _tweet_dt(t.get("created_at"))
            if ts is None or not text:
                continue
            ref = f"tweet:{t.get('id_str') or t.get('id')}"
            probe = _norm(text).rstrip("….").rstrip()
            for n in note_items:  # a tweet truncated from a long note: prefer the note
                if (
                    len(probe) >= 30
                    and n["id"] not in used_notes
                    and abs((n["ts"] - ts).total_seconds()) < 600
                    and _norm(n["text"]).startswith(probe)
                    and len(n["text"]) > len(text)
                ):
                    used_notes.add(n["id"])
                    text, ref, ts = n["text"], f"note:{n['id']}", n["ts"]
                    break
            if reply_to:
                text = clean_tweet_text(text, True)
            u = own_unit(
                source=self.name,
                ref=ref,
                ts=ts,
                text=text,
                kind="reply" if reply_to else "post",
                register="public",
                audience="public",
                thread_id=hash_ref(str(reply_to or t.get("id_str"))),
                audience_ref=hash_ref(t["in_reply_to_screen_name"])
                if reply_to and t.get("in_reply_to_screen_name")
                else None,
                meta={"public": True},
            )
            if u is not None:
                units.append(u)
        for n in note_items:
            if n["id"] in used_notes:
                continue
            u = own_unit(
                source=self.name,
                ref=f"note:{n['id']}",
                ts=n["ts"],
                text=n["text"],
                kind="post",
                register="public",
                audience="public",
                thread_id=hash_ref(n["id"]),
                meta={"public": True},
            )
            if u is not None:
                units.append(u)
        new_cursor = {loc.key: sig}
        if skipped:
            new_cursor["note"] = "skipped: file(s) over the size limit were not read"
        return units, new_cursor
