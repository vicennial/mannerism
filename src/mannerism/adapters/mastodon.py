"""Mastodon (and compatible servers): the user's own public posts from their home instance's open API (no login).

Opt-in: `[sources.mastodon]` needs `account = "@user@instance.example"` and `allow_network = true`. `discover` makes no
network call. `read` looks the account up once (`/api/v1/accounts/lookup`), binds to its id + instance in the cursor and
re-checks that the id is unchanged at the start of every new scan; a different id stops the source instead of ingesting.

`/api/v1/accounts/:id/statuses?exclude_reblogs=true` is read newest first with `since_id` (the completed high-water
mark) and `max_id` (the page continuation). Kept: statuses authored by the bound account with visibility `public` or
`unlisted` (private and direct are skipped). The HTML content becomes text (stdlib `html.parser`): link previews that
only show a URL are dropped, the leading mention links of a reply are dropped, other mention links become `@[user]`,
custom-emoji shortcodes are removed. A reply gets one masked context line from its parent (up to MAX_CONTEXT_FETCHES
extra requests per read; a failure just means no context). A quote post keeps only the user's own words.

Ordering. The listing is ordered by status id, and ids are assigned in creation order (a snowflake of the creation
time), so unlike Bluesky there is no skewed sort key: `since_id` is an exact high-water mark and needs no overlap.

Deletes and edits. A status that is deleted or edited later sits below the high-water mark and is never re-read by a
normal scan. At most once per `reconcile_interval_s` (default 7 days) a completed scan is followed by a reconcile: the
latest RECONCILE_ITEMS statuses are re-read; a stored status whose cleaned text differs from the fingerprint kept in
`seen` is emitted again under the same ref, which updates the stored text (statuses never stored are not added); a remembered id that
falls inside the window but is missing from it is confirmed with `GET /api/v1/statuses/:id` (at most RECONCILE_CHECKS)
and, when the server answers 404/410 or the status is no longer public/unlisted, reported in `cursor["_delete_refs"]`.
A failed or rate-limited reconcile is skipped and retried on the next read. Ids that are not numeric are not checked for
deletion (their order is unknown).

Cursor: {"v": 1, "identity": {"instance", "id", "acct"}, "high_water": "<status id>" | absent, "scan": {"token",
"candidate", "fetched"} | absent, "seen": {status id: text fingerprint}, "reconciled_at": iso, "status": {...} | absent}.
See `_social.scan`. A continuation token the server rejects (400/404/410) drops `scan`; the next read starts a new one.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from html.parser import HTMLParser

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
    text_hash,
    utcnow,
    with_status,
)
from ._util import own_unit, parse_iso

PAGE_SIZE = 40  # the API maximum
MAX_CONTEXT_FETCHES = 10
KEEP_VISIBILITY = ("public", "unlisted")
_INSTANCE = re.compile(r"^(?=.{4,253}$)[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$")
_USER = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")  # ids go into URL paths: never trust a server-supplied "../"


def parse_account(value) -> tuple[str, str] | None:
    """`@User@Instance.example` (leading @ optional) -> ("User", "instance.example"); None when malformed."""
    if not isinstance(value, str):
        return None
    parts = value.strip().lstrip("@").split("@")
    if len(parts) != 2 or not _USER.match(parts[0]) or not _INSTANCE.match(parts[1].lower()):
        return None
    return parts[0], parts[1].lower()


class _Text(HTMLParser):
    """Mastodon status HTML -> text. Mention links become MENTION_MARK, URL-only links vanish."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._a: dict | None = None
        self._hidden: list[bool] = []

    def handle_starttag(self, tag, attrs):
        cls = (dict(attrs).get("class") or "").split()
        if tag == "br":
            self.out.append("\n")
        elif tag == "a":
            mention = "mention" in cls and "hashtag" not in cls
            self._a = {"mention": mention, "hashtag": "hashtag" in cls, "vis": [], "full": []}
            if mention:
                self.out.append(MENTION_MARK)
        elif tag == "span":
            self._hidden.append("invisible" in cls)

    def handle_endtag(self, tag):
        if tag == "p":
            self.out.append("\n\n")
        elif tag == "span" and self._hidden:
            self._hidden.pop()
        elif tag == "a" and self._a is not None:
            a, self._a = self._a, None
            if a["mention"]:
                return
            vis, full = "".join(a["vis"]), "".join(a["full"]).strip()
            if a["hashtag"]:
                self.out.append(vis)
            elif not (full.lower().startswith(("http://", "https://", "www.")) or vis.rstrip().endswith(("…", "..."))):
                self.out.append(vis)

    def handle_data(self, data):
        hidden = any(self._hidden)
        if self._a is not None:
            if not self._a["mention"]:
                self._a["full"].append(data)
                if not hidden:
                    self._a["vis"].append(data)
        elif not hidden:
            self.out.append(data)


