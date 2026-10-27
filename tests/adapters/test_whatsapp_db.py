"""Synthetic Android schemas only. No phone backups, keys or private corpus data."""

import json
import sqlite3
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from mannerism import store
from mannerism.adapters import whatsapp_db as wa
from mannerism.core import CONTEXT_CHARS, CONTEXT_LINES, Location, hash_ref, unit_id_for

from .conftest import run

BASE = 1_800_000_000_000


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "msgstore ?# synthetic.db"
    with closing(sqlite3.connect(path)) as con:
        con.executescript("""
            CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INTEGER, from_me INTEGER,
                key_id TEXT, timestamp INTEGER, message_type INTEGER, text_data TEXT, sender_jid_row_id INTEGER);
            CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INTEGER, subject TEXT, group_member_count INTEGER);
            CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT, server TEXT);
            CREATE TABLE message_forwarded (message_row_id INTEGER);
            CREATE TABLE message_system (message_row_id INTEGER);
            CREATE TABLE message_revoked (message_row_id INTEGER);
            CREATE TABLE group_participant_user (group_jid_row_id INTEGER);
            INSERT INTO jid VALUES (10, '15550001111@s.whatsapp.net', 's.whatsapp.net');
            INSERT INTO jid VALUES (20, '15550002222-1234567890@g.us', 'g.us');
            INSERT INTO chat VALUES (1, 10, 'Ignored contact name', 0);
            INSERT INTO chat VALUES (2, 20, 'Secret Picnic', 9);
            INSERT INTO group_participant_user VALUES (20), (20), (20);
        """)
        con.commit()
    return path


def add(db, row_id, text, *, chat=1, own=1, seconds=None, mtype=0, key=None, sender=30):
    with closing(sqlite3.connect(db)) as con:
        con.execute("INSERT INTO message VALUES (?,?,?,?,?,?,?,?)", (
            row_id, chat, own, key or f"K{row_id}", BASE + (row_id if seconds is None else seconds) * 1000,
            mtype, text, sender,
        ))
        con.commit()


def sql(db, statement, args=()):
    with closing(sqlite3.connect(db)) as con:
        con.execute(statement, args)
        con.commit()


def source(db, me, **config):
    src = wa.WhatsAppDB()
    loc, = src.discover(me, {"paths": [str(db)], **config})
    return src, loc


def test_explicit_discovery_only(db, me, home):
    src = wa.WhatsAppDB()
    assert src.name == "whatsapp_db" and src.tier == "export"
    assert src.discover(me, {}) == []
    assert src.discover(me, {"paths": [str(db.parent)]}) == []
    assert src.discover(me, {"paths": [str(db.parent / "*.db")]}) == []
    assert src.discover(me, {"paths": {"bad": str(db)}}) == []
    locs = src.discover(me, {"paths": [db, str(db), None, ""]})
    assert len(locs) == 1 and locs[0].path == db
    assert wa.WhatsAppDB().discover(me, {}) == []  # configuration is per instance
    with pytest.raises(ValueError, match="explicitly configured"):
        wa.WhatsAppDB().read(locs[0], None, me)
    with pytest.raises(ValueError, match="explicitly configured"):
        src.read(Location("whatsapp", str(db), db), None, me)
    assert not (home / ".mannerism").exists()


def test_readonly_uri_and_missing_file(db, me, monkeypatch):
    add(db, 1, "hello")
    src, loc = source(db, me)
    before = (db.read_bytes(), db.stat().st_mtime_ns)
    connect = sqlite3.connect
    opened, statements = [], []

    def checked(database, **kwargs):
        opened.append((database, kwargs))
        con = connect(database, **kwargs)
        con.set_trace_callback(statements.append)
        return con

    monkeypatch.setattr(wa.sqlite3, "connect", checked)
    assert run(src, loc, None, me)[0][0].text == "hello"
    assert opened == [(db.as_uri() + "?mode=ro", {"uri": True})]
    assert "PRAGMA query_only=ON" in statements and "BEGIN" in statements
    assert (db.read_bytes(), db.stat().st_mtime_ns) == before
    db.unlink()
    with pytest.raises(FileNotFoundError):
        src.read(loc, None, me)
    assert not db.exists() and len(opened) == 1


