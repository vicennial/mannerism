"""Your own files: text you wrote, kept in files or folders you point mannerism at (one-time or continuous).

Accepts UTF-8 `.txt`, `.md` and `.jsonl` files from

- the default inbox `~/.mannerism/inbox/files/` (the built-in source named `files`), and
- named instances: `[sources.<instance>]` with `adapter = "files"`, `path` (a file or a folder), `register`,
  `kind` and `enabled`. The instance name is the source name, so texts, cursors and `forget --source` are per instance.

A folder is watched: every sync lists it again, so a file that appears or changes is picked up on the next sync. That
is the continuous path: any script can drop files there. Producers should write a temporary file and rename it into
place (a name that starts with `.` or does not end in `.txt`/`.md`/`.jsonl` is ignored while it is being written).

`.txt` / `.md`: one unit per file, or about 300 words per unit when a file is longer (`.md` also loses frontmatter,
code fences, comments and link syntax, like the `markdown` adapter). The instance's `register` and `kind`
(default `?` and `document`) apply. Cursor: {sig: [ino, size, mtime_ns], sha}.

`.jsonl`: one record per line, schema v1, documented in `docs/custom-sources.md` and checked by `parse_record`:
upsert records create or replace the unit with the record's `id`; delete records remove it and everything derived
from it. Cursor: {gen, ino, offset, lines, head, tail, size, mtime_ns}: the generation counts how many times the
file was replaced (a different inode, a shrunk file or a changed head/tail starts a new generation, re-read from the
start; units dedupe by id). A malformed line stops that file at that line: earlier lines are kept, the cursor stays on
the bad line, and the error names `file:line:reason` only, never the record's text.

Records can never set `Unit.meta`, the source or a trusted `public` flag. A record with register `public` is stored
as public-register text but is NOT `meta.public`, so it never feeds public drafts: those learn only from verified
publications (adapters that know the text was published).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from ..core import REGISTERS, Location, Me, Unit, context_line, hash_ref, mask_pii
from ._util import configured_paths, inbox_dir, own_unit, parse_iso, resolved_key, sha
from .markdown import clean_body, paragraph_groups, split_frontmatter

SCHEMA_VERSION = 1
SUFFIXES = (".txt", ".md", ".jsonl")
KINDS = ("message", "post", "comment", "document")
OPS = ("upsert", "delete")
MAX_RECORD = 1 << 20  # bytes per JSONL line
MAX_DOC_BYTES = 2 << 20  # a .txt / .md file larger than this is skipped with an error
READ_BUDGET = 8 << 20  # new bytes of one JSONL file read per call; the sync loops while `more`
MAX_FILES = 5000  # files listed per instance
EDGE = 4096  # bytes at the start and end of the consumed part that identify a file generation
WORDS = 300
DEFAULT_NAME = "files"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ID_MAX, TEXT_AUDIENCE_MAX, THREAD_MAX, CONTEXT_ITEMS, CONTEXT_MAX = 512, 200, 200, 3, 240
ORIGIN_KEYS = ("provider", "account_id", "native_id", "url")
ORIGIN_MAX = 512
URL_MAX = 2048

UPSERT_REQUIRED = ("schema_version", "op", "id", "ts", "text", "kind", "register")
UPSERT_OPTIONAL = ("audience", "thread_id", "context", "updated_at", "origin")
DELETE_FIELDS = ("schema_version", "op", "id", "ts")

# Unit.audience is a small fixed vocabulary; it follows the register the user chose.
_AUDIENCE = {"close": "close", "group": "group", "work": "colleague", "formal": "unknown", "agent": "agent",
             "public": "public", "?": "unknown"}

_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:[Zz]|[+-]\d{2}:\d{2})$")


class RecordError(ValueError):
    """A JSONL line is not a valid v1 record. The message never contains any of the line's content."""


class JsonSyntaxError(RecordError):
    """The line is not complete, valid JSON (as opposed to valid JSON that is not a valid record)."""


@dataclass(frozen=True)
class Upsert:
    id: str
    ts: datetime
    text: str
    kind: str
    register: str
    audience: str = ""
    thread_id: str | None = None
    context: tuple[str, ...] = ()
    updated_at: datetime | None = None
    origin: dict | None = None


@dataclass(frozen=True)
class Delete:
    id: str
    ts: datetime


# --- validation ----------------------------------------------------------------------------------------------------


