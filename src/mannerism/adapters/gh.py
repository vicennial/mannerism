"""GitHub activity through the `gh` CLI: the user's recent public events.

Opt-in (tier `export`): discovery only checks that `gh` is on PATH; network calls happen in `read`, once enabled.
Uses `gh api user` for the login and `gh api /users/LOGIN/events` (public events: issue comments, PR review comments, PR
reviews with a body, opened issues and PRs). GitHub keeps only the latest ~300 events (about 90 days), so the first sync
is shallow; later syncs are incremental by event id. Quoted `>` lines, HTML comments and @mentions are removed.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterable

from ..core import Location, Me, Unit, hash_ref
from ._util import own_unit, parse_iso

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_MENTION = re.compile(r"(?<![\w/])@[A-Za-z0-9][A-Za-z0-9-]{0,38}")
_PAGES = 3


def _run(args: list[str], timeout: float) -> str | None:
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    return r.stdout if r.returncode == 0 else None


def clean_body(body) -> str:
    if not isinstance(body, str):
        return ""
    body = _HTML_COMMENT.sub("", body)
    body = "\n".join(ln for ln in body.replace("\r\n", "\n").split("\n") if not ln.lstrip().startswith(">"))
    body = _MENTION.sub("@[user]", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def event_texts(ev: dict):
    """Yield (ref, created_at, body, kind_hint) for the event types we keep."""
    typ, pl = ev.get("type"), ev.get("payload") or {}
    created = ev.get("created_at")
    if typ == "IssueCommentEvent" and pl.get("action") == "created":
        c = pl.get("comment") or {}
        yield f"comment:{c.get('id')}", c.get("created_at") or created, c.get("body")
    elif typ == "PullRequestReviewCommentEvent" and pl.get("action", "created") == "created":
        c = pl.get("comment") or {}
        yield f"reviewcomment:{c.get('id')}", c.get("created_at") or created, c.get("body")
    elif typ == "PullRequestReviewEvent":
        r = pl.get("review") or {}
        yield f"review:{r.get('id')}", r.get("submitted_at") or created, r.get("body")
    elif typ == "IssuesEvent" and pl.get("action") == "opened":
        i = pl.get("issue") or {}
        yield f"issue:{i.get('id')}", i.get("created_at") or created, i.get("body")
    elif typ == "PullRequestEvent" and pl.get("action") == "opened":
        p = pl.get("pull_request") or {}
        yield f"pr:{p.get('id')}", p.get("created_at") or created, p.get("body")


class GitHub:
    name = "gh"
    tier = "export"  # opt-in: setup never enables it on its own

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            if shutil.which("gh") is None:  # no process is started and no network call is made here
                return []
            return [Location(self.name, "gh:events", None, "GitHub CLI (opt-in; public events of the signed-in user)")]
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            login = cursor.get("login") or me.handles.get("github") or me.handles.get("gh")
            if not login:
                out = _run(["api", "user"], 15)
                login = json.loads(out).get("login") if out else None
            if not login:
                return [], cursor
            last_id = int(cursor.get("last_event_id") or 0)
            max_id, units = last_id, []
            for page in range(1, _PAGES + 1):
                out = _run(["api", f"/users/{login}/events?per_page=100&page={page}"], 30)
                events = json.loads(out) if out else []
                if not isinstance(events, list) or not events:
                    break
                stop = False
                for ev in events:
                    if not isinstance(ev, dict):
                        continue
                    try:
                        eid = int(ev.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if eid <= last_id:
                        stop = True
                        continue
                    max_id = max(max_id, eid)
                    repo = (ev.get("repo") or {}).get("name") or ""
                    for ref, created, body in event_texts(ev):
                        ts = parse_iso(created) or parse_iso(ev.get("created_at"))
                        text = clean_body(body)
                        if ts is None or not text:
                            continue
                        u = own_unit(
                            source=self.name,
                            ref=ref,
                            ts=ts,
                            text=text,
                            kind="comment",
                            register="work",
                            audience="colleague",
                            thread_id=hash_ref(repo),
                            audience_ref=hash_ref(repo),
                            meta={"event": ev.get("type")},
                        )
                        if u is not None:
                            units.append(u)
                if stop:
                    break
            return units, {"login": login, "last_event_id": max_id}
        except Exception:  # noqa: BLE001
            return [], cursor
