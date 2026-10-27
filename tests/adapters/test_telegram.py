import json

from mannerism.adapters.telegram import Telegram

from .conftest import run


def m(i, frm, fid, text, ts, **kw):
    d = {
        "id": i,
        "type": "message",
        "date": "2026-03-01T10:00:00",
        "date_unixtime": str(ts),
        "from": frm,
        "from_id": fid,
        "text": text,
    }
    d.update(kw)
    return d


T0 = 1_772_000_000


def export(with_pi=True):
    chats = [
        {
            "name": "Dana",
            "type": "personal_chat",
            "id": 1,
            "messages": [
                m(1, "Dana", "user200", "are you around?", T0),
                m(2, "Jo", "user100", "yes, call me on +44 20 7946 0958", T0 + 10),
                m(3, "Jo", "user100", "or later", T0 + 20),
                m(4, "Jo", "user100", "forwarded", T0 + 30, forwarded_from="Someone"),
                {"id": 5, "type": "service", "date": "2026-03-01T10:00:00", "actor": "Jo", "action": "pin_message"},
                m(6, "Dana", "user200", "ok", T0 + 400),
                m(
                    7,
                    "Jo",
                    "user100",
                    [
                        {"type": "plain", "text": "see "},
                        {"type": "link", "text": "https://example.org/x"},
                        " and ",
                        {"type": "bold", "text": "this"},
                    ],
                    T0 + 900,
                ),
            ],
        },
        {
            "name": "Team",
            "type": "private_supergroup",
            "id": 2,
            "messages": [
                m(1, "Eve", "user300", "standup now", T0),
                m(2, "Jo", "user100", "joining in a minute", T0 + 5),
                m(3, "Dana", "user200", "Eve says hi", T0 + 8),
            ],
        },
        {"name": "News", "type": "public_channel", "id": 3, "messages": [m(1, "News", "channel9", "broadcast", T0)]},
        {"name": "Me", "type": "saved_messages", "id": 4, "messages": [m(1, "Jo", "user100", "note to self", T0)]},
    ]
    out = {"chats": {"list": chats}}
    if with_pi:
        out["personal_information"] = {"user_id": 100}
    return out


def test_telegram_own_text_only(home, me, tmp_path):
    p = tmp_path / "result.json"
    p.write_text(json.dumps(export()))
    src = Telegram()
    loc = src.discover(me, {"paths": [str(p)]})[0]
    units, cur = run(src, loc, None, me)
    by_text = {u.text: u for u in units}
    assert set(by_text) == {
        "yes, call me on [phone]\nor later",
        "see https://example.org/x and this",
        "joining in a minute",
    }
    close = by_text["yes, call me on [phone]\nor later"]
    assert (close.audience, close.register, close.kind, close.parts) == ("close", "close", "message", 2)
    assert [c.who for c in close.context] == ["other1"] and close.context[0].text == "are you around?"
    grp = by_text["joining in a minute"]
    assert (grp.audience, grp.register) == ("group", "group")
    assert grp.context[0].text == "standup now" and "Eve" not in grp.context[0].text
    assert close.thread_id and "Dana" not in (close.thread_id or "")
    again, cur2 = run(src, loc, cur, me)
    assert again == []
    # new messages appended to the same export file are incremental by message id
    data = export()
    data["chats"]["list"][0]["messages"].append(m(8, "Jo", "user100", "one more thing", T0 + 5000))
    p.write_text(json.dumps(data))
    new, _ = run(src, loc, cur2, me)
    assert [u.text for u in new] == ["one more thing"]


def test_telegram_auto_detect_only_when_safe(home, me, tmp_path):
    src = Telegram()

    def run_export(data, name):
        p = tmp_path / name
        p.write_text(json.dumps(data))
        return run(src, src.discover(me, {"paths": [str(p)]})[0], None, me)

    # two personal chats sharing exactly one participant: that is the own account
    data = export(with_pi=False)
    data["chats"]["list"].insert(
        1,
        {
            "name": "Bo",
            "type": "personal_chat",
            "id": 9,
            "messages": [m(1, "Bo", "user400", "hey", T0), m(2, "Jo", "user100", "hey bo", T0 + 5)],
        },
    )
    units, _ = run_export(data, "ok.json")
    assert {u.text for u in units} >= {"hey bo", "joining in a minute"} and len(units) == 4

    # the same account is the most frequent sender in many chats, but only ONE personal chat: refuse
    units, cur = run_export(export(with_pi=False), "one-personal.json")
    assert units == [] and cur == {"needs_self_id": True}

    # two personal chats with two common participants: ambiguous, refuse
    data = export(with_pi=False)
    data["chats"]["list"][0]["messages"].append(m(8, "Cy", "user500", "x", T0 + 1))
    data["chats"]["list"].insert(
        1,
        {
            "name": "Bo",
            "type": "personal_chat",
            "id": 9,
            "messages": [m(1, "Bo", "user500", "hey", T0), m(2, "Jo", "user100", "hey bo", T0 + 5)],
        },
    )
    units, cur = run_export(data, "ambiguous.json")
    assert units == [] and cur == {"needs_self_id": True}

    # a single-chat export cannot be attributed; an explicit handle fixes it
    single = tmp_path / "single.json"
    single.write_text(json.dumps(export()["chats"]["list"][0]))
    loc = next(lc for lc in src.discover(me, {"paths": [str(single)]}) if lc.path == single)
    units, cur = run(src, loc, None, me)
    assert units == [] and cur == {"needs_self_id": True}
    me.handles["telegram"] = "100"
    units, _ = run(src, loc, None, me)
    assert len(units) == 2


def test_telegram_public_group_only_own(home, me, tmp_path):
    data = export()
    data["chats"]["list"].append(
        {
            "name": "Open",
            "type": "public_supergroup",
            "id": 7,
            "messages": [
                m(1, "Zed", "user999", "someone else", T0),
                m(2, "Jo", "user100", "my open post here", T0 + 3),
            ],
        }
    )
    p = tmp_path / "r.json"
    p.write_text(json.dumps(data))
    src = Telegram()
    units, _ = run(src, src.discover(me, {"paths": [str(p)]})[0], None, me)
    pub = [u for u in units if u.audience == "public"]
    # decision 2: Telegram gives no public PROVENANCE -- audience is still "public" (who could read it), but
    # the register is "?" (undecided) and meta.public is never set, so it is not admitted to the public
    # register's bank without an explicit voice_feedback(register="public") or a real published source.
    assert [u.text for u in pub] == ["my open post here"] and pub[0].register == "?" and not pub[0].context
    assert not pub[0].meta.get("public")


def test_telegram_bad_file(home, me, tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{oops")
    src = Telegram()
    assert list(src.read(src.discover(me, {"paths": [str(p)]})[0], None, me)[0]) == []
