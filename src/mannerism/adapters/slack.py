"""Slack: the user's OWN messages, read through `search.messages` with a Slack user token (opt-in, read-only).

Opt-in: `[sources.slack]` needs `allow_network = true` and a Slack USER token (`xoxp-...`) in the ENVIRONMENT
(`SLACK_USER_TOKEN`; it is never read from the config and never written to the cursor, a log, an error or a unit).
Without the token, `discover` returns nothing and `status_message` says why. `discover` makes no network call. The
token needs one user scope, `search:read`; `auth.test` needs none. Bot tokens (`xoxb-`) and browser session tokens
(`xoxc-`) are refused. Hosts contacted: `slack.com` only (credentials never leave it: `_http` refuses cross-origin
redirects).

Identity. `auth.test` returns the user id and team id of the token. They are stored in the cursor on the first read and
`auth.test` is called again at the start of EVERY read: a token that now belongs to a different user or workspace stops
the source (nothing is ingested, the cursor is left as it was, so switching back resumes).

What is read. `search.messages` with the query `from:<@UID>`, `sort=timestamp`, `sort_dir=desc`, 100 per page, newest
first. A match is kept only when `user` equals the bound user id, it is a plain message (no `bot_id`; no subtype except
`thread_broadcast` and `file_share`) and some text of the user is left after cleaning, so file-only messages are
dropped. Only the `text` field is ever used: attachments, unfurls and shared (forwarded) messages are never read. In the
text, quoted lines (`>` and `>>>`) and fenced code blocks are removed, `<@U..>` mentions become `@[user]`, `<#C..>`
channel links `#[channel]`, `@here`/`@channel`/`@everyone` `@[channel]`, user groups `@[group]`, links keep their label
(a URL-only link vanishes), the leading mentions of a thread reply are dropped, and `&amp; &lt; &gt;` are unescaped.
Other people's messages are never stored and never used as context.

Where. A match is a channel only when it is positively one (`is_channel`/`is_group`, or a `C` id); anything else is
treated as a DM, so an unknown shape fails closed. Public and private channels are kept (register `work`, audience `group`). Direct messages and group DMs are kept
only with `include_dms = true` (default FALSE: private conversations are the most personal text there is; the source is
useful without them). `include_channels` / `exclude_channels` (lists of names or ids, `#` optional, case-insensitive)
narrow the channels; DMs are governed by `include_dms` alone, but an exclude entry that is a DM's id still removes it.
`register` (default `work`; also `formal`, `close`, `group`) sets the register of every message; `dm_register` (default:
the same) sets it for DMs, because a draft is paired with a message only inside one register (`capture.py`).

Ordering. Search sorts by message timestamp, but its index can lag: a message can show up in search after newer ones
did. So a scan does not stop at the first match older than the high-water mark: it reads on until matches are
OVERLAP_S (24 h) older than the mark. The API is free, so the overlap costs only requests. Matches already stored with
the same text (the fingerprint in `seen`) are not emitted again; a changed text is (an edit, see below).

Edits and deletes. A message edited or deleted later sits below the high-water mark (an edit inside the overlap is
seen at once; a message edited down to empty text counts as deleted). At most once per
`reconcile_interval_s` (default 7 days) a completed scan is followed by a reconcile: the latest RECONCILE_ITEMS of the
user's messages are re-read; a stored message whose cleaned text differs from its fingerprint is emitted again under
the same ref, which updates the stored text; a remembered ref that falls inside the window but is missing from it is
confirmed with `chat.getPermalink` (at most RECONCILE_CHECKS) and only an answer `message_not_found` reports it in
`cursor["_delete_refs"]`. Search can also miss a message because the user left its channel, so absence alone never
deletes; any other answer (rate limit, no access, error) changes nothing, and a failed or rate-limited reconcile is
skipped and retried on the next read.

Rate limits. A 429 with Retry-After becomes `status.retry_after` in the cursor (also when it stops only the reconcile, which keeps
the scan's units); while it is in the future no request is made. A read makes at most MAX_PAGES_PER_READ search
requests; an interrupted scan resumes ONE PAGE BEFORE the stored one (pages are numbered, so a deletion above the
stored page would otherwise shift a message out of reach); `seen` stops the re-read page from being stored twice.

First run. It reads at most `initial_messages` (default 200) matches. The ts of the oldest one it reached is kept as
`floor`: a message older than that which was never stored is not ingested later, so the overlap re-read cannot widen
the bound.

Cursor: {"v": 1, "identity": {"team_id", "user_id"}, "high_water": "<message ts>" | absent, "floor": "<ts>" | absent,
"scan": {"token" (the next search page), "candidate", "fetched"} | absent, "seen": {"team:channel:ts": text fingerprint}, "reconciled_at": iso,
"status": {...} | absent}. See `_social.scan`. A page the service rejects (`invalid_page`/`invalid_cursor`) drops `scan`;
the next read starts a new one. Message ref: `team:channel:ts`. Units: kind `message`, `meta.public = False`.
"""

