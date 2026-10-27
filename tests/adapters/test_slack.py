"""Slack source: fixtures are written by hand from the Slack Web API documentation (auth.test, search.messages,
chat.getPermalink) with fictional people and ids. Nothing here touches a socket (see fakenet.py)."""
import json
import math
import time
from datetime import UTC, datetime, timedelta

import pytest

from mannerism import cli, store, sync
from mannerism.adapters import slack as slk
from mannerism.adapters.slack import Slack, slack_text
from mannerism.config import Config, save_config

from .conftest import run
from .fakenet import http_error, ok

TOKEN = "xoxp-" + "synthetic-test-value-0001"  # shaped like a user token, not like a real one
UID, TEAM = "U0ANAK001", "T0TEAM001"
OTHER = "U0OTHER001"
GENERAL = {"id": "C0GEN0001", "name": "general", "is_channel": True, "is_im": False, "is_mpim": False}
PRIVATE = {"id": "G0PRIV001", "name": "finance-leads", "is_channel": False, "is_group": True, "is_private": True,
           "is_im": False, "is_mpim": False}
DM = {"id": "D0DM00001", "name": OTHER, "is_im": True, "is_channel": False, "is_mpim": False}
GROUP_DM = {"id": "G0MPIM001", "name": "mpdm-ana.k--bo.t--cy.z-1", "is_mpim": True, "is_im": False}
BASE = int(time.time()) - 3 * 86400  # ts of "message 0"; larger n = newer
CFG = {"allow_network": True}


def mts(n):
    return f"{BASE + n * 60}.{100 + n:06d}"


def msg(n, text, *, user=UID, channel=GENERAL, **extra):
    m = {"type": "message", "user": user, "username": "ana.k", "ts": mts(n), "text": text, "channel": channel,
         "team": TEAM, "permalink": f"https://demo.example.slack.com/archives/{channel['id']}/p{mts(n).replace('.', '')}"}
    m.update(extra)
    return m


class Server:
    """auth.test + search.messages (newest first, page/count) + chat.getPermalink over a list of message matches."""

    def __init__(self, messages, uid=UID, team=TEAM, scopes="search:read,identify"):
        self.messages = list(messages)
        self.uid, self.team, self.scopes = uid, team, scopes
        self.honor_query = True
        self.exists = None  # refs/ts that chat.getPermalink still answers for (None: every ts in `messages`)
        self.permalink_error = {}  # ts -> slack error code, or an int HTTP status
        self.inject = {}  # method -> FIFO list of Exception | dict (replaces the normal answer once)
        self.calls = []
        self.searches = []

    def __call__(self, u, q, h):
        assert u.scheme == "https" and u.hostname == "slack.com", u.geturl()
        assert h["authorization"] == f"Bearer {TOKEN}" and TOKEN not in u.geturl()
        method = u.path.removeprefix("/api/")
        self.calls.append(method)
        queue = self.inject.get(method)
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            if item is not None:
                return ok(item)
        if method == "auth.test":
            return ok({"ok": True, "url": "https://demo.example.slack.com/", "team": "Demo", "user": "ana.k",
                       "team_id": self.team, "user_id": self.uid}, {"x-oauth-scopes": self.scopes})
        if method == "search.messages":
            return self._search(q)
        if method == "chat.getPermalink":
            return self._permalink(q)
        raise AssertionError(f"unexpected Slack method {method}")

    def _search(self, q):
        assert q["query"] == f"from:<@{self.uid}>" and q["sort"] == "timestamp" and q["sort_dir"] == "desc"
        count, page = int(q["count"]), int(q["page"])
        assert 1 <= count <= 100 and 1 <= page <= 100
        self.searches.append(q)
        rows = sorted(self.messages, key=lambda m: -float(m["ts"]))
        if self.honor_query:
            rows = [m for m in rows if m.get("user") == self.uid]
        pages = max(1, math.ceil(len(rows) / count))
        return ok({"ok": True, "messages": {"total": len(rows), "paging": {"count": count, "total": len(rows),
                                                                           "page": page, "pages": pages},
                                            "matches": rows[(page - 1) * count:page * count]}})

    def _permalink(self, q):
        err = self.permalink_error.get(q["message_ts"])
        if isinstance(err, int):
            raise http_error("https://slack.com/api/chat.getPermalink", err)
        if err:
            return ok({"ok": False, "error": err})
        alive = {m["ts"] for m in self.messages} if self.exists is None else self.exists
        if q["message_ts"] not in alive:
            return ok({"ok": False, "error": "message_not_found"})
        return ok({"ok": True, "channel": q["channel"], "permalink": "https://demo.example.slack.com/archives/x"})

    def drop(self, n):
        self.messages = [m for m in self.messages if m["ts"] != mts(n)]


@pytest.fixture(autouse=True)
def _token(monkeypatch):
    monkeypatch.setenv(slk.TOKEN_ENV, TOKEN)


@pytest.fixture
def adapter():
    return Slack()


def loc_for(adapter, config=CFG):
    locs = adapter.discover(None, config)
    assert len(locs) == 1
    return locs[0]


def own(n_from=1, n_to=5, **kw):
    return [msg(i, f"message number {i} with a handful of words in it", **kw) for i in range(n_from, n_to + 1)]


def synced(adapter, net, me, messages=None, config=CFG):
    srv = Server(messages if messages is not None else own())
    net.handler = srv
    loc = loc_for(adapter, config)
    units, cur = run(adapter, loc, None, me)
    assert "status" not in cur
    return srv, loc, cur, units


