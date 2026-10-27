import json
import time
from datetime import UTC, datetime

import pytest

from mannerism.adapters import _social, x_api
from mannerism.adapters.x_api import XApi, estimate, ledger_path, monthly_ceiling

from .conftest import run
from .fakenet import http_error, ok

TOKEN = "AAAA-test-bearer-token-not-real-0123456789"
USER_ID = "2244994945"
CFG = {"allow_network": True, "username": "ana_k"}
ID0 = 1_800_000_000_000_000_000


def tweet(n, text, *, replied_to=None, quoted=None, retweeted=None, note=None, author=USER_ID):
    refs = []
    for typ, tid in (("replied_to", replied_to), ("quoted", quoted), ("retweeted", retweeted)):
        if tid:
            refs.append({"type": typ, "id": str(tid)})
    t = {"id": str(ID0 + n), "text": text, "author_id": author, "created_at": f"2026-10-{1 + n % 28:02d}T09:30:00.000Z"}
    if refs:
        t["referenced_tweets"] = refs
    if note:
        t["note_tweet"] = {"text": note}
    return t


class Server:
    def __init__(self, tweets, protected=False):
        self.tweets = sorted(tweets, key=lambda t: -int(t["id"]))
        self.protected, self.queries, self.fail_on, self.n = protected, [], set(), 0
        self.auth_seen, self.ledger_during, self.lookups = [], [], 0
        self.reject, self.reject_status, self.rejected_k = set(), 410, 0

    def __call__(self, u, q, h):
        assert u.hostname == "api.x.com" and u.scheme == "https"
        self.auth_seen.append(h.get("authorization"))
        if u.path.startswith("/2/users/by/username/"):
            self.lookups += 1
            assert u.path.endswith("/ana_k") and q["user.fields"] == "protected"
            return ok({"data": {"id": USER_ID, "username": "ana_k", "name": "Ana K", "protected": self.protected}})
        assert u.path == f"/2/users/{USER_ID}/tweets" and q["exclude"] == "retweets"
        for f in ("created_at", "referenced_tweets", "note_tweet", "entities"):
            assert f in q["tweet.fields"]
        if q.get("pagination_token") in self.reject:
            self.rejected_k = int(q["max_results"])
            raise http_error(u.geturl(), self.reject_status)
        self.n += 1
        try:
            self.ledger_during.append(json.loads(ledger_path().read_text())["posts"])
        except OSError:
            self.ledger_during.append(None)
        if self.n in self.fail_on:
            raise TimeoutError("slow")
        self.queries.append(q)
        k = int(q["max_results"])
        assert 5 <= k <= 100
        rows = self.tweets
        if q.get("since_id"):
            rows = [t for t in rows if int(t["id"]) > int(q["since_id"])]
        start = int(q.get("pagination_token") or 0)
        page = rows[start:start + k]
        meta = {"result_count": len(page)}
        if page:
            meta.update(newest_id=page[0]["id"], oldest_id=page[-1]["id"])
        if start + k < len(rows):
            meta["next_token"] = str(start + k)
        return ok({"data": page, "meta": meta} if page else {"meta": meta})


@pytest.fixture
def adapter():
    return XApi()


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setenv("X_BEARER_TOKEN", TOKEN)


@pytest.fixture
def clock(monkeypatch):
    state = {"now": datetime(2026, 10, 21, 8, 0, tzinfo=UTC)}
    monkeypatch.setattr(_social, "utcnow", lambda: state["now"])
    monkeypatch.setattr(x_api, "utcnow", lambda: state["now"])
    return state


def loc_for(adapter, config=CFG):
    locs = adapter.discover(None, config)
    assert len(locs) == 1
    return locs[0]


def strip(cur):
    return {k: v for k, v in cur.items() if k not in ("status", "usage")}


def many(n0, n1, text="post number {n} with enough words"):
    return [tweet(n, text.format(n=n)) for n in range(n0, n1)]


def test_contract_attributes():
    a = XApi()
    assert (a.name, a.tier, a.network, a.network_hosts, a.default_interval_s) == ("x", "auto", True, ("api.x.com",), 21600)


