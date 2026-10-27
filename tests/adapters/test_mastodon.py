import json
from datetime import UTC, datetime, timedelta

import pytest

from mannerism.adapters import mastodon as masto
from mannerism.adapters.mastodon import Mastodon, html_to_text, parse_account

from .conftest import run
from .fakenet import http_error, ok

INSTANCE = "social.example.org"
ACCT_ID = "109000000000000001"
OTHER_ID = "109000000000000099"
CFG = {"allow_network": True, "account": f"@ana.k@{INSTANCE}"}
ID0 = 112000000000000000


def status(n, html, *, visibility="public", account=ACCT_ID, reblog=None, reply_to=None, reply_acct=None,
           emojis=None, quote=False):
    """Status number n; larger n = newer. Ids are numeric snowflakes as on Mastodon."""
    return {
        "id": str(ID0 + n), "created_at": f"2026-10-{1 + n % 28:02d}T10:00:00.000Z", "visibility": visibility,
        "uri": f"https://{INSTANCE}/users/ana.k/statuses/{ID0 + n}", "url": f"https://{INSTANCE}/@ana.k/{ID0 + n}",
        "content": html, "in_reply_to_id": str(reply_to) if reply_to else None,
        "in_reply_to_account_id": reply_acct, "reblog": reblog, "emojis": emojis or [], "quote": {"state": "accepted"} if quote else None,
        "account": {"id": account, "username": "ana.k", "acct": "ana.k", "display_name": "Ana K"},
    }


class Server:
    def __init__(self, statuses, acct_id=ACCT_ID):
        self.statuses = sorted(statuses, key=lambda s: -int(s["id"]))
        self.acct_id, self.n, self.fail_on, self.queries, self.extra = acct_id, 0, set(), [], {}
        self.single, self.single_error, self.reject, self.reject_status = [], {}, set(), 410

    def __call__(self, u, q, h):
        assert u.hostname == INSTANCE and u.scheme == "https"
        if u.path == "/api/v1/accounts/lookup":
            assert q["acct"] == "ana.k"
            return ok({"id": self.acct_id, "username": "ana.k", "acct": "ana.k"})
        if u.path.startswith("/api/v1/statuses/"):
            pid = u.path.rsplit("/", 1)[1]
            self.single.append(pid)
            if pid in self.single_error:
                raise http_error(u.geturl(), self.single_error[pid])
            if pid in self.extra:
                return ok(self.extra[pid])
            for st in self.statuses:
                if st["id"] == pid:
                    return ok(st)
            raise http_error(u.geturl(), 404)
        assert u.path == f"/api/v1/accounts/{ACCT_ID}/statuses" and q["exclude_reblogs"] == "true"
        self.n += 1
        if self.n in self.fail_on:
            raise TimeoutError("slow")
        if q.get("max_id") in self.reject:
            raise http_error(u.geturl(), self.reject_status)
        self.queries.append(q)
        rows = self.statuses
        if q.get("since_id"):
            rows = [s for s in rows if int(s["id"]) > int(q["since_id"])]
        if q.get("max_id"):
            rows = [s for s in rows if int(s["id"]) < int(q["max_id"])]
        assert int(q["limit"]) <= 40
        return ok(rows[:int(q["limit"])])


@pytest.fixture
def adapter():
    return Mastodon()


def loc_for(adapter, config=CFG):
    locs = adapter.discover(None, config)
    assert len(locs) == 1
    return locs[0]


def strip_status(cur):
    return {k: v for k, v in cur.items() if k != "status"}


def test_contract_attributes():
    a = Mastodon()
    assert (a.name, a.tier, a.network, a.default_interval_s) == ("mastodon", "auto", True, 3600)
    assert a.hosts(CFG) == (INSTANCE,) and a.hosts({}) == ()


def test_discover_makes_no_network_call_and_needs_allow_network(adapter, net, me):
    assert adapter.discover(me, {"account": f"@ana.k@{INSTANCE}"}) == []
    assert adapter.discover(me, {"allow_network": True}) == []
    assert adapter.discover(me, {"allow_network": True, "account": "ana.k"}) == []
    assert adapter.discover(me, {"allow_network": True, "account": "@a@b@c.example"}) == []
    assert adapter.discover(me, {"allow_network": True, "account": "@ana@localhost"}) == []
    assert adapter.discover(me, CFG)[0].key == f"mastodon:ana.k@{INSTANCE}"
    assert net.calls == []


