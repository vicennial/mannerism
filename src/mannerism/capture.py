"""Edit capture: learn from how the user changes what the agent drafted.

Every draft that goes through `lint` is sanitised (`core.mask_pii`) and logged: all registers, at most 8 KB
per draft, 2000 rows and 4 MB in total, 45 days; re-linting a draft refreshes its time. An edit pair
(draft -> final) is then recorded from three paths:

1. **Ingest match** (`capture_new_units`): after each sync, new own units are compared with drafts logged
   in the 14 days before them. A candidate draft must be in the unit's register (any register when the unit's
   is "?") and within a 0.5 to 2.0 length ratio. The score is the difflib ratio on cleaned text (>= 0.5 to
   match); a thread match adds +0.1 but never creates a match by itself. If the runner-up draft scores within
   0.05 of the best, the match is ambiguous and skipped. Ratio >= 0.9 is `accepted` (the user published the AI
   text as is); 0.5 to 0.9 is `edited`; a unit with ratio >= 0.75 is also kept out of the bank as mostly AI.
   Per-source counts of matched/ambiguous/unmatched units are kept in meta `capture_stats` (shown by status).
2. **Prompt hook** (`match_prompt`): the user's next prompt contains a block close to a recent draft
   (ratio >= 0.6): they pasted their edited version. Must be fast, so candidates are capped and pre-filtered
   by length and `quick_ratio`.
3. **Explicit** (`record_feedback`): the user hands over draft and final.

Pairs feed `rules` (distillation) and the bank (edited finals weigh 1.5x).
"""
from __future__ import annotations

import difflib
import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta

from . import rules, store
from .core import mask_pii

KEEP_DAYS = 45
MAX_ROWS = 2000
MAX_BYTES = 4_000_000
MAX_ITEM_BYTES = 8192  # a longer draft is not logged at all
WINDOW_DAYS = 14  # an own unit can come from a draft logged up to this long before it
MATCH_MIN = 0.5
ACCEPT_MIN = 0.9
THREAD_BONUS = 0.1
AMBIGUOUS_GAP = 0.05
LEN_RATIO = (0.5, 2.0)
MIN_WORDS = 4
AI_RATIO = 0.75  # bank.MOSTLY_AI, duplicated here to avoid a capture <-> bank import cycle concern
AI_MIN_WORDS = 6  # below this, a high ratio is just two short common phrases, not evidence of AI text
DUP_WINDOW_H = 48  # a draft this similar to one logged this recently updates it instead of adding a row
DUP_RATIO = 0.9
DUP_SCAN = 50  # bounded: difflib against the draft log must not grow with its size
PROMPT_MIN = 0.6
PROMPT_MIN_WORDS = 6
PROMPT_WINDOW_H = 48
PROMPT_CANDIDATES = 120
PROMPT_IDENTICAL = 0.98  # a pasted-back draft with no changes carries no edit signal
TEXT_CAP = 3000  # difflib is quadratic; very long texts are compared on their first part

_URL = re.compile(r"https?://\S+")
_LEAD_MENTIONS = re.compile(r"^(?:@\w+(?:\s+|$))+")
_NUMBERING = re.compile(r"^\(?\d{1,2}(?:/\d{1,2})?[/.)]\s+")


def clean(text: str) -> str:
    """Text as compared: masked like stored units, no links or leading @mentions, single spaces."""
    text = _LEAD_MENTIONS.sub("", mask_pii((text or "").strip()))
    return " ".join(_URL.sub(" ", text).split())[:TEXT_CAP]


def segments(text: str) -> list[str]:
    """The whole text plus each blank-line block (one message of a drafted thread), numbering removed."""
    blocks = [_NUMBERING.sub("", b.strip()) for b in re.split(r"\n\s*\n", text or "")]
    out: list[str] = []
    for seg in [text or "", *blocks]:
        seg = clean(seg)
        if len(seg.split()) >= MIN_WORDS and seg not in out:
            out.append(seg)
    return out


def ratio(a: str, b: str, floor: float = 0.0) -> float:
    """difflib ratio of two cleaned, lowercased texts; 0.0 as soon as a cheap bound says it is below `floor`
    or the length ratio is outside LEN_RATIO."""
    la, lb = len(a), len(b)
    if not la or not lb or not LEN_RATIO[0] <= lb / la <= LEN_RATIO[1]:
        return 0.0
    if 2 * min(la, lb) / (la + lb) < floor:
        return 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)  # autojunk off: it wrecks long texts
    if sm.quick_ratio() < floor:
        return 0.0
    return sm.ratio()


