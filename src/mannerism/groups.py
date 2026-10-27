"""Groups: the unit of the "Is this you?" question, and the read / write API the review page and the CLI share.

A group is a set of stored texts the user can answer for in one click. There are two kinds:

- an ORIGIN group: source x account x project x session kind (`g_` + 12 hex). "Claude Code, ~/.claude-work, project
  app, interactive";
- a TEMPLATE group: texts that open with the same words in many sessions (`t_` + the 12-hex template id of
  `humanness.template_of`). Only shown when the template appears in at least TEMPLATE_GROUP_MIN_SESSIONS sessions and
  TEMPLATE_GROUP_MIN_UNITS texts.

The ids are stable: they are a hash of the fields (origin) or the template id, so the id `mannerism scan` printed
for a group is the id of that group after `sync`, in `exclude add --group`, in a `[[exclude]] group = ...` rule, and
on the review page. A unit with no origin metadata (read by an older version) is in the origin group of its source
with empty account / project / kind. Such a group can hold texts from many places, so it is marked `"mixed": true`
(only when its texts were stored before origin tracking; a source whose adapter never gives an origin is not mixed):
`not_me` refuses a mixed group unless `force=True`, and `mannerism review rescan` fills the origin in again where the
source can still be read (`backfill_origins`), after which the texts move to groups of their own.

API for the review page (all functions return plain JSON-serialisable dicts):

- reads (take a connection; a read-only one is fine; they never write):
  `list_groups(con, samples=3)`  every group with counts, held counts, masked samples, held first;
  `group_detail(con, group_id, samples=10)`  one group;
  `review_state(con)`  `{groups, held_total, decisions, rules}` in one call.
- writes (open their own short-lived connection and take the sync lock; they refresh the bank):
  `not_me(group_id, force=False)`  adds a store exclusion rule for the group and deletes its texts and what derives
  from them (refused for a mixed group unless `force`);
  `its_me(group_id)`  releases the group's held texts into normal use and remembers the decision, so later texts of
  the group are never held.
Both return `{"ok": True, ...}` or `{"ok": False, "error": ...}`.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3

from . import store
from .core import REGISTERS, mask_pii

TEMPLATE_GROUP_MIN_SESSIONS = 3
TEMPLATE_GROUP_MIN_UNITS = 5
SAMPLE_CHARS = 200
MAX_SAMPLES = 20

LEGACY_LIKE = '%"legacy": true%'  # score_json of a text `review rescan` scored while it still had no origin

_GROUP_ID = re.compile(r"[gt]_[0-9a-f]{12}")


def is_group_id(value: str) -> bool:
    return bool(_GROUP_ID.fullmatch(value or ""))  # fullmatch: "$" would accept a trailing newline


def origin_group_id(source: str, account: str | None = None, project: str | None = None,
                    session_kind: str | None = None) -> str:
    """Stable id of the origin group a unit with these fields belongs to."""
    key = "\x1f".join((source or "", account or "", project or "", session_kind or ""))
    return "g_" + hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:12]


def template_group_id(template: str) -> str:
    return "t_" + template


def group_ids(source: str, account: str | None, project: str | None, session_kind: str | None,
              template: str | None = None) -> tuple[str, ...]:
    """Every group id a unit belongs to: its origin group, plus its template group when it has a template."""
    ids = (origin_group_id(source, account, project, session_kind),)
    return ids + (template_group_id(template),) if template else ids


def group_ids_of_row(row) -> tuple[str, ...]:
    """`group_ids` for a mapping with source / account / project / session_kind / template keys (a stored row)."""
    return group_ids(row["source"], row["account"], row["project"], row["session_kind"], row["template"])


def group_ids_of_dict(row: dict) -> tuple[str, ...]:
    """`group_ids` for a plain dict with the same keys (missing keys count as empty)."""
    return group_ids(row["source"], row.get("account"), row.get("project"), row.get("session_kind"), row.get("template"))


def kept_groups(con: sqlite3.Connection) -> set[str]:
    """Group ids the user confirmed as theirs ("it's me")."""
    try:
        return {r[0] for r in con.execute("SELECT group_id FROM group_decisions WHERE decision='keep'")}
    except sqlite3.OperationalError:  # a store from before v4
        return set()


# --- reads -----------------------------------------------------------------------------------------------------------

def mask_sample(text: str, limit: int = SAMPLE_CHARS) -> str:
    """One line of a text for display: masked again (idempotent), whitespace collapsed, clipped."""
    one = " ".join(mask_pii(text).split())
    return one if len(one) <= limit else one[:limit - 1] + "…"


def _origin_where(g: dict) -> tuple[str, list]:
    return ("source=? AND coalesce(account,'')=? AND coalesce(project,'')=? AND coalesce(session_kind,'')=?",
            [g["source"], g["account"] or "", g["project"] or "", g["session_kind"] or ""])


def _samples(con: sqlite3.Connection, where: str, args: list, n: int) -> list[str]:
    """Up to n masked samples; held texts first, then the newest, one per distinct text."""
    n = max(0, min(int(n), MAX_SAMPLES))
    if not n:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for r in con.execute(f"SELECT text FROM units WHERE {where} ORDER BY held DESC, ts DESC LIMIT ?",
                         [*args, n * 8]):
        s = mask_sample(r[0])
        if s and s not in seen:
            seen.add(s)
            out.append(s)
            if len(out) >= n:
                break
    return out


def _origin_groups(con: sqlite3.Connection, samples: int, decisions: set[str]) -> list[dict]:
    out = []
    for r in con.execute(
            "SELECT source, coalesce(account,'') a, coalesce(project,'') p, coalesce(session_kind,'') k, count(*) n, "
            "coalesce(sum(held),0) held, min(ts) first_ts, max(ts) last_ts, "
            "count(DISTINCT coalesce(session, thread_id)) sessions, avg(human_score) score, "
            "coalesce(sum(human_score IS NULL OR score_json LIKE ?), 0) legacy "
            "FROM units GROUP BY source, a, p, k", (LEGACY_LIKE,)):
        g = {"group_id": origin_group_id(r["source"], r["a"], r["p"], r["k"]), "kind": "origin", "source": r["source"],
             "account": r["a"] or None, "project": r["p"] or None, "session_kind": r["k"] or None,
             "units": r["n"], "held": r["held"], "sessions": r["sessions"], "first": r["first_ts"], "last": r["last_ts"],
             "mean_score": None if r["score"] is None else round(r["score"], 3),
             "mixed": bool(r["legacy"]) and not (r["a"] or r["p"] or r["k"])}
        g["decision"] = "keep" if g["group_id"] in decisions else None
        where, args = _origin_where(g)
        g["samples"] = _samples(con, where, args, samples)
        out.append(g)
    return out


def _template_groups(con: sqlite3.Connection, samples: int, decisions: set[str]) -> list[dict]:
    out = []
    for r in con.execute(
            "SELECT template, count(*) n, coalesce(sum(held),0) held, min(ts) first_ts, max(ts) last_ts, "
            "count(DISTINCT coalesce(session, thread_id, unit_id)) sessions, avg(human_score) score FROM units "
            "WHERE template IS NOT NULL GROUP BY template HAVING sessions >= ? AND n >= ?",
            (TEMPLATE_GROUP_MIN_SESSIONS, TEMPLATE_GROUP_MIN_UNITS)):
        gid = template_group_id(r["template"])
        out.append({"group_id": gid, "kind": "template", "source": None, "account": None, "project": None,
                    "session_kind": None, "units": r["n"], "held": r["held"], "sessions": r["sessions"],
                    "first": r["first_ts"], "last": r["last_ts"],
                    "mean_score": None if r["score"] is None else round(r["score"], 3), "mixed": False,
                    "decision": "keep" if gid in decisions else None,
                    "samples": _samples(con, "template=?", [r["template"]], samples)})
    return out


def list_groups(con: sqlite3.Connection, samples: int = 3) -> list[dict]:
    """All groups with counts, held counts and masked samples. Groups with held texts come first (most held first),
    then the rest by size. Pure read."""
    decisions = kept_groups(con)
    found = _origin_groups(con, samples, decisions) + _template_groups(con, samples, decisions)
    found.sort(key=lambda g: (-(g["held"] > 0), -g["held"], -g["units"], g["group_id"]))
    return found


def group_detail(con: sqlite3.Connection, group_id: str, samples: int = 10) -> dict | None:
    """One group (None when the id is unknown or has no stored texts)."""
    if not is_group_id(group_id):
        return None
    for g in list_groups(con, samples=0):
        if g["group_id"] == group_id:
            if g["kind"] == "template":
                where, args = "template=?", [group_id[2:]]
            else:
                where, args = _origin_where(g)
            g["samples"] = _samples(con, where, args, samples)
            return g
    return None


def review_state(con: sqlite3.Connection, samples: int = 3) -> dict:
    """Everything the review page shows, in one read."""
    from . import exclude

    gs = list_groups(con, samples)
    try:
        rules = [{"rule_id": r.rule_id, "rule": r.describe()} for r in exclude.store_rules(con)]
    except sqlite3.OperationalError:
        rules = []
    return {"groups": gs, "held_total": sum(g["held"] for g in gs if g["kind"] == "origin"),
            "decisions": sorted(kept_groups(con)), "rules": rules}


# --- writes ----------------------------------------------------------------------------------------------------------

MIXED_MESSAGE = ("{gid} holds older texts that were stored before mannerism recorded where a text came from, so it can "
                 "mix texts from several places, some of them yours. Run `mannerism review rescan` to sort them into "
                 "groups first. To remove it as it is anyway, pass the force flag (`exclude add --group {gid} --force`).")


def is_mixed(con: sqlite3.Connection, group_id: str) -> bool:
    g = group_detail(con, group_id, samples=0)
    return bool(g and g.get("mixed"))


def not_me(group_id: str, force: bool = False, expected_units: int | None = None) -> dict:
    """"Not me": exclude the group for good (a store rule) and delete its stored texts and what was derived from them.

    A mixed group (older texts with no recorded origin; see `is_mixed`) is refused unless `force`: removing it could
    remove texts the user wrote. The review page never forces."""
    from . import exclude

    try:
        rule = exclude.make_rule(group=group_id)
    except exclude.RuleError as e:
        return {"ok": False, "error": str(e)}
    return exclude.add_rule_and_delete(rule, force=force, expected_units=expected_units)


def its_me(group_id: str, expected_units: int | None = None) -> dict:
    """"It's me": release the group's held texts into the bank and remember the decision, so texts of this group that
    arrive later are never held. Released texts are paired with drafts like any other (capture skipped them while
    they were held). Returns {"ok", "released"}."""
    from . import capture, refresh, sync
    from . import config as config_mod

    if not is_group_id(group_id):
        return {"ok": False, "error": f"{group_id!r} is not a group id (they look like g_0123456789ab)"}
    lock = sync.acquire_lock()
    if lock is None:
        return {"ok": False, "error": "could not take the sync lock within 10s (a sync is running); try again shortly"}
    try:
        cfg = config_mod.load_config()
        con = store.connect()
        try:
            current = group_detail(con, group_id, samples=0)
            if current is None:
                return {"ok": False, "error": "that group is not in the store (it may already be gone)"}
            if expected_units is not None and current["units"] != expected_units:
                return {"ok": False, "error": "this group changed, refresh"}
            ids, regs = [], set()
            for r in con.execute(
                    "SELECT u.unit_id, u.source, u.account, u.project, u.session_kind, u.template, "
                    "coalesce(l.register, u.register) reg FROM units u LEFT JOIN labels l ON l.unit_id=u.unit_id "
                    "WHERE u.held=1").fetchall():
                if group_id in group_ids_of_row(r):
                    ids.append(r["unit_id"])
                    regs.add(r["reg"])
            with store.tx(con):
                for i in range(0, len(ids), 500):
                    part = ids[i:i + 500]
                    con.execute(f"UPDATE units SET held=0 WHERE unit_id IN ({','.join('?' * len(part))})", part)
                con.execute("INSERT INTO group_decisions(group_id, decision, decided_at) VALUES (?, 'keep', ?) "
                            "ON CONFLICT(group_id) DO UPDATE SET decision='keep', decided_at=excluded.decided_at",
                            (group_id, store.now_iso()))
            cap = capture.capture_new_units(con, ids) if ids else {"registers": []}
            changed = sorted((regs | set(cap["registers"])) & set(REGISTERS))
            if changed:
                refresh.refresh(con, changed, cfg.me)
            return {"ok": True, "group_id": group_id, "released": len(ids), "registers": changed}
        finally:
            con.close()
    finally:
        sync.release_lock(lock)
