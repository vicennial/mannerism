"""X through the official API (paid, pay-per-use, opt-in): the user's own public posts.

Opt-in: `[sources.x]` needs `username = "name"`, `allow_network = true` and a bearer token in the ENVIRONMENT
(`X_BEARER_TOKEN`; it is never read from the config and never written to the cursor, a log or an error message).
Without the token, `discover` returns nothing and `status_message` says why. `discover` makes no network call.

`read` resolves the username once (`/2/users/by/username/:username`; a protected account is refused), binds to the user id
and then reads `/2/users/:id/tweets` newest first with `since_id` (the completed high-water mark) and `pagination_token`
(the page continuation), `exclude=retweets`, long-form `note_tweet.text` preferred over the truncated `text`. Replies lose
their leading @mentions and keep no parent text (fetching it would be billed); other mentions become `@[user]`; t.co
links are removed; a quote post keeps only the user's own words.

Cost controls (the X API bills per post read): `max_posts_per_run` (default 50) and `max_posts_per_month` (default 300),
`initial_posts` (default 100) for the first run. Before EVERY request a reservation is written to a small usage ledger
(`<mannerism home>/x-usage.json`, written atomically) and mirrored in the cursor; the ledger survives a crash that the
cursor (stored by the engine only after `read` returns) would not, and the larger of the two counts. The reservation is
settled to the posts really returned, or refunded after an HTTP error. A 429 stores `x-rate-limit-reset` as the retry
time.

No reconcile (it would cost money): a post that is deleted or edited on X AFTER it was stored is not noticed, and the
stored copy stays as it was. `mannerism forget --source x` removes everything this source stored. A continuation token
X rejects (400/404/410) drops the `scan`; on a first run that already read posts, the newest post seen becomes the
high-water mark, so the restart does not read (and pay for) the same posts again. The rejected request's reservation is
given back (an answered error is not billed).

Cursor: {"v": 1, "identity": {"username", "id"}, "high_water": "<tweet id>" | absent, "scan": {"token", "candidate",
"fetched"} | absent, "usage": {"month", "posts", "lookups"}, "status": {...} | absent}. See `_social.scan`.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

from ..core import Location, Me, Unit, hash_ref, home
from ._http import NetError, build_url, get_json
from ._social import (
    backoff_active,
    clean_cursor,
    finish_text,
    int_option,
    month_key,
    network_enabled,
    next_month_start,
    scan,
    utcnow,
    with_status,
)
from ._util import own_unit, parse_iso
from .x_archive import clean_tweet_text

HOST = "api.x.com"
TOKEN_ENV = "X_BEARER_TOKEN"

# X API pricing is pay-per-use. These rates come from third-party pricing summaries (about $0.005 per post read, $0.010
# per user lookup) and MUST be checked against the X developer console before they are trusted: they only drive the
# estimate printed on enable and the usd figure in the cursor; the hard limits below count posts, not dollars.
COST_PER_POST_READ_USD = 0.005
COST_PER_USER_LOOKUP_USD = 0.010

DEFAULT_MAX_POSTS_PER_RUN = 50
DEFAULT_MAX_POSTS_PER_MONTH = 300
DEFAULT_INITIAL_POSTS = 100
MIN_PAGE = 5  # the API refuses max_results below 5
MAX_PAGE = 100
PROTECTED_RECHECK_S = 7 * 24 * 3600
_USERNAME = re.compile(r"^[A-Za-z0-9_]{1,15}$")
TWEET_FIELDS = "created_at,referenced_tweets,note_tweet,entities,author_id"


def normalize_username(value) -> str | None:
    if not isinstance(value, str):
        return None
    u = value.strip().lstrip("@")
    return u if _USERNAME.match(u) else None


# --- usage ledger ------------------------------------------------------------------------------------------------


def ledger_path() -> Path:
    return home() / "x-usage.json"


def _month_usage(raw, month: str) -> dict:
    if isinstance(raw, dict) and raw.get("month") == month:
        try:
            return {"month": month, "posts": max(0, int(raw.get("posts") or 0)),
                    "lookups": max(0, int(raw.get("lookups") or 0))}
        except (TypeError, ValueError):
            pass
    return {"month": month, "posts": 0, "lookups": 0}


class Usage:
    """This calendar month's reservations. The ledger file is shared by every X location (the token, not the username,
    is what gets billed); the cursor keeps a copy. The larger of the two counts, so a crash cannot lose accounting."""

    def __init__(self, cursor_usage, now=None):
        self.now = now or utcnow()
        self.month = month_key(self.now)
        a = _month_usage(cursor_usage, self.month)
        try:
            b = _month_usage(json.loads(ledger_path().read_text("utf-8")), self.month)
        except (OSError, ValueError):
            b = _month_usage(None, self.month)
        self.posts = max(a["posts"], b["posts"])
        self.lookups = max(a["lookups"], b["lookups"])

    def as_dict(self) -> dict:
        usd = self.posts * COST_PER_POST_READ_USD + self.lookups * COST_PER_USER_LOOKUP_USD
        return {"month": self.month, "posts": self.posts, "lookups": self.lookups, "approx_usd": round(usd, 4)}

    def save(self) -> None:
        """Atomic, owner-only. Called BEFORE the request that the reservation pays for."""
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(prefix=".x-usage-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({k: v for k, v in self.as_dict().items() if k != "approx_usd"}, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    def reserve(self, posts: int = 0, lookups: int = 0) -> None:
        self.posts += posts
        self.lookups += lookups
        self.save()

    def release(self, posts: int = 0, lookups: int = 0) -> None:
        self.posts = max(0, self.posts - posts)
        self.lookups = max(0, self.lookups - lookups)
        self.save()


def estimate(config: dict | None = None) -> tuple[int, float]:
    """(posts, approx USD) of the FIRST run: one user lookup plus `initial_posts` post reads, capped by the monthly
    limit. Rates are COST_PER_* (check them in the X developer console)."""
    posts = min(int_option(config, "initial_posts", DEFAULT_INITIAL_POSTS),
                int_option(config, "max_posts_per_month", DEFAULT_MAX_POSTS_PER_MONTH))
    return posts, round(COST_PER_USER_LOOKUP_USD + posts * COST_PER_POST_READ_USD, 4)


def monthly_ceiling(config: dict | None = None) -> tuple[int, float]:
    """(posts, approx USD) at most per calendar month with the configured `max_posts_per_month` (plus the one lookup)."""
    posts = int_option(config, "max_posts_per_month", DEFAULT_MAX_POSTS_PER_MONTH)
    return posts, round(COST_PER_USER_LOOKUP_USD + posts * COST_PER_POST_READ_USD, 4)


class XApi:
    name = "x"
    tier = "auto"
    network = True
    network_hosts = (HOST,)
    default_interval_s = 21600
    session_start_default = False  # paid: never at session start unless the user asks for it
    rate_caveat = (
        "rates are estimates from third-party summaries of X's pay-per-use pricing; check your X developer console"
    )

    def __init__(self):
        self._config: dict[str, dict] = {}

    def status_message(self, config: dict | None) -> str | None:
        if not network_enabled(config):
            return "x: set allow_network = true in [sources.x]; the X API is billed per post (see `estimate`)"
        if normalize_username((config or {}).get("username")) is None:
            return "x: set username = \"name\" in [sources.x]"
        if not os.environ.get(TOKEN_ENV):
            return f"x: the {TOKEN_ENV} environment variable is not set; create a bearer token in the X developer console"
        return None

    def estimate(self, config: dict | None = None) -> tuple[int, float]:
        return estimate(config)

    def monthly_ceiling(self, config: dict | None = None) -> float:
        """Approximate USD per calendar month at most, with the configured `max_posts_per_month` (see `rate_caveat`)."""
        return monthly_ceiling(config)[1]

    def discover(self, me: Me, config: dict) -> list[Location]:
        """No network call. Empty unless allow_network is true, a valid `username` is set and X_BEARER_TOKEN exists."""
        try:
            if self.status_message(config) is not None:
                return []
            user = normalize_username(config.get("username"))
            key = f"x:{user.lower()}"
            self._config[key] = dict(config)
            return [Location(self.name, key, None, f"X @{user} (official API, billed per post)")]
        except Exception:  # noqa: BLE001
            return []

    # --- reading ---------------------------------------------------------------------------------------------------

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        orig = cursor if isinstance(cursor, dict) else {}
        try:
            return self._read(loc, orig)
        except Exception as e:  # noqa: BLE001 - a source never raises into the engine; message holds no credential
            return [], with_status(clean_cursor(orig), f"{type(e).__name__}")

    def _read(self, loc: Location, orig: dict):
        cur = clean_cursor(orig)
        token = os.environ.get(TOKEN_ENV)
        if not token:
            return [], with_status(cur, f"{TOKEN_ENV} is not set")
        if backoff_active(cur):
            return [], orig
        username = loc.key.split(":", 1)[1]
        cfg = self._config.get(loc.key, {})
        per_run = int_option(cfg, "max_posts_per_run", DEFAULT_MAX_POSTS_PER_RUN)
        per_month = int_option(cfg, "max_posts_per_month", DEFAULT_MAX_POSTS_PER_MONTH)
        initial = int_option(cfg, "initial_posts", DEFAULT_INITIAL_POSTS)
        auth = {"Authorization": f"Bearer {token}"}  # sent only to HOST (get_json refuses other origins)
        usage = Usage(cur.get("usage"))
        cur["usage"] = usage.as_dict()

        def failed(e: NetError, base: dict):
            wait = e.retry_after_s if e.kind == "rate_limit" else None
            return with_status({**base, "usage": usage.as_dict()}, str(e), wait)

        ident = cur.get("identity")
        if ident is None:
            usage.reserve(lookups=1)
            try:
                data, _ = get_json(build_url(HOST, f"/2/users/by/username/{username}", {"user.fields": "protected"}), auth)
            except NetError as e:
                if e.kind in ("http", "rate_limit"):
                    usage.release(lookups=1)
                return [], failed(e, cur)
            user = data.get("data") if isinstance(data, dict) else None
            if not isinstance(user, dict) or not str(user.get("id")).isdigit():
                usage.release(lookups=1)
                return [], failed(NetError("bad_json", f"{HOST}: user lookup returned no user"), cur)
            if user.get("protected"):
                return [], with_status({**cur, "usage": usage.as_dict()}, "the account is protected; nothing ingested",
                                       PROTECTED_RECHECK_S)
            ident = cur["identity"] = {"username": username, "id": user["id"]}
        user_id = ident["id"]
        run_left = [per_run]

        def fetch(page_token, limit):
            month_left = per_month - usage.posts
            room = min(run_left[0], month_left)
            if room < MIN_PAGE:
                raise NetError("budget", "monthly post budget reached" if month_left < MIN_PAGE
                               else "per-run post budget reached")
            k = max(MIN_PAGE, min(limit, MAX_PAGE, room))
            usage.reserve(posts=k)  # durable BEFORE the request
            params = {"max_results": k, "exclude": "retweets", "tweet.fields": TWEET_FIELDS,
                      "since_id": cur.get("high_water"), "pagination_token": page_token}
            try:
                data, _ = get_json(build_url(HOST, f"/2/users/{user_id}/tweets", params), auth)
            except NetError as e:
                if e.kind in ("http", "rate_limit"):
                    usage.release(posts=k)  # an answered error is not billed; a timeout may have been, so it stays
                raise
            if not isinstance(data, dict):
                usage.release(posts=k)
                raise NetError("bad_json", f"{HOST}: unexpected tweets shape")
            tweets = [t for t in (data.get("data") or []) if isinstance(t, dict) and isinstance(t.get("id"), str)]
            usage.release(posts=max(0, k - len(tweets)))  # billed per post actually returned
            run_left[0] -= len(tweets)
            meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
            nxt = meta.get("next_token")
            return tweets, (nxt if isinstance(nxt, str) and nxt else None)

        res = scan(cur, fetch=fetch, marker=lambda t: t["id"], at_high_water=lambda t, hw: False,
                   convert=lambda t: self._convert(t, user_id), page_size=MAX_PAGE, initial=initial, promote_on_drop=True)
        new = {**res.cursor, "usage": usage.as_dict()}
        if res.error is not None:
            wait = res.error.retry_after_s if res.error.kind == "rate_limit" else None
            if res.error.kind == "budget" and usage.posts + MIN_PAGE > per_month:
                wait = (next_month_start(usage.now) - usage.now).total_seconds()
            if res.error.kind == "budget" and wait is None:
                return res.units, new  # the per-run cap is normal operation, not a failure
            return res.units, with_status(new, str(res.error), wait)
        return res.units, new

    # --- one post --------------------------------------------------------------------------------------------------

    def _convert(self, t: dict, user_id: str) -> list[Unit]:
        if t.get("author_id") not in (None, user_id):
            return []
        refs = [r for r in (t.get("referenced_tweets") or []) if isinstance(r, dict)]
        kinds = {r.get("type") for r in refs}
        if "retweeted" in kinds:
            return []
        note = t.get("note_tweet") if isinstance(t.get("note_tweet"), dict) else {}
        raw = note.get("text") if isinstance(note.get("text"), str) and note["text"].strip() else t.get("text")
        if not isinstance(raw, str):
            return []
        is_reply = "replied_to" in kinds
        text = finish_text(clean_tweet_text(raw, is_reply), is_reply)
        ts = parse_iso(t.get("created_at"))
        if ts is None or not text:
            return []
        meta = {"public": True, "url": f"https://x.com/i/status/{t['id']}"}
        if "quoted" in kinds:
            meta["quote"] = True
        parent = next((r.get("id") for r in refs if r.get("type") == "replied_to"), None)
        u = own_unit(source=self.name, ref=f"tweet:{t['id']}", ts=ts, text=text,
                     kind="reply" if is_reply else "post", register="public", audience="public",
                     thread_id=hash_ref(str(parent or t["id"])), meta=meta)
        return [u] if u is not None else []