def age(cur, days=8):
    return {**cur, "reconciled_at": (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")}


def texts(units):
    return sorted(u.text for u in units)


# --- contract and gating --------------------------------------------------------------------------------------------


def test_contract_attributes():
    a = Slack()
    assert (a.name, a.tier, a.network, a.default_interval_s) == ("slack", "auto", True, 3600)
    assert a.network_hosts == ("slack.com",)


def test_session_start_default_matches_the_other_free_sources():
    from mannerism import schedule
    from mannerism.adapters.bluesky import Bluesky
    assert Slack.session_start_default is True and Bluesky.session_start_default is True
    assert schedule.run_on_session_start({}, Slack()) is True
    assert schedule.run_on_session_start({"run_on_session_start": False}, Slack()) is False


def test_discover_makes_no_network_call_and_needs_consent_and_a_user_token(adapter, net, me, monkeypatch):
    assert adapter.discover(me, {}) == []  # no allow_network
    assert adapter.discover(me, {"allow_network": "yes"}) == []
    for bad in ("", "xoxb-" + "bot-token-value-0001", "xoxc-" + "browser-token-0001", "xoxp-", "nonsense"):
        monkeypatch.setenv(slk.TOKEN_ENV, bad)
        assert adapter.discover(me, CFG) == [], bad
        assert "slack:" in adapter.status_message(CFG)
    monkeypatch.delenv(slk.TOKEN_ENV)
    assert adapter.discover(me, CFG) == [] and slk.TOKEN_ENV in adapter.status_message(CFG)
    monkeypatch.setenv(slk.TOKEN_ENV, "xoxe." + TOKEN)  # a rotating user token
    assert adapter.discover(me, CFG)[0].key == "slack:user"
    assert net.calls == []


def test_status_messages_never_contain_the_token(adapter, monkeypatch):
    monkeypatch.setenv(slk.TOKEN_ENV, "xoxb-" + "secret-bot-value-0001")
    assert "secret-bot-value" not in adapter.status_message(CFG)


# --- text -------------------------------------------------------------------------------------------------------------


def test_slack_text_mentions_channels_links_quotes_code_and_entities():
    raw = ("<@U0OTHER001> thanks, see <#C0GEN0001|general> and <https://example.org/a/b|the doc>, "
           "<https://example.org/bare> <mailto:jo.dev@example.org|jo.dev@example.org> &amp; cc <@U0BOT0001|bo.t> "
           "<!here> <!subteam^S0GRP0001|@oncall>\n&gt; someone else said this\n```\nrm -rf build\n```\nmy actual point &lt;3")
    assert slack_text(raw) == ("@[user] thanks, see #[channel] and the doc, jo.dev@example.org & cc @[user] @[channel] @[group]\n\n"
                               "my actual point <3")
    assert slack_text("<@U0OTHER001> <@U0BOT0001> sounds good, will do", reply=True) == "sounds good, will do"
    assert slack_text("&gt;&gt;&gt; the whole forwarded block\nstill quoted") == ""
    assert slack_text("mine first\n&gt;&gt;&gt; quoted\nstill quoted") == "mine first"
    assert slack_text("cc @bob.b about it") == "cc @[user] about it"
    assert slack_text("<!date^1700000000^{date_short}|Nov 14> works") == "Nov 14 works"


# --- which messages are kept --------------------------------------------------------------------------------------------


def test_own_messages_only_bots_subtypes_and_file_only_are_dropped(adapter, net, me):
    rows = [
        msg(9, "an ordinary message of mine with some words"),
        msg(8, "someone else's words that a search quirk returned", user=OTHER),
        msg(7, "posted by an integration on my behalf, with words", bot_id="B0BOT0001"),
        msg(6, "a bot_message subtype, with words in it", subtype="bot_message"),
        msg(5, "joined the channel with words in the text", subtype="channel_join"),
        msg(4, "changed the topic to something with words", subtype="channel_topic"),
        msg(3, "shrugs a long and pointless me message", subtype="me_message"),
        msg(2, "", subtype="file_share"),
        msg(1, "file with a comment from me, with some words", subtype="file_share"),
        msg(0, "broadcast from a thread to the channel, words", subtype="thread_broadcast"),
        msg(10, "<https://example.org/only-a-link>"),
        msg(11, "&gt; only a quote from somebody else"),
        msg(12, "```only code```"),
    ]
    srv = Server(rows)
    srv.honor_query = False  # a search result set that includes other people's messages: the adapter still refuses them
    net.handler = srv
    units, cur = run(adapter, loc_for(adapter), None, me)
    assert texts(units) == sorted(["an ordinary message of mine with some words",
                                   "file with a comment from me, with some words",
                                   "broadcast from a thread to the channel, words"])
    assert all(u.kind == "message" and u.meta["public"] is False and u.source == "slack" for u in units)
    assert all(not r.endswith(f":{mts(8)}") for r in cur["seen"])


def test_unit_shape_ref_register_audience_and_thread(adapter, net, me):
    root = msg(1, "the root of a thread, written by me")
    reply = msg(2, "<@U0OTHER001> a reply inside the thread, from me", thread_ts=mts(1))
    reply_by_link = msg(3, "<@U0OTHER001> another reply, only the permalink knows", permalink=(
        f"https://demo.example.slack.com/archives/C0GEN0001/p1?thread_ts={mts(1)}&cid=C0GEN0001"))
    srv, loc, cur, units = synced(adapter, net, me, [root, reply, reply_by_link])
    by_ref = {u.source_ref: u for u in units}
    assert set(by_ref) == {f"{TEAM}:C0GEN0001:{mts(i)}" for i in (1, 2, 3)}
    r1, r2, r3 = (by_ref[f"{TEAM}:C0GEN0001:{mts(i)}"] for i in (1, 2, 3))
    assert r2.text == "a reply inside the thread, from me" and r3.text == "another reply, only the permalink knows"
    assert r1.thread_id == r2.thread_id == r3.thread_id
    assert (r1.kind, r1.register, r1.audience, r1.audience_ref) == ("message", "work", "group", None)
    assert r1.meta["public"] is False and r1.meta["url"].startswith("https://") and r1.ts.tzinfo is not None
    assert abs(r1.ts.timestamp() - (BASE + 60)) < 1


# --- scan: first run, high water, overlap, paging -----------------------------------------------------------------------


def test_first_run_is_bounded_and_sets_the_high_water_mark(adapter, net, me):
    net.handler = srv = Server(own(1, 40))
    loc = loc_for(adapter, {**CFG, "initial_messages": 7})
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 7 and cur["high_water"] == mts(40) and "scan" not in cur
    assert cur["identity"] == {"team_id": TEAM, "user_id": UID}
    assert srv.calls == ["auth.test", "search.messages"]
    units, cur2 = run(adapter, loc, cur, me)
    # nothing new: the 33 older messages are inside the 24 h overlap, but the bound is not widened by it
    assert units == [] and cur2["floor"] == cur["floor"]
    srv.messages.append(msg(41, "a new message after the bounded first run"))
    units, _ = run(adapter, loc, cur2, me)
    assert [u.text for u in units] == ["a new message after the bounded first run"]


def test_incremental_read_only_emits_new_messages(adapter, net, me):
    srv, loc, cur, units = synced(adapter, net, me, own(1, 5))
    assert len(units) == 5 and cur["high_water"] == mts(5)
    srv.messages += [msg(6, "a brand new message after the first sync"), msg(7, "and one more new message, thanks")]
    units, cur2 = run(adapter, loc, cur, me)
    assert texts(units) == ["a brand new message after the first sync", "and one more new message, thanks"]
    assert cur2["high_water"] == mts(7)
    units, _ = run(adapter, loc, cur2, me)
    assert units == []


def test_overlap_rereads_without_duplicates_and_catches_a_late_indexed_message(adapter, net, me):
    srv, loc, cur, units = synced(adapter, net, me, own(1, 5))
    # message 3b was written between 3 and 4 but reached the search index only now: below the high-water mark
    late = msg(3, "a message the search index delivered late, words", ts=f"{BASE + 3 * 60 + 30}.000999")
    srv.messages.append(late)
    units, cur2 = run(adapter, loc, cur, me)
    assert [u.text for u in units] == ["a message the search index delivered late, words"]
    # the five already stored were read again (they sit inside the overlap) but were not emitted again
    assert len(srv.searches) >= 2 and cur2["high_water"] == mts(5)
    units, _ = run(adapter, loc, cur2, me)
    assert units == []


def test_scan_stops_once_matches_are_older_than_the_overlap(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "PAGE_SIZE", 2)
    old = [msg(i, f"old message {i} of mine, with words", ts=f"{BASE - 5 * 86400 + i}.000001") for i in range(1, 4)]
    srv, loc, cur, _ = synced(adapter, net, me, [*old, *own(1, 3)])
    srv.searches.clear()
    run(adapter, loc, cur, me)
    # page 1: messages 3 and 2; page 2: message 1 and the newest old one, which is 5 days below the mark: stop there
    assert [q["page"] for q in srv.searches] == ["1", "2"]


def test_interrupted_scan_resumes_at_the_stored_page_without_loss_or_duplicates(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "PAGE_SIZE", 3)
    net.handler = srv = Server(own(1, 12))
    loc = loc_for(adapter, {**CFG, "initial_messages": 100})
    srv.inject["search.messages"] = [None, None, TimeoutError("slow")]
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 6 and cur["scan"]["token"] == "3" and "high_water" not in cur and "slow" not in str(cur)
    assert cur["status"]["error"]
    units2, cur2 = run(adapter, loc, {k: v for k, v in cur.items() if k != "status"}, me)
    assert len(units2) == 6 and "scan" not in cur2 and cur2["high_water"] == mts(12)
    assert {u.source_ref for u in units}.isdisjoint({u.source_ref for u in units2})


def test_read_stops_cleanly_at_the_search_page_limit(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "PAGE_SIZE", 3)
    monkeypatch.setattr(slk, "MAX_SEARCH_PAGE", 2)
    net.handler = srv = Server(own(1, 20))
    units, cur = run(adapter, loc_for(adapter, {**CFG, "initial_messages": 100}), None, me)
    assert len(units) == 6 and "status" not in cur and cur["high_water"] == mts(20) and "scan" not in cur
    assert [q["page"] for q in srv.searches] == ["1", "2"]


def test_a_read_makes_at_most_max_pages_requests(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "PAGE_SIZE", 2)
    net.handler = srv = Server(own(1, 40))
    units, cur = run(adapter, loc_for(adapter, {**CFG, "initial_messages": 100}), None, me)
    assert len(srv.searches) == 5 and cur["scan"]["token"] == "6"


# --- identity, tokens, errors, rate limits ------------------------------------------------------------------------------


@pytest.mark.parametrize("who", [{"uid": "U0ZZZ0002"}, {"team": "T0OTHER02"}])
def test_identity_binding_mismatch_stops_the_source_and_leaves_the_cursor(adapter, net, me, who):
    srv, loc, cur, _ = synced(adapter, net, me)
    other = Server(own(1, 9), uid=who.get("uid", UID), team=who.get("team", TEAM))
    net.handler = other
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and "different user or workspace" in cur2["status"]["error"]
    assert cur2["identity"] == cur["identity"] and cur2["high_water"] == cur["high_water"] and cur2["seen"] == cur["seen"]
    assert other.calls == ["auth.test"]  # the other account was never searched
    net.handler = srv  # switching back resumes
    assert run(adapter, loc, {k: v for k, v in cur2.items() if k != "status"}, me)[1].get("status") is None


def test_identity_is_rechecked_even_in_the_middle_of_a_scan(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "PAGE_SIZE", 3)
    net.handler = srv = Server(own(1, 12))
    loc = loc_for(adapter, {**CFG, "initial_messages": 100})
    srv.inject["search.messages"] = [None, TimeoutError("x")]
    _, cur = run(adapter, loc, None, me)
    assert cur["scan"]
    net.handler = other = Server(own(1, 12), uid="U0ZZZ0002")
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and other.calls == ["auth.test"] and cur2["scan"] == cur["scan"]


@pytest.mark.parametrize("code", ["invalid_auth", "token_revoked", "not_authed", "account_inactive", "token_expired"])
def test_rejected_token_ingests_nothing_keeps_the_marks_and_leaks_nothing(adapter, net, me, code):
    srv, loc, cur, _ = synced(adapter, net, me)
    stuck = {**cur, "scan": {"token": "2", "candidate": cur["high_water"], "fetched": 3}}
    srv.inject["auth.test"] = [{"ok": False, "error": code}]
    units, cur2 = run(adapter, loc, stuck, me)
    assert units == [] and code in cur2["status"]["error"] and "retry_after" not in cur2["status"]
    assert cur2["high_water"] == cur["high_water"] and cur2["scan"] == stuck["scan"] and cur2["seen"] == cur["seen"]
    assert TOKEN not in json.dumps(cur2) and TOKEN not in cur2["status"]["error"]
    # the user fixes the token: the very next read works, no waiting
    units, cur3 = run(adapter, loc, cur2, me)
    assert "status" not in cur3


def test_token_rejected_by_search_is_reported_without_the_token(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.messages.append(msg(6, "a newer message that must wait for a good token"))
    srv.inject["search.messages"] = [{"ok": False, "error": "token_revoked"}]
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and "token_revoked" in cur2["status"]["error"] and cur2["high_water"] == cur["high_water"]
    assert TOKEN not in json.dumps(cur2)


def test_a_missing_scope_is_reported(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.inject["search.messages"] = [{"ok": False, "error": "missing_scope", "needed": "search:read", "provided": "x"}]
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and "search:read" in cur2["status"]["error"]


def test_no_token_at_read_time_makes_no_request(adapter, net, me, monkeypatch):
    loc = loc_for(adapter)
    monkeypatch.delenv(slk.TOKEN_ENV)
    units, cur = run(adapter, loc, None, me)
    assert units == [] and slk.TOKEN_ENV in cur["status"]["error"] and net.calls == []


def test_http_429_stores_retry_after_and_the_next_read_makes_no_request(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "PAGE_SIZE", 3)
    net.handler = srv = Server(own(1, 12))
    loc = loc_for(adapter, {**CFG, "initial_messages": 100})
    srv.inject["search.messages"] = [None, http_error("https://slack.com/api/search.messages", 429, {"Retry-After": "120"})]
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 3 and cur["scan"]["token"] == "2" and "rate limited" in cur["status"]["error"]
    wait = (datetime.fromisoformat(cur["status"]["retry_after"]) - datetime.now(UTC)).total_seconds()
    assert 100 < wait <= 120
    n = len(net.calls)
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and cur2 == cur and len(net.calls) == n  # backing off: not a single request
    past = {**cur, "status": {**cur["status"], "retry_after": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}}
    units, cur3 = run(adapter, loc, past, me)
    assert len(units) == 9 and cur3["high_water"] == mts(12)  # resumed at page 2: nothing lost, nothing repeated


def test_ok_false_ratelimited_is_a_backoff_too(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.inject["auth.test"] = [{"ok": False, "error": "ratelimited"}]
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and cur2["status"]["retry_after"] and cur2["high_water"] == cur["high_water"]
    n = len(net.calls)
    run(adapter, loc, cur2, me)
    assert len(net.calls) == n


def test_network_error_leaves_the_cursor_intact(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.inject["auth.test"] = [TimeoutError("slow")]
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and "timed out" in cur2["status"]["error"]
    assert {k: v for k, v in cur2.items() if k != "status"} == cur


@pytest.mark.parametrize("failure", ["http400", "ok_false"])
def test_rejected_page_drops_the_scan_and_keeps_the_high_water_mark(adapter, net, me, failure):
    srv, loc, cur, _ = synced(adapter, net, me)
    stuck = {**cur, "scan": {"token": "7", "candidate": cur["high_water"], "fetched": 3}}
    srv.inject["search.messages"] = [
        http_error("https://slack.com/api/search.messages", 400) if failure == "http400"
        else {"ok": False, "error": "invalid_page"}]
    units, cur2 = run(adapter, loc, stuck, me)
    assert "scan" not in cur2 and cur2["high_water"] == cur["high_water"] and cur2["status"]["error"]
    units, cur3 = run(adapter, loc, cur2, me)
    assert units == [] and "status" not in cur3 and "scan" not in cur3


def test_the_token_never_reaches_a_unit_cursor_url_or_exception(adapter, net, me):
    srv, loc, cur, units = synced(adapter, net, me)
    srv.inject["search.messages"] = [RuntimeError(f"boom {TOKEN}")]  # even an unexpected error text stays out
    srv.messages.append(msg(6, "a newer message that never arrives here"))
    units2, cur2 = run(adapter, loc, cur, me)
    blob = json.dumps([[u.__dict__ for u in units], cur, cur2], default=str)
    assert TOKEN not in blob and "xoxp" not in blob
    assert cur2["status"]["error"] == "RuntimeError"


def test_redirects_to_another_host_never_get_the_token(adapter, net, me):
    net.handler = lambda u, q, h: (_ for _ in ()).throw(
        http_error(u.geturl(), 302, {"Location": "https://evil.example.com/api/auth.test"}))
    units, cur = run(adapter, loc_for(adapter), None, me)
    assert units == [] and "another origin" in cur["status"]["error"] and len(net.calls) == 1


# --- conversations: channels, DMs, filters -------------------------------------------------------------------------------


def convo_rows():
    return [msg(1, "in the general channel, written by me", channel=GENERAL),
            msg(2, "in a private finance channel, from me", channel=PRIVATE),
            msg(3, "in a direct message to a colleague, from me", channel=DM),
            msg(4, "in a group direct message, from me here", channel=GROUP_DM)]


def kept(adapter, net, me, config):
    net.calls.clear()
    units, _ = run(adapter, loc_for(adapter, config), None, me)
    return texts(units), units


def test_dms_are_off_by_default_and_channels_are_kept(adapter, net, me):
    net.handler = Server(convo_rows())
    got, units = kept(adapter, net, me, CFG)
    assert got == ["in a private finance channel, from me", "in the general channel, written by me"]
    assert {u.meta["conversation"] for u in units} == {"channel"} and {u.register for u in units} == {"work"}


def test_include_dms_adds_direct_and_group_messages(adapter, net, me):
    net.handler = Server(convo_rows())
    got, units = kept(adapter, net, me, {**CFG, "include_dms": True})
    assert len(got) == 4
    by = {u.meta["conversation"]: u for u in units if u.meta["conversation"] != "channel"}
    assert set(by) == {"dm", "group_dm"}
    assert (by["dm"].register, by["dm"].audience) == ("work", "colleague")
    assert by["group_dm"].audience == "group" and by["dm"].audience_ref and by["dm"].audience_ref != by["group_dm"].audience_ref
    assert "D0DM00001" not in by["dm"].audience_ref and "D0DM00001" not in by["dm"].thread_id  # pseudonymous
    got, _ = kept(adapter, net, me, {**CFG, "include_dms": "yes"})  # only a real boolean counts
    assert len(got) == 2


def test_dm_register_and_register_options(adapter, net, me):
    net.handler = Server(convo_rows())
    _, units = kept(adapter, net, me, {**CFG, "include_dms": True, "dm_register": "close"})
    regs = {u.meta["conversation"]: (u.register, u.audience) for u in units}
    assert regs["channel"] == ("work", "group") and regs["dm"] == ("close", "close")
    _, units = kept(adapter, net, me, {**CFG, "include_dms": True, "register": "group"})
    assert {u.register for u in units} == {"group"}
    _, units = kept(adapter, net, me, {**CFG, "register": "public", "include_dms": True, "dm_register": 3})
    assert {u.register for u in units} == {"work"}  # not a Slack register: the default stays


@pytest.mark.parametrize("config,expected", [
    ({"include_channels": ["general"]}, ["in the general channel, written by me"]),
    ({"include_channels": ["#General"]}, ["in the general channel, written by me"]),
    ({"include_channels": ["G0PRIV001"]}, ["in a private finance channel, from me"]),
    ({"include_channels": "general, finance-leads"}, None),
    ({"exclude_channels": ["finance-leads"]}, ["in the general channel, written by me"]),
    ({"exclude_channels": ["#C0GEN0001"]}, ["in a private finance channel, from me"]),
    ({"include_channels": ["general"], "exclude_channels": ["general"]}, []),
    ({"include_channels": ["nonexistent"]}, []),
])
def test_channel_include_and_exclude_by_name_or_id(adapter, net, me, config, expected):
    net.handler = Server(convo_rows())
    got, _ = kept(adapter, net, me, {**CFG, **config})
    assert got == (expected if expected is not None else sorted(
        ["in the general channel, written by me", "in a private finance channel, from me"]))


def test_include_list_narrows_channels_but_dms_follow_include_dms(adapter, net, me):
    net.handler = Server(convo_rows())
    got, _ = kept(adapter, net, me, {**CFG, "include_channels": ["general"], "include_dms": True})
    assert len(got) == 3 and "in a private finance channel, from me" not in got
    got, _ = kept(adapter, net, me, {**CFG, "include_dms": True, "exclude_channels": ["D0DM00001", "g0mpim001"]})
    assert got == ["in a private finance channel, from me", "in the general channel, written by me"]


# --- edits and deletes ----------------------------------------------------------------------------------------------------


def test_a_fresh_reconcile_stamp_means_no_reconcile_requests(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.drop(3)
    srv.calls.clear()
    units, cur2 = run(adapter, loc, cur, me)
    assert units == [] and "_delete_refs" not in cur2 and "chat.getPermalink" not in srv.calls
    assert f"{TEAM}:C0GEN0001:{mts(3)}" in cur2["seen"]


def test_an_edit_inside_the_overlap_window_is_picked_up_at_once(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    next(m for m in srv.messages if m["ts"] == mts(4))["text"] = "message number 4, edited a minute later"
    units, _ = run(adapter, loc, cur, me)
    assert [(u.source_ref, u.text) for u in units] == [(f"{TEAM}:C0GEN0001:{mts(4)}", "message number 4, edited a minute later")]


def test_reconcile_upserts_an_edited_message_under_the_same_ref(adapter, net, me):
    srv, loc, cur, first = synced(adapter, net, me)
    target = next(m for m in srv.messages if m["ts"] == mts(2))
    target["text"] = "message number 2, edited to say something better"
    units, cur2 = run(adapter, loc, age(cur), me)
    assert [(u.source_ref, u.text) for u in units] == [(f"{TEAM}:C0GEN0001:{mts(2)}", "message number 2, edited to say something better")]
    assert "_delete_refs" not in cur2 and "chat.getPermalink" not in srv.calls
    units, _ = run(adapter, loc, age(cur2), me)  # the new fingerprint is stored: not emitted again
    assert units == []


def test_reconcile_does_not_add_a_message_that_was_never_stored(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me, own(1, 5))
    # older than the overlap, so the scan never saw it: the first-run bound is not widened by a reconcile
    srv.messages.append(msg(0, "an old message that was never stored, words", ts=f"{BASE - 5 * 86400}.000777"))
    units, cur2 = run(adapter, loc, age(cur), me)
    assert units == [] and "_delete_refs" not in cur2 and all(not r.endswith(".000777") for r in cur2["seen"])


def test_reconcile_reports_a_message_that_is_gone_from_search_and_confirmed_deleted(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.drop(3)
    units, cur2 = run(adapter, loc, age(cur), me)
    ref = f"{TEAM}:C0GEN0001:{mts(3)}"
    assert units == [] and cur2["_delete_refs"] == [ref] and ref not in cur2["seen"] and len(cur2["seen"]) == 4
    assert srv.calls.count("chat.getPermalink") == 1
    _, cur3 = run(adapter, loc, age({k: v for k, v in cur2.items() if k != "_delete_refs"}), me)
    assert "_delete_refs" not in cur3  # reported once


@pytest.mark.parametrize("answer", ["channel_not_found", "not_in_channel", "access_denied", "internal_error", 500, 403])
def test_reconcile_leaves_a_message_alone_when_the_check_is_inconclusive(adapter, net, me, answer):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.drop(3)
    srv.permalink_error[mts(3)] = answer
    units, cur2 = run(adapter, loc, age(cur), me)
    assert units == [] and "_delete_refs" not in cur2 and f"{TEAM}:C0GEN0001:{mts(3)}" in cur2["seen"]


def test_a_message_missing_from_search_but_still_there_is_not_a_delete(adapter, net, me):
    """E.g. the user left the channel: search stops returning it, but the message was not deleted."""
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.exists = {m["ts"] for m in srv.messages}  # permalink still answers for every message
    srv.drop(3)
    srv.exists.add(mts(3))
    _, cur2 = run(adapter, loc, age(cur), me)
    assert "_delete_refs" not in cur2 and f"{TEAM}:C0GEN0001:{mts(3)}" in cur2["seen"]


def test_rate_limited_confirmation_keeps_the_scan_results_and_honours_retry_after(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.drop(3)
    srv.messages.append(msg(6, "a brand new message found by the scan, words"))
    srv.inject["chat.getPermalink"] = [http_error("https://slack.com/api/chat.getPermalink", 429, {"Retry-After": "30"})]
    old = age(cur)
    units, cur2 = run(adapter, loc, old, me)
    assert [u.text for u in units] == ["a brand new message found by the scan, words"]  # the scan's results are kept
    assert "_delete_refs" not in cur2 and cur2["reconciled_at"] == old["reconciled_at"]
    wait = (datetime.fromisoformat(cur2["status"]["retry_after"]) - datetime.now(UTC)).total_seconds()
    assert 20 < wait <= 30 and "reconcile" in cur2["status"]["error"]
    n = len(net.calls)
    assert run(adapter, loc, cur2, me)[0] == [] and len(net.calls) == n  # backing off
    past = {**cur2, "status": {**cur2["status"], "retry_after": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()}}
    _, cur3 = run(adapter, loc, past, me)
    assert len(cur3["_delete_refs"]) == 1 and "status" not in cur3


def test_rate_limited_reconcile_listing_honours_retry_after(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.inject["search.messages"] = [None, http_error("https://slack.com/api/search.messages", 429, {"Retry-After": "45"})]
    _, cur2 = run(adapter, loc, age(cur), me)
    assert 30 < (datetime.fromisoformat(cur2["status"]["retry_after"]) - datetime.now(UTC)).total_seconds() <= 45
    assert cur2["high_water"] == cur["high_water"] and cur2["seen"] == cur["seen"]


def test_failed_reconcile_listing_is_skipped_and_retried(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    srv.drop(3)
    srv.inject["search.messages"] = [None, TimeoutError("slow")]  # the scan's page is fine, the reconcile's is not
    old = age(cur)
    _, cur2 = run(adapter, loc, old, me)
    assert "_delete_refs" not in cur2 and "status" not in cur2 and cur2["reconciled_at"] == old["reconciled_at"]
    _, cur3 = run(adapter, loc, cur2, me)
    assert len(cur3["_delete_refs"]) == 1


def test_a_channel_filter_change_is_not_a_delete(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me, convo_rows()[:2])
    loc2 = loc_for(adapter, {**CFG, "exclude_channels": ["general"]})
    _, cur2 = run(adapter, loc2, age(cur), me)
    assert "_delete_refs" not in cur2 and "chat.getPermalink" not in srv.calls


def test_reconcile_window_is_bounded(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "RECONCILE_ITEMS", 3)
    srv, loc, cur, _ = synced(adapter, net, me, own(1, 5))
    srv.messages = [m for m in srv.messages if m["ts"] in {mts(3), mts(4), mts(5)}]  # 1 and 2 are gone ...
    srv.messages += [msg(0, f"older message {i}, never stored", ts=f"{BASE - 5 * 86400 + i}.000001") for i in range(3)]
    _, cur2 = run(adapter, loc, age(cur), me)  # ... but they are outside the 3-message window: nobody asks about them
    assert "_delete_refs" not in cur2 and "chat.getPermalink" not in srv.calls


def test_reconcile_confirmations_are_capped(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "RECONCILE_CHECKS", 2)
    srv, loc, cur, _ = synced(adapter, net, me, own(1, 8))
    srv.messages = [m for m in srv.messages if m["ts"] in {mts(1), mts(8)}]
    _, cur2 = run(adapter, loc, age(cur), me)
    assert srv.calls.count("chat.getPermalink") == 2 and len(cur2["_delete_refs"]) == 2


# --- edited down to nothing, resumed scans, unknown conversation shapes ----------------------------------------------------


def test_a_message_edited_down_to_nothing_inside_the_overlap_is_reported_deleted(adapter, net, me):
    srv, loc, cur, _ = synced(adapter, net, me)
    next(m for m in srv.messages if m["ts"] == mts(2))["text"] = "<https://example.org/only-a-link>"
    units, cur2 = run(adapter, loc, cur, me)
    ref = f"{TEAM}:C0GEN0001:{mts(2)}"
    assert units == [] and cur2["_delete_refs"] == [ref] and ref not in cur2["seen"]


def test_a_message_edited_down_to_nothing_is_found_by_the_reconcile(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "OVERLAP_S", 0)  # so only the reconcile can see it
    srv, loc, cur, _ = synced(adapter, net, me)
    next(m for m in srv.messages if m["ts"] == mts(2))["text"] = "&gt; only a quote"
    units, cur2 = run(adapter, loc, age(cur), me)
    assert units == [] and cur2["_delete_refs"] == [f"{TEAM}:C0GEN0001:{mts(2)}"]
    assert "chat.getPermalink" not in srv.calls  # the listing already shows it is empty: no confirmation needed


def test_resumed_scan_does_not_lose_a_message_when_results_shift_up(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "PAGE_SIZE", 3)
    net.handler = srv = Server(own(1, 12))  # newest first: page 1 = 12,11,10; page 2 = 9,8,7; page 3 = 6,5,4
    loc = loc_for(adapter, {**CFG, "initial_messages": 100})
    srv.inject["search.messages"] = [None, None, TimeoutError("slow")]
    units, cur = run(adapter, loc, None, me)
    assert len(units) == 6 and cur["scan"]["token"] == "3"
    srv.drop(11)  # a deletion above the stored page: message 6 now moves from page 3 to the end of page 2
    units2, cur2 = run(adapter, loc, {k: v for k, v in cur.items() if k != "status"}, me)
    refs = {u.source_ref.rsplit(":", 1)[-1] for u in [*units, *units2]}
    assert refs >= {mts(i) for i in range(1, 13) if i != 11}  # every surviving message was stored
    assert {u.text for u in units2} >= {"message number 6 with a handful of words in it"}
    assert len({u.source_ref for u in units2}) == len(units2)  # the re-read page is not stored twice
    assert not {u.source_ref for u in units} & {u.source_ref for u in units2}
    assert "scan" not in cur2 and cur2["high_water"] == mts(12)


def test_repeated_failures_before_any_page_do_not_walk_the_resume_point_back(adapter, net, me, monkeypatch):
    monkeypatch.setattr(slk, "PAGE_SIZE", 3)
    net.handler = srv = Server(own(1, 12))
    loc = loc_for(adapter, {**CFG, "initial_messages": 100})
    srv.inject["search.messages"] = [None, None, TimeoutError("slow")]
    _, cur = run(adapter, loc, None, me)
    stuck = cur
    for _ in range(3):
        srv.inject["search.messages"] = [TimeoutError("slow")]
        _, stuck = run(adapter, loc, {k: v for k, v in stuck.items() if k != "status"}, me)
        assert stuck["scan"] == cur["scan"]


def test_an_unknown_conversation_shape_is_treated_as_a_dm(adapter, net, me):
    odd = {"id": "X0UNKNOWN1", "name": "mystery"}  # no flags, no C id: not positively a channel
    bare = {"id": "C0BARE0001"}  # a C id alone is a channel
    net.handler = Server([msg(1, "in a channel of unknown shape, words here", channel=odd),
                          msg(2, "in a channel known only by its id, words", channel=bare)])
    got, units = kept(adapter, net, me, CFG)
    assert got == ["in a channel known only by its id, words"]
    got, units = kept(adapter, net, me, {**CFG, "include_dms": True})
    assert len(got) == 2 and {u.meta["conversation"] for u in units} == {"dm", "channel"}
    got, _ = kept(adapter, net, me, {**CFG, "include_dms": True, "exclude_channels": ["X0UNKNOWN1"]})
    assert len(got) == 1


# --- sources check -------------------------------------------------------------------------------------------------------


def test_check_validates_the_token_with_auth_test_only(adapter, net):
    net.handler = srv = Server([])
    good, message = adapter.check(CFG)
    assert good and UID in message and TEAM in message and "no message was read" in message and TOKEN not in message
    assert srv.calls == ["auth.test"]


def test_check_warns_when_search_read_is_missing(adapter, net):
    net.handler = Server([], scopes="identify")
    good, message = adapter.check(CFG)
    assert not good and "search:read" in message and TOKEN not in message
    net.handler = Server([], scopes="identify, search:read")
    assert adapter.check(CFG)[0] is True
    # no scopes header at all: the token is accepted and nothing is claimed about scopes
    net.handler = lambda u, q, h: ok({"ok": True, "team_id": TEAM, "user_id": UID})
    assert adapter.check(CFG)[0] is True


def test_check_reports_a_rejected_token_and_a_missing_setup(adapter, net, monkeypatch):
    net.handler = srv = Server([])
    srv.inject["auth.test"] = [{"ok": False, "error": "invalid_auth"}]
    good, message = adapter.check(CFG)
    assert not good and "invalid_auth" in message and TOKEN not in message
    assert adapter.check({})[0] is False and srv.calls == ["auth.test"]  # no consent: no request at all


def test_cli_sources_check_slack(net, capsys, monkeypatch):
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True}}))
    net.handler = srv = Server([])
    assert cli.main(["sources", "check", "slack"]) == 0
    out = capsys.readouterr().out
    assert "token accepted" in out and TOKEN not in out and srv.calls == ["auth.test"]
    assert cli.main(["sources", "check", "slack", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    srv.inject["auth.test"] = [{"ok": False, "error": "token_revoked"}]
    assert cli.main(["sources", "check", "slack"]) == 1
    assert "token_revoked" in capsys.readouterr().out


def test_cli_sources_check_still_checks_files(tmp_path, capsys):
    rec = tmp_path / "feed.jsonl"
    rec.write_text(json.dumps({"schema_version": 1, "op": "upsert", "id": "a", "ts": "2026-10-20T12:00:00Z",
                               "text": "Hello from a script, this is the text of my first record.",
                               "kind": "post", "register": "work"}) + "\n")
    assert cli.main(["sources", "check", str(rec)]) == 0
    assert "1 valid record" in capsys.readouterr().out


def test_cli_enable_shows_the_host_and_the_token_hint(net, capsys, monkeypatch):
    monkeypatch.delenv(slk.TOKEN_ENV)
    assert cli.main(["sources", "enable", "slack"]) == 0
    out = capsys.readouterr().out
    assert "slack.com" in out and slk.TOKEN_ENV in out and "session start" in out


# --- through the real engine ------------------------------------------------------------------------------------------------

DRAFT = ("Hi team, I wanted to follow up on the invoice batch and confirm the plan for Friday. Please let me know if "
         "you have any questions.")
FINAL = "I wanted to follow up on the invoice batch and confirm the plan for Friday."


def recent(minutes_ago):
    return f"{int(time.time()) - minutes_ago * 60}.000100"


def test_a_stored_agent_draft_and_the_edited_slack_message_are_paired_as_an_edit(net):
    from mannerism import capture

    con = store.connect()
    capture.log_draft(DRAFT, "work", "mcp", con=con, now=datetime.now(UTC) - timedelta(minutes=90))
    con.close()
    net.handler = srv = Server([msg(1, FINAL, ts=recent(30)), msg(2, "an unrelated message about lunch plans today",
                                                                  ts=recent(20))])
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True}}))
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["sources"]["slack"]["new_units"] == 2 and rep["sources"]["slack"]["errors"] == []
    assert rep["capture"]["edited"] == 1 and "work" in rep["registers_changed"]
    con = store.connect()
    try:
        row = con.execute("SELECT e.status, e.ratio, u.source, u.register, u.text FROM edits e "
                          "JOIN units u ON u.unit_id = e.unit_id").fetchone()
        assert row["source"] == "slack" and row["status"] == "edited" and 0.5 <= row["ratio"] < 0.9
        assert (row["register"], row["text"]) == ("work", FINAL)
        cur = store.get_cursor(con, "slack", "slack:user")
        assert cur["identity"] == {"team_id": TEAM, "user_id": UID} and cur["high_water"] == srv.messages[-1]["ts"]
        assert TOKEN not in json.dumps(cur)
        meta = json.loads(con.execute("SELECT meta_json FROM units WHERE source='slack' LIMIT 1").fetchone()[0])
        assert meta["public"] is False
    finally:
        con.close()


def test_direct_messages_are_not_stored_through_sync_unless_enabled(net):
    net.handler = Server(convo_rows())
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True}}))
    assert sync.sync(sources=["slack"], force=True)["sources"]["slack"]["new_units"] == 2
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True, "include_dms": True}}))
    con = store.connect()
    try:
        con.execute("UPDATE cursors SET cursor_json='{}' WHERE source='slack'")
        con.commit()
    finally:
        con.close()
    assert sync.sync(sources=["slack"], force=True)["sources"]["slack"]["new_units"] == 2  # the two DMs, new refs


