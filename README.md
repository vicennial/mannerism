# mannerism

**Your agents write in your voice.**

<!-- mcp-name: io.github.vicennial/mannerism -->

mannerism learns how you write from text you already have: the prompts you type to Claude Code and Codex, your
commit messages, notes, and chat or email exports. It keeps learning as you write, and from every edit you make to
an agent's draft. Before an agent writes in your name, it gets your style card, real examples of your writing and the
rules it learned from your edits. After it drafts, a lint flags the AI tells you never use.

Everything is stored on your machine. No account, no API key.

Keep private writing on one host and use only published examples from another:
see [public-only remote access](docs/public-service.md). Existing Voice Kit users
can [import their corpus and feedback](docs/legacy-import.md) without changing the source.

<!-- demo gif goes here -->

## Install

| Harness | Command |
|---|---|
| Claude Code | `/plugin marketplace add vicennial/mannerism` then `/plugin install mannerism@mannerism` |
| Codex, Gemini CLI, Cursor, VS Code Copilot, Zed, Amp, Goose, Cline, OpenCode, Warp, Factory Droid, Crush, Windsurf, Continue, Kiro, Claude Desktop, Omnigent (MCP snippet is print-only: paste it into your agent YAML) | `mannerism connect <name> --write` |
| Any of the above, skill only | `npx skills add vicennial/mannerism` (17 of 18 harnesses; not Claude Desktop) |
| Just the CLI | `uvx mannerism init` |

`mannerism connect` with no argument lists every harness it knows and which ones look installed on this machine.
`mannerism connect <name>` (no `--write`) prints the exact snippet first, so you can check it before it touches
anything. See [`docs/harnesses.md`](docs/harnesses.md) for the full table (config paths, what's tested vs.
documented-only, and known rough edges per harness).

Then tell your agent: **"learn my voice"**.

## What happens when I say "learn my voice"?

1. mannerism looks for text you already wrote on this machine and shows what it found:

   ```
   source        found            messages
   claude_code   140 sessions     1,900
   codex         60 sessions      700
   git           9 repos          850
   ```

