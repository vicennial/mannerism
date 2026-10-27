# mannerism

**Your AI agents write like you, not like ChatGPT.**

<!-- mcp-name: io.github.vicennial/mannerism -->

mannerism learns how you write from text you already have: your prompts, commits, notes and chats. Before an agent
writes in your name, it gets your style card, real examples of your writing and the rules you taught it by editing.

*Example: the same invoice reply.*

> **Generic agent draft:** Hi there, I hope this email finds you well. I just wanted to reach out about the invoice. Please let me know if you have any questions!
>
> **With mannerism:** the invoice can wait until the 30th, that works on our side

<!-- banner or demo GIF goes here -->

```
npx skills add vicennial/mannerism
```

Then tell your agent: **learn my voice**.

<details>
<summary>Other ways to install</summary>

| Harness | Command |
|---|---|
| Claude Code | `/plugin marketplace add vicennial/mannerism` then `/plugin install mannerism@mannerism` |
| Codex, Gemini CLI, Cursor, VS Code Copilot, Zed, Amp, Goose, Cline, OpenCode, Warp, Factory Droid, Crush, Windsurf, Continue, Kiro, Claude Desktop, Omnigent | `mannerism connect <name> --write` (Goose, Continue and Omnigent use YAML config, so the command prints the snippet for you to paste) |
| Skill only | `npx skills add vicennial/mannerism` (17 of 18 harnesses; not Claude Desktop) |
| Just the CLI | `uvx mannerism init` |