def test_edit_and_delete_reach_the_store_through_sync(net):
    srv = Server([msg(i, f"message number {i} with a handful of words in it", ts=recent(60 - i)) for i in range(1, 6)])
    net.handler = srv
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True}}))
    assert sync.sync(sources=["slack"], force=True)["sources"]["slack"]["new_units"] == 5
    con = store.connect()
    try:
        cur = store.get_cursor(con, "slack", "slack:user")
        con.execute("UPDATE cursors SET cursor_json=? WHERE source='slack'", (json.dumps(
            {**cur, "reconciled_at": (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")}),))
        con.commit()
    finally:
        con.close()
    srv.messages[-1]["text"] = "the newest message, now edited by me"
    gone = srv.messages.pop(1)["ts"]
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["sources"]["slack"]["deleted_units"] == 1 and rep["sources"]["slack"]["updated_units"] >= 1
    con = store.connect()
    try:
        rows = {r[0]: r[1] for r in con.execute("SELECT source_ref, text FROM units WHERE source='slack'")}
        assert len(rows) == 4 and "the newest message, now edited by me" in rows.values()
        assert f"{TEAM}:C0GEN0001:{gone}" not in rows
    finally:
        con.close()


def test_a_rejected_token_through_sync_is_a_soft_failure_with_backoff(net):
    srv = Server(own())
    srv.inject["auth.test"] = [{"ok": False, "error": "invalid_auth"}]
    net.handler = srv
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True}}))
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["sources"]["slack"]["status"] == "error" and "invalid_auth" in rep["sources"]["slack"]["errors"][0]
    assert TOKEN not in json.dumps(rep)
    con = store.connect()
    try:
        from mannerism import schedule
        state = schedule.get(con, "slack")
        assert state["failures"] == 1 and state["next_due"] and TOKEN not in json.dumps(state)
    finally:
        con.close()


def test_without_the_token_a_sync_makes_no_request(net, monkeypatch):
    monkeypatch.delenv(slk.TOKEN_ENV)
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True}}))
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["sources"]["slack"]["locations"] == 0 and net.calls == []