def test_discover_no_network_needs_allow_network_username_and_token(adapter, net, me, monkeypatch):
    assert adapter.discover(me, {"username": "ana_k"}) == []
    assert adapter.discover(me, {"allow_network": True}) == []
    assert adapter.discover(me, {"allow_network": True, "username": "bad name!"}) == []
    assert adapter.discover(me, CFG)[0].key == "x:ana_k"
    monkeypatch.delenv("X_BEARER_TOKEN")
    assert adapter.discover(me, CFG) == []
    assert "X_BEARER_TOKEN" in adapter.status_message(CFG) and "not set" in adapter.status_message(CFG)
    assert net.calls == []


def test_missing_token_at_read_time_is_reported_without_a_request(adapter, net, me, monkeypatch):
    loc = loc_for(adapter)
    monkeypatch.delenv("X_BEARER_TOKEN")
    units, cur = run(adapter, loc, None, me)
    assert units == [] and "X_BEARER_TOKEN is not set" in cur["status"]["error"] and net.calls == []


def test_own_posts_replies_quotes_notes_and_retweets(adapter, net, me):
    long_note = "A long-form thought about tooling. " * 12 + "The end, reach me at ana@example.org."
    rows = [
        tweet(1, "shipping the new parser today https://t.co/abc123"),
        tweet(2, "RT @someone: not mine at all", retweeted=ID0 + 900),
        tweet(3, "@bo.x @cy_z thanks, that fixed it &amp; more https://t.co/zzz", replied_to=ID0 + 901),
        tweet(4, "my take on this, with words https://t.co/qqq", quoted=ID0 + 902),
        tweet(5, long_note[:140] + "… https://t.co/long", note=long_note),
        tweet(6, "someone else's tweet in the page", author="999"),
        tweet(7, "https://t.co/onlyalink"),
        tweet(8, "thanks @dee_f for the tip, call +44 7700 900123"),
    ]
    srv = net.handler = Server(rows)
    units, cur = run(adapter, loc_for(adapter), None, me)
    by = {u.source_ref: u for u in units}
    assert sorted(by) == [f"tweet:{ID0 + n}" for n in (1, 3, 4, 5, 8)]
    assert by[f"tweet:{ID0 + 1}"].text == "shipping the new parser today"
    r = by[f"tweet:{ID0 + 3}"]
    assert r.kind == "reply" and r.text == "thanks, that fixed it & more" and r.context == ()
    q = by[f"tweet:{ID0 + 4}"]
    assert q.text == "my take on this, with words" and q.meta["quote"] is True
    assert by[f"tweet:{ID0 + 5}"].text.startswith("A long-form thought") and "[email]" in by[f"tweet:{ID0 + 5}"].text
    assert "…" not in by[f"tweet:{ID0 + 5}"].text
    assert by[f"tweet:{ID0 + 8}"].text == "thanks @[user] for the tip, call [phone]"
    for u in units:
        assert (u.register, u.audience, u.source) == ("public", "public", "x") and u.meta["public"] is True
        assert u.meta["url"] == f"https://x.com/i/status/{u.source_ref.split(':')[1]}"
    assert cur["identity"] == {"username": "ana_k", "id": USER_ID} and cur["high_water"] == str(ID0 + 8)
    assert all(a == f"Bearer {TOKEN}" for a in srv.auth_seen)


def test_token_never_lands_in_cursor_status_or_units(adapter, net, me):
    srv = net.handler = Server(many(1, 4))
    units, cur = run(adapter, loc_for(adapter), None, me)
    srv.fail_on = {srv.n + 1}
    srv.tweets.insert(0, tweet(50, "newer post that fails to download"))
    _, cur2 = run(adapter, loc_for(adapter), cur, me)
    blob = json.dumps(cur) + json.dumps(cur2) + json.dumps([u.text for u in units]) + ledger_path().read_text()
    assert TOKEN not in blob and "Bearer" not in blob
    net.handler = lambda u, q, h: (_ for _ in ()).throw(http_error(u.geturl(), 401))
    _, cur3 = run(XApi(), loc_for(XApi()), None, me)
    assert TOKEN not in json.dumps(cur3) and "HTTP 401" in cur3["status"]["error"]


