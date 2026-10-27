import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from mannerism.adapters.cline import Cline, global_storage_dirs

from .conftest import run

FIXTURES = Path(__file__).parent / "fixtures" / "cline_sdk"
SDK_SESSION = "1792619072744_dcx0j"  # written by a real `cline` 3.0.73 run (TUI, mock model); system prompt elided
TS0 = 1_770_000_000_000


def install_sdk(home: Path) -> Path:
    data = home / ".cline" / "data"
    shutil.copytree(FIXTURES, data)
    return data


def sdk_user(text, ts=TS0, mid=None, **kw):
    body = text if isinstance(text, list) else [{"type": "text", "text": text}]
    return {"id": mid or f"u{ts}", "role": "user", "content": body, "ts": ts, **kw}


def sdk_agent(text, ts=TS0 + 1, mid=None):
    return {"id": mid or f"a{ts}", "role": "assistant", "content": [{"type": "text", "text": text}], "ts": ts}


def write_sdk(data: Path, sid: str, messages, manifest=None, stem=None, **top):
    d = data / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    body = {"version": 1, "updated_at": "2026-01-02T03:04:05.000Z", "sessionId": sid, "messages": messages, **top}
    p = d / f"{stem or sid}.messages.json"
    p.write_text(json.dumps(body))
    if manifest is not None:
        (d / f"{sid}.json").write_text(json.dumps(manifest))
    return p


def say(kind, text, ts, **kw):
    return {"ts": ts, "type": "say", "say": kind, "text": text, **kw}


def write_tasks(root: Path, tid: str, rows):
    p = root / "tasks" / tid / "ui_messages.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(rows))
    return p


def texts(units):
    return [u.text for u in units]


def test_cline_real_sdk_session(home, me):
    install_sdk(home)
    src = Cline()
    locs = src.discover(me, {})
    assert [lc.path.name for lc in locs] == [f"{SDK_SESSION}.messages.json"]
    units, cur = run(src, locs[0], None, me)
    # the <user_input> wrapper and the plan-mode <mode_notice> are gone; the @-mention shows as a mention
    assert texts(units) == [
        "hello there, when does the first ferry leave?",
        "ok thanks, can you look at @schedule.txt  and tell me the second sailing? keep it short pls",
        "nice, that is all - cheers",
    ]
    assert all((u.kind, u.audience, u.register, u.source) == ("prompt", "agent", "agent", "cline") for u in units)
    assert units[0].ts == datetime.fromtimestamp(1792619073.014, UTC)
    assert units[0].context == () and units[1].context[0].text.startswith("Sure. The ferry timetable")
    assert len({u.unit_id for u in units}) == 3
    again, cur2 = run(src, locs[0], cur, me)
    assert again == [] and cur2 == cur


def test_cline_sdk_filters(home, me):
    data = home / ".cline" / "data"
    mention = "ok see [file: a.txt] and [image: shot.png]please"
    msgs = [
        sdk_user('<user_input mode="act">fix the flaky test</user_input>', TS0, "u1"),
        sdk_agent("Which one?"),
        sdk_user('<user_input mode="yolo">the one in tests/test_x.py</user_input>', TS0 + 2, "u2"),
        sdk_user(
            "[SYSTEM] This run is not complete until you call submit_and_exit.",
            TS0 + 3,
            "u3",
            metadata={"userRunSpan": 0},
        ),
        sdk_user("[SYSTEM] reminder without metadata", TS0 + 4, "u4"),
        sdk_user("[TASK RESUMPTION] This task was interrupted", TS0 + 5, "u5"),
        sdk_user([{"type": "tool_result", "content": "output"}], TS0 + 6, "u6"),
        sdk_user("summary of earlier turns", TS0 + 7, "u7", metadata={"kind": "compaction", "userRunSpan": 3}),
        sdk_user("recover", TS0 + 8, "u8", metadata={"kind": "recovery_notice"}),
        sdk_user(
            '<user_command slash="deploy">Run the deploy workflow:\n1. build\n2. push</user_command> to staging',
            TS0 + 9,
            "u9",
        ),
        sdk_user(
            '<user_input mode="act"><user_command slash="summarize">long instructions</user_command></user_input>',
            TS0 + 10,
            "u10",
        ),
        sdk_user(
            '<user_input mode="act">'
            + mention
            + '</user_input>\n<file_content path="/x/a.txt">secret body</file_content>',
            TS0 + 11,
            "u11",
        ),
        sdk_user("plain string content, no wrapper", TS0 + 12, "u12"),
        {"id": "u13", "role": "user", "content": "string body", "ts": TS0 + 13},
        {"role": "user", "content": [{"type": "text", "text": "no id and no ts"}]},
        sdk_user("   ", TS0 + 14, "u15"),
        "junk",
    ]
    write_sdk(data, "s1", msgs, manifest={"session_id": "s1", "metadata": {"sessionHistoryOrigin": {"mode": "user"}}})
    src = Cline()
    (loc,) = src.discover(me, {})
    units, _ = run(src, loc, None, me)
    assert texts(units) == [
        "fix the flaky test",
        "the one in tests/test_x.py",
        "/deploy to staging",
        "ok see @a.txt and please",
        "plain string content, no wrapper",
        "string body",
        "no id and no ts",
    ]
    assert units[1].context[0].text == "Which one?"
    assert units[-1].source_ref == "s1:14"  # a message without an id is referred to by position
    assert units[-1].ts == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)  # ... and without ts by the file's updated_at
    assert "secret body" not in " ".join(texts(units))


