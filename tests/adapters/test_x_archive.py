import json
import zipfile

from mannerism.adapters.x_archive import XArchive

from .conftest import run


def tw(i, text, created="Wed Oct 10 20:19:24 +0000 2018", reply=None, **kw):
    d = {
        "id_str": str(i),
        "full_text": text,
        "created_at": created,
        "retweeted": False,
        "entities": {"urls": [{"url": "https://t.co/abc", "expanded_url": "https://example.org/z"}]},
    }
    if reply:
        d["in_reply_to_status_id_str"] = str(reply)
        d["in_reply_to_screen_name"] = "ana.k"
    d.update(kw)
    return {"tweet": d}


LONG = "A long thought about tooling, " + "which keeps going and going " * 12 + "until the very end."


def js(name, items, part=0):
    return f"window.YTD.{name}.part{part} = " + json.dumps(items)


def build(dirpath):
    d = dirpath / "data"
    d.mkdir(parents=True)
    (d / "tweets.js").write_text(
        js(
            "tweets",
            [
                tw(1, "shipping the new parser today https://t.co/abc"),
                tw(2, "RT @someone: not mine"),
                tw(3, "@ana.k @bo.x thanks, that fixed it &amp; more", reply=99),
                tw(4, LONG[:140] + "… https://t.co/zzz", created="Thu Oct 11 09:00:00 +0000 2018"),
                tw(5, "https://t.co/onlyalink"),
            ],
        )
    )
    (d / "tweets-part1.js").write_text(
        js("tweets", [tw(6, "second part tweet here", created="Fri Oct 12 09:00:00 +0000 2018")], 1)
    )
    (d / "note-tweet.js").write_text(
        js(
            "note_tweet",
            [
                {"noteTweet": {"noteTweetId": "n1", "core": {"text": LONG}, "createdAt": "2018-10-11T09:00:05.000Z"}},
                {
                    "noteTweet": {
                        "noteTweetId": "n2",
                        "core": {"text": "A standalone long note, with words."},
                        "createdAt": "2018-11-01T09:00:00.000Z",
                    }
                },
            ],
        )
    )


def check(units):
    texts = {u.source_ref: u for u in units}
    assert set(texts) == {"tweet:1", "tweet:3", "tweet:6", "note:n1", "note:n2"}
    assert texts["tweet:1"].text == "shipping the new parser today"
    assert texts["tweet:1"].kind == "post"
    r = texts["tweet:3"]
    assert (
        r.text == "thanks, that fixed it & more"
        and r.kind == "reply"
        and r.audience_ref
        and "ana" not in r.audience_ref
    )
    assert texts["note:n1"].text == LONG
    for u in units:
        assert (u.register, u.audience) == ("public", "public") and u.meta["public"] and u.ts.tzinfo is not None
    assert texts["tweet:1"].ts.year == 2018


def test_x_archive_dir(home, me, tmp_path):
    arch = tmp_path / "arch"
    build(arch)
    src = XArchive()
    locs = src.discover(me, {"paths": [str(arch)]})
    assert [lc.path for lc in locs] == [arch]
    units, cur = run(src, locs[0], None, me)
    check(units)
    assert run(src, locs[0], cur, me)[0] == []


def test_x_archive_zip_and_inbox(home, me, tmp_path):
    arch = tmp_path / "arch"
    build(arch)
    inbox = home / ".mannerism" / "inbox" / "x_archive"
    inbox.mkdir(parents=True)
    with zipfile.ZipFile(inbox / "twitter-archive.zip", "w") as z:
        for p in (arch / "data").iterdir():
            z.write(p, f"data/{p.name}")
        z.writestr("data/manifest.js", "window.YTD.manifest.part0 = {}")
    src = XArchive()
    locs = src.discover(me, {})
    assert len(locs) == 1
    units, _ = run(src, locs[0], None, me)
    check(units)
