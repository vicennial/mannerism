import json
import subprocess

from mannerism.adapters import gh as ghmod
from mannerism.adapters.gh import GitHub, clean_body

from .conftest import run


def ev(i, typ, payload, repo="acme/widgets", created="2026-03-01T10:00:00Z"):
    return {"id": str(i), "type": typ, "repo": {"name": repo}, "created_at": created, "payload": payload}


EVENTS = [
    ev(
        30,
        "IssueCommentEvent",
        {
            "action": "created",
            "comment": {
                "id": 3,
                "body": "Looks good to me.\n> quoted text\n<!-- hidden -->\nping @someone-else, mail ana@example.org",
                "created_at": "2026-03-03T10:00:00Z",
            },
        },
    ),
    ev(
        29,
        "PullRequestReviewCommentEvent",
        {"action": "created", "comment": {"id": 2, "body": "nit: rename this please"}},
    ),
    ev(28, "PullRequestReviewEvent", {"review": {"id": 5, "body": None}}),
    ev(27, "PullRequestReviewEvent", {"review": {"id": 6, "body": "Approved after the fixes landed"}}),
    ev(26, "IssuesEvent", {"action": "opened", "issue": {"id": 7, "body": "The build fails on a clean checkout."}}),
    ev(25, "IssuesEvent", {"action": "closed", "issue": {"id": 8, "body": "ignored closed"}}),
    ev(24, "PullRequestEvent", {"action": "opened", "pull_request": {"id": 9, "body": "Adds the retry helper."}}),
    ev(23, "PushEvent", {"commits": []}),
    ev(22, "IssueCommentEvent", {"action": "edited", "comment": {"id": 4, "body": "edited, ignored"}}),
]


def fake_run(calls):
    def _run(cmd, **kw):
        calls.append(cmd)
        assert kw.get("timeout")
        joined = " ".join(cmd)
        if "auth status" in joined:
            return subprocess.CompletedProcess(cmd, 0, "ok", "")
        if joined.endswith("api user"):
            return subprocess.CompletedProcess(cmd, 0, json.dumps({"login": "jo-dev"}), "")
        if "&page=1" in joined:
            return subprocess.CompletedProcess(cmd, 0, json.dumps(EVENTS), "")
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    return _run


def test_gh_events(monkeypatch, me):
    calls = []
    monkeypatch.setattr(ghmod.shutil, "which", lambda n: "/usr/bin/gh")
    monkeypatch.setattr(ghmod.subprocess, "run", fake_run(calls))
    src = GitHub()
    locs = src.discover(me, {})
    assert len(locs) == 1
    units, cur = run(src, locs[0], None, me)
    assert sorted(u.text for u in units) == sorted(
        [
            "Looks good to me.\n\nping @[user], mail [email]",
            "nit: rename this please",
            "Approved after the fixes landed",
            "The build fails on a clean checkout.",
            "Adds the retry helper.",
        ]
    )
    assert all(u.kind == "comment" and u.audience == "colleague" and u.register == "work" for u in units)
    assert all("acme" not in (u.thread_id or "") and u.thread_id for u in units)
    assert cur == {"login": "jo-dev", "last_event_id": 30}
    # incremental: only newer events
    again, cur2 = run(src, locs[0], cur, me)
    assert again == [] and cur2["last_event_id"] == 30


def test_gh_is_opt_in_and_discover_makes_no_calls(monkeypatch, me):
    assert GitHub.tier == "export"
    monkeypatch.setattr(ghmod.shutil, "which", lambda n: None)
    assert GitHub().discover(me, {}) == []
    monkeypatch.setattr(ghmod.shutil, "which", lambda n: "/usr/bin/gh")

    def forbidden(cmd, **kw):
        raise AssertionError("discover must not start a process")

    monkeypatch.setattr(ghmod.subprocess, "run", forbidden)
    assert len(GitHub().discover(me, {})) == 1


def test_gh_read_failures_are_soft(monkeypatch, me):
    monkeypatch.setattr(ghmod.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "no"))
    units, cur = GitHub().read(ghmod.Location("gh", "gh:events"), {"x": 1}, me)
    assert list(units) == [] and cur == {"x": 1}

    def boom(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 3)

    monkeypatch.setattr(ghmod.subprocess, "run", boom)
    units, cur = GitHub().read(ghmod.Location("gh", "gh:events"), {"x": 1}, me)
    assert list(units) == [] and cur == {"x": 1}


def test_clean_body():
    assert clean_body("a\n> q\n\n\n\nb <!-- c -->") == "a\n\nb"