def test_cline_sdk_skips_child_and_automated_sessions(home, me):
    data = home / ".cline" / "data"
    msgs = [sdk_user('<user_input mode="act">hello</user_input>')]
    write_sdk(data, "root", msgs, manifest={"session_id": "root"})
    write_sdk(data, "root", msgs, stem="sub-agent-1")  # a sub-agent's file in the root session's directory
    write_sdk(data, "child", msgs, manifest={"parent_session_id": "root"})
    write_sdk(data, "auto", msgs, manifest={"metadata": {"sessionHistoryOrigin": {"mode": "automation"}}})
    write_sdk(data, "sched", msgs, manifest={"metadata": {"sessionHistoryOrigin": {"mode": "user", "trigger": "cron"}}})
    write_sdk(data, "team", msgs, origin={"source": "cli", "mode": "team"})
    write_sdk(data, "odd", msgs, manifest=["not", "a", "dict"])
    src = Cline()
    locs = src.discover(me, {})
    assert sorted(lc.path.parent.name for lc in locs) == ["auto", "child", "odd", "root", "sched", "team"]
    by = {lc.path.parent.name: texts(run(src, lc, None, me)[0]) for lc in locs}
    assert by == {"root": ["hello"], "odd": ["hello"], "auto": [], "child": [], "sched": [], "team": []}


def test_cline_classic_tasks_in_data_dir(home, me):
    rows = [
        say("task", "set up the linter, my repo is at jo.dev@example.org", 1_769_000_000_000),
        say("api_req_started", json.dumps({"request": "..."}), 1_769_000_000_100),
        say("text", "I will add a config file.", 1_769_000_001_000),
        {
            "ts": 1_769_000_002_000,
            "type": "ask",
            "ask": "followup",
            "text": json.dumps({"question": "Which rules?", "options": []}),
        },
        say("user_feedback", "just the defaults please", 1_769_000_003_000),
        say("user_feedback_diff", "--- a\n+++ b", 1_769_000_003_500),
        say("subagent", json.dumps({"prompt": "scan the repo"}), 1_769_000_004_000),
        say("use_subagents", json.dumps({"prompts": ["x"]}), 1_769_000_004_100),
        say("text", "partial streaming text", 1_769_000_005_000, partial=True),
        say("completion_result", "Linter configured.", 1_769_000_006_000),
        say("user_feedback", "   ", 1_769_000_007_000),
        say("user_feedback", "thanks!", 1_769_000_008_000),
        {"ts": 1_769_000_009_000, "type": "say", "say": "user_feedback"},
        "junk",
    ]
    write_tasks(home / ".cline" / "data", "1769000000000", rows)
    src = Cline()
    (loc,) = src.discover(me, {})
    units, cur = run(src, loc, None, me)
    assert texts(units) == ["set up the linter, my repo is at [email]", "just the defaults please", "thanks!"]
    assert units[0].ts == datetime.fromtimestamp(1_769_000_000, UTC)
    assert units[1].context[0].text == "Which rules?"
    assert units[2].context[0].text == "Linter configured."
    assert units[0].source_ref == "1769000000000:1769000000000"
    assert run(src, loc, cur, me)[0] == []


def test_cline_classic_edit_and_retry_rewrites_the_file(home, me):
    first = [
        say("task", "write a parser", 1_769_000_000_000),
        say("text", "Done.", 1_769_000_001_000),
        say("user_feedback", "make it faster", 1_769_000_002_000),
        say("text", "Done again.", 1_769_000_003_000),
    ]
    path = write_tasks(home / ".cline" / "data", "t1", first)
    src = Cline()
    (loc,) = src.discover(me, {})
    one, cur = run(src, loc, None, me)
    assert texts(one) == ["write a parser", "make it faster"]
    # the user edits the second message: the array is cut back and a new row is appended
    second = [
        *first[:2],
        say("user_feedback", "make it much faster", 1_769_000_010_000),
        say("text", "Ok.", 1_769_000_011_000),
    ]
    path.write_text(json.dumps(second))
    two, cur2 = run(src, loc, cur, me)
    assert texts(two) == ["write a parser", "make it much faster"]
    assert two[0].unit_id == one[0].unit_id  # the untouched prompt keeps its id: no double count
    # a shorter file than before (checkpoint restore to the start) still reads
    path.write_text(json.dumps(first[:1]))
    three, _ = run(src, loc, cur2, me)
    assert texts(three) == ["write a parser"]


