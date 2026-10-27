"""Agent-harness hooks: `mannerism hook session-start|prompt|stop|session-end`.

All are fire-and-forget. They print nothing, make no network calls, never raise and always exit 0, so a
broken or half-installed mannerism can never get in the way of the user's session or prompt:

- not set up yet (no `config.toml` in the data directory): return at once, touching nothing;
- `session-start`: read the harness JSON (bounded), remember the config root of its `transcript_path` (see
  `hookroots`), then start a detached quick sync;
- `prompt`: read the harness JSON (`{"prompt": "...", "cwd": "...", "transcript_path": "..."}`; Claude Code and Codex
  both use `prompt`) and let `capture.match_prompt` record the pair when the prompt contains an edited version of a
  recent draft. The exclusion rules apply: a prompt that a rule matches (by what the payload tells: the harness and
  config directory from `transcript_path`, the project from `cwd`, the text) is never paired;
- `stop` (fires after every completed response): only for a transcript under a root the ordinary syncs do not read
  (not the default directory, not one in `[<source>].dirs`: a throwaway config directory), remember the root and start a
  detached, time-bounded file sync that ingests and screens that one transcript and does nothing else (no bank,
  statistics, capture or work queue: the store is marked "refresh pending" for the next normal sync). It is the event
  that fires reliably (every turn, including the last of a headless `claude -p` run, and it does not depend on a clean
  exit), so a throwaway directory's prompts are stored before it disappears;
- `session-end` (fires when the session exits; Stop does not fire on user interrupts): the same file sync for any
  root, plus one bounded refresh (the child is killed after 60 s and the pending mark stays).

Hook payload fields used (Claude Code https://code.claude.com/docs/en/hooks "Common input fields"; Codex
https://developers.openai.com/codex/hooks): `transcript_path` (string; Codex: string or null), `cwd` and `prompt`. The
path layout (`.../projects/<slug>/<id>.jsonl` or `.../sessions/YYYY/MM/DD/rollout-*.jsonl`) tells the harness apart.
Nothing else in the payload is read, stored or logged.
"""
from __future__ import annotations

import json
import os
import select
import sys
import time

READ_DEADLINE_S = 1.0
READ_MAX_BYTES = 1_000_000
BUSY_MS = 250  # how long the prompt hook waits for a locked database before giving up


def is_set_up() -> bool:
    """True when `init` has run. Looks only at the data directory; creates nothing."""
    from . import core
    try:
        return (core.home() / "config.toml").is_file()
    except OSError:
        return False


def read_stdin(max_bytes: int = READ_MAX_BYTES, deadline_s: float = READ_DEADLINE_S) -> bytes:
    """Whatever the harness piped in, without ever hanging: stops at EOF, `max_bytes` or the deadline.

    A terminal (someone ran the hook by hand) yields b"" at once."""
    try:
        fd = sys.stdin.fileno()
        if os.isatty(fd):
            return b""
    except (AttributeError, ValueError, OSError):
        return b""
    end = time.monotonic() + deadline_s
    chunks: list[bytes] = []
    total = 0
    while total < max_bytes:
        left = end - time.monotonic()
        if left <= 0:
            break
        try:
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                break
            chunk = os.read(fd, min(65536, max_bytes - total))
        except (OSError, ValueError):
            break
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def prompt_from_payload(raw: bytes) -> str:
    """The user's prompt text from a hook payload, or "" when it is missing or malformed."""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return ""
    if isinstance(data, dict):
        value = data.get("prompt")
        return value if isinstance(value, str) else ""
    return ""


def transcript_from_payload(raw: bytes) -> str | None:
    """`transcript_path` from a hook payload, or None when it is missing, null or not a string."""
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    if isinstance(data, dict):
        value = data.get("transcript_path")
        if isinstance(value, str) and value:
            return value
    return None


def _payload(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _identify(raw: bytes):
    """(harness, transcript path, root) when the payload's transcript has a known layout and the harness's source is
    switched on, else None. Reads the config; writes nothing."""
    from . import config, hookroots
    path = transcript_from_payload(raw)
    ident = hookroots.identify(path) if path else None
    if ident is None:
        return None
    harness, root = ident
    cfg = config.load_config()
    if harness not in cfg.enabled_sources():
        return None
    return harness, path, root, cfg


def _learn_root(raw: bytes):
    """Record the config root of the payload's transcript. Returns (harness, transcript path, root, config) when the
    source is enabled and the layout known, else None. Touches nothing when the source is switched off. A registry
    that cannot be written (a full disk, a read-only home) does not stop the caller: the answer is the same, only the
    root is not remembered."""
    from . import hookroots
    found = _identify(raw)
    if found is None:
        return None
    harness, path, _root, _cfg = found
    try:
        hookroots.record(harness, path)
    except Exception:  # noqa: BLE001
        pass
    return found


def session_start() -> None:
    raw = read_stdin()  # drained either way so the harness never blocks on a full pipe
    if not is_set_up():
        return
    from . import sync
    if sync.forget_all_pending():  # decision 1: stay quiet after `forget --all` until an explicit sync
        return
    try:
        _learn_root(raw)  # before the sync starts, so it already reads a throwaway root
    except Exception:  # noqa: BLE001 - never let this stop the sync below
        pass
    sync.sync_in_background(quick=True)


def stop() -> None:
    """`stop`: after every turn. A transcript under a root the ordinary syncs already read (the default directory or one
    in `[<source>].dirs`) needs nothing, so the hook returns at once. A transcript under any other root (a throwaway
    config directory a script or orchestrator made) is stored now by a detached file sync that ingests and screens
    only (`sync(files=...)` without `refresh`): the bank and statistics wait for the next normal sync or the
    session end, so a turn costs a few milliseconds of reading, never a rebuild."""
    _end_of_session("stop")


def session_end() -> None:
    """`session-end`: the same file sync once more when the session exits, with one bounded refresh, so the bank and
    statistics know the session's texts without waiting for the next sync. Any root, as it fires once per session."""
    _end_of_session("session-end")


def _end_of_session(event: str) -> None:
    raw = read_stdin()
    if not is_set_up():
        return
    from . import hookroots, sync
    if sync.forget_all_pending():
        return
    found = _learn_root(raw)
    if found is None:
        return
    harness, path, root, cfg = found
    if event == "stop" and hookroots.is_standing_root(harness, root, (cfg.sources.get(harness) or {}).get("dirs")):
        return
    if not os.path.isfile(path):
        return
    sync.sync_in_background(quick=True, sources=[harness], files=[path], refresh=event == "session-end")


def prompt() -> None:
    if not is_set_up():
        return
    raw = read_stdin()
    text = prompt_from_payload(raw)
    if not text.strip():
        return
    from . import capture, store
    data = _payload(raw)
    transcript = transcript_from_payload(raw)
    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else None
    con = store.connect()
    try:
        con.execute(f"PRAGMA busy_timeout={BUSY_MS}")  # a hook must not wait the 3 s a normal command may
        capture.match_prompt(text, con=con, cwd=cwd, transcript_path=transcript)
    finally:
        con.close()


def run(event: str) -> int:
    """Entry point used by the CLI. Always returns 0 and prints nothing."""
    try:
        {"session-start": session_start, "prompt": prompt, "stop": stop,
         "session-end": session_end}[event]()
    except BaseException:  # noqa: BLE001 - a hook must never fail the user's session
        pass
    return 0
