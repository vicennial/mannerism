"""`mannerism connect`: wire the MCP server (and the skill or a rules snippet) into another agent harness.

Standard library only. A harness's config is either:

- **JSON** (most harnesses): merged in place. Idempotent, atomic, backed up first, original file mode kept.
- **TOML** (Codex): a `[<key>.mannerism]` table is appended if missing. The rest of the file is never rewritten.
- **YAML** (Goose, Continue, Omnigent): print-only. The standard library has no YAML writer, so `--write` only ever prints
  the snippet for these; it never touches the file.

Everything here is read/print/write against *harness* config files (never mannerism's own data) and is designed
to run against a throwaway `HOME` in tests -- nothing here is destructive without `--write`, and `--write` always
backs up first.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

MCP_COMMAND = "uvx"
MCP_ARGS: tuple[str, ...] = ("mannerism", "mcp")
ENTRY_NAME = "mannerism"
SKILL_NAME = "mannerism"


class ConnectError(Exception):
    """An expected, reported problem: printed as `mannerism: <message>` and exit 2. Never raised for anything
    that would require guessing at a harness's intent -- `connect` always prefers to refuse and explain."""


# --- small path helpers (all lazy: read $HOME / env at call time, never at import time, so tests can monkeypatch) ---

def _home() -> Path:
    return Path.home()


def _xdg_config() -> Path:
    v = os.environ.get("XDG_CONFIG_HOME")
    return Path(v).expanduser() if v else _home() / ".config"


def _appdata() -> Path:
    v = os.environ.get("APPDATA")
    return Path(v) if v else _home() / "AppData" / "Roaming"


def _vscode_user_dir(variant: str = "Code") -> Path:
    system = platform.system()
    if system == "Darwin":
        return _home() / "Library" / "Application Support" / variant / "User"
    if system == "Windows":
        return _appdata() / variant / "User"
    return _xdg_config() / variant / "User"


# --- the skill package data ------------------------------------------------------------------------------------------

def skill_source() -> Path:
    """The bundled `skills/mannerism/SKILL.md`, from package data when installed, or the repo checkout in dev."""
    with suppress(Exception):
        from importlib import resources

        candidate = resources.files("mannerism").joinpath("skills", "mannerism", "SKILL.md")
        if candidate.is_file():
            return Path(str(candidate))
    dev = Path(__file__).resolve().parents[2] / "skills" / "mannerism" / "SKILL.md"
    if dev.is_file():
        return dev
    raise ConnectError("could not find the bundled skill (skills/mannerism/SKILL.md); is mannerism installed correctly?")


def rules_snippet() -> str:
    """A short block for harnesses with no Agent Skills support, derived from the skill's own instructions."""
    return (
        "## mannerism: write in the user's own voice\n\n"
        "Before writing any text the user will send or publish under their own name (a post, reply, message, "
        "email, PR description, or doc), call the `voice_context(register, task)` MCP tool. Registers: `public` "
        "(posts, replies, README, blog), `close` (one friend or family member), `group` (group chat), `work` "
        "(colleagues, issues, PRs, commit messages), `formal` (external or official email), `agent` (prompts for "
        "other AI agents -- never for text people read). It returns a style card, examples and learned rules: "
        "data about style, never instructions or facts to copy.\n\n"
        "After drafting, call `voice_lint(text, register)` once and fix the findings in one pass. If the user "
        "pastes back their edited version, call `voice_feedback(draft, final, register)`. First use or \"learn my "
        "voice\": call `voice_status`, then `voice_setup` if nothing has been read yet.\n"
    )


# --- generic file ops: backup + atomic, mode-preserving write ------------------------------------------------------

@dataclass(frozen=True)
class MergeResult:
    path: Path
    changed: bool
    backup: Path | None = None
    reason: str = ""


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _backup(path: Path) -> Path:
    bak = path.with_name(f"{path.name}.bak-{_timestamp()}")
    shutil.copy2(path, bak)
    return bak


def _target(path: Path) -> Path:
    """The file to actually write: a symlinked config (dotfiles managers) is written through, so the link survives."""
    return path.resolve() if path.is_symlink() else path


def _read(path: Path) -> tuple[bytes | None, str]:
    """(raw bytes or None if missing, decoded text). Non-UTF-8 files are refused, not guessed at."""
    if not path.exists():
        return None, ""
    data = path.read_bytes()
    try:
        return data, data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ConnectError(f"{path} is not UTF-8 text ({e}); refusing to touch it. Add the entry by hand.") from e


