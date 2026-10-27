"""Public-only client over a loopback SSH tunnel. No corpus or automatic discovery.

The CLI's MANNERISM_REMOTE_URL setting routes context/lint/feedback/status here.
`remote mcp` provides a stdio MCP facade that signs writes on the user's host.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client
from mcp.server.fastmcp import FastMCP

from . import public


def url() -> str:
    value = os.environ.get("MANNERISM_REMOTE_URL", "http://127.0.0.1:3345/mcp")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}
            or parsed.username or parsed.password or parsed.fragment or parsed.query or parsed.path != "/mcp"):
        raise ValueError("remote URL must be a loopback HTTP /mcp endpoint; use an SSH tunnel")
    return value


async def call_async(tool: str, args: dict | None = None) -> dict:
    if tool not in {"voice_context", "voice_lint", "voice_feedback", "voice_status"}:
        raise ValueError("unsupported public tool")
    defaults = {"voice_context": {"register": "public", "task": "", "k": 6},
                "voice_lint": {"register": "public", "is_draft": True},
                "voice_feedback": {"register": "public", "rejected": False}, "voice_status": {}}
    args = {**defaults[tool], **(args or {})}
    if args.get("register", "public") != "public":
        raise ValueError("remote access supports only public writing")
    headers = {}
    if tool == "voice_feedback" or (tool == "voice_lint" and args.get("is_draft", True)):
        path = Path(os.environ.get("MANNERISM_TOKEN_FILE") or public.token_path())
        headers = public.signed_headers(public.read_token(path), tool, args)
    # No proxy environment or redirects: signed requests must stay on the loopback tunnel.
    def client_factory(**kwargs):
        return httpx.AsyncClient(**{**kwargs, "trust_env": False, "follow_redirects": False})
    async with streamablehttp_client(url(), headers=headers, httpx_client_factory=client_factory) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            res = await session.call_tool(tool, args)
            if res.isError:
                raise RuntimeError("public MCP call failed")
            if res.structuredContent is not None:
                return res.structuredContent
            for item in res.content:
                if item.type == "text":
                    return json.loads(item.text)
    raise RuntimeError("public MCP returned no result")


def call(tool: str, args: dict | None = None) -> dict:
    return asyncio.run(call_async(tool, args))


def serve_stdio() -> None:
    from .public_server import INSTRUCTIONS
    mcp = FastMCP("mannerism", instructions=INSTRUCTIONS, log_level="WARNING")

    async def safe(tool, args=None):
        try:
            return await call_async(tool, args)
        except Exception:
            return {"ok": False, "error": "public connection unavailable; check the tunnel and token"}

    @mcp.tool()
    async def voice_context(register: str = "public", task: str = "", k: int = 6) -> dict:
        """Load published-only style context before drafting. Never private registers."""
        return await safe("voice_context", {"register": register, "task": task, "k": k})

    @mcp.tool()
    async def voice_status() -> dict:
        """Check public snapshot readiness."""
        return await safe("voice_status")

    @mcp.tool()
    async def voice_lint(text: str, register: str = "public", is_draft: bool = True) -> dict:
        """Lint public text and remember agent drafts; is_draft=false for the user's own text."""
        return await safe("voice_lint", {"text": text, "register": register, "is_draft": is_draft})

    @mcp.tool()
    async def voice_feedback(draft: str, final: str, register: str = "public", rejected: bool = False) -> dict:
        """Record the user's public edit or rejection; never publish it."""
        return await safe("voice_feedback", {"draft": draft, "final": final, "register": register, "rejected": rejected})

    mcp.run(transport="stdio")