def case_edited(draft: str, final: str) -> bool:
    """True when the final keeps text from the draft but changes its capitalisation ("Hi Sam" -> "hi sam").
    Ratios compare lowercased text, so without this a case-only edit reads as `accepted` and a habit such as
    starting lowercase never reaches the rules."""
    a, b = clean(draft), clean(final)
    sm = difflib.SequenceMatcher(None, a.lower(), b.lower(), autojunk=False)
    return any(a[i + k] != b[j + k] for i, j, n in sm.get_matching_blocks() for k in range(n))


def edit_status(draft: str, final: str) -> str:
    """`accepted` when the final is the draft within a few characters and the same capitalisation, else
    `edited`."""
    close = ratio(clean(draft).lower(), clean(final).lower()) >= ACCEPT_MIN
    return "accepted" if close and not case_edited(draft, final) else "edited"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _own(con: sqlite3.Connection | None):
    return (con, False) if con is not None else (store.connect(), True)


# --- draft log -------------------------------------------------------------------------------------------------------

def log_draft(text: str, register: str, origin: str = "mcp", *, target_id: str | None = None,
              con: sqlite3.Connection | None = None, now: datetime | None = None) -> int | None:
    """Remember a draft (PII-masked first) so a later unit, prompt or feedback can be matched to it.

    Returns the draft id, or None when the draft is too short or longer than MAX_ITEM_BYTES.

    A draft logged within DUP_WINDOW_H hours of this one, in the same register, and at least DUP_RATIO
    similar (the agent redrafting the same message slightly differently on a retry) updates that row
    instead of adding a new one: the log should hold distinct drafts, not near-duplicates of the same
    attempt (P1-4)."""
    raw = (text or "").strip()
    if len(raw.encode()) > MAX_ITEM_BYTES * 8:
        return None  # absurdly oversized paste: skip the regex-heavy masking below entirely
    text = mask_pii(raw)
    if len(text.split()) < MIN_WORDS or len(text.encode()) > MAX_ITEM_BYTES:
        return None
    now = now or datetime.now(UTC)
    con, close = _own(con)
    try:
        with store.tx(con):
            cleaned = clean(text).lower()
            since = store.ts_iso(now - timedelta(hours=DUP_WINDOW_H))
            dup_id = None
            for row in con.execute("SELECT draft_id, text FROM drafts WHERE register=? AND ts >= ? "
                                   "ORDER BY ts DESC LIMIT ?", (register, since, DUP_SCAN)):
                if ratio(clean(row["text"]).lower(), cleaned, DUP_RATIO) >= DUP_RATIO:
                    dup_id = row["draft_id"]
                    break
            if dup_id is not None:
                con.execute("UPDATE drafts SET ts=?, origin=?, sha256=?, text=?, "
                            "target_id=coalesce(?, target_id) WHERE draft_id=?",
                            (store.ts_iso(now), origin, _sha(text), text, target_id, dup_id))
                did = dup_id
            else:
                con.execute(
                    "INSERT INTO drafts(ts, register, origin, sha256, text, target_id) VALUES (?,?,?,?,?,?) "
                    "ON CONFLICT(sha256) DO UPDATE SET ts=excluded.ts, origin=excluded.origin, "
                    "register=excluded.register, target_id=coalesce(excluded.target_id, drafts.target_id)",
                    (store.ts_iso(now), register, origin, _sha(text), text, target_id))
                did = con.execute("SELECT draft_id FROM drafts WHERE sha256=?", (_sha(text),)).fetchone()[0]
            con.execute("DELETE FROM drafts WHERE ts < ?", (store.ts_iso(now - timedelta(days=KEEP_DAYS)),))
            con.execute("DELETE FROM drafts WHERE draft_id NOT IN (SELECT draft_id FROM drafts ORDER BY ts DESC, "
                        "draft_id DESC LIMIT ?)", (MAX_ROWS,))
            while con.execute("SELECT coalesce(sum(length(text)), 0) FROM drafts").fetchone()[0] > MAX_BYTES:
                con.execute("DELETE FROM drafts WHERE draft_id = (SELECT draft_id FROM drafts ORDER BY ts, draft_id LIMIT 1)")
        return did if con.execute("SELECT 1 FROM drafts WHERE draft_id=?", (did,)).fetchone() else None
    finally:
        if close:
            con.close()


# --- edit rows -------------------------------------------------------------------------------------------------------