def _no_duplicate_keys(pairs):
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise RecordError("duplicate field in object")
        seen[k] = v
    return seen


def _ts(obj: dict, field_name: str) -> datetime:
    v = obj[field_name]
    if not isinstance(v, str) or not _RFC3339.match(v):
        raise RecordError(f"{field_name} must be an RFC 3339 timestamp with an explicit offset, e.g. 2026-10-20T12:00:00Z")
    try:
        return datetime.fromisoformat(v.replace("t", "T").replace("z", "Z")).astimezone(UTC)
    except (ValueError, OverflowError):
        raise RecordError(f"{field_name} is not a real date and time") from None


def _str(obj: dict, key: str, max_len: int, *, allow_empty: bool = False) -> str:
    v = obj[key]
    if not isinstance(v, str):
        raise RecordError(f"{key} must be a string")
    if not allow_empty and not v.strip():
        raise RecordError(f"{key} must not be empty")
    if len(v) > max_len:
        raise RecordError(f"{key} is longer than {max_len} characters")
    return v


def _origin(v) -> dict:
    if not isinstance(v, dict) or set(v) != set(ORIGIN_KEYS):
        raise RecordError(f"origin must be null or an object with exactly: {', '.join(ORIGIN_KEYS)}")
    for k in ORIGIN_KEYS:
        if not isinstance(v[k], str) or not v[k].strip():
            raise RecordError(f"origin.{k} must be a non-empty string")
        if len(v[k]) > (URL_MAX if k == "url" else ORIGIN_MAX):
            raise RecordError(f"origin.{k} is too long")
    parts = urlsplit(v["url"])
    if parts.scheme != "https" or not parts.netloc or not parts.hostname:
        raise RecordError("origin.url must be an absolute https URL")
    return {k: v[k] for k in ORIGIN_KEYS}


def parse_record(raw: bytes) -> Upsert | Delete:
    """Validate one JSONL line (without its newline). Raises RecordError(reason); the reason holds no record text."""
    if len(raw) > MAX_RECORD:
        raise RecordError("record is larger than 1 MiB")
    try:
        line = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RecordError("line is not valid UTF-8") from None
    try:
        obj = json.loads(line, object_pairs_hook=_no_duplicate_keys)
    except RecordError:
        raise
    except RecursionError:
        raise RecordError("JSON is nested too deeply") from None
    except json.JSONDecodeError as e:
        raise JsonSyntaxError(f"invalid JSON ({e.msg}, column {e.colno})") from None
    except ValueError:
        raise JsonSyntaxError("invalid JSON") from None
    if not isinstance(obj, dict):
        raise RecordError("record must be a JSON object")
    if "schema_version" not in obj:
        raise RecordError("missing required field: schema_version")
    sv = obj["schema_version"]
    if type(sv) is not int or sv != SCHEMA_VERSION:
        raise RecordError(f"schema_version must be {SCHEMA_VERSION}")
    if "op" not in obj:
        raise RecordError("missing required field: op")
    if obj["op"] not in OPS:
        raise RecordError(f"op must be one of: {', '.join(OPS)}")
    delete = obj["op"] == "delete"
    required = DELETE_FIELDS if delete else UPSERT_REQUIRED
    allowed = set(required) | (set() if delete else set(UPSERT_OPTIONAL))
    if any(k not in allowed for k in obj):  # the names are not echoed: a key can be any text
        raise RecordError("unknown field in a delete record" if delete else "unknown field")
    missing = [k for k in required if k not in obj]
    if missing:
        raise RecordError(f"missing required field(s): {', '.join(missing)}")
    rid = _str(obj, "id", ID_MAX)
    ts = _ts(obj, "ts")
    if delete:
        return Delete(rid, ts)
    text = _str(obj, "text", MAX_RECORD)
    if obj["kind"] not in KINDS:
        raise RecordError(f"kind must be one of: {', '.join(KINDS)}")
    if obj["register"] not in REGISTERS:
        raise RecordError(f"register must be one of: {', '.join(REGISTERS)}")
    audience = _str(obj, "audience", TEXT_AUDIENCE_MAX, allow_empty=True) if "audience" in obj else ""
    thread = None
    if obj.get("thread_id") is not None:
        thread = _str(obj, "thread_id", THREAD_MAX)
    context: tuple[str, ...] = ()
    if "context" in obj:
        ctx = obj["context"]
        if not isinstance(ctx, list) or len(ctx) > CONTEXT_ITEMS:
            raise RecordError(f"context must be a list of at most {CONTEXT_ITEMS} strings")
        for item in ctx:
            if not isinstance(item, str) or len(item) > CONTEXT_MAX:
                raise RecordError(f"each context item must be a string of at most {CONTEXT_MAX} characters")
        context = tuple(ctx)
    updated = _ts(obj, "updated_at") if obj.get("updated_at") is not None else None
    origin = _origin(obj["origin"]) if obj.get("origin") is not None else None
    return Upsert(rid, ts, text, obj["kind"], obj["register"], audience, thread, context, updated, origin)


