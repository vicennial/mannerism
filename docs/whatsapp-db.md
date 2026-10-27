# Android WhatsApp databases

`WhatsAppDB` reads explicitly configured, already-decrypted Android `msgstore`
SQLite files. Its source name and every emitted unit's source are `whatsapp_db`.
It does not search folders, inspect backup inboxes, read keys, decrypt files,
run commands, or depend on Voice Kit. The source is in the built-in adapter
registry; decrypting a backup is not part of Mannerism and stays with the host.

Configure this source with:

```json
{
  "paths": ["/explicit/private/tmpfs/msgstore.db"],
  "redact_names": ["A known contact name"]
}
```

`paths` also accepts a single path string. Directories, glob patterns and
unconfigured locations are not read. Duplicate paths resolve to one location.
Use one evolving snapshot path per account. Multiple paths must represent
independent accounts; overlapping snapshots share native identities and can
conflict when one snapshot removes a unit. The adapter does not include the
file path in message identity.

The host can use its separately approved decryption helper to put a plaintext
database in private tmpfs and pass its explicit path. The host owns permissions,
lifetime and cleanup. The adapter opens SQLite with `mode=ro`, enables
`query_only`, and reads one transaction. URI path characters are escaped.
Committed WAL changes remain visible. Missing files cannot be created by a
read. Missing, corrupt or unsupported databases raise an error before a new
cursor or deletion list is returned.

## Supported schema and attribution

The modern Android schema needs:

- `message(_id, chat_row_id, from_me, key_id, timestamp, message_type, text_data)`
- `chat(_id, jid_row_id)`
- `jid(_id, raw_string, server)`

Optional fields are `message.sender_jid_row_id`, `chat.subject`, and
`chat.group_member_count`. Optional `message_forwarded`, `message_system`, and
`message_revoked` tables use `message_row_id`. Their flags exclude own text even
when its type is otherwise eligible. When these tables are absent, their flags
are unavailable; system type `7` and revoked type `15` are still excluded.
Optional `group_participant_user.group_jid_row_id` supplies participant counts.
An older Android `messages` schema is unsupported and fails explicitly.

Only `from_me = 1` supplies style text. Names and configured identity never
override this field. Types `0`, `1`, `3`, `9`, `13`, `42`, and `43` supply text
or captions from `text_data`. Other types, empty text, and invalid timestamps
do not supply style text. Voice-note counts are metadata only. Timestamps are
milliseconds since the Unix epoch and produce timezone-aware UTC datetimes.

The known `⚕ Hermes` bridge header, including its variation-selector and
bold-marker forms, excludes generated replies. Those replies can appear as
masked `agent` context. A thread with this header and no incoming party is
classified `agent`; ordinary direct chats are `close`; `g.us` chats are `group`.
The same classification sets the default register.

## Identity contract with the legacy importer

Root confirmed this contract: the importer maps legacy source `whatsapp` to
`whatsapp_db` and remaps linked unit IDs. The adapter preserves native
`key_id` refs. A singleton uses `key_id`; a burst uses `first_key..last_key`.
Mannerism computes its unit ID from `whatsapp_db` and that ref. The importer
must preserve refs exactly because preserving refs under a different source
name does not deduplicate units.

Thread IDs preserve the legacy SHA-256 pseudonym with salt `voice-kit` and
the first 16 hexadecimal characters. The literal compatibility salt does not
import or invoke the legacy package. Raw JIDs, sender IDs and subjects are not
included in metadata. A missing native `key_id` uses `row:<thread-hash>:<row-id>`;
such malformed rows have no reliable legacy identity. Normal native keys must
be unique across the configured account snapshots, as in the legacy extractor.

Messages sort by chat, timestamp and row ID. Consecutive own messages merge
within 120 seconds of the last retained own message. Consecutive identical
texts are treated as resends; they do not advance that timestamp or the last
retained ref. Numeric mentions become `@[user]` before merging, matching the
legacy extractor. Other masking follows merging, so two different texts that
mask to the same text keep their original burst refs.

An incoming message, including textless media, breaks a burst. A known bridge
reply also breaks it. Own forwarded messages, voice notes, unsupported media,
system messages and revoked messages do not break bursts. Own forwarded text
can appear as masked context. These boundary rules preserve legacy refs even
when an incremental read appends to an existing burst.

## Privacy and incremental refresh

The adapter masks core credentials, email addresses, phone numbers and other
core PII patterns. It additionally masks WhatsApp JIDs, numeric mentions,
group subjects, configured `me.names`, and optional `redact_names` in own text
and context. Android msgstore does not provide contact display names; supply
known names through `redact_names` when they must be removed from prose.
Arbitrary unknown names in prose cannot be inferred or guaranteed removed.
The adapter never emits database author labels as output fields.

Context uses roles (`me`, `other1`, `other2`, `agent`), with at most three lines
of 200 characters each. Names, subjects and raw sender identifiers are not
stored in the cursor. Native message refs remain in it for reconciliation;
masked output is represented by SHA-256 digests.

Every read scans the full SQLite snapshot. The versioned cursor maps each
current unit ref to its digest. Unchanged units are omitted from the result.
Edits, changed context, classification and count metadata update their unit.
Appending to a burst returns the rebuilt burst and `_delete_refs` for its old
ref. Deleted or newly excluded messages similarly replace obsolete bursts or
delete vanished units, including changes below the old maximum row ID.
The engine must consume `_delete_refs` through its existing transactional
delete mechanism, then persist the cursor with the changed units.

On the first adapter read there is no previous cursor inventory to delete.
Root/importer must reconcile any imported legacy refs that are already absent
from the first snapshot. Failed reads leave the saved inventory intact.
Cursor size grows with the number of units; each refresh reads the whole
snapshot to detect edits and revocations reliably.

Tests in `tests/adapters/test_whatsapp_db.py` create synthetic SQLite fixtures.
They need no private backups, keys, messages, or legacy package.
