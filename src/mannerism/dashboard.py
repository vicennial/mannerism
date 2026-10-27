"""Read-only view data for the local page (`mannerism ui`).

Everything here is computed from what is already stored (through one read-only connection): the bank, units,
edits, rules and drafts tables, and the saved cards. Nothing is written, nothing leaves the process, and
every number comes from stored rows: a figure that cannot be computed honestly is returned as `None` with
a short `need` message, and the page then says "not enough data yet" instead of a made-up value.

Texts are shown exactly as stored (already PII-masked). The only text that is cut is an evidence snippet.
"""
from __future__ import annotations

import difflib
import json
import re
import sqlite3
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from . import __version__, api, bank, cards, core, drift, groups, lint, registers, rules, schedule, store, tells
from . import config as config_mod
from . import stats as stats_mod
from .core import REGISTERS

# --- source kinds -----------------------------------------------------------------------------------------------

KIND_LABELS = {
    "agent": "agent prompts", "commit": "commits", "github": "GitHub", "notes": "notes", "mail": "mail import",
    "posts": "posts", "slack": "Slack", "chat": "chats", "dictation": "dictation", "files": "files", "other": "other",
}
SOURCE_KIND = {
    **dict.fromkeys(("claude_code", "codex", "chatgpt", "opencode", "qwen_code", "goose", "aider", "crush", "pi",
                     "continue", "cline", "kilo"), "agent"),
    "git": "commit", "gh": "github", "markdown": "notes", "mbox": "mail",
    **dict.fromkeys(("x_archive", "bluesky", "mastodon", "x"), "posts"),
    "slack": "slack", **dict.fromkeys(("telegram", "whatsapp", "imessage"), "chat"), "wispr": "dictation",
    "files": "files",
}

# --- tuning -----------------------------------------------------------------------------------------------------

HABIT_SCAN = 5000  # newest bank rows read per register
HABITS_PER_CARD = 5
HABIT_MIN, HABIT_MAX = 0.12, 0.95  # a habit must be common enough to matter and not a blanket 100%
SNIPPET_CHARS = 170
MIN_PAIRS_TREND = 6  # edit pairs needed before a trend is shown
MIN_BUCKETS = 3
MIN_WORDS_N = 20  # messages needed before a register's length range is shown
TELL_SCAN = 600  # newest drafts re-checked against the lint
TELL_TOP = 6
DIFF_PAIRS = 3
DIFF_MAX_WORDS = 110
MONTHS_SHOWN = 12
EDIT_SCAN = 5000  # newest edit pairs read
EDIT_CHARS = 4000  # characters of a draft or final that are compared
TELL_CHARS = 3000  # characters of a draft that are linted


def _pct(x: float) -> int:
    """A share as a whole percent that never reads as a blanket 100 or 0 unless it is exactly 1 or 0."""
    return 100 if x >= 1 else 0 if x <= 0 else min(99, max(1, round(100 * x)))


def _parse(ts: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts) if ts else None
    except ValueError:
        return None
    return dt if dt is None or dt.tzinfo else dt.replace(tzinfo=UTC)


def _unavailable(need: str) -> dict:
    return {"ok": False, "need": need}


# --- sync state and sources -------------------------------------------------------------------------------------

def sources_block(con: sqlite3.Connection, st: dict, cfg) -> dict:
    kinds: Counter = Counter()
    per_source = []
    for name, s in sorted(st["sources"].items()):
        entry = cfg.sources.get(name, {}) or {}
        kind = SOURCE_KIND.get(name) or SOURCE_KIND.get(entry.get("adapter", "")) or "other"
        kinds[kind] += s["units"]
        per_source.append({"name": name, "kind": KIND_LABELS[kind], "enabled": s["enabled"], "units": s["units"],
                           "last_success": s.get("last_success"), "last_error": s.get("last_error"),
                           "failures": s.get("failures", 0)})
    return {"last_sync": st["last_sync"], "last_sync_age_s": st["last_sync_age_s"],
            "kinds": [{"kind": k, "label": KIND_LABELS[k], "units": n} for k, n in kinds.most_common() if n > 0],
            "sources": per_source, "enabled": sum(1 for s in per_source if s["enabled"]),
            "with_texts": sum(1 for s in per_source if s["units"])}