from __future__ import annotations

import html
import os
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from ..core import Location, Me, Unit, hash_ref
from ._http import DEFAULT_RETRY_S, NetError, build_url, get_json
from ._social import (
    DEFAULT_INITIAL_POSTS,
    DEFAULT_RECONCILE_INTERVAL_S,
    MENTION_MARK,
    RECONCILE_CHECKS,
    RECONCILE_ITEMS,
    backoff_active,
    cap_seen,
    clean_cursor,
    finish_text,
    int_option,
    network_enabled,
    reconcile_due,
    scan,
    text_hash,
    utcnow,
    with_status,
)
from ._util import own_unit

HOST = "slack.com"
TOKEN_ENV = "SLACK_USER_TOKEN"
PAGE_SIZE = 100  # the API maximum for `count`; constant, because the continuation token is a page NUMBER
MAX_SEARCH_PAGE = 100  # search.messages serves at most 100 pages
OVERLAP_S = 24 * 3600  # how far below the high-water mark a scan keeps reading (search indexing lag)
KEY = "slack:user"
REGISTER_CHOICES = ("work", "formal", "close", "group")
KEEP_SUBTYPES = (None, "thread_broadcast", "file_share")
LOCATION_LABEL = "Slack (your own messages, user token)"

_USER_TOKEN = re.compile(r"^(?:xoxe\.)?xoxp-[A-Za-z0-9._-]{8,}$")
_ID = re.compile(r"^[A-Z0-9]{1,32}$")
_TS = re.compile(r"^\d{9,11}\.\d{1,8}$")
_ERR = re.compile(r"[^a-z0-9_]")

_AUTH_ERRORS = ("invalid_auth", "not_authed", "token_revoked", "token_expired", "account_inactive", "invalid_token",
                "org_login_required", "no_permission", "two_factor_setup_required", "not_allowed_token_type")
_PAGE_ERRORS = ("invalid_cursor", "invalid_page", "invalid_page_num", "page_out_of_range", "invalid_arguments")
_TRANSIENT = ("internal_error", "fatal_error", "service_unavailable", "request_timeout", "internal_error_retry")


# --- text ----------------------------------------------------------------------------------------------------------

_FENCE = re.compile(r"```.*?(?:```|$)", re.DOTALL)
_BLOCK_QUOTE = re.compile(r"(?:^|\n)[ \t]*(?:&gt;|>){3}.*\Z", re.DOTALL)  # `>>> ` quotes everything after it
_QUOTE_LINE = re.compile(r"^[ \t]*(?:&gt;|>).*$\n?", re.MULTILINE)
_TOKEN = re.compile(r"<([^<>\n]*)>")
_URLISH = re.compile(r"^(?:https?://|www\.|mailto:)\S+$|^[\w-]+(?:\.[\w-]+)+(?:/\S*)?$", re.IGNORECASE)


