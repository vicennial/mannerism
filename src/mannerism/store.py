"""SQLite corpus store: `~/.mannerism/corpus.db`.

One file holds everything the engine learns: units (the user's own text), cursors, labels, features,
the example bank, draft log, edit pairs, rules and work packets. Opened in WAL mode with
`secure_delete=ON` so `forget` really erases text, and chmod 0600 together with its -wal/-shm files.

Connections run in autocommit mode (`isolation_level=None`); multi-statement changes use the
`tx(con)` context manager, which is an explicit BEGIN IMMEDIATE ... COMMIT. This keeps the
"units + cursor in one transaction" guarantee easy to see in the code instead of depending on
Python's implicit transaction rules.

Schema changes: bump SCHEMA_VERSION and add a step to MIGRATIONS. The version lives in `meta`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import core
from .config import ensure_home

SCHEMA_VERSION = 7
INGEST_RUNS_KEEP = 200  # P2-13: a long-lived corpus should not grow this table forever

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS units (
  unit_id TEXT PRIMARY KEY,
  source TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  thread_id TEXT,
  audience TEXT NOT NULL DEFAULT 'unknown',
  audience_ref TEXT,
  ts TEXT NOT NULL,
  text TEXT NOT NULL,
  parts INTEGER NOT NULL DEFAULT 1,
  kind TEXT NOT NULL,
  register TEXT NOT NULL DEFAULT '?',      -- the adapter's default; a label overrides it
  context_json TEXT,
  meta_json TEXT NOT NULL DEFAULT '{}',
  run_id TEXT,
  added_at TEXT NOT NULL,
  account TEXT,                            -- v4: where the text came from (masked), for exclusion rules and review
  project TEXT,
  project_path TEXT,
  session_kind TEXT,
  session TEXT,                            -- hash of the session id
  template TEXT,                           -- hash of the text's opening words (humanness.template_of)
  human_score REAL,                        -- 0..1, humanness.score; NULL = not scored
  held INTEGER NOT NULL DEFAULT 0,         -- 1: kept out of the bank until the user says it is theirs
  score_json TEXT
);
CREATE INDEX IF NOT EXISTS units_source_ts ON units(source, ts);
CREATE INDEX IF NOT EXISTS units_thread ON units(thread_id);
CREATE INDEX IF NOT EXISTS units_ts ON units(ts);
CREATE TABLE IF NOT EXISTS ingest_runs (
  run_id TEXT PRIMARY KEY, source TEXT NOT NULL, location TEXT NOT NULL, started_at TEXT NOT NULL,
  finished_at TEXT, units_written INTEGER, new_units INTEGER, rejected INTEGER, error TEXT
);
CREATE TABLE IF NOT EXISTS cursors (
  source TEXT NOT NULL, location TEXT NOT NULL, cursor_json TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY (source, location)
);
CREATE TABLE IF NOT EXISTS labels (
  unit_id TEXT PRIMARY KEY, register TEXT NOT NULL, quality INTEGER, language TEXT, labeller TEXT NOT NULL,
  labelled_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unit_features (
  unit_id TEXT PRIMARY KEY, version INTEGER NOT NULL, features_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bank (
  rowid INTEGER PRIMARY KEY, unit_id TEXT, register TEXT NOT NULL, source TEXT NOT NULL, thread_id TEXT,
  ts TEXT NOT NULL, text TEXT NOT NULL, words INTEGER NOT NULL, weight REAL NOT NULL,
  tier TEXT NOT NULL DEFAULT 'written',     -- written | edited
  public_ok INTEGER NOT NULL DEFAULT 0, features_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bank_register ON bank(register, ts);
CREATE TABLE IF NOT EXISTS drafts (
  draft_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, register TEXT NOT NULL, origin TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE, text TEXT NOT NULL, target_id TEXT
);
CREATE INDEX IF NOT EXISTS drafts_ts ON drafts(ts);
CREATE TABLE IF NOT EXISTS edits (
  edit_id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, register TEXT NOT NULL, draft TEXT NOT NULL,
  final TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('edited', 'accepted')), origin TEXT NOT NULL,
  unit_id TEXT, draft_id INTEGER, ratio REAL, final_sha TEXT NOT NULL, distilled INTEGER NOT NULL DEFAULT 0,
  thread_key TEXT                           -- thread or task the pair came from (distill needs >= 3 distinct)
);
CREATE INDEX IF NOT EXISTS edits_register ON edits(register, status, distilled);
CREATE INDEX IF NOT EXISTS edits_unit ON edits(unit_id);
CREATE TABLE IF NOT EXISTS rules (
  rule_id INTEGER PRIMARY KEY AUTOINCREMENT, register TEXT NOT NULL, text TEXT NOT NULL,
  support INTEGER NOT NULL DEFAULT 1, contradicted INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL,
  last_seen TEXT NOT NULL, edit_ids_json TEXT NOT NULL DEFAULT '[]', avoid TEXT
);
CREATE INDEX IF NOT EXISTS rules_register ON rules(register);
CREATE TABLE IF NOT EXISTS packets (
  packet_id TEXT PRIMARY KEY, kind TEXT NOT NULL, register TEXT, created_at TEXT NOT NULL,
  payload_json TEXT NOT NULL, schema_json TEXT NOT NULL, done_at TEXT,
  expires_at TEXT, snapshot INTEGER NOT NULL DEFAULT 0,   -- snapshot: bank version of the packet's register
  result_json TEXT, invalidated INTEGER NOT NULL DEFAULT 0
);
-- A unit capture matched to a draft closely enough (>= bank.MOSTLY_AI) to call it AI text the user
-- republished verbatim, recorded even when the match was otherwise ambiguous (P1-4). Kept separate
-- from `edits` (which `forget --drafts` clears) so the exclusion survives that forget (P2-15): the flag
-- lives on the unit, not on the edit-learning log.
CREATE TABLE IF NOT EXISTS ai_units (
  unit_id TEXT PRIMARY KEY, ratio REAL NOT NULL, detected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edit_contradictions (   -- the rules an edit row counted against (undone when it is deleted)
  edit_id INTEGER NOT NULL, rule_id INTEGER NOT NULL, PRIMARY KEY (edit_id, rule_id)
);
-- v4. Exclusion rules written by `exclude add` / the review page (the ones in config.toml are read-only to the
-- code). A NULL column does not constrain; a rule has at least one non-NULL column (exclude.Rule enforces it).
CREATE TABLE IF NOT EXISTS exclusions (
  rule_id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, account TEXT, project TEXT, session_kind TEXT, text TEXT,
  grp TEXT, created_at TEXT NOT NULL
);
-- v4. What the user said about a group of held texts: "keep" = "it's me" (held units are released and stay released).
CREATE TABLE IF NOT EXISTS group_decisions (
  group_id TEXT PRIMARY KEY, decision TEXT NOT NULL CHECK (decision IN ('keep')), decided_at TEXT NOT NULL
);
"""

