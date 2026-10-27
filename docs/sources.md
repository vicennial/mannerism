# Sources

What mannerism can learn from, how each source is found, and how well each one has been tested. The README lists the
short version. For harness setup see [harnesses.md](harnesses.md).

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
| Hermes agent chats (Telegram DMs from a local `state.db`) | list the database file and your user id (`[sources.hermes]`: `enabled`, `paths`, `user_id`); see [host sources](host-sources.md) | beta |
| WhatsApp on Android (an already-decrypted `msgstore.db`) | list the file in `[sources.whatsapp_db]` (`paths`); you provide the decrypted copy, see [WhatsApp databases](whatsapp-db.md) | beta |

Bluesky and Mastodon re-check your recent posts about once a week (`reconcile_interval_s`) and drop anything you deleted (Mastodon also picks up edits; Slack does both, see below). X is billed per post, so it does not: a post you delete or edit on X stays as first stored until you run `mannerism forget --source x`.

*Real-run tested* means tested on transcripts that agent wrote itself during a test run against a stand-in model, not on anyone's real history. *Live-checked* means the fixtures were written from the API documentation and one read-only run against a public organisation account confirmed the real response shapes. *Beta* means tested on synthetic fixtures only. If a source misreads your data, please open an issue describing the
format (never paste your messages).

Some layouts are written from the tools' source code and synthetic fixtures only, with no real-run check: the Continue, Cline and Kilo Code IDE extensions, Cline's classic tasks, Kilo Code's JSON storage and OpenCode's v2 tables. Custom commands, skills and prompt templates that OpenCode, Kilo Code, Crush, Pi and Goose expand before saving cannot be told from typed text, so what they expand to is kept.

Add an export with `mannerism sources add whatsapp ~/Downloads/chat.txt`, or drop it in
`~/.mannerism/inbox/<source>/`.

## Slack

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

## Sessions in throwaway config directories

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

## Your own sources

Text of yours that lives somewhere else can be added without waiting for an adapter:

```
mannerism sources add files ~/writing/essays --name essays --register formal   # .txt, .md or .jsonl; asks you to confirm it is your own text
mannerism sources check feed.jsonl                                              # validate a JSONL file, storing nothing
mannerism sync --watch                                                          # optional: keep syncing in the foreground
```

A folder stays watched, so a script can keep dropping new files into it. Python packages can add whole sources as
plugins; they run with your permissions and are loaded only after `mannerism sources enable NAME`. Text you file under
`public` is kept but is not used for public drafts until it is verified as published. The record format, the
drop-folder pattern, the plugin contract and the privacy rules are in [custom-sources.md](custom-sources.md).