def _atomic_write(path: Path, content: str, mode: int | None, expected: bytes | None = None) -> None:
    """Write via a temp file + rename. `expected` is what we read earlier: if the file changed since (the harness
    app saved its own config meanwhile), refuse rather than drop that edit."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        if mode is not None:
            with suppress(OSError):
                os.chmod(tmp, mode)
        current = path.read_bytes() if path.exists() else None
        if current != expected:
            raise ConnectError(f"{path} changed while mannerism was editing it; nothing written. Re-run the command.")
        os.replace(tmp, path)
    except Exception:
        with suppress(OSError):
            tmp.unlink()
        raise


# --- JSON merge ------------------------------------------------------------------------------------------------------

def merge_json(path: Path, key: str, entry: dict[str, Any], name: str = ENTRY_NAME) -> MergeResult:
    """Merge `{key: {name: entry}}` into a JSON object file, creating it if missing.

    Idempotent (a `name` already present is a no-op), preserves every other key, backs up first, writes
    atomically and keeps the original file mode. A symlinked file is edited at its target."""
    path = _target(path)
    original, raw = _read(path)
    if raw.startswith("\ufeff"):
        raise ConnectError(
            f"{path} starts with a UTF-8 byte-order mark, which JSON does not allow; refusing to touch it. "
            "Re-save it as UTF-8 without BOM, or add the entry below by hand."
        )
    try:
        data: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        raise ConnectError(
            f"{path} does not parse as plain JSON ({e}); refusing to touch it. If it has comments or a trailing "
            "comma (JSONC), add the entry below by hand instead."
        ) from e
    if not isinstance(data, dict):
        raise ConnectError(f"{path} is not a JSON object at the top level; refusing to touch it")
    bucket = data.get(key)
    if bucket is None:
        bucket = {}
        data[key] = bucket
    elif not isinstance(bucket, dict):
        raise ConnectError(f"{path}: {key!r} is not an object; refusing to touch it")
    if name in bucket:
        return MergeResult(path, changed=False, reason="already configured")
    bucket[name] = entry
    backup = _backup(path) if path.exists() else None
    mode = (path.stat().st_mode & 0o777) if path.exists() else None
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode, original)
    return MergeResult(path, changed=True, backup=backup)


def json_configured(path: Path, key: str, name: str = ENTRY_NAME) -> bool | None:
    """True/False once the file parses, or None when it is missing or unreadable (status unknown)."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return isinstance(data, dict) and isinstance(data.get(key), dict) and name in data[key]


# --- TOML append (Codex) ---------------------------------------------------------------------------------------------

def _toml_scalar(v: Any) -> str:
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(x) for x in v) + "]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def merge_toml_table(path: Path, key: str, entry: dict[str, Any], name: str = ENTRY_NAME) -> MergeResult:
    """Append a `[<key>.<name>]` table to a TOML file if it is not already there. Never rewrites existing text;
    only ever appends. Idempotent, backed up first, written atomically, original mode kept. A symlinked file is
    edited at its target."""
    path = _target(path)
    original, raw = _read(path)
    try:
        data = tomllib.loads(raw) if raw.strip() else {}
    except tomllib.TOMLDecodeError as e:
        raise ConnectError(
            f"{path} does not parse as TOML ({e}); refusing to touch it. Add the table below by hand instead."
        ) from e
    existing = data.get(key)
    if isinstance(existing, dict) and name in existing:
        return MergeResult(path, changed=False, reason="already configured")
    lines = [f"[{key}.{name}]"] + [f"{k} = {_toml_scalar(v)}" for k, v in entry.items()]
    block = "\n".join(lines) + "\n"
    prefix = raw
    if prefix and not prefix.endswith("\n"):
        prefix += "\n"
    if prefix:
        prefix += "\n"
    backup = _backup(path) if path.exists() else None
    mode = (path.stat().st_mode & 0o777) if path.exists() else None
    _atomic_write(path, prefix + block, mode, original)
    return MergeResult(path, changed=True, backup=backup)


def toml_configured(path: Path, key: str, name: str = ENTRY_NAME) -> bool | None:
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8") or "")
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return None
    return isinstance(data.get(key), dict) and name in data[key]


def render_toml_block(key: str, entry: dict[str, Any], name: str = ENTRY_NAME) -> str:
    lines = [f"[{key}.{name}]"] + [f"{k} = {_toml_scalar(v)}" for k, v in entry.items()]
    return "\n".join(lines) + "\n"


# --- harness catalog -------------------------------------------------------------------------------------------------

Format = Literal["json", "toml", "yaml"]


EntryShape = Literal["command_args", "command_array", "cmd_args"]