FTS_DDL = (
    ("CREATE VIRTUAL TABLE IF NOT EXISTS bank_fts USING fts5(text, content='bank', content_rowid='rowid', "
     "tokenize='unicode61 remove_diacritics 2')"),
    ("CREATE TRIGGER IF NOT EXISTS bank_ai AFTER INSERT ON bank BEGIN "
     "INSERT INTO bank_fts(rowid, text) VALUES (new.rowid, new.text); END"),
    ("CREATE TRIGGER IF NOT EXISTS bank_ad AFTER DELETE ON bank BEGIN "
     "INSERT INTO bank_fts(bank_fts, rowid, text) VALUES ('delete', old.rowid, old.text); END"),
)

UNIT_COLUMNS_V4 = (("account", "TEXT"), ("project", "TEXT"), ("project_path", "TEXT"), ("session_kind", "TEXT"),
                   ("session", "TEXT"), ("template", "TEXT"), ("human_score", "REAL"),
                   ("held", "INTEGER NOT NULL DEFAULT 0"), ("score_json", "TEXT"))


def _migrate_v4(con: sqlite3.Connection) -> None:
    """v4: per-unit origin metadata and the machine-typed score. Old units keep NULLs (they match only source / text
    rules, and are not held). Idempotent, and run inside the write transaction `_migrate` opens, so two processes that
    open an old store at the same time cannot both add a column."""
    have = {r[1] for r in con.execute("PRAGMA table_info(units)")}
    for name, decl in UNIT_COLUMNS_V4:
        if name not in have:
            con.execute(f"ALTER TABLE units ADD COLUMN {name} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS units_group ON units(source, account, project, session_kind)")
    con.execute("CREATE INDEX IF NOT EXISTS units_template ON units(template)")
    con.execute("CREATE INDEX IF NOT EXISTS units_session ON units(session)")
    _ensure_indexes(con)


def _ensure_indexes(con: sqlite3.Connection) -> None:
    """Indexes added after a store was migrated to v4. The cross-session screening asks "how many texts has session S"
    for the key `coalesce(session, thread_id)` on every location read; without this expression index that is a scan
    of the whole table."""
    con.execute("CREATE INDEX IF NOT EXISTS units_sess_key ON units(coalesce(session, thread_id))")


