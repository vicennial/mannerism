"""Exclusion rules: text that must never be stored, and a way to remove what already is.

A rule matches a unit when ALL of its fields match (an AND-combination):

- `source`: the source name, exactly (`claude_code`);
- `account`: a glob on the config / home directory the text came from, as stored: the path with `$HOME` shown as
  `~` (`~/.claude-work*`). The expanded path (`/home/jo/.claude-work*`) matches too;
- `project`: a glob on the working directory's basename (`app-*`) or its path (`~/work/*`);
- `session_kind`: how the adapter says the session was driven (`interactive`, `ide`, ...), case-insensitive;
- `text`: a Python regular expression searched in the unit's text;
- `group`: a group id from `mannerism scan` / the review page (`groups.py`).

A unit that lacks the metadata a rule asks for (read by an older version, or by a source that does not know it) does
not match that rule. A rule with no field at all is rejected: it would match everything.

Where rules come from: `[[exclude]]` tables in `config.toml` (the user writes them; the code never edits them) and the
`exclusions` table in the store (written by `exclude add` and the review page). Both apply. Sync checks every unit
against them before it is stored, so a matching unit never enters the store (the cursor still moves on, so removing a
rule later does not bring back text that was skipped). Adding a rule, `forget --where` and a changed `[[exclude]]`
config delete what is stored already, with everything derived from it, the way `forget --source` does.

Regular expressions come from the user but run on every unit, so they are guarded: a length limit, no nested
quantifiers (`(a+)+`), a smoke test against a hostile input, matching only the first TEXT_SCAN_CHARS characters, and a
per-match time limit in an isolated worker. A timeout excludes new text, but it never authorizes deletion of stored
text.
"""
from __future__ import annotations

import atexit
import fnmatch
import glob
import hashlib
import json
import os
import re
import select
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from . import groups, store
from .core import REGISTERS

RULE_FIELDS = ("source", "account", "project", "session_kind", "text", "group")
FIELD_ALIASES = {"kind": "session_kind", "grp": "group"}
MAX_PATTERN_CHARS = 500
MAX_GLOB_CHARS = 300
TEXT_SCAN_CHARS = 20_000
REGEX_TIMEOUT_S = 0.25  # per match, enforced by the worker process (no signals, so it works in any thread)
REGEX_START_S = 5.0  # how long the worker may take to start
REGEX_POISON_AFTER = 5  # a pattern that timed out this often in one process is not run again (counts as a match)
MAX_RULES = 500

STATS = {"regex_timeouts": 0}  # process-wide count of text matches that timed out (reported by sync)
_POISONED: dict[str, int] = {}

try:  # the parser's node types, for the static check (3.11+: `re._parser`)
    from re import _constants as _sre_c  # type: ignore[attr-defined]
    from re import _parser as _sre_parse  # type: ignore[attr-defined]
except ImportError:  # pragma: no cover - older layout
    _sre_parse = _sre_c = None

_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d*,\d*\})")


class RuleError(ValueError):
    """A rule that cannot be used (empty, bad regular expression, unknown group id ...)."""


# --- running a regular expression safely ------------------------------------------------------------------------------
#
# A user-written pattern runs on every text, so a pathological one must not hang a sync, a hook or the MCP server.
# Signals cannot do this safely (a process has one SIGALRM / interval timer and other code, such as the detached
# sync's hard time limit, owns it; and a handler only runs on the main thread). So the match runs in a small worker
# process: the parent sends `[pattern, text]`, waits at most REGEX_TIMEOUT_S for the answer and kills the worker when it
# does not come. A timed-out match counts as a MATCH at ingest (the text is excluded: fail closed), so a pattern that
# cannot be evaluated never lets text through. A pattern that keeps timing out is not run again in this process.
# If a worker cannot be started, matching fails closed at ingest and fails open for destructive stored-text scans.
# It never falls back to an unbounded match in the caller process.