def _angle(m: re.Match) -> str:
    body = m.group(1)
    target, _, label = body.partition("|")
    if target.startswith("@"):
        return MENTION_MARK
    if target.startswith("#"):
        return "#[channel]"
    if target.startswith("!"):
        name = target[1:].split("^", 1)[0]
        if name in ("here", "channel", "everyone"):
            return "@[channel]"
        if name == "subteam":
            return "@[group]"
        return label if name == "date" else ""
    label = label.strip()
    if label and not _URLISH.match(label) and not label.endswith(("…", "...")):
        return label
    return ""  # a link that only shows (or hides) a URL


def slack_text(raw: str, reply: bool = False) -> str:
    """Slack mrkdwn message text -> the user's own words: no quotes, no code blocks, people and channels neutral."""
    text = _FENCE.sub("", raw)
    text = _BLOCK_QUOTE.sub("", text)
    text = _QUOTE_LINE.sub("", text)
    text = _TOKEN.sub(_angle, text)
    text = html.unescape(text)  # after the tokens: `&lt;` typed by the user is not markup
    return re.sub(r"\n{3,}", "\n\n", finish_text(text, reply))


# --- config ----------------------------------------------------------------------------------------------------------


def _names(config: dict | None, key: str) -> frozenset[str]:
    raw = (config or {}).get(key)
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return frozenset()
    return frozenset(x.strip().lstrip("#").lower() for x in raw if isinstance(x, str) and x.strip().lstrip("#"))


def _register(config: dict | None, key: str, default: str) -> str:
    v = (config or {}).get(key)
    return v if v in REGISTER_CHOICES else default


def _user_token() -> str | None:
    token = os.environ.get(TOKEN_ENV, "").strip()
    return token or None


# --- the API ---------------------------------------------------------------------------------------------------------


def _call(token: str, method: str, params: dict | None = None):
    """GET https://slack.com/api/<method>; (data, headers). Slack answers most failures with HTTP 200 and
    {"ok": false, "error": "<code>"}: they become a NetError that carries the code and nothing else."""
    data, headers = get_json(build_url(HOST, f"/api/{method}", params), {"Authorization": f"Bearer {token}"})
    if not isinstance(data, dict):
        raise NetError("bad_json", f"{HOST}: unexpected {method} shape")
    if data.get("ok") is not True:
        code = _ERR.sub("", str(data.get("error") or "unknown_error").lower())[:64] or "unknown_error"
        if code in _AUTH_ERRORS:
            raise NetError("auth", f"{HOST}: token rejected ({code})")
        if code == "missing_scope":
            raise NetError("scope", f"{HOST}: the token lacks a scope for {method} (search:read is needed)")
        if code in ("ratelimited", "rate_limited"):
            raise NetError("rate_limit", f"{HOST}: rate limited", 429, float(DEFAULT_RETRY_S))
        if code in _PAGE_ERRORS:
            raise NetError("http", f"{HOST}: {method} rejected the page ({code})", 400)
        if code in _TRANSIENT:
            raise NetError("http", f"{HOST}: {method} failed ({code})", 503)
        if code == "message_not_found":
            raise NetError("not_found", f"{HOST}: {code}", 404)
        raise NetError("api", f"{HOST}: {method} failed ({code})")
    return data, headers


def whoami(token: str) -> tuple[dict, str | None]:
    """({"team_id", "user_id"}, X-OAuth-Scopes header or None) from `auth.test`. Reads no message."""
    data, headers = _call(token, "auth.test")
    team, user = data.get("team_id"), data.get("user_id")
    if not (isinstance(team, str) and _ID.match(team) and isinstance(user, str) and _ID.match(user)):
        raise NetError("bad_json", f"{HOST}: auth.test returned no usable ids")
    scopes = headers.get("x-oauth-scopes") if headers is not None else None
    return {"team_id": team, "user_id": user}, (scopes if isinstance(scopes, str) else None)


def _ts_float(ts: str) -> float:
    try:
        return float(ts)
    except (TypeError, ValueError):
        return 0.0


