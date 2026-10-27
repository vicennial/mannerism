import json
import zipfile

from mannerism.adapters.chatgpt import ChatGPT

from .conftest import run


def node(nid, role, text, parent, children, t, ctype="text", parts=None, **meta):
    return {
        nid: {
            "id": nid,
            "parent": parent,
            "children": children,
            "message": {
                "author": {"role": role},
                "create_time": t,
                "metadata": meta,
                "content": {"content_type": ctype, "parts": parts if parts is not None else [text]},
            },
        }
    }


def conv():
    m = {"root": {"id": "root", "parent": None, "children": ["u1"], "message": None}}
    m.update(
        node("u1", "user", "explain the borrow checker, I am at jo.dev@example.org", "root", ["a1"], 1_770_000_000)
    )
    m.update(node("a1", "assistant", "It tracks ownership. Questions?", "u1", ["u2", "u2b"], 1_770_000_010))
    m.update(node("u2", "user", "abandoned branch", "a1", [], 1_770_000_020))
    m.update(
        node("u2b", "user", "", "a1", ["a2"], 1_770_000_030, parts=["give an example", {"asset": 1}, "with lifetimes"])
    )
    m.update(node("a2", "assistant", "Here you go", "u2b", ["u3"], 1_770_000_040))
    m.update(node("u3", "user", "", "a2", ["u4"], 1_770_000_050, ctype="multimodal_text", parts=[{"x": 1}]))
    m.update(node("u4", "user", "custom instructions text", "u3", ["u5"], 1_770_000_060, is_user_system_message=True))
    m.update(node("u5", "user", "thanks, that helps a lot", "u4", [], 1_770_000_070))
    return {"id": "conv-1", "title": "t", "create_time": 1_770_000_000, "current_node": "u5", "mapping": m}


def test_chatgpt_active_branch_only(home, me, tmp_path):
    p = tmp_path / "conversations.json"
    p.write_text(json.dumps([conv(), "junk"]))
    src = ChatGPT()
    locs = src.discover(me, {"paths": [str(p)]})
    assert len(locs) == 1
    units, cur = run(src, locs[0], None, me)
    assert [u.text for u in units] == [
        "explain the borrow checker, I am at [email]",
        "give an example\nwith lifetimes",
        "thanks, that helps a lot",
    ]
    assert all((u.kind, u.audience, u.register) == ("prompt", "agent", "agent") for u in units)
    assert units[1].context[0].who == "agent" and units[1].context[0].text.startswith("It tracks")
    assert "abandoned branch" not in " ".join(u.text for u in units)
    assert units[0].ts.tzinfo is not None
    again, cur2 = run(src, locs[0], cur, me)
    assert again == []
    p.write_text(json.dumps([conv(), conv()]))
    changed, _ = run(src, locs[0], cur2, me)
    assert len(changed) == 6  # re-read when changed; the engine dedupes by unit_id


def test_chatgpt_zip_and_inbox(home, me):
    inbox = home / ".mannerism" / "inbox" / "chatgpt"
    inbox.mkdir(parents=True)
    with zipfile.ZipFile(inbox / "export.zip", "w") as z:
        z.writestr("export/conversations.json", json.dumps([conv()]))
        z.writestr("export/chat.html", "<html/>")
    src = ChatGPT()
    locs = src.discover(me, {})
    assert len(locs) == 1
    units, _ = run(src, locs[0], None, me)
    assert len(units) == 3
    (inbox / "bad.zip").write_bytes(b"not a zip")
    loc_bad = next(lc for lc in src.discover(me, {}) if lc.path.name == "bad.zip")
    assert list(src.read(loc_bad, None, me)[0]) == []
