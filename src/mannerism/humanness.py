"""Machine-typed detection: a deterministic `human_score` (0 to 1) per unit, with the reasons it lost points.

No model and no network. A unit starts at 1.0 and loses points for signals that text written by a person rarely
shows but scripts, orchestrators and relays show all the time:

- wrapper / markup: XML-like tags wrapping most of the text (`<x-message from=...>`), a `[SYSTEM` marker, front matter,
  a JSON blob;
- relay / status formats: mostly `key: value` lines, or bullet-only status blocks;
- repetition: the same opening words (a "template") in many different sessions;
- speed: a long block that arrived faster than anyone types, measured from the end of the previous turn (only
  when the transcript has the timestamps);
- structured sessions: nearly every prompt of the session has one of the structural signals above.

Thresholds (conservative: prefer holding over dropping, and drop only when several signals agree):

- score < LOW (0.20): the unit is not stored; sync and scan count it as filtered;
- LOW <= score < REVIEW (0.60): stored but `held`: kept out of the bank, cards and examples, and listed for the user
  to confirm ("Is this you?");
- score >= REVIEW: normal.

Short text is never judged: text under MIN_CHARS characters has no structure to find, so "lgtm" or "fix the test" can
never lose points, and a template needs at least TEMPLATE_MIN_WORDS words, so a short prompt a person types in every
session ("continue", "yes, go ahead") is not a template.

The cross-session signals (repetition, structured sessions) depend on what else is known, so a unit's score can change
as more sessions arrive. `Screener` computes the score at ingest from the batch plus the store and notes which templates
and sessions the batch moved across a threshold of the score; `rescore_after_write` scores the stored units of those
again, each with its own real counts. Only ingest drops a unit, and only a new one: a stored unit that falls below LOW
is held, never deleted and never released. A group the user confirmed ("it's me") is never held or dropped.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field

from . import store

LOW = 0.20
REVIEW = 0.60
MIN_CHARS = 20  # shorter text has no structure to judge
TEMPLATE_MIN_WORDS = 8
TEMPLATE_PREFIX_WORDS = 8  # a template is the normalised first words
TEMPLATE_MIN_CHARS = 40
SESSION_MIN_UNITS = 5  # a session needs this many prompts before "nearly all are structured" means anything
SESSION_STRUCTURED_SHARE = 0.8
FAST_MIN_CHARS = 300
FAST_CHARS_PER_S = 30.0  # about 360 words a minute: faster than anyone types
JSON_MAX_CHARS = 200_000

# Points lost per signal. Several must add up to reach LOW; one alone never drops a unit.
PEN_MARKUP_STRONG = 0.55
PEN_MARKUP_WEAK = 0.40
PEN_MARKER = 0.55
PEN_FRONT_MATTER = 0.50
PEN_JSON = 0.60
PEN_KEY_VALUE = 0.35
PEN_STATUS = 0.30
PEN_FAST = 0.20
PEN_SESSION = 0.25
TEMPLATE_STEPS = ((12, 0.55), (6, 0.40), (3, 0.25))  # (sessions sharing the template, penalty)
TEMPLATE_LONG_WORDS = 30
TEMPLATE_LONG_EXTRA = 0.10  # long boilerplate repeated across sessions is even less likely to be typed each time

STRUCTURED_LIKE = '%"structured": true%'  # matches score_json (json.dumps, sorted keys) of a structured unit
STRUCTURAL = frozenset({"markup", "markup-weak", "marker", "front-matter", "json", "key-value", "status"})

_OPEN_TAG = re.compile(r"^\s*<([A-Za-z][\w:.-]*)((?:\s[^<>]*)?)>")
_STRONG_TAG = re.compile(r"(?i)(?:-|_)(?:message|notification|event|task|result|status|report|context|handoff|update)$")
_MARKER = re.compile(r"^\s*\[(?:SYSTEM|system|INTERNAL|internal|AUTOMATED|automated)\b")
_FRONT = re.compile(r"\A---[ \t]*\n(?:[^\n]*\n){1,40}?---[ \t]*(?:\n|\Z)")
_KEY_VALUE = re.compile(r"^\s*(?:[-*]\s+)?[A-Za-z_][\w .-]{0,31}:\s+\S")
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)]|\[[ xX]\]|[✓✔✅❌⏳🟢🔴])\s*\S")
_STATUS_WORDS = re.compile(r"(?i)\b(?:done|completed?|merged|passed|failed|pending|blocked|running|queued|in progress|"
                           r"todo|ok|error|timed out)\b|[✓✔✅❌⏳🟢🔴]")
_HEX = re.compile(r"\b[0-9a-f]{8,}\b")
_DIGITS = re.compile(r"\d+")
_EDGE_PUNCT = ".,;:!?\"'()[]{}*`_"


@dataclass
class Signals:
    """What the text alone shows (no cross-unit information)."""
    base: float = 0.0
    codes: list[str] = field(default_factory=list)
    structured: bool = False
    template: str | None = None
    words: int = 0


def template_of(text: str) -> tuple[str | None, int]:
    """(template id, word count). The id hashes the normalised first words (numbers and hex ids folded), so the same
    prompt with a different ticket number or run id is the same template. None for text too short to be one."""
    t = text.strip()
    words = _DIGITS.sub("#", _HEX.sub("#", t.lower())).split()
    if len(t) < TEMPLATE_MIN_CHARS or len(words) < TEMPLATE_MIN_WORDS:
        return None, len(words)
    toks = [w.strip(_EDGE_PUNCT) or w for w in words[:TEMPLATE_PREFIX_WORDS]]
    return hashlib.sha1(" ".join(toks).encode("utf-8", "replace")).hexdigest()[:12], len(words)


def _markup(text: str) -> tuple[str, float] | None:
    """Tags wrapping the text. A tag that names a message / event / task or carries `from=` is a relay wrapper (strong);
    any other tag that opens the text and closes near its end is a weak signal (people paste HTML too)."""
    m = _OPEN_TAG.match(text)
    if not m:
        return None
    tag, attrs = m.group(1), m.group(2)
    lowered = text.rstrip().lower()
    end = f"</{tag.lower()}>"
    pos = lowered.rfind(end)
    wraps = pos > 0 and (pos + len(end)) / len(lowered) >= 0.6
    strong = bool(_STRONG_TAG.search(tag)) or "from=" in attrs
    if strong and (wraps or "=" in attrs):
        return "markup", PEN_MARKUP_STRONG
    if wraps or ("-" in tag or "_" in tag):
        return "markup-weak", PEN_MARKUP_WEAK
    return None


def _json_blob(text: str) -> bool:
    t = text.strip()
    if len(t) < MIN_CHARS or t[0] not in "{[" or t[-1] not in "}]":
        return False
    if len(t) <= JSON_MAX_CHARS:
        try:
            json.loads(t)
            return True
        except ValueError:
            pass
    punct = sum(t.count(c) for c in '{}[]":,')
    return punct / len(t) >= 0.25


def analyse(text: str, gap_s: float | None = None) -> Signals:
    """The signals the text itself (plus the typing gap, when known) gives. Pure and deterministic."""
    sig = Signals()
    tmpl, sig.words = template_of(text)
    sig.template = tmpl
    stripped = text.strip()
    if len(stripped) < MIN_CHARS:
        return sig
    pen = 0.0
    hit = _markup(stripped)
    if hit:
        sig.codes.append(hit[0])
        pen += hit[1]
    if _MARKER.match(stripped):
        sig.codes.append("marker")
        pen += PEN_MARKER
    if _FRONT.match(stripped):
        sig.codes.append("front-matter")
        pen += PEN_FRONT_MATTER
    if _json_blob(stripped):
        sig.codes.append("json")
        pen += PEN_JSON
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if len(lines) >= 4 and sum(bool(_KEY_VALUE.match(ln)) for ln in lines) / len(lines) >= 0.6:
        sig.codes.append("key-value")
        pen += PEN_KEY_VALUE
    if len(lines) >= 3 and sum(bool(_BULLET.match(ln)) for ln in lines) / len(lines) >= 0.9 \
            and sum(bool(_STATUS_WORDS.search(ln)) for ln in lines) / len(lines) >= 0.5:
        sig.codes.append("status")
        pen += PEN_STATUS
    sig.structured = any(c in STRUCTURAL for c in sig.codes)
    if len(stripped) >= FAST_MIN_CHARS and gap_s is not None and 0 <= gap_s < len(stripped) / FAST_CHARS_PER_S:
        sig.codes.append("fast")
        pen += PEN_FAST
    sig.base = round(pen, 3)
    return sig


def template_penalty(sessions: int, words: int = 0) -> float:
    """Points lost for a template seen in `sessions` different sessions (0 below 3)."""
    for n, pen in TEMPLATE_STEPS:
        if sessions >= n:
            return pen + (TEMPLATE_LONG_EXTRA if words >= TEMPLATE_LONG_WORDS else 0.0)
    return 0.0


def session_is_structured(total: int, structured: int) -> bool:
    return total >= SESSION_MIN_UNITS and structured / total >= SESSION_STRUCTURED_SHARE


def score(sig: Signals, template_sessions: int = 0, session_total: int = 0, session_structured: int = 0
          ) -> tuple[float, dict]:
    """(score, info) for a unit's signals plus what is known across units. `info` is what the store keeps:
    the reasons, and what a later rescore needs (`base`, `structured`, `words`, `template`)."""
    codes = list(sig.codes)
    pen = sig.base
    tp = template_penalty(template_sessions, sig.words) if sig.template else 0.0
    if tp:
        codes.append(f"template:{template_sessions} sessions")
        pen += tp
    sp = 0.0
    if session_is_structured(session_total, session_structured):
        sp = PEN_SESSION
        codes.append(f"session:{session_structured}/{session_total} structured")
        pen += sp
    value = round(max(0.0, 1.0 - pen), 3)
    info = {"reasons": codes, "base": sig.base, "structured": sig.structured, "words": sig.words,
            "template": sig.template, "n_sess": template_sessions}
    return value, info


def is_held(value: float) -> bool:
    return LOW <= value < REVIEW


def is_dropped(value: float) -> bool:
    return value < LOW


def score_text(text: str, gap_s: float | None = None, template_sessions: int = 0, session_total: int = 0,
               session_structured: int = 0) -> tuple[float, dict]:
    """Convenience for tests and callers with one text: analyse + score."""
    return score(analyse(text, gap_s), template_sessions, session_total, session_structured)


# --- ingest: score a batch of units against the store ----------------------------------------------------------------

def _session_of(u) -> str | None:
    return u.session or u.thread_id


def template_tier(sessions: int) -> int:
    """Which step of TEMPLATE_STEPS a session count has reached (0 below the first). Only a change of tier changes the
    penalty of the texts that already carry the template."""
    return sum(sessions >= n for n, _pen in TEMPLATE_STEPS)


def holds(value: float) -> bool:
    """Whether a stored text with this score is kept out of the bank. Everything below REVIEW is, including a text that
    fell below LOW after it was stored: only ingest drops a text, and only a new one."""
    return value < REVIEW


class Screener:
    """Scores the units of one read against the batch itself plus what the store already holds.

    `kept` is the set of group ids the user confirmed as theirs: a unit in one is never held. `apply` fills each
    unit's `template`, `human_score`, `held` and `score_info`; it does not drop anything (the caller decides what
    to do with `is_dropped` units) and writes nothing. It also remembers which templates and sessions this batch
    moved across a threshold of the score (`crossed_templates`, `crossed_sessions`): only the stored texts of those
    need scoring again."""

    def __init__(self, con: sqlite3.Connection | None, kept: frozenset[str] | set[str] = frozenset()):
        self.con = con
        self.kept = set(kept)
        self._db_tmpl: dict[str, set[str]] = {}
        self._db_tmpl_sess: dict[tuple[str, str], int] = {}
        self._db_sess: dict[str, tuple[int, int]] = {}
        self.crossed_templates: set[str] = set()
        self.crossed_sessions: set[str] = set()
        self.stored_ids: set[str] = set()

    def reset(self) -> None:
        """Forget the store numbers read so far (the store changes between reads)."""
        self._db_tmpl.clear()
        self._db_tmpl_sess.clear()
        self._db_sess.clear()
        self.stored_ids.clear()

    def _db_template_sessions(self, tmpl: str) -> set[str]:
        if tmpl not in self._db_tmpl:
            self._db_tmpl[tmpl] = template_sessions(self.con, tmpl) if self.con is not None else set()
        return self._db_tmpl[tmpl]

    def _db_session(self, sess: str) -> tuple[int, int]:
        if sess not in self._db_sess:
            self._db_sess[sess] = session_counts(self.con, sess) if self.con is not None else (0, 0)
        return self._db_sess[sess]

    def _db_template_session_count(self, tmpl: str, sess: str) -> int:
        key = (tmpl, sess)
        if key not in self._db_tmpl_sess:
            if self.con is None:
                self._db_tmpl_sess[key] = 0
            else:
                self._db_tmpl_sess[key] = int(self.con.execute(
                    "SELECT count(*) FROM units WHERE template=? "
                    "AND coalesce(session, thread_id, unit_id)=?", key).fetchone()[0])
        return self._db_tmpl_sess[key]

    def _stored_rows(self, ids: list[str]) -> dict[str, tuple[str | None, str | None, bool]]:
        have: dict[str, tuple[str | None, str | None, bool]] = {}
        if self.con is None:
            return have
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            for r in self.con.execute(
                    f"SELECT unit_id, template, coalesce(session, thread_id) sess, score_json LIKE ? structured "
                    f"FROM units WHERE unit_id IN ({','.join('?' * len(part))})", [STRUCTURED_LIKE, *part]):
                have[r[0]] = (r[1], r[2], bool(r[3]))
        return have

    def is_kept(self, u, group_ids) -> bool:
        return bool(set(group_ids(u)) & self.kept)

    def apply(self, units: list, group_ids) -> list:
        """Score `units` in place (and return them). `group_ids(u)` gives the group ids a unit belongs to."""
        if not units:
            self.stored_ids = set()
            return units
        sigs = [analyse(u.text, u.gap_s) for u in units]
        stored = self._stored_rows([u.unit_id for u in units])
        self.stored_ids = set(stored)

        old_templates: Counter[tuple[str, str]] = Counter()
        new_templates: Counter[tuple[str, str]] = Counter()
        old_sessions: dict[str, list[int]] = {}
        new_sessions: dict[str, list[int]] = {}
        real_sessions: set[str] = set()
        old_real_sessions: set[str] = set()
        for u, sg in zip(units, sigs):
            if u.unit_id in stored:
                old_template, old_session, old_structured = stored[u.unit_id]
                old_key = old_session or u.unit_id
                if old_template:
                    old_templates[(old_template, old_key)] += 1
                old_count = old_sessions.setdefault(old_key, [0, 0])
                old_count[0] += 1
                old_count[1] += int(old_structured)
                self.crossed_templates.update(t for t in (old_template, sg.template) if t)
                if old_session:
                    old_real_sessions.add(old_session)
                    self.crossed_sessions.add(old_session)
            sess = _session_of(u) or u.unit_id
            if _session_of(u):
                real_sessions.add(sess)
                if u.unit_id in stored:
                    self.crossed_sessions.add(sess)
            if sg.template:
                new_templates[(sg.template, sess)] += 1
            c = new_sessions.setdefault(sess, [0, 0])
            c[0] += 1
            c[1] += int(sg.structured)

        # Compute the exact post-write aggregate before deciding whether any new unit can be dropped. Stored updates
        # replace their old contribution; new units add one. This avoids irreversible decisions from stale counts.
        template_sessions_after: dict[str, set[str]] = {}
        template_buckets: dict[str, set[str]] = {}
        for tmpl, sess in old_templates:
            template_buckets.setdefault(tmpl, set()).add(sess)
        for tmpl, sess in new_templates:
            template_buckets.setdefault(tmpl, set()).add(sess)
        for tmpl, sessions in template_buckets.items():
            before = self._db_template_sessions(tmpl)
            after = set(before)
            for sess in sessions:
                if new_templates[(tmpl, sess)] > 0:
                    after.add(sess)
                elif self._db_template_session_count(tmpl, sess) == old_templates[(tmpl, sess)]:
                    after.discard(sess)
            template_sessions_after[tmpl] = after
            if template_tier(len(before)) != template_tier(len(after)):
                self.crossed_templates.add(tmpl)

        session_counts_after: dict[str, tuple[int, int]] = {}
        for sess in set(old_sessions) | set(new_sessions):
            if sess in real_sessions or sess in old_real_sessions:
                before = self._db_session(sess)
            else:  # no session/thread id: the fallback key is this unit id, so only its stored row can contribute
                before = tuple(old_sessions.get(sess, [0, 0]))
            old = old_sessions.get(sess, [0, 0])
            new = new_sessions.get(sess, [0, 0])
            after = (before[0] - old[0] + new[0], before[1] - old[1] + new[1])
            session_counts_after[sess] = after
            if sess in real_sessions and session_is_structured(*before) != session_is_structured(*after):
                self.crossed_sessions.add(sess)

        for u, sg in zip(units, sigs):
            sess = _session_of(u) or u.unit_id
            n_t = len(template_sessions_after.get(sg.template, ())) if sg.template else 0
            total, structured = session_counts_after[sess]
            value, info = score(sg, n_t, total, structured)
            u.template = sg.template
            u.human_score = value
            u.score_info = info
            u.held = holds(value) and not self.is_kept(u, group_ids)
        return units


# --- stored units: counts and rescoring after the counts moved -------------------------------------------------------

def template_sessions(con: sqlite3.Connection, tmpl: str) -> set[str]:
    return {r[0] for r in con.execute(
        "SELECT DISTINCT coalesce(session, thread_id, unit_id) FROM units WHERE template=?", (tmpl,))}


def session_counts(con: sqlite3.Connection, sess: str) -> tuple[int, int]:
    """(texts, structured texts) the store holds for a session. Uses the expression index `units_sess_key`."""
    row = con.execute("SELECT count(*), coalesce(sum(score_json LIKE ?), 0) FROM units "
                      "WHERE coalesce(session, thread_id)=?", (STRUCTURED_LIKE, sess)).fetchone()
    return int(row[0]), int(row[1])


def _rescore_rows(con, rows, tmpl_n: dict[str, int], sess_n: dict[str, tuple[int, int]], kept, group_of) -> set[str]:
    """Recompute rows (sqlite rows of units with score_json). Every count comes from the store as it is now: the
    template's sessions and the row's own session, whichever batch the caller is handling (`tmpl_n`, `sess_n` are
    caches the function fills). Returns the registers of units whose held flag changed.

    A text is released only when its score reached REVIEW (or its group was confirmed). A text that falls below LOW
    stays held; it is never dropped or released here."""
    changed: set[str] = set()
    updates = []
    for r in rows:
        try:
            info = json.loads(r["score_json"] or "{}")
        except ValueError:
            continue
        sg = Signals(base=float(info.get("base", 0.0)), codes=[c for c in info.get("reasons", [])
                                                              if not c.startswith(("template:", "session:"))],
                     structured=bool(info.get("structured")), template=info.get("template"),
                     words=int(info.get("words", 0)))
        n_t = 0
        if sg.template:
            if sg.template not in tmpl_n:
                tmpl_n[sg.template] = len(template_sessions(con, sg.template))
            n_t = tmpl_n[sg.template]
        sess = r["session"] or r["thread_id"]
        total, structured = (0, 0)
        if sess:
            if sess not in sess_n:
                sess_n[sess] = session_counts(con, sess)
            total, structured = sess_n[sess]
        value, new = score(sg, n_t, total, structured)
        for key in ("legacy",):  # flags a rescan stored with the score survive a rescore
            if info.get(key):
                new[key] = info[key]
        held = int(holds(value) and not (set(group_of(r)) & kept))
        if value != r["human_score"] or held != r["held"]:
            updates.append((value, held, json.dumps(new, sort_keys=True), r["unit_id"]))
            if held != r["held"]:
                changed.add(r["reg"])
    for i in range(0, len(updates), 500):
        con.executemany("UPDATE units SET human_score=?, held=?, score_json=? WHERE unit_id=?", updates[i:i + 500])
    return changed


_RESCORE_SQL = ("SELECT u.unit_id, u.session, u.thread_id, u.human_score, u.held, u.score_json, u.source, u.account, "
                "u.project, u.session_kind, u.template, coalesce(l.register, u.register) AS reg FROM units u "
                "LEFT JOIN labels l ON l.unit_id=u.unit_id WHERE ")


def rescore_after_write(con: sqlite3.Connection, templates, sessions, kept, group_of) -> set[str]:
    """Score the stored units of these templates and sessions again. The caller passes only the ones whose count crossed
    a threshold of the score (`Screener.crossed_templates` / `crossed_sessions`: 3 / 6 / 12 sessions for a template, the
    80% structured line for a session), because a write that moves no threshold changes no score.

    Returns the registers whose bank must be rebuilt (units entered or left `held`)."""
    changed: set[str] = set()
    tmpl_n: dict[str, int] = {}
    sess_n: dict[str, tuple[int, int]] = {}
    templates = sorted({t for t in templates if t})
    sessions = sorted({s for s in sessions if s})
    if not templates and not sessions:
        return changed
    with store.tx(con):
        for t in templates:
            rows = con.execute(_RESCORE_SQL + "u.template=? AND u.score_json IS NOT NULL", (t,)).fetchall()
            changed |= _rescore_rows(con, rows, tmpl_n, sess_n, kept, group_of)
        for s in sessions:
            rows = con.execute(_RESCORE_SQL + "coalesce(u.session, u.thread_id)=? AND u.score_json IS NOT NULL",
                               (s,)).fetchall()
            changed |= _rescore_rows(con, rows, tmpl_n, sess_n, kept, group_of)
    return {r for r in changed if r}


def rescan_stored(con: sqlite3.Connection, kept, group_of, limit: int | None = None) -> dict:
    """Score the stored units that were never scored (read before this version) from their stored text.

    Explicit (`mannerism review rescan`), never automatic: it can move old units into `held`. There is no typing
    gap for them. A score below LOW holds the text like any other (it is not deleted). Texts scored here are marked
    `legacy` when they still have no origin metadata, so the groups they form can be told apart (`groups.py`).
    Returns {"scored": n, "held": n, "registers": set}."""
    where = "WHERE u.human_score IS NULL" + (f" LIMIT {int(limit)}" if limit else "")
    rows = con.execute(f"SELECT u.unit_id, u.text, u.source, u.account, u.project, u.session_kind, u.session, "
                       f"u.thread_id, coalesce(l.register, u.register) AS reg FROM units u "
                       f"LEFT JOIN labels l ON l.unit_id=u.unit_id {where}").fetchall()
    sigs = {r["unit_id"]: analyse(r["text"]) for r in rows}
    t_sessions: dict[str, set[str]] = {}
    s_counts: dict[str, list[int]] = {}
    for r in rows:
        sg = sigs[r["unit_id"]]
        sess = r["session"] or r["thread_id"] or r["unit_id"]
        if sg.template:
            t_sessions.setdefault(sg.template, set()).add(sess)
        c = s_counts.setdefault(sess, [0, 0])
        c[0] += 1
        c[1] += int(sg.structured)
    # add what scored units already contribute, so a half-scored store gives the same answer as a full one
    for t in t_sessions:
        t_sessions[t] |= {x[0] for x in con.execute(
            "SELECT DISTINCT coalesce(session, thread_id, unit_id) FROM units WHERE template=? AND human_score IS NOT NULL",
            (t,))}
    kept = set(kept)
    updates, regs, held_n = [], set(), 0
    for r in rows:
        sg = sigs[r["unit_id"]]
        sess = r["session"] or r["thread_id"] or r["unit_id"]
        total, structured = s_counts[sess]
        if r["session"] or r["thread_id"]:
            # every text of the session is in the store (scored or not): they all count; the structured ones among the
            # scored texts are in the store's numbers, the ones scored here are in `s_counts`
            total, dbs = session_counts(con, sess)
            structured += dbs
        value, info = score(sg, len(t_sessions.get(sg.template, ())) if sg.template else 0, total, structured)
        if not (r["account"] or r["project"] or r["session_kind"]):
            info["legacy"] = True
        held = int(holds(value) and not (set(group_of({**dict(r), "template": sg.template})) & kept))
        updates.append((sg.template, value, held, json.dumps(info, sort_keys=True), r["unit_id"]))
        if held:
            held_n += 1
            regs.add(r["reg"])
    with store.tx(con):
        for i in range(0, len(updates), 500):
            con.executemany("UPDATE units SET template=?, human_score=?, held=?, score_json=? WHERE unit_id=?",
                            updates[i:i + 500])
    return {"scored": len(rows), "held": held_n, "registers": {r for r in regs if r}}
