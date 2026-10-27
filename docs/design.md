# mannerism — design (v0.1)

Status: design for the first public release. Written 2026-10-21. The prototype it generalises ran privately for one
user for about two weeks (blind test: a judge preferred the voice-matched draft 43% vs 19% for plain drafts; the
user spotted real-vs-generated in 17 of 20 pairs and preferred the matched drafts 13 to 4).

## 1. What it is

mannerism makes coding and chat agents (Claude Code, Codex, Cursor, Gemini CLI, anything that speaks MCP) write in
**your** voice. It reads text you already wrote, keeps learning from new text and from your edits to its drafts,
and gives the agent three things right before it writes in your name:

1. a short **style card** for the register (who the text is for),
2. 5–8 **real examples** of your writing in that register,
3. **rules learned from your edits** ("you delete greetings", "you never write 'leverage'").

After the agent drafts, `lint` checks the draft against your own statistics and a list of common AI tells. It flags only tells
that you do not use yourself.

### Non-goals

- No hosted service, no account, no telemetry. Everything is local files.
- No LLM API key. All model work (labelling, card writing, rule distillation, eval) is done by the host agent through
  MCP tools. mannerism prepares bounded work packets and stores the agent's answers.
- No fine-tuning. Research (and the prototype) show card + examples gets most of the gain at zero training cost.
- No posting or sending anything anywhere. mannerism reads and advises; the user sends.

## 2. Design principles

| Principle | Consequence |
|---|---|
| Reuse existing data, no interview | `init` discovers what is already on disk (agent transcripts, git, notes, exports) and works with zero questions. |
| Useful after one command | `init` ends with a deterministic stats card per register, usable at once. Agent-written cards are an upgrade, not a gate. |
| Continuous, cheap ingestion | Per-source cursors; `sync --quick` runs from the SessionStart hook in the background, in a few seconds. |
| Edits are the best signal | Every linted draft is logged; any later text you write (sent mail, post, commit, chat message, the next prompt) is matched against recent drafts. |
| Own text only | Adapters drop other people's messages before storage. Chat context is kept only as short masked lines. |
| Private by default | One directory, mode 0700, files 0600. Public drafts draw only on text you already published. |
| Agent-agnostic | MCP tools + CLI + a skill file. Hooks where the harness has them; everything also works without hooks. |

## 3. Surfaces and onboarding

```
Claude Code   /plugin marketplace add vicennial/mannerism
              /plugin install mannerism@mannerism          → skill + MCP + hooks
Codex, Gemini, Cursor, OpenCode …
              npx skills add vicennial/mannerism            → skill
              + MCP config: command "uvx", args ["mannerism", "mcp"]
Anyone        uvx mannerism init                            → CLI only
```

Then the user says **"learn my voice"**. The skill makes the agent call `voice_status` → `voice_setup` (which runs
`init`): discovery, a checklist of found sources, a quick sync, deterministic cards. Then, optionally and in the
background, the agent works through the queue (`voice_work_next` / `voice_work_submit`): labels ambiguous text,
writes richer cards, and offers a 2-minute blind test.

### MCP tools (stdio, FastMCP from the official `mcp` SDK; logs to stderr only)

| Tool | Purpose |
|---|---|
| `voice_context(register, task="")` | Card + learned rules + 5–8 diverse examples for a register. Call before writing in the user's name. |
| `voice_lint(text, register, is_draft=True)` | Findings (tells, out-of-range stats, public-leak entities). Logs drafts for edit capture. |
| `voice_feedback(draft, final, register)` | Explicit edit pair (when the user pastes their edited version). |
| `voice_status()` | Sources, counts per register, last sync, pending work, card state. |
| `voice_setup(sources=None)` | First run: discover + enable sources + quick sync + stats cards. Idempotent. |
| `voice_work_next(kind=None)` | Next bounded packet: `label`, `card`, `distill`, `blind`. |
| `voice_work_submit(kind, packet_id, result)` | Stores the agent's answer for a packet (validated against the packet's schema). |

### CLI (`mannerism …`)

`init`, `sources [enable|disable NAME | add files PATH --name N | check FILE.jsonl]`, `sync [--quick] [--force] [--watch [--every S]] [--source S]`, `status`, `context REGISTER`,
`lint [--register R] [--no-log] FILE|-`, `feedback DRAFT FINAL`, `work next|submit`, `blind`, `eval`, `mcp`,
`hook session-start|prompt|stop|session-end`, `export-card REGISTER`, `forget [--source S | --drafts | --all | --where <rule flags>]`,
`ui [--port N] [--no-open]`, `scan`, `exclude add|list|rm`, `review [keep GROUP | rescan]` (§13).

### Hooks

| Hook | Claude Code | Codex | Action |
|---|---|---|---|
| SessionStart | yes (plugin) | not installed | `mannerism hook session-start`: spawns a detached `sync --quick`, prints nothing. |
| UserPromptSubmit | yes (plugin) | not installed | `mannerism hook prompt`: reads `.prompt`, `.cwd` and `.transcript_path` from stdin JSON; if the prompt closely matches a recent draft, and no exclusion rule matches it, records the edit. Never blocks, never prints, exits 0 on any error. Time budget 300 ms. |
| Stop | yes (plugin) | not installed | `mannerism hook stop`: for a transcript under a root that is neither a default nor a configured one, registers the root and starts a detached single-file sync that ingests and screens only (below). Otherwise returns at once. |
| SessionEnd | yes (plugin; 1.5 s budget) | not installed | `mannerism hook session-end`: registers the root and starts the same single-file sync for any root, with one bounded refresh. |

**What is installed.** Only the Claude Code plugin ships hooks (`hooks/hooks.json`). The Codex plugin manifest
declares none and `mannerism connect codex --write` writes the MCP entry and the skill only, so no Codex Stop or
SessionEnd hook is installed today, nor a Codex SessionStart or UserPromptSubmit one. This is an accepted deviation
from the original plan (spec item 5: "`connect` installs the extra hook event where the harness supports it"). The
`hook` command and `hookroots` already understand Codex's payload (`sessions/YYYY/MM/DD/rollout-*.jsonl`), so a user
can wire them into Codex by hand; until then a throwaway `CODEX_HOME` is read only if it is listed in `[codex].dirs`.

`session-start` also registers the root, then starts its usual quick sync, so a throwaway root is already read.