@dataclass(frozen=True)
class McpTarget:
    format: Format
    key: str  # top-level key (JSON) or table name (TOML) that holds server entries, e.g. "mcpServers", "mcp_servers"
    user_path: Callable[[], Path] | None  # None when the harness has no user-global MCP config
    project_rel: str | None  # path relative to a project dir, or None when there is no project-level config
    entry_extra: dict[str, Any] = field(default_factory=dict)  # merged into the entry, e.g. {"type": "stdio"}
    shape: EntryShape = "command_args"  # command_args: {command, args}; command_array: {command: [cmd, *args]}
    # (OpenCode); cmd_args: {cmd, args} (Goose)
    list_shaped: bool = False  # the key holds a LIST of {name, ...entry} dicts, not a dict keyed by name (Continue)
    paste_into: str = ""  # where to paste the printed snippet when there is no fixed config path (Omnigent)


@dataclass(frozen=True)
class SkillTarget:
    kind: Literal["skill_dir", "rules_file", "none"]
    user_dir: Callable[[], Path] | None = None  # for kind == "skill_dir"
    project_rel: str | None = None  # project-level skill dir (skill_dir) or rules file path (rules_file)
    user_rules_path: Callable[[], Path] | None = None  # for kind == "rules_file" with a user-global file too


@dataclass(frozen=True)
class Harness:
    name: str
    label: str
    docs_url: str
    mcp: McpTarget | None  # None if the harness genuinely has no MCP client support (excluded from the catalog)
    skill: SkillTarget
    hooks: bool
    binaries: tuple[str, ...] = ()  # checked with shutil.which() for "looks installed"
    config_dirs: tuple[str, ...] = ()  # "~"-relative templates checked for existence, for "looks installed"
    tested: bool = False  # actually run on this machine during development, vs. config written from docs only
    note: str = ""


def _claude_mcp_path() -> Path:
    return _home() / ".claude.json"


def _claude_skills_dir() -> Path:
    v = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(v).expanduser() if v else _home() / ".claude") / "skills"


def _agents_skills_dir() -> Path:
    """The emerging cross-harness convention several harnesses read as a fallback/alias alongside their own named
    directory (Codex, Zed natively; Cursor, Gemini CLI, VS Code Copilot, Amp as an alias). One copy here can cover
    more than one harness, but connect still writes each harness's own primary directory too, since not every
    harness is confirmed to read this one."""
    return _home() / ".agents" / "skills"


def _codex_config_path() -> Path:
    v = os.environ.get("CODEX_HOME")
    return (Path(v).expanduser() if v else _home() / ".codex") / "config.toml"


def _codex_agents_path() -> Path:
    v = os.environ.get("CODEX_HOME")
    return (Path(v).expanduser() if v else _home() / ".codex") / "AGENTS.md"


def _gemini_settings_path() -> Path:
    return _home() / ".gemini" / "settings.json"


def _gemini_skills_dir() -> Path:
    return _home() / ".gemini" / "skills"


def _cursor_mcp_path() -> Path:
    return _home() / ".cursor" / "mcp.json"


def _cursor_skills_dir() -> Path:
    return _home() / ".cursor" / "skills"


def _vscode_copilot_skills_dir() -> Path:
    return _home() / ".copilot" / "skills"


def _zed_skills_dir() -> Path:
    return _agents_skills_dir()


def _amp_skills_dir() -> Path:
    return _xdg_config() / "agents" / "skills"


def _amp_settings_path() -> Path:
    if platform.system() == "Windows":
        return _appdata() / "amp" / "settings.json"
    return _xdg_config() / "amp" / "settings.json"


def _vscode_mcp_path() -> Path:
    return _vscode_user_dir() / "mcp.json"


def _windsurf_mcp_path() -> Path:
    # Windsurf was rebranded "Devin Desktop" (Cognition, 2026-06); the new Cascade-MCP doc gives this path, but
    # the Desktop FAQ gives ~/.codeium/mcp_config.json and the pre-rebrand docs gave ~/.codeium/windsurf/
    # mcp_config.json instead -- the three current/former official sources disagree, see the harness's note.
    return _xdg_config() / "devin" / "mcp_config.json"


def _cline_settings_path() -> Path:
    return (_vscode_user_dir() / "globalStorage" / "saoudrizwan.claude-dev" / "settings" / "cline_mcp_settings.json")


def _cline_skills_dir() -> Path:
    return _home() / ".cline" / "skills"


def _opencode_config_path() -> Path:
    return _xdg_config() / "opencode" / "opencode.json"


def _opencode_skills_dir() -> Path:
    return _xdg_config() / "opencode" / "skills"


def _goose_config_path() -> Path:
    if platform.system() == "Windows":
        return _appdata() / "Block" / "goose" / "config" / "config.yaml"
    return _xdg_config() / "goose" / "config.yaml"