def test_parse_account():
    assert parse_account("@Ana.K@Social.Example.org") == ("Ana.K", "social.example.org")
    assert parse_account("ana@x.example") == ("ana", "x.example")
    assert parse_account("ana") is None and parse_account(None) is None and parse_account("@a@b") is None


def test_html_to_text():
    html = ('<p><span class="h-card"><a href="https://x.example/@bo" class="u-url mention">@<span>bo</span></a></span> '
            'thanks &amp; see <a href="https://example.org/a/long/path" rel="nofollow noopener" target="_blank">'
            '<span class="invisible">https://</span><span class="ellipsis">example.org/a/lon</span>'
            '<span class="invisible">g/path</span></a> <a href="https://x.example/tags/tea" class="mention hashtag" rel="tag">'
            '#<span>tea</span></a> :blobcat:</p><p>second<br>line, ping <span class="h-card"><a href="https://y.example/@cy" '
            'class="u-url mention">@<span>cy</span></a></span></p>')
    assert html_to_text(html, reply=True, emojis=[{"shortcode": "blobcat"}]) == "thanks & see #tea\n\nsecond\nline, ping @[user]"
    assert html_to_text(html, reply=False).startswith("@[user] thanks")
    assert html_to_text("<p>named <a href='https://e.org/x'>my notes</a></p>") == "named my notes"
    assert html_to_text("<p>broken <b>markup") == "broken markup"


def test_own_statuses_only_boosts_and_private_visibility_skipped(adapter, net, me):
    boost_inner = status(0, "<p>someone else's words</p>", account=OTHER_ID)
    rows = [
        status(6, "<p>an ordinary public post of mine</p>"),
        status(5, "<p>boosted</p>", reblog=boost_inner),
        status(4, "<p>unlisted is fine, still public writing</p>", visibility="unlisted"),
        status(3, "<p>followers only, must not be read</p>", visibility="private"),
        status(2, "<p>direct message, must not be read</p>", visibility="direct"),
        status(1, "<p>a status by another account in the page</p>", account=OTHER_ID),
        status(7, "<p>quote post with my own words here</p>", quote=True),
    ]
    net.handler = Server(rows)
    units, cur = run(adapter, loc_for(adapter), None, me)
    refs = sorted(u.source_ref for u in units)
    assert refs == sorted(f"{INSTANCE}/{ID0 + n}" for n in (4, 6, 7))
    for u in units:
        assert (u.kind, u.register, u.audience) == ("post", "public", "public")
        assert u.meta["public"] is True and u.meta["url"].startswith(f"https://{INSTANCE}/@ana.k/")
    assert {u.meta.get("quote") for u in units} == {True, None}
    assert cur["identity"] == {"instance": INSTANCE, "id": ACCT_ID, "acct": "ana.k"}
    assert cur["high_water"] == str(ID0 + 7) and "scan" not in cur
    q = net.paths("/statuses")[0].query
    assert "since_id" not in q and q["exclude_reblogs"] == "true"


def test_reply_strips_mentions_and_gets_one_masked_context_line(adapter, net, me):
    parent = status(1, "<p>Hey Ana, my mail is bob@corp.example, call +44 7700 900123 or ask @dee</p>", account=OTHER_ID)
    parent["account"].update(display_name="Bob Builder", username="bob", acct="bob@other.example")
    srv = Server([
        status(5, '<p><a class="u-url mention" href="https://o.example/@bob">@<span>bob</span></a> will do, thanks</p>',
               reply_to=ID0 + 1, reply_acct=OTHER_ID),
        status(6, "<p>second part of my own thread</p>", reply_to=ID0 + 5, reply_acct=ACCT_ID),
        status(7, "<p>reply to a status that is gone</p>", reply_to=ID0 + 404, reply_acct=OTHER_ID),
    ])
    srv.extra[str(ID0 + 1)] = parent
    net.handler = srv
    units, _ = run(adapter, loc_for(adapter), None, me)
    by = {u.source_ref.rsplit("/", 1)[1]: u for u in units}
    r = by[str(ID0 + 5)]
    assert r.kind == "reply" and r.text == "will do, thanks"
    assert len(r.context) == 1 and r.context[0].who == "other1"
    c = r.context[0].text
    assert "[email]" in c and "[phone]" in c and "@[user]" in c and "bob@corp" not in c and "dee" not in c
    assert by[str(ID0 + 6)].context[0].who == "me" and by[str(ID0 + 6)].context[0].text == "@[user] will do, thanks"
    assert by[str(ID0 + 7)].context == ()  # the parent could not be fetched: no context, still a unit
    assert r.audience_ref and OTHER_ID not in r.audience_ref


