import subprocess

from mannerism.adapters.git import Git, clean_message

from .conftest import run, write_jsonl


def commit(repo, name, email, msg, date="2026-03-01T10:00:00+0000"):
    env = {
        "GIT_AUTHOR_DATE": date,
        "GIT_COMMITTER_DATE": date,
        "HOME": str(repo),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            f"user.name={name}",
            "-c",
            f"user.email={email}",
            "commit",
            "--allow-empty",
            "-q",
            "-m",
            msg,
        ],
        check=True,
        env=env,
    )


def make_repo(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    commit(
        repo,
        "Jo Dev",
        "JO.dev@example.org",
        "Fix the parser so it handles empty input\n\nThe loop never ended.\n\n"
        "Co-Authored-By: Someone <s@example.org>\nSigned-off-by: Jo <jo.dev@example.org>",
    )
    commit(
        repo, "Sam Other", "sam@example.org", "Add caching layer for the registry lookups", "2026-03-02T10:00:00+0000"
    )
    commit(repo, "Jo Dev", "jo.dev@example.org", "Bump version to 1.2.3", "2026-03-03T10:00:00+0000")
    commit(repo, "Jo Dev", "jo.dev@example.org", "wip", "2026-03-04T10:00:00+0000")
    commit(
        repo, "Jo Dev", "jo.dev@example.org", "Rename helpers for clarity across modules", "2026-03-05T10:00:00+0000"
    )
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "feat"], check=True)
    commit(repo, "Jo Dev", "jo.dev@example.org", "Side work on the branch only", "2026-03-06T10:00:00+0000")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=Jo Dev",
            "-c",
            "user.email=jo.dev@example.org",
            "merge",
            "--no-ff",
            "-q",
            "feat",
            "-m",
            "Merge branch feat",
        ],
        check=True,
    )
    return repo


def test_git_only_own_commits(home, me, tmp_path):
    repo = make_repo(tmp_path)
    src = Git()
    locs = src.discover(me, {"roots": [str(repo)]})
    assert [lc.path for lc in locs] == [repo]
    units, cur = run(src, locs[0], None, me)
    texts = sorted(u.text for u in units)
    assert texts == [
        "Fix the parser so it handles empty input\n\nThe loop never ended.",
        "Rename helpers for clarity across modules",
        "Side work on the branch only",
    ]
    u = next(x for x in units if x.text.startswith("Fix"))
    assert (u.kind, u.audience, u.register) == ("commit", "colleague", "work")
    assert u.meta["repo_hash"] and str(repo) not in str(u.meta) and u.ts.tzinfo is not None
    # incremental: same dedupe by sha, nothing new
    again, cur2 = run(src, locs[0], cur, me)
    assert {x.unit_id for x in again} <= {x.unit_id for x in units}
    assert cur2["last_committer_ts"] >= cur["last_committer_ts"]


def test_git_requires_emails_and_discovers_from_transcripts(home, me, tmp_path):
    from mannerism.core import Me

    repo = make_repo(tmp_path)
    sub = repo / "pkg"
    sub.mkdir()
    write_jsonl(
        home / ".claude" / "projects" / "-p" / "s.jsonl",
        [{"type": "user", "cwd": str(sub), "message": {"role": "user", "content": "hi"}}],
    )
    write_jsonl(
        home / ".codex" / "sessions" / "2026" / "03" / "01" / "rollout-a.jsonl",
        [{"type": "session_meta", "payload": {"id": "x", "cwd": str(tmp_path / "gone")}}],
    )
    (home / "code" / "other").mkdir(parents=True)
    (home / "code" / "other" / ".git").mkdir()
    src = Git()
    locs = src.discover(me, {})
    assert {lc.path.name for lc in locs} == {"proj", "other"}
    units, _ = src.read(next(lc for lc in locs if lc.path.name == "proj"), None, Me())
    assert list(units) == []


def test_clean_message_trailers():
    assert (
        clean_message("Do the thing\n\nBody text.\n\nChange-Id: I123\nCo-Authored-By: A <a@x.org>\n")
        == "Do the thing\n\nBody text."
    )


def test_git_discover_never_raises(home, me, monkeypatch):
    assert Git().discover(me, {"roots": [None, 5]}) == []
    assert Git().discover(me, None) == []


def test_git_skips_ai_coauthored_commits_unless_opted_in(home, me, tmp_path):
    """P1-3 / decision 4: an agent-identity co-author trailer (or 'Generated with') makes a commit skipped
    by default; [git].include_ai_commits restores it, flagged with meta.ai so the bank still never learns
    from it (see test_bank's equivalent coverage of the exclusion itself)."""
    repo = tmp_path / "proj"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    commit(
        repo, "Jo Dev", "jo.dev@example.org",
        "Add retry logic to the sync client\n\nCo-Authored-By: Claude <noreply@anthropic.com>",
    )
    commit(
        repo, "Jo Dev", "jo.dev@example.org", "Write the changelog for this release by hand",
        "2026-03-02T10:00:00+0000",
    )
    src = Git()
    loc = src.discover(me, {"roots": [str(repo)]})[0]
    units, _ = run(src, loc, None, me)
    assert [u.text for u in units] == ["Write the changelog for this release by hand"]
    src2 = Git()
    loc2 = src2.discover(me, {"roots": [str(repo)], "include_ai_commits": True})[0]
    units2, _ = run(src2, loc2, None, me)
    assert {u.text for u in units2} == {
        "Add retry logic to the sync client",
        "Write the changelog for this release by hand",
    }
    ai_unit = next(u for u in units2 if u.text.startswith("Add retry"))
    assert ai_unit.meta.get("ai") is True
