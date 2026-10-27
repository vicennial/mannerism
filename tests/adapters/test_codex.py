import json

from mannerism.adapters.codex import Codex

from .conftest import append_jsonl, run, write_jsonl

SID = "019a0000-aaaa-bbbb-cccc-000000000001"


def meta():
    return {
        "timestamp": "2026-03-02T08:00:00.000Z",
        "type": "session_meta",
        "payload": {"id": SID, "cwd": "/work/demo"},
    }


def resp(text, ts, role="user", kind="input_text"):
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": [{"type": kind, "text": text}]},
    }


def event(text, ts):
    return {"timestamp": ts, "type": "event_msg", "payload": {"type": "user_message", "message": text}}


def path(home):
    return home / ".codex" / "sessions" / "2026" / "03" / "02" / f"rollout-2026-03-02T08-00-00-{SID}.jsonl"


def test_codex_filters_injected_and_dedupes_dual_formats(home, me):
    p = path(home)
    write_jsonl(
        p,
        [
            meta(),
            resp("<user_instructions>be nice</user_instructions>", "2026-03-02T08:00:01Z"),
            resp("<environment_context>cwd</environment_context>", "2026-03-02T08:00:01Z"),
            resp("# AGENTS.md instructions for /work/demo\n...", "2026-03-02T08:00:01Z"),
            resp("<INSTRUCTIONS>x</INSTRUCTIONS>", "2026-03-02T08:00:01Z"),
            resp("<permissions instructions>x</permissions instructions>", "2026-03-02T08:00:01Z"),
            resp("<turn_aborted>x</turn_aborted>", "2026-03-02T08:00:01Z"),
            resp("hello <user_instructions>embedded</user_instructions>", "2026-03-02T08:00:01Z"),
            resp(
                "Done. Anything else, jo.dev@example.org?", "2026-03-02T08:00:02Z", role="assistant", kind="output_text"
            ),
            # same prompt in both variants, within 2 s: one unit
            resp("add a retry to the fetch helper", "2026-03-02T08:01:00.100Z"),
            event("add a retry to the fetch helper", "2026-03-02T08:01:00.900Z"),
            # the other order: event first
            event("now write the tests", "2026-03-02T08:05:00Z"),
            resp("now write the tests", "2026-03-02T08:05:01Z"),
            # the same text much later is a separate prompt
            resp("now write the tests", "2026-03-02T08:30:00Z"),
            resp("a tool says hi", "2026-03-02T08:31:00Z", role="developer"),
            "{broken",
        ],
    )
    src = Codex()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert [u.text for u in units] == ["add a retry to the fetch helper", "now write the tests", "now write the tests"]
    assert units[1].ts.isoformat().startswith("2026-03-02T08:05:00")  # event_msg preferred
    assert (units[0].kind, units[0].audience, units[0].register) == ("prompt", "agent", "agent")
    assert units[0].context[0].who == "agent" and "[email]" in units[0].context[0].text
    assert units[0].meta["cwd_hash"] and units[0].thread_id
    assert cur["skipped"] == 1


def test_codex_older_variant_without_envelope_and_incremental(home, me):
    p = path(home)
    write_jsonl(p, [meta(), event("first thing to do", "2026-03-02T09:00:00Z")])
    src = Codex()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    assert len(units) == 1
    units, cur2 = run(src, loc, cur, me)
    assert units == []
    append_jsonl(
        p,
        [
            {
                "type": "message",
                "role": "user",
                "timestamp": "2026-03-02T09:10:00Z",
                "content": [{"type": "input_text", "text": "older style prompt"}],
            }
        ],
        newline_end=False,
    )
    units, cur3 = run(src, loc, cur2, me)
    assert units == [] and cur3["offset"] == cur2["offset"]
    with open(p, "a") as f:
        f.write("\n")
    units, cur4 = run(src, loc, cur3, me)
    assert [u.text for u in units] == ["older style prompt"]
    assert units[0].thread_id == hash_of_sid()
    write_jsonl(p, [meta(), event("after truncation", "2026-03-02T10:00:00Z")])
    units, _ = run(src, loc, cur4, me)
    assert [u.text for u in units] == ["after truncation"]


def hash_of_sid():
    from mannerism.core import hash_ref

    return hash_ref(SID)


def test_codex_home_override(home, me, monkeypatch, tmp_path):
    alt = tmp_path / "codexalt"
    monkeypatch.setenv("CODEX_HOME", str(alt))
    write_jsonl(alt / "sessions" / "2026" / "03" / "02" / "rollout-x.jsonl", [meta()])
    assert len(Codex().discover(me, {})) == 1
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "missing"))
    assert Codex().discover(me, {}) == []