@pytest.mark.parametrize("platform,config_parts", [
    ("linux", (".config",)),
    ("darwin", ("Library", "Application Support")),
    ("win32", ("AppData", "Roaming")),
])
def test_cline_classic_vscode_global_storage(home, me, monkeypatch, platform, config_parts):
    monkeypatch.setattr("mannerism.adapters.cline.sys.platform", platform)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    config_home = home.joinpath(*config_parts)
    rows = [say("task", "legacy prompt", 1_760_000_000_000)]
    stores = [
        config_home / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev",
        config_home / "Cursor" / "User" / "globalStorage" / "saoudrizwan.claude-dev",
        home / ".vscode-server" / "data" / "User" / "globalStorage" / "saoudrizwan.claude-dev",
    ]
    for i, store in enumerate(stores):
        write_tasks(store, f"176000000000{i}", rows)
    write_tasks(config_home / "Code" / "User" / "globalStorage" / "someone.else", "1", rows)  # another extension
    src = Cline()
    locs = src.discover(me, {})
    assert len(locs) == 3
    assert [texts(run(src, lc, None, me)[0]) for lc in locs] == [["legacy prompt"]] * 3
    # the same task copied from the old store to ~/.cline/data keeps its unit id
    write_tasks(home / ".cline" / "data", "1760000000000", rows)
    ids = {
        run(src, lc, None, me)[0][0].unit_id for lc in src.discover(me, {}) if lc.path.parent.name == "1760000000000"
    }
    assert len(ids) == 1
    if platform == "linux":
        monkeypatch.setenv("XDG_CONFIG_HOME", str(home / "xdg"))
        write_tasks(home / "xdg" / "Code" / "User" / "globalStorage" / "saoudrizwan.claude-dev", "5", rows)
        assert any(lc.path.parent.name == "5" for lc in src.discover(me, {}))


def test_cline_global_storage_dirs_per_platform(home, monkeypatch):
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    mac = global_storage_dirs("darwin")
    assert home / "Library" / "Application Support" / "Code" / "User" / "globalStorage" in mac
    assert home / "Library" / "Application Support" / "Windsurf" / "User" / "globalStorage" in mac
    win = global_storage_dirs("win32")
    assert home / "AppData" / "Roaming" / "Code - Insiders" / "User" / "globalStorage" in win
    assert home / "AppData" / "Roaming" / "Cursor" / "User" / "globalStorage" in win
    lin = global_storage_dirs("linux")
    assert home / ".config" / "VSCodium" / "User" / "globalStorage" in lin
    assert home / ".vscode-server" / "data" / "User" / "globalStorage" in lin


def test_cline_env_overrides_and_config_dirs(home, me, tmp_path, monkeypatch):
    msgs = [sdk_user('<user_input mode="act">hi</user_input>')]
    for name, data in (
        ("default", home / ".cline" / "data"),
        ("viaenv", tmp_path / "a"),
        ("viadir", tmp_path / "b" / "data"),
        ("extra", tmp_path / "c"),
    ):
        write_sdk(data, name, msgs)
    write_sdk(tmp_path / "elsewhere" / "sessions-root", "pinned", msgs)  # not under any data dir
    pinned = tmp_path / "pin"
    shutil.copytree(tmp_path / "elsewhere" / "sessions-root" / "sessions", pinned)
    src = Cline()

    def found(config=None):
        return sorted(lc.path.parent.name for lc in src.discover(me, config or {}))

    assert found() == ["default"]
    monkeypatch.setenv("CLINE_DATA_DIR", str(tmp_path / "a"))
    assert found() == ["default", "viaenv"]
    monkeypatch.delenv("CLINE_DATA_DIR")
    monkeypatch.setenv("CLINE_DIR", str(tmp_path / "b"))
    assert found() == ["default", "viadir"]
    monkeypatch.setenv("CLINE_SESSION_DATA_DIR", str(pinned))
    assert found({"dirs": [str(tmp_path / "c")]}) == ["default", "extra", "pinned", "viadir"]


def test_cline_torn_and_odd_files(home, me):
    data = home / ".cline" / "data"
    path = write_sdk(data, "s1", [sdk_user('<user_input mode="act">hello there</user_input>')])
    src = Cline()
    (loc,) = src.discover(me, {})
    full = path.read_text()
    path.write_text(full[: len(full) // 2])
    units, cur = run(src, loc, None, me)
    assert units == [] and cur == {}  # not stored: retried on the next sync
    path.write_text(full)
    units, cur = run(src, loc, cur, me)
    assert texts(units) == ["hello there"]
    for body in ("[]", "null", '{"messages": 4}', '{"messages": [5, {"role": "user", "content": 7}]}', ""):
        path.write_text(body)
        assert list(src.read(loc, {}, me)[0]) == []
    classic = write_tasks(home / ".cline" / "data", "t", [say("task", "x", 1)])
    classic.write_text('{"not": "a list"}')
    (cloc,) = [lc for lc in src.discover(me, {}) if lc.path.name == "ui_messages.json"]
    assert list(src.read(cloc, {}, me)[0]) == []
    path.unlink()
    assert list(src.read(loc, {}, me)[0]) == []