def test_own_attribution_captions_and_group_mapping(db, me):
    add(db, 1, "can you help?", own=0)
    add(db, 2, "certainly", own=1)
    add(db, 3, "wrong attribution", own=2)
    add(db, 4, "Jo Dev", own=0)  # matching a configured name never establishes authorship
    for row_id, mtype in enumerate((0, 1, 3, 9, 13, 42, 43), 10):
        add(db, row_id, f"caption {mtype}", chat=2, mtype=mtype)
    src, loc = source(db, me)
    units, _ = run(src, loc, None, me)
    assert [u.text for u in units] == ["certainly", "\n".join(f"caption {t}" for t in (0, 1, 3, 9, 13, 42, 43))]
    dm, group = units
    assert (dm.source_ref, dm.source, dm.register, dm.audience, dm.parts) == ("K2", "whatsapp_db", "close", "close", 1)
    assert dm.ts == datetime.fromtimestamp((BASE + 2000) / 1000, UTC)
    assert [(c.who, c.text) for c in dm.context] == [("other1", "can you help?")]
    assert group.source_ref == "K10..K16" and group.parts == 7
    assert (group.audience, group.register, group.meta["is_group"], group.meta["participant_count"]) == (
        "group", "group", True, 3,
    )
    assert group.thread_id == hash_ref("15550002222-1234567890@g.us", salt="voice-kit")
    assert group.thread_id == group.audience_ref
    assert "1555000" not in str(asdict(group)) and "Secret Picnic" not in str(asdict(group))


@pytest.mark.parametrize("mtype", [2, 4, 5, 7, 8, 15, 16, 999])
def test_nontext_system_and_revoked_types_excluded(db, me, mtype):
    add(db, 1, "excluded even if text exists", mtype=mtype)
    src, loc = source(db, me)
    assert run(src, loc, None, me)[0] == []


@pytest.mark.parametrize("table", ["message_forwarded", "message_system", "message_revoked"])
def test_flag_tables_exclude_even_text_captions(db, me, table):
    add(db, 1, "excluded", mtype=1)
    sql(db, f"INSERT INTO {table} VALUES (1)")
    sql(db, f"INSERT INTO {table} VALUES (1)")  # duplicate auxiliary rows must not multiply messages
    add(db, 2, "retained", seconds=200)
    src, loc = source(db, me)
    units, _ = run(src, loc, None, me)
    assert [u.text for u in units] == ["retained"]
    if table == "message_forwarded":
        assert [(c.who, c.text) for c in units[0].context] == [("me", "excluded")]
    else:
        assert not units[0].context


def test_legacy_burst_boundaries_and_resend_refs(db, me):
    add(db, 1, "first", seconds=0)
    add(db, 2, "first", seconds=5)  # resend is skipped without moving the previous own timestamp
    add(db, 3, "forwarded", seconds=10)
    sql(db, "INSERT INTO message_forwarded VALUES (3)")
    add(db, 4, "voice note", seconds=20, mtype=2)
    add(db, 5, "system", seconds=30, mtype=7)
    add(db, 6, "second", seconds=120)  # exactly 120 seconds still merges
    add(db, 7, None, seconds=125, own=0, mtype=2)  # incoming media breaks bursts
    add(db, 8, "third", seconds=130)
    add(db, 9, "fourth", seconds=251)  # greater than 120 seconds breaks
    add(db, 10, "⚕️ *Hermes bridge reply", seconds=252)
    add(db, 11, "fifth", seconds=253)
    src, loc = source(db, me)
    units, _ = run(src, loc, None, me)
    assert [(u.source_ref, u.text, u.parts) for u in units] == [
        ("K1..K6", "first\nsecond", 2), ("K8", "third", 1),
        ("K9", "fourth", 1), ("K11", "fifth", 1),
    ]
    assert all(u.meta["thread_voice_notes"] == 1 for u in units)
    assert units[-1].context[-1].who == "agent"
    assert all(u.audience == "close" for u in units)  # an incoming voice note establishes another party