def _ref_ts(ref: str) -> float:
    return _ts_float(ref.rsplit(":", 1)[-1])


class Slack:
    name = "slack"
    tier = "auto"
    network = True
    network_hosts = (HOST,)
    default_interval_s = 3600
    session_start_default = True  # free (search:read, Tier 2) and light: core may run it at session start

    def __init__(self):
        self._config: dict[str, dict] = {}

    def status_message(self, config: dict | None) -> str | None:
        """Why discover finds nothing (for the CLI), or None when the source is ready."""
        if not network_enabled(config):
            return ("slack: set allow_network = true in [sources.slack] to let mannerism read your own Slack "
                    "messages (user scope search:read)")
        token = _user_token()
        if token is None:
            return (f"slack: the {TOKEN_ENV} environment variable is not set; create a Slack app with the user "
                    "scope search:read, install it to your workspace and export its User OAuth Token (xoxp-...)")
        if not _USER_TOKEN.match(token):
            return f"slack: {TOKEN_ENV} must be a Slack user token (it starts with xoxp-)"
        return None

    def discover(self, me: Me, config: dict) -> list[Location]:
        """No network call. Empty unless allow_network is true and SLACK_USER_TOKEN holds a user token."""
        try:
            if self.status_message(config) is not None:
                return []
            self._config[KEY] = dict(config)
            return [Location(self.name, KEY, None, LOCATION_LABEL)]
        except Exception:  # noqa: BLE001
            return []

    def check(self, config: dict | None) -> tuple[bool, str]:
        """`mannerism sources check slack`: validate the token with `auth.test`. Reads no message."""
        msg = self.status_message(config)
        if msg is not None:
            return False, msg
        try:
            ident, scopes = whoami(_user_token() or "")
        except NetError as e:
            return False, f"slack: {e}"
        except Exception as e:  # noqa: BLE001 - the message must hold no credential
            return False, f"slack: {type(e).__name__}"
        who = f"user {ident['user_id']} in workspace {ident['team_id']}"
        if scopes is not None and "search:read" not in {x.strip() for x in scopes.split(",")}:
            return False, (f"slack: token accepted for {who}, but it has no search:read scope, so no message can be "
                           "read; add the user scope search:read to the app and reinstall it (no message was read)")
        return True, f"slack: token accepted for {who} (no message was read)"

    # --- reading ---------------------------------------------------------------------------------------------------

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        orig = cursor if isinstance(cursor, dict) else {}
        try:
            return self._read(loc, orig)
        except Exception as e:  # noqa: BLE001 - a source never raises into the engine; the class name holds no credential
            return [], with_status(clean_cursor(orig), type(e).__name__)

    def _read(self, loc: Location, orig: dict):
        cur = clean_cursor(orig)
        token = _user_token()
        if not token:
            return [], with_status(cur, f"{TOKEN_ENV} is not set")
        if backoff_active(cur):
            return [], orig
        cfg = self._config.get(loc.key, {})
        initial = int_option(cfg, "initial_messages", DEFAULT_INITIAL_POSTS)
        try:
            now_ident, _ = whoami(token)  # every read: a swapped token must never be searched under the old identity
        except NetError as e:
            return [], with_status(cur, str(e), e.retry_after_s if e.kind == "rate_limit" else None)
        ident = cur.get("identity")
        if ident is None:
            ident = cur["identity"] = now_ident
        elif ident.get("user_id") != now_ident["user_id"] or ident.get("team_id") != now_ident["team_id"]:
            return [], with_status(cur, "the token now belongs to a different user or workspace than the one bound; "
                                        "nothing ingested")
        team, uid = ident["team_id"], ident["user_id"]
        flt = _Filters(cfg)

        def fetch(page_token, _limit):
            return self._page(token, uid, page_token)

        seen = dict(cur.get("seen") or {})
        learned: dict[str, str] = {}
        emitted: set[str] = set()
        floor = [_ts_float(cur["floor"]) if isinstance(cur.get("floor"), str) else None]  # see the docstring

        emptied: list[str] = []  # stored messages the user has since edited down to nothing

        def convert(m):
            if cur.get("high_water") is None:  # the first scan: remember how far back it reached
                t = _ts_float(str(m.get("ts")))
                floor[0] = t if floor[0] is None else min(floor[0], t)
            return self._convert(m, team, uid, flt, seen, learned, emitted, floor[0], emptied)

        # Resume one page back: pages are numbered, so a message deleted above the stored page shifts the later ones
        # up and the first of them would be skipped. `seen` keeps the re-read page from being stored twice.
        stored = cur.get("scan") if isinstance(cur.get("scan"), dict) else None
        stored_page = str(stored.get("token") or "") if stored else ""
        base_fetched = 0
        if stored and stored_page.isdigit() and int(stored_page) > 1:
            base_fetched = max(0, int(stored.get("fetched") or 0) - PAGE_SIZE)
            cur["scan"] = {**stored, "token": str(int(stored_page) - 1), "fetched": base_fetched}
        res = scan(cur, fetch=fetch, marker=lambda m: m.get("ts") if _TS.match(str(m.get("ts"))) else None,
                   at_high_water=lambda m, hw: _ts_float(m["ts"]) < _ts_float(hw) - OVERLAP_S,
                   convert=convert, page_size=PAGE_SIZE, initial=initial)
        if stored and res.error is not None and res.fetched == base_fetched and res.cursor.get("scan"):
            res.cursor["scan"] = stored  # no page was read: do not walk back another page at every retry
        seen.update(learned)
        new, units, deletes = res.cursor, res.units, list(dict.fromkeys(emptied))
        for ref in deletes:
            seen.pop(ref, None)
        limited: list[float] = []  # a reconcile that hit a rate limit: its wait
        if floor[0] is not None and floor[0] > 0:
            new["floor"] = f"{floor[0]:.6f}"
        if res.error is None and res.completed:
            now = utcnow()
            interval = int_option(cfg, "reconcile_interval_s", DEFAULT_RECONCILE_INTERVAL_S)
            if "reconciled_at" not in new:
                new["reconciled_at"] = now.isoformat(timespec="seconds")
            elif reconcile_due(new, interval, now):
                out = self._reconcile(token, fetch, team, uid, flt, seen, limited)
                if out is not None:  # None: it could not finish; try again on the next read
                    edited, gone, fresh = out
                    units = [*units, *edited]
                    seen.update(fresh)
                    for ref in gone:
                        seen.pop(ref, None)
                    deletes = list(dict.fromkeys([*deletes, *gone]))
                    new["reconciled_at"] = now.isoformat(timespec="seconds")
        new["seen"] = cap_seen(seen, lambda k, v: _ref_ts(k))
        if deletes:
            new["_delete_refs"] = deletes
        if res.error is not None:
            return units, with_status(new, str(res.error),
                                      res.error.retry_after_s if res.error.kind == "rate_limit" else None)
        if limited:  # the scan's units and cursor are kept; the reconcile waits for Slack's Retry-After
            return units, with_status(new, f"{HOST}: rate limited during the reconcile (it is retried later)", limited[0])
        return units, new

    def _page(self, token: str, uid: str, page_token) -> tuple[list[dict], str | None]:
        """One search page: (matches, token of the next page or None). The token is the page number."""
        try:
            page = int(page_token) if page_token else 1
        except ValueError:
            raise NetError("http", f"{HOST}: unusable page token", 400) from None
        if page > MAX_SEARCH_PAGE:
            return [], None  # the search window ends here: stop cleanly
        data, _ = _call(token, "search.messages", {
            "query": f"from:<@{uid}>", "sort": "timestamp", "sort_dir": "desc", "count": PAGE_SIZE, "page": page,
            "highlight": "false"})
        block = data.get("messages")
        matches = block.get("matches") if isinstance(block, dict) else None
        if not isinstance(matches, list):
            raise NetError("bad_json", f"{HOST}: unexpected search shape")
        items = [m for m in matches if isinstance(m, dict)]
        pages = None
        for key, field in (("paging", "pages"), ("pagination", "page_count")):
            info = block.get(key)
            if isinstance(info, dict) and isinstance(info.get(field), int):
                pages = info[field]
        more = page < pages if pages is not None else len(matches) >= PAGE_SIZE
        return items, (str(page + 1) if more and page < MAX_SEARCH_PAGE and items else None)

    # --- reconcile -------------------------------------------------------------------------------------------------

    def _reconcile(self, token: str, fetch, team: str, uid: str, flt: _Filters, seen: dict, limited: list):
        """(edited units, deleted refs, new fingerprints) over the latest RECONCILE_ITEMS messages, or None when the
        check could not be completed (a rate limit's Retry-After is appended to `limited`). A stored message that is
        now empty text counts as deleted."""
        items: list[dict] = []
        page_token = None
        try:
            while len(items) < RECONCILE_ITEMS:
                page, page_token = fetch(page_token, PAGE_SIZE)
                items += page
                if not page:
                    page_token = None
                    break
        except NetError as e:
            if e.kind == "rate_limit":
                limited.append(e.retry_after_s or float(DEFAULT_RETRY_S))
            return None
        truncated = (page_token is not None or len(items) > RECONCILE_ITEMS) and bool(items)
        items = items[:RECONCILE_ITEMS]
        present, changed, emptied = set(), [], []
        for m in items:
            ref = self._own_ref(m, team, uid)
            if ref is None:
                continue
            present.add(ref)  # present regardless of today's channel filters: a filter change is not a delete
            text = self._text(m)
            if ref in seen and flt.keep(m):
                if not text:
                    emptied.append(ref)  # edited down to nothing: the stored text must not outlive it
                elif seen[ref] != text_hash(text):
                    changed.append(m)
        oldest = _ts_float(str(items[-1].get("ts"))) if truncated else None
        candidates = [r for r in seen if r not in present and (oldest is None or _ref_ts(r) > oldest)]
        candidates = sorted(candidates, key=_ref_ts, reverse=True)[:RECONCILE_CHECKS]
        gone = []
        for ref in candidates:
            parts = ref.split(":")
            if len(parts) != 3 or parts[0] != team or not _ID.match(parts[1]) or not _TS.match(parts[2]):
                continue
            try:
                _call(token, "chat.getPermalink", {"channel": parts[1], "message_ts": parts[2]})
            except NetError as e:
                if e.kind == "not_found":
                    gone.append(ref)  # the only answer that counts as a delete
                elif e.kind == "rate_limit":
                    limited.append(e.retry_after_s or float(DEFAULT_RETRY_S))
                    return None
                continue  # no access, error, timeout: unknown, leave it alone
        learned: dict[str, str] = {}
        edited = [u for m in changed for u in self._convert(m, team, uid, flt, {}, learned, set())]
        return edited, list(dict.fromkeys([*emptied, *gone])), learned

    # --- one message -----------------------------------------------------------------------------------------------

    @staticmethod
    def _own_ref(m: dict, team: str, uid: str) -> str | None:
        """`team:channel:ts` when `m` is a plain message of the bound user, else None."""
        ch = m.get("channel") if isinstance(m.get("channel"), dict) else {}
        cid, ts = ch.get("id"), m.get("ts")
        if m.get("type", "message") != "message" or m.get("user") != uid or m.get("bot_id"):
            return None
        if m.get("subtype") not in KEEP_SUBTYPES:
            return None
        if not (isinstance(cid, str) and _ID.match(cid) and isinstance(ts, str) and _TS.match(ts)):
            return None
        return f"{team}:{cid}:{ts}"

    @staticmethod
    def _reply_to(m: dict) -> str | None:
        """The thread root ts when the message is a thread reply (from `thread_ts`, else the permalink's query)."""
        ts = m.get("thread_ts")
        if not isinstance(ts, str):
            link = m.get("permalink")
            try:
                q = parse_qs(urlsplit(link).query) if isinstance(link, str) else {}
            except ValueError:
                q = {}
            ts = (q.get("thread_ts") or [None])[0]
        return ts if isinstance(ts, str) and _TS.match(ts) and ts != m.get("ts") else None

    def _text(self, m: dict) -> str:
        raw = m.get("text")
        if not isinstance(raw, str):
            return ""
        return slack_text(raw, self._reply_to(m) is not None)

    def _convert(self, m: dict, team: str, uid: str, flt: _Filters, seen: dict, learned: dict,
                 emitted: set, floor: float | None = None, emptied: list | None = None) -> list[Unit]:
        ref = self._own_ref(m, team, uid)
        if ref is None or ref in emitted or not flt.keep(m):
            return []
        if floor is not None and ref not in seen and _ts_float(m["ts"]) < floor - 1e-3:
            return []  # older than the first run reached: the overlap re-read must not widen that bound
        text = self._text(m)
        if not text:
            if emptied is not None and ref in seen:
                emptied.append(ref)  # edited down to nothing
            return []
        if ref in seen and seen[ref] == text_hash(text):
            return []  # the overlap re-read it: already stored with this text
        try:
            ts = datetime.fromtimestamp(float(m["ts"]), UTC)
        except (OverflowError, OSError, ValueError):
            return []
        cid = m["channel"]["id"]
        kind = flt.conversation(m)
        reply_to = self._reply_to(m)
        register = flt.dm_register if kind != "channel" else flt.register
        if kind == "channel":
            audience = "group"
        else:
            audience = "close" if register == "close" else ("group" if kind == "group_dm" else "colleague")
        link = m.get("permalink")
        meta = {"public": False, "conversation": kind}
        if isinstance(link, str) and link.startswith("https://"):
            meta["url"] = link
        u = own_unit(source=self.name, ref=ref, ts=ts, text=text, kind="message", register=register, audience=audience,
                     thread_id=hash_ref(f"{team}:{cid}:{reply_to or m['ts']}"),
                     audience_ref=hash_ref(f"{team}:{cid}") if kind != "channel" else None, meta=meta)
        if u is None:
            if emptied is not None and ref in seen:
                emptied.append(ref)  # e.g. now only a URL
            return []
        emitted.add(ref)
        learned[ref] = text_hash(text)
        return [u]


