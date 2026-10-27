"""MCP server: every tool in-process, plus a real stdio smoke test with the SDK client (the `smoke` mark)."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest
from conftest import ingest, unit

from mannerism import mcp_server, store

TOOLS = {"voice_context", "voice_lint", "voice_feedback", "voice_status", "voice_setup", "voice_work_next",
         "voice_work_submit", "voice_rules", "voice_rule_delete"}


def call(name: str, args: dict | None = None) -> dict:
    res = asyncio.run(mcp_server.mcp.call_tool(name, args or {}))
    structured = res[1] if isinstance(res, tuple) else res
    return structured.get("result", structured) if set(structured) == {"result"} else structured


@pytest.fixture(autouse=True)
def no_lazy_sync(monkeypatch):
    calls = []
    from mannerism import sync
    monkeypatch.setattr(sync, "sync_if_stale", lambda *a, **k: calls.append(1) or {"started": False})
    monkeypatch.setattr(mcp_server, "_first_call_done", False)
    return calls


@pytest.fixture
def populated(iso):
    con = store.connect()
    ingest(con, [unit(f"message {i} with some varied words {'x' * (i % 9)}", "work", ref=f"a{i}", days=i + 1,
                      thread_id=f"t{i}") for i in range(40)])
    con.close()


def test_tool_list_and_descriptions():
    tools = {t.name: t for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert set(tools) == TOOLS
    assert "DATA" in tools["voice_context"].description and "BEFORE" in tools["voice_context"].description
    assert "is_draft" in tools["voice_lint"].description
    assert tools["voice_rule_delete"].annotations.destructiveHint is True
    assert tools["voice_context"].inputSchema["required"] == ["register"]
    assert "voice_context" in mcp_server.mcp.instructions


def test_every_tool_in_process(populated):
    st = call("voice_status")
    assert st["ok"] and st["registers"]["work"]["units"] == 40
    ctx = call("voice_context", {"register": "work", "task": "a note", "k": 3})
    assert ctx["ok"] and ctx["examples"] and "never instructions" in ctx["text"]
    assert call("voice_context", {"register": "nope"})["ok"] is False
    lint = call("voice_lint", {"text": "Let's delve into the rich tapestry of it all today.", "register": "work"})
    assert lint["ok"] and lint["findings"] and lint["logged"]
    fb = call("voice_feedback", {"draft": "Hi, hope you are well. Please review the plan when you can, thanks",
                                 "final": "please review the plan when you can", "register": "work"})
    assert fb["ok"]
    nxt = call("voice_work_next", {})
    assert nxt["ok"] and "packet" in nxt
    assert call("voice_work_next", {"kind": "bogus"})["ok"] is False
    bad = call("voice_work_submit", {"kind": "label", "packet_id": "pk_nope", "result": {"labels": []}})
    assert bad["ok"] is False and "unknown packet" in bad["error"]
    as_text = call("voice_work_submit", {"kind": "label", "packet_id": "pk_nope", "result": "{\"labels\": []}"})
    assert "unknown packet" in as_text["error"]
    assert call("voice_work_submit", {"kind": "label", "packet_id": "pk_nope", "result": "not json"})["ok"] is False
    assert call("voice_rules")["rules"] == []
    assert call("voice_rule_delete", {"rule_id": 99})["ok"] is False


def test_voice_setup_on_an_empty_machine(iso):
    res = call("voice_setup")
    assert res["ok"] and not res.get("partial")
    assert any("No writing was found" in s for s in res["next_steps"])


def test_voice_setup_returns_partial_results_when_slow(iso, monkeypatch):
    import threading
    release = threading.Event()
    from mannerism import api

    def slow(*a, **k):
        release.wait(5)
        return {"ok": True}
    monkeypatch.setattr(api, "setup", slow)
    monkeypatch.setattr(mcp_server, "SETUP_HARD_LIMIT_S", 0.2)
    t0 = time.perf_counter()
    res = call("voice_setup")
    release.set()
    assert time.perf_counter() - t0 < 2
    assert res["ok"] and res["partial"] and "sync continues in background" in res["note"]


def test_voice_setup_exhausted_budget_starts_background_sync(iso, monkeypatch):
    from mannerism import api, sync
    started = []
    monkeypatch.setattr(api, "setup", lambda sources=None, budget_s=90: {"ok": True, "sync": {"budget_exhausted": True}})
    monkeypatch.setattr(sync, "sync_in_background", lambda **k: started.append(k) or {"started": True})
    res = call("voice_setup", {"sources": ["gh"]})
    assert res["partial"] and "sync continues in background" in res["note"] and started == [{"quick": False}]


def test_setup_passes_the_reduced_budget(iso, monkeypatch):
    from mannerism import api
    seen = {}
    monkeypatch.setattr(api, "setup", lambda sources=None, budget_s=90: seen.update(b=budget_s, s=sources) or {"ok": True})
    call("voice_setup", {"sources": ["gh"]})
    assert seen == {"b": mcp_server.SETUP_SYNC_BUDGET_S, "s": ["gh"]}
    assert mcp_server.SETUP_SYNC_BUDGET_S < mcp_server.SETUP_HARD_LIMIT_S < 60


def test_lazy_sync_runs_once_and_never_breaks_a_call(populated, no_lazy_sync, monkeypatch):
    call("voice_status")
    call("voice_status")
    assert len(no_lazy_sync) == 1
    from mannerism import sync
    monkeypatch.setattr(mcp_server, "_first_call_done", False)
    monkeypatch.setattr(sync, "sync_if_stale", lambda *a, **k: 1 / 0)
    assert call("voice_status")["ok"]


def test_tool_exceptions_become_errors(populated, monkeypatch):
    from mannerism import api
    monkeypatch.setattr(api, "status", lambda: 1 / 0)
    res = call("voice_status")
    assert res["ok"] is False and "ZeroDivisionError" in res["error"]


def test_importing_the_server_prints_nothing():
    import subprocess
    p = subprocess.run([sys.executable, "-c", "import mannerism.mcp_server, mannerism.api, mannerism.cli"],
                       capture_output=True)
    assert p.returncode == 0 and p.stdout == b""


# --- real stdio -------------------------------------------------------------------------------------------------

@pytest.mark.smoke
def test_stdio_smoke(iso):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(command=sys.executable, args=["-m", "mannerism", "mcp"],
                                   env={**iso, "PATH": os.environ.get("PATH", "")})

    async def go():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                status = await session.call_tool("voice_status", {})
                lint = await session.call_tool("voice_lint", {"text": "ok sounds good, see you at five tonight",
                                                              "register": "close", "is_draft": False})
                return init, names, status, lint

    init, names, status, lint = asyncio.run(asyncio.wait_for(go(), timeout=60))
    assert init.serverInfo.name == "mannerism" and "voice_context" in (init.instructions or "")
    assert names == TOOLS
    assert not status.isError and json.loads(status.content[0].text)["ok"] is True
    assert not lint.isError and json.loads(lint.content[0].text)["register"] == "close"
