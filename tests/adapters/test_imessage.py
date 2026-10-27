import sqlite3
import sys

from mannerism.adapters import imessage as im
from mannerism.adapters.imessage import IMessage, decode_attributed

from .conftest import run

NS = 1_000_000_000


def attributed(text: str) -> bytes:
    data = text.encode()
    ln = bytes([len(data)]) if len(data) < 0x80 else b"\x81" + len(data).to_bytes(2, "little")
    return (
        b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x12NSAttributedString\x00\x84\x84\x08NSObject\x00\x85\x92\x84\x84\x84"
        b"\x08NSString\x01\x94\x84\x01+" + ln + data + b"\x86\x84\x02iI\x01"
    )


def make_db(path):
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB, is_from_me INTEGER, date INTEGER,
        handle_id INTEGER, associated_message_type INTEGER DEFAULT 0, item_type INTEGER DEFAULT 0);
    CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, style INTEGER);
    CREATE TABLE chat_message_join (chat_id INTEGER, message_id INTEGER);
    """)
    base = 800_000_000  # seconds since 2001 -> 2026
    rows = [
        (1, "are you home?", None, 0, (base) * NS, 7, 0, 0, 1),
        (2, "yes, call +44 20 7946 0958", None, 1, (base + 10) * NS, 0, 0, 0, 1),
        (3, None, attributed("on my way, ana@example.org"), 1, (base + 20) * NS, 0, 0, 0, 1),
        (4, "Liked “on my way”", None, 1, (base + 25) * NS, 0, 2000, 0, 1),  # tapback
        (5, "\ufffc", None, 1, (base + 30) * NS, 0, 0, 0, 1),  # attachment only
        (6, "group renamed", None, 1, (base + 35) * NS, 0, 0, 1, 1),  # item_type != 0
        (7, "lunch anyone?", None, 0, (base + 40), 8, 0, 0, 2),  # seconds-precision timestamp
        (8, "me too", None, 1, base + 50, 0, 0, 0, 2),
    ]
    con.executemany("INSERT INTO message VALUES (?,?,?,?,?,?,?,?)", [r[:8] for r in rows])
    con.executemany("INSERT INTO chat_message_join VALUES (?,?)", [(r[8], r[0]) for r in rows])
    con.executemany("INSERT INTO chat VALUES (?,?,?)", [(1, "+15550001111", 45), (2, "chat123456", 43)])
    con.commit()
    con.close()


def test_decode_attributed():
    assert decode_attributed(attributed("hello there")) == "hello there"
    long = "x" * 200
    assert decode_attributed(attributed(long)) == long
    assert decode_attributed(None) == "" and decode_attributed(b"garbage") == ""


def test_imessage_read(home, me, monkeypatch):
    lib = home / "Library" / "Messages"
    lib.mkdir(parents=True)
    make_db(lib / "chat.db")
    monkeypatch.setattr(im.sys, "platform", "darwin")
    src = IMessage()
    locs = src.discover(me, {})
    assert len(locs) == 1 and "Full Disk Access" in locs[0].label
    units, cur = run(src, locs[0], None, me)
    by = {u.text: u for u in units}
    assert set(by) == {"yes, call [phone]\non my way, [email]", "me too"}
    one = by["yes, call [phone]\non my way, [email]"]
    assert (one.audience, one.register, one.kind, one.parts) == ("close", "close", "message", 2)
    assert one.context[0].who == "other1" and one.context[0].text == "are you home?"
    assert one.ts.year == 2026 and one.ts.tzinfo is not None
    grp = by["me too"]
    assert (grp.audience, grp.register) == ("group", "group") and grp.context[0].text == "lunch anyone?"
    assert "chat123456" not in (grp.thread_id or "") and "+1555" not in str(grp.audience_ref)
    assert cur == {"max_rowid": 8}
    assert run(src, locs[0], cur, me)[0] == []
    # new message appended -> only that one, with earlier context
    con = sqlite3.connect(lib / "chat.db")
    con.execute("INSERT INTO message VALUES (9,'sounds good',NULL,1,?,0,0,0)", ((800_000_100) * NS,))
    con.execute("INSERT INTO chat_message_join VALUES (1,9)")
    con.commit()
    con.close()
    units, cur2 = run(src, locs[0], cur, me)
    assert [u.text for u in units] == ["sounds good"] and cur2["max_rowid"] == 9
    assert (lib / "chat.db").exists()


def test_imessage_discover_elsewhere_and_errors(home, me, monkeypatch):
    assert IMessage().discover(me, {}) == []
    monkeypatch.setattr(im.sys, "platform", "darwin")
    assert IMessage().discover(me, {}) == []  # no database
    bad = home / "bad.db"
    bad.write_bytes(b"not a database")
    from mannerism.core import Location

    assert list(IMessage().read(Location("imessage", str(bad), bad), None, me)[0]) == []
    assert sys.platform  # sanity
