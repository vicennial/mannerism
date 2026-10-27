"""User configuration: `~/.mannerism/config.toml`.

Read with `tomllib`; written by a small serializer here (the standard library has no TOML
writer). The format is deliberately plain: tables of strings, ints, floats, bools and lists of
those, so a person can edit the file by hand and `init` can rewrite it without losing keys.

```toml
[me]
names = ["Jo Rivera"]
emails = ["jo@example.org"]
phone_suffixes = ["0123"]
[me.handles]
x = "jo.dev"

[sources.claude_code]
enabled = true
paths = []            # extra places this source should read

[sources.essays]          # your own files: a named instance of the built-in `files` adapter
adapter = "files"        # the table name is the source name; it must not be a built-in source or a plugin
path = "/home/jo/essays" # a file or a folder (watched on every sync)
register = "formal"      # optional; kind = "document" | "post" | "message" | "comment" (default document)
enabled = true
# Keys any source may have: allow_network (required, and set by `sources enable`, for a network adapter),
# interval_s (seconds between runs; overrides the adapter's default), run_on_session_start (may a session
# start launch a background sync for it when due; default: on for free network adapters, off for plugins and
# paid ones), plugin / plugin_dist / plugin_version (set by `sources enable` for an installed plugin).

[contacts]
work = ["<hash_ref of a work contact>"]

[lint]
tell_threshold = 0.02 # a tell fires only if your own rate of it in the register is at most this

[[exclude]]              # text that must never be stored; fields combine with AND (see exclude.py)
account = "~/.claude-work*"   # glob on the config / home dir the text came from ("~" = your home)
project = "scratch-*"         # glob on the working directory's name or path
source = "claude_code"        # exact source name
kind = "headless"             # how the session was driven (interactive, ide, ...)
text = '^\\[nightly\\]'        # regular expression on the text (single quotes: no TOML escapes)
group = "g_0123456789ab"      # a group id from `mannerism scan`
```

Unknown tables are kept in `Config.extra` and written back unchanged.
"""
from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import core
from .core import Me

DEFAULT_TELL_THRESHOLD = 0.02


@dataclass
class Config:
    me: Me = field(default_factory=Me)
    sources: dict[str, dict] = field(default_factory=dict)  # name -> {"enabled": bool, "paths": [..], ...}
    contacts_work: list[str] = field(default_factory=list)  # hashed refs of contacts treated as `work`
    lint: dict = field(default_factory=dict)
    exclude: list[dict] = field(default_factory=list)  # [[exclude]] tables: user-written, never edited by the code
    extra: dict = field(default_factory=dict)  # every other top-level table, round-tripped untouched

    @property
    def tell_threshold(self) -> float:
        try:
            return float(self.lint.get("tell_threshold", DEFAULT_TELL_THRESHOLD))
        except (TypeError, ValueError):
            return DEFAULT_TELL_THRESHOLD

    def enabled_sources(self) -> list[str]:
        return [n for n, s in self.sources.items() if s.get("enabled")]

    def to_dict(self) -> dict:
        me = {k: v for k, v in (("names", self.me.names), ("emails", self.me.emails),
                                ("phone_suffixes", self.me.phone_suffixes)) if v}
        if self.me.handles:
            me["handles"] = dict(self.me.handles)
        out: dict = {"me": me, "sources": {n: dict(s) for n, s in self.sources.items()}}
        if self.contacts_work:
            out["contacts"] = {"work": list(self.contacts_work)}
        if self.lint:
            out["lint"] = dict(self.lint)
        if self.exclude:
            out["exclude"] = [dict(e) for e in self.exclude]
        out.update(self.extra)
        return out

    def for_adapter(self, name: str) -> dict:
        """The `config` argument handed to `Source.discover`.

        Adapters are written independently of this module, so the dict offers every
        reasonable spelling: all parsed tables (`cfg["contacts"]["work"]`, `cfg["git"]["roots"]`)
        plus the keys of `[sources.<name>]` and of a `[<name>]` table flattened to the top level
        (`cfg.get("paths")`, `cfg.get("roots")`)."""
        raw = self.to_dict()
        flat = dict(raw.get(name) if isinstance(raw.get(name), dict) else {})
        flat.update(self.sources.get(name, {}))
        return {**raw, **flat, "_name": name, "work_contacts": list(self.contacts_work)}


def config_path() -> Path:
    return core.home() / "config.toml"


def ensure_home() -> Path:
    """Create the data directory with mode 0700 (and tighten an existing one)."""
    os.umask(0o077)  # every file mannerism creates (db sidecars, temp copies, cards, stats, pages) is owner-only
    h = core.home()
    h.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(h, 0o700)
    except OSError:
        pass
    return h