def test_bridge_self_chat_is_agent(db, me):
    add(db, 1, "my request")
    add(db, 2, "⚕ Hermes response")
    add(db, 3, "my followup")
    src, loc = source(db, me)
    units, _ = run(src, loc, None, me)
    assert [u.text for u in units] == ["my request", "my followup"]
    assert all((u.audience, u.register) == ("agent", "agent") for u in units)
    assert units[1].context[-1].who == "agent"


def test_masking_own_context_and_cursor(db, me):
    for row_id in range(1, 6):
        add(db, row_id, "Secret Picnic Dana Jo Dev dana@example.org @15550001111 "
            "15550002222-1234567890@g.us 123456@lid +44 20 7946 0958 " + "x" * 500,
            own=0, chat=2, sender=row_id % 2 + 30)
    add(db, 6, "Dana Jo Dev Secret Picnic dana@example.org @15550001111 "
        "15550002222-1234567890@g.us 123456@lid +44 20 7946 0958 password=swordfish", chat=2)
    src, loc = source(db, me, redact_names=["Dana"])
    units, cursor = run(src, loc, None, me)
    unit, = units
    assert len(unit.context) == CONTEXT_LINES and all(len(c.text) <= CONTEXT_CHARS for c in unit.context)
    assert {c.who for c in unit.context} == {"other1", "other2"}
    output = json.dumps([asdict(unit), cursor], default=str)
    for private in ("Dana", "Jo Dev", "Secret Picnic", "dana@example.org", "1555000", "123456@lid", "7946", "swordfish"):
        assert private not in output
    for mask in ("[person]", "[me]", "[group]", "[email]", "@[user]", "[phone]", "[secret]"):
        assert mask in unit.text
    assert "text" not in cursor and "context" not in cursor


def test_merges_before_masking_to_preserve_legacy_refs(db, me):
    add(db, 1, "email one@example.org")
    add(db, 2, "email two@example.org")
    src, loc = source(db, me)
    unit, = run(src, loc, None, me)[0]
    assert (unit.source_ref, unit.parts, unit.text) == ("K1..K2", 2, "email [email]\nemail [email]")


@pytest.mark.parametrize("subject", ["X", "[Private Picnic]", "🎉 Private Picnic 🎉"])
def test_short_and_punctuated_group_subjects_are_masked(db, me, subject):
    sql(db, "UPDATE chat SET subject=? WHERE _id=2", (subject,))
    add(db, 1, f"before {subject} after", own=0, chat=2)
    add(db, 2, f"before {subject} after", chat=2)
    src, loc = source(db, me)
    unit, = run(src, loc, None, me)[0]
    assert unit.text == "before [group] after"
    assert unit.context[0].text == "before [group] after"