# version -> function applied when upgrading a database that is older than that version.
def _migrate_v5(con: sqlite3.Connection) -> None:
    con.execute("CREATE TABLE IF NOT EXISTS rejections (rejection_id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
                "register TEXT NOT NULL, draft TEXT NOT NULL, final TEXT NOT NULL, origin TEXT NOT NULL, "
                "UNIQUE(register,draft,final))")


def _migrate_v6(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS feedback_sources (
        kind TEXT NOT NULL, feedback_id INTEGER NOT NULL, source TEXT NOT NULL,
        source_ref TEXT NOT NULL, unit_id TEXT, published INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(kind,feedback_id,source,source_ref))""")


def _migrate_v7(con: sqlite3.Connection) -> None:
    con.execute("""CREATE TABLE IF NOT EXISTS feedback_drafts (
        kind TEXT NOT NULL, feedback_id INTEGER NOT NULL, draft_id INTEGER NOT NULL,
        PRIMARY KEY(kind,feedback_id,draft_id))""")


MIGRATIONS: dict[int, callable] = {4: _migrate_v4, 5: _migrate_v5, 6: _migrate_v6, 7: _migrate_v7}


def db_path() -> Path:
    return core.home() / "corpus.db"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def ts_iso(ts: datetime) -> str:
    """Canonical stored timestamp: UTC, second precision, so string order is time order."""
    return ts.astimezone(UTC).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    """Open (creating and migrating if needed) the corpus database."""
    ensure_home()
    p = path or db_path()
    fresh = not p.exists()
    if fresh:
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600))
    deadline = time.monotonic() + 3
    last_busy = None
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise last_busy or sqlite3.OperationalError("database startup timed out")
        con = sqlite3.connect(p, timeout=remaining, isolation_level=None)
        con.row_factory = sqlite3.Row
        try:
            con.execute(f"PRAGMA busy_timeout={int(remaining * 1000)}")
            # Concurrent first opens can race on the journal-mode transition.
            # SQLite may report BUSY immediately here despite busy_timeout.
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA secure_delete=ON")
            con.execute("PRAGMA synchronous=NORMAL")
            _migrate(con)
            con.execute("PRAGMA busy_timeout=3000")  # normal operations keep the established timeout
            break
        except sqlite3.OperationalError as exc:
            con.close()
            code = getattr(exc, "sqlite_errorcode", 0) & 0xff
            if code not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) or time.monotonic() >= deadline:
                raise
            last_busy = exc
            time.sleep(min(0.02, max(0, deadline - time.monotonic())))
        except BaseException:
            con.close()
            raise
    for suffix in ("", "-wal", "-shm"):  # SQLite creates the sidecars lazily; tighten whichever exist
        try:
            os.chmod(str(p) + suffix, 0o600)
        except OSError:
            pass
    return con


def _version(con: sqlite3.Connection) -> int:
    try:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        return 0
    return int(row[0]) if row else 0


def _migrate(con: sqlite3.Connection) -> None:
    version = _version(con)
    if version >= SCHEMA_VERSION:
        try:
            _ensure_indexes(con)  # a no-op once it exists
        except sqlite3.OperationalError:
            pass
        return
    con.executescript(SCHEMA)  # CREATE ... IF NOT EXISTS only: safe when another process runs it too
    fts = 1
    try:
        for ddl in FTS_DDL:
            con.execute(ddl)
    except sqlite3.OperationalError:  # SQLite built without FTS5: retrieval just skips the tie-break
        fts = 0
    # The steps that change an existing table are not idempotent across processes (ALTER TABLE ADD COLUMN), so they
    # run in one write transaction and the version is read again once the lock is ours: whoever waited finds the
    # work done.
    with tx(con):
        version = _version(con)
        if version < SCHEMA_VERSION:
            for v in sorted(MIGRATIONS):
                if v > version:
                    MIGRATIONS[v](con)
            set_meta(con, "fts", str(fts))
            set_meta(con, "schema_version", str(SCHEMA_VERSION))


@contextmanager
def tx(con: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One explicit write transaction: BEGIN IMMEDIATE, then COMMIT, or ROLLBACK on any exception."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    else:
        con.execute("COMMIT")


def get_meta(con: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else default


def set_meta(con: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        con.execute("DELETE FROM meta WHERE key=?", (key,))
    else:
        con.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value))


def get_meta_json(con: sqlite3.Connection, key: str, default=None):
    raw = get_meta(con, key)
    if raw is None:
        return default
    try:
        return json.loads(raw)
    except ValueError:
        return default


def set_meta_json(con: sqlite3.Connection, key: str, value) -> None:
    set_meta(con, key, None if value is None else json.dumps(value, ensure_ascii=False, sort_keys=True))


def bump_bank_version(con: sqlite3.Connection, registers) -> None:
    """Bump the per-register bank version (packets store it as their snapshot to detect staleness)."""
    for r in registers:
        v = int(get_meta(con, f"bank_version:{r}", "0") or 0) + 1
        set_meta(con, f"bank_version:{r}", str(v))


def bank_version(con: sqlite3.Connection, register: str | None) -> int:
    return int(get_meta(con, f"bank_version:{register}", "0") or 0) if register else 0


def has_fts(con: sqlite3.Connection) -> bool:
    return get_meta(con, "fts", "0") == "1"


# --- cursors and units ----------------------------------------------------------------------------------------------

def get_cursor(con: sqlite3.Connection, source: str, location: str) -> dict | None:
    row = con.execute("SELECT cursor_json FROM cursors WHERE source=? AND location=?", (source, location)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except ValueError:
        return None


@dataclass
class WriteResult:
    run_id: str
    new_ids: list[str] = field(default_factory=list)  # units that did not exist before
    updated_ids: list[str] = field(default_factory=list)  # units that already existed and were rewritten
    rejected: int = 0  # invalid units the adapter yielded (dropped, cursor still advances)
    pruned: int = 0  # stale units removed by `prune` (a location's old units not re-emitted on re-read)
    deleted_ids: list[str] = field(default_factory=list)  # units removed by `delete_refs` (they existed)
    purged_ids: list[str] = field(default_factory=list)  # units whose old derived data was purged: text edited, or pruned
    deleted_registers: set[str] = field(default_factory=set)  # registers units LEFT (deleted, purged, re-registered)
    edited_ids: list[str] = field(default_factory=list)  # existing units whose text changed (the caller re-pairs them)
    kept_draft_ids: list[int] = field(default_factory=list)  # drafts kept for a re-pair (the caller drops unpaired ones)


def write_units(con: sqlite3.Connection, source: str, loc_key: str, units: Iterable[core.Unit],
                new_cursor: dict | None, *, prune: dict[str, list[str]] | None = None,
                delete_refs: Iterable[str] | None = None) -> WriteResult:
    """Upsert `units` and store `new_cursor` for (source, loc_key) in ONE transaction.

    A crash or error before COMMIT leaves both the units and the cursor untouched, so the next sync
    re-reads the same text and nothing is ever skipped. Units that fail `Unit.validate()` or belong to
    another source are dropped and counted; they must not wedge the cursor forever.

    `prune`: {thread_id: [unit_ids still current]} for any location keyed by `thread_id` that an adapter
    just fully re-read (e.g. an edited markdown note): every OTHER existing unit of that source+thread_id
    is deleted, so a changed file's old paragraph-units do not linger forever (P2-17).

    `delete_refs`: `source_ref`s of this source whose units the source says no longer exist (a JSONL delete
    record). They are removed with everything derived from them (`delete_unit_rows`), in the same transaction as
    the units and the cursor, after the upserts. A unit that is updated with different text is replaced like a
    delete + insert (`delete_unit_rows`): its labels, features, bank rows, linked edits and rules, and all packets
    go, so nothing derived from the old text survives; the agent draft it was paired with is kept (and the unit is
    listed in `edited_ids`, so the caller can pair the new text with it). A unit that only changes register keeps its
    labels. In both cases the register it left is in `deleted_registers`, so the caller rebuilds that bank too.
    Stale units removed by `prune` are purged the same way."""
    started = now_iso()
    run_id = f"{source}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    res = WriteResult(run_id)
    public_revoked = False
    with tx(con):
        for u in units:
            try:
                u.validate()
                if u.source != source:
                    raise ValueError("unit from another source")
            except ValueError:
                res.rejected += 1
                continue
            if not public_revoked:
                invalidate_public_snapshot(con)
                public_revoked = True
            ctx = (json.dumps([{"who": c.who, "text": c.text} for c in u.context], ensure_ascii=False)
                   if u.context else None)
            old = con.execute(
                "SELECT u.text, u.register, coalesce(l.register, u.register) FROM units u "
                "LEFT JOIN labels l ON l.unit_id=u.unit_id WHERE u.unit_id=?", (u.unit_id,)).fetchone()
            existed = old is not None
            if existed and old[0] != u.text:  # an edit: everything derived describes the old text
                # The matched draft is the user's earlier agent draft, not derived from this text: it stays, so the
                # new text can be paired with it again (`edited_ids`); only the pair (the edit row) goes.
                res.kept_draft_ids += [r[0] for r in con.execute(
                    "SELECT draft_id FROM edits WHERE unit_id=? AND draft_id IS NOT NULL", (u.unit_id,))]
                gone = delete_unit_rows(con, [u.unit_id], keep_drafts=True)
                res.edited_ids.append(u.unit_id)
                res.purged_ids += gone["ids"]
                res.deleted_registers |= gone["registers"]
            elif existed and old[1] != u.register:  # the old register's bank still holds this text
                res.deleted_registers.add(old[2])
            con.execute(
                """INSERT INTO units(unit_id, source, source_ref, thread_id, audience, audience_ref, ts, text, parts,
                                     kind, register, context_json, meta_json, run_id, added_at, account, project,
                                     project_path, session_kind, session, template, human_score, held, score_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(unit_id) DO UPDATE SET
                     thread_id=excluded.thread_id, audience=excluded.audience, audience_ref=excluded.audience_ref,
                     ts=excluded.ts, text=excluded.text, parts=excluded.parts, kind=excluded.kind,
                     register=excluded.register, context_json=excluded.context_json, meta_json=excluded.meta_json,
                     run_id=excluded.run_id, account=excluded.account, project=excluded.project,
                     project_path=excluded.project_path, session_kind=excluded.session_kind,
                     session=excluded.session, template=excluded.template, human_score=excluded.human_score,
                     held=excluded.held, score_json=excluded.score_json""",
                (u.unit_id, u.source, u.source_ref, u.thread_id, u.audience, u.audience_ref, ts_iso(u.ts), u.text,
                 u.parts, u.kind, u.register, ctx, json.dumps(u.meta, ensure_ascii=False, sort_keys=True), run_id,
                 started, u.account, u.project, u.project_path, u.session_kind, u.session, u.template, u.human_score,
                 int(bool(u.held)), json.dumps(u.score_info, sort_keys=True) if u.score_info else None))
            if existed:
                res.updated_ids.append(u.unit_id)
            else:
                res.new_ids.append(u.unit_id)
        if prune:
            for thread_id, keep_ids in prune.items():
                keep = set(keep_ids)
                existing = [r[0] for r in con.execute(
                    "SELECT unit_id FROM units WHERE source=? AND thread_id=?", (source, thread_id))]
                stale = [uid for uid in existing if uid not in keep]
                if stale:
                    gone = delete_unit_rows(con, stale)
                    res.pruned += len(stale)
                    res.purged_ids += gone["ids"]
                    res.deleted_registers |= gone["registers"]
        if delete_refs:
            gone = delete_unit_rows(con, [core.unit_id_for(source, ref) for ref in delete_refs])
            res.deleted_ids = gone["ids"]
            res.deleted_registers |= gone["registers"]
        if new_cursor is not None:
            con.execute(
                "INSERT INTO cursors(source, location, cursor_json, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(source, location) DO UPDATE SET cursor_json=excluded.cursor_json, "
                "updated_at=excluded.updated_at",
                (source, loc_key, json.dumps(new_cursor, sort_keys=True), now_iso()))
        con.execute(
            "INSERT INTO ingest_runs(run_id, source, location, started_at, finished_at, units_written, new_units, "
            "rejected) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, source, loc_key, started, now_iso(), len(res.new_ids) + len(res.updated_ids), len(res.new_ids),
             res.rejected))
        _prune_ingest_runs(con)
    return res


def delete_unit_rows(con: sqlite3.Connection, unit_ids: Iterable[str], *, keep_drafts: bool = False) -> dict:
    """Delete the given units and everything derived from them. The caller holds the write transaction.

    Same reach as `forget_rows(source=...)`, for single units: labels, features, bank rows, mostly-AI flags,
    the edits (and the drafts they matched) linked to the units, and rules whose evidence was one of those edits.
    When at least one unit existed, every packet and `blind:*` run is deleted too (they can embed the text);
    the caller rebuilds the bank, stats and cards for `registers`, which the units were in. Unknown ids are
    ignored. `keep_drafts` (a text edit, not a delete): the drafts the edits matched stay in the draft log.
    Returns {"ids": the units that existed, "registers": their effective registers}."""
    wanted = list(dict.fromkeys(unit_ids))
    found: list[str] = []
    regs: set[str] = set()
    for i in range(0, len(wanted), 500):
        part = wanted[i:i + 500]
        ph = ",".join("?" * len(part))
        for r in con.execute(
                f"SELECT u.unit_id, coalesce(l.register, u.register) FROM units u "
                f"LEFT JOIN labels l ON l.unit_id=u.unit_id WHERE u.unit_id IN ({ph})", part):
            found.append(r[0])
            regs.add(r[1])
    for i in range(0, len(found), 500):
        part = found[i:i + 500]
        ph = ",".join("?" * len(part))
        invalidate_public_snapshot(con)
        _delete_feedback_sources(con, unit_ids=part, keep_drafts=keep_drafts)
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_voicekit_rows'").fetchone():
            # Remove private archived copies before the live rows lose their links.
            con.execute(f"DELETE FROM legacy_voicekit_rows WHERE "
                        f"(source_table IN ('units','labels','splits','x_capture') AND target_id IN ({ph})) OR "
                        f"(source_table='edits' AND json_extract(payload_json,'$.status')!='rejected' "
                        f"AND target_id IN (SELECT CAST(edit_id AS TEXT) FROM edits WHERE unit_id IN ({ph}))) OR "
                        f"(source_table='drafts' AND target_id IN (SELECT CAST(draft_id AS TEXT) FROM edits WHERE unit_id IN ({ph})))",
                        [*part, *part, *part])
            con.execute("DELETE FROM legacy_voicekit_cards")
            con.execute("DELETE FROM meta WHERE key='legacy:voicekit:v1'")
        if not keep_drafts:
            con.execute(f"DELETE FROM drafts WHERE draft_id IN (SELECT draft_id FROM edits WHERE draft_id IS NOT NULL "
                        f"AND unit_id IN ({ph}))", part)
        dead_edits = [r[0] for r in con.execute(f"SELECT edit_id FROM edits WHERE unit_id IN ({ph})", part)]
        undo_contradictions(con, dead_edits)
        con.execute(f"DELETE FROM edits WHERE unit_id IN ({ph})", part)
        _delete_rules_referencing(con, dead_edits)
        for table in ("labels", "unit_features", "bank", "ai_units", "units"):
            con.execute(f"DELETE FROM {table} WHERE unit_id IN ({ph})", part)
    if found:
        con.execute("DELETE FROM packets")
        con.execute("DELETE FROM meta WHERE key LIKE 'blind:%' OR key IN ('blind_exclude', 'blind_current')")
    return {"ids": found, "registers": regs}


def undo_contradictions(con: sqlite3.Connection, edit_ids: Iterable[int]) -> None:
    """Give back what these (about to be deleted) edit rows counted against rules, exactly, never below 0."""
    ids = list(dict.fromkeys(edit_ids))
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        ph = ",".join("?" * len(part))
        for (rule_id,) in con.execute(f"SELECT rule_id FROM edit_contradictions WHERE edit_id IN ({ph})", part).fetchall():
            con.execute("UPDATE rules SET contradicted = max(0, contradicted - 1) WHERE rule_id=?", (rule_id,))
        con.execute(f"DELETE FROM edit_contradictions WHERE edit_id IN ({ph})", part)


def drop_unpaired_drafts(con: sqlite3.Connection, draft_ids: Iterable[int]) -> int:
    """Delete these drafts unless an edit row still points at them (a kept draft whose re-pair did not happen)."""
    ids = list(dict.fromkeys(draft_ids))
    n = 0
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        ph = ",".join("?" * len(part))
        n += con.execute(f"DELETE FROM drafts WHERE draft_id IN ({ph}) AND draft_id NOT IN "
                         f"(SELECT draft_id FROM edits WHERE draft_id IS NOT NULL)", part).rowcount
    return n


def _prune_ingest_runs(con: sqlite3.Connection) -> None:
    """Keep only the most recent INGEST_RUNS_KEEP rows (P2-13): a long-lived corpus syncs constantly."""
    con.execute("DELETE FROM ingest_runs WHERE rowid NOT IN (SELECT rowid FROM ingest_runs ORDER BY started_at DESC "
                "LIMIT ?)", (INGEST_RUNS_KEEP,))


def record_run_error(con: sqlite3.Connection, source: str, loc_key: str, error: str) -> None:
    """Log a failed location read (no units, no cursor change)."""
    run_id = f"{source}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:6]}"
    con.execute("INSERT INTO ingest_runs(run_id, source, location, started_at, finished_at, units_written, "
                "new_units, rejected, error) VALUES (?,?,?,?,?,0,0,0,?)",
                (run_id, source, loc_key, now_iso(), now_iso(), error[:500]))
    _prune_ingest_runs(con)


def _delete_rules_referencing(con: sqlite3.Connection, edit_ids: Iterable[int]) -> int:
    """A rule loses the now-deleted edits it cites (P1-6): their ids leave `edit_ids_json` and support becomes the
    number of unique surviving citations. A rule that still cites other edits stays; a rule left with no edit is
    deleted, because its evidence is gone. Returns the number of rules deleted. The edits that remain are not touched:
    they stay distilled, so a rule built from kept text is never learned again from scratch (nor deleted with it)."""
    ids = {int(e) for e in edit_ids}
    if not ids:
        return 0
    dropped = 0
    for r in con.execute("SELECT rule_id, support, edit_ids_json FROM rules").fetchall():
        try:
            cited = [int(x) for x in json.loads(r["edit_ids_json"] or "[]")]
        except (ValueError, TypeError):
            cited = []
        hit = ids.intersection(cited)
        if not hit:
            continue
        left = sorted(set(cited) - ids)
        support = len(left)
        if not left:
            con.execute("DELETE FROM rules WHERE rule_id=?", (r["rule_id"],))
            dropped += 1
        else:
            con.execute("UPDATE rules SET edit_ids_json=?, support=? WHERE rule_id=?",
                        (json.dumps(left), support, r["rule_id"]))
    return dropped


def _rebuild_fts(con: sqlite3.Connection) -> None:
    if has_fts(con):
        try:
            con.execute("INSERT INTO bank_fts(bank_fts) VALUES('rebuild')")
        except sqlite3.OperationalError:
            pass


def forget_rows(con: sqlite3.Connection, source: str | None = None, *, drafts_only: bool = False) -> dict:
    """Delete stored text and everything derived from it.

    - `drafts_only`: drafts, edits and rules (what the agent drafted and what was learned from edits).
      The `ai_units` flag is deliberately NOT touched here: it lives on the unit, not the edit log, so a
      unit correctly excluded as mostly-AI text does not get re-admitted to the bank by this (P2-15).
    - `source`: that source's units, labels, features, cursors, bank rows, ai_units flags, and the edits
      (and the drafts they matched) linked to those units; rules built only from those edits are deleted
      too (not merely invalidated: their supporting evidence is gone).
    - neither: the whole corpus.

    In every case: every packet is deleted (a pending OR already-answered one can still embed the
    forgotten text verbatim in its payload/result), every `blind:*` run is deleted (it embeds the real
    held-out text), and the FTS index is rebuilt. A `VACUUM` to reclaim the freed pages is deliberately
    not run here: it rewrites the whole file and can take a while on a large corpus, so it is left as a
    manual, optional step (`sqlite3 corpus.db VACUUM`) rather than something every forget blocks on.

    Cards, stats and the bank are rebuilt by the caller (`api.forget`)."""
    counts: dict[str, int] = {}
    with tx(con):
        invalidate_public_snapshot(con)
        if source is not None:
            _delete_feedback_sources(con, source=source)
        else:
            con.execute("DELETE FROM feedback_sources")
            con.execute("DELETE FROM feedback_drafts")
        # Legacy archives can embed copies and cross-source references. Erase the
        # entire migration audit on any forget rather than retain hidden copies.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in ("legacy_voicekit_rows", "legacy_voicekit_cards"):
            if table in tables:
                counts[table] = con.execute(f"DELETE FROM {table}").rowcount
        con.execute("DELETE FROM meta WHERE key='legacy:voicekit:v1'")
        if drafts_only:
            for table in ("drafts", "edits", "rules", "edit_contradictions", "rejections"):
                counts[table] = con.execute(f"DELETE FROM {table}").rowcount
            con.execute("DELETE FROM bank WHERE tier='edited' AND unit_id LIKE 'edit:%'")
        elif source is None:
            for table in ("units", "ingest_runs", "cursors", "labels", "unit_features", "bank", "drafts", "edits",
                          "rules", "packets", "ai_units", "edit_contradictions", "rejections"):
                counts[table] = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                con.execute(f"DELETE FROM {table}")
            con.execute("DELETE FROM meta WHERE key NOT IN ('schema_version', 'fts')")
        else:
            ids = "(SELECT unit_id FROM units WHERE source=?)"
            counts["units"] = con.execute("SELECT count(*) FROM units WHERE source=?", (source,)).fetchone()[0]
            for table in ("labels", "unit_features", "ai_units"):
                con.execute(f"DELETE FROM {table} WHERE unit_id IN {ids}", (source,))
            con.execute(f"DELETE FROM drafts WHERE draft_id IN (SELECT draft_id FROM edits WHERE draft_id IS NOT NULL "
                        f"AND unit_id IN {ids})", (source,))
            dead_edits = [r[0] for r in con.execute(f"SELECT edit_id FROM edits WHERE unit_id IN {ids}", (source,))]
            undo_contradictions(con, dead_edits)
            counts["edits"] = con.execute(f"DELETE FROM edits WHERE unit_id IN {ids}", (source,)).rowcount
            counts["rules"] = _delete_rules_referencing(con, dead_edits)
            con.execute(f"DELETE FROM bank WHERE unit_id IN {ids} OR source=?", (source, source))
            con.execute("DELETE FROM units WHERE source=?", (source,))
            counts["cursors"] = con.execute("DELETE FROM cursors WHERE source=?", (source,)).rowcount
            con.execute("DELETE FROM ingest_runs WHERE source=?", (source,))
            con.execute("DELETE FROM meta WHERE key=?", (f"sched:{source}",))
        if drafts_only or source is not None:
            # The `source is None` (forget everything) branch above already cleared packets and meta
            # wholesale; this covers the other two cases, where those tables are not otherwise touched.
            counts["packets"] = con.execute("DELETE FROM packets").rowcount
            con.execute("DELETE FROM meta WHERE key LIKE 'blind:%' OR key IN ('blind_exclude', 'blind_current')")
        _rebuild_fts(con)
    return counts


def invalidate_public_snapshot(con: sqlite3.Connection) -> None:
    """Revoke the file before mutations can commit; a failed rebuild stays unavailable."""
    path = core.home() / "public.db"
    if path.exists() or get_meta(con, "public_snapshot_enabled"):
        set_meta(con, "public_snapshot_enabled", "1")
        set_meta(con, "public_snapshot_dirty", "1")
        path.unlink(missing_ok=True)


def _delete_feedback_sources(con: sqlite3.Connection, *, source: str | None = None,
                             unit_ids: list[str] | None = None, keep_drafts: bool = False) -> None:
    if source is not None:
        rows = con.execute("SELECT kind,feedback_id FROM feedback_sources WHERE source=?", (source,)).fetchall()
    else:
        ids = unit_ids or []
        if not ids:
            return
        ph = ",".join("?" * len(ids))
        rows = con.execute(f"SELECT kind,feedback_id FROM feedback_sources p WHERE p.unit_id IN ({ph}) OR "
                           f"EXISTS (SELECT 1 FROM units u WHERE u.unit_id IN ({ph}) "
                           "AND u.source=p.source AND u.source_ref=p.source_ref)", [*ids, *ids]).fetchall()
    edits = sorted({r[1] for r in rows if r[0] == "edit"})
    undo_contradictions(con, edits)
    _delete_rules_referencing(con, edits)
    for kind, fid in set(map(tuple, rows)):
        # Only matched edits participate in replacement recapture. Rejected
        # drafts have no recapture owner, so erase them with their rejection.
        retain_drafts = keep_drafts and kind == "edit"
        draft_ids = [r[0] for r in con.execute(
            "SELECT draft_id FROM feedback_drafts WHERE kind=? AND feedback_id=?", (kind, fid))]
        if con.execute("SELECT 1 FROM sqlite_master WHERE name='legacy_voicekit_rows'").fetchone():
            op = "=" if kind == "rejection" else "!="
            con.execute("DELETE FROM legacy_voicekit_rows WHERE source_table='edits' AND target_id=? "
                        f"AND json_extract(payload_json,'$.status'){op}'rejected'", (str(fid),))
            if kind == "edit" and not keep_drafts:
                con.execute("DELETE FROM legacy_voicekit_rows WHERE source_table='drafts' AND target_id IN "
                            "(SELECT CAST(draft_id AS TEXT) FROM edits WHERE edit_id=?)", (fid,))
            if not retain_drafts:
                con.executemany("DELETE FROM legacy_voicekit_rows WHERE source_table='drafts' AND target_id=?",
                                [(str(did),) for did in draft_ids])
        if not retain_drafts:
            con.executemany("DELETE FROM drafts WHERE draft_id=?", [(did,) for did in draft_ids])
        if kind == "edit":
            if not keep_drafts:
                con.execute("DELETE FROM drafts WHERE draft_id IN (SELECT draft_id FROM edits WHERE edit_id=?)", (fid,))
            con.execute("DELETE FROM edits WHERE edit_id=?", (fid,))
        elif kind == "rejection":
            con.execute("DELETE FROM rejections WHERE rejection_id=?", (fid,))
        con.execute("DELETE FROM feedback_sources WHERE kind=? AND feedback_id=?", (kind, fid))
        con.execute("DELETE FROM feedback_drafts WHERE kind=? AND feedback_id=?", (kind, fid))