def test_context_fetches_are_capped_and_stop_on_rate_limit(adapter, net, me, monkeypatch):
    monkeypatch.setattr(masto, "MAX_CONTEXT_FETCHES", 2)
    rows = [status(n, f"<p>reply number {n} to somebody</p>", reply_to=ID0 - n, reply_acct=OTHER_ID) for n in range(1, 6)]
    srv = Server(rows)
    net.handler = srv
    run(adapter, loc_for(adapter), None, me)
    assert len([c for c in net.calls if "/api/v1/statuses/" in c.parts.path]) == 2


def test_binding_change_is_refused(adapter, net, me):
    srv = Server([status(1, "<p>my first post on this instance</p>")])
    net.handler = srv
    loc = loc_for(adapter)
    _, cur = run(adapter, loc, None, me)
    srv.acct_id = "109000000000000777"  # the name was released and re-registered by somebody else
    net.calls.clear()
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and "different account id" in cur2["status"]["error"]
    assert strip_status(cur2) == strip_status(cur) and not net.paths("/statuses")


def test_pagination_interrupted_then_resumed_without_loss_or_duplicates(adapter, net, me, monkeypatch):
    monkeypatch.setattr(masto, "PAGE_SIZE", 10)
    rows = [status(n, f"<p>post number {n} with enough words</p>") for n in range(1, 36)]
    srv = Server(rows)
    srv.fail_on = {3}
    net.handler = srv
    loc = loc_for(adapter)
    u1, c1 = run(adapter, loc, None, me)
    assert len(u1) == 20 and "high_water" not in c1
    assert c1["scan"]["token"] == str(ID0 + 16) and c1["scan"]["candidate"] == str(ID0 + 35)
    u2, c2 = run(adapter, loc, c1, me)
    assert srv.queries[2]["max_id"] == str(ID0 + 16) and "since_id" not in srv.queries[2]  # resumed at the token
    assert len(u2) == 15 and "scan" not in c2 and "status" not in c2 and c2["high_water"] == str(ID0 + 35)
    refs = [u.source_ref for u in u1 + u2]
    assert len(refs) == len(set(refs)) == 35
    srv.statuses.insert(0, status(40, "<p>a new post after the last sync</p>"))
    srv.queries.clear()
    u3, c3 = run(adapter, loc, c2, me)
    assert [u.source_ref for u in u3] == [f"{INSTANCE}/{ID0 + 40}"] and c3["high_water"] == str(ID0 + 40)
    assert srv.queries[0]["since_id"] == str(ID0 + 35)


def test_first_run_is_bounded(adapter, net, me, monkeypatch):
    rows = [status(n, f"<p>post number {n} with enough words</p>") for n in range(1, 301)]
    srv = Server(rows)
    net.handler = srv
    units, cur = run(adapter, loc_for(adapter, {**CFG, "initial_posts": 50}), None, me)
    assert len(units) == 50 and [q["limit"] for q in srv.queries] == ["40", "10"]
    assert cur["high_water"] == str(ID0 + 300) and "scan" not in cur
    srv2 = Server(rows)
    net.handler = srv2
    units, _ = run(Mastodon(), loc_for(Mastodon()), None, me)
    assert len(units) == 200


def test_network_error_leaves_cursor_unchanged(adapter, net, me):
    srv = Server([status(1, "<p>an ordinary post of mine</p>")])
    net.handler = srv
    loc = loc_for(adapter)
    _, cur = run(adapter, loc, None, me)
    srv.fail_on = {srv.n + 1}
    srv.statuses.insert(0, status(2, "<p>newer, never delivered</p>"))
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and strip_status(cur2) == strip_status(cur) and "timed out" in cur2["status"]["error"]
    net.handler = lambda u, q, h: (_ for _ in ()).throw(http_error(u.geturl(), 401))
    units, cur3 = run(Mastodon(), loc_for(Mastodon()), None, me)  # an instance with a closed public API
    assert units == [] and "HTTP 401" in cur3["status"]["error"] and "identity" not in cur3