2. It reads your own words only (your prompts, not the agent's replies; your commits, not your colleagues' or an
   agent's own co-authored ones) and writes a first style card for each kind of writing it has enough of. This
   takes about a minute and asks you nothing.
3. Your agent can then make the cards sharper by reading through your writing, and offer a 2-minute blind test:
   can you tell your real messages from the ones it writes?

From then on it updates itself. Each new session syncs what you wrote since the last one.

## What does it learn from?

| Source | How | Status |
|---|---|---|
| Claude Code prompts | found automatically | tested on real data |
| Codex prompts | found automatically | tested on real data |
| OpenCode prompts | found automatically | real-run tested |
| Cline prompts | found automatically | real-run tested (CLI) |
| Goose prompts | found automatically | real-run tested |
| Aider prompts | found automatically | real-run tested |
| Continue prompts | found automatically | real-run tested (CLI) |
| Qwen Code prompts | found automatically | real-run tested |
| Kilo Code prompts | found automatically | real-run tested (CLI) |
| Crush prompts | found automatically | real-run tested |
| Pi prompts | found automatically | real-run tested |
| git commit messages | found automatically (repos you work in) | beta |
| Markdown / Obsidian notes | point at a folder | beta |
| GitHub comments | opt in (`mannerism sources enable gh`) | beta |
| Bluesky posts | found when configured (`[sources.bluesky]`: `handle`, `allow_network = true`) | live-checked |
| Mastodon posts | found when configured (`[sources.mastodon]`: `account`, `allow_network = true`) | live-checked |
| Slack messages (your own, user token) | found when configured (`[sources.slack]`: `allow_network = true`, token in `SLACK_USER_TOKEN`) | beta |
| X posts (official API, billed per post) | found when configured (`[sources.x]`: `username`, `allow_network = true`, token in `X_BEARER_TOKEN`) | beta |
| ChatGPT export | drop the zip in `~/.mannerism/inbox/chatgpt/` | beta |
| Telegram export | `result.json` | beta |
| WhatsApp chat export | `.txt` files | beta |
| X archive | the archive zip | beta |
| Email (mbox, e.g. Gmail Takeout) | `.mbox` | beta |
| iMessage (macOS) | needs Full Disk Access | beta |
| Wispr Flow dictation | found automatically | beta |
| Hermes agent chats (Telegram DMs from a local `state.db`) | list the database file and your user id (`[sources.hermes]`: `enabled`, `paths`, `user_id`); see [host sources](docs/host-sources.md) | beta |
| WhatsApp on Android (an already-decrypted `msgstore.db`) | list the file in `[sources.whatsapp_db]` (`paths`); you provide the decrypted copy, see [WhatsApp databases](docs/whatsapp-db.md) | beta |

Bluesky and Mastodon re-check your recent posts about once a week (`reconcile_interval_s`) and drop anything you deleted (Mastodon also picks up edits; Slack does both, see below). X is billed per post, so it does not: a post you delete or edit on X stays as first stored until you run `mannerism forget --source x`.

*Real-run tested* means tested on transcripts that agent wrote itself during a test run against a stand-in model, not on anyone's real history. *Live-checked* means the fixtures were written from the API documentation and one read-only run against a public organisation account confirmed the real response shapes. *Beta* means tested on synthetic fixtures only. If a source misreads your data, please open an issue describing the
format (never paste your messages).

Some layouts are written from the tools' source code and synthetic fixtures only, with no real-run check: the Continue, Cline and Kilo Code IDE extensions, Cline's classic tasks, Kilo Code's JSON storage and OpenCode's v2 tables. Custom commands, skills and prompt templates that OpenCode, Kilo Code, Crush, Pi and Goose expand before saving cannot be told from typed text, so what they expand to is kept.

Add an export with `mannerism sources add whatsapp ~/Downloads/chat.txt`, or drop it in
`~/.mannerism/inbox/<source>/`.

### Slack

mannerism can keep learning from your own Slack messages. It is off until you turn it on. It stores only text you
wrote (masked, stored on your machine) and ignores other people's messages, including any that Slack puts around your
own in a search result.

1. Create a Slack app for your workspace at <https://api.slack.com/apps> ("From scratch"). Under *OAuth & Permissions*
   add one **User Token Scope**, `search:read`, and install the app to your workspace. (Some workspaces need an admin
   to approve the app.) Copy the *User OAuth Token*; it starts with `xoxp-`. A bot token does not work, and browser
   session tokens (`xoxc-`) are refused.
2. Export it in the environment of the shell that runs `mannerism`: `export SLACK_USER_TOKEN=xoxp-...`. The token is
   read from the environment only. It is never written to the config, the corpus, the cursor, a log or an error.
3. `mannerism sources enable slack`, then `mannerism sources check slack` to test the token. The check calls Slack's
   `auth.test` and reads no message.
   The first successful read binds mannerism to that Slack user and workspace, and it refuses a token for anyone
   else. If you started with the wrong token, run `mannerism forget --source slack` (it deletes what was stored for
   Slack and the binding), then set the right token.

It searches Slack for `from:<you>` (read-only; the only host it contacts is `slack.com`), keeps messages you typed, and
drops everything else: other people's messages, bots and integrations, join/topic notices, file-only posts, quoted
text, code blocks and forwarded messages. `@person` and `#channel` mentions become neutral tokens. The first run reads
your latest 200 messages; later runs read what is new, plus a day of overlap in case Slack's search index lags. About
once a week (`reconcile_interval_s`) it re-checks recent messages: edits update the stored text and messages you
deleted are dropped. Channel messages go into the `work` register.

**Direct messages are off by default** because they are the most personal text you write. Set `include_dms = true` in
`[sources.slack]` to include DMs and group DMs (`dm_register = "close"` files them under your close-friends register
instead). `include_channels` and `exclude_channels` take channel names or ids. Your agent's drafts are paired with
the message you actually sent in the same register, so edits you make in Slack teach the rules like any other source.

```toml
[sources.slack]
enabled = true
allow_network = true
include_dms = false                 # default
exclude_channels = ["random", "C0123456789"]
```

### Sessions in throwaway config directories

If you run Claude Code or Codex with a temporary `CLAUDE_CONFIG_DIR` / `CODEX_HOME` (a script, a container, an
orchestrator that creates a directory per session), its history lives where no default points and may be deleted
when the session ends. The Claude Code plugin's hooks fix that without any setting: every hook payload carries the
session's `transcript_path`, so mannerism remembers that directory (in `~/.mannerism/hook-roots.json`, owner-only,
at most 64 entries, entries for directories that no longer exist are dropped after 30 days) and reads it on every
sync. A remembered directory is used only if you own it and nobody else can write to it.

- `Stop` (after every turn): only for a transcript under a directory that is not `~/.claude` / `~/.codex` and not
  listed in `[claude_code].dirs` / `[codex].dirs`, because those are read by every ordinary sync anyway. For such a
  throwaway directory it stores and screens that one transcript in a detached background process (60 s hard limit)
  and does nothing else, so a turn costs milliseconds, not a rebuild of your cards.
- `SessionEnd` (the session exits): the same one-file sync for any directory, plus one refresh of the cards and
  statistics for what was stored. If that refresh does not finish, the store stays marked "refresh pending" and the next
  ordinary sync does it.

