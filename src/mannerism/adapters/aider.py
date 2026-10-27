"""Aider chat history: `.aider.chat.history.md` in the git root (or the folder aider ran in).

Where: aider writes one Markdown file per repository and appends to it for ever (`--chat-history-file` or
$AIDER_CHAT_HISTORY_FILE moves it). Nothing records where the files are, so they are looked for in the git repos mannerism
already knows (the `git` adapter's discovery: folders the Claude Code / Codex transcripts ran in, plus ~/code, ~/src,
~/projects, ~/work, ~/dev, ~/repos), the transcripts' own working folders (aider outside a git repo writes into the
current folder), ~/, $AIDER_CHAT_HISTORY_FILE and `[aider].paths` (a file, or a folder searched two levels deep).

Format (aider/io.py): `# aider chat started at YYYY-MM-DD HH:MM:SS` opens a session (local time, no zone: this is the only
time in the file, so every prompt of a session gets it); a typed prompt is one or more consecutive `#### ` lines, each
ending in two spaces; `> ` lines are aider's own output; any other line is the model's reply. Aider's replies can contain
`#### ` headings, but those never end in two spaces, so they are not mistaken for prompts.

Own text = the `#### ` blocks. Dropped: slash commands and `!shell` lines (`/ask why?` is logged twice, once as the
command and once as the question it passes on; the question is kept), confirmation questions, and `<blank>`.
The model's last reply (masked, clipped) becomes the agent context line.

Aider also logs text it generated through the same `io.user_input` path, as `#### ` lines (`Coder.run(with_message=...)`
logs whatever it is given). These are not typed and are dropped:
- the architect's plan, which `ArchitectCoder` feeds to the editor model (the default `--auto-accept-architect`): a block
  that equals the model reply that came right before it, or holds at least 80% of its letters and sits inside it (a
  prompt that merely quotes a suggestion stays); only a summary of the reply (sizes, hashes, a 600-letter masked prefix)
  is kept between reads,
- `/lint` output: blocks that start with `# Fix any errors below, if possible.`,
- `/help`: the question plus the retrieved docs (`# Question: ...` / `# Relevant docs:`).
The other callers log what the user gave them (`--message`, `--message-file`, `/ask` `/code` `/architect` `/context`
arguments), and auto-lint / auto-test fixes are sent without being logged. Histories that git tracks in their own
repository (found by directory search, not named by $AIDER_CHAT_HISTORY_FILE or a `[aider].paths` file) came with the
clone and are somebody else's: they are skipped, and so is any such file when git cannot answer (no git, "dubious
ownership", a timeout): fail closed.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path

from ..core import Location, Me, Unit, context_line, hash_ref
from ._util import (
    clip_tail,
    configured_paths,
    is_relay,
    mask_pii,
    own_unit,
    read_new_lines,
    resolved_key,
    transcript_cwds,
)
from .git import Git

HISTORY_FILE = ".aider.chat.history.md"
_SESSION = re.compile(r"^# aider chat started at (\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s*$")
_CONFIRM = re.compile(r"\(Y\)es/\(N\)o")
ASSISTANT_KEEP = 600  # characters of the model's reply carried between reads (masked)
PREFIX_KEEP = 600  # letters (masked) of the model's reply carried between reads, next to its length and hashes
PLAN_MIN = 24  # a block shorter than this (letters) is never taken for the architect's plan
PLAN_COVER = 0.8  # a block counts as the plan when it equals the reply or holds this share of its letters
_GENERATED = ("# Fix any errors below, if possible.",)
_NOT_WORD = re.compile(r"\W+")
GIT_TIMEOUT_S = 5


def _norm(text: str) -> str:
    return _NOT_WORD.sub("", text).lower()


_GIT_VARS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY", "GIT_NAMESPACE")


def tracked_by_git(path: Path) -> bool | None:
    """True when the file is committed to the git repository it sits in, False when git answered that it is not
    (untracked, or no repository there), None when git could not answer (missing git, a timeout, "dubious ownership"...).
    Cheap, bounded, never raises."""
    env = {k: v for k, v in os.environ.items() if k not in _GIT_VARS}
    env.update(GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    try:
        r = subprocess.run(
            ["git", "-C", str(path.parent), "ls-files", "--error-unmatch", "--", path.name],
            capture_output=True,
            timeout=GIT_TIMEOUT_S,
            check=False,
            env=env,
        )
    except (subprocess.SubprocessError, OSError, ValueError):
        return None
    if r.returncode == 0:
        return True
    err = r.stderr.decode("utf-8", "replace")
    if r.returncode == 1 and "did not match" in err:
        return False
    if r.returncode == 128 and "not a git repository" in err:
        return False
    return None


def reply_summary(main: str, whole: str) -> dict:
    """What is kept of the model's reply between reads: its sizes, hashes and a short prefix (masked letters)."""
    return {
        "len_main": len(main),
        "len_all": len(whole),
        "prefix": whole[:PREFIX_KEEP],
        "h_main": hashlib.sha1(main.encode()).hexdigest(),
        "h_all": hashlib.sha1(whole.encode()).hexdigest(),
    }