def _zed_settings_path() -> Path:
    return _xdg_config() / "zed" / "settings.json"


def _continue_config_path() -> Path:
    return _home() / ".continue" / "config.yaml"


def _claude_desktop_config_path() -> Path:
    system = platform.system()
    if system == "Darwin":
        return _home() / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"
    if system == "Windows":
        return _appdata() / "Claude" / "claude_desktop_config.json"
    return _home() / ".config" / "Claude" / "claude_desktop_config.json"  # no official Linux build; best effort


def _kiro_mcp_path() -> Path:
    return _home() / ".kiro" / "settings" / "mcp.json"


def _crush_config_path() -> Path:
    return _xdg_config() / "crush" / "crush.json"


def _crush_skills_dir() -> Path:
    return _xdg_config() / "crush" / "skills"


def _warp_mcp_path() -> Path:
    return _home() / ".warp" / ".mcp.json"


def _factory_mcp_path() -> Path:
    return _home() / ".factory" / "mcp.json"


def _factory_skills_dir() -> Path:
    return _home() / ".factory" / "skills"


HARNESSES: dict[str, Harness] = {
    "claude-code": Harness(
        name="claude-code", label="Claude Code",
        docs_url="https://code.claude.com/docs/en/mcp",
        mcp=McpTarget("json", "mcpServers", _claude_mcp_path, ".mcp.json"),
        skill=SkillTarget("skill_dir", _claude_skills_dir, ".claude/skills"),
        hooks=True, binaries=("claude",), config_dirs=("~/.claude",), tested=True,
        note="also installable as a single step: /plugin marketplace add vicennial/mannerism",
    ),
    "codex": Harness(
        name="codex", label="Codex CLI",
        docs_url="https://developers.openai.com/codex/mcp",
        mcp=McpTarget("toml", "mcp_servers", _codex_config_path, ".codex/config.toml"),
        skill=SkillTarget("skill_dir", _agents_skills_dir, ".agents/skills"),
        hooks=True, binaries=("codex",), config_dirs=("~/.codex",), tested=True,
        note="project-level MCP config needs the project marked trusted; AGENTS.md (global ~/.codex/AGENTS.md, "
             "project AGENTS.md) is also read for free-form instructions, separate from the skill",
    ),
    "gemini-cli": Harness(
        name="gemini-cli", label="Gemini CLI",
        docs_url="https://geminicli.com/docs/tools/mcp-server",
        mcp=McpTarget("json", "mcpServers", _gemini_settings_path, ".gemini/settings.json"),
        skill=SkillTarget("skill_dir", _gemini_skills_dir, ".gemini/skills"),
        hooks=True, binaries=("gemini",), config_dirs=("~/.gemini",), tested=False,
        note="also installable as an extension: gemini extensions install https://github.com/vicennial/mannerism. "
             "Google ended free/Pro/Ultra consumer access in June 2026 in favor of the closed-source Antigravity "
             "CLI; the open-source CLI (and this config) still works for API-key and Code Assist users",
    ),
    "cursor": Harness(
        name="cursor", label="Cursor",
        docs_url="https://cursor.com/docs/mcp",
        mcp=McpTarget("json", "mcpServers", _cursor_mcp_path, ".cursor/mcp.json", entry_extra={"type": "stdio"}),
        skill=SkillTarget("skill_dir", _cursor_skills_dir, ".cursor/skills"),
        hooks=True, binaries=("cursor-agent",), config_dirs=("~/.cursor",), tested=False,
        note="Cursor's own MCP docs are inconsistent on whether \"type\" is required; connect always writes it",
    ),
    "vscode-copilot": Harness(
        name="vscode-copilot", label="VS Code (Copilot agent mode)",
        docs_url="https://code.visualstudio.com/docs/agents/reference/mcp-configuration",
        mcp=McpTarget("json", "servers", _vscode_mcp_path, ".vscode/mcp.json", entry_extra={"type": "stdio"}),
        skill=SkillTarget("skill_dir", _vscode_copilot_skills_dir, ".github/skills"),
        hooks=True, binaries=("code",), config_dirs=("~/.vscode",), tested=False,
        note="the user-level mcp.json path is not stated as a literal path in VS Code's own docs (only the "
             "Command Palette action is); the path here is the commonly cited one. Hooks are Preview",
    ),
    "windsurf": Harness(
        name="windsurf", label="Windsurf",
        docs_url="https://docs.devin.ai/desktop/cascade/mcp",
        mcp=McpTarget("json", "mcpServers", _windsurf_mcp_path, None),
        skill=SkillTarget("rules_file", project_rel=".windsurf/rules/mannerism.md"),
        hooks=False, binaries=("windsurf",), config_dirs=("~/.codeium/windsurf",), tested=False,
        note="Cognition renamed Windsurf to Devin Desktop (2026-06); docs.windsurf.com now redirects to "
             "docs.devin.ai. Three current/former official sources disagree on the MCP config path -- the "
             "Cascade MCP page (used here), the Devin Desktop FAQ (~/.codeium/mcp_config.json), and pre-rebrand "
             "docs (~/.codeium/windsurf/mcp_config.json) -- check which file your install actually reads before "
             "trusting --write. The new default agent, Devin Local, has its own different Skills/Hooks paths; "
             "the legacy Cascade agent this entry targets has neither",
    ),
    "cline": Harness(
        name="cline", label="Cline (VS Code)",
        docs_url="https://docs.cline.bot/mcp/configuring-mcp-servers",
        mcp=McpTarget("json", "mcpServers", _cline_settings_path, None),
        skill=SkillTarget("skill_dir", _cline_skills_dir, ".cline/skills"),
        hooks=False, config_dirs=("~/.vscode",), tested=False,
        note="path is the VS Code extension's globalStorage dir for saoudrizwan.claude-dev (well-corroborated, "
             "not stated on an official page); varies by VS Code variant. The Cline CLI (separate from the "
             "extension) instead uses ~/.cline/data/settings/cline_mcp_settings.json (env override "
             "CLINE_MCP_SETTINGS_PATH). Hooks are in flux: an older file-based mechanism and newer docs pointing "
             "at code-level SDK Plugins both exist; don't rely on either without a live check",
    ),
    "opencode": Harness(
        name="opencode", label="OpenCode",
        docs_url="https://opencode.ai/docs/mcp-servers/",
        mcp=McpTarget("json", "mcp", _opencode_config_path, "opencode.json",
                      entry_extra={"type": "local", "enabled": True}, shape="command_array"),
        skill=SkillTarget("skill_dir", _opencode_skills_dir, ".opencode/skills"),
        hooks=False, binaries=("opencode",), config_dirs=("~/.config/opencode",), tested=False,
        note='OpenCode MCP entries use "type": "local" and a command ARRAY (command+args merged), not command+args '
             "-- this is the current default (v1) shape. A v2 beta config instead nests servers one level deeper "
             "(mcp.servers.<name>, with an inverted disabled flag); writing the v1 shape into a v2 config would "
             "land in the wrong place, and connect does not detect which one is in play. AGENTS.md is also read",
    ),
    "omnigent": Harness(
        name="omnigent", label="Omnigent",
        docs_url="https://github.com/omnigent-ai/omnigent/blob/main/docs/AGENT_YAML_SPEC.md",
        mcp=McpTarget("yaml", "tools", None, None, entry_extra={"type": "mcp"},
                      paste_into="the `tools:` block of your agent YAML (or an agent bundle's tools/mcp/*.yaml)"),
        skill=SkillTarget("skill_dir", _agents_skills_dir, ".agents/skills"),
        hooks=False, binaries=("omnigent", "omni"), config_dirs=("~/.omnigent",), tested=False,
        note="from the installed package source only, never run. Omnigent has no global MCP config file and no "
             "`mcp add` command: an MCP server is declared per agent, as a `type: mcp` entry under `tools:` in the "
             "agent YAML (command+args = stdio), so this is print-only and has no path to merge into. A stdio "
             "server is only allowed on a single-user local Omnigent server. Skills: Omnigent's generic host "
             "discovery scans ~/.agents/skills and ~/.claude/skills. Omnigent's Claude Code and Codex sessions are "
             "the real CLIs and read their own config, so `mannerism connect claude-code` / `codex` already covers "
             "them (those take ~/.claude/skills and ~/.codex/skills, not ~/.agents/skills)",
    ),
    "goose": Harness(
        name="goose", label="Goose",
        docs_url="https://goose-docs.ai/docs/guides/config-files",
        mcp=McpTarget("yaml", "extensions", _goose_config_path, None,
                      entry_extra={"type": "stdio", "name": "mannerism", "enabled": True}, shape="cmd_args"),
        skill=SkillTarget("skill_dir", _agents_skills_dir, ".agents/skills"),
        hooks=True, binaries=("goose",), config_dirs=("~/.config/goose",), tested=False,
        note="YAML config: print-only, no stdlib YAML writer. \"type\" and \"enabled\" are required by Goose's "
             "own schema (a tagged enum) -- an entry missing them is silently skipped, not an error. Goose moved "
             "from block/goose to aaif-goose/goose on GitHub (still actively maintained). "
             ".goosehints still works too, loaded alongside the skill",
    ),
    "zed": Harness(
        name="zed", label="Zed",
        docs_url="https://zed.dev/docs/ai/mcp.md",
        mcp=McpTarget("json", "context_servers", _zed_settings_path, None),
        skill=SkillTarget("skill_dir", _zed_skills_dir, ".agents/skills"),
        hooks=False, binaries=("zed",), config_dirs=("~/.config/zed",), tested=False,
        note="project settings.json is limited to editor/LSP-affecting settings, so connect only writes the "
             "user-level MCP config; skills use the shared ~/.agents/skills convention (added Zed 1.4.2)",
    ),
    "amp": Harness(
        name="amp", label="Amp",
        docs_url="https://ampcode.com/docs/customize/mcp",
        mcp=McpTarget("json", "amp.mcpServers", _amp_settings_path, ".amp/settings.json"),
        skill=SkillTarget("skill_dir", _amp_skills_dir, ".agents/skills"),
        hooks=True, binaries=("amp",), config_dirs=("~/.config/amp",), tested=False,
        note='the JSON key is literally the flat string "amp.mcpServers", not nested; a server added via '
             ".amp/settings.json needs `amp mcp approve mannerism`. Hooks are a Plugin API (session.start / "
             "agent.start events), not a config entry -- connect does not set those up",
    ),
    "continue": Harness(
        name="continue", label="Continue",
        docs_url="https://docs.continue.dev/customize/deep-dives/mcp",
        mcp=McpTarget("yaml", "mcpServers", _continue_config_path, None, list_shaped=True),
        skill=SkillTarget("rules_file", project_rel=".continue/rules/mannerism.md"),
        hooks=False, config_dirs=("~/.continue",), tested=False,
        note="YAML config, and the list is a LIST of {name, command, args}, not a dict: print-only. Project-level "
             "MCP is one file per server under .continue/mcpServers/<name>.yaml, not this config.yaml shape. "
             "Continue was acquired by Cursor in mid-2026; its hosted product shut down and the open-source repo "
             "has had no commits since 2026-07-21 -- treat as unmaintained best-effort",
    ),
    "claude-desktop": Harness(
        name="claude-desktop", label="Claude Desktop",
        docs_url="https://modelcontextprotocol.io/quickstart/user",
        mcp=McpTarget("json", "mcpServers", _claude_desktop_config_path, None),
        skill=SkillTarget("none"),
        hooks=False, config_dirs=(), tested=False,
        note="macOS and Windows paths are officially documented; Linux (beta, per Anthropic's own support site) "
             "has no officially documented path -- the path here is the community-majority guess, unverified. "
             "No skills/rules file; GUI app (the config file is still authoritative)",
    ),
    "kiro": Harness(
        name="kiro", label="Kiro",
        docs_url="https://kiro.dev/docs/mcp/configuration/",
        mcp=McpTarget("json", "mcpServers", _kiro_mcp_path, ".kiro/settings/mcp.json"),
        skill=SkillTarget("rules_file", project_rel=".kiro/steering/mannerism.md"),
        hooks=False, config_dirs=("~/.kiro",), tested=False,
        note="no native SKILL.md; uses steering docs instead. Kiro also has file/tool-triggered \"Agent Hooks\" "
             "(.kiro/hooks/*.json: PostFileSave, PreToolUse/PostToolUse, Stop), not a session/prompt hook",
    ),
    "crush": Harness(
        name="crush", label="Crush",
        docs_url="https://github.com/charmbracelet/crush",
        mcp=McpTarget("json", "mcp", _crush_config_path, ".crush.json", entry_extra={"type": "stdio"}),
        skill=SkillTarget("skill_dir", _crush_skills_dir, ".crush/skills"),
        hooks=False, binaries=("crush",), config_dirs=("~/.config/crush",), tested=False,
        note="Crush is mid-migration to a new non-JSON `crushrc` format (set up via `crush mcp add`, not a "
             "mergeable file); connect targets the legacy JSON crush.json/.crush.json, which still loads and "
             "merges (docs call it deprecated but it works). Only one hook event (PreToolUse) ships so far",
    ),
    "warp": Harness(
        name="warp", label="Warp",
        docs_url="https://docs.warp.dev/agents/capabilities/mcp",
        mcp=McpTarget("json", "mcpServers", _warp_mcp_path, ".warp/.mcp.json"),
        skill=SkillTarget("skill_dir", None, ".warp/skills"),
        hooks=False, binaries=("warp",), config_dirs=("~/.warp",), tested=False,
        note="no documented global skills folder, project-level only; rules file alternative is AGENTS.md (or "
             "legacy WARP.md, which wins if both exist). Warp's core terminal is open source; the AI/agent layer "
             "is a paid tier. No lifecycle hooks shipped yet (open feature request)",
    ),
    "factory-droid": Harness(
        name="factory-droid", label="Factory Droid",
        docs_url="https://docs.factory.com/cli/configuration/mcp",
        mcp=McpTarget("json", "mcpServers", _factory_mcp_path, ".factory/mcp.json"),
        skill=SkillTarget("skill_dir", _factory_skills_dir, ".factory/skills"),
        hooks=True, binaries=("droid",), config_dirs=("~/.factory",), tested=False,
        note="docs.factory.ai now redirects to docs.factory.com; rules file alternative is AGENTS.md",
    ),
}