def test_pii_masked_in_text_and_custom_emoji_removed(adapter, net, me):
    net.handler = Server([status(1, "<p>write to jo@example.org :wave: key sk-abcdefghijklmnopqrstuvwxyz123456</p>",
                                  emojis=[{"shortcode": "wave"}])])
    units, _ = run(adapter, loc_for(adapter), None, me)
    assert units[0].text == "write to [email] [secret]" or units[0].text == "write to [email] key [secret]"
    assert "jo@example.org" not in units[0].text and ":wave:" not in units[0].text


# --- deletes and edits: reconcile (P2-7) --------------------------------------------------------------------------


def synced(adapter, net, me, n=5):
    srv = Server([status(i, f"<p>post number {i} with a handful of words</p>") for i in range(1, n + 1)])
    net.handler = srv
    loc = loc_for(adapter)
    units, cur = run(adapter, loc, None, me)
    assert len(units) == n and "reconciled_at" in cur and len(cur["seen"]) == n
    return srv, loc, cur


def age(cur, days=8):
    return {**cur, "reconciled_at": (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")}


def test_fresh_reconcile_stamp_means_no_reconcile_requests(adapter, net, me):
    srv, loc, cur = synced(adapter, net, me)
    srv.statuses.pop(2)
    srv.statuses[0]["content"] = "<p>edited text of my newest post</p>"
    srv.single.clear()
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and "_delete_refs" not in cur2 and srv.single == []


def test_reconcile_updates_an_edited_status_under_the_same_ref(adapter, net, me):
    srv, loc, cur = synced(adapter, net, me)
    srv.statuses[1]["content"] = "<p>post number 4, edited to say something better</p>"
    units, cur2 = run(adapter, loc, age(cur), me)
    assert [(u.source_ref, u.text) for u in units] == [(f"{INSTANCE}/{ID0 + 4}", "post number 4, edited to say something better")]
    assert "_delete_refs" not in cur2 and srv.single == []
    # the new fingerprint is stored: the same edit is not emitted again at the next reconcile
    units, _ = run(adapter, loc, age(cur2), me)
    assert units == []


def test_reconcile_reports_a_deleted_status(adapter, net, me):
    srv, loc, cur = synced(adapter, net, me)
    gone = srv.statuses.pop(2)["id"]
    units, cur2 = run(adapter, loc, age(cur), me)
    assert units == [] and cur2["_delete_refs"] == [f"{INSTANCE}/{gone}"]
    assert gone not in cur2["seen"] and len(cur2["seen"]) == 4 and srv.single == [gone]


def test_reconcile_deletes_a_status_that_is_no_longer_public(adapter, net, me):
    srv, loc, cur = synced(adapter, net, me)
    hidden = srv.statuses.pop(1)
    hidden["visibility"] = "private"
    srv.extra[hidden["id"]] = hidden  # the single-status endpoint still answers, but not for the public
    _, cur2 = run(adapter, loc, age(cur), me)
    assert cur2["_delete_refs"] == [f"{INSTANCE}/{hidden['id']}"]


def test_reconcile_leaves_a_status_alone_when_the_check_is_inconclusive(adapter, net, me):
    srv, loc, cur = synced(adapter, net, me)
    gone = srv.statuses.pop(2)["id"]
    srv.single_error[gone] = 500
    _, cur2 = run(adapter, loc, age(cur), me)
    assert "_delete_refs" not in cur2 and gone in cur2["seen"]


def test_failed_reconcile_listing_is_skipped_and_retried(adapter, net, me):
    srv, loc, cur = synced(adapter, net, me)
    srv.statuses.pop(2)
    srv.fail_on = {srv.n + 2}  # the scan makes one request (nothing new); the reconcile listing is the next
    old = age(cur)
    _, cur2 = run(adapter, loc, old, me)
    assert "_delete_refs" not in cur2 and "status" not in cur2 and cur2["reconciled_at"] == old["reconciled_at"]
    _, cur3 = run(adapter, loc, cur2, me)
    assert len(cur3["_delete_refs"]) == 1


def test_reconcile_window_is_bounded(adapter, net, me, monkeypatch):
    monkeypatch.setattr(masto, "RECONCILE_ITEMS", 3)
    srv, loc, cur = synced(adapter, net, me)
    srv.statuses = srv.statuses[:3]  # statuses 1 and 2 are outside the 3-item window: nobody asks about them
    _, cur2 = run(adapter, loc, age(cur), me)
    assert "_delete_refs" not in cur2 and srv.single == []


def test_edit_and_delete_reach_the_store_through_sync(engine_home_masto, net):
    from mannerism import store, sync
    from mannerism.config import Config, save_config

    srv = Server([status(i, f"<p>post number {i} with a handful of words</p>") for i in range(1, 6)])
    net.handler = srv
    save_config(Config(sources={"mastodon": {"enabled": True, "account": CFG["account"], "allow_network": True}}))
    assert sync.sync(sources=["mastodon"], force=True)["sources"]["mastodon"]["new_units"] == 5
    con = store.connect()
    key = f"mastodon:ana.k@{INSTANCE}"
    try:
        con.execute("UPDATE cursors SET cursor_json=? WHERE source='mastodon'",
                    (json.dumps(age(store.get_cursor(con, "mastodon", key), 9)),))
        con.commit()
    finally:
        con.close()
    srv.statuses[0]["content"] = "<p>the newest post, now edited</p>"
    srv.statuses.pop(3)
    rep = sync.sync(sources=["mastodon"], force=True)
    assert rep["sources"]["mastodon"]["deleted_units"] == 1 and rep["sources"]["mastodon"]["updated_units"] >= 1
    con = store.connect()
    try:
        texts = sorted(r[0] for r in con.execute("SELECT text FROM units WHERE source='mastodon'"))
        assert len(texts) == 4 and "the newest post, now edited" in texts
    finally:
        con.close()


@pytest.fixture
def engine_home_masto(tmp_path, monkeypatch):
    monkeypatch.setenv("MANNERISM_HOME", str(tmp_path / "mh"))


# --- a rejected continuation token (L3) ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 404, 410])
def test_rejected_continuation_token_drops_the_scan_and_keeps_the_high_water_mark(adapter, net, me, code):
    srv, loc, cur = synced(adapter, net, me)
    stuck = {**cur, "scan": {"token": str(ID0 + 2), "candidate": cur["high_water"], "fetched": 3}}
    srv.reject, srv.reject_status = {str(ID0 + 2)}, code
    units, cur2 = run(adapter, loc, stuck, me)
    assert "scan" not in cur2 and cur2["high_water"] == cur["high_water"] and f"HTTP {code}" in cur2["status"]["error"]
    units, cur3 = run(adapter, loc, cur2, me)
    assert units == [] and "status" not in cur3 and "scan" not in cur3


def test_session_start_default():
    assert Mastodon.session_start_default is True


def test_reconcile_does_not_backfill_statuses_older_than_the_first_run_bound(adapter, net, me):
    rows = [status(n, f"<p>post number {n} with a handful of words</p>") for n in range(1, 61)]
    net.handler = Server(rows)
    loc = loc_for(adapter, {**CFG, "initial_posts": 10})
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 10
    units, cur2 = run(adapter, loc, age(cur), me)
    assert units == [] and "_delete_refs" not in cur2


def test_a_status_edited_after_it_was_paired_is_paired_again_and_keeps_its_draft(engine_home_masto, net):
    from mannerism import capture, store, sync
    from mannerism.config import Config, save_config

    draft = ("Hi everyone, I wanted to follow up on the invoice batch and confirm the plan for Friday. Please let me "
             "know if you have any questions.")
    final = "I wanted to follow up on the invoice batch and confirm the plan for Friday."
    con = store.connect()
    capture.log_draft(draft, "public", "mcp", con=con, now=datetime.now(UTC) - timedelta(minutes=90))
    con.close()
    st = status(1, f"<p>{draft}</p>")
    st["created_at"] = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    srv = Server([st])
    net.handler = srv
    save_config(Config(sources={"mastodon": {"enabled": True, "account": CFG["account"], "allow_network": True}}))
    assert sync.sync(sources=["mastodon"], force=True)["capture"]["accepted"] == 1
    con = store.connect()
    try:
        key = f"mastodon:ana.k@{INSTANCE}"
        con.execute("UPDATE cursors SET cursor_json=? WHERE source='mastodon'",
                    (json.dumps(age(store.get_cursor(con, "mastodon", key), 9)),))
        con.commit()
    finally:
        con.close()
    srv.statuses[0]["content"] = f"<p>{final}</p>"  # edited on the server after it was posted
    rep = sync.sync(sources=["mastodon"], force=True)
    assert rep["sources"]["mastodon"]["updated_units"] == 1 and rep["capture"]["edited"] == 1
    con = store.connect()
    try:
        rows = con.execute("SELECT status, final FROM edits").fetchall()
        assert [(r["status"], r["final"]) for r in rows] == [("edited", final)]
        assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
    finally:
        con.close()