class _Filters:
    """Which conversations are read, from the `[sources.slack]` options."""

    def __init__(self, cfg: dict | None):
        self.include_dms = (cfg or {}).get("include_dms") is True
        self.include = _names(cfg, "include_channels")
        self.exclude = _names(cfg, "exclude_channels")
        self.register = _register(cfg, "register", "work")
        self.dm_register = _register(cfg, "dm_register", self.register)

    @staticmethod
    def conversation(m: dict) -> str:
        ch = m.get("channel") if isinstance(m.get("channel"), dict) else {}
        cid = str(ch.get("id") or "")
        if ch.get("is_mpim") or str(ch.get("name") or "").startswith("mpdm-"):
            return "group_dm"
        if ch.get("is_im") or cid.startswith("D"):
            return "dm"
        if (ch.get("is_channel") is True or ch.get("is_group") is True or cid.startswith("C")
                or (ch.get("is_mpim") is False and ch.get("is_private") is True)):  # a legacy private channel
            return "channel"
        return "dm"  # not positively a channel: fail closed, so `include_dms = false` keeps it out

    def keep(self, m: dict) -> bool:
        ch = m.get("channel") if isinstance(m.get("channel"), dict) else {}
        kind = self.conversation(m)
        ids = {str(ch.get("id") or "").lower()}
        if kind == "channel":
            ids.add(str(ch.get("name") or "").lstrip("#").lower())
        ids.discard("")
        if ids & self.exclude:
            return False
        if kind != "channel":
            return self.include_dms
        return not self.include or bool(ids & self.include)
