"""The packaging manifests agree with the package and point at things that exist."""
from __future__ import annotations

import asyncio
import json
import re
import shlex
import tomllib
from pathlib import Path

import pytest

from mannerism import __version__, cli, connect

ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = [".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", ".mcp.json", "hooks/hooks.json",
             ".codex-plugin/plugin.json", "gemini-extension.json", "server.json"]
MCP_ARGS = ["mannerism", "mcp"]


def load(rel: str) -> dict:
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def parses(argv: list[str]) -> bool:
    try:
        cli.build_parser().parse_args(argv)
        return True
    except SystemExit:
        return False


def hook_commands() -> list[tuple[str, dict]]:
    out = []
    for event, matchers in load("hooks/hooks.json")["hooks"].items():
        for m in matchers:
            out += [(event, h) for h in m["hooks"]]
    return out


@pytest.mark.parametrize("rel", MANIFESTS)
def test_manifest_is_valid_json(rel):
    assert isinstance(load(rel), dict)


def test_versions_match_the_package():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    assert pyproject == __version__
    assert load(".claude-plugin/plugin.json")["version"] == __version__
    assert load(".codex-plugin/plugin.json")["version"] == __version__
    assert load("gemini-extension.json")["version"] == __version__
    server = load("server.json")
    assert server["version"] == __version__ and server["packages"][0]["version"] == __version__
    assert [p["version"] for p in load(".claude-plugin/marketplace.json")["plugins"]] == [__version__]


def test_names_and_identity():
    plugin, market = load(".claude-plugin/plugin.json"), load(".claude-plugin/marketplace.json")
    assert plugin["name"] == load(".codex-plugin/plugin.json")["name"] == load("gemini-extension.json")["name"] == "mannerism"
    assert market["name"] == "mannerism" and market["owner"]["name"] == "vicennial"
    assert market["plugins"][0]["name"] == plugin["name"] and market["plugins"][0]["source"] in (".", "./")
    assert plugin["author"] == {"name": "vicennial"}
    assert plugin["homepage"] == plugin["repository"] == "https://github.com/vicennial/mannerism"
    server = load("server.json")
    assert server["name"] == "io.github.vicennial/mannerism"
    pkg = server["packages"][0]
    assert (pkg["registryType"], pkg["identifier"], pkg["transport"]["type"]) == ("pypi", "mannerism", "stdio")
    assert pkg["packageArguments"] == [{"type": "positional", "value": "mcp"}]
    assert "$schema" in server and server["$schema"].startswith("https://static.modelcontextprotocol.io/schemas/")
    assert "mcp-name: io.github.vicennial/mannerism" in (ROOT / "README.md").read_text()


def test_mcp_server_entries_launch_the_cli_mcp_command():
    for rel in (".mcp.json", "gemini-extension.json"):
        server = load(rel)["mcpServers"]["mannerism"]
        assert server["command"] == "uvx" and server["args"] == MCP_ARGS, rel
    assert parses(MCP_ARGS[1:])
    assert load(".codex-plugin/plugin.json")["mcpServers"] == "./.mcp.json"


def test_hook_commands_run_real_subcommands():
    seen = {}
    for event, h in hook_commands():
        assert h["type"] == "command" and 0 < h["timeout"] <= 10
        inner = shlex.split(h["command"])
        assert inner[:2] == ["sh", "-c"], "hooks must be wrapped so a failure is never shown to the user"
        script = inner[2]
        assert script.endswith("|| true") and "2>/dev/null" in script
        m = re.fullmatch(r"uvx --offline (mannerism .+?) 2>/dev/null \|\| true", script)
        assert m, script
        argv = shlex.split(m.group(1))
        assert argv[0] == "mannerism" and parses(argv[1:]), argv
        seen[event] = argv[1:]
    assert seen == {"SessionStart": ["hook", "session-start"], "UserPromptSubmit": ["hook", "prompt"],
                    "Stop": ["hook", "stop"], "SessionEnd": ["hook", "session-end"]}


def test_referenced_files_exist():
    codex = load(".codex-plugin/plugin.json")
    assert (ROOT / codex["skills"]).is_dir() and (ROOT / codex["mcpServers"]).is_file()
    assert (ROOT / load("gemini-extension.json")["contextFileName"]).is_file()
    assert (ROOT / "skills/mannerism/SKILL.md").is_file()


def test_skill_is_bundled_as_wheel_package_data():
    """Package data for `uvx mannerism` / `pip install mannerism`: checked against a real `uv build` wheel during
    development (skills/mannerism/SKILL.md landed at mannerism/skills/mannerism/SKILL.md inside it)."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    force_include = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert force_include.get("skills/mannerism/SKILL.md") == "mannerism/skills/mannerism/SKILL.md"


def test_every_connect_harness_has_a_docs_url():
    """`mannerism connect`'s harness catalog: every entry is citable and well-formed."""
    assert len(connect.HARNESSES) >= 10
    for name, h in connect.HARNESSES.items():
        assert h.name == name and h.label
        assert h.docs_url.startswith("https://"), f"{name} has no docs URL"
        assert h.skill.kind in ("skill_dir", "rules_file", "none")
        if h.mcp is not None:
            assert h.mcp.format in ("json", "toml", "yaml") and h.mcp.key


def test_skill_frontmatter_and_cli_references():
    text = (ROOT / "skills/mannerism/SKILL.md").read_text()
    m = re.match(r"---\nname: (\S+)\ndescription: (.+)\n---\n", text)
    assert m and m.group(1) == "mannerism" and len(m.group(2)) < 1024
    from mannerism import mcp_server
    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    for name in set(re.findall(r"`(voice_\w+)", text)):
        assert name in tools, f"SKILL.md mentions unknown tool {name}"
