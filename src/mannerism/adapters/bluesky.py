"""Bluesky: the user's own public posts, read from the public AppView (no login, no credentials).

Opt-in: `[sources.bluesky]` needs `handle = "name.example.com"` and `allow_network = true`. `discover` makes no network
call; `read` does. The handle is resolved to a DID once (`com.atproto.identity.resolveHandle`), the DID is stored in the
cursor and every post is bound to it: a post counts only when its author DID equals the bound DID. If the handle later
resolves to another DID, the source stops and reports it instead of ingesting.

`app.bsky.feed.getAuthorFeed` (filter `posts_with_replies`) is read newest first. Skipped: reposts, anything not
authored by the bound DID, pinned-post ordering quirks (a pin carries no ordering mark). Kept: the text of the user's own
record (a quote post keeps only the user's words), with link facets that only display a URL removed, the leading
mentions of a reply dropped, other mentions masked as `@[user]`. A reply gets one short masked context line from the
parent post that the feed already contains; the parent's text is never a unit.

Ordering. The AppView sorts the feed by `sortAt`, about min(createdAt, indexedAt), not by `indexedAt`: a backdated or
clock-skewed post sorts BELOW newer ones. So a scan does not stop at the first item older than the high-water mark; it
keeps reading until items are older than `high_water.ts` minus OVERLAP_S (48 h) AND the high-water post itself has been
passed (if that post was deleted, until items are older than twice the margin). The API is free, so the overlap costs
only requests. Posts cannot be edited, so refs already in `seen` are not emitted again.

Deletes. At most once per `reconcile_interval_s` (default 7 days) a completed scan is followed by a reconcile: the
latest RECONCILE_ITEMS feed items are re-read; refs in `seen` that fall inside that window but are missing from it are
confirmed with `app.bsky.feed.getPosts` (at most RECONCILE_CHECKS), and the ones the AppView no longer returns are
reported to the engine in `cursor["_delete_refs"]`, which deletes the stored units and what was derived from them.
Bluesky has no edits. A failed reconcile is skipped silently and retried on the next read.

Cursor: {"v": 1, "identity": {"handle", "did"}, "high_water": {"uri", "ts"} | absent, "scan": {"token", "candidate",
"fetched"} | absent, "seen": {rkey: sort-ts}, "reconciled_at": iso, "status": {"error", "at", "retry_after"?} | absent}.
See `_social.scan`. A continuation token the service rejects (400/404/410) drops `scan`; the next read starts a new one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, timedelta

from ..core import ContextLine, Location, Me, Unit, context_line, hash_ref
from ._http import NetError, build_url, get_json
from ._social import (
    DEFAULT_INITIAL_POSTS,
    DEFAULT_RECONCILE_INTERVAL_S,
    MENTION_MARK,
    RECONCILE_CHECKS,
    RECONCILE_ITEMS,
    backoff_active,
    cap_seen,
    clean_cursor,
    context_text,
    finish_text,
    int_option,
    network_enabled,
    reconcile_due,
    scan,
    utcnow,
    with_status,
)
from ._util import own_unit, parse_iso

HOST = "public.api.bsky.app"
PAGE_SIZE = 100
OVERLAP_S = 48 * 3600  # how far below the high-water mark a scan keeps reading (see the module docstring)
GETPOSTS_BATCH = 25
_HANDLE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_URLISH = re.compile(r"^(?:https?://|www\.)\S+$|^[\w-]+(?:\.[\w-]+)+(?:/\S*)?$", re.IGNORECASE)
_REPOST = "app.bsky.feed.defs#reasonRepost"
_PIN = "app.bsky.feed.defs#reasonPin"


def normalize_handle(value) -> str | None:
    """`@Name.Example.com` -> `name.example.com`; None when it is not a plausible handle."""
    if not isinstance(value, str):
        return None
    h = value.strip().lstrip("@").lower()
    return h if _HANDLE.match(h) else None


def facet_text(text: str, facets, reply: bool) -> str:
    """The post text with link facets that merely display a URL removed and mention facets replaced by a placeholder
    (facet ranges are UTF-8 byte offsets)."""
    raw = text.encode("utf-8")
    spans: list[tuple[int, int, str]] = []
    for f in facets if isinstance(facets, list) else []:
        try:
            idx, feats = f["index"], f["features"]
            lo, hi = int(idx["byteStart"]), int(idx["byteEnd"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= lo < hi <= len(raw)):
            continue
        kinds = {x.get("$type", "") for x in feats if isinstance(x, dict)}
        shown = raw[lo:hi].decode("utf-8", "replace")
        if "app.bsky.richtext.facet#mention" in kinds:
            spans.append((lo, hi, MENTION_MARK))
        elif "app.bsky.richtext.facet#link" in kinds and (_URLISH.match(shown.strip()) or shown.rstrip().endswith(("…", "..."))):
            spans.append((lo, hi, ""))
    out, pos = [], 0
    for lo, hi, repl in sorted(spans):
        if lo < pos:
            continue
        out.append(raw[pos:lo].decode("utf-8", "replace"))
        out.append(repl)
        pos = hi
    out.append(raw[pos:].decode("utf-8", "replace"))
    return finish_text("".join(out), reply)


def _item_ts(item: dict):
    reason = item.get("reason") if isinstance(item.get("reason"), dict) else {}
    if reason.get("$type") == _REPOST:
        return reason.get("indexedAt")
    return (item.get("post") or {}).get("indexedAt")


class Bluesky:
    name = "bluesky"
    tier = "auto"
    network = True
    network_hosts = (HOST,)
    default_interval_s = 3600
    session_start_default = True  # free and light: core may run it at session start (`run_on_session_start`)

    def __init__(self):
        self._config: dict[str, dict] = {}

    def status_message(self, config: dict | None) -> str | None:
        """Why discover finds nothing (for the CLI), or None when the source is ready."""
        if not network_enabled(config):
            return "bluesky: set allow_network = true in [sources.bluesky] to let mannerism read your public posts"
        if normalize_handle((config or {}).get("handle")) is None:
            return "bluesky: set handle = \"name.example.com\" in [sources.bluesky]"
        return None

    def discover(self, me: Me, config: dict) -> list[Location]:
        """No network call. Empty unless `allow_network` is true and a valid `handle` is configured."""
        try:
            if self.status_message(config) is not None:
                return []
            handle = normalize_handle(config.get("handle"))
            key = f"bluesky:{handle}"
            self._config[key] = dict(config)
            return [Location(self.name, key, None, f"Bluesky @{handle} (public posts)")]
        except Exception:  # noqa: BLE001
            return []

    # --- reading ---------------------------------------------------------------------------------------------------

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        orig = cursor if isinstance(cursor, dict) else {}
        try:
            return self._read(loc, orig)
        except Exception as e:  # noqa: BLE001 - a source never raises into the engine
            return [], with_status(clean_cursor(orig), f"{type(e).__name__}: {e}")

    def _read(self, loc: Location, orig: dict):
        cur = clean_cursor(orig)
        if backoff_active(cur):
            return [], orig
        handle = loc.key.split(":", 1)[1]
        cfg = self._config.get(loc.key, {})
        initial = int_option(cfg, "initial_posts", DEFAULT_INITIAL_POSTS)
        ident = cur.get("identity")
        try:
            if ident is None or not cur.get("scan"):  # first run, or a new scan: (re)check the handle -> DID binding
                did = self._resolve(handle)
                if ident is None:
                    ident = cur["identity"] = {"handle": handle, "did": did}
                elif did != ident.get("did"):
                    return [], with_status(
                        cur, f"handle {handle} now resolves to a different DID than the one bound; nothing ingested"
                    )
        except NetError as e:
            return [], with_status(cur, str(e), e.retry_after_s if e.kind == "rate_limit" else None)
        did = ident["did"]

        def fetch(token, limit):
            data, _ = get_json(build_url(HOST, "/xrpc/app.bsky.feed.getAuthorFeed", {
                "actor": did, "filter": "posts_with_replies", "limit": limit, "cursor": token}))
            feed = data.get("feed") if isinstance(data, dict) else None
            if not isinstance(feed, list):
                raise NetError("bad_json", f"{HOST}: unexpected feed shape")
            nxt = data.get("cursor")
            return [x for x in feed if isinstance(x, dict) and isinstance(x.get("post"), dict)], (
                nxt if isinstance(nxt, str) and nxt else None)

        def marker(item):
            reason = item.get("reason") if isinstance(item.get("reason"), dict) else {}
            if reason.get("$type") == _PIN:
                return None
            ts = _item_ts(item)
            return {"uri": item["post"].get("uri"), "ts": ts} if ts else None

        state = {"passed": False}  # has the scan walked past the high-water post itself?

        def at_high_water(item, hw):
            m = marker(item)
            t, ht = parse_iso(m["ts"]), parse_iso((hw or {}).get("ts"))
            if t is None or ht is None:
                return False
            if m["uri"] == hw.get("uri"):
                state["passed"] = True
            margin = timedelta(seconds=OVERLAP_S)
            # sortAt can be earlier than indexedAt, so stop only well below the mark; if the high-water post is gone
            # (deleted), twice the margin is the fallback.
            return t < ht - margin and (state["passed"] or t < ht - 2 * margin)

        seen = dict(cur.get("seen") or {})
        learned: dict[str, str] = {}
        res = scan(cur, fetch=fetch, marker=marker, at_high_water=at_high_water,
                   convert=lambda item: self._convert(item, did, seen, learned), page_size=PAGE_SIZE, initial=initial)
        seen.update(learned)
        new = res.cursor
        deletes: list[str] = []
        units = res.units
        if res.error is None and res.completed:
            now = utcnow()
            interval = int_option(cfg, "reconcile_interval_s", DEFAULT_RECONCILE_INTERVAL_S)
            if "reconciled_at" not in new:
                new["reconciled_at"] = now.isoformat(timespec="seconds")
            elif reconcile_due(new, interval, now):
                gone = self._reconcile(fetch, did, seen)
                if gone is not None:  # None: it could not finish; try again on the next read
                    deletes = gone
                    for ref in gone:
                        seen.pop(ref.rsplit("/", 1)[-1], None)
                    new["reconciled_at"] = now.isoformat(timespec="seconds")
        new["seen"] = cap_seen(seen, lambda k, v: v)
        if deletes:
            new["_delete_refs"] = deletes
        if res.error is not None:
            return units, with_status(new, str(res.error),
                                      res.error.retry_after_s if res.error.kind == "rate_limit" else None)
        return units, new

    def _reconcile(self, fetch, did: str, seen: dict) -> list[str] | None:
        """Refs (AT URIs) of stored posts that the AppView no longer has, among the latest RECONCILE_ITEMS feed items.
        None when the check could not be completed. A post is reported only after `getPosts` confirms it is gone."""
        items: list[dict] = []
        token = None
        try:
            while len(items) < RECONCILE_ITEMS:
                page, token = fetch(token, min(PAGE_SIZE, RECONCILE_ITEMS - len(items)))
                items += page
                if token is None or not page:
                    break
            present = set()
            stamps = []
            for item in items:
                post = item["post"]
                reason = item.get("reason") if isinstance(item.get("reason"), dict) else {}
                if reason.get("$type") == _PIN:
                    continue
                t = parse_iso(_item_ts(item))
                if t is not None:
                    stamps.append(t)
                if reason.get("$type") != _REPOST and (post.get("author") or {}).get("did") == did:
                    present.add(str(post.get("uri")).rsplit("/", 1)[-1])
            floor = min(stamps) + timedelta(hours=1) if token is not None and stamps else None
            missing = [rk for rk, ts in seen.items() if rk not in present
                       and (floor is None or ((parse_iso(ts) or floor) >= floor))]
            missing = sorted(missing)[:RECONCILE_CHECKS]
            gone = []
            for i in range(0, len(missing), GETPOSTS_BATCH):
                batch = missing[i:i + GETPOSTS_BATCH]
                uris = [f"at://{did}/app.bsky.feed.post/{rk}" for rk in batch]
                data, _ = get_json(build_url(HOST, "/xrpc/app.bsky.feed.getPosts", {"uris": uris}))
                posts = data.get("posts") if isinstance(data, dict) else None
                if not isinstance(posts, list):
                    return None
                alive = {p.get("uri") for p in posts if isinstance(p, dict)}
                gone += [u for u in uris if u not in alive]
            return gone
        except NetError:
            return None

    def _resolve(self, handle: str) -> str:
        data, _ = get_json(build_url(HOST, "/xrpc/com.atproto.identity.resolveHandle", {"handle": handle}))
        did = data.get("did") if isinstance(data, dict) else None
        if not isinstance(did, str) or not did.startswith("did:"):
            raise NetError("bad_json", f"{HOST}: handle did not resolve to a DID")
        return did

    # --- one feed item ---------------------------------------------------------------------------------------------

    def _convert(self, item: dict, did: str, seen: dict, learned: dict) -> list[Unit]:
        reason = item.get("reason") if isinstance(item.get("reason"), dict) else {}
        if reason.get("$type") == _REPOST:
            return []
        post = item["post"]
        author = post.get("author") if isinstance(post.get("author"), dict) else {}
        rec = post.get("record") if isinstance(post.get("record"), dict) else {}
        if author.get("did") != did or rec.get("$type", "app.bsky.feed.post") != "app.bsky.feed.post":
            return []
        uri = post.get("uri")
        text = rec.get("text")
        if not isinstance(uri, str) or not isinstance(text, str):
            return []
        is_reply = isinstance(rec.get("reply"), dict)
        text = facet_text(text, rec.get("facets"), is_reply)
        ts = parse_iso(rec.get("createdAt"))
        indexed = parse_iso(post.get("indexedAt"))
        if ts is None or (indexed is not None and ts > indexed):
            ts = indexed or ts
        if ts is None or not text:
            return []
        embed = rec.get("embed") if isinstance(rec.get("embed"), dict) else {}
        quote = str(embed.get("$type", "")).startswith("app.bsky.embed.record")
        context: list[ContextLine] = []
        thread, audience_ref = hash_ref(uri), None
        if is_reply:
            root = (rec["reply"].get("root") or {}).get("uri")
            thread = hash_ref(root if isinstance(root, str) else uri)
            parent = (item.get("reply") or {}).get("parent") if isinstance(item.get("reply"), dict) else None
            line = self._parent_context(parent, did)
            if line is not None:
                context.append(line)
            if isinstance(parent, dict) and isinstance(parent.get("author"), dict) and parent["author"].get("did") != did:
                audience_ref = hash_ref(str(parent["author"].get("did")))
        rkey = uri.rsplit("/", 1)[-1]
        if rkey in seen:
            return []  # posts cannot be edited: already stored
        meta = {"public": True, "url": f"https://bsky.app/profile/{did}/post/{rkey}"}
        if quote:
            meta["quote"] = True
        u = own_unit(source=self.name, ref=uri, ts=ts, text=text, kind="reply" if is_reply else "post",
                     register="public", audience="public", thread_id=thread, audience_ref=audience_ref,
                     context=context, meta=meta)
        if u is None:
            return []
        learned[rkey] = ts.astimezone(UTC).isoformat(timespec="seconds")
        return [u]

    @staticmethod
    def _parent_context(parent, did: str) -> ContextLine | None:
        if not isinstance(parent, dict) or not isinstance(parent.get("record"), dict):
            return None  # notFoundPost / blockedPost: no text to show
        text = parent["record"].get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        pa = parent.get("author") if isinstance(parent.get("author"), dict) else {}
        if pa.get("did") == did:
            return context_line("me", context_text(text))
        names = {n: "other1" for n in (pa.get("displayName"), pa.get("handle")) if isinstance(n, str)}
        return context_line("other1", context_text(text), names)
