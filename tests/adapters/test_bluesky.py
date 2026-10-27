import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs

import pytest

from mannerism.adapters import bluesky as bsky
from mannerism.adapters.bluesky import Bluesky, facet_text, normalize_handle

from .conftest import run
from .fakenet import http_error, ok

HANDLE = "ana.example.com"
DID = "did:plc:ana1111111111111111111111"
OTHER_DID = "did:plc:bob2222222222222222222222"
BASE = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
CFG = {"allow_network": True, "handle": HANDLE}


def stamp(minutes_ago: int) -> str:
    return (BASE - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def post(i, text, *, age=None, did=DID, handle=HANDLE, facets=None, reply_to=None, embed=None, indexed=None):
    """One feedViewPost, newest first by `age` (minutes ago, default = i)."""
    age = i if age is None else age
    rec = {"$type": "app.bsky.feed.post", "text": text, "createdAt": stamp(age), "langs": ["en"]}
    if facets:
        rec["facets"] = facets
    if embed:
        rec["embed"] = embed
    item = {"post": {"uri": f"at://{did}/app.bsky.feed.post/3k{i:04d}", "cid": f"bafy{i}",
                     "author": {"did": did, "handle": handle, "displayName": "Ana Example" if did == DID else "Bob B"},
                     "record": rec, "indexedAt": indexed or stamp(age)}}
    if reply_to:
        parent = {"uri": f"at://{OTHER_DID}/app.bsky.feed.post/3kpar", "author": {
            "did": OTHER_DID, "handle": "bob.example.org", "displayName": "Bob B"},
            "record": {"$type": "app.bsky.feed.post", "text": reply_to, "createdAt": stamp(age + 5)}}
        rec["reply"] = {"root": {"uri": parent["uri"], "cid": "x"}, "parent": {"uri": parent["uri"], "cid": "x"}}
        item["reply"] = {"root": parent, "parent": parent}
    return item


def repost(i, text, age=None):
    item = post(i, text, did=OTHER_DID, handle="bob.example.org", age=(age if age is not None else i) + 600)
    item["reason"] = {"$type": "app.bsky.feed.defs#reasonRepost", "by": {"did": DID, "handle": HANDLE},
                      "indexedAt": stamp(i if age is None else age)}
    return item


class Server:
    """getAuthorFeed over a newest-first list; cursor = the index of the next item."""

    def __init__(self, feed, did=DID):
        self.feed, self.did, self.n, self.fail_on = feed, did, 0, set()
        self.limits = []
        self.getposts, self.getposts_fail, self.extra_alive = [], False, []
        self.reject, self.reject_status = set(), 400

    def __call__(self, u, q, h):
        if u.hostname != "public.api.bsky.app":
            raise AssertionError(u.geturl())
        if u.path.endswith("resolveHandle"):
            assert q["handle"] == HANDLE
            return ok({"did": self.did})
        if u.path.endswith("getPosts"):
            self.getposts.append(parse_qs(u.query)["uris"])
            if self.getposts_fail:
                raise TimeoutError("slow")
            alive = {i["post"]["uri"] for i in self.feed} | set(self.extra_alive)
            return ok({"posts": [{"uri": x} for x in parse_qs(u.query)["uris"] if x in alive]})
        assert u.path.endswith("getAuthorFeed") and q["actor"] == DID and q["filter"] == "posts_with_replies"
        self.n += 1
        if self.n in self.fail_on:
            raise TimeoutError("slow")
        if q.get("cursor") in self.reject:
            raise http_error(u.geturl(), self.reject_status)
        start, limit = int(q.get("cursor") or 0), int(q["limit"])
        self.limits.append(limit)
        page = self.feed[start:start + limit]
        out = {"feed": page}
        if start + limit < len(self.feed):
            out["cursor"] = str(start + limit)
        return ok(out)


@pytest.fixture
def adapter():
    return Bluesky()


def loc_for(adapter, config=CFG):
    locs = adapter.discover(None, config)
    assert len(locs) == 1
    return locs[0]


def strip_status(cur):
    return {k: v for k, v in cur.items() if k != "status"}


def test_contract_attributes():
    a = Bluesky()
    assert (a.name, a.tier, a.network, a.network_hosts, a.default_interval_s) == (
        "bluesky", "auto", True, ("public.api.bsky.app",), 3600)


def test_discover_makes_no_network_call_and_needs_allow_network(adapter, net, me):
    assert adapter.discover(me, {"handle": HANDLE}) == []
    assert adapter.discover(me, {"handle": HANDLE, "allow_network": "yes"}) == []
    assert adapter.discover(me, {"allow_network": True}) == []
    assert adapter.discover(me, {"allow_network": True, "handle": "not a handle"}) == []
    assert adapter.status_message({"handle": HANDLE}).startswith("bluesky: set allow_network")
    loc = adapter.discover(me, {"allow_network": True, "handle": "@Ana.Example.com"})[0]
    assert loc.key == f"bluesky:{HANDLE}" and adapter.status_message(CFG) is None
    assert net.calls == []


def test_normalize_handle():
    assert normalize_handle(" @Ana.Example.com ") == HANDLE
    assert normalize_handle("localhost") is None and normalize_handle(None) is None


def test_own_posts_only_reposts_and_foreign_authors_skipped(adapter, net, me):
    feed = [
        post(1, "just shipped the thing, finally"),
        repost(2, "someone else's thought that I reposted"),
        post(3, "a post by another author that leaked into the feed", did=OTHER_DID, handle="bob.example.org"),
        post(4, "second own post about gardening and tea"),
    ]
    net.handler = Server(feed)
    units, cur = run(adapter, loc_for(adapter), None, me)
    assert [u.source_ref for u in units] == [f"at://{DID}/app.bsky.feed.post/3k0001",
                                              f"at://{DID}/app.bsky.feed.post/3k0004"]
    for u in units:
        assert (u.kind, u.register, u.audience, u.source) == ("post", "public", "public", "bluesky")
        assert u.meta["public"] is True and u.meta["url"].startswith(f"https://bsky.app/profile/{DID}/post/3k")
    assert cur["identity"] == {"handle": HANDLE, "did": DID} and cur["v"] == 1
    assert "scan" not in cur and cur["high_water"]["uri"].endswith("3k0001")  # the repost's feed position is not own text


def test_pinned_post_does_not_become_the_high_water_mark(adapter, net, me):
    pinned = post(9, "my pinned introduction post here", age=9000)
    pinned["reason"] = {"$type": "app.bsky.feed.defs#reasonPin"}
    net.handler = Server([pinned, post(1, "the newest post of the day"), post(2, "an older one")])
    units, cur = run(adapter, loc_for(adapter), None, me)
    assert len(units) == 3 and cur["high_water"]["uri"].endswith("3k0001")


def test_text_cleaning_facets_replies_quotes_and_masking(adapter, net, me):
    link_text = "https://example.org/a-long-pat…"
    t1 = f"read this {link_text} please"
    b = t1.encode()
    lo = b.index(link_text.encode())
    f1 = [{"index": {"byteStart": lo, "byteEnd": lo + len(link_text.encode())},
           "features": [{"$type": "app.bsky.richtext.facet#link", "uri": "https://example.org/a-long-path"}]}]
    t2 = "@bob.example.org thanks, mail me at ana@example.org or +44 7700 900123 — and ping @cy.example.net later"
    b2 = t2.encode()
    m1 = b2.index(b"@bob.example.org")
    m2 = b2.index(b"@cy.example.net")
    f2 = [{"index": {"byteStart": m1, "byteEnd": m1 + len("@bob.example.org")},
           "features": [{"$type": "app.bsky.richtext.facet#mention", "did": OTHER_DID}]},
          {"index": {"byteStart": m2, "byteEnd": m2 + len("@cy.example.net")},
           "features": [{"$type": "app.bsky.richtext.facet#mention", "did": "did:plc:cy"}]}]
    quote = {"$type": "app.bsky.embed.record", "record": {"uri": f"at://{OTHER_DID}/app.bsky.feed.post/q", "cid": "c"}}
    named_link = "my notes"
    t4 = f"see {named_link} for more"
    lo4 = t4.index(named_link)
    f4 = [{"index": {"byteStart": lo4, "byteEnd": lo4 + len(named_link)},
           "features": [{"$type": "app.bsky.richtext.facet#link", "uri": "https://example.org/n"}]}]
    feed = [
        post(1, t1, facets=f1),
        post(2, t2, facets=f2, reply_to="Bob B wrote: reach me at bob@corp.example or @dee.example.net for the plan"),
        post(3, "this is exactly why the tooling matters, a lot", embed=quote),
        post(4, t4, facets=f4),
        post(5, "https://example.org/only-a-link"),
    ]
    net.handler = Server(feed)
    units, _ = run(adapter, loc_for(adapter), None, me)
    by = {u.source_ref.rsplit("/", 1)[1]: u for u in units}
    assert by["3k0001"].text == "read this please"
    r = by["3k0002"]
    assert r.kind == "reply" and r.text.startswith("thanks, mail me at [email] or [phone]")
    assert "@bob" not in r.text and r.text.endswith("and ping @[user] later")
    assert len(r.context) == 1 and r.context[0].who == "other1"
    ctx = r.context[0].text
    assert "[email]" in ctx and "@[user]" in ctx and "bob@corp" not in ctx and "dee.example" not in ctx
    assert r.audience_ref and r.audience_ref != OTHER_DID
    assert by["3k0003"].text.startswith("this is exactly why") and by["3k0003"].meta["quote"] is True
    assert not by["3k0003"].context
    assert by["3k0004"].text == "see my notes for more"  # a link facet with real words is kept
    assert "3k0005" not in by  # a pure URL is not writing


def test_facet_text_handles_multibyte_offsets_and_bad_ranges():
    text = "héllo wörld https://example.org/x…"
    raw = text.encode()
    lo = raw.index(b"https")
    good = {"index": {"byteStart": lo, "byteEnd": len(raw)}, "features": [{"$type": "app.bsky.richtext.facet#link"}]}
    bad = {"index": {"byteStart": 50, "byteEnd": 5}, "features": []}
    assert facet_text(text, [good, bad, {"nonsense": 1}], False) == "héllo wörld"
    assert facet_text(text, "not-a-list", False).startswith("héllo wörld https")


def test_self_reply_context_is_marked_me(adapter, net, me):
    own_parent = {"uri": f"at://{DID}/app.bsky.feed.post/3kpar", "author": {"did": DID, "handle": HANDLE},
                  "record": {"text": "part one of my own thread about tooling"}}
    item = post(1, "and part two of the thread, continuing the thought")
    item["post"]["record"]["reply"] = {"root": {"uri": own_parent["uri"]}, "parent": {"uri": own_parent["uri"]}}
    item["reply"] = {"root": own_parent, "parent": own_parent}
    nf = post(2, "reply whose parent was deleted by someone")
    nf["post"]["record"]["reply"] = {"root": {"uri": "at://x/y/z"}, "parent": {"uri": "at://x/y/z"}}
    nf["reply"] = {"root": {"$type": "app.bsky.feed.defs#notFoundPost"}, "parent": {"$type": "app.bsky.feed.defs#notFoundPost"}}
    net.handler = Server([item, nf])
    units, _ = run(adapter, loc_for(adapter), None, me)
    assert units[0].context[0].who == "me" and units[0].audience_ref is None
    assert units[1].context == ()


def test_binding_change_is_refused_and_nothing_is_ingested(adapter, net, me):
    srv = Server([post(1, "first post of the account, hello world")])
    net.handler = srv
    loc = loc_for(adapter)
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 1
    srv.did = OTHER_DID  # the handle now resolves elsewhere (handle hijack / transfer)
    srv.feed = [post(2, "a post by whoever owns the handle now", did=OTHER_DID)] + srv.feed
    net.calls.clear()
    units2, cur2 = run(adapter, loc, cur, me)
    assert units2 == [] and cur2["identity"] == {"handle": HANDLE, "did": DID}
    assert "different DID" in cur2["status"]["error"] and strip_status(cur2) == strip_status(cur)
    assert not net.paths("getAuthorFeed")


def test_pagination_interrupted_then_resumed_without_loss_or_duplicates(adapter, net, me, monkeypatch):
    monkeypatch.setattr(bsky, "PAGE_SIZE", 10)
    feed = [post(i, f"post number {i} with enough words to count") for i in range(1, 36)]
    srv = Server(feed)
    srv.fail_on = {3}  # page 1 and 2 succeed, page 3 times out
    net.handler = srv
    loc = loc_for(adapter)
    u1, cur1 = run(adapter, loc, None, me)
    assert len(u1) == 20 and "high_water" not in cur1
    assert cur1["scan"]["token"] == "20" and cur1["scan"]["candidate"]["uri"].endswith("3k0001")
    assert cur1["status"]["error"].startswith("public.api.bsky.app: timed out")
    u2, cur2 = run(adapter, loc, cur1, me)  # resumes at the stored page token
    assert len(u2) == 15 and "scan" not in cur2 and "status" not in cur2
    assert cur2["high_water"]["uri"].endswith("3k0001")
    refs = [u.source_ref for u in u1 + u2]
    assert len(refs) == len(set(refs)) == 35
    # a new post arrives: only it is read; the old ones are not re-read past the high-water mark
    srv.feed = [post(0, "brand new post after the last sync ran")] + feed
    net.calls.clear()
    u3, cur3 = run(adapter, loc, cur2, me)
    assert [u.source_ref.rsplit("/", 1)[1] for u in u3] == ["3k0000"]
    # the 48 h overlap keeps the scan reading below the high-water mark (the whole 36-item feed is inside it), but
    # nothing already stored is emitted again
    assert len(net.paths("getAuthorFeed")) == 4


def test_pages_per_read_are_bounded_and_resumed(adapter, net, me, monkeypatch):
    monkeypatch.setattr(bsky, "PAGE_SIZE", 5)
    monkeypatch.setattr("mannerism.adapters._social.MAX_PAGES_PER_READ", 2)
    feed = [post(i, f"post number {i} with enough words to count") for i in range(1, 13)]
    net.handler = Server(feed)
    loc = loc_for(adapter, {**CFG, "initial_posts": 100})
    u1, c1 = run(adapter, loc, None, me)
    assert len(u1) == 10 and "scan" in c1 and "status" not in c1
    u2, c2 = run(adapter, loc, c1, me)
    assert len(u2) == 2 and "scan" not in c2


def test_first_run_is_bounded_by_initial_posts(adapter, net, me, monkeypatch):
    monkeypatch.setattr(bsky, "PAGE_SIZE", 10)
    feed = [post(i, f"post number {i} with enough words to count") for i in range(1, 41)]
    srv = Server(feed)
    net.handler = srv
    loc = loc_for(adapter, {**CFG, "initial_posts": 15})
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 15 and srv.limits == [10, 5]
    assert "scan" not in cur and cur["high_water"]["uri"].endswith("3k0001")


def test_default_first_run_bound_is_200(adapter, net, me):
    srv = Server([post(i, f"post number {i} with enough words to count") for i in range(1, 400)])
    net.handler = srv
    units, cur = run(adapter, loc_for(adapter), None, me)
    assert len(units) == 200 and srv.limits == [100, 100] and "scan" not in cur


def test_network_error_leaves_cursor_unchanged(adapter, net, me):
    srv = Server([post(1, "an ordinary post with several words")])
    net.handler = srv
    loc = loc_for(adapter)
    _, cur = run(adapter, loc, None, me)
    srv.fail_on = {2}
    srv.feed.insert(0, post(0, "newer post that will never arrive now"))
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and strip_status(cur2) == strip_status(cur) and cur2["status"]["error"]
    # first-ever read failing at the very first request: no identity is invented
    net.handler = lambda u, q, h: (_ for _ in ()).throw(TimeoutError())
    units, cur3 = run(Bluesky(), loc_for(adapter), None, me)
    assert units == [] and "identity" not in cur3 and cur3["status"]["error"]


def test_429_is_remembered_and_next_read_makes_no_request(adapter, net, me):
    import time

    reset = int(time.time()) + 600
    net.handler = lambda u, q, h: (_ for _ in ()).throw(http_error(u.geturl(), 429, {"ratelimit-reset": str(reset)}))
    loc = loc_for(adapter)
    units, cur = run(adapter, loc, None, me)
    assert units == [] and "retry_after" in cur["status"]
    n = len(net.calls)
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and cur2 == cur and len(net.calls) == n


def test_unusable_feed_shape_is_reported_not_raised(adapter, net, me):
    def handler(u, q, h):
        return ok({"did": DID}) if u.path.endswith("resolveHandle") else ok({"oops": 1})

    net.handler = handler
    units, cur = run(adapter, loc_for(adapter), None, me)
    assert units == [] and "unexpected feed shape" in cur["status"]["error"]


def test_no_credentials_are_sent(adapter, net, me):
    net.handler = Server([post(1, "an ordinary post with several words")])
    run(adapter, loc_for(adapter), None, me)
    assert all("authorization" not in c.headers and c.parts.scheme == "https" for c in net.calls)
    assert json.dumps([c.headers for c in net.calls]).count("mannerism/") == len(net.calls)


# --- ordering: sortAt can sit below newer posts (P2-3) --------------------------------------------------------------


def test_backdated_post_that_sorts_below_the_high_water_mark_is_not_skipped(adapter, net, me):
    a = post(1, "post A written natively by me, on time", age=0)
    # B: cross-posted / clock-skewed. createdAt is earlier than A's, but it was indexed LATER, after the first scan ran;
    # the AppView sorts by about min(createdAt, indexedAt), so it lands below A in the feed.
    b = post(2, "post B created a bit earlier but indexed later", age=2, indexed=stamp(-5))
    srv = Server([a])
    net.handler = srv
    loc = loc_for(adapter)
    u1, c1 = run(adapter, loc, None, me)
    assert [u.text for u in u1] == ["post A written natively by me, on time"]
    srv.feed = [a, b]
    u2, c2 = run(adapter, loc, c1, me)
    assert [u.text for u in u2] == ["post B created a bit earlier but indexed later"]  # A is not emitted again
    u3, _ = run(adapter, loc, c2, me)
    assert u3 == []


def test_scan_stops_once_items_are_older_than_the_overlap_margin(adapter, net, me, monkeypatch):
    monkeypatch.setattr(bsky, "PAGE_SIZE", 2)
    feed = [post(1, "the newest post of the account today", age=0), post(2, "an hour older post of mine", age=60),
            post(3, "a post four days older than the mark", age=60 * 24 * 4), post(4, "a post a week older", age=60 * 24 * 7)]
    srv = Server(feed)
    net.handler = srv
    loc = loc_for(adapter, {**CFG, "initial_posts": 2})
    _, c1 = run(adapter, loc, None, me)
    assert c1["high_water"]["uri"].endswith("3k0001")
    net.calls.clear()
    u2, _ = run(adapter, loc, c1, me)
    assert [u.source_ref.rsplit("/", 1)[1] for u in u2] == []  # nothing new: page 2 holds the first item past 48 h
    assert len(net.paths("getAuthorFeed")) == 2  # it did not walk the whole history


def test_deleted_high_water_post_falls_back_to_twice_the_margin(adapter, net, me, monkeypatch):
    monkeypatch.setattr(bsky, "PAGE_SIZE", 1)
    top = post(1, "the post that will be deleted later", age=0)
    srv = Server([top])
    net.handler = srv
    loc = loc_for(adapter)
    _, c1 = run(adapter, loc, None, me)
    srv.feed = [post(5, "a fresh post after the deletion", age=-60), post(6, "a post sixty hours older", age=60 * 60),
                post(7, "a post a hundred hours older", age=60 * 100)]
    net.calls.clear()
    u2, _ = run(adapter, loc, c1, me)
    assert [u.source_ref.rsplit("/", 1)[1] for u in u2] == ["3k0005", "3k0006"]
    assert len(net.paths("getAuthorFeed")) == 3  # the 60 h post is below the margin but the mark post was never passed


# --- deletes: reconcile (P2-7) ------------------------------------------------------------------------------------


def synced_five(adapter, net, me):
    srv = Server([post(i, f"post number {i} with a handful of words") for i in range(1, 6)])
    net.handler = srv
    loc = loc_for(adapter)
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 5 and "reconciled_at" in cur and len(cur["seen"]) == 5
    return srv, loc, cur


def age_reconcile(cur, days=8):
    cur = dict(cur)
    cur["reconciled_at"] = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    return cur


def test_first_scan_stamps_reconciled_at_and_a_fresh_stamp_does_no_reconcile(adapter, net, me):
    srv, loc, cur = synced_five(adapter, net, me)
    srv.feed.pop(2)  # a deletion on the service
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and "_delete_refs" not in cur2 and srv.getposts == []


def test_reconcile_reports_posts_deleted_on_the_service(adapter, net, me):
    srv, loc, cur = synced_five(adapter, net, me)
    gone = srv.feed.pop(2)["post"]["uri"]
    units, cur2 = run(adapter, loc, age_reconcile(cur), me)
    assert units == [] and cur2["_delete_refs"] == [gone]
    assert gone.rsplit("/", 1)[1] not in cur2["seen"] and len(cur2["seen"]) == 4
    assert srv.getposts == [[gone]]  # only the vanished-looking post was confirmed
    assert datetime.fromisoformat(cur2["reconciled_at"]) > datetime.now(UTC) - timedelta(minutes=1)
    # and the stamp is fresh again: no second reconcile
    srv.getposts.clear()
    run(adapter, loc, {k: v for k, v in cur2.items() if k != "_delete_refs"}, me)
    assert srv.getposts == []


def test_reconcile_interval_is_configurable(adapter, net, me):
    srv = Server([post(i, f"post number {i} with a handful of words") for i in range(1, 4)])
    net.handler = srv
    loc = loc_for(adapter, {**CFG, "reconcile_interval_s": 3600})
    _, cur = run(adapter, loc, None, me)
    gone = srv.feed.pop(1)["post"]["uri"]
    cur["reconciled_at"] = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
    _, cur2 = run(adapter, loc, cur, me)
    assert cur2["_delete_refs"] == [gone]


def test_reconcile_keeps_a_post_that_the_service_still_returns(adapter, net, me):
    srv, loc, cur = synced_five(adapter, net, me)
    odd = srv.feed.pop(2)["post"]["uri"]
    srv.extra_alive = [odd]  # not in the feed window, but getPosts still has it: not deleted
    _, cur2 = run(adapter, loc, age_reconcile(cur), me)
    assert "_delete_refs" not in cur2 and odd.rsplit("/", 1)[1] in cur2["seen"]


def test_failed_reconcile_changes_nothing_and_retries_next_read(adapter, net, me):
    srv, loc, cur = synced_five(adapter, net, me)
    srv.feed.pop(2)
    srv.getposts_fail = True
    old = age_reconcile(cur)
    units, cur2 = run(adapter, loc, old, me)
    assert "_delete_refs" not in cur2 and "status" not in cur2 and cur2["reconciled_at"] == old["reconciled_at"]
    srv.getposts_fail = False
    _, cur3 = run(adapter, loc, cur2, me)
    assert len(cur3["_delete_refs"]) == 1


def test_reconcile_looks_only_at_the_recent_window(adapter, net, me, monkeypatch):
    monkeypatch.setattr(bsky, "RECONCILE_ITEMS", 3)
    srv, loc, cur = synced_five(adapter, net, me)
    # post 5 is outside the 3-item window (and its 1 h floor): a deletion there is not even looked for
    srv.feed.pop()
    _, cur2 = run(adapter, loc, age_reconcile(cur), me)
    assert "_delete_refs" not in cur2 and srv.getposts == []


# --- a rejected continuation token (L3) ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 404, 410])
def test_rejected_continuation_token_drops_the_scan_and_keeps_the_high_water_mark(adapter, net, me, code):
    srv = Server([post(i, f"post number {i} with a handful of words") for i in range(1, 6)])
    net.handler = srv
    loc = loc_for(adapter)
    _, cur = run(adapter, loc, None, me)
    stuck = {**cur, "scan": {"token": "expired", "candidate": cur["high_water"], "fetched": 3}}
    srv.reject, srv.reject_status = {"expired"}, code
    n = len(net.paths("getAuthorFeed"))
    units, cur2 = run(adapter, loc, stuck, me)
    assert "scan" not in cur2 and cur2["high_water"] == cur["high_water"]
    assert f"HTTP {code}" in cur2["status"]["error"] and len(net.paths("getAuthorFeed")) == n + 1
    units, cur3 = run(adapter, loc, cur2, me)  # not stuck: a fresh scan from the top works
    assert units == [] and "status" not in cur3 and "scan" not in cur3


def test_other_http_errors_keep_the_continuation(adapter, net, me):
    srv = Server([post(i, f"post number {i} with a handful of words") for i in range(1, 6)])
    net.handler = srv
    loc = loc_for(adapter)
    _, cur = run(adapter, loc, None, me)
    stuck = {**cur, "scan": {"token": "expired", "candidate": cur["high_water"], "fetched": 3}}
    srv.reject, srv.reject_status = {"expired"}, 503
    _, cur2 = run(adapter, loc, stuck, me)
    assert cur2["scan"]["token"] == "expired"


def test_session_start_default():
    assert Bluesky.session_start_default is True