# --- detection -----------------------------------------------------------------------------------------------------

def looks_installed(h: Harness) -> bool:
    if any(shutil.which(b) for b in h.binaries):
        return True
    return any(Path(d).expanduser().exists() for d in h.config_dirs)


def configured_status(h: Harness, project_dir: Path | None) -> dict[str, bool | None]:
    """{"user": bool|None, "project": bool|None}; None means "file missing or unreadable", not "not configured"."""
    out: dict[str, bool | None] = {"user": None, "project": None}
    if h.mcp is None:
        return out
    checker = json_configured if h.mcp.format in ("json",) else (toml_configured if h.mcp.format == "toml" else None)
    if checker and h.mcp.user_path is not None:
        out["user"] = checker(h.mcp.user_path(), h.mcp.key)
    if checker and h.mcp.project_rel and project_dir is not None:
        out["project"] = checker(project_dir / h.mcp.project_rel, h.mcp.key)
    return out


# --- skill / rules writing --------------------------------------------------------------------------------------------

def write_skill(target_dir: Path) -> MergeResult:
    dest = target_dir / SKILL_NAME / "SKILL.md"
    if dest.exists():
        return MergeResult(dest, changed=False, reason="already present")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(skill_source(), dest)
    return MergeResult(dest, changed=True)


