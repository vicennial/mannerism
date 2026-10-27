# Import a Voice Kit corpus

The Python API converts a legacy Voice Kit corpus into the directory selected by
`MANNERISM_HOME`. The default destination is Mannerism's usual data directory.
Use a separate, empty destination for the first import.

```python
from mannerism.legacy import import_voicekit

report = import_voicekit("/path/to/legacy-backup")  # no destination or corpus data writes
report = import_voicekit("/path/to/legacy-backup", apply=True)
```

The path can name the corpus directory or its `corpus.db`. The importer also
reads an adjacent `drafts.db`, direct Markdown files in `cards/`, and an optional
`migration-cards.json`. It does not discover exports, read credentials, contact
providers, or alter source records. SQLite sources open with `mode=ro` and an
explicit read transaction, which includes committed WAL records. SQLite may
create `-wal` and `-shm` read-coordination sidecars. These sidecars are permitted;
logical corpus writes and changes to existing main database or WAL content are
not. Do not use `immutable=1` on a live WAL source, because it can miss committed
records.

Pause source writers, take coordinated backups of the source databases, and
import the backup copies. Keep each database and its WAL together when copying
SQLite files. Separate source databases have separate read snapshots; the
importer does not create a cross-database backup or coordinate source writers.

The dry-run report contains aggregate counts and mappings. It contains no source
text, identifiers, or paths. Dry runs make no destination or corpus data writes.
They do not open or create the destination. SQLite read-coordination sidecars
can still appear beside a WAL source during a dry run.
Destination validation occurs only with `apply=True`. Source read errors abort
before destination writes. A populated, unrelated destination is refused on
apply, including an already initialized Mannerism database.

| Legacy register | Mannerism register |
| --- | --- |
| public | public |
| casual_close | close |
| casual_group | group |
| semiformal | work |
| formal | formal |
| directive | agent |
| draft, unknown, missing | skip |

WhatsApp chat metadata resolves close versus group as in Voice Kit. Legacy
`whatsapp` sources become `whatsapp_db`; their native `source_ref` stays the same,
including burst ranges. Unit IDs and linked labels, edits, and AI flags are
remapped together. Other source names keep their identity.

The selected label is the highest numeric version, then latest `labeled_at`, then
lexicographically greatest model name. The full serialized row breaks any
remaining tie. Every label version remains in the local migration audit. Missing
or invalid quality is zero, never an artificial high score. Held units remain
held. Drafts and unknown labels cannot become style evidence.

The importer rebuilds features, bank rows, statistics, leak detection, and stats
cards with Mannerism APIs. A public label is not publication proof. Only a native
X source or an explicit source `public: true` can supply that proof, and explicit
private-fact and third-party-detail checks must both pass. AI-suspect labels,
legacy assisted long posts, accepted captures, and captures with ratio at least
0.75 stay excluded. Linked edited finals use the existing unit once. Private
standalone feedback never becomes a public example.

Active draft-log text is stripped and PII-masked as in native draft capture.
Active edit and rejection text uses native PII masking. An edit's `final_sha` is
the SHA-256 of `capture.clean(final).lower()`, and native-equivalent pairs dedupe
on that hash and the masked draft. Submitting the same pair through
`api.feedback` after import does not add an edit. Missing ratios are computed
with native capture comparison on cleaned lowercase text. A linked edited unit
with ratio at least 0.75 remains excluded as mostly AI, even when its preserved
legacy status is `edited`. Original text, hashes, ratios, and statuses remain
unchanged in the audit.

Draft text, times, origins, targets, and available edit links survive. Edited and
accepted feedback retain their statuses. Rejected feedback enters `rejections`
and remains in `legacy_voicekit_rows`, outside Mannerism's `edits` table, whose
status constraint does not accept `rejected`. It cannot enter the bank or rule distillation.
Unlinked public X capture feedback remains active in `edits` with origin
`x-capture` and no linked unit. It also remains in the audit. The importer never
creates a unit or publication flag for it. Mannerism excludes public standalone
feedback from the example bank; public rule eligibility is enforced separately.
`feedback_sources` records each known mapped unit linkage for edits and
rejections, including the mapped source, native reference, and destination unit
ID. A structured `x:<id>` task can also record a source association with no unit;
that association alone does not prove publication. Generic unlinked feedback
without a source association stays generic.

`feedback_drafts` records mapped draft-log associations for both edits and
rejections. The importer resolves an explicit mapped draft ID first, then uses
the existing masked draft-text hash fallback. Every available association is
retained, including associations from native-equivalent feedback that dedupes
to one active record. Shared source and scoped unit erasure use these links to
remove associated drafts. Edit and rejection IDs have separate namespaces;
the link kind and archived rejection status distinguish overlapping IDs.

An unlinked edit receives task-based `published=1` only when its legacy origin
is exactly `x-capture` and its task post ID, edited/accepted status, and capture
timestamp match the corresponding `x_capture` decision. Missing, unmatched,
stale, or contradictory decisions do not prove publication. For linked units,
publication comes from the mapped unit's validated source provenance and privacy
flags; a task never upgrades a private unit. Rejection provenance always uses
`published=0`. Shared rule eligibility and source erasure use this table;
importing provenance does not synthesize published units or bank examples.

Unlinked X capture feedback in other registers stays audit-only, because those
registers can admit standalone finals as examples. Legacy split assignments
remain audit records; they are not silently converted into a current evaluation
holdout.

Erasing all data, drafts, or a source also clears the migration archive and its
completion marker, because those records can contain extra copies and mixed
references. Scoped unit deletion removes linked archived records and archived
cards. Original backup files outside Mannerism are not erased by these commands.

The destination database contains a private local migration archive:

- `legacy_voicekit_rows` stores original units, all label versions, feedback,
  capture decisions, drafts, splits, and ingest provenance. `target_id` links
  imported records to their destination identity where available.
- Each unit's `meta_json.legacy_voicekit` retains its original unit and the
  selected original label, including review and provenance fields.
- `legacy_voicekit_cards` stores every original card, its review provenance,
  activation status, and validation results. This table can contain private
  text; do not publish or export it as public content.

Original cards activate only when they pass Mannerism's current privacy,
injection, and length validation, have eligible evidence in the mapped register,
and have explicit matching review provenance. To supply that provenance, place
a JSON object in the legacy directory's `migration-cards.json`. Each key is a
direct card filename, such as `public.md`. Its value must contain `reviewed:
true`, `reviewer`, `reviewed_at`, and the exact text's hexadecimal SHA-256 in
`sha256`. The importer never creates or infers a review. Cards without matching
review evidence, oversized cards, and unsafe cards stay archived. Stats cards
provide the active fallback. Review and shorten archived cards separately if
needed; the importer does not rewrite them.

Activated cards record `card_sources:<register>` for later source invalidation.
The importer conservatively includes every imported/current unit source across
registers, even sources with no currently eligible bank rows. Legacy cards could
borrow evidence or use units that fail today's bank criteria. Attributable bank
sources are included as well. If any eligible bank evidence has no attributable
unit source, the importer archives the card instead of activating it without a
complete source dependency set. Forgetting any recorded source invalidates the
activated card through the shared erase workflow.

Import data commits in one transaction. A failed data write rolls back and can
be retried. A failed derived-file rebuild resumes from committed data without
duplicating units or feedback. A completed repeat with the identical snapshot
does not rewrite records or cards. The source snapshot fingerprint is local
metadata and is not included in the report. A changed source snapshot or a
different source corpus is refused; this API is a one-time conversion, not an
incremental sync. Keep normal source ingestion and other destination writers
stopped until conversion completes. No live migration or deployment is performed
by installing this module.
