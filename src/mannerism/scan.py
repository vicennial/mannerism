"""`mannerism scan`: what a sync would read, and how much of it looks machine-typed, without storing anything.

It discovers and reads the same sources a full sync would (the enabled ones, plus every zero-setup source that is not
enabled yet, so it works before `init`), through the same adapters, filters and exclusion rules, and groups what it finds by
source x account x project x session kind. It writes nothing: no units, no cursors, no config, no data directory
(the store is only opened read-only to read the exclusion rules and the "it's me" decisions, and not at all when it
does not exist). Cursors live in memory for the duration of the scan. Network sources are skipped unless `network=True`
(and still need their own `allow_network` consent in the config).

Memory is bounded: a scan keeps a few bytes per text (group, template, session and penalty), the samples it shows (a
reservoir of `samples` masked texts per group) and the set of sessions per template up to a cap. Time is bounded by
`budget_s`: the scan stops starting new reads when it is spent and says so.

The result is a plain dict (`--json` prints it as is):

    {"ok": True, "sources": {name: {"status", "locations", "texts", "excluded", "filtered", "held", "errors"}},
     "groups": [{"group_id", "source", "account", "project", "session_kind", "texts", "sessions", "first", "last",
                 "filtered", "held", "flagged_share", "reasons", "samples"}],
     "templates": [{"group_id", "texts", "sessions", "filtered", "held", "samples"}],
     "totals": {...}, "suggestions": [{"group_id", "command", "why"}], "budget_exhausted": bool, "elapsed_s": float}

`texts` counts what would be stored (normal or held); `filtered` counts texts a sync would drop as machine-typed.
"""
from __future__ import annotations

import random
import shlex
import sqlite3
import time
from array import array
from collections import Counter
from dataclasses import dataclass, field

from . import adapters, exclude, groups, humanness
from . import config as config_mod
from . import store as store_mod
from . import sync as sync_mod
from .core import Unit, mask_pii

DEFAULT_SAMPLES = 3
MAX_SAMPLES = 20
DEFAULT_BUDGET_S = 120.0
MAX_ROUNDS_PER_LOCATION = 200
MAX_TEMPLATES = 200_000  # beyond this many distinct templates a scan stops tracking repetition for new ones
TEMPLATE_SESSION_CAP = 64
GROUP_SESSION_CAP = 5000
SUGGEST_MIN_SHARE = 0.5
SUGGEST_MIN_FLAGGED = 5
SUGGEST_MAX = 5
OPT_IN = {"gh", "files"}  # never read without being asked (api.OPT_IN_SOURCES)
PAID = {"x"}  # the official X API bills per read: a scan never calls it, even with --network or --source


@dataclass
class _Group:
    key: tuple
    gid: str
    texts: int = 0
    first: float | None = None
    last: float | None = None
    sessions: set = field(default_factory=set)
    codes: Counter = field(default_factory=Counter)
    seen: int = 0
    samples: list = field(default_factory=list)  # reservoir of any text
    odd: int = 0
    odd_samples: list = field(default_factory=list)  # reservoir of texts that already looked machine-typed when read


@dataclass
class _Template:
    sessions: set = field(default_factory=set)
    texts: int = 0
    sample: str = ""


def _clip(text: str) -> str:
    return groups.mask_sample(mask_pii(text))


def _reservoir(rng: random.Random, bucket: list, seen: int, item: str, k: int) -> None:
    if len(bucket) < k:
        bucket.append(item)
    else:
        j = rng.randrange(seen)
        if j < k:
            bucket[j] = item


