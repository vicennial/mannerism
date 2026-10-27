import json
import shutil
from pathlib import Path

from mannerism.adapters.pi import Pi

from .conftest import append_jsonl, run, write_jsonl

FIXTURES = Path(__file__).parent / "fixtures" / "pi"
SID = "01a16061-56b3-701b-93c5-012ccf1a439d"


def header(**kw):
    return {"type": "session", "version": 3, "id": SID, "timestamp": "2026-03-01T09:59:00.000Z", "cwd": "/work/demo", **kw}


def msg(role, content, id_="m1", parent=None, ts="2026-03-01T10:00:00.000Z", **kw):
    return {"type": "message", "id": id_, "parentId": parent, "timestamp": ts,
            "message": {"role": role, "content": content, "timestamp": 1772359200000, **kw}}


def user(text, id_="m1", **kw):
    return msg("user", [{"type": "text", "text": text}], id_, **kw)


def assistant(text, id_="a1"):
    return msg("assistant", [{"type": "text", "text": text}], id_)


def sessions(home):
    return home / ".pi" / "agent" / "sessions" / "--work-demo--"


def path(home):
    return sessions(home) / f"2026-03-01T09-59-00-000Z_{SID}.jsonl"


def test_real_run_and_fork_dedupe(home, me):
    """Written by pi 1.3.0 (`pi -p`): a /review prompt template, a /skill: command, an unknown /command, then a --fork."""
    for name in ("real-1.3.0-main.jsonl", "real-1.3.0-fork.jsonl"):
        sessions(home).mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / name, sessions(home) / name.replace("real-1.3.0-main", "2026-10-21T21-32-34-355Z_a").replace("real-1.3.0-fork", "2026-10-21T21-32-56-121Z_b"))
    src = Pi()
    locs = src.discover(me, {})
    assert len(locs) == 2
    main, fork = (run(src, lc, None, me)[0] for lc in locs)
    assert [u.text for u in main] == [
        "please rename greet to welcome in @hello.py, keep the string",
        "and add a docstring, then mail [email] when done",
        # a prompt template is expanded before it is stored: it cannot be told from typed text (known limit)
        "Please review the file hello.py and list any problems you can see.",
        "hello.py please",  # the skill body is gone, the typed argument stays; "/nonexistent thing" is dropped
    ]
    assert all((u.kind, u.audience, u.register) == ("prompt", "agent", "agent") for u in main)
    assert main[0].context == () and "nothing to change" in main[1].context[0].text
    assert main[0].ts.isoformat() == "2026-10-21T21:32:34.427000+00:00" and main[0].meta["cwd_hash"]
    # the fork copied the first four entries: same unit ids, so the engine stores them once
    assert [u.unit_id for u in fork[:4]] == [u.unit_id for u in main]
    assert fork[4].text == "forked continuation prompt here"
    assert fork[4].thread_id != main[0].thread_id and main[0].thread_id == main[3].thread_id


def test_filters_roles_and_entry_types(home, me):
    skill = '<skill name="tidy" location="/x/SKILL.md">\nReferences are relative to /x.\n\nTidy it.\n</skill>'
    write_jsonl(
        path(home),
        [
            header(),
            {"type": "model_change", "id": "x1", "parentId": None, "timestamp": "2026-03-01T10:00:00Z",
             "provider": "p", "modelId": "m"},
            msg("system", "", "s1"),
            assistant("Which file? Mail bot@example.org", "a1"),
            user("rename foo to bar, mail jo.dev@example.org", "u1", parent="a1"),
            msg("custom", "injected by an extension", "c1", customType="x", display=True),
            {"type": "custom_message", "id": "c2", "parentId": "c1", "timestamp": "2026-03-01T10:00:00Z",
             "customType": "x", "content": "also injected", "display": True},
            msg("bashExecution", "", "b1", command="ls", output="a", exitCode=0),
            msg("toolResult", [{"type": "text", "text": "ok"}], "t1"),
            user(skill, "k1"),
            user(skill + "\n\nand run the tests", "k2"),
            user("/nonexistent thing", "k3"),
            user("/etc/hosts has a wrong entry, please fix", "k4"),
            user("<task-notification>x</task-notification>", "r1"),
            user("https://example.org/page", "r2"),
            msg("user", "a plain string prompt", "k5"),
            msg("user", [{"type": "image", "data": "AA", "mimeType": "image/png"},
                         {"type": "text", "text": "what is in this picture?"}], "k6"),
            {"type": "message", "id": "k7", "parentId": None,
             "message": {"role": "user", "content": "timestamp only on the message", "timestamp": 1772359260000}},
            {"type": "message", "message": {"role": "user", "content": "an old v1 line", "timestamp": 1772359320000}},
            "{not json",
        ],
    )
    src = Pi()
    units, cur = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == [
        "rename foo to bar, mail [email]",
        "and run the tests",
        "/etc/hosts has a wrong entry, please fix",
        "a plain string prompt",
        "what is in this picture?",
        "timestamp only on the message",
        "an old v1 line",
    ]
    assert "[email]" in units[0].context[0].text
    assert units[0].source_ref == "u1@2026-03-01T10:00:00.000Z"
    assert units[5].ts.isoformat() == "2026-03-01T10:01:00+00:00"
    assert cur["skipped"] == 1 and cur["sid"] == SID and SID not in (units[0].thread_id or "")