def add_edit(con: sqlite3.Connection, register: str, draft: str, final: str, origin: str, *, unit_id: str | None = None,
             draft_id: int | None = None, score: float | None = None, status: str | None = None,
             thread_key: str | None = None, now: datetime | None = None) -> tuple[int, str, bool]:
    """Insert an edit pair (both texts PII-masked) unless the same (draft, final) is already recorded.

    Returns (edit_id, status, created). A pair first seen via the prompt hook and again when the same text
    arrives through a sync is ONE edit: the second sighting only attaches its unit id. `score` is the raw
    difflib ratio, computed here (P2-14) when the caller does not already have one (e.g. explicit
    feedback), so the stored `ratio` column is never left NULL just because nobody passed it in.
    `thread_key` identifies the thread or task so distillation can demand variety."""
    draft, final = mask_pii(draft), mask_pii(final)
    if score is None:
        score = ratio(clean(draft).lower(), clean(final).lower())
    fsha = _sha(clean(final).lower())
    status = status or edit_status(draft, final)
    row = con.execute("SELECT edit_id, status, unit_id FROM edits WHERE final_sha=? AND draft=?", (fsha, draft)).fetchone()
    if row:
        if unit_id and not row["unit_id"]:
            con.execute("UPDATE edits SET unit_id=? WHERE edit_id=?", (unit_id, row["edit_id"]))
        return row["edit_id"], row["status"], False
    thread_key = thread_key or (f"draft:{draft_id}" if draft_id else "draft:" + _sha(clean(draft).lower())[:12])
    with store.tx(con):
        cur = con.execute(
            "INSERT INTO edits(ts, register, draft, final, status, origin, unit_id, draft_id, ratio, final_sha, "
            "thread_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (store.ts_iso(now or datetime.now(UTC)), register, draft, final, status, origin, unit_id,
             draft_id, round(score, 3), fsha, thread_key))
        rules.apply_contradictions(con, register, final, cur.lastrowid)
    return cur.lastrowid, status, True


def record_feedback(draft: str, final: str, register: str, *, con: sqlite3.Connection | None = None,
                    origin: str = "feedback") -> dict:
    """Explicit edit pair (masked before storage). Returns {edit_id, status, created, register}."""
    con, close = _own(con)
    try:
        eid, status, created = add_edit(con, register, draft, final, origin)
        return {"edit_id": eid, "status": status, "created": created, "register": register}
    finally:
        if close:
            con.close()


# --- ingest match ----------------------------------------------------------------------------------------------------

def _count(counts: dict, source: str, key: str) -> None:
    counts.setdefault(source, {"matched": 0, "ambiguous": 0, "unmatched": 0})[key] += 1