The hooks print nothing, make no network calls and do nothing until `mannerism init` has run, when the source is
switched off (checked again when the sync starts, so `forget --source` cannot be undone by a hook that was already
running), or after `forget --all`. Install the Claude Code plugin to get them (`mannerism connect` writes the MCP
entry and skill, not hooks). **Codex:** no Codex Stop or SessionEnd hook is installed today (nor a SessionStart or
UserPromptSubmit one): the Codex plugin manifest declares no hooks and `mannerism connect codex --write` only writes
the MCP entry and the skill. `mannerism hook ...` understands Codex's payload, so you can wire it into Codex's hook
config by hand (then trust it with `/hooks`), but until you do, add a throwaway `CODEX_HOME` to `[codex].dirs` if you
want it read.

### Your own sources

Text of yours that lives somewhere else can be added without waiting for an adapter:

```
mannerism sources add files ~/writing/essays --name essays --register formal   # .txt, .md or .jsonl; asks you to confirm it is your own text
mannerism sources check feed.jsonl                                              # validate a JSONL file, storing nothing
mannerism sync --watch                                                          # optional: keep syncing in the foreground
```

A folder stays watched, so a script can keep dropping new files into it. Python packages can add whole sources as
plugins; they run with your permissions and are loaded only after `mannerism sources enable NAME`. Text you file under
`public` is kept but is not used for public drafts until it is verified as published. The record format, the
drop-folder pattern, the plugin contract and the privacy rules are in [docs/custom-sources.md](docs/custom-sources.md).

## A busy machine: several accounts, orchestrators, scripts

If agents, relays or scripts also type into your sessions (a second Claude account, an orchestrator that sends the
same status message every time), say so before the first sync:

```
mannerism scan
```

`scan` reads exactly what a sync would read and **stores nothing** (no texts, no cursors, no config). It prints, per
source, account and project: how many texts, the date range, what share looks machine-typed, a few masked samples, and
a group id for each group. It ends with copyable commands for the noisiest groups, for example:

```
mannerism exclude add --source claude_code --account '~/.claude-work'
```

`scan` also takes `--json`, `--samples N` (0 to 20), `--source S`, `--network` (sources you already allowed) and
`--budget SECONDS`. It skips sources your config switches off (unless you name them with `--source`), and it never
calls the X API, even with `--network`, because that API bills for every read: it prints that X was skipped. It
creates no file; if the store has a write-ahead log without its index file it reads the store without the log and says so.