class _Tally:
    """Accumulates everything a scan learns, a few bytes per text."""

    def __init__(self, samples: int):
        self.k = max(0, min(int(samples), MAX_SAMPLES))
        self.rng = random.Random(0)  # fixed: the same scan shows the same samples
        self.g: dict[tuple, int] = {}
        self.groups: list[_Group] = []
        self.t: dict[str, int] = {}
        self.templates: list[_Template] = []
        self.s: dict[tuple, int] = {}
        self.s_total = array("I")
        self.s_struct = array("I")
        self.rec_g, self.rec_t, self.rec_s = array("I"), array("i"), array("I")
        self.rec_pen, self.rec_words = array("f"), array("H")
        self.untracked_templates = 0

    def _group(self, u: Unit) -> int:
        key = (u.source, u.account or "", u.project or "", u.session_kind or "")
        i = self.g.get(key)
        if i is None:
            i = self.g[key] = len(self.groups)
            self.groups.append(_Group(key, groups.origin_group_id(*key)))
        return i

    def _template(self, tmpl: str | None) -> int:
        if not tmpl:
            return -1
        i = self.t.get(tmpl)
        if i is None:
            if len(self.templates) >= MAX_TEMPLATES:
                self.untracked_templates += 1
                return -1
            i = self.t[tmpl] = len(self.templates)
            self.templates.append(_Template())
        return i

    def add(self, u: Unit, sg: humanness.Signals) -> bool:
        """Record one text. Returns True when it already looks machine-typed from what is known so far."""
        gi = self._group(u)
        g = self.groups[gi]
        sess_key = (u.source, u.session or u.thread_id or u.unit_id)
        si = self.s.get(sess_key)
        if si is None:
            si = self.s[sess_key] = len(self.s_total)
            self.s_total.append(0)
            self.s_struct.append(0)
        self.s_total[si] += 1
        self.s_struct[si] += int(sg.structured)
        ti = self._template(sg.template)
        sess_id = hash(sess_key)
        if ti >= 0:
            tp = self.templates[ti]
            tp.texts += 1
            if len(tp.sessions) < TEMPLATE_SESSION_CAP:
                tp.sessions.add(sess_id)
            if not tp.sample and self.k:
                tp.sample = _clip(u.text)
        g.texts += 1
        ts = u.ts.timestamp()
        g.first = ts if g.first is None else min(g.first, ts)
        g.last = ts if g.last is None else max(g.last, ts)
        if len(g.sessions) < GROUP_SESSION_CAP:
            g.sessions.add(sess_id)
        for c in sg.codes:
            g.codes[c] += 1
        n_t = len(self.templates[ti].sessions) if ti >= 0 else 0
        odd = bool(sg.base) or humanness.template_penalty(n_t, sg.words) > 0
        if self.k:
            g.seen += 1
            _reservoir(self.rng, g.samples, g.seen, _clip(u.text), self.k)
            if odd:
                g.odd += 1
                _reservoir(self.rng, g.odd_samples, g.odd, _clip(u.text), self.k)
        self.rec_g.append(gi)
        self.rec_t.append(ti)
        self.rec_s.append(si)
        self.rec_pen.append(sg.base)
        self.rec_words.append(min(sg.words, 65535))
        return odd

    def finish(self, kept: set[str]) -> tuple[list[dict], list[dict]]:
        """Final scores (every text against the complete counts): per-group and per-template numbers."""
        n = len(self.rec_g)
        by_id = {v: k for k, v in self.t.items()}  # template index -> template id
        filtered = [0] * len(self.groups)
        held = [0] * len(self.groups)
        t_filtered = [0] * len(self.templates)
        t_held = [0] * len(self.templates)
        for i in range(n):
            gi, ti, si = self.rec_g[i], self.rec_t[i], self.rec_s[i]
            tmpl = None
            n_t = 0
            if ti >= 0:
                tmpl = "x"  # only its presence matters to humanness.score
                n_t = len(self.templates[ti].sessions)
            sg = humanness.Signals(base=float(self.rec_pen[i]), template=tmpl, words=self.rec_words[i])
            value, _ = humanness.score(sg, n_t, self.s_total[si], self.s_struct[si])
            gid = self.groups[gi].gid
            keep = gid in kept or (ti >= 0 and groups.template_group_id(by_id[ti]) in kept)
            if keep:  # "it's me": stored as normal text, never dropped or held (as a sync does)
                continue
            if humanness.is_dropped(value):
                filtered[gi] += 1
                if ti >= 0:
                    t_filtered[ti] += 1
            elif humanness.holds(value):
                held[gi] += 1
                if ti >= 0:
                    t_held[ti] += 1
        out_groups = []
        for gi, g in enumerate(self.groups):
            texts = g.texts - filtered[gi]
            total = g.texts
            reasons = {c: v for c, v in g.codes.most_common(5)}
            samples = list(g.odd_samples)
            samples += [s for s in g.samples if s not in samples]
            out_groups.append({
                "group_id": g.gid, "source": g.key[0], "account": g.key[1] or None, "project": g.key[2] or None,
                "session_kind": g.key[3] or None, "texts": texts, "sessions": len(g.sessions),
                "first": _iso(g.first), "last": _iso(g.last), "filtered": filtered[gi], "held": held[gi],
                "flagged_share": round((filtered[gi] + held[gi]) / total, 3) if total else 0.0,
                "reasons": reasons, "samples": samples[:self.k]})
        out_t = []
        for ti, tp in enumerate(self.templates):
            if (len(tp.sessions) >= groups.TEMPLATE_GROUP_MIN_SESSIONS and tp.texts >= groups.TEMPLATE_GROUP_MIN_UNITS):
                out_t.append({"group_id": groups.template_group_id(by_id[ti]), "texts": tp.texts - t_filtered[ti],
                              "sessions": len(tp.sessions), "filtered": t_filtered[ti], "held": t_held[ti],
                              "samples": [tp.sample] if tp.sample else []})
        return out_groups, out_t


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    from datetime import UTC, datetime
    return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()