def html_to_text(content: str, reply: bool = False, emojis=None) -> str:
    p = _Text()
    try:
        p.feed(content)
        p.close()
    except Exception:  # noqa: BLE001 - malformed markup: keep what was parsed
        pass
    text = "".join(p.out)
    for e in emojis if isinstance(emojis, list) else []:
        if isinstance(e, dict) and isinstance(e.get("shortcode"), str):
            text = text.replace(f":{e['shortcode']}:", "")
    return re.sub(r"\n{3,}", "\n\n", finish_text(text, reply))


def _not_newer(a: str, b: str) -> bool:
    """Is status id `a` not newer than `b`? Ids are numeric snowflakes on Mastodon; other servers: unknown -> False."""
    return a.isdigit() and b.isdigit() and int(a) <= int(b)


class Mastodon:
    name = "mastodon"
    tier = "auto"
    network = True
    network_hosts = ("<instance of `account`>",)  # the real host is the configured instance: see hosts()
    default_interval_s = 3600
    session_start_default = True  # free and light: core may run it at session start (`run_on_session_start`)

    def __init__(self):
        self._config: dict[str, dict] = {}

    def hosts(self, config: dict | None) -> tuple[str, ...]:
        acct = parse_account((config or {}).get("account"))
        return (acct[1],) if acct else ()

    def status_message(self, config: dict | None) -> str | None:
        if not network_enabled(config):
            return "mastodon: set allow_network = true in [sources.mastodon] to let mannerism read your public posts"
        if parse_account((config or {}).get("account")) is None:
            return "mastodon: set account = \"@user@instance.example\" in [sources.mastodon]"
        return None

    def discover(self, me: Me, config: dict) -> list[Location]:
        """No network call. Empty unless `allow_network` is true and a valid `account` is configured."""
        try:
            if self.status_message(config) is not None:
                return []
            user, instance = parse_account(config.get("account"))
            key = f"mastodon:{user.lower()}@{instance}"
            self._config[key] = dict(config)
            return [Location(self.name, key, None, f"Mastodon @{user}@{instance} (public posts)")]
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
        user, instance = loc.key.split(":", 1)[1].rsplit("@", 1)
        cfg = self._config.get(loc.key, {})
        initial = int_option(cfg, "initial_posts", DEFAULT_INITIAL_POSTS)
        ident = cur.get("identity")
        try:
            if ident is None or not cur.get("scan"):  # first run, or a new scan: (re)check the account binding
                acct = self._lookup(instance, user)
                if ident is None:
                    ident = cur["identity"] = {"instance": instance, "id": acct, "acct": user}
                elif acct != ident.get("id"):
                    return [], with_status(
                        cur, f"{user}@{instance} now resolves to a different account id than the one bound; nothing ingested"
                    )
        except NetError as e:
            return [], with_status(cur, str(e), e.retry_after_s if e.kind == "rate_limit" else None)
        acct_id = ident["id"]
        pages: dict[str, dict] = {}  # statuses fetched in this read (parents of self-replies are found here)
        budget = [MAX_CONTEXT_FETCHES]

        def get_page(token, limit, since):
            data, _ = get_json(build_url(instance, f"/api/v1/accounts/{acct_id}/statuses", {
                "exclude_reblogs": "true", "limit": limit, "since_id": since, "max_id": token}))
            if not isinstance(data, list):
                raise NetError("bad_json", f"{instance}: unexpected statuses shape")
            items = [x for x in data if isinstance(x, dict) and isinstance(x.get("id"), str)]
            for x in items:
                pages[x["id"]] = x
            return items, (items[-1]["id"] if items else None)

        seen = dict(cur.get("seen") or {})
        learned: dict[str, str] = {}
        res = scan(cur, fetch=lambda token, limit: get_page(token, limit, cur.get("high_water")),
                   marker=lambda s: s["id"],
                   at_high_water=lambda s, hw: isinstance(hw, str) and _not_newer(s["id"], hw),
                   convert=lambda s: self._convert(s, instance, acct_id, pages, budget, learned),
                   page_size=PAGE_SIZE, initial=initial)
        seen.update(learned)
        new, units, deletes = res.cursor, res.units, []
        if res.error is None and res.completed:
            now = utcnow()
            interval = int_option(cfg, "reconcile_interval_s", DEFAULT_RECONCILE_INTERVAL_S)
            if "reconciled_at" not in new:
                new["reconciled_at"] = now.isoformat(timespec="seconds")
            elif reconcile_due(new, interval, now):
                out = self._reconcile(get_page, instance, acct_id, seen, pages)
                if out is not None:  # None: it could not finish; try again on the next read
                    edited, deletes, fresh = out
                    units = [*units, *edited]
                    seen.update(fresh)
                    for sid in deletes:
                        seen.pop(sid, None)
                    new["reconciled_at"] = now.isoformat(timespec="seconds")
        new["seen"] = cap_seen(seen, lambda k, v: (len(k), k))
        if deletes:
            new["_delete_refs"] = [f"{instance}/{sid}" for sid in deletes]
        if res.error is not None:
            return units, with_status(new, str(res.error),
                                      res.error.retry_after_s if res.error.kind == "rate_limit" else None)
        return units, new

    def _reconcile(self, get_page, instance: str, acct_id: str, seen: dict, pages: dict):
        """(edited units, deleted status ids, new fingerprints) for the latest RECONCILE_ITEMS statuses, or None when
        the check could not be completed."""
        items: list[dict] = []
        token = None
        try:
            while len(items) < RECONCILE_ITEMS:
                page, token = get_page(token, min(PAGE_SIZE, RECONCILE_ITEMS - len(items)), None)
                items += page
                if not page:
                    token = None
                    break
        except NetError:
            return None
        truncated = token is not None and bool(items)
        present, changed = set(), []
        for st in items:
            author = st.get("account") if isinstance(st.get("account"), dict) else {}
            if st.get("reblog") or author.get("id") != acct_id or st.get("visibility") not in KEEP_VISIBILITY:
                continue
            present.add(st["id"])
            text = self._text(st)
            if text and st["id"] in seen and seen[st["id"]] != text_hash(text):
                changed.append(st)  # edited since it was stored (statuses never stored stay out: first-run bound)
        oldest = items[-1]["id"] if truncated else None
        candidates = [sid for sid in seen if sid not in present
                      and (oldest is None or (sid.isdigit() and oldest.isdigit() and int(sid) > int(oldest)))]
        candidates = sorted(candidates, key=lambda i: (len(i), i), reverse=True)[:RECONCILE_CHECKS]
        gone = []
        for sid in candidates:
            try:
                data, _ = get_json(build_url(instance, f"/api/v1/statuses/{sid}"))
            except NetError as e:
                if e.kind == "http" and e.status in (404, 410):
                    gone.append(sid)
                elif e.kind == "rate_limit":
                    return None
                continue  # unknown: leave it alone
            author = data.get("account") if isinstance(data, dict) and isinstance(data.get("account"), dict) else {}
            if isinstance(data, dict) and (author.get("id") != acct_id or data.get("visibility") not in KEEP_VISIBILITY):
                gone.append(sid)  # no longer public (or the id now belongs to something else)
        learned: dict[str, str] = {}
        budget = [MAX_CONTEXT_FETCHES]
        edited = [u for st in changed for u in self._convert(st, instance, acct_id, pages, budget, learned)]
        return edited, gone, learned

    def _lookup(self, instance: str, user: str) -> str:
        data, _ = get_json(build_url(instance, "/api/v1/accounts/lookup", {"acct": user}))
        ident = data.get("id") if isinstance(data, dict) else None
        if not isinstance(ident, str) or not _ID.match(ident):
            raise NetError("bad_json", f"{instance}: account lookup returned no usable id")
        return ident

    # --- one status ------------------------------------------------------------------------------------------------

    @staticmethod
    def _text(st: dict) -> str:
        """The cleaned text of a status (what would be stored, before PII masking); "" when there is none."""
        content = st.get("content")
        if not isinstance(content, str):
            return ""
        reply_to = st.get("in_reply_to_id")
        return html_to_text(content, isinstance(reply_to, str) and bool(reply_to), st.get("emojis"))

    def _convert(self, st: dict, instance: str, acct_id: str, pages: dict, budget: list, learned: dict) -> list[Unit]:
        author = st.get("account") if isinstance(st.get("account"), dict) else {}
        if st.get("reblog") or author.get("id") != acct_id or st.get("visibility") not in KEEP_VISIBILITY:
            return []
        text = self._text(st)
        reply_to = st.get("in_reply_to_id")
        is_reply = isinstance(reply_to, str) and bool(reply_to)
        ts = parse_iso(st.get("created_at"))
        if ts is None or not text:
            return []
        context: list[ContextLine] = []
        audience_ref = None
        if is_reply:
            line = self._parent_context(reply_to, instance, acct_id, pages, budget)
            if line is not None:
                context.append(line)
            other = st.get("in_reply_to_account_id")
            if isinstance(other, str) and other != acct_id:
                audience_ref = hash_ref(f"{instance}:{other}")
        url = st.get("url") if isinstance(st.get("url"), str) and st["url"].startswith("https://") else None
        meta = {"public": True, "url": url or f"https://{instance}/web/statuses/{st['id']}"}
        if st.get("quote"):
            meta["quote"] = True
        u = own_unit(source=self.name, ref=f"{instance}/{st['id']}", ts=ts, text=text,
                     kind="reply" if is_reply else "post", register="public", audience="public",
                     thread_id=hash_ref(f"{instance}:{reply_to if is_reply else st['id']}"),
                     audience_ref=audience_ref, context=context, meta=meta)
        if u is None:
            return []
        learned[st["id"]] = text_hash(text)
        return [u]

    def _parent_context(self, pid: str, instance: str, acct_id: str, pages: dict, budget: list) -> ContextLine | None:
        parent = pages.get(pid)
        if parent is None and budget[0] > 0 and _ID.match(pid):
            budget[0] -= 1
            try:
                data, _ = get_json(build_url(instance, f"/api/v1/statuses/{pid}"))
            except NetError as e:
                if e.kind == "rate_limit":
                    budget[0] = 0
                return None
            parent = data if isinstance(data, dict) else None
        if not isinstance(parent, dict) or parent.get("visibility") not in KEEP_VISIBILITY:
            return None
        content = parent.get("content")
        pa = parent.get("account") if isinstance(parent.get("account"), dict) else {}
        if not isinstance(content, str):
            return None
        text = context_text(html_to_text(content, False, parent.get("emojis")))
        if not text:
            return None
        if pa.get("id") == acct_id:
            return context_line("me", text)
        names = {n: "other1" for n in (pa.get("display_name"), pa.get("username"), pa.get("acct")) if isinstance(n, str)}
        return context_line("other1", text, names)
