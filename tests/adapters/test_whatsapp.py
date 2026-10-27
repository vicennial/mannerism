import zipfile

from mannerism.adapters.whatsapp import WhatsApp, parse_chat

from .conftest import run

IOS = """\u200e[01/03/2026, 10:00:00] Dana: \u200eMessages and calls are end-to-end encrypted..
[01/03/2026, 10:01:00] Dana: are you free tonight?
[01/03/2026, 10:02:10] Jo Dev: yes! my number is +44 20 7946 0958
and it is the second line
[01/03/2026, 10:02:30] Jo Dev: ok
[01/03/2026, 10:03:00] Jo Dev: \u200e<attached: 0001-PHOTO.jpg>
[01/03/2026, 10:04:00] Dana: \u200eimage omitted
[01/03/2026, 10:05:00] Jo Dev: This message was deleted
[01/03/2026, 10:06:00] Dana changed the subject
[13/03/2026, 10:30:00 PM] Jo Dev: later one
"""

ANDROID = """3/14/26, 9:05 PM - Messages and calls are end-to-end encrypted.
3/14/26, 9:06 PM - Sam: dinner?
3/14/26, 9:07 PM - Jo Dev: sounds good
3/14/26, 9:07 PM - Jo Dev: <Media omitted>
3/14/26, 9:08 PM - Sam: great\nsee you
"""


def test_parse_date_orders():
    msgs, _ = parse_chat(IOS)
    assert msgs[-1]["ts"].day == 13 and msgs[-1]["ts"].hour == 22
    assert parse_chat(ANDROID)[0][1]["ts"].month == 3  # second field > 12 -> m/d/y
    msgs, _ = parse_chat("01/02/2026, 10:00 - A: hi there\n", "mdy")
    assert (msgs[0]["ts"].month, msgs[0]["ts"].day) == (1, 2)
    msgs, _ = parse_chat("01/02/2026, 10:00 - A: hi there\n")
    assert (msgs[0]["ts"].month, msgs[0]["ts"].day) == (2, 1)
    msgs, _ = parse_chat("2026-02-01, 10:00 a.m. - A: hi there\n")
    assert (msgs[0]["ts"].month, msgs[0]["ts"].day, msgs[0]["ts"].hour) == (2, 1, 10)


def test_whatsapp_ios_and_android_with_names(home, me, tmp_path):
    (tmp_path / "ios.txt").write_text(IOS)
    (tmp_path / "android.txt").write_text(ANDROID)
    src = WhatsApp()
    locs = src.discover(me, {"paths": [str(tmp_path)]})
    assert len(locs) == 2
    ios = next(lc for lc in locs if lc.path.name == "ios.txt")
    units, cur = run(src, ios, None, me)
    assert [u.text for u in units] == ["yes! my number is [phone]\nand it is the second line\nok", "later one"]
    u = units[0]
    assert (u.audience, u.register, u.kind) == ("close", "close", "message") and u.parts == 2
    assert u.context[0].who == "other1" and u.context[0].text == "are you free tonight?"
    assert u.ts.tzinfo is not None and u.meta["tz"] == "assumed-utc" and "ios" not in (u.thread_id or "")
    assert units[1].ts.day == 13
    again, _ = run(src, ios, cur, me)
    assert again == []
    android = next(lc for lc in locs if lc.path.name == "android.txt")
    units, _ = run(src, android, None, me)
    assert [u.text for u in units] == ["sounds good"]
    assert units[0].ts.month == 3 and units[0].ts.day == 14 and units[0].ts.hour == 21