# --- read-only access to the store's rules ----------------------------------------------------------------------------

def _readonly_rules_and_decisions() -> tuple[list[exclude.Rule], set[str], list[str], list[str]]:
    """(store rules, kept group ids, rule errors, notes). Opens the store without ever creating a file.

    - no store: no rules;
    - a write-ahead log WITH its index file (`-shm`): opened read-only (`mode=ro`), so what the last writers committed
      is seen;
    - a log WITHOUT its index file (the writer crashed, or the files were copied): `mode=ro` would have SQLite
      create and rebuild the index, so the store is opened `immutable` instead, which ignores the log. Rules and
      decisions that are only in the log are then missed, and the scan says so;
    - no log: `immutable`, which takes no lock and creates no sidecar."""
    path = store_mod.db_path()
    if not path.exists():
        return [], set(), [], []
    wal = path.with_name(path.name + "-wal")
    shm = path.with_name(path.name + "-shm")
    notes: list[str] = []
    if wal.exists() and shm.exists():
        uri = f"{path.as_uri()}?mode=ro"
    else:
        uri = f"{path.as_uri()}?immutable=1"
        if wal.exists():
            notes.append("the store has a write-ahead log without its index file, so the scan read it without the log: "
                         "exclusion rules and decisions saved only there were not applied (run `mannerism status` once "
                         "to let the store settle, then scan again)")
    try:
        con = sqlite3.connect(uri, uri=True, timeout=3)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA query_only=ON")
            try:
                rules, errors = exclude.store_rules_checked(con)
            except sqlite3.OperationalError:  # a store from before v4: no rules table yet
                rules, errors = [], []
            return rules, groups.kept_groups(con), errors, notes
        finally:
            con.close()
    except sqlite3.Error as e:
        return [], set(), [], [*notes, f"could not read the stored exclusion rules ({e}); only config.toml rules were applied"]


# --- the scan --------------------------------------------------------------------------------------------------------

def _source_names(cfg, sources: list[str] | None, network: bool) -> list[str]:
    """The sources to scan: the ones named, else the enabled ones plus every zero-setup source that is not configured at
    all. A source the config switches off (for example after `forget --source`) is only scanned when it is named."""
    if sources:
        return list(sources)
    names = list(cfg.enabled_sources())
    for name in adapters.REGISTRY:
        if name in names or name in OPT_IN or name in cfg.sources:  # in cfg.sources and not enabled: switched off
            continue
        try:
            ad = adapters.load(name)
        except (ImportError, AttributeError, KeyError):
            continue
        if getattr(ad, "tier", None) == "auto" and (network or not getattr(ad, "network", False)):
            names.append(name)
    return names