_WORKER_SRC = r"""
import json, re, signal, sys

class MatchTimeout(Exception):
    pass

def alarm(_signum, _frame):
    raise MatchTimeout()

if hasattr(signal, "setitimer"):
    signal.signal(signal.SIGALRM, alarm)

cache = {}
out = sys.stdout.buffer
out.write(b"ready\n"); out.flush()
for line in sys.stdin.buffer:
    try:
        pat, text = json.loads(line)
        rx = cache.get(pat)
        if rx is None:
            if len(cache) > 256:
                cache.clear()
            rx = cache[pat] = re.compile(pat)
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0.20)
        res = b"1\n" if rx.search(text) else b"0\n"
    except MatchTimeout:
        res = b"t\n"
    except Exception:
        res = b"e\n"
    finally:
        if hasattr(signal, "setitimer"):
            signal.setitimer(signal.ITIMER_REAL, 0)
    out.write(res); out.flush()
"""


class _Worker:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.broken = sys.platform == "win32"  # no select() on pipes there: run in process
        self.buf = b""

    def _read_line(self, timeout: float) -> bytes | None:
        fd = self.proc.stdout.fileno()
        end = time.monotonic() + timeout
        while b"\n" not in self.buf:
            left = end - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                return None
            chunk = os.read(fd, 4096)
            if not chunk:
                return None
            self.buf += chunk
        line, _, self.buf = self.buf.partition(b"\n")
        return line

    def stop(self) -> None:
        proc, self.proc, self.buf = self.proc, None, b""
        if proc is None:
            return
        try:
            proc.kill()
        except OSError:
            pass
        for f in (proc.stdin, proc.stdout):
            try:
                f.close()
            except OSError:
                pass
        try:
            proc.wait(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass

    def _start(self) -> bool:
        try:
            self.proc = subprocess.Popen([sys.executable, "-I", "-S", "-c", _WORKER_SRC], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, close_fds=True)
        except OSError:
            self.broken = True
            return False
        if self._read_line(REGEX_START_S) != b"ready":
            self.stop()
            self.broken = True
            return False
        return True

    def search(self, pattern: str, text: str) -> bool | None:
        """True / False, or None when the match timed out (or the worker is unusable and `broken` is set)."""
        with self.lock:
            for attempt in range(2):
                if self.broken:
                    return None
                if self.proc is not None and self.proc.poll() is not None:
                    self.stop()
                if self.proc is None and not self._start():
                    return None
                try:
                    self.proc.stdin.write(json.dumps([pattern, text]).encode("ascii") + b"\n")
                    self.proc.stdin.flush()
                    line = self._read_line(REGEX_TIMEOUT_S)
                except (OSError, ValueError):
                    line = None
                if line is None:
                    died = self.proc.poll() is not None
                    self.stop()
                    if died and attempt == 0:
                        continue  # a temporary creator thread exited; restart without calling it a regex timeout
                    return None
                if line == b"t":
                    return None
                if line == b"e":
                    return False
                return line == b"1"
            return None


_WORKER = _Worker()
atexit.register(_WORKER.stop)


def _search(pattern: str, text: str, timeout_result: bool = True) -> bool:
    """Does `pattern` match inside the first TEXT_SCAN_CHARS characters of `text`? Never hangs.

    A match that runs out of time counts as `timeout_result` (True at ingest: the text is excluded; False for the
    retroactive delete, which must not remove stored text because a pattern was slow)."""
    text = text[:TEXT_SCAN_CHARS]
    if _POISONED.get(pattern, 0) >= REGEX_POISON_AFTER:
        return timeout_result
    res = _WORKER.search(pattern, text)
    if res is not None:
        return res
    STATS["regex_timeouts"] += 1
    _POISONED[pattern] = _POISONED.get(pattern, 0) + 1
    return timeout_result


@lru_cache(maxsize=256)
def _compile(pattern: str) -> re.Pattern:
    return re.compile(pattern)


def _static_problem(pattern: str) -> str | None:
    """Why `pattern` can run for ever, from its parse tree alone (deterministic: no timing), else None.

    Refused: a repeat that can run many times around another repeat (`(a+)+`, `(\\s*\\w+)*`), and an unbounded repeat around an
    alternation whose branches can start with the same character (`(a|aa)+`)."""
    if _sre_parse is None:
        return "nested quantifiers like (a+)+ can run for ever; rewrite the pattern" if _NESTED_QUANTIFIER.search(pattern) \
            else None
    try:
        tree = _sre_parse.parse(pattern)
    except (re.error, RecursionError, OverflowError):
        return None  # compile() reports it
    repeats = tuple(getattr(_sre_c, n) for n in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT") if hasattr(_sre_c, n))
    unbounded = _sre_c.MAXREPEAT
    nested = "nested quantifiers like (a+)+ can run for ever; rewrite the pattern"

    def distinct_starts(alts) -> bool:
        firsts = []
        for alt in alts:
            if not alt or alt[0][0] != _sre_c.LITERAL:
                return False
            firsts.append(alt[0][1])
        return len(set(firsts)) == len(firsts)

    def walk(nodes, in_many: bool, in_unbounded: bool) -> str | None:
        for op, av in nodes:
            bad = None
            if op in repeats:
                _lo, hi, sub = av
                many, unb = hi > 1, hi == unbounded
                if (many and in_unbounded) or (unb and in_many):
                    return nested
                bad = walk(sub, in_many or many, in_unbounded or unb)
            elif op == _sre_c.BRANCH:
                if in_unbounded and not distinct_starts(av[1]):
                    return "an alternation like (a|aa)+ inside a repeat can run for ever; rewrite the pattern"
                for alt in av[1]:
                    bad = bad or walk(alt, in_many, in_unbounded)
            elif op == _sre_c.SUBPATTERN:
                bad = walk(av[3], in_many, in_unbounded)
            elif op in (_sre_c.ASSERT, _sre_c.ASSERT_NOT):
                bad = walk(av[1], in_many, in_unbounded)
            elif hasattr(_sre_c, "ATOMIC_GROUP") and op == _sre_c.ATOMIC_GROUP:
                bad = walk(av, in_many, in_unbounded)
            if bad:
                return bad
        return None

    return walk(tree, False, False)


def check_pattern(pattern: str) -> re.Pattern:
    """Compile a `text` rule's regular expression or raise RuleError.

    The checks are static and deterministic (the same answer on every machine and every run): length, a valid
    expression, and no repeat shapes that can run for ever (see `_static_problem`). Run time is guarded separately
    when the pattern is used (`_search`)."""
    if not isinstance(pattern, str) or not pattern:
        raise RuleError("text: give a regular expression")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise RuleError(f"text: pattern is longer than {MAX_PATTERN_CHARS} characters")
    try:
        rx = _compile(pattern)
    except re.error as e:
        raise RuleError(f"text: not a valid regular expression ({e})") from None
    problem = _static_problem(pattern)
    if problem:
        raise RuleError(f"text: {problem}")
    return rx


def _expand_home(label: str) -> str:
    if label == "~" or label.startswith("~/"):
        return str(Path.home()) + label[1:]
    return label


def glob_escape(label: str) -> str:
    """`label` as a glob that matches only itself. Masked labels contain `[secret]` or `[email]`, which a glob reads as a
    character class, so a label shown as a rule argument is escaped first (`[` becomes `[[]`)."""
    return glob.escape(label)


def _glob(pattern: str, value: str | None) -> bool:
    if not value:
        return False
    if pattern == value:  # a label copied as it is shown names itself, whatever it contains
        return True
    return fnmatch.fnmatchcase(value, pattern) or fnmatch.fnmatchcase(_expand_home(value), pattern) \
        or fnmatch.fnmatchcase(value, _expand_home(pattern))


@dataclass
class Facts:
    """What a rule can look at for one unit (a unit being ingested, or a stored row)."""
    source: str
    text: str = ""
    account: str | None = None
    project: str | None = None
    project_path: str | None = None
    session_kind: str | None = None
    template: str | None = None
    _groups: tuple[str, ...] | None = field(default=None, repr=False)

    @property
    def group_ids(self) -> tuple[str, ...]:
        if self._groups is None:
            self._groups = groups.group_ids(self.source, self.account, self.project, self.session_kind, self.template)
        return self._groups


def facts_of_unit(u) -> Facts:
    return Facts(u.source, u.text, u.account, u.project, u.project_path, u.session_kind, u.template)


@dataclass(frozen=True)
class Rule:
    source: str | None = None
    account: str | None = None
    project: str | None = None
    session_kind: str | None = None
    text: str | None = None
    group: str | None = None
    rule_id: int | None = None
    origin: str = "store"  # "config" (a [[exclude]] table) or "store" (exclude add / the review page)

    def criteria(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in RULE_FIELDS if getattr(self, f)}

    def describe(self) -> str:
        return " ".join(f"{'kind' if k == 'session_kind' else k}={v!r}" for k, v in self.criteria().items())

    def matches(self, f: Facts, timeout_result: bool = True) -> bool:
        """Does this rule match? `timeout_result`: what a `text` match that ran out of time counts as (see `_search`)."""
        if self.source and f.source.casefold() != self.source.casefold():
            return False
        if self.session_kind and (f.session_kind or "").casefold() != self.session_kind.casefold():
            return False
        if self.account and not _glob(self.account, f.account):
            return False
        if self.project and not (_glob(self.project, f.project) or _glob(self.project, f.project_path)):
            return False
        if self.group and self.group not in f.group_ids:
            return False
        if self.text and not _search(self.text, f.text, timeout_result):
            return False
        return True

    @property
    def needs_text(self) -> bool:
        return bool(self.text)


def make_rule(*, source=None, account=None, project=None, session_kind=None, kind=None, text=None, group=None,
              rule_id=None, origin="store") -> Rule:
    """Validate and build a rule. `kind` is the CLI spelling of `session_kind`. Raises RuleError."""
    if kind and session_kind and kind != session_kind:
        raise RuleError("give kind or session_kind, not both")
    vals = {"source": source, "account": account, "project": project, "session_kind": session_kind or kind,
            "text": text, "group": group}
    for k, v in vals.items():
        if v is not None and not isinstance(v, str):
            raise RuleError(f"{k}: must be text")
    vals = {k: (v.strip() if k != "text" else v) for k, v in vals.items() if v is not None}
    vals = {k: v for k, v in vals.items() if v != ""}
    if not vals:
        raise RuleError("a rule needs at least one of: source, account, project, kind, text, group "
                        "(an empty rule would match everything)")
    for k in ("account", "project"):
        if k in vals and len(vals[k]) > MAX_GLOB_CHARS:
            raise RuleError(f"{k}: longer than {MAX_GLOB_CHARS} characters")
    if "text" in vals:
        check_pattern(vals["text"])
    if "group" in vals and not groups.is_group_id(vals["group"]):
        raise RuleError(f"group: {vals['group']!r} is not a group id (they look like g_0123456789ab; see `mannerism scan`)")
    return Rule(rule_id=rule_id, origin=origin, **vals)


# --- where rules come from -------------------------------------------------------------------------------------------

def config_rules(cfg) -> tuple[list[Rule], list[str]]:
    """The `[[exclude]]` tables of config.toml as rules, and a message for each one that cannot be used."""
    rules, errors = [], []
    for i, raw in enumerate(getattr(cfg, "exclude", None) or [], 1):
        if not isinstance(raw, dict):
            errors.append(f"[[exclude]] #{i}: not a table")
            continue
        unknown = sorted(set(raw) - set(RULE_FIELDS) - set(FIELD_ALIASES))
        if unknown:
            errors.append(f"[[exclude]] #{i}: unknown key(s) {', '.join(unknown)}")
            continue
        fields = {FIELD_ALIASES.get(k, k): v for k, v in raw.items()}
        try:
            rules.append(make_rule(**fields, origin="config"))
        except RuleError as e:
            errors.append(f"[[exclude]] #{i}: {e}")
    return rules[:MAX_RULES], errors


def config_signature(rules: list[Rule]) -> str:
    """A fingerprint of the config rules: sync deletes retroactively when it changes."""
    if not rules:
        return ""
    body = json.dumps(sorted(json.dumps(r.criteria(), sort_keys=True) for r in rules))
    return hashlib.sha1(body.encode()).hexdigest()[:16]


def store_rules_checked(con: sqlite3.Connection) -> tuple[list[Rule], list[str]]:
    """The stored rules that can be used, and a message for each one that cannot (a regular expression that no longer
    compiles or is refused by the static check, a field that is not text). A broken rule is skipped, never raised."""
    out, errors = [], []
    for r in con.execute("SELECT rule_id, source, account, project, session_kind, text, grp FROM exclusions "
                         "ORDER BY rule_id"):
        try:
            rule = make_rule(source=r["source"], account=r["account"], project=r["project"],
                             session_kind=r["session_kind"], text=r["text"], group=r["grp"], rule_id=r["rule_id"],
                             origin="store")
        except RuleError as e:
            errors.append(f"stored exclusion rule {r['rule_id']} is skipped: {e}")
            continue
        out.append(rule)
    return out, errors


def store_rules(con: sqlite3.Connection) -> list[Rule]:
    return store_rules_checked(con)[0]


def all_rules_checked(con: sqlite3.Connection | None, cfg) -> tuple[list[Rule], list[str]]:
    """Every rule in force (config.toml first, then the store) and the messages for the ones that were skipped."""
    rules, errors = config_rules(cfg)
    if con is not None:
        stored, bad = store_rules_checked(con)
        rules, errors = rules + stored, errors + bad
    return rules, errors


def all_rules(con: sqlite3.Connection | None, cfg) -> list[Rule]:
    return all_rules_checked(con, cfg)[0]


def add_store_rule(con: sqlite3.Connection, rule: Rule) -> tuple[int, bool]:
    """Store `rule`; returns (rule_id, created). An identical rule is not stored twice."""
    crit = rule.criteria()
    where = " AND ".join(f"{('grp' if k == 'group' else k)}=?" for k in crit)
    others = " AND ".join(f"{c} IS NULL" for c in ("source", "account", "project", "session_kind", "text", "grp")
                          if c not in {('grp' if k == 'group' else k) for k in crit})
    row = con.execute(f"SELECT rule_id FROM exclusions WHERE {where}" + (f" AND {others}" if others else ""),
                      list(crit.values())).fetchone()
    if row:
        return row[0], False
    if con.execute("SELECT count(*) FROM exclusions").fetchone()[0] >= MAX_RULES:
        raise RuleError(f"at most {MAX_RULES} stored rules; remove some with `exclude rm`")
    with store.tx(con):
        cur = con.execute(
            "INSERT INTO exclusions(source, account, project, session_kind, text, grp, created_at) VALUES (?,?,?,?,?,?,?)",
            (rule.source, rule.account, rule.project, rule.session_kind, rule.text, rule.group, store.now_iso()))
    return cur.lastrowid, True


def remove_store_rule(con: sqlite3.Connection, rule_id: int) -> bool:
    with store.tx(con):
        return con.execute("DELETE FROM exclusions WHERE rule_id=?", (int(rule_id),)).rowcount > 0


# --- ingest ----------------------------------------------------------------------------------------------------------

def first_match(rules: list[Rule], facts: Facts, timeout_result: bool = True) -> Rule | None:
    for r in rules:
        if r.matches(facts, timeout_result):
            return r
    return None


def filter_units(units: list, rules: list[Rule]) -> tuple[list, list]:
    """(kept, excluded) units. The caller passes units whose `template` is already set when group rules may refer
    to template clusters."""
    if not rules:
        return units, []
    kept, out = [], []
    for u in units:
        (out if first_match(rules, facts_of_unit(u)) else kept).append(u)
    return kept, out


# --- stored units ----------------------------------------------------------------------------------------------------

_ROW_COLUMNS = "unit_id, source, account, project, project_path, session_kind, template"


def matching_unit_ids(con: sqlite3.Connection, rules: list[Rule]) -> list[str]:
    """Ids of the stored units any of `rules` matches (read-only)."""
    if not rules:
        return []
    need_text = any(r.needs_text for r in rules)
    cols = _ROW_COLUMNS + (", text" if need_text else "")
    sources = {r.source.casefold() for r in rules if r.source}
    # every rule names a source: only those sources' rows can match
    where, args = "", []
    if all(r.source for r in rules):
        where = f"WHERE lower(source) IN ({','.join('?' * len(sources))})"
        args = sorted(sources)
    ids = []
    cur = con.execute(f"SELECT {cols} FROM units {where}", args)
    while True:
        rows = cur.fetchmany(2000)
        if not rows:
            break
        for r in rows:
            f = Facts(r["source"], r["text"] if need_text else "", r["account"], r["project"], r["project_path"],
                      r["session_kind"], r["template"])
            if first_match(rules, f, timeout_result=False):  # slow pattern: never delete stored text because of it
                ids.append(r["unit_id"])
    return ids


def delete_matching(con: sqlite3.Connection, rules: list[Rule], *, dry_run: bool = False) -> dict:
    """Delete (or with `dry_run` only count) the stored units `rules` match, and everything derived from them.

    The caller holds the sync lock and refreshes what the deletion invalidated (`finish_delete`).
    Returns {"matched": n, "ids": [...], "registers": set, "sources": set, "by_source": {...}}."""
    ids = matching_unit_ids(con, rules)
    by_source: dict[str, int] = {}
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        for r in con.execute(f"SELECT source, count(*) FROM units WHERE unit_id IN ({','.join('?' * len(part))}) "
                             f"GROUP BY source", part):
            by_source[r[0]] = by_source.get(r[0], 0) + r[1]
    out = {"matched": len(ids), "ids": [], "registers": set(), "sources": set(by_source), "by_source": by_source}
    if dry_run or not ids:
        return out
    with store.tx(con):
        gone = store.delete_unit_rows(con, ids)
    out.update(ids=gone["ids"], registers=set(gone["registers"]))
    return out


def finish_delete(con: sqlite3.Connection, cfg, deleted: dict) -> dict:
    """Rebuild what deleted units fed: agent cards built on their sources are dropped, banks, stats and stats cards
    are rebuilt for the registers they were in, the leak index and the work queue follow. Returns a short report."""
    from . import packets, refresh, sync

    if not deleted.get("ids"):
        return {"stale_cards": [], "registers": []}
    stale = sync._invalidate_after_delete(con, set(deleted["sources"]))
    regs = sorted((set(deleted["registers"]) & set(REGISTERS)) | set(stale))
    refresh.refresh(con, regs or None, cfg.me)
    refresh.maybe_build_leak(con, force=True)
    packets.queue_work(con, cfg.me)
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return {"stale_cards": stale, "registers": regs}


def apply_config_rules(con: sqlite3.Connection, cfg) -> dict | None:
    """When the `[[exclude]]` tables changed since the last time, delete what they match from the store.

    Returns the `delete_matching` result when it deleted something (the caller refreshes), else None."""
    rules, _errors = config_rules(cfg)
    sig = config_signature(rules)
    if sig == (store.get_meta(con, "exclude_config_sig") or ""):
        return None
    res = delete_matching(con, rules) if rules else None
    store.set_meta(con, "exclude_config_sig", sig or None)
    return res if res and res["ids"] else None


# --- the operations the CLI, the API and the review page share ---------------------------------------------------------

def _mixed_refusal(con: sqlite3.Connection, rule: Rule, force: bool) -> str | None:
    """Why a rule that is only a group id must not run: the group is mixed (see `groups.is_mixed`)."""
    if force or set(rule.criteria()) != {"group"}:
        return None
    return groups.MIXED_MESSAGE.format(gid=rule.group) if groups.is_mixed(con, rule.group) else None


def add_rule_and_delete(rule: Rule, *, dry_run: bool = False, force: bool = False,
                        expected_units: int | None = None) -> dict:
    """Store `rule`, then delete the units it matches. With `dry_run`: store nothing, delete nothing, count only.
    A rule that is only a mixed group is refused unless `force` (a dry run reports it as `mixed` instead).

    Takes the sync lock (a sync never runs beside a deletion) and its own connection. Returns
    {"ok", "rule_id", "created", "removed", "by_source", "stale_cards"} or an error dict."""
    from . import config as config_mod
    from . import sync

    lock = sync.acquire_lock()
    if lock is None:
        return {"ok": False, "error": "could not take the sync lock within 10s (a sync is running); try again shortly"}
    try:
        cfg = config_mod.load_config()
        if dry_run and not store.db_path().exists():
            return {"ok": True, "dry_run": True, "rule": rule.describe(), "would_remove": 0, "by_source": {}}
        con = store.connect()
        try:
            refusal = _mixed_refusal(con, rule, force)
            if dry_run:
                return {**_dry_run(con, rule), **({"mixed": True, "warning": refusal} if refusal else {})}
            if refusal:
                return {"ok": False, "mixed": True, "error": refusal}
            if expected_units is not None and len(matching_unit_ids(con, [rule])) != expected_units:
                return {"ok": False, "error": "this group changed, refresh"}
            rule_id, created = _store_rule(con, rule)
            res = delete_matching(con, [replace(rule, rule_id=rule_id)])
            fin = finish_delete(con, cfg, res)
            return {"ok": True, "rule_id": rule_id, "created": created, "rule": rule.describe(),
                    "removed": len(res["ids"]), "by_source": res["by_source"], "stale_cards": fin["stale_cards"]}
        finally:
            con.close()
    finally:
        sync.release_lock(lock)


def _store_rule(con: sqlite3.Connection, rule: Rule) -> tuple[int, bool]:
    rule_id, created = add_store_rule(con, rule)
    if rule.group:  # "not me" supersedes an earlier "it's me" for the same group
        with store.tx(con):
            con.execute("DELETE FROM group_decisions WHERE group_id=?", (rule.group,))
    return rule_id, created


def _dry_run(con, rule: Rule) -> dict:
    res = delete_matching(con, [rule], dry_run=True)
    return {"ok": True, "dry_run": True, "rule": rule.describe(), "would_remove": res["matched"],
            "by_source": res["by_source"]}


def forget_where(rule: Rule, *, dry_run: bool = False, force: bool = False) -> dict:
    """Delete the stored units `rule` matches, and what derives from them, without storing a rule. A rule that is only
    a mixed group is refused unless `force`, as in `add_rule_and_delete`."""
    from . import config as config_mod
    from . import sync

    lock = sync.acquire_lock()
    if lock is None:
        return {"ok": False, "error": "could not take the sync lock within 10s (a sync is running); try again shortly"}
    try:
        cfg = config_mod.load_config()
        if dry_run and not store.db_path().exists():
            return {"ok": True, "dry_run": True, "rule": rule.describe(), "would_remove": 0, "by_source": {}}
        con = store.connect()
        try:
            refusal = _mixed_refusal(con, rule, force)
            if dry_run:
                return {**_dry_run(con, rule), **({"mixed": True, "warning": refusal} if refusal else {})}
            if refusal:
                return {"ok": False, "mixed": True, "error": refusal}
            res = delete_matching(con, [rule])
            fin = finish_delete(con, cfg, res)
            return {"ok": True, "rule": rule.describe(), "removed": len(res["ids"]), "by_source": res["by_source"],
                    "stale_cards": fin["stale_cards"]}
        finally:
            con.close()
    finally:
        sync.release_lock(lock)