def test_incremental_append_edit_revoke_delete_and_store(db, me, tmp_path):
    add(db, 1, "first")
    src, loc = source(db, me)
    with closing(store.connect(tmp_path / "synthetic-corpus.db")) as con:
        units, cursor = run(src, loc, None, me)
        first_id = units[0].unit_id
        store.write_units(con, src.name, loc.key, units, cursor)
        assert run(src, loc, cursor, me) == ([], cursor)
        add(db, 2, "second")  # extending the old burst replaces its old singleton
        units, updated = run(src, loc, cursor, me)
        assert [(u.source_ref, u.text) for u in units] == [("K1..K2", "first\nsecond")]
        assert updated.pop("_delete_refs") == ["K1"]
        result = store.write_units(con, src.name, loc.key, units, updated, delete_refs=["K1"])
        assert result.deleted_ids == [first_id]
        sql(db, "UPDATE message SET text_data='edited' WHERE _id=1")
        units, edited = run(src, loc, updated, me)
        assert units[0].source_ref == "K1..K2" and units[0].text == "edited\nsecond"
        assert units[0].unit_id == unit_id_for("whatsapp_db", "K1..K2")
        store.write_units(con, src.name, loc.key, units, edited)
        sql(db, "INSERT INTO message_revoked VALUES (2)")
        units, revoked = run(src, loc, edited, me)
        assert [(u.source_ref, u.text) for u in units] == [("K1", "edited")]
        store.write_units(con, src.name, loc.key, units, {k: v for k, v in revoked.items() if k != "_delete_refs"},
                          delete_refs=revoked["_delete_refs"])
        sql(db, "DELETE FROM message")
        units, empty = run(src, loc, revoked, me)
        assert units == [] and empty["_delete_refs"] == ["K1"]
        store.write_units(con, src.name, loc.key, units, None, delete_refs=empty["_delete_refs"])
        assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 0


def test_stable_ids_across_copies_and_unrelated_chats(db, me, tmp_path):
    add(db, 1, "hello", key="NATIVEKEY")
    src, loc = source(db, me)
    units, cursor = run(src, loc, None, me)
    copy = tmp_path / "another-snapshot.db"
    copy.write_bytes(db.read_bytes())
    other, other_loc = source(copy, me)
    assert run(other, other_loc, None, me)[0][0].unit_id == units[0].unit_id
    sql(db, "UPDATE message SET _id=900 WHERE _id=1")
    assert run(src, loc, cursor, me) == ([], cursor)  # native key, not local row id, owns identity
    add(db, 2, "group only", chat=2)
    changed, _ = run(src, loc, cursor, me)
    assert [u.source_ref for u in changed] == ["K2"]


def test_optional_tables_and_fields(db, me):
    for table in ("message_forwarded", "message_system", "message_revoked", "group_participant_user"):
        sql(db, f"DROP TABLE {table}")
    sql(db, "ALTER TABLE message DROP COLUMN sender_jid_row_id")
    sql(db, "ALTER TABLE chat DROP COLUMN subject")
    src, loc = source(db, me)
    sql(db, "INSERT INTO message VALUES (1,2,1,'CAPTION',?,1,'caption')", (BASE,))
    unit, = run(src, loc, None, me)[0]
    assert unit.text == "caption" and unit.meta["participant_count"] == 9
    sql(db, "ALTER TABLE chat DROP COLUMN group_member_count")
    assert run(src, loc, None, me)[0][0].meta["participant_count"] is None


def test_bad_schema_and_corrupt_db_do_not_advance_or_delete(db, me):
    src, loc = source(db, me)
    cursor = {"version": 1, "units": {"existing": "digest"}}
    sql(db, "DROP TABLE message")
    with pytest.raises(ValueError, match="Unsupported Android"):
        src.read(loc, cursor, me)
    db.write_bytes(b"not sqlite")
    with pytest.raises(sqlite3.DatabaseError):
        src.read(loc, cursor, me)
    assert cursor == {"version": 1, "units": {"existing": "digest"}}


@pytest.mark.parametrize("value", [0, -1, None, "invalid", 9e99])
def test_bad_timestamp_excluded(db, me, value):
    add(db, 1, "bad time")
    sql(db, "UPDATE message SET timestamp=?", (value,))
    src, loc = source(db, me)
    assert run(src, loc, None, me)[0] == []


def test_committed_wal_updates(db, me):
    with closing(sqlite3.connect(db)) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        add(db, 1, "from wal")
        src, loc = source(db, me)
        unit, = run(src, loc, None, me)[0]
        assert unit.text == "from wal"
        writer.execute("UPDATE message SET text_data='wal edit'")
        writer.commit()
        assert run(src, loc, None, me)[0][0].text == "wal edit"
