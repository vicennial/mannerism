import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

from mannerism.adapters.continue_dev import Continue

from .conftest import run

FIXTURES = Path(__file__).parent / "fixtures" / "continue"
CN_SESSION = "21310ff4-82f2-4998-a79a-1fe4b76f5eb2"  # written by a real `cn` 1.5.47 run (headless, mock model)
CREATED_MS = 1792619058588


def install(home: Path) -> Path:
    base = home / ".continue"
    shutil.copytree(FIXTURES, base)
    return base


def user(content, editor=None, **kw):
    item = {"message": {"role": "user", "content": content}, "contextItems": []}
    if editor is not None:
        item["editorState"] = editor
    item["message"].update(kw)
    return item


def agent(text):
    return {"message": {"role": "assistant", "content": text}, "contextItems": []}


def doc(*blocks):
    return {"type": "doc", "content": list(blocks)}


def para(*nodes):
    return {"type": "paragraph", "content": list(nodes)}


def text(t):
    return {"type": "text", "text": t}


def write_session(base: Path, sid: str, history, created_ms=CREATED_MS, index=True):
    sessions = base / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{sid}.json").write_text(
        json.dumps({"sessionId": sid, "title": "t", "workspaceDirectory": "/home/jo.dev/proj", "history": history})
    )
    if index:
        rows = [{"sessionId": sid, "title": "t", "dateCreated": str(created_ms), "workspaceDirectory": "/x"}]
        (sessions / "sessions.json").write_text(json.dumps(rows))
    return sessions / f"{sid}.json"


def test_continue_real_cn_session(home, me):
    install(home)
    src = Continue()
    locs = src.discover(me, {})
    assert [lc.path.name for lc in locs] == [f"{CN_SESSION}.json"]  # the sessions.json index is not a session
    units, cur = run(src, locs[0], None, me)
    # the bare "/help" (a slash command, not writing) is dropped; "@schedule.txt" stays as typed
    assert [u.text for u in units] == [
        "hello there, when does the first ferry leave?",
        "ok thanks, can you look at @schedule.txt and tell me the second sailing? keep it short pls",
        "nice, that is all - cheers",
    ]
    assert all((u.kind, u.audience, u.register, u.source) == ("prompt", "agent", "agent", "continue") for u in units)
    # no per-message time exists: every turn takes the session's dateCreated from sessions.json
    assert {u.ts for u in units} == {datetime.fromtimestamp(CREATED_MS / 1000, UTC)}
    assert units[0].context == ()
    assert units[1].context[0].who == "agent" and units[1].context[0].text.startswith("Sure. The ferry timetable")
    assert units[0].thread_id == units[2].thread_id and units[0].meta["cwd_hash"]
    assert len({u.unit_id for u in units}) == 3
    again, cur2 = run(src, locs[0], cur, me)
    assert again == [] and cur2 == cur


def test_continue_gui_editor_state(home, me):
    base = home / ".continue"
    mention = {"type": "mention", "attrs": {"id": "file", "label": "schedule.txt", "itemType": "file"}}
    history = [
        # the extension's content is the expanded prompt; what the person typed is in editorState
        user(
            "Explain schedule.txt\n```ts schedule.txt (1-3)\nconst x = 1\n```\n",
            doc(
                para(text("explain "), mention, text(" please")),
                {"type": "code-block", "attrs": {"item": {"name": "schedule.txt", "content": "const x = 1"}}},
            ),
            id="m1",
        ),
        agent("It lists two sailings."),
        user(
            "You are a reviewer. Review the code below.\n...long rendered prompt file...",
            doc({"type": "prompt-block", "attrs": {"item": {"name": "review"}}}, para(text("focus on naming"))),
            id="m2",
        ),
        agent("Looks fine."),
        user("rendered prompt only", doc({"type": "prompt-block", "attrs": {"item": {"name": "review"}}}), id="m3"),
        user("this one has no editor state", id="m4"),
        user([{"type": "text", "text": "multimodal text"}, {"type": "imageUrl", "imageUrl": {"url": "x"}}], id="m5"),
        user("fallback", doc(para()), id="m6"),  # an empty document means nothing was typed
    ]
    write_session(base, "gui-1", history)
    src = Continue()
    (loc,) = src.discover(me, {})
    units, _ = run(src, loc, None, me)
    assert [u.text for u in units] == [
        "explain @schedule.txt please",
        "/review\nfocus on naming",
        "this one has no editor state",
        "multimodal text",
    ]
    assert units[1].context[0].text == "It lists two sailings."
    assert units[2].context[0].text == "Looks fine."  # the drop of the prompt-only turn in between keeps it
    assert [u.source_ref for u in units][:2] == ["gui-1:m1", "gui-1:m2"]