def repeats_reply(flat: str, info: dict) -> bool:
    """True when a prompt block (masked, letters only) is the model reply that came right before it, as ArchitectCoder
    hands the plan to the editor model: it equals the reply, or holds at least PLAN_COVER of its letters (`> ` lines
    left out) and sits inside it. `info` is the whole reply ("main" without `> ` lines, "all"), or, when the reply was
    read in an earlier call, its summary: hashes for an exact match, else length and prefix."""
    if len(flat) < PLAN_MIN:
        return False
    if "all" in info:
        main, whole = info["main"], info["all"]
        return flat in (main, whole) or (bool(main) and len(flat) >= PLAN_COVER * len(main) and flat in whole)
    if hashlib.sha1(flat.encode()).hexdigest() in (info.get("h_main"), info.get("h_all")):
        return True
    n = min(200, len(info["prefix"]), len(flat))
    return (
        info["len_main"] > 0
        and PLAN_COVER * info["len_main"] <= len(flat) <= info["len_all"] * 1.05
        and flat[:n] == info["prefix"][:n]
    )


def _candidates(me: Me, config: dict) -> list[tuple[Path, bool]]:
    """(file, named by the user) pairs. A file the user named (env var, a file in `[aider].paths`) is theirs; the others
    were found by looking in folders."""
    dirs: list[Path] = [Path.home()]
    try:
        dirs += [loc.path for loc in Git().discover(me, {}) if loc.path is not None]
    except Exception:  # noqa: BLE001
        pass
    dirs += [Path(c) for c in transcript_cwds(200, 20)]
    files = [(d / HISTORY_FILE, False) for d in dirs]
    env = os.environ.get("AIDER_CHAT_HISTORY_FILE")
    if env:
        files.append((Path(env).expanduser(), True))
    for p in configured_paths(config):
        if p.is_dir():
            files += [(f, False) for f in (p / HISTORY_FILE, *p.glob(f"*/{HISTORY_FILE}"), *p.glob(f"*/*/{HISTORY_FILE}"))]
        else:
            files.append((p, True))
    return files


def session_start(stamp: str) -> datetime:
    """The header time is the local wall clock of the machine aider ran on."""
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").astimezone()


def generated_block(text: str) -> bool:
    """True for blocks aider logged from its own generated messages (lint output, /help context)."""
    return text.startswith(_GENERATED) or (text.startswith("# Question:") and "# Relevant docs:" in text)


def own_prompt_text(lines: list[str]) -> str:
    text = "\n".join(lines).strip()
    if not text or text == "<blank>" or text[0] in "/!" or _CONFIRM.search(text) or generated_block(text):
        return ""
    return text