def ref_for(record_id: str) -> str:
    """The `source_ref` a record id is stored under: the id itself, unless masking would change it (an id that
    holds an email address, a phone number or a token), in which case a hash of it. A delete record uses the same."""
    return record_id if mask_pii(record_id) == record_id else "id:" + sha(record_id, 16)


def to_unit(source: str, rec: Upsert) -> Unit | None:
    """The masked unit for an upsert record, or None when nothing usable is left (empty or a bare URL)."""
    meta: dict = {}
    if rec.origin:
        meta["origin"] = {k: mask_pii(v) for k, v in rec.origin.items()}
    if rec.updated_at:
        meta["updated_at"] = rec.updated_at.isoformat(timespec="seconds")
    context = [context_line("other1", c) for c in rec.context if c.strip()]
    return own_unit(
        source=source, ref=ref_for(rec.id), ts=rec.ts, text=rec.text, kind=rec.kind, register=rec.register,
        audience=_AUDIENCE[rec.register], thread_id=hash_ref(mask_pii(rec.thread_id)) if rec.thread_id else None,
        audience_ref=hash_ref(mask_pii(rec.audience)) if rec.audience.strip() else None,
        context=[c for c in context if c.text.strip()], meta=meta)


# --- check (no storage) --------------------------------------------------------------------------------------------


@dataclass
class CheckReport:
    records: int = 0
    upserts: int = 0
    deletes: int = 0
    public: int = 0
    errors: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason)
    more_errors: int = 0  # errors beyond the first MAX_CHECK_ERRORS


MAX_CHECK_ERRORS = 100


def check_file(path: Path) -> CheckReport:
    """Validate every line of a JSONL file locally. Reads only; stores and logs nothing. OSError if unreadable."""
    rep = CheckReport()
    with open(path, "rb") as f:
        n = 0
        while True:
            raw = f.readline(MAX_RECORD + 2)
            if not raw:
                break
            n += 1
            if not raw.endswith(b"\n") and len(raw) > MAX_RECORD + 1:  # a line longer than the cap: skip the rest of it
                while True:
                    rest = f.readline(1 << 20)
                    if not rest or rest.endswith(b"\n"):
                        break
            line = raw.rstrip(b"\r\n")
            if n == 1 and line.startswith(b"\xef\xbb\xbf"):
                line = line[3:]
            if not line.strip():
                continue
            try:
                rec = parse_record(line)
            except RecordError as e:
                if len(rep.errors) < MAX_CHECK_ERRORS:
                    rep.errors.append((n, str(e)))
                else:
                    rep.more_errors += 1
                continue
            rep.records += 1
            if isinstance(rec, Delete):
                rep.deletes += 1
            else:
                rep.upserts += 1
                rep.public += rec.register == "public"
    return rep


# --- the source ----------------------------------------------------------------------------------------------------


def _unbom(line: bytes, first: bool) -> bytes:
    """Drop a UTF-8 byte order mark from the first line of a file (Windows editors add one)."""
    return line[3:] if first and line.startswith(b"\xef\xbb\xbf") else line


def _hash_range(f, start: int, end: int) -> str:
    f.seek(start)
    return hashlib.sha1(f.read(max(0, end - start))).hexdigest()


def _group_text(body: str, words: int = WORDS) -> list[str]:
    """Blank-line separated paragraphs grouped to about `words` words; a short file is one group."""
    groups, cur, n = [], [], 0
    for blk in re.split(r"\n\s*\n", body.replace("\r\n", "\n")):
        blk = blk.strip()
        if not blk:
            continue
        w = len(blk.split())
        if cur and n + w > words:
            groups.append("\n\n".join(cur))
            cur, n = [], 0
        cur.append(blk)
        n += w
    if cur:
        groups.append("\n\n".join(cur))
    return groups