def capture_new_units(con: sqlite3.Connection, unit_ids: list[str], recaptured: Iterable[str] = ()) -> dict:
    """Match new own units against recently logged drafts; record edits. One transaction per unit.

    `recaptured`: units whose text was edited after they were stored (they were counted in `capture_stats` the first
    time, so they are matched again but not counted again).

    A held unit (machine-typed, not yet confirmed as the user's own) is never paired with a draft: the pair would feed
    rules and the bank with text the user may not have written. `groups.its_me` captures the units it releases.

    Returns {"edited", "accepted", "ambiguous", "unmatched", "edit_ids", "registers"}. `registers` lists the
    registers whose bank must be rebuilt (a mostly-AI unit leaves it; an edited one gains weight)."""
    out: dict = {"edited": 0, "accepted": 0, "ambiguous": 0, "unmatched": 0, "edit_ids": [], "registers": []}
    if not unit_ids:
        return out
    oldest = con.execute("SELECT min(ts) FROM drafts").fetchone()[0]
    if not oldest:
        return out
    drafts = [{"id": r["draft_id"], "ts": r["ts"], "register": r["register"], "text": r["text"],
               "target": r["target_id"], "segs": [s.lower() for s in segments(r["text"])]}
              for r in con.execute("SELECT draft_id, ts, register, text, target_id FROM drafts")]
    stats = store.get_meta_json(con, "capture_stats", {}) or {}
    again = set(recaptured)
    regs: set[str] = set()
    ph = ",".join("?" * len(unit_ids))
    units = con.execute(
        f"SELECT u.unit_id, u.source, u.ts, u.text, u.thread_id, u.meta_json, "
        f"coalesce(l.register, u.register) AS reg FROM units u "
        f"LEFT JOIN labels l ON l.unit_id = u.unit_id WHERE u.unit_id IN ({ph}) AND u.ts >= ? AND u.held = 0 "
        f"ORDER BY u.ts",
        [*unit_ids, oldest]).fetchall()
    for u in units:
        if con.execute("SELECT 1 FROM edits WHERE unit_id=?", (u["unit_id"],)).fetchone():
            continue
        final = clean(u["text"]).lower()
        if len(final.split()) < MIN_WORDS:
            continue
        try:
            u_public = bool(json.loads(u["meta_json"] or "{}").get("public"))
        except ValueError:
            u_public = False
        uts = datetime.fromisoformat(u["ts"])
        lo, hi = store.ts_iso(uts - timedelta(days=WINDOW_DAYS)), u["ts"]
        # P1-5: a draft logged in the public register only ever matches a unit the adapter itself marked
        # meta.public (a verified publication) -- never a unit merely LABELLED public by a register label,
        # which decision 3 never treats as public provenance either.
        window = [d for d in drafts if lo <= d["ts"] <= hi and (u["reg"] == "?" or d["register"] == u["reg"])
                  and (d["register"] != "public" or u_public)]
        if not window:
            continue  # nothing was drafted for this unit's register around then: not a capture candidate
        scored = []  # (score with thread bonus, raw ratio, draft) - the best segment of each draft
        for d in window:
            threaded = bool(d["target"] and u["thread_id"] and d["target"] == u["thread_id"])
            floor = MATCH_MIN - (THREAD_BONUS if threaded else 0.0)
            raw = max((ratio(s, final, floor) for s in d["segs"]), default=0.0)
            if raw <= 0.0:
                continue
            eff = min(1.0, raw + (THREAD_BONUS if threaded else 0.0))
            if eff >= MATCH_MIN:
                scored.append((eff, raw, d))
        scored.sort(key=lambda x: (-x[0], x[2]["id"]))
        if not scored:
            if u["unit_id"] not in again:
                _count(stats, u["source"], "unmatched")
            out["unmatched"] += 1
            continue
        # The best RAW ratio across every candidate draft, computed before the ambiguity check below:
        # even when two drafts are too close to call (ambiguous) and no edit is recorded, a unit this
        # close to SOME draft is mostly AI text and must stay out of the bank either way (P1-4). Below
        # AI_MIN_WORDS this is just two short common phrases agreeing by chance, not evidence of anything.
        best_raw = max(raw for _, raw, _ in scored)
        if best_raw >= AI_RATIO and len(final.split()) >= AI_MIN_WORDS:
            con.execute(
                "INSERT INTO ai_units(unit_id, ratio, detected_at) VALUES (?,?,?) "
                "ON CONFLICT(unit_id) DO UPDATE SET ratio=excluded.ratio, detected_at=excluded.detected_at",
                (u["unit_id"], round(best_raw, 3), store.now_iso()))
            regs.add(u["reg"])  # the bank must drop this unit
        if len(scored) > 1 and scored[0][0] - scored[1][0] < AMBIGUOUS_GAP:
            if u["unit_id"] not in again:
                _count(stats, u["source"], "ambiguous")
            out["ambiguous"] += 1
            continue
        eff, raw, d = scored[0]
        eid, status, created = add_edit(
            con, d["register"], d["text"], u["text"], "ingest", unit_id=u["unit_id"], draft_id=d["id"], score=raw,
            status="accepted" if raw >= ACCEPT_MIN and not case_edited(d["text"], u["text"]) else "edited",
            thread_key=u["thread_id"] or d["target"], now=uts)
        if u["unit_id"] not in again:
            _count(stats, u["source"], "matched")
        if created:
            out[status] += 1
            out["edit_ids"].append(eid)
        regs.update({d["register"], u["reg"]})
    store.set_meta_json(con, "capture_stats", stats)
    out["registers"] = sorted(r for r in regs if r and r != "?")
    return out


# --- prompt hook -----------------------------------------------------------------------------------------------------