def test_branches_in_one_file_are_all_read(home, me):
    """/tree: a second child of the same parent is another thing the user typed."""
    write_jsonl(path(home), [header(), user("first idea", "u1"), assistant("ok", "a1"),
                             user("take path A", "u2", parent="a1", ts="2026-03-01T10:01:00Z"),
                             user("take path B instead", "u3", parent="a1", ts="2026-03-01T10:02:00Z")])
    src = Pi()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["first idea", "take path A", "take path B instead"]


def test_incremental_header_state_and_truncation(home, me):
    p = path(home)
    src = Pi()
    write_jsonl(p, [header(), user("first prompt here", "u1"), assistant("Shall I go on?", "a1")])
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert len(units) == 1
    units, cur2 = run(src, loc, cur, me)
    assert units == [] and cur2["offset"] == cur["offset"]
    append_jsonl(p, [user("second prompt here", "u2", parent="a1")], newline_end=False)
    units, cur3 = run(src, loc, cur2, me)
    assert units == [] and cur3["offset"] == cur2["offset"]
    with open(p, "a") as f:
        f.write("\n")
    units, cur4 = run(src, loc, cur3, me)
    assert [u.source_ref.split("@")[0] for u in units] == ["u2"]
    assert units[0].context[0].text == "Shall I go on?" and units[0].meta["cwd_hash"]  # carried in the cursor
    assert json.dumps(cur2).count("Shall I go on") == 1  # only the masked reply, never a raw line
    write_jsonl(p, [header(id="other"), user("brand new content", "n1")])
    units, _ = run(src, loc, cur4, me)
    assert [u.text for u in units] == ["brand new content"]


def test_dirs_env_overrides_and_settings(home, me, monkeypatch, tmp_path):
    write_jsonl(path(home), [header(), user("in the default dir")])
    src = Pi()
    assert len(src.discover(me, {})) == 1
    alt = tmp_path / "alt-agent"
    write_jsonl(alt / "sessions" / "--x--" / "b.jsonl", [header(id="b"), user("from the env dir")])
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(alt))
    assert sorted(lc.path.name for lc in src.discover(me, {})) == ["2026-03-01T09-59-00-000Z_" + SID + ".jsonl", "b.jsonl"]
    custom = tmp_path / "flat"
    write_jsonl(custom / "c.jsonl", [header(id="c"), user("from a flat session dir")])
    (alt / "settings.json").write_text(json.dumps({"sessionDir": str(custom)}))
    assert "c.jsonl" in {lc.path.name for lc in src.discover(me, {})}
    (alt / "settings.json").write_text("{broken")
    monkeypatch.setenv("PI_CODING_AGENT_SESSION_DIR", str(custom))
    assert "c.jsonl" in {lc.path.name for lc in src.discover(me, {})}
    monkeypatch.delenv("PI_CODING_AGENT_SESSION_DIR")
    other = tmp_path / "other-agent"
    write_jsonl(other / "sessions" / "--y--" / "d.jsonl", [header(id="d"), user("from the config")])
    assert "d.jsonl" in {lc.path.name for lc in src.discover(me, {"dirs": [str(other)]})}
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/nonexistent/nowhere")
    assert [lc.path.name for lc in src.discover(me, {})] == ["2026-03-01T09-59-00-000Z_" + SID + ".jsonl"]