def test_continue_rewrite_and_edit_do_not_double_count(home, me):
    base = home / ".continue"
    path = write_session(base, "s1", [user("first thing"), agent("ok"), user("second thing"), user("second thing")])
    src = Continue()
    (loc,) = src.discover(me, {})
    first, cur = run(src, loc, None, me)
    assert [u.text for u in first] == ["first thing", "second thing", "second thing"]
    assert len({u.unit_id for u in first}) == 3  # a repeated message is a separate turn
    # the file is rewritten whole with one more turn: earlier turns keep their unit ids
    write_session(
        base, "s1", [user("first thing"), agent("ok"), user("second thing"), user("second thing"), user("third")]
    )
    assert path.stat().st_size
    more, cur2 = run(src, loc, cur, me)
    assert [u.unit_id for u in more[:3]] == [u.unit_id for u in first] and more[3].text == "third"
    # an earlier message removed: the remaining ones keep their ids too (no index in the ref)
    write_session(base, "s1", [user("second thing"), user("second thing"), user("third")])
    after, _ = run(src, loc, cur2, me)
    assert {u.unit_id for u in after} <= {u.unit_id for u in more}


def test_continue_torn_and_odd_files(home, me):
    base = home / ".continue"
    path = write_session(base, "s1", [user("hello there")])
    src = Continue()
    (loc,) = src.discover(me, {})
    full = path.read_text()
    path.write_text(full[: len(full) // 2])  # caught mid-write
    units, cur = run(src, loc, None, me)
    assert units == [] and cur == {}  # nothing stored: the next sync tries again
    path.write_text(full)
    units, cur = run(src, loc, cur, me)
    assert [u.text for u in units] == ["hello there"]
    for body in ("[]", "null", '{"history": 3}', '{"history": [1, {"message": 5}, {"message": {"role": "user"}}]}', ""):
        path.write_text(body)
        assert list(src.read(loc, {}, me)[0]) == []
    path.unlink()
    assert list(src.read(loc, {}, me)[0]) == []


def test_continue_time_fallback_and_masking(home, me):
    base = home / ".continue"
    path = write_session(
        base,
        "s1",
        [user("mail me at jo.dev@example.org"), agent("sent to jo.dev@example.org"), user("ok")],
        index=False,
    )
    src = Continue()
    (loc,) = src.discover(me, {})
    units, _ = run(src, loc, None, me)
    mtime = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    assert units[0].ts == mtime  # not in the index: the file's mtime
    assert units[0].text == "mail me at [email]"
    assert "jo.dev@example.org" not in units[1].context[0].text


def test_continue_global_dir_and_extra_dirs(home, me, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    write_session(other, "env-1", [user("from the env dir")])
    write_session(home / ".continue", "def-1", [user("from the default dir")])
    write_session(tmp_path / "extra", "x-1", [user("from a configured dir")])
    monkeypatch.setenv("CONTINUE_GLOBAL_DIR", str(other))
    src = Continue()
    names = sorted(lc.path.name for lc in src.discover(me, {"dirs": [str(tmp_path / "extra")]}))
    assert names == ["def-1.json", "env-1.json", "x-1.json"]
    monkeypatch.delenv("CONTINUE_GLOBAL_DIR")
    assert [lc.path.name for lc in src.discover(me, {})] == ["def-1.json"]
    assert Continue().discover(me, {"dirs": [None, 5]}) is not None