def scan(*, sources: list[str] | None = None, samples: int = DEFAULT_SAMPLES, network: bool = False,
         budget_s: float | None = DEFAULT_BUDGET_S) -> dict:
    t0 = time.monotonic()
    cfg = config_mod.load_config()
    me = cfg.me
    if not (me.names or me.emails or me.handles or me.phone_suffixes):
        me = config_mod.identity_guess()  # read-only: `git config` lookups, written nowhere
    store_rules, kept, store_errors, notes = _readonly_rules_and_decisions()
    cfg_rules, rule_errors = exclude.config_rules(cfg)
    rule_errors = [*rule_errors, *store_errors]
    rules = cfg_rules + store_rules
    tally = _Tally(samples)
    report: dict[str, dict] = {}
    exhausted = False

    def out_of_time() -> bool:
        return budget_s is not None and time.monotonic() - t0 >= budget_s

    for name in _source_names(cfg, sources, network):
        entry = cfg.sources.get(name) or {}
        is_plugin = bool(entry.get("plugin"))
        rep = report[name] = {"status": "ok", "locations": 0, "texts": 0, "excluded": 0, "filtered": 0, "held": 0,
                              "errors": []}
        try:
            adapter = adapters.load_for(name, entry, warnings=[])
        except Exception as e:  # noqa: BLE001 - a broken adapter must not stop the scan
            rep.update(status="adapter_missing", errors=[f"{type(e).__name__}: {e}"])
            continue
        if name in PAID:
            rep.update(status="skipped_paid", note="X was skipped: its API bills for every read, so a scan never calls it "
                                                   "(`mannerism sync` reads it when you have enabled it)")
            continue
        if getattr(adapter, "network", False):
            if not network:
                rep["status"] = "skipped_network"
                continue
            if entry.get("allow_network") is not True:
                rep.update(status="skipped_no_consent",
                           note=f"needs your OK to use the network: run `mannerism sources enable {name}`")
                continue
        try:
            locations = list(adapter.discover(me, cfg.for_adapter(name)))
        except Exception as e:  # noqa: BLE001
            rep.update(status="error", errors=[f"discover: {type(e).__name__}: {e}"])
            continue
        for loc in locations:
            if out_of_time():
                exhausted = True
                break
            rep["locations"] += 1
            cursor: dict | None = None
            for _round in range(MAX_ROUNDS_PER_LOCATION):
                try:
                    units_iter, cursor = adapter.read(loc, cursor, me)
                    units = list(units_iter)
                except Exception as e:  # noqa: BLE001
                    rep["errors"].append(f"{loc.key}: {type(e).__name__}: {e}"[:300])
                    rep["status"] = "error"
                    break
                cursor = dict(cursor or {})
                for u in units:
                    if is_plugin:  # third-party code: the same masking and labels a sync gives its units
                        sync_mod._sanitize_plugin_unit(u)
                    try:
                        u.validate()
                    except ValueError:
                        continue
                    sg = humanness.analyse(u.text, u.gap_s)
                    u.template = sg.template
                    if rules and exclude.first_match(rules, exclude.facts_of_unit(u)):
                        rep["excluded"] += 1
                        continue
                    tally.add(u, sg)
                    rep["texts"] += 1
                if not cursor.get("more"):
                    break
                if out_of_time():
                    exhausted = True
                    break
    group_list, template_list = tally.finish(kept)
    for g in group_list:
        r = report.get(g["source"])
        if r is not None:
            r["filtered"] += g["filtered"]
            r["held"] += g["held"]
            r["texts"] -= g["filtered"]
    group_list.sort(key=lambda g: (-(g["filtered"] + g["held"]), -g["texts"], g["group_id"]))
    template_list.sort(key=lambda g: (-(g["filtered"] + g["held"]), -g["texts"], g["group_id"]))
    suggestions = _suggestions(group_list, template_list)
    totals = {"texts": sum(r["texts"] for r in report.values()), "filtered": sum(r["filtered"] for r in report.values()),
              "held": sum(r["held"] for r in report.values()), "excluded": sum(r["excluded"] for r in report.values()),
              "groups": len(group_list)}
    res = {"ok": True, "sources": report, "groups": group_list, "templates": template_list, "totals": totals,
           "suggestions": suggestions, "budget_exhausted": exhausted, "elapsed_s": round(time.monotonic() - t0, 2)}
    if rule_errors:
        res["exclude_errors"] = rule_errors
    if notes:
        res["note"] = "; ".join(notes)
    if tally.untracked_templates:
        res["untracked_templates"] = tally.untracked_templates
    return res


def _describe(g: dict) -> str:
    if g["group_id"].startswith("t_"):
        return f"{g['texts'] + g['filtered']} repeated texts in {g['sessions']} sessions"
    where = " ".join(x for x in (g["source"], g.get("account"), g.get("project"), g.get("session_kind")) if x)
    return f"{where} ({g['texts']} texts)"


def _suggestions(group_list: list[dict], template_list: list[dict]) -> list[dict]:
    """Commands that would exclude the noisiest groups: mostly flagged, and at least SUGGEST_MIN_FLAGGED flagged texts.

    An orchestrator often starts every session in a new working directory, which splits it into many tiny groups; so an
    account whose texts are mostly flagged is suggested as a whole (`--source S --account A`), and its groups are not
    suggested one by one."""
    def noisy(flagged: int, total: int) -> bool:
        return flagged >= SUGGEST_MIN_FLAGGED and total > 0 and flagged / total >= SUGGEST_MIN_SHARE

    accounts: dict[tuple[str, str], list[dict]] = {}
    for g in group_list:
        if g["account"]:
            accounts.setdefault((g["source"], g["account"]), []).append(g)
    pool: list[tuple[int, str, str, str]] = []  # (flagged, sort id, command, why)
    covered: set[str] = set()
    for (source, account), gs in accounts.items():
        flagged = sum(g["filtered"] + g["held"] for g in gs)
        total = sum(g["texts"] + g["filtered"] for g in gs)
        if len(gs) > 1 and noisy(flagged, total):
            covered |= {g["group_id"] for g in gs}
            pool.append((flagged, f"{source}\x00{account}",
                         f"mannerism exclude add --source {shlex.quote(source)} --account {shlex.quote(exclude.glob_escape(account))}",
                         f"{source} {account} ({total} texts in {len(gs)} groups): {flagged} look machine-typed"))
    for g in [*group_list, *template_list]:
        flagged = g["filtered"] + g["held"]
        if g["group_id"] not in covered and noisy(flagged, g["texts"] + g["filtered"]):
            pool.append((flagged, g["group_id"], f"mannerism exclude add --group {g['group_id']}",
                         f"{_describe(g)}: {flagged} look machine-typed"))
    pool.sort(key=lambda p: (-p[0], p[1]))
    return [{"group_id": sid if sid.startswith(("g_", "t_")) else None, "command": cmd, "why": why}
            for _flagged, sid, cmd, why in pool[:SUGGEST_MAX]]


