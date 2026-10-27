"""Registry of harness config roots learned from hook payloads (`hook-roots.json` in the data directory).

Claude Code and Codex hand every hook the path of the session transcript (`transcript_path`). From that path the
config root can be derived:

- Claude Code: `<root>/projects/<slug>/<session>.jsonl`  ->  `<root>`   (the directory above `projects`)
- Codex:       `<root>/sessions/YYYY/MM/DD/rollout-*.jsonl`  ->  `<root>`   (the directory above `sessions`)

A session started with a throwaway `CLAUDE_CONFIG_DIR` / `CODEX_HOME` (an orchestrator, a script, a container)
keeps its history in a place no default or `[claude_code].dirs` entry knows about. Recording the root here lets
the adapters read it on the next sync. The adapters call `registered(harness)` (read-only, never raises); only the
hooks call `record(...)`.

File format: `{"v": 1, "roots": {"<harness>": {"<abs root>": <last seen, unix seconds>}}}`.

- Written atomically (temp file in the same directory, fsync, `os.replace`), mode 0600.
- Bounded: at most `MAX_ROOTS` roots per harness (the least recently seen go first), paths of at most `MAX_PATH`
  characters, and a registry file larger than `MAX_FILE_BYTES` is ignored (and rewritten from scratch on the next
  `record`).
- Pruned on every `record`: a root that does not exist and was last seen more than `PRUNE_DAYS` ago is dropped.
  Roots that exist are kept however old (they are cheap to read; the cap bounds them).
- A registered root is used only when the current user owns it and neither its group nor others can write to it
  (`usable_root`): anyone who could write there could plant a transcript that mannerism would then read as the user's
  own words. An unusable root is skipped silently, like a vanished one.
- Two hooks racing can lose one update (read-modify-write without a lock). That is harmless: the next hook event of
  the same session records the root again.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from . import core

REGISTRY_NAME = "hook-roots.json"
MAX_ROOTS = 64
MAX_PATH = 1024
MAX_FILE_BYTES = 256 * 1024
PRUNE_DAYS = 30

# harness -> (directory name above the transcript files, how many path parts lie between that directory and the file)
LAYOUTS = {
    "claude_code": ("projects", 2),  # projects/<slug>/<session>.jsonl
    "codex": ("sessions", 4),  # sessions/YYYY/MM/DD/rollout-*.jsonl
}


def registry_path() -> Path:
    return core.home() / REGISTRY_NAME


def root_from_transcript(harness: str, transcript_path) -> Path | None:
    """The config root a transcript path belongs to, or None when the path does not have the harness's layout.

    Pure path arithmetic: nothing is read, and the path need not exist."""
    layout = LAYOUTS.get(harness)
    if layout is None or not isinstance(transcript_path, str):
        return None
    if not transcript_path or len(transcript_path) > MAX_PATH or "\x00" in transcript_path:
        return None
    p = Path(transcript_path).expanduser()
    if not p.is_absolute() or p.suffix != ".jsonl":
        return None
    marker, depth = layout
    parts = p.parts
    # parts = (/, ..., <root>, <marker>, <depth-1 dirs...>, <file>): the marker sits `depth + 1` from the end
    if len(parts) < depth + 3 or parts[-(depth + 1)] != marker:
        return None
    if harness == "codex" and not p.name.startswith("rollout-"):
        return None
    root = Path(*parts[: -(depth + 1)])
    return root if str(root) != root.anchor else None  # never "/" itself


def identify(transcript_path) -> tuple[str, Path] | None:
    """(harness, config root) for a transcript path whose layout matches a known harness, else None."""
    for harness in LAYOUTS:
        root = root_from_transcript(harness, transcript_path)
        if root is not None:
            return harness, root
    return None


def _load(path: Path) -> dict[str, dict[str, int]]:
    """The registry as {harness: {root: last seen}}. A bad entry (a NaN or huge number, a path that is not text) is
    dropped on its own; it never spoils the others or raises."""
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return {}
        data = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    roots = data.get("roots") if isinstance(data, dict) else None
    out: dict[str, dict[str, int]] = {}
    if not isinstance(roots, dict):
        return out
    for harness, entries in roots.items():
        if harness not in LAYOUTS or not isinstance(entries, dict):
            continue
        clean: dict[str, int] = {}
        for p, t in entries.items():
            try:
                if (isinstance(p, str) and 0 < len(p) <= MAX_PATH and isinstance(t, int | float)
                        and not isinstance(t, bool)):
                    clean[p] = int(t)  # int(nan) / int(inf) raise: that one entry is dropped
            except (ValueError, OverflowError):
                continue
        out[harness] = clean
    return out


def usable_root(d: Path) -> bool:
    """A directory mannerism may read transcripts from: it exists, is owned by the current user and cannot be written by
    the group or by others. (Without `getuid`, on Windows, the ownership check is skipped.)"""
    try:
        st = d.stat()
    except OSError:
        return False
    if not d.is_dir():
        return False
    if hasattr(os, "getuid") and (st.st_uid != os.getuid() or st.st_mode & 0o022):
        return False
    return True


def standing_dirs(harness: str, configured=()) -> list[Path]:
    """The default config directory of the harness and the ones the user listed in `[<harness>].dirs`: the roots
    ordinary syncs read anyway. (Not the CLAUDE_CONFIG_DIR / CODEX_HOME of the calling process: inside a hook that is
    the session's own, possibly throwaway, directory.)"""
    default = {"claude_code": ".claude", "codex": ".codex"}.get(harness)
    out = [Path.home() / default] if default else []
    out += [Path(str(x)).expanduser() for x in configured or []]
    return out


def is_standing_root(harness: str, root: Path, configured=()) -> bool:
    """True when `root` is a default or configured config directory (see `standing_dirs`)."""
    try:
        key = root.resolve()
        return any(d.resolve() == key for d in standing_dirs(harness, configured))
    except (OSError, RuntimeError):
        return False


def registered(harness: str) -> list[Path]:
    """Usable registered roots for `harness`, most recently seen first. Read-only; never raises or creates.
    A root that vanished, is not owned by the user or is writable by others is skipped silently."""
    try:
        entries = _load(registry_path()).get(harness, {})
        out = []
        for p, _seen in sorted(entries.items(), key=lambda kv: kv[1], reverse=True):
            d = Path(p)
            if d.is_absolute() and usable_root(d):
                out.append(d)
        return out
    except Exception:  # noqa: BLE001 - discovery must never fail because of this file
        return []


def _write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".hook-roots-", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            os.fchmod(f.fileno(), 0o600)
            json.dump(data, f, separators=(",", ":"), sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _prune(entries: dict[str, int], now: float) -> dict[str, int]:
    cutoff = now - PRUNE_DAYS * 86400
    kept = {p: t for p, t in entries.items() if t >= cutoff or os.path.isdir(p)}
    if len(kept) > MAX_ROOTS:
        newest = sorted(kept.items(), key=lambda kv: kv[1], reverse=True)[:MAX_ROOTS]
        kept = dict(newest)
    return kept


def record(harness: str, transcript_path, now: float | None = None) -> Path | None:
    """Remember the config root of `transcript_path`. Returns the root, or None when the path is not a usable
    transcript path (wrong layout, root not an existing directory) and nothing was written.

    Skips the write when the registry already holds the root and the entry is under a day old, so a busy session
    does not rewrite the file on every prompt."""
    root = root_from_transcript(harness, transcript_path)
    if root is None or not root.is_dir():
        return None
    now = time.time() if now is None else now
    path = registry_path()
    data = _load(path)
    entries = data.get(harness, {})
    key = str(root)
    previous = entries.get(key)
    if previous is not None and now - previous < 86400 and previous <= now:
        return root
    entries[key] = int(now)
    data[harness] = _prune(entries, now)
    for other in list(data):
        if other != harness:
            data[other] = _prune(data[other], now)
    _write_atomic(path, {"v": 1, "roots": data})
    return root