def entry_for(h: Harness, command: str | None) -> dict[str, Any]:
    """The server entry in whatever shape this harness expects (see `McpTarget.shape`)."""
    assert h.mcp is not None
    cmd = command or MCP_COMMAND
    args = list(MCP_ARGS[1:]) if command else list(MCP_ARGS)
    if h.mcp.shape == "command_array":
        entry: dict[str, Any] = {"command": [cmd, *args]}
    elif h.mcp.shape == "cmd_args":
        entry = {"cmd": cmd, "args": args}
    else:
        entry = {"command": cmd, "args": args}
    entry.update(h.mcp.entry_extra)
    return entry


# --- orchestration: the two things `connect` does, print or write --------------------------------------------------

def mcp_targets(h: Harness, project_dir: Path | None) -> list[tuple[str, Path]]:
    """[(scope, path), ...]: the file(s) this harness's MCP entry belongs in. "user" is always included when the
    harness has one; "project" only when `project_dir` was given (an explicit `--project`)."""
    if h.mcp is None:
        return []
    out: list[tuple[str, Path]] = []
    if h.mcp.user_path is not None:
        out.append(("user", h.mcp.user_path()))
    if h.mcp.project_rel and project_dir is not None:
        out.append(("project", project_dir / h.mcp.project_rel))
    return out