def prompt_excluded(con: sqlite3.Connection, text: str, cwd: str | None = None, transcript_path: str | None = None,
                    cfg=None) -> bool:
    """Would an exclusion rule drop this prompt? The facts come from the hook payload: the harness and the config
    directory from the layout of `transcript_path`, the project from `cwd`, and the (masked) text. Bounded transcript
    metadata supplies the Claude Code or Codex session kind. If that kind is unavailable, capture is deferred when a
    potentially matching rule depends on it. Slow patterns count as a match (fail closed)."""
    from . import config as config_mod
    from . import exclude, hookroots, humanness
    from .adapters._util import project_of
    from .core import clean_label, home_label

    rules = exclude.all_rules(con, cfg or config_mod.load_config())
    if not rules:
        return False
    ident = hookroots.identify(transcript_path) if transcript_path else None
    project, project_path = project_of(cwd)
    session_kind = None
    if ident:
        if ident[0] == "claude_code":
            from .adapters.claude_code import prompt_session_kind
        else:
            from .adapters.codex import prompt_session_kind
        session_kind = prompt_session_kind(transcript_path)
    facts = exclude.Facts(ident[0] if ident else "", text, home_label(ident[1]) if ident else None,
                          clean_label(project), home_label(project_path), session_kind,
                          humanness.template_of(text)[0])
    if session_kind is None:
        # The hook payload has no kind. If transcript metadata cannot establish it, defer capture whenever a rule whose
        # other known criteria match depends on kind/group; otherwise an IDE "Not me" decision could leak into edits.
        for rule in rules:
            if (rule.session_kind or rule.group) and exclude.Rule(
                    source=rule.source if facts.source else None,
                    account=rule.account if facts.account else None,
                    project=rule.project if facts.project or facts.project_path else None,
                    text=rule.text).matches(facts):
                return True
    return exclude.first_match(rules, facts) is not None


def match_prompt(prompt_text: str, *, con: sqlite3.Connection | None = None, now: datetime | None = None,
                 cwd: str | None = None, transcript_path: str | None = None) -> dict | None:
    """If the prompt contains an edited version of a recent draft, record the pair and return it.

    Used by the UserPromptSubmit hook, so it must be quick and quiet: it returns None for short prompts,
    when nothing matches (or two drafts match about equally well), for a block that is the draft
    unchanged (no information in that), and for a prompt an exclusion rule matches (`prompt_excluded`, using the
    hook payload's `cwd` and `transcript_path`)."""
    prompt_text = mask_pii(prompt_text or "")
    if len(prompt_text.split()) < PROMPT_MIN_WORDS:
        return None
    now = now or datetime.now(UTC)
    con, close = _own(con)
    try:
        if prompt_excluded(con, prompt_text, cwd, transcript_path):
            return None
        since = store.ts_iso(now - timedelta(hours=PROMPT_WINDOW_H))
        rows = con.execute("SELECT draft_id, register, text FROM drafts WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                           (since, PROMPT_CANDIDATES)).fetchall()
        if not rows:
            return None
        # The user's edited text is most likely a block of its own; the whole prompt is only a fallback.
        raw_blocks = [b for b in re.split(r"\n\s*\n", prompt_text) if b.strip()]
        blocks = [clean(_NUMBERING.sub("", b.strip())).lower() for b in raw_blocks]
        blocks = [b for b in blocks if len(b.split()) >= PROMPT_MIN_WORDS]
        whole = clean(prompt_text).lower()
        per_draft = []  # (score, row, block)
        for tier in ([blocks, [whole]] if len(raw_blocks) > 1 else [[whole]]):
            for r in rows:
                best, blk = 0.0, None
                for seg in (s.lower() for s in segments(r["text"])):
                    for b in tier:
                        sc = ratio(seg, b, max(PROMPT_MIN, best))
                        if sc > best:
                            best, blk = sc, b
                if best >= PROMPT_MIN:
                    per_draft.append((best, r, blk))
            if per_draft:
                break
        if not per_draft:
            return None
        per_draft.sort(key=lambda x: -x[0])
        if len(per_draft) > 1 and per_draft[0][0] - per_draft[1][0] < AMBIGUOUS_GAP:
            return None
        best, r, block = per_draft[0]
        final = _original_block(prompt_text, block)
        if best >= PROMPT_IDENTICAL and not case_edited(r["text"], final):
            return None
        eid, status, created = add_edit(con, r["register"], r["text"], final, "prompt", draft_id=r["draft_id"],
                                        score=best, status="edited", now=now)
        return {"edit_id": eid, "status": status, "ratio": round(best, 3), "register": r["register"], "created": created}
    finally:
        if close:
            con.close()


def _original_block(prompt: str, cleaned_lower: str) -> str:
    """The prompt block (original case) whose cleaned form is `cleaned_lower`; the whole prompt as a fallback."""
    for blk in [prompt, *re.split(r"\n\s*\n", prompt)]:
        if clean(_NUMBERING.sub("", blk.strip())).lower() == cleaned_lower:
            return blk.strip()
    return prompt.strip()