def test_whatsapp_group_and_auto_self_detection(home, me, tmp_path):
    from mannerism.core import Me

    anon = Me()
    (tmp_path / "a.txt").write_text(IOS)  # 1:1, Jo Dev <-> Dana
    (tmp_path / "b.txt").write_text(
        "[05/03/2026, 11:00:00] Eve: hello all\n[05/03/2026, 11:01:00] Jo Dev: hey Eve\n"
        "[05/03/2026, 11:02:00] Sam: yo\n"
    )  # 3 senders: a group file, never a vote (P0-1)
    # A second genuine 1:1 (Jo Dev <-> Max) so Jo Dev has 2 DISTINCT corroborating counterparts: the
    # per-candidate vote needs this even though the old global-intersection algorithm did not.
    (tmp_path / "c.txt").write_text("[08/03/2026, 09:00:00] Max: hey\n[08/03/2026, 09:01:00] Jo Dev: hi Max\n")
    src = WhatsApp()
    locs = src.discover(anon, {"paths": [str(tmp_path)]})
    b = next(lc for lc in locs if lc.path.name == "b.txt")
    units, _ = run(src, b, None, anon)
    assert [u.text for u in units] == ["hey Eve"]
    assert (units[0].audience, units[0].register) == ("group", "group")
    assert units[0].context[0].text == "hello all"
    assert units[0].meta.get("self_detected") is True  # a vote, not a config/name match: flagged as a guess
    # one file only and no name: cannot attribute
    single = tmp_path / "solo"
    single.mkdir()
    (single / "only.txt").write_text(IOS)
    src2 = WhatsApp()
    loc = src2.discover(anon, {"paths": [str(single / "only.txt")]})[0]
    units, cur = run(src2, loc, None, anon)
    assert units == [] and cur == {"needs_self_name": True}
    src3 = WhatsApp()
    loc = src3.discover(anon, {"paths": [str(single / "only.txt")], "self_name": "jo dev"})[0]
    conf_units, conf_cur = run(src3, loc, None, anon)
    assert len(conf_units) == 2
    assert not conf_units[0].meta.get("self_detected")  # an explicit config name is a fact, not a guess
    assert "self_detected" not in conf_cur


def test_whatsapp_lurker_group_does_not_poison_detection(home, tmp_path):
    """A lurker (someone who never sends a message) in a group chat broke the OLD global
    set-intersection algorithm: Jo is absent from the group file's sender set, so intersecting it with
    Jo's other, genuine 1:1 chats gave an empty set and self-detection failed outright. The P0-1
    per-candidate vote ignores group files for voting instead, so Jo's two real 1:1s are enough."""
    from mannerism.core import Me

    anon = Me()
    (tmp_path / "alice.txt").write_text("[01/04/2026, 09:00:00] Alice: hi\n[01/04/2026, 09:01:00] Jo Dev: hey Alice\n")
    (tmp_path / "dave.txt").write_text("[02/04/2026, 09:00:00] Dave: hi\n[02/04/2026, 09:01:00] Jo Dev: hey Dave\n")
    # Jo lurks here (reads but never sends): absent from this file's sender set entirely.
    (tmp_path / "family.txt").write_text(
        "[03/04/2026, 09:00:00] Bob: dinner sunday?\n[03/04/2026, 09:01:00] Carol: yes!\n"
    )
    src = WhatsApp()
    loc = next(lc for lc in src.discover(anon, {"paths": [str(tmp_path)]}) if lc.path.name == "alice.txt")
    units, _ = run(src, loc, None, anon)
    assert [u.text for u in units] == ["hey Alice"]
    assert units[0].meta.get("self_detected") is True


def test_whatsapp_zip_and_truncation_rules(home, me, tmp_path):
    z = tmp_path / "WhatsApp Chat with Dana.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("_chat.txt", IOS)
        zf.writestr("IMG-0001.jpg", b"x")
    src = WhatsApp()
    loc = src.discover(me, {"paths": [str(z)]})[0]
    units, cur = run(src, loc, None, me)
    assert len(units) == 2
    assert run(src, loc, cur, me)[0] == []
    inbox = home / ".mannerism" / "inbox" / "whatsapp"
    inbox.mkdir(parents=True)
    (inbox / "chat.txt").write_text("")
    assert any(lc.path == inbox / "chat.txt" for lc in src.discover(me, {}))


def test_whatsapp_auto_detect_refuses_when_ambiguous(home, tmp_path):
    from mannerism.core import Me

    anon = Me()
    d = tmp_path / "chats"
    d.mkdir()
    # Jo and Eve are both in every file: two common names, so no safe pick
    (d / "a.txt").write_text("[05/03/2026, 11:00:00] Eve: hello\n[05/03/2026, 11:01:00] Jo Dev: hi Eve\n")
    (d / "b.txt").write_text("[06/03/2026, 11:00:00] Eve: yo\n[06/03/2026, 11:01:00] Jo Dev: hi again\n")
    src = WhatsApp()
    for loc in src.discover(anon, {"paths": [str(d)]}):
        units, cur = run(src, loc, None, anon)
        assert units == [] and cur == {"needs_self_name": True}
    # a third chat where only Jo is common to all three makes the pick unique
    (d / "c.txt").write_text("[07/03/2026, 11:00:00] Sam: yo\n[07/03/2026, 11:01:00] Jo Dev: hi Sam\n")
    src = WhatsApp()
    loc = next(lc for lc in src.discover(anon, {"paths": [str(d)]}) if lc.path.name == "a.txt")
    assert [u.text for u in run(src, loc, None, anon)[0]] == ["hi Eve"]