def test_without_allow_network_nothing_is_read_even_if_enabled(net):
    save_config(Config(sources={"slack": {"enabled": True}}))
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["sources"]["slack"]["status"] == "skipped_no_consent" and net.calls == []


def test_a_message_edited_after_it_was_paired_is_paired_again_and_keeps_its_draft(net):
    """Post an agent draft unchanged (accepted), then edit the message in Slack: the new text is paired with the
    SAME draft as an edit, and the draft is not deleted."""
    from mannerism import capture

    con = store.connect()
    capture.log_draft(DRAFT, "work", "mcp", con=con, now=datetime.now(UTC) - timedelta(minutes=90))
    con.close()
    srv = Server([msg(1, DRAFT, ts=recent(30))])
    net.handler = srv
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True}}))
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["capture"]["accepted"] == 1
    srv.messages[0]["text"] = FINAL  # the user edits the sent message
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["sources"]["slack"]["updated_units"] == 1 and rep["capture"]["edited"] == 1
    con = store.connect()
    try:
        rows = con.execute("SELECT status, final, draft FROM edits").fetchall()
        assert [(r["status"], r["final"]) for r in rows] == [("edited", FINAL)] and rows[0]["draft"] == DRAFT
        assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
    finally:
        con.close()


# --- rules, capture stats and kept drafts around an edit -----------------------------------------------------------------