def from_dict(data: dict) -> Config:
    me_t = data.get("me") or {}
    me = Me(names=[str(x) for x in me_t.get("names", [])], emails=[str(x) for x in me_t.get("emails", [])],
            handles={str(k): str(v) for k, v in (me_t.get("handles") or {}).items()},
            phone_suffixes=[str(x) for x in me_t.get("phone_suffixes", [])])
    sources = {str(k): dict(v) for k, v in (data.get("sources") or {}).items() if isinstance(v, dict)}
    known = {"me", "sources", "contacts", "lint", "exclude"}
    excl = data.get("exclude")
    return Config(me=me, sources=sources, contacts_work=[str(x) for x in (data.get("contacts") or {}).get("work", [])],
                  lint=dict(data.get("lint") or {}),
                  exclude=[dict(e) for e in excl if isinstance(e, dict)] if isinstance(excl, list) else [], extra={k: v for k, v in data.items() if k not in known})


def load_config(path: Path | None = None) -> Config:
    """The saved config, or an empty one when the file is missing or unreadable."""
    p = path or config_path()
    try:
        return from_dict(tomllib.loads(p.read_text(encoding="utf-8")))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return Config()


def save_config(cfg: Config, path: Path | None = None) -> Path:
    p = path or config_path()
    ensure_home()
    tmp = p.with_name(p.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(dumps(cfg.to_dict()))
    os.replace(tmp, p)
    os.chmod(p, 0o600)
    return p


REPO_IDENTITY_CAP = 25  # root finding (c): bound how many repos' local config we probe, each call has its own timeout


def identity_guess() -> Me:
    """A starting identity from `git config --global`, unioned with the REPO-LOCAL `user.name`/`user.email` of
    discovered repos (root finding (c): plenty of machines never set a global git identity at all, but every
    repo they actually commit to has its own `user.email` -- still the user's own config, never a commit
    author, which this never reads). Never raises; each `git config` call has its own 2 s timeout."""
    me = Me()

    def git(args: list[str], cwd: str | None = None) -> str:
        try:
            out = subprocess.run(["git", "config", *args], capture_output=True, text=True, timeout=2,
                                 check=False, cwd=cwd)
            return out.stdout.strip() if out.returncode == 0 else ""
        except (OSError, subprocess.SubprocessError, ValueError):
            return ""

    name, email = git(["--global", "user.name"]), git(["--global", "user.email"])
    if name:
        me.names.append(name)
    if email and "@" in email:
        me.emails.append(email)
    try:
        from .adapters.git import Git

        repos = Git().discover(Me(), {})[:REPO_IDENTITY_CAP]
    except Exception:  # noqa: BLE001 - identity_guess() never raises
        repos = []
    for loc in repos:
        n, e = git(["user.name"], cwd=str(loc.path)), git(["user.email"], cwd=str(loc.path))
        if n and n not in me.names:
            me.names.append(n)
        if e and "@" in e and e not in me.emails:
            me.emails.append(e)
    return me


# --- TOML writer ---------------------------------------------------------------------------------------------------

_BARE = re.compile(r"^[A-Za-z0-9_-]+$")
_ESC = {'"': '\\"', "\\": "\\\\", "\n": "\\n", "\t": "\\t", "\r": "\\r"}


def _key(k: str) -> str:
    return k if _BARE.match(k) else _string(k)


def _string(s: str) -> str:
    out = []
    for ch in s:
        if ch in _ESC:
            out.append(_ESC[ch])
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise TypeError(f"cannot write {type(v).__name__} to TOML")


def dumps(data: dict) -> str:
    """Serialize nested dicts to TOML: scalars and lists first, then sub-tables with dotted headers."""
    lines: list[str] = []

    def is_tables(v) -> bool:
        return isinstance(v, list) and bool(v) and all(isinstance(x, dict) for x in v)

    def table(path: list[str], d: dict) -> None:
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict) and not is_tables(v)}
        arrays = {k: v for k, v in d.items() if is_tables(v)}
        subs = {k: v for k, v in d.items() if isinstance(v, dict)}
        if path and (scalars or not (subs or arrays)):
            lines.append("")
            lines.append("[" + ".".join(_key(p) for p in path) + "]")
        for k, v in scalars.items():
            lines.append(f"{_key(k)} = {_value(v)}")
        for k, v in subs.items():
            table([*path, k], v)
        for k, items in arrays.items():  # arrays of tables: [[path.k]], one block per item (scalar values only)
            for item in items:
                lines.append("")
                lines.append("[[" + ".".join(_key(p) for p in [*path, k]) + "]]")
                for ik, iv in item.items():
                    lines.append(f"{_key(ik)} = {_value(iv)}")

    table([], data)
    return "\n".join(lines).lstrip("\n") + "\n"
