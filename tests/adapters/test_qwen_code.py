import json
import shutil
from pathlib import Path

from mannerism.adapters.qwen_code import QwenCode

from .conftest import append_jsonl, run, write_jsonl

FIXTURES = Path(__file__).parent / "fixtures" / "qwen_code"
SID = "7d6c1e5a-3b1f-4c2a-9e55-0c4f6a1b2d01"


def rec(type_, *, text=None, uuid="u1", parent=None, ts="2026-03-01T10:00:00.000Z", **kw):
    d = {
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SID,
        "timestamp": ts,
        "type": type_,
        "cwd": "/work/demo",
        "version": "0.25.3",
    }
    if type_ == "user":
        d["provenance"] = "real_user"
    if text is not None:
        d["message"] = {"role": "model" if type_ == "assistant" else "user", "parts": [{"text": text}]}
    d.update(kw)
    return d


def chats(home, slug="-work-demo"):
    return home / ".qwen" / "projects" / slug / "chats"


def install(home, fixture, name=None):
    dst = chats(home) / (name or fixture)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / fixture, dst)
    return dst


def test_real_interactive_run_keeps_typed_prompts_only(home, me):
    """Written by Qwen Code 0.25.3 (TUI): an @-mention, /about, a custom /tidy command and a plain prompt."""
    p = install(home, "real-0.25.3-interactive.jsonl", f"{SID}.jsonl")
    src = QwenCode()
    locs = src.discover(me, {})
    assert [lc.path for lc in locs] == [p]
    units, cur = run(src, locs[0], None, me)
    assert [u.text for u in units] == [
        "please rename greet to welcome in @hello.py",  # the file contents Qwen appended are gone
        "and add a docstring, mail [email] when done",
    ]
    assert all((u.kind, u.audience, u.register) == ("prompt", "agent", "agent") for u in units)
    assert units[0].context == () and units[1].context[0].who == "agent"
    assert "nothing to change" in units[1].context[0].text
    assert units[0].ts.isoformat().startswith("2026-10-21T21:28:36")
    assert "/home" not in str(units[0].meta) and units[0].meta["cwd_hash"]
    # only /tidy (its invocation and result records) sent anything to the model; /about printed a panel
    assert cur["offset"] == p.stat().st_size and len(cur["slash"]) == 2


def test_real_headless_run(home, me):
    """Written by `qwen "<prompt>"` and `--continue`: no slash records, still one session file."""
    install(home, "real-0.25.3-headless.jsonl", f"{SID}.jsonl")
    src = QwenCode()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == [
        "please rename greet to welcome in @hello.py, keep the string",
        "and add a docstring, then mail [email] when done",  # Qwen split the address; it is rejoined, then masked
        "thanks, now explain @hello.py in one line",
    ]


def test_filters_by_subtype_provenance_and_content(home, me):
    p = chats(home) / f"{SID}.jsonl"
    write_jsonl(
        p,
        [
            rec("system", uuid="s0", subtype="session_source", provenance="system", systemPayload={"sourceType": "cli"}),
            rec("assistant", text="Which file? Mail bot@example.org", uuid="a1", parent="s0"),
            rec("assistant", uuid="a2", message={"role": "model", "parts": [{"text": "hidden", "thought": True}]}),
            rec("user", text="rename foo to bar", uuid="k1", parent="a1"),
            rec("user", text="side task", uuid="k2", isSidechain=True),
            rec("user", text="every 5 minutes", uuid="k3", subtype="cron", provenance="system"),
            rec("user", text="job finished", uuid="k4", subtype="notification", provenance="system"),
            rec("user", text="goal text", uuid="k5", subtype="goal_runtime", provenance="goal_runtime"),
            rec("user", text="hi from agent", uuid="k6", subtype="agent_message", provenance="external_agent"),
            rec("user", text="not from a person", uuid="k7", provenance="tool_result"),
            rec("tool_result", text="ok", uuid="k8"),
            rec("user", text="<task-notification>x</task-notification>", uuid="k9"),
            rec("user", text="<system-reminder>only a reminder</system-reminder>", uuid="k10"),
            rec("user", text="https://example.org/page", uuid="k11"),
            rec("system", uuid="k12", subtype="slash_command", provenance="system",
                systemPayload={"phase": "invocation", "rawCommand": "/clear"}),
            rec("system", uuid="at13", subtype="at_command", provenance="system",
                systemPayload={"filesRead": ["a.py"], "status": "success",
                               "userText": "check @a.py and @mcp:doc, then fix it"}),
            rec(
                "user",
                uuid="k13",
                parent="at13",
                ts="2026-03-01T10:05:00Z",
                message={
                    "role": "user",
                    "parts": [
                        {"text": "check @a.py and @mcp:doc, then fix it"},
                        {"text": "<system-reminder>skip me</system-reminder>"},
                        {"text": "\n--- Content from referenced files ---"},
                        {"text": "\nContent from a.py:\n"},
                        {"text": "print('x')\n"},
                        {"text": "\n--- End of content ---"},
                        {"inlineData": {"mimeType": "image/png", "data": "AA"}},
                    ],
                },
            ),
            rec("user", text="typed while the model ran", uuid="k14", subtype="mid_turn_user_message",
                provenance="real_user", ts="2026-03-01T10:06:00Z"),
            rec("user", text="expanded text", uuid="k15", systemPayload={"displayText": "what was typed", "hookContext": ""},
                ts="2026-03-01T10:07:00Z"),
            "{not json",
        ],
    )
    src = QwenCode()
    units, cur = run(src, src.discover(me, {})[0], None, me)
    assert [u.source_ref for u in units] == ["k1", "k13", "k14", "k15"]
    assert units[0].text == "rename foo to bar" and "[email]" in units[0].context[0].text
    assert "hidden" not in units[0].context[0].text
    assert units[1].text == "check @a.py and @mcp:doc, then fix it"
    assert units[3].text == "what was typed"
    assert cur["skipped"] == 1 and SID not in (units[0].thread_id or "")