def _slack_posted_unchanged(net, avoid="follow up", support=2):
    """A rule (support 2) and a draft; the draft is posted unchanged and synced (capture: accepted)."""
    from mannerism import capture

    con = store.connect()
    capture.log_draft(DRAFT, "work", "mcp", con=con, now=datetime.now(UTC) - timedelta(minutes=90))
    con.execute("INSERT INTO rules(register, text, support, contradicted, created_at, last_seen, edit_ids_json, avoid) "
                "VALUES ('work', 'never says follow up', ?, 0, ?, ?, '[]', ?)", (support, store.now_iso(), store.now_iso(), avoid))
    con.close()
    srv = Server([msg(1, DRAFT, ts=recent(30))])
    net.handler = srv
    save_config(Config(sources={"slack": {"enabled": True, "allow_network": True}}))
    assert sync.sync(sources=["slack"], force=True)["capture"]["accepted"] == 1
    return srv


def _rule():
    con = store.connect()
    try:
        return dict(con.execute("SELECT contradicted, support FROM rules").fetchone())
    finally:
        con.close()


def test_a_posted_then_edited_message_counts_once_against_a_rule(net):
    srv = _slack_posted_unchanged(net)
    assert _rule() == {"contradicted": 1, "support": 2}
    srv.messages[0]["text"] = FINAL  # still contains "follow up"
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["capture"]["edited"] == 1
    assert _rule() == {"contradicted": 1, "support": 2}  # not (2, 2): the rule stays visible
    srv.messages[0]["text"] = FINAL + " Thanks!"  # edited again
    sync.sync(sources=["slack"], force=True)
    assert _rule()["contradicted"] == 1


