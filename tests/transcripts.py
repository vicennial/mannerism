"""Synthetic Claude Code / Codex transcripts for tests that run the real adapters. Everything here is fictional."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

HUMAN = [
    "can you look at why the login test is flaky on CI? it fails about one in five runs",
    "I think we should split this module in two, the parsing is fine but the caching keeps growing",
    "hm, that is not what I meant. I want the retry to wrap the whole call, not just the send",
    "please rename the config loader so the names match the docs we wrote last week",
    "why does the build take four minutes now? it was one minute before the dependency bump",
    "lgtm, ship it",
    "fix the test",
    "ok go ahead and open the pull request, but keep the description short",
    "the second option, and drop the cache for now since nobody reads it",
    "no wait, undo that last change, the old name was better",
    "add a unit test for the empty-input case and run the whole suite again",
    "what does this error mean? mail me the trace at jo.dev@example.org if it is long",
]
BOILERPLATE = ("Continue working on ticket ABC-{n} following the plan in the ledger and report status when finished, "
               "keeping the commit small")
STATUS = "Status update\ntask: build-{n}\nstate: passed\nduration: 31s\nowner: ci-bot\nnext: deploy"


def _ts(base: datetime, minutes: float) -> str:
    return (base + timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_claude(root: Path, project_slug: str, sid: str, prompts: list[str], cwd: str, *, start: datetime,
                 entrypoint: str = "cli", gap_min: float = 5.0) -> Path:
    """`<root>/projects/<slug>/<sid>.jsonl` with an assistant line before every prompt."""
    p = root / "projects" / project_slug / f"{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for i, text in enumerate(prompts):
        t = i * gap_min
        lines.append({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
                      "timestamp": _ts(start, t - 0.5), "sessionId": sid, "uuid": f"{sid}-a{i}"})
        lines.append({"type": "user", "message": {"role": "user", "content": text}, "timestamp": _ts(start, t),
                      "sessionId": sid, "uuid": f"{sid}-u{i}", "cwd": cwd, "entrypoint": entrypoint})
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", "utf-8")
    return p


def write_codex(root: Path, sid: str, prompts: list[str], cwd: str, *, start: datetime, source: str = "cli") -> Path:
    p = root / "sessions" / f"{start:%Y}" / f"{start:%m}" / f"{start:%d}" / f"rollout-{start:%Y-%m-%dT%H-%M-%S}-{sid}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [{"timestamp": _ts(start, 0), "type": "session_meta", "payload": {"id": sid, "cwd": cwd, "source": source}}]
    for i, text in enumerate(prompts):
        lines.append({"timestamp": _ts(start, 1 + i * 5), "type": "event_msg",
                      "payload": {"type": "user_message", "message": text}})
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", "utf-8")
    return p


def world(home: Path) -> dict:
    """A machine with two Claude accounts and a Codex home. The default account holds a person's own projects; the
    second holds an orchestrator that sends the same ticket prompt and status blocks in many sessions. Returns paths."""
    start = datetime(2026, 5, 1, 9, tzinfo=UTC)
    a = home / ".claude"
    b = home / ".claude-work"
    for i in range(6):  # a person: varied prompts in two projects
        write_claude(a, "-home-jo-work-app", f"aaaaaaaa-0000-0000-0000-{i:012d}", HUMAN[i * 2:i * 2 + 2],
                     str(home / "work" / "app"), start=start + timedelta(days=i))
    for i in range(3):
        write_claude(a, "-home-jo-work-site", f"bbbbbbbb-0000-0000-0000-{i:012d}", HUMAN[i:i + 3],
                     str(home / "work" / "site"), start=start + timedelta(days=10 + i))
    for i in range(14):  # an orchestrator: the same prompt in every session
        write_claude(b, "-srv-orch-run", f"cccccccc-0000-0000-0000-{i:012d}",
                     [BOILERPLATE.format(n=100 + i), STATUS.format(n=i)], f"/srv/orch/run{i}",
                     start=start + timedelta(days=20 + i), gap_min=0.01)
    c = home / ".codex"
    for i in range(2):
        write_codex(c, f"dddddddd-0000-0000-0000-{i:012d}", HUMAN[4 + i:6 + i], str(home / "work" / "app"),
                    start=start + timedelta(days=40 + i))
    write_codex(c, "eeeeeeee-0000-0000-0000-000000000000", ["a headless run that must not be read at all"],
                str(home / "work" / "app"), start=start + timedelta(days=50), source="exec")
    return {"a": a, "b": b, "c": c, "start": start}