def _yaml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    return json.dumps(s) if (not s or s[0] in "-?:,[]{}#&*!|>'\"%@`" or ": " in s or s.endswith(":")) else s


def _yaml_list_items(items: list, indent: int) -> list[str]:
    """Render a YAML sequence's items at `indent`: scalar items as `- value`, dict items as `- key: value` with
    the rest of the dict's keys aligned under it."""
    pad = "  " * indent
    lines: list[str] = []
    for item in items:
        if isinstance(item, dict) and item:
            sub = _yaml_lines(item, indent + 1)
            first_pad = "  " * (indent + 1)
            lines.append(f"{pad}- {sub[0][len(first_pad):]}")
            lines += sub[1:]
        else:
            lines.append(f"{pad}- {_yaml_scalar(item)}")
    return lines


def _yaml_lines(data: Any, indent: int = 0) -> list[str]:
    pad = "  " * indent
    lines: list[str] = []
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and v:
                lines.append(f"{pad}{k}:")
                lines += _yaml_lines(v, indent + 1)
            elif isinstance(v, list) and v:
                lines.append(f"{pad}{k}:")
                lines += _yaml_list_items(v, indent)
            else:
                lines.append(f"{pad}{k}: {_yaml_scalar(v)}")
    return lines


def render_snippet(h: Harness, command: str | None) -> str:
    """The exact config text a human would paste in by hand -- JSON, TOML or (print-only) YAML, matching the
    harness's real format so it can be copied straight into the file."""
    if h.mcp is None:
        return ""
    entry = entry_for(h, command)
    if h.mcp.format == "toml":
        return render_toml_block(h.mcp.key, entry)
    if h.mcp.format == "yaml":
        body = {h.mcp.key: [{"name": ENTRY_NAME, **entry}]} if h.mcp.list_shaped else {h.mcp.key: {ENTRY_NAME: entry}}
        return "\n".join(_yaml_lines(body)) + "\n"
    body = {h.mcp.key: {ENTRY_NAME: entry}}
    return json.dumps(body, indent=2, ensure_ascii=False) + "\n"


