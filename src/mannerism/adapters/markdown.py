"""Markdown notes: configured folders/files plus Obsidian vaults found through obsidian.json.

Every .md file is own text (the folder is the user's). Frontmatter, fenced code, HTML comments, embeds and wikilink
syntax are
stripped; headings are kept out; paragraphs are grouped into units of about 300 words. Files marked `public: true` in
the
frontmatter or living under a directory listed in `config["public_dirs"]` (absolute, or relative to the notes root)
are public
writing; nothing is guessed from folder names. Everything else is a private note
(register `?`, audience self). Cursor: {relative path: [mtime_ns, sha1]}.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from ..core import Location, Me, Unit, hash_ref
from ._util import configured_paths, own_unit, parse_iso, sha

MAX_BYTES = 1_000_000
WORDS = 300
_SKIP_DIRS = {".obsidian", ".trash", "node_modules"}
_PD_KEY = "\x00public_dirs"  # cursor entry: signature of config["public_dirs"], so a change re-reads everything
_FRONT = re.compile(r"\A\ufeff?---[ \t]*\n(.*?)\n(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL)
_FENCE = re.compile(r"^[ \t]*(```|~~~).*?^[ \t]*\1[ \t]*$|^[ \t]*(```|~~~).*\Z", re.DOTALL | re.MULTILINE)
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_EMBED = re.compile(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)")
_WIKI = re.compile(r"\[\[([^\]|]*)(?:\|([^\]]*))?\]\]")
_LINK = re.compile(r"\[([^\]]+)\]\((?:[^)]*)\)")


def obsidian_vaults() -> list[Path]:
    out = []
    for cfg in (
        Path.home() / ".config/obsidian/obsidian.json",
        Path.home() / "Library/Application Support/obsidian/obsidian.json",
    ):
        try:
            data = json.loads(cfg.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        vaults = data.get("vaults") if isinstance(data, dict) else None
        for v in vaults.values() if isinstance(vaults, dict) else []:
            p = Path(v["path"]).expanduser() if isinstance(v, dict) and v.get("path") else None
            if p is not None and p.is_dir() and p not in out:
                out.append(p)
    return out


def split_frontmatter(text: str) -> tuple[dict, str]:
    m = _FRONT.match(text)
    if not m:
        return {}, text
    meta = {}
    for ln in m.group(1).splitlines():
        k, sep, v = ln.partition(":")
        if sep and k.strip() and not k.startswith((" ", "\t", "-")):
            meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, text[m.end() :]


def clean_body(body: str) -> str:
    body = _COMMENT.sub("", body)
    body = _FENCE.sub("", body)
    body = _EMBED.sub("", body)
    body = _WIKI.sub(lambda m: m.group(2) or m.group(1), body)
    return _LINK.sub(r"\1", body)


def paragraph_groups(body: str, words: int = WORDS) -> list[str]:
    paras = []
    for blk in re.split(r"\n\s*\n", body.replace("\r\n", "\n")):
        lines = [
            ln
            for ln in blk.split("\n")
            if not re.match(r"^\s{0,3}#{1,6}\s", ln) and not re.match(r"^\s*(?:[-*_]\s*){3,}$", ln)
        ]
        blk = "\n".join(lines).strip()
        if blk:
            paras.append(blk)
    groups, cur, n = [], [], 0
    for p in paras:
        w = len(p.split())
        if cur and n + w > words:
            groups.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(p)
        n += w
    if cur:
        groups.append("\n\n".join(cur))
    return groups


def _walk(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in _SKIP_DIRS)
        for f in sorted(files):
            if f.lower().endswith(".md") and not f.startswith("."):
                yield Path(dirpath) / f


class Markdown:
    name = "markdown"
    tier = "auto"

    def __init__(self):
        self._config: dict = {}

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            self._config = dict(config or {})
            seen: dict[str, Location] = {}
            for p in configured_paths(config):
                if p.exists():
                    seen[str(p.resolve())] = Location(self.name, str(p.resolve()), p.resolve(), "Markdown notes")
            for p in obsidian_vaults():
                seen.setdefault(str(p.resolve()), Location(self.name, str(p.resolve()), p.resolve(), "Obsidian vault"))
            return list(seen.values())
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = dict(cursor or {})
        root = loc.path
        if root is None or not root.exists():
            return [], cursor
        base = root.parent if root.is_file() else root
        pub_dirs = self._public_dirs(base)
        pd_sig = sha("|".join(sorted(str(p) for p in pub_dirs)))
        if cursor.get(_PD_KEY) not in (None, pd_sig):
            cursor = {}  # public_dirs changed: registers differ, re-read every file
        new: dict = {_PD_KEY: pd_sig}
        units: list[Unit] = []
        stale_prune: dict[str, list[str]] = {}
        for p in _walk(root):
            rel = p.relative_to(base).as_posix()
            try:
                st = p.stat()
                if st.st_size > MAX_BYTES:
                    continue
                prev = cursor.get(rel)
                if prev and prev[0] == st.st_mtime_ns:
                    new[rel] = prev
                    continue
                raw = p.read_bytes()
            except OSError:
                continue
            digest = hashlib.sha1(raw).hexdigest()
            new[rel] = [st.st_mtime_ns, digest]
            if prev and prev[1] == digest:
                continue
            file_units = self._file_units(
                rel, raw.decode("utf-8", "replace"), st.st_mtime, (base / rel).resolve(), pub_dirs
            )
            if prev:
                # The file existed before and its content changed (not a first-time read): a paragraph
                # group that is no longer emitted here -- edited away, merged into another, or the whole
                # file trimmed -- must not leave its old unit sitting in the store forever (P2-17).
                # hash_ref(rel) is this file's thread_id, so the engine prunes that thread down to exactly
                # this read's unit_ids (store.write_units' `prune`).
                stale_prune[hash_ref(rel)] = [u.unit_id for u in file_units]
            units.extend(file_units)
        if stale_prune:
            new["_stale_prune"] = stale_prune
        return units, new

    def _public_dirs(self, base: Path) -> list[Path]:
        out = []
        for d in self._config.get("public_dirs") or []:
            try:
                p = Path(str(d)).expanduser()
                out.append((p if p.is_absolute() else base / p).resolve())
            except (OSError, ValueError):
                continue
        return out

    def _file_units(self, rel: str, text: str, mtime: float, full: Path, pub_dirs: list[Path]) -> list[Unit]:
        meta, body = split_frontmatter(text)
        public = meta.get("public", "").lower() in ("true", "yes", "1") or any(full.is_relative_to(d) for d in pub_dirs)
        ts = None
        for key in ("date", "created", "published"):
            ts = parse_iso(meta.get(key, "")) if meta.get(key) else None
            if ts:
                break
        ts = ts or datetime.fromtimestamp(mtime, tz=UTC)
        thread = hash_ref(rel)
        out = []
        for grp in paragraph_groups(clean_body(body)):
            if len(grp.split()) < 3:
                continue
            u = own_unit(
                source=self.name,
                ref=f"{thread}:{sha(grp)}",
                ts=ts,
                text=grp,
                kind="post" if public else "note",
                register="public" if public else "?",
                audience="public" if public else "self",
                thread_id=thread,
                meta={"public": True} if public else {},
            )
            if u is not None:
                out.append(u)
        return out