`mannerism connect` with no argument lists every harness it knows and which look installed on this machine.
`mannerism connect <name>` without `--write` prints the exact snippet first, so you can check it before it touches
anything. Config paths, what is tested and known rough edges per harness: [docs/harnesses.md](https://github.com/vicennial/mannerism/blob/main/docs/harnesses.md).

</details>

## How it works

1. **It reads what you already wrote.** Your prompts to coding agents, your commits, notes and chat exports. Your
   words only: not the agent's replies, and not other people's messages.
2. **Your agent gets your voice before it writes.** A style card of your habits, real examples of your writing and
   rules learned from your edits. After it drafts, a lint flags the AI tells you never use yourself.
3. **It learns from every edit you make to a draft.** Fix a draft, send your version, and mannerism pairs the two.
   After repeated, consistent edits it turns them into short rules such as "drops greetings". You can see and delete
   every rule.

Everything is stored on your machine. No account, no API key. Apache-2.0, v0.1.

## Setting up

Saying "learn my voice" starts a first pass that takes about a minute and asks you nothing:

1. mannerism looks for text you already wrote on this machine and shows what it found:

   ```
   source        found            messages
   claude_code   140 sessions     1,900
   codex         60 sessions      700
   git           9 repos          850
   ```

2. It reads your own words only (your prompts, not the agent's replies; your commits, not your colleagues' or an
   agent's own co-authored ones) and writes a first style card for each kind of writing it has enough of.
3. Your agent can then sharpen the cards by reading your writing, and offer a 2-minute blind test: can you tell your
   real messages from the ones it writes?

After that, each new session syncs what you wrote since the last one. The Claude Code plugin does this with hooks. For
Codex, `mannerism connect codex --write` writes only the MCP entry and the skill, so no Codex Stop or SessionEnd hook
is installed today. Details: [sessions in throwaway config directories](https://github.com/vicennial/mannerism/blob/main/docs/sources.md#sessions-in-throwaway-config-directories).

## What it learns from

Prompts from Claude Code, Codex, OpenCode, Cline, Goose, Aider, Continue, Qwen Code, Kilo Code, Crush and Pi, plus git
commit messages, Markdown and Obsidian notes and GitHub comments. Posts and messages from Bluesky, Mastodon, Slack and
X, once you configure them. Exports from ChatGPT, Telegram, WhatsApp, X, email (mbox) and iMessage, Wispr Flow
dictation, and files or plugins of your own.

The full table (how each source is found, and how well it is tested) is in [docs/sources.md](https://github.com/vicennial/mannerism/blob/main/docs/sources.md), with
setup notes for [Slack](https://github.com/vicennial/mannerism/blob/main/docs/sources.md#slack).
More: [your own sources](https://github.com/vicennial/mannerism/blob/main/docs/custom-sources.md), [host sources](https://github.com/vicennial/mannerism/blob/main/docs/host-sources.md) (Hermes) and
[WhatsApp databases](https://github.com/vicennial/mannerism/blob/main/docs/whatsapp-db.md). To keep private writing on one host and use only published examples from
another, see [public-only remote access](https://github.com/vicennial/mannerism/blob/main/docs/public-service.md).

## What your agent gets

```
voice_context("work", task="PR description for the retry fix")
```

returns:

- a **style card**: your habits as rates ("starts lowercase 67% of the time", "rarely uses exclamation marks"),
- **real examples** of your writing in that register, chosen to show your range rather than the same topic,
- **rules learned from your edits**.

Then `voice_lint(draft, "work")` checks the draft against your own numbers and a list of common AI tells. It only
flags tells that you do not use yourself: if you love em dashes, it won't nag you about em dashes.

Registers: `close` (one friend), `group` (group chats), `work`, `formal`, `agent` (prompts to AI agents) and
`public` (posts, READMEs, blogs). Public drafts only ever see your public writing.

## Learning from your edits

Every draft your agent lints is remembered (masked, for 45 days). When you later write something that is an edited
version of a draft (a commit, a post, a message, or simply pasting your fixed version back into the chat), mannerism
pairs them. A register needs at least 5 edited drafts from 3 different conversations before your agent is asked to
write rules from them, so one noisy conversation cannot teach a rule. See and delete rules with `mannerism rules`.

## See your voice

```
mannerism ui
```

opens a read-only page in your browser: your voice cards (each habit with its count and a real example), the rules
learned from your edits, and a few insights. It also has an **Is this you?** review for groups of stored text. The page
listens on 127.0.0.1 only and loads nothing from the network. Details: [docs/ui.md](https://github.com/vicennial/mannerism/blob/main/docs/ui.md).

## Control what it reads

If agents, relays or scripts also type into your sessions (a second Claude account, an orchestrator that sends the
same status message every time), run `mannerism scan` before the first sync. It prints what a sync would read, grouped
by source, account and project, and stores nothing. Exclusion rules (`mannerism exclude add ...`) keep text out for
good, and text that looks machine-typed is held back from your cards until you say it is yours. See
[docs/exclusions.md](https://github.com/vicennial/mannerism/blob/main/docs/exclusions.md).

## Privacy

Your corpus, cards and rules stay in `~/.mannerism` (private files, no telemetry). The only network reads are the
opt-in public-post sources and `gh`, each off until you set `allow_network = true`. The text that `voice_context` and
the setup tasks return goes to your agent, and so to whichever model provider your agent uses, like anything else you
show it. Other people's messages are dropped when reading, and emails, phone numbers, card numbers and secrets are
masked before anything is stored.

Besides the text, each stored text keeps where it came from: the config directory (your home shown as `~`), the
working directory's name and path, how the session was driven and a hash of the session id. Exclusion rules and
`scan` use that.

`mannerism forget --source whatsapp` (or `--all`) deletes a source and everything derived from it, including packets
and rules built from its text, and disables the source so a plain sync cannot quietly bring it back. It cannot recall
text your agent has already seen. `forget --where --project 'scratch-*'` deletes just the texts that match.
`forget --all` also clears the list of config directories the hooks learned. It keeps `config.toml` (your identity,
the switched-off sources, any `[[exclude]]` rules), the exclusion rules you added with `exclude add` and your "it's
me" decisions; remove those with `exclude rm ID` or by editing the file.

## Does it work?

The approach comes from a private prototype the author used for two weeks. On 94 held-out messages, a judge model
mistook plain Claude drafts for the real message 19% of the time; with a style card and examples, 43%. In a blind
test on his own messages, the author still spotted his real message 17 times out of 20, but preferred the
voice-matched draft over plain Claude 13 to 4. So: closer, not identical. Run the blind test on yourself.

## How is this different from…

| | mannerism | AI-tell rewriters (e.g. humanizer) | A style prompt | Fine-tuning |
|---|---|---|---|---|
| Learns *your* voice | yes, from your own text | no, removes generic AI tells | only what you describe | yes |
| Different voices for friends, work, public | yes (registers) | no | by hand | one model per voice |
| Keeps learning from your edits | yes | no | no | retrain |
| Setup | one command, no data prep | none | write it yourself | dataset + training |
| Stored on your machine | yes | yes | yes | depends |

They combine well: mannerism's lint already includes common AI tells (credits below).

## Commands

```
mannerism init                     find sources, read them, write first cards
mannerism status                   what it knows and what's pending
mannerism sync [--quick]           read new text (hooks do this for you)
mannerism context REGISTER         card + rules + examples
mannerism lint [--register R] FILE check a draft
mannerism rules [rm ID]            learned rules
mannerism ui [--port N] [--no-open]  voice cards, insights and the "Is this you?" review in your browser
mannerism sources [add|enable|disable]
mannerism scan [--json] [--samples N]  what a sync would read, grouped; stores nothing
mannerism exclude add|list|rm      never store matching text (--source --account --project --kind --text --group [--force])
mannerism review [keep GROUP|rescan]  texts held back as machine-typed
mannerism forget --source S | --drafts | --all | --where <same flags as exclude add>
mannerism mcp                      run the MCP server (stdio)
```

Design notes and internals: [docs/design.md](https://github.com/vicennial/mannerism/blob/main/docs/design.md).

## Credits

The AI-tell list adapts entries from [blader/humanizer](https://github.com/blader/humanizer) (MIT) and
[sam-paech/slop-forensics](https://github.com/sam-paech/slop-forensics) (MIT); see `NOTICE`. Wikipedia's
"Signs of AI writing" was used as a checklist. Example selection follows research showing that diverse examples
from the same register beat topic-matched ones for style imitation (arXiv 2509.14543); edit-learned rules follow
CIPHER (arXiv 2404.15269).

## License

Apache-2.0