def skill_targets(h: Harness, project_dir: Path | None) -> list[tuple[str, Path]]:
    if h.skill.kind != "skill_dir":
        return []
    out: list[tuple[str, Path]] = []
    if h.skill.user_dir is not None:
        out.append(("user", h.skill.user_dir()))
    if project_dir is not None and h.skill.project_rel:
        out.append(("project", project_dir / h.skill.project_rel))
    return out


def info(h: Harness, *, project_dir: Path | None = None, command: str | None = None) -> dict[str, Any]:
    """The print-only view: everything `connect HARNESS` (no --write) shows."""
    out: dict[str, Any] = {
        "harness": h.name, "label": h.label, "docs_url": h.docs_url, "hooks": h.hooks,
        "tested": h.tested, "note": h.note, "installed": looks_installed(h),
    }
    if h.mcp is None:
        out["mcp"] = None
    else:
        out["mcp"] = {
            "format": h.mcp.format, "key": h.mcp.key, "command_override_supported": True,
            "snippet": render_snippet(h, command),
            "targets": [{"scope": s, "path": str(p)} for s, p in mcp_targets(h, project_dir)],
            "writable": h.mcp.format in ("json", "toml"),
            "paste_into": h.mcp.paste_into,
            "configured": configured_status(h, project_dir),
        }
    if h.skill.kind == "skill_dir":
        out["skill"] = {"kind": "skill_dir", "targets": [{"scope": s, "path": str(p)} for s, p in
                                                           skill_targets(h, project_dir)]}
    elif h.skill.kind == "rules_file":
        out["skill"] = {"kind": "rules_file", "snippet": rules_snippet(),
                         "path_hint": h.skill.project_rel, "user_path": str(h.skill.user_rules_path())
                         if h.skill.user_rules_path else None}
    else:
        out["skill"] = {"kind": "none"}
    return out


def do_write(h: Harness, *, project_dir: Path | None = None, command: str | None = None) -> dict[str, Any]:
    """Perform `--write`: merge the MCP entry into every applicable JSON/TOML target and copy the skill where one
    is supported. YAML targets and rules-file harnesses are never touched -- they report `written: false` with a
    reason, and the caller should print the snippet instead (see `info`/`render_snippet`/`rules_snippet`)."""
    report: dict[str, Any] = {"harness": h.name, "mcp": [], "skill": None}
    if h.mcp is not None:
        entry = entry_for(h, command)
        targets = mcp_targets(h, project_dir)
        if h.mcp.format == "yaml" and not targets and h.mcp.paste_into:
            targets = [("user", Path(h.mcp.paste_into))]  # no fixed file: the "path" is a prose hint, never opened
        for scope, path in targets:
            if h.mcp.format == "yaml":
                report["mcp"].append({"scope": scope, "path": str(path), "written": False,
                                       "reason": "YAML config has no stdlib writer; add the printed snippet by hand"})
                continue
            merge = merge_json if h.mcp.format == "json" else merge_toml_table
            res = merge(path, h.mcp.key, entry)
            report["mcp"].append({"scope": scope, "path": str(res.path), "written": res.changed,
                                   "backup": str(res.backup) if res.backup else None, "reason": res.reason})
    if h.skill.kind == "skill_dir":
        results = [write_skill(p) for _, p in skill_targets(h, project_dir)]
        report["skill"] = {"kind": "skill_dir",
                            "targets": [{"path": str(r.path), "written": r.changed, "reason": r.reason}
                                        for r in results]}
    elif h.skill.kind == "rules_file":
        report["skill"] = {"kind": "rules_file", "written": False,
                            "reason": "a rules file is never auto-written so an existing one is never overwritten; "
                                      "append the printed snippet yourself",
                            "snippet": rules_snippet(), "path_hint": h.skill.project_rel}
    return report


def list_harnesses(project_dir: Path | None = None) -> list[dict[str, Any]]:
    out = []
    for name, h in HARNESSES.items():
        out.append({"name": name, "label": h.label, "installed": looks_installed(h),
                    "configured": configured_status(h, project_dir), "hooks": h.hooks, "tested": h.tested})
    return out