def _session(home, name, meta_payload, texts=("do the task",)):
    p = home / ".codex" / "sessions" / "2026" / "03" / "03" / f"rollout-{name}.jsonl"
    rows = [{"timestamp": "2026-03-03T08:00:00Z", "type": "session_meta", "payload": meta_payload}]
    rows += [event(t, f"2026-03-03T08:0{i + 1}:00Z") for i, t in enumerate(texts)]
    write_jsonl(p, rows)
    return p


def test_codex_skips_non_interactive_sessions(home, me):
    cases = {
        "cli": ({"id": "a", "source": "cli", "originator": "codex-tui"}, True),
        "vscode": ({"id": "b", "source": "vscode", "originator": "custom_host"}, True),
        "exec": ({"id": "c", "source": "exec"}, False),
        "subagent": ({"id": "d", "source": {"subagent": {"name": "guardian"}}}, False),
        "originator": ({"id": "e", "source": "cli", "originator": "codex_exec"}, False),
        "parent": ({"id": "f", "source": "cli", "parent_thread_id": "zzz"}, False),
        "role": ({"id": "g", "source": "cli", "agent_role": "worker"}, False),
        "nosource": ({"id": "h"}, True),
    }
    src = Codex()
    paths = {k: _session(home, k, payload) for k, (payload, _) in cases.items()}
    locs = {lc.path: lc for lc in src.discover(me, {})}
    for k, (_, kept) in cases.items():
        units, cur = run(src, locs[paths[k]], None, me)
        assert bool(units) is kept, k
        if not kept:
            assert cur.get("skip") is True
            # decided once: the next sync returns immediately with the same cursor
            again, cur2 = run(src, locs[paths[k]], cur, me)
            assert again == [] and cur2 == cur


def test_codex_skip_decision_without_peek_and_no_meta(home, me):
    p = home / ".codex" / "sessions" / "2026" / "03" / "04" / "rollout-nometa.jsonl"
    write_jsonl(p, [event("a prompt without any session_meta", "2026-03-04T08:00:00Z")])
    src = Codex()
    units, _ = run(src, src.discover(me, {})[0], None, me)
    assert len(units) == 1


def test_codex_drops_relay_wrappers(home, me):
    p = _session(
        home,
        "relay",
        {"id": "r", "source": "cli"},
        texts=[
            "<task-notification>x</task-notification>",
            "<agent-message a>x",
            "<cross-session-message>x",
            "[SYSTEM NOTIFICATION] done",
            "Subagent hand-back: ok",
            "a real prompt",
        ],
    )
    src = Codex()
    loc = next(lc for lc in src.discover(me, {}) if lc.path == p)
    units, _ = run(src, loc, None, me)
    assert [u.text for u in units] == ["a real prompt"]


def test_wrapper_tags_and_excluded_originators():
    from mannerism.adapters._util import is_relay
    from mannerism.adapters.codex import headless_session

    assert is_relay("<codex_internal_context>\nstuff</codex_internal_context>")
    assert is_relay("  <subagent_notification id='1'>done")
    assert not is_relay("<b>bold</b> is what I meant")
    assert not is_relay("can you check <this> later")
    assert headless_session({"source": "cli", "originator": "lane-host"}, frozenset({"lane-host"}))
    assert not headless_session({"source": "cli", "originator": "codex-tui"}, frozenset({"lane-host"}))


def test_units_carry_account_project_kind_and_gap_and_keep_them_across_reads(home, me):
    p = path(home)
    m = meta()
    m["payload"]["cwd"] = str(home / "work" / "demo")
    m["payload"]["source"] = "vscode"
    write_jsonl(p, [m, resp("add a retry to the fetch helper please", "2026-03-02T08:01:00Z")])
    src = Codex()
    loc = src.discover(me, {})[0]
    units, cur = run(src, loc, None, me)
    u = units[0]
    assert u.account == "~/.codex" and u.project == "demo" and u.project_path == "~/work/demo"
    assert u.session_kind == "ide" and u.session == u.thread_id and u.gap_s == 60.0
    assert cur["project"] == "demo" and "/home" not in json.dumps(cur) and str(home) not in json.dumps(cur)
    append_jsonl(p, [resp("now add a test for the retry in the helper", "2026-03-02T08:05:00Z")])
    units, _ = run(src, loc, cur, me)
    assert units[0].project == "demo" and units[0].session_kind == "ide"