class Aider:
    name = "aider"
    tier = "auto"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            out, seen = [], set()
            for p, named in _candidates(me, config):
                try:
                    key = resolved_key(p)
                    if key in seen or not p.is_file():
                        continue
                    if not named and tracked_by_git(p) is not False:
                        continue  # shipped with a cloned repository (somebody else's), or git could not tell
                except OSError:
                    continue
                seen.add(key)
                out.append(Location(self.name, key, p, f"Aider history in {Path(key).parent.name}"))
            return sorted(out, key=lambda loc: loc.key)
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        try:
            lines, new = read_new_lines(loc.path, cursor)
        except OSError:
            return [], cursor
        resumed = bool(cursor) and new["start"] == int(cursor.get("offset", 0))
        stamp = cursor.get("session") if resumed else None
        n = int(cursor.get("n", 0)) if resumed else 0
        asst = cursor.get("asst") or "" if resumed else ""  # the model's reply so far: masked, clipped
        block: list[str] = list(cursor.get("block", [])) if resumed else []  # a prompt cut by the read budget
        pending: list[str] = []  # reply lines not yet folded into `asst`
        carry = cursor.get("reply") if resumed and isinstance(cursor.get("reply"), dict) else None  # summary
        since: list[str] = []  # reply lines (every kind) since the last prompt block, or since this read began
        plan_info = cursor.get("plan_ref") if resumed and isinstance(cursor.get("plan_ref"), dict) else None
        path_hash = hash_ref(str(loc.path))
        repo_hash = hash_ref(str(loc.path.parent))
        units: list[Unit] = []

        def fold() -> str:
            if pending:
                return clip_tail(mask_pii(" ".join([asst, *pending])), ASSISTANT_KEEP)
            return asst

        def reply_info() -> dict:
            """The reply since the last prompt block: its text if all of it is in this read, else a summary."""
            whole = _norm(mask_pii("\n".join(since)))
            main = _norm(mask_pii("\n".join(ln for ln in since if not ln.startswith("> "))))
            if carry is None:
                return {"main": main, "all": whole}
            merged = dict(carry)
            merged["len_main"] += len(main)
            merged["len_all"] += len(whole)
            if len(merged["prefix"]) < PREFIX_KEEP:
                merged["prefix"] = (merged["prefix"] + whole)[:PREFIX_KEEP]
            if since:
                merged["h_main"] = merged["h_all"] = None  # the hashes no longer cover the whole reply
            return merged

        def summary(info: dict) -> dict:
            return reply_summary(info["main"], info["all"]) if "all" in info else info

        def is_plan(text: str) -> bool:
            return plan_info is not None and repeats_reply(_norm(mask_pii(text)), plan_info)

        def emit() -> str:
            """Close the open prompt block; returns the reply context to carry on."""
            nonlocal n, block
            lines_, block = block, []
            n += 1
            text = own_prompt_text(lines_)
            if not text or is_plan(text) or is_relay(text):
                return asst
            if stamp:
                ts = session_start(stamp)
            else:
                try:
                    ts = datetime.fromtimestamp(loc.path.stat().st_mtime).astimezone()
                except OSError:
                    return asst
            ctx = [context_line("agent", asst)] if asst else []
            u = own_unit(
                source=self.name,
                ref=f"{path_hash}:{stamp or 'nohead'}:{n}",
                ts=ts,
                text=text,
                kind="prompt",
                register="agent",
                audience="agent",
                thread_id=hash_ref(f"{loc.path}:{stamp}"),
                context=ctx,
                meta={"repo_hash": repo_hash},
            )
            if u is not None:
                units.append(u)
            return ""

        for raw in lines:
            line = raw.rstrip("\r\n")
            m = _SESSION.match(line)
            user = line.startswith("#### ") and line.endswith("  ")
            if user:
                if not block:
                    asst, pending = fold(), []
                    plan_info, carry, since = reply_info(), None, []
                block.append(line[5:].rstrip())
                continue
            if block:
                asst = emit()
            if m:
                stamp, asst, pending = m.group(1), "", []
                carry, since, plan_info = None, [], None
                n = 0
                continue
            if line.strip():
                since.append(line)
            if line.startswith(("> ", "# ")):
                continue
            if line.strip():
                pending.append(line.strip())
        asst = fold()
        if block and not new["more"]:
            asst = emit()
        new.update(
            session=stamp,
            n=n,
            asst=asst,
            block=[mask_pii(b) for b in block] if block else [],
            reply=summary(reply_info()) if (since or carry) else None,
            plan_ref=summary(plan_info) if block and plan_info else None,
        )
        return units, new
