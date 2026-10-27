"""Hermes schema fixtures contain invented sessions and messages only."""
import json
import sqlite3

import pytest

from mannerism import store
from mannerism.adapters.hermes import Hermes, clean
from mannerism.core import Location, Me, unit_id_for

from .conftest import run

TS = 1_750_000_000
OWNER = "12345"


def database(path):
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, user_id TEXT, parent_session_id TEXT,
                              model_config TEXT, chat_id TEXT, chat_type TEXT DEFAULT 'dm');
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL,
                              platform_message_id TEXT, display_kind TEXT, _compressed_summary INTEGER DEFAULT 0,
                              observed INTEGER DEFAULT 0, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
                              active INTEGER DEFAULT 1);
    """)
    session(con, "root")
    con.commit()
    return con


def session(con, sid, source="telegram", user=OWNER, parent=None, cfg=None, chat="room-one"):
    con.execute("INSERT INTO sessions(id,source,user_id,parent_session_id,model_config,chat_id) VALUES (?,?,?,?,?,?)",
                (sid, source, user, parent, json.dumps(cfg or {}), chat))


def message(con, mid, text, sid="root", role="user", offset=0, pmid=None, kind=None, **extra):
    con.execute("INSERT INTO messages(id,session_id,role,content,timestamp,platform_message_id,display_kind) "
                "VALUES (?,?,?,?,?,?,?)", (mid, sid, role, text, TS + offset, pmid, kind))
    for column, value in extra.items():
        con.execute(f"UPDATE messages SET {column}=? WHERE id=?", (value, mid))
    con.commit()


def source(path, me=None, **config):
    src = Hermes()
    me = me or Me()
    locs = src.discover(me, {"paths": [str(path)], "user_id": OWNER, **config})
    assert len(locs) == 1
    return src, locs[0], me


def test_discovery_is_configured_files_and_explicit_identity_only(tmp_path, home):
    path = home / ".hermes" / "state.db"
    path.parent.mkdir()
    database(path).close()
    src = Hermes()
    assert src.discover(Me(), {}) == []
    assert src.discover(Me(), {"paths": [str(path)]}) == []
    assert src.discover(Me(), {"user_id": OWNER, "paths": [str(path.parent)]}) == []
    assert src.discover(Me(), {"user_id": OWNER, "paths": str(path)}) == []
    locs = src.discover(Me(handles={"telegram": OWNER}), {"paths": [str(path), str(path)]})
    assert len(locs) == 1 and locs[0].path == path
    src.discover(Me(), {})  # stale discover authorization must not survive a configuration change
    assert "_error" in src.read(locs[0], None, Me())[1]
    assert "_error" in src.read(Location("hermes", "invented", path), None, Me())[1]


@pytest.mark.parametrize("config", [None, [], "invalid", 0])
def test_missing_or_malformed_config_clears_discovery_authorization(tmp_path, config):
    path = tmp_path / "state.db"
    database(path).close()
    src, loc, me = source(path, Me(handles={"telegram": OWNER}))
    assert src.discover(me, config) == []
    assert "_error" in src.read(loc, None, me)[1]


def test_explicit_attribution_child_and_synthetic_exclusion(tmp_path):
    path = tmp_path / "state.db"
    con = database(path)
    session(con, "stranger", user="another-user")
    session(con, "cron", source="cron")
    session(con, "tool", source="tool")
    session(con, "cli", source="cli")
    session(con, "subagent", source="subagent")
    session(con, "child", parent="root", cfg={"_delegate_from": "root"})
    session(con, "orphan", cfg={"_delegate_from": "deleted-parent"})
    session(con, "descendant", parent="child")
    session(con, "foreign-parent", parent="stranger")
    session(con, "missing-parent", parent="missing")
    session(con, "cycle", parent="cycle")
    session(con, "shared")
    con.execute("UPDATE sessions SET chat_type='group' WHERE id='shared'")
    for mid, sid in enumerate(["stranger", "cron", "tool", "cli", "subagent", "child", "orphan",
                               "descendant", "foreign-parent", "missing-parent", "cycle", "shared"], 10):
        message(con, mid, "this is synthetic or someone else's text", sid=sid)
    message(con, 1, "please make the wording a little more direct", pmid="77")
    message(con, 2, "[ASYNC DELEGATION] worker complete")
    message(con, 3, "machine display notice", kind="system")
    message(con, 4, "summary machine text", _compressed_summary=1)
    message(con, 5, "observed machine text", observed=1)
    message(con, 6, "tool invocation text", tool_name="shell")
    message(con, 7, "system policy prose", role="system")
    message(con, 8, "tool result prose", role="tool")
    con.close()
    src, loc, me = source(path)
    units, _ = run(src, loc, None, me)
    assert [(u.source, u.source_ref, u.text) for u in units] == [
        ("hermes", "1", "please make the wording a little more direct")]
    assert units[0].register == "agent" and units[0].audience == "agent"


def test_legacy_bursts_compaction_and_context(tmp_path):
    path = tmp_path / "state.db"
    con = database(path)
    session(con, "continued", parent="root")
    message(con, 1, "send me the short version please", pmid="77", active=0)
    message(con, 2, "keep the ending open", offset=15, pmid="78")
    message(con, 3, "Assistant context user@example.org", role="assistant", offset=30)
    message(con, 4, "and give me another option", offset=45, pmid="79")
    message(con, 10, "send me the short version please", sid="continued", pmid="77")
    src, loc, me = source(path)
    units, cur = run(src, loc, None, me)
    assert [u.source_ref for u in units] == ["1..2", "4"]
    assert units[0].parts == 2 and units[0].text == "send me the short version please\nkeep the ending open"
    assert units[0].unit_id == unit_id_for("hermes", "1..2")
    assert units[1].context[0].who == "agent" and "user@example.org" not in units[1].context[0].text
    assert run(src, loc, cur, me)[0] == []
    con.execute("DELETE FROM messages WHERE id=1")  # only the compaction copy survives
    con.commit()
    assert run(src, loc, cur, me)[0] == []
    con.close()


def test_incremental_updates_new_burst_boundary_and_store_dedup(tmp_path):
    path = tmp_path / "state.db"
    con = database(path)
    message(con, 1, "keep the first sentence short", pmid="77")
    src, loc, me = source(path)
    target = store.connect()

    def ingest(cursor):
        units, cursor = run(src, loc, cursor, me)
        deleted = cursor.pop("_delete_refs", None)
        result = store.write_units(target, "hermes", loc.key, units, cursor, delete_refs=deleted)
        return units, cursor, result

    _, cur, result = ingest(None)
    assert len(result.new_ids) == 1
    message(con, 2, "and preserve the useful details", offset=30, pmid="78")
    units, cur, result = ingest(cur)
    assert [u.source_ref for u in units] == ["1..2"] and len(result.deleted_ids) == 1
    assert target.execute("SELECT source_ref FROM units").fetchone()[0] == "1..2"
    con.execute("UPDATE messages SET content='and preserve all the useful details' WHERE id=2")
    con.commit()
    units, cur, result = ingest(cur)
    assert "all the useful" in units[0].text and len(result.updated_ids) == 1
    assert ingest(cur)[0] == []
    assert target.execute("SELECT count(*) FROM units").fetchone()[0] == 1
    target.close()
    con.close()


def test_platform_ids_are_scoped_to_chat(tmp_path):
    path = tmp_path / "state.db"
    con = database(path)
    session(con, "other-room", chat="room-two")
    message(con, 1, "the first room has this prompt", pmid="77")
    message(con, 2, "the second room has another prompt", sid="other-room", pmid="77")
    src, loc, me = source(path)
    assert len(run(src, loc, None, me)[0]) == 2
    con.close()


@pytest.mark.parametrize(("raw", "expected", "voice"), [
    ('[Replying to: "someone else wrote this"]\n\nmy own reply goes here', "my own reply goes here", False),
    ('[The user sent a voice message]\n"please keep my spoken words"', "please keep my spoken words", True),
    ('[Image attached at: /tmp/private.png]\nmy own caption here', "my own caption here", False),
    ('my actual words\n[IMPORTANT machine instructions]', "my actual words", False),
    ('\x00json:[{"type":"image_url"},{"type":"text","text":"my own text part"}]', "my own text part", False),
    ('\x00json:{"text":"not a parts array"}', "", False),
    ('\x00json:broken', "", False),
    ('\x00json:[{"type":"text","text":45}]', "", False),
    ('[System: scheduled task]', "", False),
    ('<task-notification>worker output</task-notification>', "", False),
    ('<agent-message from="worker">synthetic</agent-message>', "", False),
    ('https://example.org/only-a-link', "", False),
    ('[The user shared an image]\n[ASYNC DELEGATION] result', "", False),
])
def test_wrapper_cleaning(raw, expected, voice):
    text, is_voice, _ = clean(raw)
    assert (text, is_voice) == (expected, voice)


def test_read_only_and_malformed_inputs_preserve_cursor(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    con = database(path)
    message(con, 1, "genuine user text without a wrapper")
    message(con, 2, "invalid timestamp", offset=float("inf"))
    con.close()
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    real_connect = sqlite3.connect
    connections = []

    def check_connect(database, *args, **kwargs):
        assert str(database) == path.as_uri() + "?mode=ro" and kwargs["uri"] is True
        connections.append(database)
        connection = real_connect(database, *args, **kwargs)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden_write(id INTEGER)")
        return connection

    monkeypatch.setattr(sqlite3, "connect", check_connect)
    src, loc, me = source(path)
    units, cur = run(src, loc, None, me)
    assert len(units) == 1 and connections
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    path.write_bytes(b"not a database")
    units, after = run(src, loc, cur, me)
    assert units == [] and after["_error"]
    assert after["units"] == cur["units"]


def test_wal_changes_are_reread_without_logical_source_writes(tmp_path):
    path = tmp_path / "state.db"
    con = database(path)
    con.execute("PRAGMA journal_mode=WAL")
    message(con, 1, "first genuine prompt without a wrapper")
    src, loc, me = source(path)
    _, cur = run(src, loc, None, me)
    message(con, 2, "second prompt after the last reply", offset=600)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file() and p.name != "state.db-shm"}
    units, _ = run(src, loc, cur, me)
    assert [u.source_ref for u in units] == ["2"]
    assert before == {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file() and p.name != "state.db-shm"}
    con.close()


def test_live_read_is_one_snapshot_during_write_and_checkpoint(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    writer = database(path)
    writer.execute("PRAGMA journal_mode=WAL")
    message(writer, 1, "the original first message", pmid="77")
    message(writer, 2, "the original second message", pmid="78", offset=600)
    src, loc, me = source(path)
    _, cursor = run(src, loc, None, me)
    native_extract = src._extract
    read_marks = []

    def concurrent_extract(reader, user, saved, db_path):
        assert reader.in_transaction
        assert reader.execute("PRAGMA query_only").fetchone()[0] == 1
        # Pin the reader's snapshot before the other connection replaces rows.
        assert reader.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("DELETE FROM messages")
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("DELETE FROM messages WHERE id=1")
        writer.execute("INSERT INTO messages(id,session_id,role,content,timestamp,platform_message_id) "
                       "VALUES (10,'root','user','the revised first message',?,'77')", (TS,))
        writer.execute("UPDATE messages SET content='the revised second message' WHERE id=2")
        writer.commit()
        read_marks.append(writer.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
        return native_extract(reader, user, saved, db_path)

    # A changed signature forces a read without altering either message.
    writer.execute("UPDATE sessions SET model_config='{} '")
    writer.commit()
    monkeypatch.setattr(src, "_extract", concurrent_extract)
    units, during = run(src, loc, cursor, me)
    assert units == [] and "_delete_refs" not in during and "_error" not in during
    assert read_marks and read_marks[0][1] > read_marks[0][2]  # pinned reader blocks full checkpoint
    monkeypatch.setattr(src, "_extract", native_extract)
    units, after = run(src, loc, during, me)
    assert {u.source_ref: u.text for u in units} == {
        "1": "the revised first message", "2": "the revised second message"}
    assert "_delete_refs" not in after and "_error" not in after
    writer.close()


def test_incomplete_snapshot_never_deletes_old_refs(tmp_path, monkeypatch):
    from mannerism.adapters import hermes

    path = tmp_path / "state.db"
    con = database(path)
    message(con, 1, "a genuine prompt")
    src, loc, me = source(path)
    _, cur = run(src, loc, None, me)
    monkeypatch.setattr(hermes, "MAX_ROWS", 1)
    message(con, 2, "another genuine prompt", offset=600)
    units, after = run(src, loc, cur, me)
    assert units == [] and after["_error"] and after["units"] == cur["units"]
    assert "_delete_refs" not in after
    con.close()


def test_long_agent_replies_are_bounded_context_not_snapshot_budget(tmp_path, monkeypatch):
    from mannerism.adapters import hermes

    path = tmp_path / "state.db"
    con = database(path)
    message(con, 1, "reply " + "x" * 50_000, role="assistant")
    message(con, 2, "a genuine prompt after a long reply", offset=60)
    monkeypatch.setattr(hermes, "MAX_SNAPSHOT_CHARS", hermes.AGENT_CONTEXT_READ + 1_000)
    src, loc, me = source(path)
    units, cur = run(src, loc, None, me)
    assert "_error" not in cur and [u.text for u in units] == ["a genuine prompt after a long reply"]
    assert units[0].context[0].text.startswith("reply x")
    # Own text still counts in full: an oversized user snapshot fails closed without deleting anything.
    message(con, 3, "y" * 20_000, offset=600)
    units, after = run(src, loc, cur, me)
    assert units == [] and "text budget" in after["_error"] and "_delete_refs" not in after
    con.close()