def _is_regular(path: Path) -> bool:
    """A regular file (a symlink to one counts). Never a FIFO, socket or device: opening those can block forever."""
    try:
        return stat.S_ISREG(os.stat(path).st_mode)
    except OSError:
        return False


def _open_regular(path: Path):
    """(binary file, fstat) for a regular file, or (None, None) when `path` is not one. Opens with O_NONBLOCK and
    checks the opened descriptor, so a file swapped for a FIFO after discovery cannot hang a sync."""
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            os.close(fd)
            return None, None
        return os.fdopen(fd, "rb"), st
    except BaseException:
        try:
            os.close(fd)
        except OSError:
            pass
        raise


def _iter_files(root: Path) -> Iterable[Path]:
    if root.suffix.lower() in SUFFIXES and _is_regular(root):
        yield root
        return
    if not root.is_dir():
        return
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            p = Path(dirpath) / name
            if not name.startswith(".") and name.lower().endswith(SUFFIXES) and _is_regular(p):
                yield p


class Files:
    tier = "auto"  # cheap and local; but only ever read once the user enabled it (never auto-enabled by `init`)

    def __init__(self, name: str = DEFAULT_NAME):
        self.name = name
        self._register = "?"
        self._kind = "document"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            config = config if isinstance(config, dict) else {}
            reg, kind = config.get("register"), config.get("kind")
            self._register = reg if reg in REGISTERS else "?"
            self._kind = kind if kind in KINDS else "document"
            roots: list[Path] = []
            if isinstance(config.get("path"), str) and config["path"].strip():
                roots.append(Path(config["path"]).expanduser())
            roots += configured_paths(config)
            if self.name == DEFAULT_NAME:
                roots.append(inbox_dir("files"))
            found: dict[str, tuple[Path, str]] = {}
            for root in roots:
                if not root.exists():
                    continue
                base = root.parent if root.is_file() else root
                for p in _iter_files(root):
                    key = resolved_key(p)
                    if key not in found:
                        found[key] = (p, p.relative_to(base).as_posix())
                    if len(found) >= MAX_FILES:
                        break
            return [Location(self.name, k, Path(k), f"{self.name}: {rel}") for k, (_, rel) in sorted(found.items())]
        except Exception:  # noqa: BLE001 - discover never raises
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        if loc.path is None:
            return [], dict(cursor or {})
        if loc.path.suffix.lower() == ".jsonl":
            return self._read_jsonl(loc, dict(cursor or {}))
        return self._read_document(loc, dict(cursor or {}))

    # .txt / .md ---------------------------------------------------------------------------------------------------

    def _read_document(self, loc: Location, cursor: dict) -> tuple[list[Unit], dict]:
        path = loc.path
        f, st = _open_regular(path)
        if f is None:  # not a regular file any more: skipped
            return [], cursor
        with f:
            sig = [st.st_ino, st.st_size, st.st_mtime_ns]
            if cursor.get("sig") == sig:
                return [], cursor
            if st.st_size > MAX_DOC_BYTES:
                raise ValueError("file is larger than 2 MiB")
            raw = f.read(MAX_DOC_BYTES + 1)
        if len(raw) > MAX_DOC_BYTES:
            raise ValueError("file is larger than 2 MiB")
        digest = hashlib.sha1(raw).hexdigest()
        new = {"sig": sig, "sha": digest}
        if cursor.get("sha") == digest:
            return [], new
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ValueError("file is not valid UTF-8") from None
        ts = datetime.fromtimestamp(st.st_mtime, tz=UTC)
        if path.suffix.lower() == ".md":
            front, body = split_frontmatter(text)
            body = clean_body(body)
            for key in ("date", "created", "published"):
                dated = parse_iso(front.get(key, "")) if front.get(key) else None
                if dated:
                    ts = dated
                    break
            groups = paragraph_groups(body)
        else:
            groups = _group_text(text)
        thread = hash_ref(loc.key)  # the resolved absolute path: unique per file, so two files never prune each other
        units = []
        for grp in groups:
            u = own_unit(source=self.name, ref=f"{thread}:{sha(grp)}", ts=ts, text=grp, kind=self._kind,
                         register=self._register, audience=_AUDIENCE[self._register], thread_id=thread)
            if u is not None:
                units.append(u)
        if cursor:
            # The file was read before and changed: a chunk that is no longer there must not stay stored.
            new["_stale_prune"] = {thread: [u.unit_id for u in units]}
        return units, new

    # .jsonl -------------------------------------------------------------------------------------------------------

    def _resume(self, f, st: os.stat_result, cursor: dict) -> tuple[int, int, int]:
        """(offset, line number, generation) to continue from; offset 0 and the next generation when the file was replaced."""
        gen = int(cursor.get("gen") or 0)
        try:
            off, lines = int(cursor.get("offset") or 0), int(cursor.get("lines") or 0)
        except (TypeError, ValueError):
            off, lines = 0, 0
        if off > 0:
            same = (cursor.get("ino") == st.st_ino and st.st_size >= off
                    and cursor.get("head") == _hash_range(f, 0, min(EDGE, off))
                    and cursor.get("tail") == _hash_range(f, max(0, off - EDGE), off))
            if same:
                return off, lines, gen
            return 0, 0, gen + 1
        return 0, 0, gen or 1

    def _read_jsonl(self, loc: Location, cursor: dict) -> tuple[list[Unit], dict]:
        path = loc.path
        f, st = _open_regular(path)
        if f is None:  # not a regular file any more: skipped
            return [], cursor
        if (cursor.get("offset") == st.st_size and cursor.get("ino") == st.st_ino
                and cursor.get("mtime_ns") == st.st_mtime_ns and not cursor.get("more")):
            f.close()
            return [], cursor  # nothing changed since the last read
        with f:
            off, line_no, gen = self._resume(f, st, cursor)
            f.seek(off)
            data = f.read(READ_BUDGET)
            at_eof = len(data) < READ_BUDGET
            ops: dict[str, Unit | None] = {}  # ref -> the unit to store, or None to delete it
            pos, error, unterminated = 0, None, False
            if cursor.get("nl_pending") and off == cursor.get("offset"):
                # The last read accepted a final line that had no newline; its newline is not a new line.
                pos = 2 if data.startswith(b"\r\n") else 1 if data.startswith(b"\n") else 0
            while pos < len(data):
                nl = data.find(b"\n", pos)
                if nl < 0:
                    tail = data[pos:]
                    if len(tail) > MAX_RECORD:
                        error = f"{path}:{line_no + 1}:record is larger than 1 MiB"
                    elif at_eof and tail.strip():
                        # A last line with no newline: complete if it is valid JSON, otherwise still being written.
                        try:
                            self._apply(parse_record(_unbom(tail.rstrip(b"\r"), line_no == 0 and off == 0)), ops)
                        except JsonSyntaxError:
                            pass
                        except RecordError as e:
                            error = f"{path}:{line_no + 1}:{e}"
                        else:
                            line_no += 1
                            pos = len(data)
                            unterminated = True
                    break
                line = _unbom(data[pos:nl].rstrip(b"\r"), line_no == 0 and off == 0)
                if line.strip():
                    try:
                        rec = parse_record(line)
                    except RecordError as e:
                        error = f"{path}:{line_no + 1}:{e}"
                        break
                    self._apply(rec, ops)
                line_no += 1
                pos = nl + 1
            new_off = off + pos
            cur = {"gen": gen, "ino": st.st_ino, "offset": new_off, "lines": line_no, "size": st.st_size,
                   "mtime_ns": st.st_mtime_ns, "head": _hash_range(f, 0, min(EDGE, new_off)),
                   "tail": _hash_range(f, max(0, new_off - EDGE), new_off)}
        if unterminated:
            cur["nl_pending"] = True
        if error is None and not at_eof and pos > 0:
            cur["more"] = True
        units = [u for u in ops.values() if u is not None]
        gone = [ref for ref, u in ops.items() if u is None]
        if gone:
            cur["_delete_refs"] = gone
        if error:
            cur["_error"] = error
        return units, cur

    def _apply(self, rec: Upsert | Delete, ops: dict[str, Unit | None]) -> None:
        ref = ref_for(rec.id)
        if isinstance(rec, Delete):
            ops[ref] = None
            return
        # An upsert with nothing usable left (empty after masking, a bare URL) means the current version of
        # this id has no style evidence in it: the previous version, if any, must not stay.
        ops[ref] = to_unit(self.name, rec)