def test_custom_slash_command_expansion_dropped_across_reads(home, me):
    """The prompt a custom command expands to is a user record whose parent is the slash_command record."""
    p = chats(home) / f"{SID}.jsonl"
    slash = rec("system", uuid="sc1", subtype="slash_command", provenance="system",
                systemPayload={"phase": "result", "rawCommand": "/tidy x.py", "sentToModel": True})
    write_jsonl(p, [slash])
    src = QwenCode()
    loc = src.discover(me, {})[0]
    _, cur = run(src, loc, None, me)
    assert cur["slash"] == ["sc1"]
    append_jsonl(p, [rec("user", text="Please tidy the file x.py", uuid="t1", parent="sc1"),
                     rec("user", text="and then run the tests", uuid="t2", parent="t1")])
    units, _ = run(src, loc, cur, me)
    assert [u.source_ref for u in units] == ["t2"]


def test_non_interactive_session_sources_are_skipped(home, me):
    p = chats(home) / f"{SID}.jsonl"
    write_jsonl(p, [rec("system", uuid="s0", subtype="session_source", provenance="system",
                        systemPayload={"sourceType": "channel", "sourceId": "tg"}),
                    rec("user", text="message relayed from a chat bot", uuid="k1")])
    src = QwenCode()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert units == [] and cur["skip"]
    append_jsonl(p, [rec("user", text="and one more relayed message", uuid="k2")])
    units, cur = run(src, loc, cur, me)
    assert units == [] and cur["skip"]


def test_incremental_partial_line_and_truncation(home, me):
    p = chats(home) / f"{SID}.jsonl"
    src = QwenCode()
    write_jsonl(p, [rec("user", text="first prompt here", uuid="a")])
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert len(units) == 1
    units, cur2 = run(src, loc, cur, me)
    assert units == [] and cur2["offset"] == cur["offset"]
    append_jsonl(p, [rec("user", text="second prompt here", uuid="b")], newline_end=False)
    units, cur3 = run(src, loc, cur2, me)
    assert units == [] and cur3["offset"] == cur2["offset"]
    with open(p, "a") as f:
        f.write("\n")
    units, cur4 = run(src, loc, cur3, me)
    assert [u.source_ref for u in units] == ["b"]
    write_jsonl(p, [rec("user", text="brand new content", uuid="c")])
    units, _ = run(src, loc, cur4, me)
    assert [u.source_ref for u in units] == ["c"]


def test_agent_reply_context_survives_between_reads(home, me):
    p = chats(home) / f"{SID}.jsonl"
    write_jsonl(p, [rec("assistant", text="Shall I start with the parser?", uuid="a1")])
    src = QwenCode()
    loc = src.discover(me, {})[0]
    _, cur = run(src, loc, None, me)
    append_jsonl(p, [rec("user", text="yes, the parser first", uuid="u2")])
    units, _ = run(src, loc, cur, me)
    assert units[0].context[0].text == "Shall I start with the parser?"
    assert json.dumps(cur).count("parser") == 1  # only the masked reply, never a raw transcript line