def test_forgetting_the_source_gives_back_what_its_edits_counted_against_rules(net):
    _slack_posted_unchanged(net)
    assert _rule()["contradicted"] == 1
    con = store.connect()
    try:
        store.forget_rows(con, "slack")
        assert dict(con.execute("SELECT contradicted FROM rules").fetchone()) == {"contradicted": 0}
        assert con.execute("SELECT count(*) FROM edit_contradictions").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
    finally:
        con.close()


def test_a_deleted_message_gives_back_what_its_edit_counted_against_rules(net):
    srv = _slack_posted_unchanged(net)
    con = store.connect()
    try:
        cur = store.get_cursor(con, "slack", "slack:user")
        con.execute("UPDATE cursors SET cursor_json=? WHERE source='slack'", (json.dumps(
            {**cur, "reconciled_at": (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")}),))
        con.commit()
    finally:
        con.close()
    srv.messages.clear()
    assert sync.sync(sources=["slack"], force=True)["sources"]["slack"]["deleted_units"] == 1
    assert _rule()["contradicted"] == 0


def test_undoing_contradictions_never_goes_below_zero(home):
    con = store.connect()
    con.execute("INSERT INTO rules(register, text, created_at, last_seen, avoid) VALUES ('work','r','x','x','a b')")
    con.execute("INSERT INTO edit_contradictions VALUES (7, 1)")
    store.undo_contradictions(con, [7, 7])
    assert con.execute("SELECT contradicted FROM rules").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM edit_contradictions").fetchone()[0] == 0


def test_capture_stats_count_an_edited_message_once(net):
    srv = _slack_posted_unchanged(net)
    srv.messages[0]["text"] = FINAL
    sync.sync(sources=["slack"], force=True)
    con = store.connect()
    try:
        stats = store.get_meta_json(con, "capture_stats")["slack"]
        assert stats == {"matched": 1, "ambiguous": 0, "unmatched": 0}
    finally:
        con.close()


def test_a_kept_draft_is_dropped_when_the_edited_text_pairs_with_nothing(net):
    srv = _slack_posted_unchanged(net)
    srv.messages[0]["text"] = "Completely different words now, about lunch plans and nothing else at all."
    rep = sync.sync(sources=["slack"], force=True)
    assert rep["sources"]["slack"]["updated_units"] == 1
    con = store.connect()
    try:
        assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0  # nothing links it to a unit any more
    finally:
        con.close()


def test_a_re_paired_draft_goes_with_the_source_when_it_is_forgotten(net):
    srv = _slack_posted_unchanged(net)
    srv.messages[0]["text"] = FINAL
    sync.sync(sources=["slack"], force=True)
    con = store.connect()
    try:
        assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
        store.forget_rows(con, "slack")
        assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM units WHERE source='slack'").fetchone()[0] == 0
    finally:
        con.close()


def test_a_legacy_private_channel_is_a_channel_not_a_dm(adapter, net, me):
    legacy = {"id": "X0LEGACY1", "name": "old-private", "is_private": True, "is_mpim": False, "is_im": False}
    net.handler = Server([msg(1, "in an old private channel, words here", channel=legacy)])
    got, units = kept(adapter, net, me, CFG)
    assert got == ["in an old private channel, words here"] and units[0].meta["conversation"] == "channel"
    # only when both keys are present: a bare is_private is not enough to call it a channel
    odd = {"id": "X0ODD00001", "is_private": True}
    net.handler = Server([msg(2, "in a conversation of unknown shape, words", channel=odd)])
    assert kept(adapter, net, me, CFG)[0] == []