**Capture through the hook's own transcript path (`hookroots.py`).** The payloads of both harnesses carry
`transcript_path` (Claude Code: [hooks reference, "Common input fields"](https://code.claude.com/docs/en/hooks), a
string; Codex: [hooks docs, "Common input fields"](https://developers.openai.com/codex/hooks), `string | null`, the
rollout file). The root is derived from it; `cwd` and `prompt` are the only other fields read (the prompt hook). The path layout tells the harness apart and gives the
config root: `<root>/projects/<slug>/<id>.jsonl` (Claude Code) or `<root>/sessions/YYYY/MM/DD/rollout-*.jsonl`
(Codex). `hookroots.record()` stores `{harness: {root: last_seen}}` in `hook-roots.json` in the data directory
(temp file + fsync + `os.replace`, mode 0600, read capped at 256 KB, at most 64 roots per harness, a root that no
longer exists and was last seen over 30 days ago is pruned; a root that exists is kept; the write is skipped when
the root was recorded under a day ago). A registry entry that is not a finite number is dropped on its own (it never
fails the load). The `claude_code` and `codex` adapters add `hookroots.registered()` to `[<source>].dirs` and the
env/default dirs and de-duplicate by real path; a vanished root, a root the current user does not own and a root the
group or others can write to are skipped silently (anyone who could write there could plant text that would be read as
the user's own).

*Which end event.* `Stop` runs after every completed response, so the last turn of a headless `claude -p` run is
always followed by one, and it does not depend on a clean exit. It does not run when the user interrupts a turn
(Claude Code docs), and `SessionEnd` has a 1.5 s default budget that a plugin hook cannot raise, and runs only on a
clean exit. Both are installed for Claude Code; each returns at once and leaves the work to a detached child
`python -m mannerism.sync --source S --file PATH [--refresh]` (new session, no std streams).

*Cost per turn.* `Stop` fires every turn, so it must not rebuild anything. (1) The hook acts only for transcripts under
a root that is neither the default directory nor in `[<source>].dirs` (`hookroots.is_standing_root`; the caller's own
`CLAUDE_CONFIG_DIR` is not "standing": inside a hook it is the session's, possibly throwaway, directory). Ordinary syncs
already read the others. (2) `sync(files=[...])` filters the discovered locations to that real path and the sources to
those switched on in the config as read after the lock is taken, runs the normal read, exclusion rules and screening,
and stops: no bank, statistics, cards, edit capture or work queue. What it stored is recorded in `meta`
(`refresh_pending`: new unit ids, edited units, kept drafts, registers); privacy-relevant invalidation (agent cards
built on deleted text, test pages) still happens at once. The next normal sync (SessionStart's quick sync included) or a
file sync with `refresh` does the rest and clears the mark; if it fails or is killed the mark stays. `SessionEnd` passes
`--refresh`: one refresh per session, bounded by the child's hard limit. The pending lists keep at most 20,000 ids each;
beyond that the texts are kept but edit capture for the overflow is skipped. The child retries a busy sync lock for 20 s
and is killed by `SIGALRM` after 60 s (the units + cursor transaction rolls back; the next sync resumes); the regular
expression guard (§13) uses no signal in the calling process, so it cannot cancel that limit. The hook does nothing when the source is
disabled (and the child checks again once it holds the lock, so a `forget --source` that raced a hook that was already
running cannot bring the source back), after `forget --all`, before `init`, or when the file is already gone. A registry
that cannot be written does not stop the sync. Limit: if something deletes the directory within the child's start-up
time (a few hundred ms) of the last `Stop`, that turn is lost; `SessionEnd` gives a second chance when the harness
exits cleanly.

*Prompt hook.* `capture.match_prompt` applies the exclusion rules before it pairs a prompt: the harness and config
directory come from `transcript_path`, and the project comes from `cwd`. A bounded read of Claude Code's recent user
lines or Codex's session metadata supplies the session kind, so IDE origin-group and kind rules work too. If the kind
cannot be established, capture is deferred when a potentially matching rule depends on it. A timed-out text rule
counts as a match.

Without hooks, the MCP server syncs lazily: if the last sync is older than 6 h, the first tool call of a session
starts a background sync.

## 4. Data model

Home: `$MANNERISM_HOME` or `~/.mannerism/` (0700).

```
~/.mannerism/
  config.toml          identity + enabled sources + paths (written by init, user-editable)
  corpus.db            units, cursors, labels, bank, drafts, edits, rules, packets (SQLite, WAL, 0600)
  cards/<register>.md  current card (agent-written or stats-generated)
  stats/<register>.json  per-register stylometric distribution (lint calibration)
  eval/                blind-test pages and results
  sync.lock            flock for sync
```

### Identity (`[me]` in config.toml)

`names`, `emails`, `handles` (per service), `phone_suffixes` (last digits only, for WhatsApp/iMessage self-detection).
`init` fills it from `git config user.name/email`, `gh api user` (login), and asks nothing; the agent may add more
from what the user says. Every adapter decides "own" with the strongest signal it has (role/flag/id first, name match
last).

### Unit

One coherent piece of text the user wrote (prototype `Unit`, generalised):

```
unit_id      sha1(source:source_ref)[:20]
source       adapter name
source_ref   stable native id (or first..last ids for merged bursts)
thread_id    conversation/thread key (hashed when it is an address)
audience     close | group | colleague | service | agent | public | self | unknown
audience_ref hashed counterpart id (never a raw address/number)
ts           UTC
kind         message | email | prompt | commit | comment | post | reply | note | dictation
text         own text, PII-masked (emails, phones, IBAN/cards, secrets/tokens)
context      ≤3 preceding lines, masked role + truncated text (chat sources only)
meta         per-source extras (app bundle id for dictation, repo for git …)
account      (v4) config/home dir the text came from, $HOME shown as "~"   (Claude Code, Codex)
project      (v4) working directory basename;   project_path: the directory, $HOME as "~"
session_kind (v4) how the session was driven: interactive | ide | ... (the adapter's label)
session      (v4) hash of the session id
human_score  (v4) 0..1 machine-typed score;  held: 1 = kept out of the bank until confirmed;  template: opening-words hash
```

### Registers

Six generation registers: `close` (1:1 friends/family), `group` (group chats), `work` (colleagues, issues, PRs,
Slack), `formal` (external email, applications), `agent` (prompts to AI agents), `public` (posts, blog, README).
Plus `skip` for text that is not usable style evidence (pastes, code, logs, signatures-only).

**Metadata first.** Each adapter assigns a default register from source + audience (table below). Only units whose
default is `?` go to the agent label queue, newest first, in batches of 60 short items. Unlabelled units are left out
of the bank. The bank works with whatever is labelled.

| Source | Default register |
|---|---|
| Claude Code, Codex, ChatGPT, other agent transcripts | `agent` |
| git commits, gh comments | `work` |
| X archive (own posts), blog/markdown marked public | `public` |
| Telegram/WhatsApp/iMessage 1:1 | `close` (or `work` if the contact is in `[contacts].work`) |
| group chats | `group` |
| email (sent) | `?` (work vs formal vs close) |
| notes, dictation | `?` (dictation `app` bundle id narrows it: chat app → close/group, IDE → agent) |

### Tables (corpus.db)

`units`, `ingest_runs`, `cursors(source, location, cursor_json, updated_at)`, `labels(unit_id, register, quality,
language, labeller, labelled_at)`, `bank` (+ FTS5 for tie-break only), `drafts(draft_id, ts, register, origin,
sha256 UNIQUE, text)`, `edits(edit_id, ts, register, draft, final, status, origin, unit_id NULL)`,
`rules(rule_id, register, text, support, contradicted, created_at, last_seen, edit_ids_json)`,
`packets(packet_id, kind, created_at, payload_json, schema_json, done_at)`, `meta(key, value)` (schema version, last
sync, holdout cutoff), and from schema 4 `exclusions(rule_id, source, account, project, session_kind, text, grp,
created_at)` and `group_decisions(group_id, decision, decided_at)`.

## 5. Sources (v0.1)

All adapters are written in-house with the standard library only. Several community parsers have unclear or
conflicting licences, so the project bundles none of them. Each adapter implements:

```python
class Source(Protocol):
    name: str
    tier: Literal["auto", "export", "macos"]
    def discover(self, cfg) -> list[Location]            # paths found on this machine
    def read(self, loc, cursor: dict | None, me) -> tuple[Iterable[Unit], dict]   # units + new cursor
```

| Source | Where | Own text | Cursor | Notes |
|---|---|---|---|---|
| `claude_code` | `~/.claude/projects/*/*.jsonl` (+ `CLAUDE_CONFIG_DIR`, `[claude_code].dirs`, config roots registered by hooks) | `type=user`, `message.role=user`, text blocks only; drop `isMeta`, `isSidechain`, tool results, attachment lines, command/hook wrappers (`<command-…>`, `<system-reminder>`) | byte offset + inode per file | pasted blocks > N lines → `skip` |
| `codex` | `~/.codex/sessions/**/rollout-*.jsonl` (+ `CODEX_HOME`, `[codex].dirs`, config roots registered by hooks) | `response_item` / `message` with `role=user`; drop injected instructions (`<user_instructions>`, `<environment_context>`, AGENTS.md text) | byte offset per file | |
| `git` | repos under `[git].roots` (default: discovered from agent-transcript cwds) | author email ∈ me.emails | last seen SHA per repo + ts | drop merge commits, trailers, bot authors |
| `gh` | `gh api` (only if `gh auth status` ok) | `user.login == me` | `since=` updated_at watermark | issue + PR review comments; strip quoted `>` lines |
| `markdown` | `[markdown].dirs` (Obsidian vaults auto-found via obsidian.json) | all (folder is own) | mtime + sha per file | strip frontmatter, code fences, wikilink syntax |
| `files` | `~/.mannerism/inbox/files/` (the built-in), or `[sources.<name>] adapter = "files"` with `path` (file or folder, watched) | everything in it (the user confirmed it is theirs); `.txt`/`.md` one unit per file (chunked ~300 words), `.jsonl` schema-v1 records (`upsert`/`delete`, unknown fields rejected) | per file: generation (inode + head/tail hash) + byte offset + line; `.txt`/`.md` signature + sha | register `public` is never `meta.public`; a delete removes the unit and its derived rows; user plugins (`mannerism.sources.v1`) and network sources: see `docs/custom-sources.md` |
| `chatgpt` | `conversations.json` from export zip | `author.role=user`, walk `current_node` chain | full re-read, dedupe by id | |
| `telegram` | `result.json` | `from_id == me` (auto-detected: the id with most messages across personal chats) | max message id per chat | text may be an entity array |
| `whatsapp` | exported `.txt` (iOS + Android formats, locale date orders) | sender name ∈ me.names (auto: the one name present in every chat) | dedupe by (ts, text) | `<Media omitted>` dropped |
| `x_archive` | `data/tweets.js` (+ `note-tweet.js`) | all except retweets; quote text kept | dedupe by `id_str` | strip `window.YTD.*` prefix |
| `bluesky` | `public.api.bsky.app` `getAuthorFeed` (`posts_with_replies`); config `handle`, `allow_network` | `post.author.did` == the DID bound at first run (handle re-resolved at each new scan; a change stops the source); no reposts; quote posts keep own words; reply = leading mentions dropped + one masked parent line | `high_water` {uri, ts} + in-progress `scan` {token, candidate, fetched} + `seen` {rkey: ts} + `reconciled_at`; the scan reads on until items are 48 h older than the mark (the feed sorts by about min(createdAt, indexedAt), so a backdated post lands below newer ones); first run bounded by `initial_posts` (200) | link facets that only show a URL removed; mentions → `@[user]`; opt-in network source, no credentials; deletes: every `reconcile_interval_s` (7 days) the latest 200 items are re-read and missing refs confirmed with `getPosts` are reported in `_delete_refs` (Bluesky has no edits) |
| `mastodon` | `https://<instance>/api/v1/accounts/:id/statuses?exclude_reblogs=true`; config `account` (`@user@instance`), `allow_network` | `account.id` == the id bound by `accounts/lookup`; visibility `public`/`unlisted` only; no boosts; reply = leading mention links dropped + one masked parent line (≤ 10 extra fetches per read) | `high_water` status id (`since_id`; ids follow creation order, so no overlap) + `scan` token (`max_id`) + `seen` {id: text fingerprint} + `reconciled_at`; first run bounded (200) | HTML → text with `html.parser`; URL-only links, custom emoji dropped; opt-in network source, no credentials; edits and deletes: every `reconcile_interval_s` (7 days) the latest 200 statuses are re-read, a changed text is upserted under the same ref and a vanished or no-longer-public id (confirmed by `GET /statuses/:id`) is reported in `_delete_refs` |
| `slack` | `https://slack.com/api/search.messages` (`query=from:<@UID>`, `sort=timestamp`, 100 per page, page-numbered), `auth.test`, `chat.getPermalink`; config `allow_network`, `include_dms` (default false), `dm_register`, `register` (default `work`), `include_channels` / `exclude_channels`, `initial_messages` (200); token only from env `SLACK_USER_TOKEN` (`xoxp-`, scope `search:read`) | `match.user` == the user id bound by `auth.test` (user id + team id; `auth.test` is repeated on EVERY read, mid-scan too, so a swapped token is never searched under the old identity); no `bot_id`; subtype none / `thread_broadcast` / `file_share` with text; only the `text` field (attachments, unfurls and shared messages never read); quotes and fenced code removed, `<@U..>` → `@[user]`, `<#C..>` → `#[channel]`, `@here` → `@[channel]`; DMs and group DMs only with `include_dms` | `high_water` message ts + `floor` (ts of the oldest message the first run reached) + `scan` {token = next search page, candidate, fetched} + `seen` {`team:channel:ts`: text fingerprint} + `reconciled_at`; the scan reads on until matches are 24 h older than the mark (search indexing can lag); `seen` stops the overlap from emitting twice, `floor` stops it from widening the first-run bound | kind `message`, `meta.public = False`, register `work` (or `register` / `dm_register`), audience `group` (channels), `colleague` (1:1 DM), thread id from the root ts; 429 `Retry-After` → `status.retry_after`; edits: every `reconcile_interval_s` (7 days) the latest 200 messages are re-read and a changed text is upserted under the same ref; deletes: a ref missing from that window is reported in `_delete_refs` only when `chat.getPermalink` answers `message_not_found` (any other answer, e.g. the user left the channel, changes nothing); `mannerism sources check slack` runs `auth.test` only |
| `x` | `https://api.x.com/2/users/:id/tweets` (official API, pay-per-use); config `username`, `allow_network`, `max_posts_per_run` (50), `max_posts_per_month` (300), `initial_posts` (100); token only from env `X_BEARER_TOKEN` | author id == the id bound by `users/by/username` (protected accounts refused); `exclude=retweets`; `note_tweet.text` preferred; reply = leading mentions dropped, no parent text (it would be billed) | `high_water` tweet id (`since_id`) + `scan` token (`pagination_token`) + `usage` {month, posts, lookups} | reservation written to `x-usage.json` BEFORE each request, settled to posts returned; 429 → `x-rate-limit-reset`; `estimate()` prints the first-run cost; no reconcile (paid): deletes and edits on X are NOT propagated, and `mannerism forget --source x` removes everything stored |

The four network sources share `adapters/_http.py` (https only, 10 s timeout, `mannerism/<version>` User-Agent, 8 MiB JSON cap, redirects only inside the original origin, credentials never leave it) and `adapters/_social.py` (config gate, `status` in the cursor, the paged scan). `discover` never touches the network and returns nothing unless `allow_network` is true and the account is configured. A scan keeps the completed `high_water` apart from the in-progress `scan`: the mark is promoted only when the scan finishes, so a timeout resumes at the stored page token and loses nothing. A failed read returns no units and the cursor with a `status` {error, at, retry_after?} added so the scheduler can back off.
A continuation token the service rejects (HTTP 400/404/410) drops the in-progress `scan` and keeps `high_water`, so the next read starts a fresh scan instead of retrying the dead token forever (on X, a first run that already read posts promotes the newest one to the mark, so the restart does not pay for them twice; the rejected request's reservation is given back). Deletes and edits on the free sources are found by the periodic reconcile above; the engine receives deleted refs in the cursor key `_delete_refs`, like the files source.

*Slack was not live-checked*: no token was used and no real Slack call was made; the fixtures follow the Slack Web API documentation (`auth.test`, `search.messages`, `chat.getPermalink`). Points to verify against a real workspace before relying on them: the `search:read` scope for user tokens, whether `chat.getPermalink` answers `message_not_found` for a deleted message (if it does not, deletes are simply never confirmed), whether search matches carry `thread_ts` (the permalink's `thread_ts` is the fallback), and the `X-OAuth-Scopes` header that `sources check slack` reads.

*Live check, 2026-10-21.* One read-only smoke run of the Bluesky and Mastodon adapters against the public organisation accounts `bsky.app` and `@Mastodon@mastodon.social`, limited to 10 posts each, in a throw-away home directory that was deleted afterwards. Only counts were printed and no data was kept. It confirmed the response shapes the fixtures assume (feed items and reply/repost fields, facet types, Link and rate-limit headers); the X adapter was not run against the real API.
| `mbox` | Gmail Takeout / any mbox | `From` ∈ me.emails (+ `X-Gmail-Labels` Sent) | dedupe by Message-ID | strip quoted replies + signatures (in-house `On … wrote:` patterns) |
| `imessage` (macOS) | `~/Library/Messages/chat.db` (needs Full Disk Access; read a copy) | `is_from_me=1` | max ROWID | `attributedBody` fallback when `text` is NULL |
| `wispr` (macOS/Win) | Wispr Flow `flow.sqlite` `History` | all | max timestamp | prefer `editedText` > `formattedText` > `asrText` |
| `opencode` | `~/.local/share/opencode/opencode*.db` (+ `XDG_DATA_HOME`, `OPENCODE_DB`) | user messages' text parts, not `synthetic`/`ignored`; v2 `session_message.type=user`; no sub-agent sessions | part / message rowid | read via a WAL-aware copy; v1 and v2 layouts |
| `goose` | `~/.local/share/goose/sessions/sessions.db` (Windows `%APPDATA%\Block\goose\data`; + `GOOSE_PATH_ROOT`) | `session_type='user'`, no parent, no recipe/schedule; drop `userVisible=false`, `turnContext`, built-in slash commands | max message id | |
| `crush` | per project `.crush/crush.db`, found via Crush's `projects.json` + agent-transcript cwds | `role=user` text parts, not `hidden`; no sub-agent / title / channel sessions | max rowid per db | |
| `qwen_code` | `~/.qwen/projects/*/chats/*.jsonl` (+ `QWEN_HOME`, `QWEN_RUNTIME_DIR`) | `type=user`, `provenance=real_user`, no subtype; drop custom-command expansions | byte offset per file | strip `@file` content blocks |
| `pi` | `~/.pi/agent/sessions/*/*.jsonl` (+ `PI_CODING_AGENT_DIR`, session dir settings) | `message` entries with `role=user`; strip `/skill:` bodies | byte offset per file | forks dedupe by entry id |
| `aider` | `.aider.chat.history.md` in git roots / transcript cwds (+ `AIDER_CHAT_HISTORY_FILE`) | `#### ` blocks; drop `/` and `!` commands, confirmations | byte offset per file | session-start time for every prompt |
| `continue` | `~/.continue/sessions/*.json` (+ `CONTINUE_GLOBAL_DIR`) | `role=user`; typed text from `editorState` | mtime + size; dedupe by ref | session creation time for every prompt |
| `cline` | `~/.cline/data/sessions` (CLI 3.x) + classic `tasks/*/ui_messages.json` incl. VS Code-family globalStorage | SDK: user text blocks, no sub-agent/automation sessions; classic: `say` ∈ {task, user_feedback} | mtime + size; dedupe by ref | |
| `kilo` | `~/.local/share/kilo/kilo*.db` (+ `KILO_DB`); old JSON `storage/` tree | user text parts, not `synthetic`/`ignored`; no sub-agent sessions | db signature + newest time | |

v0.2 queue: Cursor, Claude.ai export, Gemini CLI, Zed, Discord, Notion, Apple Notes, IMAP Sent, LinkedIn, Messenger. Deferred: Windsurf (encrypted store), Signal (decrypting another app's
store), macOS Dictation (no local store).

**Custom sources.** `files` (above) and Python entry-point plugins (group `mannerism.sources.v1`, listed from
metadata, imported only after `sources enable NAME`, never by a quick sync) are described in `docs/custom-sources.md`.
Adapter class attributes `network` (default false), `network_hosts` / `hosts(config)` and `default_interval_s`
(default 0) drive scheduling: a network source needs `allow_network = true` in its table, never runs in `sync --quick`
(which may only start a background sync for it, and only when its `run_on_session_start` is true: default on for free network adapters via `session_start_default`, off for plugins and metered adapters with `estimate()`), runs in a full sync once its interval has passed, backs off
exponentially after a failure (`meta` key `sched:<source>`), and is forced past intervals by `sync --force`.
`sync --watch` loops the scheduler in the foreground; no OS scheduler is installed.

Export-tier sources are enabled by pointing at a file (`mannerism sources add whatsapp ~/Downloads/chat.txt`) or by
dropping exports in `~/.mannerism/inbox/<source>/`, which `sync` scans.

## 6. Pipeline

```
discover → read (cursor) → own-filter → mask PII → merge bursts (chat) → default register
       → write units → label queue (only '?') → features → bank build → stats per register → stats card
       → capture (new units vs draft log) → distill queue (when ≥5 new edits in a register)
```

`sync --quick`: only `auto` sources, time budget 5 s, stops early and keeps cursors consistent (each file commits
its own transaction with its new cursor). Full `sync`: all enabled sources. A sync never re-reads text before a
cursor; `forget` deletes units by source and resets cursors.

### Bank and retrieval

- Eligible: labelled (or defaulted) units in a generation register, `MIN_WORDS` per register, not `skip`, not
  AI-accepted drafts (capture status `accepted`), ≤2 per thread.
- Weight: recency half-life 120 days (75% of picks from the last 365 days), edited finals ×1.5.
- **Retrieval is diverse, not topic-matched**: sample k (default 6, range 5–8) by weighted maximal-marginal-relevance
  over stylometric feature vectors (length, punctuation, casing, emoji, contractions) so the examples span the
  register's range. `task` is used only as a weak tie-break (FTS5 bm25), never as the main ranking. This follows
  2509.14543 (diverse in-register exemplars beat topic-matched ones for style).
- `public` retrieval draws only from units whose source is public (X archive, public markdown) so a public draft never
  sees private text.

### Cards

- **Stats card** (deterministic, instant): rendered from `stats/<register>.json`: casing, sentence length, punctuation
  habits, emoji/laugh use, contractions, typical length, language mix, top openers/closers (masked), "never uses"
  list from the tells you do not use.
- **Agent card** (upgrade): `voice_work_next("card")` gives a packet = stats + 80–120 diverse masked examples + the
  card spec (≤250 words, concrete, quotes short real patterns, no names). The agent writes it; `submit` validates
  length, runs a leak check (no names/handles/emails from the corpus) and stores it. The stats card stays as a
  fallback.

### Lint

Findings, each with a `why`:

1. **Tells**, calibrated: a seeded list of AI-writing tells (phrases, em-dash runs, "not X but Y", rule-of-three
   lists, emoji bullets, title-case headings, sign-off boilerplate). Sources: the slop-forensics and blader/humanizer
   lists (MIT, attributed in `NOTICE`), with Wikipedia "Signs of AI writing" used only as a checklist (not copied).
   A tell fires only when your own rate for it in the register is below your p10, so a person who loves em-dashes is not
   nagged about em-dashes.
2. **Range**: length, sentence length, casing, punctuation density outside your p5–p95 for the register.
3. **Public leak** (public register only): capitalised entities, @handles, emails, URLs present in your private
   units but never in your public ones.

Each `voice_lint` with `is_draft=True` logs the draft (all registers; 45 days, 5000 rows max).

### Edit loop (CIPHER/PRELUDE-style)

1. **Capture**, from three paths:
   - *Ingest match*: after each sync, new own units are compared with drafts logged in the 14 days before them
     (same register first, then any). difflib ratio ≥ 0.5 on cleaned text, or a thread match where the source gives
     one. Status: `accepted` (≥ 0.9, i.e. AI text published as is → excluded from the bank) or `edited`.
   - *Prompt hook*: the next user prompt contains text with ratio ≥ 0.6 to a recent draft → `edited` pair.
   - *Explicit*: `voice_feedback`.
2. **Distill**: when a register has ≥ 5 undistilled `edited` pairs, a `distill` packet asks the agent for ≤ 5 short,
   general preference rules (with the edit ids that support each). New rules merge into existing ones (same meaning →
   support += 1).
3. **Retrieve**: `voice_context` includes the top rules for the register (by support, recency), max 8.
4. **Decay**: a rule whose pattern a later accepted/edited final contradicts gets `contradicted += 1`; rules with
   contradicted ≥ support are hidden.

### Local page (`mannerism ui`)

A page for the cards, rules and insights, plus the "Is this you?" review whose two actions are its only writes (`web.py` serves it; `dashboard.py` builds one JSON document from
the store; the assets are packaged in `webui/` and need no network). Security follows our other local pages: bind
127.0.0.1 only (no host option), random port by default, `Host` must be this server's own `127.0.0.1:PORT` or
`localhost:PORT` (DNS rebinding), a random per-launch token (URL once, then an HttpOnly SameSite=Strict cookie, or the
`X-Mannerism-Token` header), GET only except the two review POSTs (below), a foreign `Origin` refused, no CORS headers, a CSP that allows only this
origin, request lines never logged. The page's read connection is `mode=ro` with `query_only` and never creates or migrates the store (a missing store is shown as empty); the review writes use their own connection (see "Review page and its writes"), so `forget` shows on the next load. The browser is opened on a one-time nonce URL (`?k=`, valid 60 seconds, accepted once, exchanged for the cookie and redirected to a clean URL), so the long-lived token is not in the process list; the address printed in the terminal keeps the token so it can be pasted into another browser. A failed browser open never stops the server. Pairs and drafts longer than the compare caps are skipped, not cut. The cookie is not port-scoped and is also sent to other servers on 127.0.0.1, and a local program can set any `Origin`. For a read-only page that was low impact; since the review actions exist, whoever holds the cookie and the CSRF token can delete stored texts (see "Review page and its writes"). Do not run `mannerism ui` on a shared machine. The page data is computed once and remembered until the stored files change (one computation at a time), since linting the draft log and diffing the edits is not cheap.

What each part is computed from, and what it refuses to invent:

| Part | Source | Shown only when |
|---|---|---|
| Source strip, sync state | `status()` sources, `units` per source mapped to a kind (agent prompts, commits, GitHub, notes, mail import, posts, Slack, chats, dictation, files) | units exist |
| Voice cards | card files + `bank` rows. Habits are measured, not parsed from card prose: each is a feature test over the bank (starts lowercase, short, emoji, opener word ...) with `count of N` and one real masked message close to the typical length of the matches; a habit must hold in 12% to 95% of messages, ranked by distance from a typical-writer rate | register has >= 30 messages |
| Card state | fresh (stats or current agent card), upgrade available (`card_needed` / new text since the agent card), stale (drift flagged), building | always |
| Learned from edits | `rules` (+ `created_at`, `edit_ids_json`); diff pairs from `edits` for the newest three rules (word-level, masked as stored) | a rule exists; with none, the latest edit pairs are shown with the distill threshold |
| Edits per draft | `edits`: separate changed passages (diff hunks) per matched draft, mean per week (per day under 6 weeks) | >= 6 matched drafts over >= 3 buckets |
| Sent as written | `edits.status = accepted` (at least 90% the same) over drafts matched to something the user wrote (at least half the same); drafts rewritten completely or never sent are not matched, and the last 30 days | >= 5 matched drafts |
| AI tells caught | `drafts` re-checked with the lint against current stats (tells the user writes themselves are not counted) | >= 5 logged drafts |
| Words per message | `bank.words` per register, quartiles | >= 20 messages in the register |

Not stored, so not shown: which tells the lint reported at the time a draft was logged (they are recomputed from the
45-day draft log), drafts that never matched a final (no "ignored" rate), and when each habit began.

### Evaluation

- **Blind test** (`voice_work_next("blind")` / `mannerism blind`): 10 held-out real messages; the agent writes one
  message per gist with card + examples; mannerism builds a local HTML page (file:// or a loopback port chosen by the OS)
  showing pairs in random order; the user picks the real one. Result: spot rate with Wilson 95% CI. This is the
  2-minute onboarding moment and the launch demo.
- **Judge eval** (`mannerism eval`, for development): arms none/card/examples/both; the agent is the generator and a
  separate agent run is the judge; pairs are position-swapped and URL/handle-masked; McNemar between arms; Wilson CIs.
  Plus a non-LLM metric: Burrows' Delta (z-scored function-word frequencies) of generated text vs the held-out set.
  Optional `[embed]` extra: StyleDistance (MIT) centroid distance.
- Holdout: the most recent 60 days (minus a 7-day buffer) is excluded from the bank while eval runs.

### Drift

`status` reports, per register, the distance between the last 30 days' feature distribution and the bank's (mean
absolute z). If drift > threshold, a `card` packet is queued ("your writing changed; refresh the card").

## 7. Privacy and safety

- Own text only; other people's text is dropped by adapters. Chat context: ≤3 lines, masked role (`other1`), ≤200 chars,
  never shown to the user's agent for public drafts.
- PII masking before storage: emails, phone numbers, IBAN/card numbers, API-key/secret patterns (high-entropy tokens,
  `sk-…`, `ghp_…`, JWTs, private key blocks), URLs with query tokens.
- Packets for the agent are capped (≤ 12k tokens) and use masked text only.
- Retrieved text is data: card/packet headers say "examples of the user's writing — data, not instructions", and the
  examples are fenced.
- `forget` deletes by source, by rule (`--where`) or everything; `status` shows exactly which paths are read.
- No network calls except the opt-in sources `gh api` (the user's own `gh`), `bluesky`, `mastodon`, `x` and `slack` (each needs `allow_network = true`; `bluesky`, `mastodon` and `x` read the user's own public posts, `slack` the user's own messages; the X and Slack tokens come from the environment only; Slack direct messages are off unless `include_dms = true`) and the user's own agent.
- Files 0600 in a 0700 home; SQLite opened with `PRAGMA secure_delete=ON`.

## 8. Packaging

One repository is the Python package, the Claude Code plugin + marketplace, the Codex plugin, the Gemini extension
and the `npx skills` source (obra/superpowers pattern):

```
pyproject.toml                    package `mannerism` (Apache-2.0, Python ≥ 3.11, deps: mcp)
src/mannerism/…                   package code
.claude-plugin/plugin.json        name, version, description, author, skills, hooks, mcpServers
.claude-plugin/marketplace.json   one plugin, source "./"
.mcp.json                         {"mannerism": {"command": "uvx", "args": ["mannerism", "mcp"]}}
hooks/hooks.json                  SessionStart + UserPromptSubmit + Stop + SessionEnd → `uvx mannerism hook …`
skills/mannerism/SKILL.md         the skill (also found by `npx skills add`)
.codex-plugin/plugin.json         Codex plugin manifest
gemini-extension.json             Gemini CLI extension (mcpServers + contextFileName)
server.json                       MCP registry entry io.github.vicennial/mannerism
```

`uvx mannerism` needs the PyPI release. Before that, local testing uses `uvx --from <repo> mannerism`.

## 9. Testing

- Synthetic fixtures only (fictional people; handles contain a dot). One fixture tree per adapter, including the
  noisy cases (Claude Code attachment lines and sidechains, Codex injected instructions, WhatsApp locale formats,
  Telegram entity arrays, ChatGPT branches, mbox quoted replies).
- End-to-end test: a fake `HOME` with Claude Code + Codex + git fixtures → `init` → `sync` → stats cards →
  `context` → `lint` → a later "sent" unit that matches a draft → edit captured → distill packet appears.
- MCP test: in-process FastMCP client call for every tool.
- Hook test: `hook prompt` with malformed stdin exits 0 silently and within budget.
- CI: GitHub Actions, Python 3.11–3.13, ubuntu + macos, `ruff` + `pytest`.

## 10. Release plan

1. Private repo, build rounds with code review, local validation on the author's real data (outputs stay local).
2. Launch assets: 10–20 s before/after demo video (fictional text), README with a comparison table, social image.
3. Author decides: squash + public switch, PyPI publish, plugin marketplace listing, MCP registry entry, launch post.

## 11. Open questions (decided defaults)

| Question | Default |
|---|---|
| Register names | `close/group/work/formal/agent/public` (friendlier than the prototype's) |
| Unlabelled `?` units | out of the bank until labelled; agent labels newest first |
| Hook runtime cost | `uvx` cold start ~1 s; hooks detach and never block |
| Windows | best effort for paths; CI on ubuntu + macos only in v0.1 |
| Card language | the card is written in English; examples stay in their own language |

## 12. Review amendments (2026-10-21)

An independent design review (and a count-only run of the transcript adapters on real data) changed these points.
They override the sections above where they conflict.

**Ownership.** Identity is never guessed from "most frequent sender". Telegram uses the export's own user id, a
configured id, or the one id present in every personal chat (≥ 2 chats). WhatsApp auto-detects only when exactly one
sender appears in every exported chat (≥ 2 files). Otherwise the source reports "needs your name/id" and is skipped.
Agent transcripts keep only human-typed turns: Claude Code `origin.kind = human` / `turnOrigin = human`, no `sdk-cli`
entrypoint, no task notifications or peer messages, pasted blocks removed; Codex sessions whose `source` is a subagent
or `exec` are skipped. (Real data: 89% of Codex units on the author's machine came from automated sessions.)

**Disclosure boundary.** Storage is local. Text that `voice_context` and work packets return goes to the user's agent
and its model provider; the README says so plainly. Third-party context lines are stored but never emitted in v0.1.
No runtime network calls by default; `gh`, `bluesky`, `mastodon`, `x` and `slack` are opt-in sources.

**Public provenance.** The `public` register (bank, stats, cards, packets, rules, lint calibration) uses only units
from sources marked public (`x_archive`, `bluesky`, `mastodon`, `x`, markdown with `public: true` or a configured public dir). Labels cannot move
private-source text into `public`.

**Drafts and capture.** Drafts are PII-masked before storage, ≤ 8 KB each, ≤ 2,000 rows / 4 MB in total, and
`voice_lint` returns a `draft_id`. Capture: same register (or unknown), length ratio 0.5–2.0, a thread match is a
+0.1 bonus only, and a match is skipped as ambiguous when the runner-up draft is within 0.05. Units with ratio ≥ 0.75
to a draft are kept out of the bank as mostly AI text. Capture reports matched / ambiguous / unmatched counts.

**Rules.** A distill packet needs ≥ 5 edits from ≥ 3 threads or tasks. Rules have stable ids and are listed and
deletable (`mannerism rules`, `rules rm ID`). Submitted rules are ≤ 200 characters, with no URLs, code or tool
language, and cite only edit ids from the packet.

**Work queue.** Packets expire after 2 h, carry a bank snapshot version, submit is idempotent, and stale packets
or unknown ids are rejected. The first label batch is ≤ 40 items. Status per register is one of: stats card ready,
insufficient data (n, need 30), agent card ready, upgrade available.

**Lint.** Sample-size aware: < 30 units in a register → universal tells only, marked uncalibrated. A tell is
"yours" when it appears in ≥ 2% and ≥ 3 of your units. Short drafts (< 8 words) skip sentence/punctuation ranges.
Wording is advisory; lint never claims text is AI-written.

**Retrieval.** Quality before diversity: exclude skip, pastes and quality < 3; z-score features within the register,
clipped at ±2.5; ≤ 60% of picks from one source (when more exist), ≤ 2 per thread; return fewer than k if needed.

**Forget.** Deletes units, labels, features, cursors, linked drafts/edits/rules; rebuilds bank, FTS and stats;
invalidates packets; marks agent cards built on that data stale. It cannot recall text already shown to a model.

**Hooks.** Hooks call `uvx --offline mannerism hook …` so a cold or missing cache fails fast instead of downloading;
only one sync worker runs (non-blocking lock); detached workers close all std streams. Hook latency is measured in
tests.

**Support matrix.** [sources.md](sources.md) lists each source as *tested on real data* or *beta (synthetic fixtures only)*. In
v0.1 the real-data set is the sources the author can validate locally.

### R3 fixes (2026-10-21)

A follow-up fix round, driven by further real-data findings and adversarial repro tests, changed these behaviors:

- **Forget semantics.** Forgetting a source also clears packets and rules wholly derived from it, not only its
  units; a source marked forgotten stays forgotten across the next plain sync instead of silently reappearing
  from its own cursor.
- **Git AI commits.** Commits carrying an AI co-author trailer (Claude Code, Codex, and other agent trailers) are
  skipped unless `include_ai_commits` is set in config; an included AI commit is flagged `meta.ai` and excluded
  from the bank even with no captured edit. The `--since` cutoff now follows the commit's *committer* date with a
  3-day overlap, and repo-local `user.name`/`user.email` (up to 25 repos) feed `identity_guess()` too.
- **WhatsApp detection.** Self-identity is a per-candidate vote across every export that looks like a genuine 1:1
  (exactly two senders); a lone lurker group can no longer poison the vote. A guessed identity is flagged
  `meta.self_detected` rather than asserted as fact, and `status` surfaces it for confirmation. Thread identity
  now comes from the participant set, not the export filename, so a re-exported chat still dedupes correctly.
- **Telegram provenance.** Units from a public channel are stored as register `?` (unlabelled), not `public`:
  only a unit an adapter itself marks `meta.public` (a verified publication) ever reaches the public register,
  matching the "public means verified" rule above, not a channel type guess.
- **Thread-balanced stats.** Stats weight down any one large, automated thread (`1 / max(1, n_thread/25)`) so it
  cannot dominate calibration or cards, and opener/closer detection is now restricted to a fixed discourse-word
  list rather than raw top-300-frequency words, so topic nouns no longer leak in as "tells".
- **Secret masking and size caps.** `core.mask_pii` and new byte/row budgets (`adapters._util.read_new_lines`,
  and JSON/zip member caps in `chatgpt`, `telegram`, `x_archive`, `whatsapp`) keep lint and ingestion fast and
  bounded on pathological input. Cursor text for Claude Code and Codex is masked before storage, and Codex `cwd`
  is stored hashed, never raw.
- **Mail and capture.** `mbox` drops mail that only claims to be from the user without a matching Gmail "sent"
  label or that fails sender authentication, and stores a hashed Message-ID instead of the raw one. Capture
  checks the best raw ratio across every candidate draft against `AI_RATIO` *before* the ambiguity check, so a
  near-verbatim AI draft stays out of the bank even when which prior draft it matches is itself ambiguous; a
  public-register draft only ever matches a unit the adapter marked `meta.public`.

**Known issues after the second review (v0.1).** A markdown file that is deleted keeps its old units until
`forget --source markdown`; secrets written as prose ("the password is …") are not masked (only `key: value`,
URL-credential and token shapes are); WhatsApp self-detection with exactly one 1:1 chat plus groups can still pick
the wrong name, so such units carry `meta.self_detected` and `status` asks the user to confirm.

## 13. Setup features for complex machines (2026-10-22)

Goal: a machine with several accounts, orchestrators, relays and scripts is set up in four steps: install, `mannerism
scan`, drop the groups that are not the user, done. General features; nothing in them is specific to one machine.

### Origin metadata (schema 4)

`SCHEMA_VERSION` is 4 (a later index, `units_sess_key` on `coalesce(session, thread_id)`, is created on every open if missing). `units` gains `account`, `project`, `project_path`, `session_kind`, `session`, `template`,
`human_score`, `held`, `score_json` (nullable; `held` defaults to 0); the tables `exclusions` and `group_decisions` and
three indexes are new. `store._migrate` runs the steps that change a table inside one `BEGIN IMMEDIATE` transaction and
reads the version again once it holds the lock, so two processes that open an old store at once cannot both add a
column; `store._migrate_v4` adds the columns in place and is idempotent. Old units keep NULLs: they match only `source`
/ `text` rules and are not held until `review rescan` scores them (below). The Claude Code and Codex adapters fill the
fields (`account` from the config directory the file lives under, `project` from the line's `cwd`, `session_kind` from
Claude Code's `entrypoint` or Codex's `session_meta.source`); other adapters leave them empty. All labels are masked
with `core.clean_label` (secrets and emails, not numbers, so a directory like `/mnt/vol_1234567` stays readable). This
amends the earlier "Codex cwd is stored hashed, never raw": the working directory is now stored as a home-relative
path and a basename, because exclusion rules and review need to name it; the `cwd_hash` stays too.

### Exclusion rules (`exclude.py`)

A rule is an AND of `source` (exact), `account` (glob on the label; the expanded `~` path matches too), `project`
(glob on the basename or the path), `session_kind` (case-insensitive), `text` (regex) and `group` (a group id). A unit
without the metadata a rule asks for does not match it. An empty rule is refused. Rules come from `[[exclude]]` tables
in config.toml (read-only to the code; `config.Config.exclude` round-trips them) and the `exclusions` table; both apply.

- At ingest, `intake.Intake` (built once per sync) drops a matching unit and passes its `source_ref` to the same
  `delete_refs` path adapters use, so a stored older copy of a text that now matches is removed too. A regex timeout
  excludes the new text but does not authorize deleting the stored copy. The sync cursor still advances: removing a
  rule does not bring back text that was skipped (`forget --source S` then `sync` does).
- Retroactively, `exclude add`, `forget --where` and a changed `[[exclude]]` section (a fingerprint in `meta`,
  checked at the start of every sync) call `store.delete_unit_rows`, the machinery of `forget --source`: bank rows,
  features, labels, edit pairs, the drafts they matched, rules whose only support was a deleted edit, all packets and
  blind runs go; agent cards built on the affected sources are dropped, banks, stats and stats cards are rebuilt.
  `exclude add` and `forget --where` take the sync lock, count first (`--dry-run` stops there, and opens no store that
  does not exist), and ask for confirmation like `forget`.
- Regular expressions run on every text, so they are guarded without signals in the calling process (its SIGALRM /
  interval timer can belong to the detached sync's 60 s kill, and MCP `voice_setup` syncs in a daemon thread). Validity
  is a static check of the parse tree, the same on every machine: at most 500 characters,
  a repeat around another repeat that can run many times (`(a+)+`, `(\s*\w+)*`) refused, an unbounded repeat around an
  alternation whose branches can start with the same character (`(a|aa)+`) refused. Only the first 20,000 characters of a
  text are searched, and the match runs in a small worker process (`exclude._Worker`, started on first use, one pipe round
  trip per match): the worker has a 0.20 s timer and the parent stops it after 0.25 s. A timed-out match counts as a **match** at
  ingest (the text is excluded: fail closed); in the retroactive delete it counts as no match, so a slow pattern never
  removes stored text. A pattern that timed out five times in one process is not run again. If the worker is unavailable,
  matching fails closed at ingest and fails open for destructive scans; it never runs unbounded in the caller. Timeouts
  are counted in the sync report. The worker's own timer bounds a match after its caller dies; the closed input pipe
  then makes it exit. A worker that died between calls is restarted without treating that lifecycle event as a match
  timeout.
- A stored rule that no longer passes these checks (it compiled once, or was written by hand) is skipped with a warning
  (`exclude_errors` in the sync report and in `scan`), never raised on every location.
- A masked label contains `[secret]` or `[email]`, which a glob reads as a character class. A rule value equal to the
  label matches it (so a label pasted as shown works), and the commands `scan` prints use `glob.escape` (`[[]secret]`).

### `scan` (`scan.py`)

Runs the sync read loop with in-memory cursors and no store connection (the store is opened read-only, `mode=ro` when
the write-ahead log and its `-shm` index exist, `immutable` otherwise, only to read stored rules and "it's me"
decisions; a log without its index is read without the log and the scan says so, because opening it read-only would make
SQLite create the index file). It creates no file. It skips sources the config switches off unless they are named with
`--source`, never calls the X API (paid per read; the report says it was skipped, with `--network` too), and gives plugin
units the same masking and label cleaning a sync does, so group ids match. It applies the same adapters, filters,
exclusion rules and scoring (a confirmed group is neither filtered nor held), and keeps about 18 bytes per text plus a
reservoir of masked samples per group, so a large history stays bounded; a time budget (default 120 s) stops it
starting new reads. It prints per source, account and project the counts, date range, machine-typed share, samples and
group id, then commands for the noisiest groups. An account whose texts are mostly flagged is suggested as a whole
(`--source S --account A`) because an orchestrator that starts every session in a new directory would otherwise be
hundreds of tiny groups. A scan sees the complete counts; a sync meets texts one read at a time, so it drops only what is
clear on arrival and holds the rest when later sessions confirm the repetition.

### Machine-typed detection (`humanness.py`)

A unit starts at 1.0 and loses points: relay wrappers (0.55; any other wrapping tag 0.4), `[SYSTEM` markers (0.55), front
matter (0.5), JSON blobs (0.6), mostly `key: value` lines (0.35), bullet-only status blocks (0.3), a long block that
arrived faster than 30 characters a second after the previous turn (0.2; Claude Code and Codex timestamps), the same
opening words in 3 / 6 / 12 sessions (0.25 / 0.4 / 0.55, +0.1 for long texts), and a session where at least 80% of five or
more prompts are structured (0.25). `LOW = 0.20`: below it the unit is not stored and is counted as filtered (it takes
two strong signals); `REVIEW = 0.60`: from LOW up to it the unit is stored but `held`. Text under 20 characters is never
judged and a template needs 8 words, so short prompts people repeat in every session are never touched.

Only ingest drops, and only a new text. The cross-session signals depend on what else is known, so a stored text can
score lower later; it is then **held, never deleted or released** (`humanness.holds`: any score below REVIEW), and it
leaves `held` only when its score reaches REVIEW or the user confirms its group. A text in a confirmed group is stored
as normal text at ingest whatever it scores.

`Screener` scores a batch against the batch plus the store and records which templates and sessions the batch moved over a
threshold: a template over 3 / 6 / 12 sessions (`template_tier`), a session over the 80%-structured line. Only the stored
texts of those are scored again (`rescore_after_write`, "when a count crosses a threshold"), and each is scored with its
own real counts read from the store: its template's sessions and its own session's texts, not only the sessions of the
current batch. The session lookup uses the expression index `units_sess_key`. `bank.build` and the label queue skip held
units, so held text is in no example, card statistic or label packet; capture does not pair held units with drafts
(`groups.its_me` pairs the ones it releases) and distillation ignores edits made from held texts; the privacy leak
index still sees held text (conservative).

*Typing speed.* The "fast" signal needs the end of the previous turn: the adapters measure it from the agent's last line
(or the previous prompt, or the Codex session start), carry it in the cursor, and leave it unknown (so nothing is
flagged) when it is not known, e.g. a cursor from an older version. Queue markers, attachments, meta and tool-result lines
never count, and a read that starts mid-session measures the same gap as a read from the top.

*`review rescan`* (explicit, because it can hold old texts) first reads each source that has stored texts without an
origin again from the start (`backfill.py`: in-memory cursors, no network and no plugin code) and writes the origin into the
rows that are already stored and have none, when the text is identical (an UPDATE keyed on the unit id: nothing is added
or duplicated); then it scores the never-scored texts. A score below LOW holds the text. Texts it scored while they still
had no origin are marked `legacy` in their score record.

### `forget --all`

Deletes the corpus tables, cards, stats and test pages, disables every source, sets the quiet-until-sync marker, and clears
`hook-roots.json` (paths of the user's config directories). It keeps `config.toml` (identity, switched-off sources,
`[[exclude]]` tables), the `exclusions` table and `group_decisions` (decisions, not text); the command's note says so.

### Groups and the review API (`groups.py`)

A group is an origin group (`g_` + 12 hex of a hash of source, account, project, session kind) or a template group (`t_` +
the template id; shown when it spans 3 sessions and 5 texts). Ids are stable, so the id `scan` prints is the id in
`exclude add --group`, in `[[exclude]] group = ...` and on the review page. The page uses:
`list_groups(con, samples)`, `group_detail(con, id, samples)`, `review_state(con)` (reads, pure, any connection), and
`not_me(id, force=False)` / `its_me(id)` (writes: their own connection and the sync lock). `not_me` stores a rule and deletes
retroactively; `its_me` releases held units, records a `keep` decision, pairs the released texts with drafts and refreshes
the bank. Every group carries `mixed`: true for the origin group with empty account, project and kind of a source whose
texts were stored before origin tracking (a never-scored text, or one `review rescan` scored without finding an origin).
Such a group can hold texts from many places, so `not_me` and `exclude add --group` / `forget --where --group` refuse it
(a dry run reports `mixed`) unless `force` / `--force`; the page never forces, and the CLI prints no "not you" hint for it.
A rule that is only a mixed group is refused; a group combined with other fields is not. CLI: `exclude add --group`, `review keep GROUP`.
`api.review_groups()`, `api.review_keep()`, `api.exclude_add()` and friends wrap them for the MCP server and the UI.

### Review page and its writes (`mannerism ui`, "Is this you?")

The section lists `review_state` (via `dashboard.review()`, read-only; at most 300 groups, held first, with `groups_total`
for the rest) as cards: source, project, account, session kind, texts, sessions, date range, held count, three masked
samples (`textContent` only). With held groups, the section is placed above the voice cards; otherwise it sits at the
end, shows a friendly "nothing needs a second look" state and a button to list all groups. **Not me** opens an inline
confirmation naming the number of stored messages that will be deleted (the group's `units`; a rule matches exactly those
texts) and only then POSTs; **It's me** POSTs at once. After either, the page shows the result (`removed` / `released`) and
refreshes the section and the overview.

These are the first writes of a page that was GET-only. They are limited to `POST /api/review/not-me` and
`POST /api/review/its-me`, body `{"group_id": "g_0123456789ab"}`; every other method or POST path is still 405 with
`Allow: GET`. Checks, in order, before any body is read or anything is written (all failures are 403 unless noted, and
change nothing):

1. `Host` is this server's (DNS rebinding).
2. The session cookie is present and right. The header token that GET accepts is not enough for a write.
3. `Origin` is present and equals `http://<Host>` exactly (so `localhost` vs `127.0.0.1`, `null`, other schemes, ports or a
   trailing path fail); `Referer`, if the browser sends one, must be on that origin (the page sends none: it is served
   `Referrer-Policy: no-referrer`).
4. `X-Mannerism-Csrf` equals this launch's CSRF token (a second random secret, constant-time compare). The page gets it from
   `GET /api/csrf`, which needs the session cookie and is **single-use per launch**: the first successful fetch wins, every
   later one is 403 `{"ok": false, "error": "used"}` and prints one warning line in the terminal (as does a spent or
   expired `?k=` link). The page asks at load and keeps the token in `sessionStorage` (each access in try/catch, memory
   as fallback). A tab without the token (a second tab, a closed-and-reopened tab) shows its groups read-only, without
   buttons, and says so; only restarting `mannerism ui` issues a new token. The token appears in no URL, cookie, markup
   or other response. This narrows, but does not remove, the risk of a program that holds a copied cookie: it can set
   `Origin` itself, so the first one to fetch the token wins.
5. `Content-Type` is `application/json` (415 otherwise). A cross-site form cannot send it, and a cross-site `fetch` with it
   needs a CORS preflight, which is never granted. Together with `SameSite=Strict` this makes the CSRF token a second layer.
6. A `Content-Length` of ASCII digits only (`str.isdigit` alone also accepts "²") and at most `MAX_BODY` (1024) bytes, and no
   `Transfer-Encoding` (411 / 413). A body shorter than its length is 400 once the 15 s socket timeout passes.
7. The body is a JSON object with `group_id` (matching `[gt]_` + 12 lowercase hex under `fullmatch`) and optionally
   `expected_units`, an integer from 0 to 10^7; any other key or type is 400 (the input is never echoed). A well-formed id
   that names no stored group is 404, so a typo or a stale page cannot store an exclusion rule for nothing. If
   `expected_units` differs from the group's current size, 409 "this group changed, refresh": the page sends the number
   its confirmation showed, so a group that grew after the page was drawn is not deleted unseen.

The work is `groups.not_me` / `groups.its_me`, which take the sync lock (10 s; busy gives 409 with a plain message) and open
their own short-lived write connection, so a write never waits behind or shares the page's read connection. Unexpected
errors give a generic 500 (no stored text in any message). After a successful write the cached overview is dropped.
`/api/review` sends no rule text and no scores, only what the cards show. Template groups carry `breakdown` (the biggest
source / project pairs and a count of the rest), which the **Not me** confirmation lists, since a repeated opening can
span several tools. Groups the engine marks `mixed` (older texts with no account or project) get no **Not me** button
and a note to run `mannerism review rescan`; the page never forces, and the server refuses it as well (409). Page
behaviour: review replies carry a sequence number and older ones are ignored; busy state is per group; the section keeps
its place and the result notice is scrolled into view; a card animates in only the first time it appears. Cookies: every
cookie of the right name is checked and any match is accepted, because another local server may have set a stale one.
Known limits: a client that sends its body very slowly can hold a handler thread until the 15 s per-read timeout (there
is no total-time limit); handler threads are daemons, so Ctrl-C ends the process without waiting for an action in
flight (the engine's transaction either committed or rolled back). The
CSP is unchanged (no inline script, `connect-src 'self'`). Fixed while building this: `groups.is_group_id` used `$`, which
accepts a trailing newline; it now uses `fullmatch`.

## 14. Later additions (pointers)

Features added after this design, each documented where it lives:

- **Host sources.** Explicitly configured adapters that read a database file the host provides, never scanning for
  one: `hermes` (Telegram direct messages from a local `state.db`, read-only, snapshot-based) in
  [host-sources.md](host-sources.md), and `whatsapp_db` (an already-decrypted Android `msgstore.db`) in
  [whatsapp-db.md](whatsapp-db.md). Both are `export`-tier sources with a refresh that can delete units a source
  later removes.
- **Public-only remote service.** `mannerism public build` / `public serve` expose a public-only projection of the
  bank over a loopback HTTP endpoint, and a remote client mode uses it so a second host never holds the private
  corpus: [public-service.md](public-service.md).
- **Blind test.** Held-out messages are excluded from every retrieval by unit id and by normalised text while a
  test is pending, so a duplicate of a held-out message cannot reach the writing agent.