**Exclusion rules** keep text out for good. A rule matches when all its fields match: `--source`, `--account` (a glob on
the config or home directory, `~` is your home), `--project` (a glob on the working directory's name or path), `--kind`
(how the session was driven: `interactive`, `ide`, ...), `--text` (a regular expression) and `--group` (an id from
`scan`). `exclude add` first says how many stored texts it would remove (`--dry-run` stops there), then deletes them and
everything derived from them (examples, edit pairs, rules that only they supported), and no later sync stores a match.
`exclude list` shows the rules; `exclude rm ID` removes one (text it kept out is not read again). `forget --where` takes
the same flags and deletes without keeping a rule. A label with a masked part (`~/.claude-[secret]`) works as it is
shown, and the commands `scan` prints escape the brackets, so they can be pasted. A text rule that takes too long on a
text counts as a match at ingest (the text is left out), never as a pass; a pattern that could run for ever
(`(a+)+`, `(a|aa)+`) is refused when you add it, and a stored rule that no longer compiles is skipped with a warning. You can also write rules by hand in `config.toml`:

```toml
[[exclude]]
account = "~/.claude-work*"

[[exclude]]
project = "scratch-*"
kind = "interactive"
```

**Machine-typed text** is detected without a model: wrapper tags, JSON blobs, `key: value` status blocks, the same
opening words in many sessions, long blocks that arrived faster than anyone types. Each text gets a score from 0 to 1
(reasons recorded). Clearly machine text is not stored; unsure text is stored but **held**: kept out of your cards and
examples until you say it is yours. Short prompts ("lgtm", "fix the test") are never judged. `mannerism review` lists
what is held; `mannerism review keep GROUP` says it is you (and that is remembered); `mannerism exclude add --group GROUP`
says it is not. A group you said is yours is never dropped or held again. A text that was stored and later scores as
machine-typed is held, never deleted: only new texts are dropped when they are read.

**Older texts without an origin.** Texts stored before mannerism recorded where they came from sit in one group per
source that can hide texts from many places, so `review` and the page mark it "mixed", and `exclude add --group` refuses
it (add `--force` to remove it as it is anyway). `mannerism review rescan` reads those sources again from the start,
writes the origin into the texts it finds already stored (no duplicates, nothing new is added, no network or plugin
source), and then scores the texts that were never scored; the old group splits into groups you can judge one by one.

## How does it learn from my edits?

Every draft your agent lints is remembered (masked, for 45 days). When you later write something that is an edited
version of a draft (a commit, a post, a message, or simply pasting your fixed version back into the chat),
mannerism pairs them. After a few edits it asks your agent to turn them into short rules, such as "drops greetings"
or "never says 'leverage'". You can see and delete every rule: `mannerism rules`.

## See your voice

```
mannerism ui
```

opens a page in your browser with your voice cards (each habit with its count and a real example from your own
writing), the rules learned from your edits (with a draft-versus-what-you-sent view), and a few insights: edits per
draft over time, how often you send a draft as written, the AI tells the lint caught, and words per message by
register. Where there is too little stored data it says "not enough data yet" instead of showing a number.

The page also has an **Is this you?** section. It lists groups of stored texts (the same source, account, project and
kind of session, or a message that repeats across many sessions) with counts, date range and a few masked examples,
groups with machine-typed texts held back first. For each group:

- **Not me** asks you to confirm ("Delete 17 stored messages from this group?"), then deletes those messages and
  everything learned from them, and skips the group from now on. This cannot be undone.
- **It's me** releases the group's held texts into your voice and remembers the answer, so later texts from the group are
  never held.

With nothing held, the section says so and offers to list all groups. The same two actions are in the terminal:
`mannerism exclude add --group ID` and `mannerism review keep ID`.

Everything shown is read from files stored on your machine. The page listens on 127.0.0.1 only (a random port, or
`--port`), checks the `Host` header, needs a token that is new on every launch, sends no CORS headers, and loads no
script, font or image from the network, so it works offline. It is read-only except for those two actions, which are
the only POST requests it accepts. Each one also needs the page's own session cookie, a second per-launch token sent in
a header, an `Origin` that is exactly the page's address and a JSON body, so another website open in your browser cannot
trigger them. The second token is given out once per launch, to the first tab that loads the page; a second tab shows the
groups but has no buttons (restart `mannerism ui` to review in another tab). A program on your computer that copies the
page's cookie could still use the buttons, so do not run `mannerism ui` on a shared machine. `--no-open` prints the address without opening a browser. After `mannerism forget`, reload the page to see
the data gone.

## What does it give the agent?

```
voice_context("work", task="PR description for the retry fix")
```

returns:

- a **style card**: your habits as rates ("starts lowercase 67% of the time", "rarely uses exclamation marks"),
- **5–8 real examples** of your writing in that register, chosen to show your range rather than the same topic,
- **rules learned from your edits**.

Then `voice_lint(draft, "work")` checks the draft against your own numbers and a list of common AI tells. It only
flags tells that you do not use yourself: if you love em dashes, it won't nag you about em dashes.

Registers: `close` (one friend), `group` (group chats), `work`, `formal`, `agent` (prompts to AI agents) and
`public` (posts, READMEs, blogs). Public drafts only ever see your public writing.

## What leaves my machine?

Your corpus, cards and rules stay in `~/.mannerism` (private files, no telemetry; the only network reads are the opt-in public-post sources above and `gh`, each off until you set `allow_network = true`). The text that
`voice_context` and the setup tasks return goes to your agent, and so to whichever model provider your agent uses,
like anything else you show it. Other people's messages are dropped when reading, and emails, phone numbers, card
numbers and secrets are masked before anything is stored.

`mannerism forget --source whatsapp` (or `--all`) deletes a source and everything derived from it — including
packets and rules built from its text — and disables the source, so a plain sync can't quietly bring it back. It
can't recall text your agent has already seen. `forget --where --project 'scratch-*'` deletes just the texts that match.
`forget --all` also clears the list of config directories the hooks learned. It keeps `config.toml` (your identity, the
switched-off sources, any `[[exclude]]` rules), the exclusion rules you added with `exclude add` (`exclude list` shows
them) and your "it's me" decisions; remove those with `exclude rm ID` or by editing the file.

Besides the text, each stored text keeps where it came from: the config directory (your home shown as `~`), the
working directory's name and path, how the session was driven and a hash of the session id. That is what exclusion
rules and `scan` use. It is stored on your machine with the rest of the corpus · no new service · no API key.

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

## Credits

The AI-tell list adapts entries from [blader/humanizer](https://github.com/blader/humanizer) (MIT) and
[sam-paech/slop-forensics](https://github.com/sam-paech/slop-forensics) (MIT); see `NOTICE`. Wikipedia's
"Signs of AI writing" was used as a checklist. Example selection follows research showing that diverse examples
from the same register beat topic-matched ones for style imitation (arXiv 2509.14543); edit-learned rules follow
CIPHER (arXiv 2404.15269).

## License

Apache-2.0