def test_protected_account_is_refused(adapter, net, me):
    net.handler = Server(many(1, 4), protected=True)
    units, cur = run(adapter, loc_for(adapter), None, me)
    assert units == [] and "protected" in cur["status"]["error"] and "identity" not in cur
    assert not net.paths("/tweets") and "retry_after" in cur["status"]


def test_since_id_and_high_water_promotion(adapter, net, me):
    srv = net.handler = Server(many(1, 11))
    loc = loc_for(adapter)
    u1, c1 = run(adapter, loc, None, me)
    assert len(u1) == 10 and c1["high_water"] == str(ID0 + 10) and "since_id" not in srv.queries[0]
    srv.tweets.insert(0, tweet(20, "a fresh post after the first sync"))
    srv.queries.clear()
    u2, c2 = run(adapter, loc, c1, me)
    assert [u.source_ref for u in u2] == [f"tweet:{ID0 + 20}"] and srv.queries[0]["since_id"] == str(ID0 + 10)
    assert srv.lookups == 1  # the username is resolved once and bound
    assert c2["high_water"] == str(ID0 + 20)


def test_per_run_cap_interrupts_scan_and_next_run_resumes_without_loss(adapter, net, me, clock):
    srv = net.handler = Server(many(1, 61))  # 60 posts, newest first
    loc = loc_for(adapter, {**CFG, "initial_posts": 1000, "max_posts_per_run": 20, "max_posts_per_month": 1000})
    seen = []
    cur = None
    for i in range(1, 6):
        units, cur = run(adapter, loc, cur, me)
        assert len(units) <= 20 and cur["usage"]["posts"] == min(20 * i, 60)
        seen += [u.source_ref for u in units]
        if "scan" not in cur:
            break
    assert len(seen) == len(set(seen)) == 60
    assert cur["high_water"] == str(ID0 + 60) and "status" not in cur  # the per-run cap is normal, not an error
    assert all(int(q["max_results"]) <= 20 for q in srv.queries)


def test_first_run_fetches_only_initial_posts(adapter, net, me):
    srv = net.handler = Server(many(1, 301))
    units, cur = run(adapter, loc_for(adapter, {**CFG, "initial_posts": 15, "max_posts_per_run": 100}), None, me)
    assert len(units) == 15 and "scan" not in cur and cur["high_water"] == str(ID0 + 300)
    assert srv.queries[0]["max_results"] == "15"


def test_default_first_run_is_100_posts_over_two_runs(net, me):
    srv2 = net.handler = Server(many(1, 301))
    a = XApi()
    loc = loc_for(a)
    u1, c1 = run(a, loc, None, me)
    assert len(u1) == 50 and c1["scan"]["fetched"] == 50
    u2, c2 = run(a, loc, c1, me)
    assert len(u2) == 50 and "scan" not in c2 and c2["usage"]["posts"] == 100
    assert srv2.n == 2 and len({u.source_ref for u in u1 + u2}) == 100


def test_monthly_budget_stops_reading_and_reports(adapter, net, me, clock):
    net.handler = Server(many(1, 200))
    loc = loc_for(adapter, {**CFG, "initial_posts": 1000, "max_posts_per_run": 100, "max_posts_per_month": 130})
    u1, c1 = run(adapter, loc, None, me)
    assert len(u1) == 100 and c1["usage"]["posts"] == 100
    u2, c2 = run(adapter, loc, c1, me)
    assert len(u2) == 30 and c2["usage"]["posts"] == 130  # the second request was capped to the room left
    assert "monthly post budget" in c2["status"]["error"] and "scan" in c2
    assert c2["status"]["retry_after"].startswith("2026-11-01")
    n = len(net.calls)
    u3, c3 = run(adapter, loc, c2, me)  # budget gone and backing off: no request at all
    assert u3 == [] and c3 == c2 and len(net.calls) == n


