# Host source contracts

`Hermes` in `mannerism.adapters.hermes` implements `Source` with name `hermes`
and tier `export`. It is in the built-in adapter registry. There is no implicit
scan of Hermes homes or profiles.

```toml
[sources.hermes]
enabled = true
paths = ["/absolute/path/to/state.db"]
user_id = "12345"
```

Paths must be explicit database files. Directories are not searched. The configured
`user_id`, or an explicitly configured `me.handles.hermes` / `me.handles.telegram`,
must match `sessions.user_id`. Only Telegram sessions are eligible. Cron, tool,
CLI and subagent sources are excluded. Sessions with `_delegate_from` and their
descendants are excluded even when they inherit the Telegram source. Compaction
continuations without that marker are eligible. Unknown ancestry, cycles and
malformed session metadata fail closed.
Known non-DM chat types are excluded: a shared session owner cannot identify the
sender of every stored message. Older schemas without `chat_type` use the explicit
session user id, as the legacy adapter did.

The source is opened with SQLite `mode=ro`, `query_only=ON`, and one real read
transaction spanning session metadata and message extraction. SQLite coordinates
the snapshot with concurrent writers and checkpoints. WAL/SHM read coordination
is allowed; the adapter does not change logical data or main/WAL contents.
It does not copy live SQLite files or require a quiesced Hermes service.
The main file and WAL signature trigger a snapshot reread; hashes emit only changed units. Snapshot
reads include inactive originals and deduplicate platform-message copies within
their chat. The cursor retains their first row-id aliases across compaction.
Gateway system, tool, summary, delegation and notification text is excluded.
Media wrappers are removed; reply/assistant text is masked context only.

Identity stays `source="hermes"`, with numeric message-row `source_ref`s or
`first_id..last_id` for adjacent own messages within 120 seconds. These match
legacy imports while the original rows remain available. Changed burst boundaries
return `_delete_refs` for the old units; the normal sync pipeline must consume
that reserved cursor key. Repeated unchanged reads return no units. Failed,
unsupported or over-limit reads retain the cursor and return `_error` without
deletion instructions. The snapshot limit is 100,000 selected message rows.
Snapshot text is limited to 32 Mi characters; individual user text over 256,000
characters is skipped. Synthetic child sessions do not consume the row budget.

An initial read after original rows were already removed cannot recover legacy
row ids from platform ids alone. Separate databases can also reuse row ids. Use
one logical Hermes database lineage per source; do not combine unrelated databases
under `hermes` without an explicit migration decision.

`mannerism.published.capture_posts(posts_path, candidates_paths=(), *, con=None,
me=None)` handles local, trusted **own-post** JSON. It never fetches or publishes.
Accepted shapes are `{"data": [...]}`, `{"items": [...]}`, and a JSON array.
Official API fields use `created_at`, `text`, `note_tweet.text`, and
`conversation_id`. Legacy rows can use `createdAt`, `isReply`, `isRetweet`, and
`conversationId`. A note tweet supplies the full text. Retweets are excluded.
When present, author handles must match the configured `me.handles.x`; configured
`me.handles.x_id` also checks API `author_id`. An own-post export without author
fields depends on the caller's trusted ownership selection. This API is unsuitable
for arbitrary timelines, search results or third-party JSON.

Published identity stays **`source="x", source_ref=str(post_id)`**, without a
`tweet:` prefix. New text has public provenance `meta.public=true`; unchanged
imported text retains any existing public/privacy denial. The existing network X
adapter uses `tweet:<id>`; registry/CLI wiring must keep it separate from this
legacy-preserving host path to prevent duplicate identities.

Candidate paths are explicit JSONL files. Each row has a timestamp in `ts` or
`added`, draft text in `text` or `reply_text`, and optional `target_id`. The
register defaults to public; explicit non-public rows are ignored. Missing optional
files and malformed rows are skipped. Candidates are never imported as units or
human examples and never added to the persistent draft log.

Capture compares public drafts from the store and eligible candidates from the
14 days before each post. It uses the current capture helpers for segments,
length bounds, ratios, ambiguity, case edits and edit recording. A target match
adds the current thread bonus; it does not prove a match on its own. Accepted
and mostly-AI posts stay outside the human bank, including ambiguous high-ratio
matches. Edit rows link to the post unit to avoid duplicate bank evidence.
Imported explicit AI exclusions and existing linked edit evidence are retained.
For unchanged text, capture preserves reviewed metadata and never replaces
`public=false` with publication provenance or learns public edits from that text.
For changed text, capture builds fresh content metadata and drops old privacy
flags and the `legacy_voicekit` raw payload. It carries only a conservative
boolean AI exclusion from the previous metadata, not the obsolete raw text.
Existing non-public register labels prevent public edit learning. Invalid Unicode
and post text over 256,000 UTF-8 bytes are skipped.

Return values include integer `edited`, `accepted`, `unmatched`, `ambiguous`, and
`skipped` counts, compatible with the host caller's first three fields. A post is
decided once per text/timestamp/thread fingerprint. Unchanged repeat runs return
zero counts. New candidates do not reopen prior decisions. Interrupted matches
leave units held until retry. Unchanged imported posts retain their existing hold.
Before temporary quarantine, capture persists `_published_capture_quarantine` in
private unit metadata with the input fingerprint and prior held state. Only a
matching marker with a boolean prior state permits recovery of a temporary hold.
The final write restores the prior/current screening hold and removes the marker
atomically. An unmarked hold is never treated as temporary. A separate pending
marker permits retry of a failed refresh.
Capture requires `store.invalidate_public_snapshot(con)`. It calls that helper
before the held/write mutation and before its direct held-state update. The helper
must persist the rebuild-needed marker and revoke the old projection before any
mutation can commit or an interruption can occur. There is no silent fallback
when the helper is unavailable; root integration supplies and tests this dependency.
The normal API acquires the sync lock and owns its store connection; callers
passing `con` must hold that lock themselves. The root owns the `capture-posts`
CLI and live caller changes.

Files are limited to 32 MiB, a capture to 10,000 post rows and 5,000 candidate rows.
Oversized or malformed top-level inputs fail before the store is opened. Runtime
source files, databases and credentials must never be copied to development hosts.
Tests use only synthetic data.

The host's recurring private refresh currently selects `whatsapp`, `hermes`,
`agent_prompt`, and `x`. Gmail and ChatGPT exports are one-off imports. This module
does not change source schedules or enable those imports for recurring discovery.

Before the first native Hermes snapshot after import, the root should run an
aggregate-only reconciliation under the sync lock. Compare imported Hermes source
references with a complete, successful native snapshot before adopting its cursor.
Report counts of exact reference matches, native-only references, legacy-only
references, overlapping burst endpoints, and references that cannot be parsed.
Partition legacy-only counts by old privacy/held/AI exclusions and linked edits or
labels, so a future decision does not discard reviewed evidence implicitly. Compare
IDs locally and expose only counts; do not print text, context, raw IDs or audit payloads.

An overlapping `first..last` range alone does not prove that every member of an
old burst is present: row IDs can include assistant or other-session messages.
Missing originals, excluded synthetic sessions and changed burst boundaries need
separate aggregate categories. Keep every unresolved legacy row on the baseline
pass. A later, explicitly reviewed cleanup could use per-message lineage to prove
replacement and preserve applicable labels/edits before removing superseded units.
Neither a missing reference nor a partial/failed snapshot authorizes a purge.
This is a proposal only; this adapter does not implement baseline reconciliation
or delete imported rows absent from its first cursor.