# --- voice cards ------------------------------------------------------------------------------------------------

# key, label, test(features, text, ctx) -> bool, typical rate of people in general (used only to rank which habits
# are distinctive: a habit far above what is typical is worth naming)
HABITS = (
    ("starts_lower", "Starts messages in lowercase", lambda f, t, c: bool(f["starts_lower"]), 0.10),
    ("all_lower", "Writes entirely in lowercase", lambda f, t, c: bool(f["all_lower"]), 0.05),
    ("no_end_punct", "Leaves off the final punctuation", lambda f, t, c: not f["ends_with_punct"], 0.40),
    ("short", "Keeps it short (8 words or fewer)", lambda f, t, c: f["words"] <= 8, 0.30),
    ("long", "Writes at length (40 words or more)", lambda f, t, c: f["words"] >= 40, 0.10),
    ("multiline", "Breaks a message over several lines", lambda f, t, c: f["lines"] > 1, 0.20),
    ("emoji", "Uses emoji", lambda f, t, c: f["emoji"] > 0, 0.05),
    ("laugh", "Laughs in text (haha, lol)", lambda f, t, c: f["laugh"] > 0, 0.03),
    ("exclaim", "Uses exclamation marks", lambda f, t, c: f["exclaim"] > 0, 0.15),
    ("question", "Asks questions", lambda f, t, c: f["question"] > 0, 0.20),
    ("ellipsis", "Trails off with an ellipsis", lambda f, t, c: f["ellipsis"] > 0, 0.03),
    ("em_dash", "Reaches for the em dash", lambda f, t, c: f["em_dash"] > 0, 0.05),
    ("hedge", "Hedges (I think, maybe)", lambda f, t, c: f["hedge"] > 0, 0.08),
    ("shorthand", "Uses shorthand (u, pls, btw)", lambda f, t, c: f["shorthand_per100w"] > 0, 0.10),
    ("contractions", "Uses contractions", lambda f, t, c: f["contraction_per100w"] > 0, 0.45),
    ("urls", "Includes a link", lambda f, t, c: f["urls"] > 0, 0.10),
)
_FIRST = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)?", re.UNICODE)


def _first_last(text: str) -> tuple[str, str]:
    words = [w.lower() for w in _FIRST.findall(text)]
    return (words[0], words[-1]) if words else ("", "")


def _snippet(text: str) -> str:
    one = " ".join(text.split())
    return one if len(one) <= SNIPPET_CHARS else one[:SNIPPET_CHARS].rsplit(" ", 1)[0] + "…"