def test_month_rollover_resets_usage_and_resumes(adapter, net, me, clock):
    net.handler = Server(many(1, 200))
    loc = loc_for(adapter, {**CFG, "initial_posts": 1000, "max_posts_per_run": 100, "max_posts_per_month": 100})
    u1, c1 = run(adapter, loc, None, me)
    assert len(u1) == 100
    u1b, c1b = run(adapter, loc, c1, me)
    assert u1b == [] and "monthly post budget" in c1b["status"]["error"]
    clock["now"] = datetime(2026, 11, 1, 0, 5, tzinfo=UTC)
    u2, c2 = run(adapter, loc, c1b, me)
    assert len(u2) == 99  # 199 posts exist; 100 were read in October
    assert c2["usage"]["month"] == "2026-11" and c2["usage"]["posts"] == len(u2)
    assert "scan" not in c2 and "status" not in c2
    assert len({u.source_ref for u in u1 + u2}) == len(u1) + len(u2)


def test_reservation_is_persisted_before_the_request(adapter, net, me):
    srv = net.handler = Server(many(1, 30))
    loc = loc_for(adapter, {**CFG, "max_posts_per_run": 25, "initial_posts": 25})
    run(adapter, loc, None, me)
    assert srv.ledger_during == [25]  # the full page was reserved on disk while the request was in flight


def test_reservation_is_settled_to_the_posts_returned(adapter, net, me):
    srv = net.handler = Server([])
    _, cur = run(adapter, loc_for(adapter, {**CFG, "max_posts_per_run": 25}), None, me)
    assert srv.ledger_during == [25] and cur["usage"]["posts"] == 0  # an empty answer gives the reservation back
    assert json.loads(ledger_path().read_text())["posts"] == 0


class Crash(BaseException):
    pass


def test_crash_cannot_lose_the_accounting(adapter, net, me):
    first = Server(many(1, 30))

    def crashing(u, q, h):
        if u.path.endswith("/tweets"):
            raise Crash()  # the process dies mid-request: read() never returns, the engine stores no cursor
        return first(u, q, h)

    net.handler = crashing
    loc = loc_for(adapter, {**CFG, "max_posts_per_run": 20, "initial_posts": 20})
    with pytest.raises(Crash):
        run(adapter, loc, None, me)
    assert json.loads(ledger_path().read_text())["posts"] == 20
    net.handler = Server(many(1, 30))
    a = XApi()
    _, cur = run(a, loc_for(a, {**CFG, "max_posts_per_run": 20, "initial_posts": 20}), None, me)
    assert cur["usage"]["posts"] == 40  # the crashed request is still counted (the next one adds to it)


def test_http_error_refunds_but_timeout_keeps_the_reservation(adapter, net, me):
    loc = loc_for(adapter, {**CFG, "max_posts_per_run": 20})

    def err_after_lookup(code):
        srv = Server(many(1, 10))

        def handler(u, q, h):
            if u.path.endswith("/tweets"):
                raise http_error(u.geturl(), code)
            return srv(u, q, h)

        return handler

    net.handler = err_after_lookup(503)
    _, cur = run(adapter, loc, None, me)
    assert cur["usage"]["posts"] == 0 and "HTTP 503" in cur["status"]["error"] and cur["identity"]["id"] == USER_ID
    net.handler = lambda u, q, h: (_ for _ in ()).throw(TimeoutError())
    _, cur2 = run(adapter, loc, cur, me)
    assert cur2["usage"]["posts"] == 20  # a timeout may have been billed: stays reserved


def test_429_uses_rate_limit_reset_header(adapter, net, me):
    srv = Server(many(1, 10))
    reset = int(time.time()) + 900

    def handler(u, q, h):
        if u.path.endswith("/tweets"):
            raise http_error(u.geturl(), 429, {"x-rate-limit-reset": str(reset)})
        return srv(u, q, h)

    net.handler = handler
    loc = loc_for(adapter)
    units, cur = run(adapter, loc, None, me)
    assert units == [] and "rate limited" in cur["status"]["error"] and cur["usage"]["posts"] == 0
    retry = datetime.fromisoformat(cur["status"]["retry_after"]).timestamp()
    assert abs(retry - reset) < 5
    n = len(net.calls)
    _, cur2 = run(adapter, loc, cur, me)
    assert cur2 == cur and len(net.calls) == n


