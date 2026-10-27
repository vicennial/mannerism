"""Read-only Voice Kit conversion, with local audit records and resumable rebuilds."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from . import bank, capture, cards, core, refresh, store
from .config import load_config

REGISTER_MAP = {"public": "public", "casual_close": "close", "casual_group": "group",
                "semiformal": "work", "formal": "formal", "directive": "agent"}
SOURCE_MAP = {"whatsapp": "whatsapp_db"}
_MARKER = "legacy:voicekit:v1"
_TABLES = ("units", "labels", "edits", "x_capture", "splits", "ingest_runs", "drafts")


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(raw) -> dict:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("legacy metadata must be a JSON object")
    return value


def _read_db(path: Path, *, required: bool = False) -> dict[str, list[dict]]:
    """Read corpus data without writes; SQLite may create WAL/SHM coordination sidecars.

    Keep a real read transaction: immutable=1 would silently miss a live WAL.
    """
    try:
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA query_only=ON")
            con.execute("BEGIN")
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if required and "units" not in tables:
                raise ValueError("legacy corpus has no units table")
            return {name: sorted((dict(r) for r in con.execute(f'SELECT * FROM "{name}"')), key=_json)
                    for name in _TABLES if name in tables}
    except (sqlite3.Error, OSError) as exc:
        raise ValueError("cannot read legacy SQLite source") from exc


def _snapshot(path: Path) -> dict:
    root = path if path.is_dir() else path.parent
    corpus = root / "corpus.db" if path.is_dir() else path
    data = {"corpus": _read_db(corpus, required=True)}
    drafts = root / "drafts.db"
    if drafts.exists() and drafts.resolve() != corpus.resolve():
        data["drafts"] = _read_db(drafts)
    originals = []
    card_dir = root / "cards"
    if card_dir.exists():
        # Only direct Markdown cards, never packet directories or arbitrary linked paths.
        for p in sorted(card_dir.glob("*.md")):
            if p.is_symlink():
                raise ValueError("legacy cards must not be symbolic links")
            originals.append({"name": p.name, "text": p.read_text(encoding="utf-8")})
    manifest = root / "migration-cards.json"
    return {"databases": data, "cards": originals,
            "card_provenance": _object(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}}


def _latest(rows: list[dict]) -> dict[str, dict]:
    selected = {}
    # Highest schema version wins, then latest timestamp, then model and full row.
    # Never depend on SQLite's row order or an arbitrary GROUP BY result.
    def order(row):
        ts = datetime.fromisoformat(row["labeled_at"].replace("Z", "+00:00"))
        if ts.tzinfo is None:
            raise ValueError("legacy label timestamp must include a timezone")
        return int(row["version"]), ts, row["model"], _json(row)

    for row in sorted(rows, key=order):
        selected[row["unit_id"]] = row
    return selected


def _register(lab: dict, row: dict) -> str:
    reg = lab.get("register")
    if row.get("kind") == "draft" or reg == "draft":
        return core.SKIP_REGISTER
    if row["source"] == "whatsapp" and reg in ("casual_close", "casual_group"):
        reg = "casual_group" if row.get("audience_kind") == "group" else "casual_close"
    return REGISTER_MAP.get(reg, core.SKIP_REGISTER)


def _prepare(snapshot: dict) -> tuple[list[dict], dict]:
    corpus = snapshot["databases"]["corpus"]
    latest = _latest(corpus.get("labels", []))
    units = []
    counts = dict.fromkeys((*core.REGISTERS, core.SKIP_REGISTER), 0)
    seen = set()
    mapped_ids = set()
    for row in corpus["units"]:
        uid = row["unit_id"]
        if uid in seen:
            raise ValueError("duplicate legacy unit identity")
        seen.add(uid)
        label_row = latest.get(uid)
        lab = _object(label_row["labels_json"]) if label_row else {}
        reg = _register(lab, row)
        meta = _object(row.get("meta_json"))
        quality = lab.get("quality")
        if type(quality) is not int or quality not in range(1, 6):
            quality = 0  # missing quality must never become an unlabelled, eligible example
        public = ((row["source"] == "x" and row["kind"] in ("post", "reply")) or meta.get("public") is True)
        public = public and lab.get("private_fact") is False and lab.get("third_party_detail") is False
        meta["public"] = bool(public)
        assisted = bool(meta.get("ai_assist_possible")) and ("\n\n" in row["text"] or len(row["text"].split()) >= 30)
        meta["ai"] = bool(meta.get("ai") or lab.get("ai_suspect") or assisted)
        meta["legacy_voicekit"] = {"unit": row, "selected_label": label_row}
        ts = datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
        if ts.tzinfo is None or not row["text"].strip():
            raise ValueError("invalid legacy unit timestamp or text")
        audience = row.get("audience_kind", "unknown")
        audience = "close" if audience == "family" else audience
        kind = "note" if row["kind"] == "draft" else row["kind"]
        if kind not in core.KINDS or audience not in core.AUDIENCES:
            raise ValueError("unsupported legacy unit kind or audience")
        mapped_source = SOURCE_MAP.get(row["source"], row["source"])
        mapped_id = core.unit_id_for(mapped_source, row["source_ref"]) if mapped_source != row["source"] else uid
        if mapped_id in mapped_ids:
            raise ValueError("legacy source mapping produces duplicate unit identities")
        mapped_ids.add(mapped_id)
        units.append({"row": row, "source": mapped_source, "unit_id": mapped_id,
                      "reg": reg, "meta": meta, "quality": quality, "lab": lab,
                      "label_row": label_row, "ts": store.ts_iso(ts), "kind": kind, "audience": audience})
        counts[reg] += 1
    return units, counts


def _target_state(target: Path, fingerprint: str) -> dict | None:
    if not target.exists() or not any(target.iterdir()):
        return None
    db = target / "corpus.db"
    if not db.is_file() or db.is_symlink():
        raise ValueError("destination is populated and is not a recognized legacy import")
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            row = con.execute("SELECT value FROM meta WHERE key=?", (_MARKER,)).fetchone()
            state = _object(row[0]) if row else {}
    except (sqlite3.Error, ValueError) as exc:
        raise ValueError("destination is not a recognized legacy import") from exc
    if state.get("fingerprint") != fingerprint or state.get("phase") not in ("prepared", "data", "complete"):
        raise ValueError("destination is populated or belongs to a different legacy snapshot")
    return state


def _populated(con: sqlite3.Connection) -> bool:
    tables = con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'meta' "
                         "AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'bank_fts%'").fetchall()
    for (name,) in tables:
        quoted = '"' + name.replace('"', '""') + '"'
        if con.execute(f"SELECT 1 FROM {quoted} LIMIT 1").fetchone():
            return True
    return False


def _audit_schema(con: sqlite3.Connection) -> None:
    # Matches store schemas v5/v6/v7; kept local so the importer also works against its v4 base.
    con.execute("""CREATE TABLE IF NOT EXISTS rejections (
        rejection_id INTEGER PRIMARY KEY, ts TEXT NOT NULL, register TEXT NOT NULL,
        draft TEXT NOT NULL, final TEXT NOT NULL, origin TEXT NOT NULL,
        UNIQUE(register,draft,final))""")
    con.execute("""CREATE TABLE IF NOT EXISTS feedback_sources (
        kind TEXT NOT NULL, feedback_id INTEGER NOT NULL, source TEXT NOT NULL,
        source_ref TEXT NOT NULL, unit_id TEXT,
        published INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(kind,feedback_id,source,source_ref))""")
    con.execute("""CREATE TABLE IF NOT EXISTS feedback_drafts (
        kind TEXT NOT NULL, feedback_id INTEGER NOT NULL, draft_id INTEGER NOT NULL,
        PRIMARY KEY(kind,feedback_id,draft_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS legacy_voicekit_rows (
        source_db TEXT NOT NULL, source_table TEXT NOT NULL, source_key TEXT NOT NULL,
        payload_json TEXT NOT NULL, target_id TEXT,
        PRIMARY KEY(source_db, source_table, source_key))""")
    con.execute("""CREATE TABLE IF NOT EXISTS legacy_voicekit_cards (
        name TEXT PRIMARY KEY, text TEXT NOT NULL, provenance_json TEXT NOT NULL,
        status TEXT NOT NULL, problems_json TEXT NOT NULL DEFAULT '[]')""")


def _write_data(con: sqlite3.Connection, snapshot: dict, units: list[dict]) -> None:
    for item in units:
        row, lab, label = item["row"], item["lab"], item["label_row"]
        uid = item["unit_id"]
        con.execute("""INSERT INTO units(unit_id, source, source_ref, thread_id, audience, audience_ref,
            ts, text, parts, kind, register, context_json, meta_json, run_id, added_at, held,
            human_score, score_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (uid, item["source"], row["source_ref"], row.get("thread_id"), item["audience"],
                     row.get("audience_ref"), item["ts"], row["text"], row.get("parts", 1), item["kind"],
                     item["reg"], row.get("context_json"), _json(item["meta"]), row.get("run_id"),
                     store.now_iso(), int(bool(row.get("held") or item["meta"].get("held"))),
                     row.get("human_score"), row.get("score_json")))
        con.execute("INSERT INTO labels VALUES (?,?,?,?,?,?)",
                    (uid, item["reg"], item["quality"], lab.get("language"),
                     "voicekit:" + label["model"] if label else "voicekit:unlabelled",
                     label["labeled_at"] if label else store.now_iso()))
        if item["meta"].get("ai"):
            con.execute("INSERT INTO ai_units VALUES (?,?,?)", (uid, 1.0, store.now_iso()))

    remapped = {u["row"]["unit_id"]: u["unit_id"] for u in units}
    mapped_units = {u["unit_id"]: u for u in units}
    native = {(u["row"]["source"], str(u["row"]["source_ref"])): u["unit_id"] for u in units}
    by_text = {}
    for item in units:
        key = " ".join(core.mask_pii(item["row"]["text"]).lower().split())
        by_text.setdefault((item["reg"], key), []).append(item["unit_id"])
        by_text.setdefault((None, key), []).append(item["unit_id"])
    draft_ids = {}
    for db_name, tables in snapshot["databases"].items():
        for row in tables.get("drafts", []):
            reg = REGISTER_MAP.get(row["register"], core.SKIP_REGISTER)
            text = core.mask_pii(row["text"].strip())
            sha = hashlib.sha256(text.encode()).hexdigest()
            con.execute("INSERT INTO drafts(ts,register,origin,sha256,text,target_id) VALUES (?,?,?,?,?,?) "
                        "ON CONFLICT(sha256) DO NOTHING",
                        (row["ts"], reg, row["origin"], sha, text, row.get("target_id")))
            draft_ids[(db_name, row["draft_id"])] = con.execute(
                "SELECT draft_id FROM drafts WHERE sha256=?", (sha,)).fetchone()[0]
    edit_ids = {}
    corpus = snapshot["databases"]["corpus"]
    captures = {str(r["post_id"]): r for r in corpus.get("x_capture", [])}
    for row in corpus.get("edits", []):
        if row["status"] not in ("accepted", "edited", "rejected"):
            continue  # unknown feedback remains in the audit, never in the bank or rule distillation
        reg = REGISTER_MAP.get(row["register"], core.SKIP_REGISTER)
        task = row.get("task") or ""
        post_id = task[2:] if task.startswith("x:") and task[2:] else None
        cap = captures.get(post_id)
        # Legacy capture writes both rows with the same timestamp, status and native post id.
        # A task string, origin alone, unmatched decision or stale decision cannot prove publication.
        trusted_capture = bool(row.get("origin") == "x-capture" and cap
                               and row["status"] in ("edited", "accepted")
                               and cap["status"] == row["status"] and cap["captured_at"] == row["ts"])
        linked = remapped.get(row.get("unit_id"))
        if post_id:
            linked = linked or native.get(("x", post_id))
        if linked is None and row.get("origin") != "x-capture":
            key = " ".join(core.mask_pii(row["final"]).lower().split())
            matches = by_text.get((reg, key)) or by_text.get((None, key), [])
            linked = sorted(matches)[0] if matches else None
        draft, final = core.mask_pii(row["draft"]), core.mask_pii(row["final"])
        draft_id = draft_ids.get(("corpus", row.get("draft_id")))
        if draft_id is None:
            draft_row = con.execute("SELECT draft_id FROM drafts WHERE sha256=?",
                                    (hashlib.sha256(core.mask_pii(row["draft"].strip()).encode()).hexdigest(),)).fetchone()
            draft_id = draft_row[0] if draft_row else None
        if row["status"] == "rejected":
            con.execute("INSERT INTO rejections(ts,register,draft,final,origin) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(register,draft,final) DO NOTHING",
                        (row["ts"], reg, draft, final, row.get("origin") or "voicekit"))
            feedback_id = con.execute(
                "SELECT rejection_id FROM rejections WHERE register=? AND draft=? AND final=?",
                (reg, draft, final)).fetchone()[0]
            edit_ids[row["edit_id"]] = feedback_id
            if draft_id is not None:
                con.execute("INSERT OR IGNORE INTO feedback_drafts VALUES (?,?,?)", ("rejection", feedback_id, draft_id))
            _feedback_sources(con, "rejection", feedback_id, mapped_units.get(linked), post_id, False)
            continue
        score = cap.get("ratio") if trusted_capture else None
        if score is None:
            score = row.get("ratio")
        if score is None:
            score = capture.ratio(capture.clean(draft).lower(), capture.clean(final).lower())
        ratio = round(score, 3)
        # Public standalone feedback is excluded by bank.build(public_ok=0). Retain it for
        # feedback processing without inventing a unit or treating the final as publication proof.
        # Other registers do admit standalone finals, so their unlinked captures stay audit-only.
        if row.get("origin") == "x-capture" and linked is None and reg != "public":
            continue
        # Use capture.add_edit's masking, comparison and deduplication contract without its nested transaction.
        fsha = hashlib.sha256(capture.clean(final).lower().encode()).hexdigest()
        existing = con.execute("SELECT edit_id,unit_id FROM edits WHERE final_sha=? AND draft=?", (fsha, draft)).fetchone()
        if existing:
            feedback_id = existing["edit_id"]
            if linked and not existing["unit_id"]:
                con.execute("UPDATE edits SET unit_id=? WHERE edit_id=?", (linked, feedback_id))
        else:
            cur = con.execute("""INSERT INTO edits(ts,register,draft,final,status,origin,unit_id,draft_id,
                ratio,final_sha,distilled,thread_key) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (row["ts"], reg, draft, final, row["status"], row.get("origin") or "voicekit",
                               linked, draft_id, ratio, fsha, 0, row.get("thread_key") or task or None))
            feedback_id = cur.lastrowid
        edit_ids[row["edit_id"]] = feedback_id
        if draft_id is not None:
            con.execute("INSERT OR IGNORE INTO feedback_drafts VALUES (?,?,?)", ("edit", feedback_id, draft_id))
        _feedback_sources(con, "edit", feedback_id, mapped_units.get(linked), post_id, trusted_capture)
        if linked and (row["status"] == "accepted" or ratio >= bank.MOSTLY_AI):
            con.execute("INSERT OR REPLACE INTO ai_units VALUES (?,?,?)", (linked, ratio, store.now_iso()))
    for pid, row in captures.items():
        uid = native.get(("x", pid))
        ratio = row.get("ratio")
        if uid and (row["status"] == "accepted" or (ratio is not None and ratio >= bank.MOSTLY_AI)):
            con.execute("INSERT OR REPLACE INTO ai_units VALUES (?,?,?)", (uid, ratio or 1.0, store.now_iso()))
    for db_name, tables in snapshot["databases"].items():
        for table, rows in tables.items():
            for i, row in enumerate(rows):
                key = str(row.get("edit_id", row.get("draft_id", row.get("unit_id", i))))
                # Composite label keys and repeated rows are preserved without replacing earlier versions.
                if table == "labels":
                    key = _json([row["unit_id"], row["version"], row["model"]])
                target_id = (edit_ids.get(row.get("edit_id")) if table == "edits" else
                             draft_ids.get((db_name, row.get("draft_id"))) if table == "drafts" else
                             remapped.get(row.get("unit_id")) if table in ("units", "labels", "splits") else
                             native.get(("x", str(row.get("post_id")))) if table == "x_capture" else None)
                con.execute("INSERT INTO legacy_voicekit_rows VALUES (?,?,?,?,?)",
                            (db_name, table, key, _json(row), target_id))
    for card in snapshot["cards"]:
        provenance = snapshot["card_provenance"].get(card["name"], {})
        con.execute("INSERT INTO legacy_voicekit_cards(name,text,provenance_json,status) VALUES (?,?,?,?)",
                    (card["name"], card["text"], _json(provenance), "archived"))


def _feedback_sources(con: sqlite3.Connection, kind: str, feedback_id: int, unit: dict | None,
                      post_id: str | None, trusted_capture: bool) -> None:
    if unit:
        # Mapped unit provenance is authoritative for identity. Never upgrade its privacy decision
        # using a task string, even when a legacy capture decision exists for another post.
        con.execute("INSERT OR IGNORE INTO feedback_sources VALUES (?,?,?,?,?,?)",
                    (kind, feedback_id, unit["source"], unit["row"]["source_ref"], unit["unit_id"],
                     int(kind == "edit" and unit["meta"].get("public") is True)))
    if post_id and (not unit or unit["source"] != "x" or unit["row"]["source_ref"] != post_id):
        # A structured legacy task can identify an erasure association without proving publication.
        # Only matching native capture records for an unlinked edit carry published=1.
        con.execute("INSERT OR IGNORE INTO feedback_sources VALUES (?,?,?,?,?,?)",
                    (kind, feedback_id, "x", post_id, None, int(kind == "edit" and trusted_capture and not unit)))


def _restore_cards(con: sqlite3.Connection) -> dict:
    counts = {"activated": 0, "archived": 0}
    me = load_config().me
    # Legacy cards could borrow across registers or use units excluded by today's bank criteria.
    # Track all current unit sources, including sources with no eligible bank rows.
    evidence = con.execute("SELECT b.source,u.unit_id FROM bank b LEFT JOIN units u ON u.unit_id=b.unit_id").fetchall()
    sources = sorted({r[0] for r in con.execute("SELECT DISTINCT source FROM units")}
                     | {r["source"] for r in evidence if r["unit_id"]})
    attributable = bool(sources) and all(r["unit_id"] for r in evidence)
    for row in con.execute("SELECT * FROM legacy_voicekit_cards ORDER BY name").fetchall():
        original_provenance = json.loads(row["provenance_json"])
        provenance = original_provenance if isinstance(original_provenance, dict) else {}
        reg = REGISTER_MAP.get(Path(row["name"]).stem)
        digest = hashlib.sha256(row["text"].encode()).hexdigest()
        reviewed = (provenance.get("reviewed") is True and provenance.get("sha256") == digest
                    and all(isinstance(provenance.get(k), str) and provenance[k].strip()
                            for k in ("reviewed_at", "reviewer")))
        problems = cards.validate_agent_card(row["text"], me)
        if not reg or not reviewed:
            problems.append("no explicit matching review provenance")
        if reg and not con.execute("SELECT 1 FROM bank WHERE register=? LIMIT 1", (reg,)).fetchone():
            problems.append("no eligible evidence in mapped register")
        if not attributable:
            problems.append("eligible contributing sources cannot be established")
        status = "archived"
        if not problems:
            store.set_meta_json(con, f"card_sources:{reg}", sources)
            cards.save_agent_card(reg, row["text"], me)
            status = "activated"
        con.execute("UPDATE legacy_voicekit_cards SET status=?, problems_json=? WHERE name=?",
                    (status, _json(problems), row["name"]))
        counts[status] += 1
    return counts


def import_voicekit(path: str | Path, *, apply: bool = False) -> dict:
    """Import one fixed legacy snapshot into MANNERISM_HOME; dry-run never opens the destination.

    Return aggregate counts and register mappings only. The raw migration archive is local to
    the destination database. A changed snapshot or unrelated populated destination is refused.
    Dry runs make no destination or corpus data writes. SQLite may create WAL/SHM read-coordination
    sidecars; existing main database and WAL content stays unchanged.
    """
    source = Path(path).expanduser().resolve()
    target = core.home().expanduser().resolve()
    source_root = source if source.is_dir() else source.parent
    if target == source_root or target.is_relative_to(source_root) or source_root.is_relative_to(target):
        raise ValueError("source and destination must be separate directories")
    snapshot = _snapshot(source)
    units, registers = _prepare(snapshot)
    fingerprint = hashlib.sha256(_json(snapshot).encode()).hexdigest()
    counts = {table: sum(len(t.get(table, [])) for t in snapshot["databases"].values()) for table in _TABLES}
    report = {"applied": False, "counts": counts, "registers": registers, "mappings": dict(REGISTER_MAP),
              "source_mappings": dict(SOURCE_MAP), "cards": {"found": len(snapshot["cards"])}}
    if not apply:
        return report
    state = _target_state(target, fingerprint)
    with closing(store.connect()) as con:
        if state is None:
            with store.tx(con):
                # Recheck after acquiring the writer lock; two importers cannot both start.
                if store.get_meta(con, _MARKER) is not None or _populated(con):
                    raise ValueError("destination changed while preparing import")
                _audit_schema(con)
                state = {"fingerprint": fingerprint, "phase": "prepared"}
                store.set_meta_json(con, _MARKER, state)
        if state["phase"] == "prepared":
            with store.tx(con):
                _write_data(con, snapshot, units)
                state = {"fingerprint": fingerprint, "phase": "data"}
                store.set_meta_json(con, _MARKER, state)
        if state["phase"] != "complete":
            refresh.refresh(con)
            refresh.maybe_build_leak(con, force=True)
            _restore_cards(con)
            store.set_meta_json(con, _MARKER, {"fingerprint": fingerprint, "phase": "complete"})
        report["cards"] = {"found": len(snapshot["cards"]), **{
            status: con.execute("SELECT count(*) FROM legacy_voicekit_cards WHERE status=?", (status,)).fetchone()[0]
            for status in ("activated", "archived")}}
        report["bank"] = bank.bank_counts(con)
    report["applied"] = True
    return report