def _example(matches: list[sqlite3.Row]) -> dict | None:
    """A real message that shows the habit: close to the typical length of the matches, readable at a glance."""
    if not matches:
        return None
    lens = sorted(len(" ".join(r["text"].split())) for r in matches)
    target = min(max(lens[len(lens) // 2], 28), 110)
    best = min(matches, key=lambda r: (abs(len(" ".join(r["text"].split())) - target), r["ts"]))
    return {"text": _snippet(best["text"]), "month": best["ts"][:7],
            "kind": KIND_LABELS[SOURCE_KIND.get(best["source"], "other")]}


def habits_for(rows: list[sqlite3.Row], st: dict | None) -> list[dict]:
    """The habits that set this register apart, each with its count and a real example. A habit shown has to be
    present in 12% to 95% of the messages, so it is a tendency, never a blanket claim."""
    n = len(rows)
    if n < 10:
        return []
    feats = [json.loads(r["features_json"]) for r in rows]
    cands: list[tuple[str, str, list[int], float]] = []  # key, label, matching row indexes, baseline
    for key, label, test, base in HABITS:
        cands.append((key, label, [i for i, (f, r) in enumerate(zip(feats, rows, strict=True)) if test(f, r["text"], None)], base))
    firsts = [_first_last(r["text"]) for r in rows]
    for kind, idx, noun in (("opener", 0, "Opens with"), ("closer", 1, "Ends on")):
        for word, _rate in ((st or {}).get("openers" if idx == 0 else "closers") or [])[:2]:
            cands.append((f"{kind}:{word}", f"{noun} “{word}”", [i for i, p in enumerate(firsts) if p[idx] == word], 0.02))
    scored = []
    for key, label, idxs, base in cands:
        rate = len(idxs) / n
        if HABIT_MIN <= rate <= HABIT_MAX:
            scored.append((abs(rate - base), key, label, idxs))
    scored.sort(key=lambda x: (-x[0], x[1]))
    out, seen = [], set()
    for _score, key, label, idxs in scored:
        if len(out) >= HABITS_PER_CARD:
            break
        if frozenset(idxs) in seen:  # the same messages as a stronger habit: say it once
            continue
        seen.add(frozenset(idxs))
        out.append({"key": key, "label": label, "count": len(idxs), "of": n, "share": round(len(idxs) / n, 3),
                    "example": _example([rows[i] for i in idxs])})
    return out


def _card_status(api_state: str, drift_score) -> str:
    if api_state.startswith("insufficient"):
        return "building"
    if api_state == "upgrade available":
        return "upgrade"
    if drift_score is not None and drift_score >= 0.75:
        return "stale"
    return "fresh"


def cards_block(con: sqlite3.Connection, st: dict) -> dict:
    out, waiting = [], []
    for reg in REGISTERS:
        info = st["registers"][reg]
        source = cards.card_source(reg)
        if source is None or info["units"] < api.CARD_MIN:
            if info["units"]:
                waiting.append({"register": reg, "note": cards.REGISTER_NOTES.get(reg, reg), "units": info["units"],
                                "need": api.CARD_MIN})
            continue
        rows = con.execute("SELECT source, ts, text, features_json FROM bank WHERE register=? ORDER BY ts DESC LIMIT ?",
                           (reg, HABIT_SCAN)).fetchall()
        try:
            mtime = (cards.cards_dir() / f"{reg}.md").stat().st_mtime
            updated = datetime.fromtimestamp(mtime, UTC).isoformat(timespec="seconds")
        except OSError:
            updated = None
        out.append({"register": reg, "note": cards.REGISTER_NOTES.get(reg, reg), "units": info["units"],
                    "scanned": len(rows), "status": _card_status(info["state"], info.get("drift")),
                    "card_source": source, "updated": updated, "text": cards.load_card(reg),
                    "habits": habits_for(rows, stats_mod.load_stats(reg))})
    return {"cards": out, "waiting": waiting}


# --- rules and edits --------------------------------------------------------------------------------------------

def diff_ops(draft: str, final: str) -> list[dict]:
    """Word-level diff as [{"t": "eq"|"del"|"ins", "s": text}], for the draft-vs-sent view."""
    a, b = draft.split(), final.split()
    ops: list[dict] = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            ops.append({"t": "eq", "s": " ".join(a[i1:i2])})
            continue
        if i2 > i1:
            ops.append({"t": "del", "s": " ".join(a[i1:i2])})
        if j2 > j1:
            ops.append({"t": "ins", "s": " ".join(b[j1:j2])})
    return ops


def edit_hunks(draft: str, final: str) -> int:
    """Number of separate changed passages between a draft and what was sent (0: sent as written)."""
    sm = difflib.SequenceMatcher(None, draft.split(), final.split(), autojunk=False)
    return sum(1 for op in sm.get_opcodes() if op[0] != "equal")


def _pair(row: sqlite3.Row) -> dict | None:
    if len(row["draft"].split()) > DIFF_MAX_WORDS or len(row["final"].split()) > DIFF_MAX_WORDS:
        return None
    return {"edit_id": row["edit_id"], "register": row["register"], "ts": row["ts"], "status": row["status"],
            "ops": diff_ops(row["draft"], row["final"])}


def _ids(rule: sqlite3.Row) -> list[int]:
    try:
        return [int(i) for i in json.loads(rule["edit_ids_json"] or "[]")]
    except (ValueError, TypeError):
        return []


def rules_block(con: sqlite3.Connection) -> dict:
    existing = {r[0] for r in con.execute("SELECT edit_id FROM edits")}
    rows = con.execute("SELECT rule_id, register, text, support, contradicted, created_at, last_seen, edit_ids_json "
                       "FROM rules ORDER BY created_at DESC, rule_id DESC").fetchall()
    shown, hidden = [], 0
    for r in rows:
        if r["contradicted"] >= r["support"]:
            hidden += 1
            continue
        ids = [i for i in _ids(r) if i in existing]  # evidence that was forgotten no longer counts
        shown.append({"rule_id": r["rule_id"], "register": r["register"], "text": r["text"], "support": r["support"],
                      "edits": len(ids), "learned": r["created_at"], "last_seen": r["last_seen"], "_ids": ids,
                      "pair": None})
    for item in shown[:DIFF_PAIRS]:
        if not item["_ids"]:
            continue
        ph = ",".join("?" * len(item["_ids"]))
        cands = con.execute(f"SELECT edit_id, register, ts, draft, final, status FROM edits WHERE edit_id IN ({ph}) "
                            f"ORDER BY (status='edited') DESC, length(draft)+length(final)", item["_ids"]).fetchall()
        item["pair"] = next((p for p in map(_pair, cands) if p), None)
    for item in shown:
        del item["_ids"]
    pending = con.execute("SELECT count(*) FROM edits WHERE status='edited' AND distilled=0").fetchone()[0]
    recent: list[dict] = []
    if not shown:
        for row in con.execute("SELECT edit_id, register, ts, draft, final, status FROM edits WHERE status='edited' "
                               "ORDER BY ts DESC, edit_id DESC LIMIT 12"):
            p = _pair(row)
            if p:
                recent.append(p)
            if len(recent) >= DIFF_PAIRS:
                break
    evidence = len({i for r in rows if r["contradicted"] < r["support"] for i in _ids(r) if i in existing})
    return {"rules": shown, "hidden": hidden, "evidence_edits": evidence, "pending_edits": pending, "recent_pairs": recent,
            "need": f"Rules form once a register has {rules.MIN_PAIRS} edited drafts from {rules.MIN_THREADS} "
                    f"different conversations."}


# --- insights ---------------------------------------------------------------------------------------------------

def _bucket_start(dt: datetime, weekly: bool) -> datetime:
    day = dt.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return day - timedelta(days=day.weekday()) if weekly else day


def edits_trend(con: sqlite3.Connection) -> dict:
    # Pairs longer than the cap are left out (cutting a draft and its final at the same length would invent changes).
    rows = con.execute("SELECT ts, draft, final, status FROM edits WHERE length(draft) <= ? AND length(final) <= ? "
                       "ORDER BY ts DESC, edit_id DESC LIMIT ?", (EDIT_CHARS, EDIT_CHARS, EDIT_SCAN)).fetchall()
    rows.reverse()  # the newest EDIT_SCAN pairs, oldest first
    n = len(rows)
    if n < MIN_PAIRS_TREND:
        return {**_unavailable(f"{MIN_PAIRS_TREND} drafts matched to what you sent ({n} so far)"), "pairs": n}
    pts = []
    for r in rows:
        dt = _parse(r["ts"])
        if dt:
            pts.append((dt, edit_hunks(r["draft"], r["final"]), r["status"]))
    span = (pts[-1][0] - pts[0][0]).days
    weekly = span >= 42
    buckets: dict[datetime, list[int]] = {}
    for dt, hunks, _s in pts:
        buckets.setdefault(_bucket_start(dt, weekly), []).append(hunks)
    series = [{"start": k.date().isoformat(), "mean": round(sum(v) / len(v), 2), "n": len(v)}
              for k, v in sorted(buckets.items())]
    if len(series) < MIN_BUCKETS:
        return {**_unavailable(f"edits spread over at least {MIN_BUCKETS} {'weeks' if weekly else 'days'}"),
                "pairs": n}
    third = max(2, len(pts) // 3)
    first = sum(h for _d, h, _s in pts[:third]) / third
    latest = sum(h for _d, h, _s in pts[-third:]) / third
    return {"ok": True, "bucket": "week" if weekly else "day", "series": series, "pairs": n,
            "first": round(first, 2), "latest": round(latest, 2), "window": third}


def written_share(con: sqlite3.Connection) -> dict:
    rows = con.execute("SELECT ts, status FROM edits").fetchall()
    n = len(rows)  # drafts matched to something the user wrote (at least half the same), not every draft logged
    drafts = con.execute("SELECT count(*) FROM drafts").fetchone()[0]
    if n < 5:
        return {**_unavailable(f"5 drafts matched to what you sent ({n} so far)"), "pairs": n, "drafts": drafts}
    accepted = sum(1 for r in rows if r["status"] == "accepted")
    cut = store.ts_iso(datetime.now(UTC) - timedelta(days=30))
    recent = [r for r in rows if r["ts"] >= cut]
    rec_share = (sum(1 for r in recent if r["status"] == "accepted") / len(recent)) if len(recent) >= 5 else None
    return {"ok": True, "pairs": n, "as_written": accepted, "edited": n - accepted, "share": accepted / n,
            "recent_share": rec_share, "recent_n": len(recent),
            "drafts": drafts}


def tells_caught(con: sqlite3.Connection, cfg) -> dict:
    drafts = con.execute("SELECT text, register FROM drafts WHERE length(text) <= ? ORDER BY ts DESC LIMIT ?",
                         (TELL_CHARS, TELL_SCAN)).fetchall()
    skipped = con.execute("SELECT count(*) FROM drafts WHERE length(text) > ?", (TELL_CHARS,)).fetchone()[0]
    n = len(drafts)
    if n < 5:
        return {**_unavailable(f"5 logged drafts ({n} so far)"), "drafts": n, "skipped_long": skipped}
    stats_cache: dict[str, dict | None] = {}
    seen: Counter = Counter()
    with_any = 0
    uncalibrated = set()
    for d in drafts:
        reg = d["register"]
        if reg not in stats_cache:
            stats_cache[reg] = stats_mod.load_stats(reg)
            if not lint.is_calibrated(stats_cache[reg]):
                uncalibrated.add(reg)
        codes = {f.code for f in lint.lint(d["text"], reg, stats=stats_cache[reg], threshold=cfg.tell_threshold)
                 if f.code in tells.BY_ID}
        with_any += bool(codes)
        seen.update(codes)
    top = [{"id": tid, "label": tells.BY_ID[tid].label, "drafts": c} for tid, c in seen.most_common(TELL_TOP)]
    return {"ok": True, "drafts": n, "with_tells": with_any, "top": top, "uncalibrated": sorted(uncalibrated),
            "skipped_long": skipped}  # drafts over TELL_CHARS are not checked, not cut


def words_by_register(con: sqlite3.Connection) -> dict:
    by: dict[str, list[int]] = {}
    for r in con.execute("SELECT register, words FROM bank"):
        by.setdefault(r["register"], []).append(r["words"])
    rows = []
    for reg in REGISTERS:
        vals = sorted(by.get(reg, []))
        if len(vals) >= MIN_WORDS_N:
            rows.append({"register": reg, "n": len(vals), "p25": round(stats_mod.percentile(vals, 25), 1),
                         "median": round(stats_mod.percentile(vals, 50), 1),
                         "p75": round(stats_mod.percentile(vals, 75), 1)})
    if not rows:
        return _unavailable(f"{MIN_WORDS_N} messages in a register")
    allv = sorted(v for vs in by.values() for v in vs)
    return {"ok": True, "registers": rows, "overall_median": round(stats_mod.percentile(allv, 50), 1),
            "overall_n": len(allv)}


def texts_by_month(con: sqlite3.Connection, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    months, y, m = [], now.year, now.month
    for _ in range(MONTHS_SHOWN):
        months.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    months.reverse()
    counts = {r[0]: r[1] for r in con.execute("SELECT substr(ts, 1, 7), count(*) FROM units GROUP BY 1")}
    series = [{"month": mo, "n": counts.get(mo, 0)} for mo in months]
    if sum(s["n"] for s in series) < 20:
        return _unavailable("20 texts from the last 12 months")
    return {"ok": True, "series": series}


def insights_block(con: sqlite3.Connection, st: dict, cfg, rules_info: dict) -> dict:
    trend = edits_trend(con)
    share = written_share(con)
    tell = tells_caught(con, cfg)
    words = words_by_register(con)
    months = texts_by_month(con)
    first_ts = con.execute("SELECT min(ts) FROM units").fetchone()[0]
    first = _parse(first_ts)
    span_months = None
    if first:
        span_months = max(1, round((datetime.now(UTC) - first).days / 30.4))
    tiles = [
        {"key": "texts", "label": "Texts learned from", "value": st["total_units"], "format": "int",
         "sub": f"{st['sources_with_texts']} source{'s' if st['sources_with_texts'] != 1 else ''}"},
        {"key": "span", "label": "Writing history", "value": span_months, "format": "months",
         "sub": f"since {first.strftime('%b %Y')}" if first else None, "need": "some texts"},
        {"key": "rules", "label": "Rules from your edits", "value": len(rules_info["rules"]), "format": "int",
         "sub": f"from {rules_info['evidence_edits']} of your edits" if rules_info["rules"] else
         f"form after {rules.MIN_PAIRS} edited drafts from {rules.MIN_THREADS} conversations", "need": None},
        {"key": "written", "label": "Sent as written (90%+ the same)",
         "value": _pct(share["share"]) if share.get("ok") else None, "format": "pct",
         "sub": f"{share['as_written']} of {share['pairs']} matched drafts" if share.get("ok") else None,
         "need": share.get("need")},
        {"key": "edits", "label": "Edits per draft, now",
         "value": trend["latest"] if trend.get("ok") else None, "format": "num1",
         "sub": f"was {trend['first']:g} at the start" if trend.get("ok") else None, "need": trend.get("need")},
        {"key": "words", "label": "Median message", "value": words.get("overall_median") if words.get("ok") else None,
         "format": "words", "sub": f"across {words['overall_n']:,} messages" if words.get("ok") else None,
         "need": words.get("need")},
        {"key": "tells", "label": "Drafts with an AI tell",
         "value": tell["with_tells"] if tell.get("ok") else None, "format": "int",
         "sub": f"of {tell['drafts']} logged" if tell.get("ok") else None, "need": tell.get("need")},
    ]
    for t in tiles:
        t["available"] = t["value"] is not None  # a real zero is a value; only a missing figure is "not enough data"
    return {"tiles": tiles, "edits_per_draft": trend, "written": share, "tells": tell, "words": words,
            "months": months}


# --- the whole page ---------------------------------------------------------------------------------------------

def _next_step(st: dict, cards_info: dict) -> str | None:
    if not st["total_units"]:
        return "Nothing read yet. Run `mannerism init` to find your writing and build your first cards."
    if not cards_info["cards"]:
        need = api.CARD_MIN
        return (f"Your writing is in, but no register has {need} messages yet. Keep working; "
                f"`mannerism sync` reads new text, or add a source with `mannerism sources`.")
    return None


# The oldest store schema this page can read. v3 only added `edit_contradictions`, which the page never reads, so a
# store from before an upgrade still opens without waiting for another command to migrate it.
PAGE_MIN_SCHEMA = 2


def _open_readonly() -> sqlite3.Connection:
    """A connection that cannot write: `mode=ro` plus `query_only`, never migrating or creating anything.

    A store that does not exist yet is an empty in-memory one, so a fresh home stays absent."""
    path = store.db_path()
    if not path.exists():
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(store.SCHEMA)
        return con
    con = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=3)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=3000")
    con.execute("PRAGMA query_only=ON")
    try:
        row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    except sqlite3.OperationalError:
        row = None
    if not row or int(row[0]) < PAGE_MIN_SCHEMA:
        con.close()
        raise StoreUnavailable("the store is from an older version: run `mannerism status` once to upgrade it")
    return con


class StoreUnavailable(Exception):
    pass


def read_status(con: sqlite3.Connection, cfg) -> dict:
    """The part of `api.status()` the page needs, from a read-only connection (api.status opens a normal one)."""
    flagged = drift.flagged(con)
    bank_n = bank.bank_counts(con)
    regs = {}
    for r in REGISTERS:
        n = bank_n.get(r, 0)
        regs[r] = {"units": n, "state": api._card_state(con, r, n, set(flagged)),  # a pure read
                   "drift": (flagged.get(r) or {}).get("score")}
    units = {r[0]: r[1] for r in con.execute("SELECT source, count(*) FROM units GROUP BY source")}
    srcs = {}
    for name in set(units) | set(cfg.sources):
        state = schedule.get(con, name)
        srcs[name] = {"enabled": bool(cfg.sources.get(name, {}).get("enabled")), "units": units.get(name, 0),
                      "last_success": state.get("last_success"), "last_error": state.get("last_error"),
                      "failures": int(state.get("failures") or 0)}
    last = store.get_meta(con, "last_sync")
    when = _parse(last)
    return {"ok": True, "last_sync": last,
            "last_sync_age_s": (datetime.now(UTC) - when).total_seconds() if when else None,
            "sources": srcs, "registers": regs, "total_units": sum(units.values()),
            "sources_with_texts": sum(1 for s in srcs.values() if s["units"]),
            "pending_work": {"label": registers.unlabelled_count(con)}}


def _compute() -> dict:
    cfg = config_mod.load_config()
    try:
        con = _open_readonly()
    except StoreUnavailable as e:
        return {"ok": False, "error": str(e)}
    try:
        st = read_status(con, cfg)
        src = sources_block(con, st, cfg)
        card_info = cards_block(con, st)
        rule_info = rules_block(con)
        insights = insights_block(con, st, cfg, rule_info)
        n_drafts = con.execute("SELECT count(*) FROM drafts").fetchone()[0]
    finally:
        con.close()
    step = _next_step(st, card_info)
    return {"ok": True, "version": __version__, "generated_at": store.now_iso(), "home": str(core.home()),
            "stage": "empty" if not st["total_units"] else ("early" if step else "ready"),
            "next_step": step, "total_units": st["total_units"], "sources": src, "cards": card_info,
            "learned": rule_info, "insights": insights, "pending": st["pending_work"],
            "drafts_logged": n_drafts}


# One computation at a time, remembered until the stored data changes: repeated Refresh or focus events must not
# re-lint hundreds of drafts or re-diff thousands of edits.
_LOCK = threading.Lock()
_CACHE: dict = {}


def _stat(path: Path):
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size) if st.st_size else None  # an empty -wal (made by a reader) is no change


def _signature() -> tuple:
    db = store.db_path()
    parts: list = [datetime.now(UTC).date().isoformat(), str(core.home()), _stat(db), _stat(Path(f"{db}-wal")),
                   _stat(config_mod.config_path())]
    for d in (stats_mod.stats_dir(), cards.cards_dir()):
        try:
            parts.extend((f.name, _stat(f)) for f in sorted(d.iterdir()))
        except OSError:
            parts.append(None)
    return tuple(parts)


def _fresh(payload: dict) -> dict:
    """The cached page data with the clock-dependent fields brought up to date."""
    if not payload.get("ok"):
        return payload
    out = dict(payload, generated_at=store.now_iso())
    src = dict(out["sources"])
    when = _parse(src.get("last_sync"))
    src["last_sync_age_s"] = (datetime.now(UTC) - when).total_seconds() if when else None
    out["sources"] = src
    return out


def overview() -> dict:
    """Everything the page shows, in one JSON-serialisable dict (`ok` is False only when the store cannot be read).

    Read-only: nothing is created, migrated or written, even in a fresh home."""
    sig = _signature()
    with _LOCK:
        hit = _CACHE.get("v")
        if hit and hit[0] == sig:
            return _fresh(hit[1])
        payload = _compute()
        if payload.get("ok"):
            _CACHE["v"] = (sig, payload)
        return _fresh(payload)


# --- "Is this you?" (read side; the two writes are in groups.py and are POSTed through web.py) -------------------

REVIEW_MAX_GROUPS = 300  # groups sent to the page; the biggest and the held ones come first, the rest is counted
REVIEW_SAMPLES = 3
REVIEW_MIN_SCHEMA = 4  # origin columns and group decisions


def _open_review() -> sqlite3.Connection:
    con = _open_readonly()
    row = con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    if con.execute("SELECT count(*) FROM units").fetchone()[0] and (not row or int(row[0]) < REVIEW_MIN_SCHEMA):
        con.close()
        raise StoreUnavailable(UPGRADE)
    return con


UPGRADE = "the store is from an older version: run `mannerism status` once to upgrade it"
BREAKDOWN_ROWS = 6  # source / project lines shown in a template group's delete confirmation


def _is_schema_error(err: sqlite3.OperationalError) -> bool:
    text = str(err)
    return "no such column" in text or "no such table" in text


def _template_breakdown(con: sqlite3.Connection, templates: list[str]) -> dict[str, dict]:
    """Per template group: where its texts come from, as {"by_origin": [{source, project, units}], "more": n}."""
    out: dict[str, list] = {t: [] for t in templates}
    for i in range(0, len(templates), 400):
        part = templates[i:i + 400]
        for r in con.execute(
                f"SELECT template, source, coalesce(project,'') p, count(*) n FROM units WHERE template IN "
                f"({','.join('?' * len(part))}) GROUP BY template, source, p ORDER BY n DESC, source, p", part):
            out[r["template"]].append({"source": r["source"], "project": r["p"] or None, "units": r["n"]})
    return {t: {"by_origin": rows[:BREAKDOWN_ROWS], "more": max(0, len(rows) - BREAKDOWN_ROWS)} for t, rows in out.items()}


def review() -> dict:
    """Every group with counts, held counts, masked samples and the user's earlier answers, held groups first.

    Read-only, like `overview`: one `mode=ro` connection, nothing created or migrated. Template groups carry where
    their texts come from (`breakdown`), so the page can say what a "Not me" would delete. No rule text and no scores
    are sent: the page does not show them."""
    try:
        con = _open_review()
    except StoreUnavailable as e:
        return {"ok": False, "error": str(e)}
    try:
        state = groups.review_state(con, REVIEW_SAMPLES)
        shown = state["groups"][:REVIEW_MAX_GROUPS]
        crumbs = _template_breakdown(con, [g["group_id"][2:] for g in shown if g["kind"] == "template"])
    except sqlite3.OperationalError as e:
        return {"ok": False, "error": UPGRADE if _is_schema_error(e) else "could not read the groups"}
    finally:
        con.close()
    out = []
    for g in shown:
        g = {k: v for k, v in g.items() if k != "mean_score"}
        if g["kind"] == "template":
            g["breakdown"] = crumbs[g["group_id"][2:]]
        out.append(g)
    return {"ok": True, "groups_total": len(state["groups"]), "held_groups": sum(1 for g in out if g["held"]),
            "groups": out, "held_total": state["held_total"], "decisions": state["decisions"]}


def group_info(group_id: str) -> dict | None:
    """The group `group_id` names right now (counts, `mixed` when the engine reports it), or None. Read-only."""
    con = _open_review()
    try:
        return groups.group_detail(con, group_id, samples=0)
    finally:
        con.close()


def invalidate() -> None:
    """Forget the cached overview: a write just changed what it was computed from."""
    with _LOCK:
        _CACHE.clear()
