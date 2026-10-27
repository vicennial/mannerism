# Private server, public-only remote client

Keep the private corpus on one trusted host. A second host can use a public-only
projection over a loopback SSH tunnel. No corpus, source exports, private cards,
rules, leak index, or credentials for source services belong on the client.

```sh
# Private host, after importing or syncing writing:
mannerism public build
mannerism public serve --port 3345

# Client, with an established loopback tunnel:
export MANNERISM_REMOTE_URL=http://127.0.0.1:3345/mcp
mannerism context public --task 'short technical reply'
mannerism lint --register public -
mannerism mcp
```

`public serve` binds only to `127.0.0.1`. The client accepts only loopback HTTP
`/mcp` URLs, disables environment proxies and redirects, and exposes four tools:
`voice_context`, `voice_status`, `voice_lint`, and `voice_feedback`. Non-public
registers, ingestion, setup, work packets, and hooks cannot run through this client.
In remote mode hooks are silent no-ops. This is a trusted-host tunnel design, not
a service to expose to a network or share with untrusted local users.

The private host creates `~/.config/mannerism/public-token` with owner-only
permissions. Transfer that token through your secure host-management channel to
the same location on the client. Never put it in Git, URLs, MCP arguments, or
logs. `MANNERISM_TOKEN_FILE` can select the client's file. The server's optional
`--token-file` selects its file. Use the same token on both ends. Read requests
need no token. Draft logging and feedback require HMAC signatures bound to the
exact tool and arguments, with a 60-second window and durable replay protection.
Keep clocks synchronized. Rotate both files together if a client is retired.

`public.db` is a separately built, atomically replaced SQLite database. It
contains only eligible, human-written, positively published bank rows. Public
labels alone are not publication evidence. AI matches, held records, unpublished
feedback, private-derived cards and rules are excluded. Calibration and cards
are computed from that projection alone. Remote lint does **not** query the
private entity index; it cannot tell whether a name occurs in private writing.
`public status` shows snapshot age and the example count. Regular corpus refresh
rebuilds an existing projection; a failed rebuild removes it rather than serving
stale revoked records. `forget --all` also removes it.

Start the public service only after a successful build. Install both client and
server from a tested, pinned release. Keep old data and release references for
rollback. Stop legacy writers before taking a coordinated migration snapshot.
New drafts and feedback after cutover must be preserved and reconciled if you
roll back; pointing to an old database alone would lose those writes.

## Source identity boundaries during migration

When replacing an old adapter with a different source identity, set an explicit
boundary on the new source to avoid re-importing old history:

```toml
[sources.codex]
enabled = true
since = "2026-10-23T12:00:00Z"
```

`since` must be an ISO timestamp with a timezone. Sync keeps only units strictly
newer than it while advancing the adapter cursor normally. It does not erase
existing records or rewind a cursor if changed. Choose the boundary from verified
legacy coverage; do not invent it from an installation date. Sources that retain
the same stable identity do not need this boundary.

For legacy conversion see [legacy-import.md](legacy-import.md). For local own-post
capture and Hermes configuration see [host-sources.md](host-sources.md); for an
already-decrypted WhatsApp database see [whatsapp-db.md](whatsapp-db.md).
