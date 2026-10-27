# Harness support

mannerism reaches an agent three ways: a skill (`SKILL.md`), an MCP server (`uvx mannerism mcp`), and a CLI. Most
harnesses need only two things: where the MCP config goes, and where the skill or a rules file goes. `mannerism
connect` knows both for the harnesses below.

```
mannerism connect                 list harnesses, and which look installed on this machine
mannerism connect NAME            print the exact snippet, file path, and skill/rules step
mannerism connect NAME --write    wire it in (idempotent, backs up first, never overwrites a rules file)
```

The table below is the research behind that command. Run `mannerism connect NAME` for the current, exact snippet
for your machine — it is generated from the same data as this table, so it never drifts from what `--write` does.
All facts were checked against official docs on 2026-10-21 (vendor doc pages are mostly undated; see the source
links in `src/mannerism/connect.py`'s `HARNESSES` table for one citation per harness).

## Tested vs. documented

**Tested by actually running the harness** (on this machine, with a throwaway `HOME`): **Claude Code** and
**Codex CLI**. For both, `mannerism connect <name> --write` produced a config that the real `claude mcp list` /
`codex mcp list` then reported as a connected MCP server.

Everything else below is **config written from docs only** — cross-checked across multiple independent research
passes with citations, but not run on this machine (the binary isn't installed here, or it needs a login
mannerism's author didn't want to set up just to test a config file). Treat those as beta until someone runs
them. If a path turns out wrong, `mannerism connect NAME` still shows you exactly what it tried, so fixing it by
hand is one copy-paste away.

## The table

| Harness | MCP config (user-level) | Key | Skills / rules | Hooks | Tested |
|---|---|---|---|---|---|
| [Claude Code](https://code.claude.com/docs/en/mcp) | `~/.claude.json` (JSON) | `mcpServers` | skill: `~/.claude/skills/` | yes | **yes** |
| [Codex CLI](https://developers.openai.com/codex/mcp) | `~/.codex/config.toml` (TOML) | `mcp_servers` | skill: `~/.agents/skills/` (+ AGENTS.md) | yes | **yes** |
| [Gemini CLI](https://geminicli.com/docs/tools/mcp-server) | `~/.gemini/settings.json` (JSON) | `mcpServers` | skill: `~/.gemini/skills/` | yes | no |
| [Cursor](https://cursor.com/docs/mcp) | `~/.cursor/mcp.json` (JSON) | `mcpServers` | skill: `~/.cursor/skills/` | yes | no |
| [VS Code Copilot](https://code.visualstudio.com/docs/agents/reference/mcp-configuration) | `~/.config/Code/User/mcp.json` (JSON) | `servers` | skill: `~/.copilot/skills/` | yes (preview) | no |
| [Windsurf](https://docs.devin.ai/desktop/cascade/mcp) | `~/.config/devin/mcp_config.json` (JSON, disputed — see below) | `mcpServers` | rules: `.windsurf/rules/*.md` | no | no |
| [Cline](https://docs.cline.bot/mcp/configuring-mcp-servers) | VS Code `globalStorage` dir (JSON) | `mcpServers` | skill: `~/.cline/skills/` | unclear | no |
| [OpenCode](https://opencode.ai/docs/mcp-servers/) | `~/.config/opencode/opencode.json` (JSON) | `mcp` (array-shaped entry) | skill: `~/.config/opencode/skills/` | no (own "Plugins") | no |
| [Goose](https://goose-docs.ai/docs/guides/config-files) | `~/.config/goose/config.yaml` (**YAML, print-only**) | `extensions` | skill: `~/.agents/skills/` | yes | no |
| [Zed](https://zed.dev/docs/ai/mcp.md) | `~/.config/zed/settings.json` (JSON) | `context_servers` | skill: `~/.agents/skills/` | no | no |
| [Amp](https://ampcode.com/docs/customize/mcp) | `~/.config/amp/settings.json` (JSON) | `amp.mcpServers` (flat key) | skill: `~/.config/agents/skills/` | yes (plugin API) | no |
| [Omnigent](https://github.com/omnigent-ai/omnigent/blob/main/docs/AGENT_YAML_SPEC.md) | none: declared per agent, in the agent YAML (**YAML, print-only**) | `tools` (`type: mcp` entry) | skill: `~/.agents/skills/` | no | no (source-read) |
| [Continue](https://docs.continue.dev/customize/deep-dives/mcp) | `~/.continue/config.yaml` (**YAML list, print-only**) | `mcpServers` | rules: `.continue/rules/*.md` | no | no |
| [Claude Desktop](https://modelcontextprotocol.io/quickstart/user) | `claude_desktop_config.json`, path by OS | `mcpServers` | none | no | no |
| [Kiro](https://kiro.dev/docs/mcp/configuration/) | `~/.kiro/settings/mcp.json` (JSON) | `mcpServers` | rules: `.kiro/steering/*.md` | no (file/tool hooks, not session) | no |
| [Crush](https://github.com/charmbracelet/crush) | `~/.config/crush/crush.json` (JSON, legacy format) | `mcp` | skill: `~/.config/crush/skills/` | no (1 of ~9 planned) | no |
| [Warp](https://docs.warp.dev/agents/capabilities/mcp) | `~/.warp/.mcp.json` (JSON) | `mcpServers` | skill: project `.warp/skills/` only | no | no |
| [Factory Droid](https://docs.factory.com/cli/configuration/mcp) | `~/.factory/mcp.json` (JSON) | `mcpServers` | skill: `~/.factory/skills/` | yes | no |

Dropped: **Roo Code** — officially sunset 2026-05-15, GitHub repo archived; its own docs now point people at
Cline instead. Not worth wiring up a dead harness.

## Omnigent (documented from source, not run)

Omnigent is an agent host that runs other harnesses (Claude Code, Codex, Pi, Kimi, OpenCode, Goose and more)
under one server. Its support here was read from the installed package source (v0.16.0) and its README. Nothing
was run: Omnigent was never started, and `mannerism connect omnigent` has not been tried against a real Omnigent
install.

- **MCP.** Omnigent has no global MCP config file and no `mcp add` command. A server is declared per agent, as
  a `type: mcp` entry under `tools:` in the agent YAML (`command` + `args` means stdio, `url` means HTTP), or as
  `tools/mcp/<name>.yaml` in an agent bundle. `mannerism connect omnigent` therefore prints that snippet and
  tells you where to paste it. It never writes a file for you. A stdio server is only accepted by a
  single-user local Omnigent server, not a multi-tenant one.
- **Skills.** Omnigent's generic host skill discovery scans `.agents/skills` and `.claude/skills` (project
  ancestors and your home directory), so `--write` copies the skill to `~/.agents/skills/mannerism/`. An agent
  bundle can also ship its own `skills/<name>/SKILL.md`.
- **Claude Code and Codex inside Omnigent.** These are the real CLIs. They read their own config, so
  `mannerism connect claude-code` and `mannerism connect codex` already apply there. Omnigent's Claude Code
  sessions load `~/.claude/skills` and enabled Claude Code plugins, and add Omnigent's own MCP server
  alongside your MCP servers (it does not replace them). For those sessions, use the Claude Code or Codex
  skill location, not `~/.agents/skills`.

## Rough popularity (for the "top 12", Oct 2026)

VS Code Copilot, Cursor, Claude Code, OpenCode, Codex CLI, Gemini CLI, Zed, Windsurf, Cline, Warp, Factory Droid,
Goose — then Continue, Crush, Kiro, Amp, Claude Desktop. Mixing GitHub stars with reported revenue/user numbers
isn't apples to apples, so treat the order as directional, not a leaderboard.

## Things worth knowing before you trust a config blindly

- **A lot of harnesses added native Agent Skills support in 2026.** Codex, Gemini CLI, Cursor, VS Code Copilot,
  Zed, Amp, Goose, Cline, OpenCode, Warp and Factory Droid all now read `SKILL.md` folders natively — most of
  them also accept the shared `~/.agents/skills/` / `.agents/skills/` convention as a fallback alongside their
  own named directory. `mannerism connect NAME --write` copies the skill into that harness's own primary
  directory; if several of your harnesses share `.agents/skills/`, one copy there can cover more than one of
  them.
- **Gemini CLI**: Google ended free/Pro/Ultra consumer access in June 2026 in favor of the closed-source
  Antigravity CLI. The open-source CLI keeps working for API-key and Code Assist users, and this config is for
  that CLI, not Antigravity.
- **Windsurf** was rebranded **Devin Desktop** by Cognition in June 2026, and three different official-ish
  sources disagree on the MCP config path. The new default agent, Devin Local, has a separate Skills/Hooks setup
  from the legacy Cascade agent this entry targets (which has neither). Check which file your install actually
  reads before trusting `--write` here.
- **OpenCode** has two live config shapes: the current default nests servers directly under `mcp.<name>`
  (what `connect` writes); a v2 beta nests one level deeper under `mcp.servers.<name>` with an inverted enable
  flag. Writing the first shape into a v2 config lands in the wrong place. `connect` does not detect which one
  you're on.
- **Crush** is mid-migration to a new `crushrc` format set up via `crush mcp add`, not a mergeable file.
  `connect` targets the older `crush.json`/`.crush.json`, which docs call deprecated but still works.
- **Continue** was acquired by Cursor in mid-2026; its hosted product shut down and the open-source repo has had
  no commits since 2026-07-21. The local extension and this config still work — there's just no one behind it
  anymore. Also: Continue's YAML config holds a *list* of servers, not a map, which is part of why it's
  print-only here.
- **Claude Desktop** added Linux support (beta) sometime in 2026; there's no officially documented Linux config
  path yet, so the Linux row above is the community-majority guess, not a confirmed one.
- **VS Code Copilot**'s user-level `mcp.json` path isn't stated as a literal path in VS Code's own docs (only
  "open it from the Command Palette" is) — the path above is the commonly cited one, not an official one.

## `npx skills add` coverage

`npx skills add vicennial/mannerism` (from [vercel-labs/skills](https://github.com/vercel-labs/skills)) covers 17
of the 18 harnesses researched for this project — every one above except **Claude Desktop**, which has no row in
its supported-agent table at all. It copies `SKILL.md` into each harness's own skill folder (the same paths this
page lists, in most cases). For a couple of harnesses — **Kiro** and **Continue** — the path `vercel-labs/skills`
writes to doesn't match what that harness's own docs say it reads (Kiro reads `.kiro/steering/`, not
`.kiro/skills/`; Continue reads `.continue/rules/`, not `.continue/skills/`), so a skill dropped there by that
tool may just sit unused. `mannerism connect` avoids this by using each harness's actually-documented directory.