def test_network_error_leaves_scan_state_unchanged(adapter, net, me):
    srv = net.handler = Server(many(1, 6))
    loc = loc_for(adapter)
    _, cur = run(adapter, loc, None, me)
    srv.fail_on = {srv.n + 1}
    srv.tweets.insert(0, tweet(40, "newer one that never arrives"))
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and strip(cur2) == strip(cur) and "timed out" in cur2["status"]["error"]


def test_binding_to_user_id_and_foreign_author_dropped(adapter, net, me):
    net.handler = Server([tweet(1, "mine, a normal post with words"), tweet(2, "not mine but returned", author="42")])
    units, _ = run(adapter, loc_for(adapter), None, me)
    assert [u.source_ref for u in units] == [f"tweet:{ID0 + 1}"]


def test_estimate_and_named_rates():
    posts, usd = estimate(None)
    assert posts == 100 and usd == pytest.approx(x_api.COST_PER_USER_LOOKUP_USD + 100 * x_api.COST_PER_POST_READ_USD)
    assert x_api.COST_PER_POST_READ_USD == 0.005 and x_api.COST_PER_USER_LOOKUP_USD == 0.010
    assert estimate({"initial_posts": 500, "max_posts_per_month": 120})[0] == 120
    assert monthly_ceiling(None)[0] == 300 and monthly_ceiling({"max_posts_per_month": 10})[1] == pytest.approx(0.06)
    assert XApi().estimate({"initial_posts": 20}) == (20, pytest.approx(0.11))
    assert estimate({"initial_posts": "junk", "max_posts_per_month": -5}) == estimate(None)


# --- class attributes the CLI and the scheduler read ---------------------------------------------------------------


def test_monthly_ceiling_method_is_float_usd_and_caveat_is_exposed():
    a = XApi()
    assert isinstance(a.monthly_ceiling({"max_posts_per_month": 100}), float)
    assert a.monthly_ceiling({"max_posts_per_month": 100}) == pytest.approx(0.01 + 100 * 0.005)
    assert a.monthly_ceiling(None) == pytest.approx(monthly_ceiling(None)[1])
    assert a.rate_caveat == ("rates are estimates from third-party summaries of X's pay-per-use pricing; "
                             "check your X developer console")
    assert a.session_start_default is False


# --- a rejected continuation token (L3) ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [400, 404, 410])
def test_rejected_token_on_a_first_run_promotes_the_mark_and_counts_only_posts_received(adapter, net, me, monkeypatch, code):
    monkeypatch.setattr(x_api, "MAX_PAGE", 10)
    srv = net.handler = Server(many(1, 100))
    srv.reject, srv.reject_status = {"10"}, code  # page 2's token is refused
    loc = loc_for(adapter, {**CFG, "initial_posts": 25, "max_posts_per_run": 1000, "max_posts_per_month": 1000})
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 10 and "scan" not in cur and f"HTTP {code}" in cur["status"]["error"]
    assert cur["high_water"] == str(ID0 + 99)  # what was read is not read (and paid for) again
    assert cur["usage"]["posts"] == 10  # the rejected request's reservation was given back
    assert json.loads(ledger_path().read_text())["posts"] == 10
    srv.reject = set()
    units, cur2 = run(adapter, loc, {k: v for k, v in cur.items() if k != "status"}, me)
    assert units == [] and cur2["usage"]["posts"] == 10  # nothing new: nothing more is billed


def test_rejected_stored_token_drops_scan_keeps_mark_and_does_not_double_count(adapter, net, me):
    srv = net.handler = Server(many(1, 30))
    loc = loc_for(adapter, {**CFG, "initial_posts": 30, "max_posts_per_run": 30})
    _, cur = run(adapter, loc, None, me)
    used = cur["usage"]["posts"]
    stuck = {**cur, "scan": {"token": "gone", "candidate": cur["high_water"], "fetched": 3}}
    srv.reject = {"gone"}
    units, cur2 = run(adapter, loc, stuck, me)
    assert units == [] and "scan" not in cur2 and cur2["high_water"] == cur["high_water"]
    assert cur2["usage"]["posts"] == used and srv.rejected_k > 0  # reserved before, released after the 410
    assert json.loads(ledger_path().read_text())["posts"] == used
