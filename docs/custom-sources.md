# Your own sources

mannerism learns your voice from text **you wrote**. If your writing lives somewhere mannerism does not read yet,
there are two ways to add it:

1. the built-in **`files`** source: put UTF-8 `.txt`, `.md` or `.jsonl` files in a folder (once, or continuously);
2. a Python **plugin**: a small package that adds a whole new source.

Rules for both:

- **Own text only.** Do not add other people's messages, documents or quotes. When you add a folder, mannerism asks
  you to confirm the text is yours (`--yes` answers for scripts).
- **Everything is masked** (emails, phone numbers, tokens, card numbers) before it is stored, and it is stored on your
  machine (`~/.mannerism`). The `files` source masks what it reads; for a plugin the engine masks every text and
  context line again, whatever the plugin did.
- **`public` needs verification.** Text you file under the `public` register is stored, but it is never marked as
  published, so it is not used to draft public posts. Public drafts learn only from text a built-in adapter *knows*
  was published (for example an X archive). A file cannot claim that about itself, and neither can a plugin: the
  engine removes any `meta["public"]` a plugin sets, because a plugin does not verify publications.
- **Forgetting works as everywhere**: `mannerism forget --source NAME` removes the texts and what was learned from them.

## The `files` source

### One-time import

```
mannerism sources add files ~/writing/essays --name essays --register formal
mannerism sync --source essays
```

`--name` is the name of this source (lowercase letters, digits, `-`, `_`; it must not be a built-in source or an
installed plugin). `--register` is one of `close`, `group`, `work`, `formal`, `agent`, `public`; without it your agent
labels each text later. `--kind` (`message`, `post`, `comment`, `document`; default `document`) applies to `.txt`
and `.md` files. `PATH` can be a file or a folder (searched recursively; names that start with `.` are skipped).

It is stored in `~/.mannerism/config.toml` as an ordinary source table, which you can also write by hand:

```toml
[sources.essays]
adapter = "files"
path = "/home/jo/writing/essays"
register = "formal"
kind = "document"
enabled = true
```

The built-in source named `files` (no `adapter` line) reads the default inbox `~/.mannerism/inbox/files/`. Enable it
with `mannerism sources enable files`. It is never enabled by `init` on its own.

### What each file type gives

| File | Result |
|---|---|
| `.txt` | one text per file; a file longer than about 300 words is split at blank lines into chunks of about 300 words |
| `.md` | the same, after frontmatter, fenced code, comments, embeds and link syntax are removed; `date:` in the frontmatter sets the time |
| `.jsonl` | one record per line (below) |

Frontmatter such as `public: true` is **not** honoured here. When a `.txt` or `.md` file changes, its old chunks are
replaced by the new ones. Removing a file does not remove its texts: use `mannerism forget --source NAME`.

### JSONL records (schema v1)

One JSON object per line, UTF-8, at most 1 MiB per line. Copyable example (two lines of a feed file):

```jsonl
{"schema_version":1,"op":"upsert","id":"post:123","ts":"2026-10-20T12:00:00Z","text":"Shipped the invoice batch today. The part that surprised me: reviewing early saved a week.","kind":"post","register":"public","audience":"","thread_id":null,"context":[],"updated_at":null,"origin":{"provider":"example","account_id":"jo","native_id":"123","url":"https://example.org/jo/123"}}
{"schema_version":1,"op":"delete","id":"post:99","ts":"2026-10-21T08:00:00+02:00"}
```

| Field | Rule |
|---|---|
| `schema_version` | required, the integer `1` |
| `op` | required, `upsert` or `delete` |
| `id` | required, non-empty string, at most 512 characters. **Stable across edits**: it identifies the text. Ids belong to the *source instance*, not to a file: the same id in two files of one instance is the same text, and a delete record in any file of the instance removes that id. |
| `ts` | required, RFC 3339 with an explicit offset (`Z` or `+02:00`); stored as UTC |
| `text` | required, non-empty string: your own words |
| `kind` | required, `message`, `post`, `comment` or `document` |
| `register` | required, `close`, `group`, `work`, `formal`, `agent` or `public` |
| `audience` | optional string, at most 200 characters. Only a pseudonymous hash of it is kept. |
| `thread_id` | optional, `null` or a string up to 200 characters (hashed) |
| `context` | optional list of at most 3 strings of at most 240 characters: what you replied to. Masked, never used as a style example. |
| `updated_at` | optional, `null` or an RFC 3339 timestamp |
| `origin` | optional, `null` or exactly `provider`, `account_id`, `native_id` and `url` (all strings; `url` absolute `https`) |

