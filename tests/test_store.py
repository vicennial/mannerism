import os
import sqlite3
import stat
from datetime import UTC, datetime

import pytest

from mannerism import store
from mannerism.core import Unit


def u(ref, text="hello there my friend", source="s1", **kw):
    return Unit(source, ref, datetime(2026, 1, 1, 12, tzinfo=UTC), text, "message", register="close", **kw)


def test_connect_retries_busy_journal_transition_and_closes_failed_connection(monkeypatch):
    real = sqlite3.connect
    opened = []
    class BusyOnce(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA journal_mode=WAL" and len(opened) == 1:
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return super().execute(sql, *args, **kwargs)
    def connect(*args, **kwargs):
        con = real(*args, **kwargs, factory=BusyOnce)
        opened.append(con)
        return con
    monkeypatch.setattr(sqlite3, "connect", connect)
    store.connect().close()
    assert len(opened) == 2
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1")


def test_connect_retry_wait_is_limited_to_remaining_deadline(monkeypatch):
    real = sqlite3.connect
    clock, waits = [0.0], []
    monkeypatch.setattr(store.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(store.time, "sleep", lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    class Busy(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == "PRAGMA journal_mode=WAL":
                clock[0] += 2.9 if len(waits) == 1 else waits[-1]
                error = sqlite3.OperationalError("database is locked")
                error.sqlite_errorcode = sqlite3.SQLITE_BUSY
                raise error
            return super().execute(sql, *args, **kwargs)
    def connect(*args, **kwargs):
        waits.append(kwargs["timeout"])
        return real(*args, **kwargs, factory=Busy)
    monkeypatch.setattr(sqlite3, "connect", connect)
    with pytest.raises(sqlite3.OperationalError, match="locked"):
        store.connect()
    assert len(waits) == 2 and waits[0] == 3
    assert 0 < waits[1] < 0.1
    assert clock[0] == pytest.approx(3)


def test_connect_settings_and_modes(home):
    con = store.connect()
    assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert con.execute("PRAGMA secure_delete").fetchone()[0] == 1
    assert con.execute("PRAGMA busy_timeout").fetchone()[0] == 3000
    assert store.get_meta(con, "schema_version") == str(store.SCHEMA_VERSION)
    con.execute("INSERT INTO meta VALUES ('k', 'v')")
    for suffix in ("", "-wal", "-shm"):
        path = str(store.db_path()) + suffix
        if os.path.exists(path):
            assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    store.connect().close()  # reopening a migrated database is a no-op


def test_write_units_is_atomic_with_cursor(home):
    con = store.connect()

    def bad_units():
        yield u("a")
        raise RuntimeError("crash")

    try:
        store.write_units(con, "s1", "loc", bad_units(), {"i": 1})
    except RuntimeError:
        pass
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 0
    assert store.get_cursor(con, "s1", "loc") is None
    res = store.write_units(con, "s1", "loc", [u("a"), u("b")], {"i": 2})
    assert len(res.new_ids) == 2 and store.get_cursor(con, "s1", "loc") == {"i": 2}
    again = store.write_units(con, "s1", "loc", [u("a", "changed text here ok")], {"i": 3})
    assert again.new_ids == [] and len(again.updated_ids) == 1
    assert con.execute("SELECT text FROM units WHERE source_ref='a'").fetchone()[0] == "changed text here ok"


def test_invalid_and_foreign_units_are_dropped_not_fatal(home):
    con = store.connect()
    naive = Unit("s1", "n", datetime(2026, 1, 1), "no tz here at all", "message")
    res = store.write_units(con, "s1", "loc", [naive, u("x", source="other"), u("ok")], {"i": 1})
    assert res.rejected == 2 and len(res.new_ids) == 1


def test_forget_rows_source_and_all(home):
    con = store.connect()
    store.write_units(con, "s1", "l", [u("a")], {"i": 1})
    store.write_units(con, "s2", "l", [u("b", source="s2")], {"i": 1})
    out = store.forget_rows(con, "s1")
    assert out["units"] == 1 and con.execute("SELECT source FROM units").fetchall()[0][0] == "s2"
    assert store.get_cursor(con, "s1", "l") is None and store.get_cursor(con, "s2", "l") == {"i": 1}
    store.forget_rows(con, None)
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == 0
    assert store.get_meta(con, "schema_version")  # the schema marker survives a full wipe


def test_tx_rolls_back(home):
    con = store.connect()
    try:
        with store.tx(con):
            con.execute("INSERT INTO meta VALUES ('x', '1')")
            raise sqlite3.Error("no")
    except sqlite3.Error:
        pass
    assert store.get_meta(con, "x") is None
