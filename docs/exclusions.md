# Control what it reads

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
