import sqlite3

from mannerism.adapters.wispr import Wispr, register_for

from .conftest import run


def make(path, cols):
    con = sqlite3.connect(path)
    con.execute(f"CREATE TABLE History ({', '.join(cols)})")
    return con


def test_wispr_full_columns(home, me):
    p = home / "Library" / "Application Support" / "Wispr Flow" / "flow.sqlite"
    p.parent.mkdir(parents=True)
    con = make(
        p,
        ["asrText TEXT", "formattedText TEXT", "editedText TEXT", "timestamp TEXT", "app TEXT", "conversationId TEXT"],
    )
    con.executemany(
        "INSERT INTO History VALUES (?,?,?,?,?,?)",
        [
            (
                "um hello",
                "Hello there.",
                "Hello there, Ana.",
                "2026-03-01T10:00:00Z",
                "com.tinyspeck.slackmacgap",
                "c1",
            ),
            ("raw only", None, None, "2026-03-01T10:01:00Z", "com.apple.MobileSMS", "c2"),
            (
                "call +44 20 7946 0958",
                "Call +44 20 7946 0958 please",
                "",
                "2026-03-01T10:02:00Z",
                "com.apple.mail",
                None,
            ),
            ("", "", "", "2026-03-01T10:03:00Z", "x", None),
            ("ide words", "Refactor this module", None, "2026-03-01T10:04:00Z", "com.todesktop.cursor", None),
            ("no ts", None, None, "garbage", "com.apple.mail", None),
        ],
    )
    con.commit()
    con.close()
    src = Wispr()
    locs = src.discover(me, {})
    assert [lc.path for lc in locs] == [p]
    units, cur = run(src, locs[0], None, me)
    assert [u.text for u in units] == ["Hello there, Ana.", "raw only", "Call [phone] please", "Refactor this module"]
    assert [u.register for u in units] == ["work", "close", "?", "agent"]
    assert all(u.kind == "dictation" and u.audience == "unknown" for u in units)
    assert units[0].meta == {"app": "com.tinyspeck.slackmacgap"} and units[0].ts.tzinfo is not None
    assert cur["max_rowid"] == 6 and cur["skipped"] == 1
    assert run(src, locs[0], cur, me)[0] == []
    con = sqlite3.connect(p)
    con.execute("INSERT INTO History VALUES ('later one here',NULL,NULL,'2026-03-02T00:00:00Z','x',NULL)")
    con.commit()
    con.close()
    assert [u.text for u in run(src, locs[0], cur, me)[0]] == ["later one here"]


def test_wispr_minimal_columns_and_epoch(home, me):
    p = home / "Library" / "Application Support" / "Wispr Flow" / "flow.sqlite"
    p.parent.mkdir(parents=True)
    con = make(p, ["asrText TEXT", "timestamp INTEGER"])
    con.execute("INSERT INTO History VALUES ('just asr text', 1772000000000)")
    con.commit()
    con.close()
    units, _ = run(Wispr(), Wispr().discover(me, {})[0], None, me)
    assert units[0].text == "just asr text" and units[0].register == "?" and units[0].ts.year == 2026


def test_wispr_register_and_missing(home, me):
    assert register_for("net.whatsapp.WhatsApp") == "close" and register_for("ru.keepcoder.Telegram") == "close"
    assert register_for("com.anthropic.claudefordesktop") == "agent" and register_for(None) == "?"
    assert Wispr().discover(me, {}) == []