Unknown fields are rejected (the error says "unknown field" and the line, never the field's name), so a record can never set internal metadata, a source name or a "public" flag.
A **delete** record is exactly `{"schema_version":1,"op":"delete","id":"...","ts":"..."}` and nothing else. It removes
that text and everything derived from it: the example bank entry, its labels, learned features, edit pairs and rules
that came from it, open work packets and agent cards built on that source (they are rebuilt). An edit is an upsert
with the same `id` and new text.

Check a file before you sync it. This only reads the file: nothing is stored and no record text is printed.

```
$ mannerism sources check feed.jsonl
feed.jsonl:2: invalid JSON (Expecting value, column 1)
feed.jsonl:3: kind must be one of: message, post, comment, document
2 valid record(s) (1 upsert, 1 delete), 2 error(s). Nothing was stored.
```

### Continuous: a drop folder

Point a `files` source at a folder and let any script, cron job or exporter write into it. Every `mannerism sync`
(also the background sync a session start triggers, and `mannerism sync --watch`) lists the folder again and reads new
and changed files.

Producers must **write a temporary file and rename it** into place, so a sync never reads half a file:

```sh
tmp="$(mktemp ~/drop/.feed.XXXXXX)"      # starts with "." : ignored while it is written
my-exporter > "$tmp" && mv "$tmp" ~/drop/feed.jsonl
```

Or append whole lines to one `.jsonl` file (`>>`), ending each line with a newline. A last line without a newline is
accepted if it is complete, and waited for if it is not.

### Cursor rules (what a sync re-reads)

- Per file, mannerism remembers a **generation** and the **byte offset** it has read up to. Appended lines are read
  from the offset.
- A file that is **replaced** (new inode, as with a rename), shrinks, or whose start or end changed starts a new
  generation and is read again from the top. That is safe: the same `id` replaces the same text.
- Editing the middle of a file in place, without changing its size or its start and end, is not noticed. Publish a new
  version with a temporary file and a rename instead.
- A **malformed line stops that file at that line**. Earlier lines stay stored; the cursor waits on the bad line; the
  error is reported as `file:line:reason` (never the text) in `mannerism sync` and `mannerism status`, and repeats on
  every sync until the file is fixed. Other files and sources are not affected.
- For `.txt` and `.md`, an unchanged file (same size, time and content) is skipped.

## Python plugins

A plugin is a package that offers a source through the entry-point group `mannerism.sources.v1`. Use a plugin when the
text sits behind an API or a format that a drop-folder cannot express.

**Plugins run with your permissions. There is no sandbox.** Install and enable only code you trust.

Minimal plugin, `pyproject.toml`:

```toml
[project]
name = "mannerism-my-notes"
version = "0.1.0"
dependencies = ["mannerism"]

[project.entry-points."mannerism.sources.v1"]
my-notes = "mannerism_my_notes:MyNotes"
```

`mannerism_my_notes.py`:

```python
from datetime import UTC, datetime
from pathlib import Path

from mannerism.core import Location, Unit, mask_pii


class MyNotes:
    name = "my-notes"          # must equal the entry point name
    tier = "export"            # "auto" sources also run in the quick session sync (local, cheap ones only)
    network = False            # True if read() contacts a service (see below)
    default_interval_s = 0     # seconds between runs; 0 = every sync

    def discover(self, me, config):
        """Where to read. Fast, never raises, never touches the network."""
        notes = Path.home() / "notes"
        return [Location(self.name, str(notes), notes, "my notes")] if notes.is_dir() else []

    def read(self, loc, cursor, me):
        """Units written since `cursor`, and the cursor to store with them (one transaction)."""
        seen = set((cursor or {}).get("seen", []))
        units = []
        for p in sorted(loc.path.glob("*.txt")):
            if p.name in seen:
                continue
            units.append(Unit(self.name, p.name, datetime.fromtimestamp(p.stat().st_mtime, UTC),
                              mask_pii(p.read_text("utf-8")), "note", register="?"))
            seen.add(p.name)
        return units, {"seen": sorted(seen)}
```

The contract is `mannerism.core.Source`:

- `name` is the entry point name: lowercase letters, digits, `-`, `_`. It must not equal a built-in source, a source
  instance you named, or another installed plugin (such a name is "ambiguous" and refused).
- Yield only text the user wrote. Mask it (`mask_pii`). Other people's text goes only into `Unit.context`, via
  `core.context_line`, at most 3 lines.
- Do not set `meta["public"]`: the engine removes it from plugin units (a plugin is not a publication verifier).
- The cursor is stored in the same transaction as the units. If `read` raises, nothing from that call is stored and
  the previous cursor is kept. A cursor may also carry `_delete_refs` (a list of `source_ref`s to delete with
  everything derived from them), `_stale_prune` and `_error` (a string reported after the commit).
- Do not print or log the text you read.

Install and use it:

```
pip install mannerism-my-notes
mannerism sources            # lists "my-notes" from package metadata; nothing is imported yet
mannerism sources enable my-notes
mannerism sync --source my-notes
```

Installing a package never runs its code inside mannerism. `mannerism sources` only reads package metadata. The plugin
is imported when you run `sources enable NAME`, and later by a full sync. `enable` records the distribution that
provides the plugin, its version and the entry point target. If a *different* distribution later provides the same name,
the plugin is not loaded until you enable it again. If the same distribution is upgraded (or moves its target), it still
loads, and `mannerism sync` shows a warning until you run `enable` again.

**A session start can run plugin code, but only if you ask.** The quick sync that a session start runs never imports a
plugin and never runs one. It may, though, start a *background full sync* for a due source. For a plugin that happens
only when you set `run_on_session_start = true` in its `[sources.NAME]` table (the default is off). A background sync
then loads the plugin, with your permissions, without you running a command.

### Network plugins and schedules

A source that contacts a service sets `network = True` and `network_hosts = ("api.example.org",)` (or a method
`hosts(config)` when the host depends on configuration). Then:

- `mannerism sources enable NAME` prints the hosts it will contact and records `allow_network = true`. Without that
  line in `[sources.NAME]` the source never runs. A source with an `estimate(config)` method (a metered service) also
  shows the cost and asks for confirmation (`--yes` in scripts).
- The quick sync (the one a session start or the MCP server runs) never runs it. If it is due, the quick sync may start a
  **background full sync that contacts the service** (and loads a plugin), but only for sources whose effective
  `run_on_session_start` is true. That is the `[sources.NAME] run_on_session_start` setting; without it, the adapter's
  `session_start_default = True` applies to a free network adapter. It is off for plugins, for adapters that define
  `estimate()` (metered, paid services such as X) and for adapters that do not set the attribute. `sources enable` prints
  which applies. Set it to `false` to keep a source out of session starts, or to `true` to opt a paid one in.
- It runs when it is due: `default_interval_s` (or `interval_s` in `[sources.NAME]`, or `--interval SECONDS` on `enable`)
  after its last success. After a failure it waits 1, 2, 4 ... minutes (at most 6 hours and never longer than its
  interval); a failing source never blocks the others. A source that does not raise can report a failed read by returning
  its cursor with `status = {"error": "...", "at": "<iso>", "retry_after": "<iso>"}`.
- `mannerism sync --force` ignores intervals and backoff (not consent, and not the time budget).
- `mannerism sync --watch [--every SECONDS]` keeps running full syncs in the foreground until Ctrl-C. mannerism
  installs no cron job, launchd agent or systemd timer: schedule `mannerism sync` yourself if you want one.
- `mannerism status` shows each source's last success, next due time and last error (no text).