# --- text output -----------------------------------------------------------------------------------------------------

def format_text(res: dict, top: int = 25) -> str:
    """The scan as readable text: source -> account -> project, then the commands to run next."""
    lines: list[str] = []
    t = res["totals"]
    lines.append(f"Scanned {t['texts']} texts ({t['held']} to review, {t['filtered']} filtered as machine-typed, "
                 f"{t['excluded']} excluded by your rules) in {res['elapsed_s']} s. Nothing was stored.")
    if res.get("budget_exhausted"):
        lines.append("The scan stopped on its time budget: counts are partial. Raise it with --budget.")
    for note in (res.get("note"), *(res.get("exclude_errors") or [])):
        if note:
            lines.append(f"note: {note}")
    by_source: dict[str, list[dict]] = {}
    for g in res["groups"]:
        by_source.setdefault(g["source"], []).append(g)
    for name, rep in res["sources"].items():
        gs = by_source.get(name, [])
        if rep["status"] != "ok" and not gs:
            extra = rep.get("note") or "; ".join(rep["errors"][:1])
            lines.append("")
            lines.append(f"{name}: {rep['status'].replace('_', ' ')}" + (f" ({extra})" if extra else ""))
            continue
        if not gs:
            continue
        first = min((g["first"] for g in gs if g["first"]), default=None)
        last = max((g["last"] for g in gs if g["last"]), default=None)
        flagged = rep["filtered"] + rep["held"]
        share = flagged / (rep["texts"] + rep["filtered"]) if (rep["texts"] + rep["filtered"]) else 0
        lines.append("")
        lines.append(f"{name}: {rep['texts']} texts, {first or '?'} to {last or '?'}, "
                     f"{round(share * 100)}% flagged as machine-typed")
        by_account: dict[str, list[dict]] = {}
        for g in gs:
            by_account.setdefault(g["account"] or "-", []).append(g)
        shown = 0
        for account, ags in sorted(by_account.items(), key=lambda kv: -sum(g["texts"] for g in kv[1])):
            lines.append(f"  account {account}")
            for g in ags:
                if shown >= top:
                    break
                shown += 1
                bits = [f"{g['texts']} texts", f"{g['sessions']} sessions", f"{g['first']} to {g['last']}"]
                if g["session_kind"]:
                    bits.append(g["session_kind"])
                if g["held"] or g["filtered"]:
                    bits.append(f"{round(g['flagged_share'] * 100)}% machine-typed")
                lines.append(f"    project {g['project'] or '-'}  [{g['group_id']}]  " + ", ".join(bits))
                for s in g["samples"]:
                    lines.append(f"      > {s}")
        more = len(gs) - shown
        if more > 0:
            lines.append(f"  ... and {more} more group(s); `--top N` shows more, `--json` has them all")
    if res["templates"]:
        lines.append("")
        lines.append("Repeated openings (the same words in many sessions):")
        for g in res["templates"][:top]:
            lines.append(f"  [{g['group_id']}]  {g['texts']} texts in {g['sessions']} sessions"
                         + (f", {g['held'] + g['filtered']} flagged" if g["held"] or g["filtered"] else ""))
            for s in g["samples"]:
                lines.append(f"      > {s}")
    lines.append("")
    if res["suggestions"]:
        lines.append("Not you? These would leave the noisiest groups out (run any, copy as needed):")
        for s in res["suggestions"]:
            lines.append(f"  {s['command']}    # {s['why']}")
        lines.append("")
    lines.append("When the list looks right: `mannerism init` to set up, or `mannerism sync` to read.")
    return "\n".join(lines)