def test_dirs_env_overrides_and_noise_files(home, me, monkeypatch, tmp_path):
    write_jsonl(chats(home) / "a.jsonl", [rec("user", text="in the default dir")])
    (chats(home) / "a.runtime.json").write_text("{}")
    (chats(home) / "a.jsonl.lock").write_text("{}")
    src = QwenCode()
    assert [lc.path.name for lc in src.discover(me, {})] == ["a.jsonl"]
    alt = tmp_path / "alt"
    write_jsonl(alt / "projects" / "-x" / "chats" / "b.jsonl", [rec("user", text="in the home dir")])
    monkeypatch.setenv("QWEN_HOME", str(alt))
    assert sorted(lc.path.name for lc in src.discover(me, {})) == ["a.jsonl", "b.jsonl"]
    monkeypatch.delenv("QWEN_HOME")
    monkeypatch.setenv("QWEN_RUNTIME_DIR", str(alt))
    assert sorted(lc.path.name for lc in src.discover(me, {})) == ["a.jsonl", "b.jsonl"]
    monkeypatch.delenv("QWEN_RUNTIME_DIR")
    other = tmp_path / "other"
    write_jsonl(other / "projects" / "-y" / "chats" / "c.jsonl", [rec("user", text="from the config")])
    assert sorted(lc.path.name for lc in src.discover(me, {"dirs": [str(other)]})) == ["a.jsonl", "c.jsonl"]
    monkeypatch.setenv("QWEN_HOME", "/nonexistent/nowhere")
    assert [lc.path.name for lc in src.discover(me, {})] == ["a.jsonl"]


def test_split_email_is_rejoined_before_masking(home, me):
    """P1-3: Qwen's @-parser stores jo.dev@example.org as 'jo.dev @example.org'; masking must still see an email."""
    p = chats(home) / f"{SID}.jsonl"
    write_jsonl(p, [rec("user", text="mail jo.dev @example.org and ana-b @mail.example.co.uk today", uuid="k1"),
                    rec("user", text="the handle @octocat and a note @ home", uuid="k2")])
    src = QwenCode()
    units, cur = run(src, src.discover(me, {})[0], None, me)
    assert [u.text for u in units] == ["mail [email] and [email] today", "the handle @octocat and a note @ home"]
    assert "example" not in repr(units[0]) and "example" not in json.dumps(cur)


def test_at_command_user_text_is_preferred_and_survives_between_reads(home, me):
    """The at_command record holds the query as typed; the message holds Qwen's re-spelling of it."""
    p = chats(home) / f"{SID}.jsonl"
    typed = "write to jo.dev@example.org about @notes.txt please"
    at = rec("system", uuid="at1", subtype="at_command", provenance="system",
             systemPayload={"filesRead": ["notes.txt"], "status": "success", "userText": typed})
    write_jsonl(p, [at])
    src = QwenCode()
    loc = src.discover(me, {})[0]
    _, cur = run(src, loc, None, me)
    assert "example" not in json.dumps(cur) and "[email]" in json.dumps(cur)  # masked before it sits in the cursor
    append_jsonl(p, [rec("user", uuid="k1", parent="at1",
                         message={"role": "user", "parts": [{"text": "write to jo.dev @example.org about @notes.txt please"},
                                                            {"text": "\n--- Content from referenced files ---"},
                                                            {"text": "body"}, {"text": "\n--- End of content ---"}]})])
    units, _ = run(src, loc, cur, me)
    assert [u.text for u in units] == ["write to [email] about @notes.txt please"]


def test_builtin_slash_command_does_not_swallow_the_next_typed_prompt(home, me):
    """P2-7: /about is printed, not sent; the prompt typed after it has the /about result record as its parent."""
    p = chats(home) / f"{SID}.jsonl"
    write_jsonl(p, [
        rec("system", uuid="s1", subtype="slash_command", provenance="system",
            systemPayload={"phase": "invocation", "rawCommand": "/about", "sentToModel": False}),
        rec("system", uuid="s2", parent="s1", subtype="slash_command", provenance="system",
            systemPayload={"phase": "result", "rawCommand": "/about", "outputHistoryItems": []}),
        rec("user", text="the prompt I typed right after /about", uuid="k1", parent="s2"),
        rec("system", uuid="s3", subtype="slash_command", provenance="system",
            systemPayload={"phase": "invocation", "rawCommand": "/tidy x.py", "sentToModel": True}),
        rec("system", uuid="s4", parent="s3", subtype="slash_command", provenance="system",
            systemPayload={"phase": "result", "rawCommand": "/tidy x.py", "outputHistoryItems": []}),
        rec("user", text="Please tidy x.py (the template)", uuid="k2", parent="s4"),
        rec("user", text="and now the tests", uuid="k3", parent="k2"),
    ])
    src = QwenCode()
    units, cur = run(src, src.discover(me, {})[0], None, me)
    assert [u.source_ref for u in units] == ["k1", "k3"]
    assert cur["slash"] == ["s3", "s4"]
