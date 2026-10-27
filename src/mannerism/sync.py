"""Ingestion: read every enabled source from its cursor, store units, then update everything derived.

Per source: `adapters.load(name)`, `discover(me, config)` for locations, then for each location
`read(loc, cursor, me)`. A location's units and its new cursor are written in ONE transaction
(`store.write_units`), so a crash or an adapter error mid-location advances nothing and the next sync
re-reads it. Adapter modules that do not exist yet, and adapters that raise, are recorded in the result
and `meta` and never stop the other sources.

`config` handed to `discover` is `Config.for_adapter(name)`: every parsed table plus the keys of
`[sources.<name>]` flattened to the top level.

`quick=True` (SessionStart hook): only `tier == "auto"` sources and a 5 s budget. The budget only stops
*starting* new locations; post-processing for what was ingested always runs (it happens in a detached
process, see `sync_in_background`). A quick sync never runs a network source and never imports a plugin; when
one is due it only starts a full background sync for it, after releasing the lock. Exactly one sync runs at a
time: a non-blocking flock on `sync.lock`; a second caller returns {"status": "busy"} at once.

Scheduled sources (network adapters and plugins, see `schedule.py`) run in a full sync only when due; `force`
ignores the due time (not the `allow_network` consent and not the budget). A network adapter (class attribute
`network = True`) also needs `allow_network = true` in its `[sources.<name>]` table. Besides units, an adapter's
cursor may carry three engine-only keys, removed before it is stored: `_stale_prune` ({thread_id: [unit_id]}),
`_delete_refs` (source_refs whose units and derived rows are deleted) and `_error` (a `file:line:reason` string
to report after the units and cursor were committed). A network adapter that does not raise reports a failed read by
returning its cursor with `status = {"error": str, "at": iso, "retry_after"?: iso}`: the run counts as failed
(backoff, never earlier than `retry_after`) and the cursor is stored as returned.

Before a unit is stored `intake.py` applies the exclusion rules (`exclude.py`: a matching unit is dropped and its stored
copy deleted) and the machine-typed screen (`humanness.py`: dropped, held for review, or normal); both are counted in the
report (`excluded`, `filtered`, `held`).

A file sync (`files=`, the Stop / SessionEnd hooks) only reads the named files, only for sources that are switched on in
the config as it is once the lock is held, and by default only stores and screens: the work below is left for the next
normal sync (the store is marked `refresh_pending`) unless `refresh=True` (SessionEnd) asks for it.

After ingest: features for new units, capture (new units vs the draft log), bank + stats + stats cards for the
registers that changed, drift flags, and distill/card packets queued.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import warnings
from datetime import UTC, datetime

from . import adapters, capture, cards, core, exclude, intake, packets, refresh, schedule, store, stylometrics
from .config import ensure_home, load_config

QUICK_BUDGET_S = 5.0
MAX_ROUNDS_PER_LOCATION = 200  # x the adapter's per-read byte budget: a first sync catches up big transcripts
STALE_AFTER_S = 6 * 3600
LOCK_WAIT_S = 10.0  # how long a blocking caller (forget) waits for the sync lock before giving up
FILE_SYNC_LOCK_WAIT_S = 20.0  # a single-file sync (end-of-session hook) retries a busy lock for this long
FILE_SYNC_HARD_LIMIT_S = 60  # ... and the whole detached process is killed after this (a rolled-back transaction loses nothing)

try:
    import fcntl
except ImportError:  # Windows: no flock; best effort (see docs/design.md §11)
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

_WARNED_NO_LOCK = False


def lock_path():
    return core.home() / "sync.lock"


def _try_lock(fd: int) -> bool:
    """Non-blocking attempt at the exclusive sync lock. True on success."""
    global _WARNED_NO_LOCK
    if fcntl is not None:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False
    if msvcrt is not None:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    if not _WARNED_NO_LOCK:
        warnings.warn("mannerism: no file-locking primitive on this platform; concurrent syncs are not guarded",
                      RuntimeWarning, stacklevel=2)
        _WARNED_NO_LOCK = True
    return True  # no-op: never blocks, but offers no real exclusion guarantee


def _unlock(fd: int) -> None:
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
    elif msvcrt is not None:
        try:
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass


def acquire_lock(timeout_s: float = LOCK_WAIT_S) -> int | None:
    """Block up to `timeout_s` for the sync lock (used by `forget`, which must not run beside a sync).

    Returns the open fd to pass to `release_lock`, or None if it timed out."""
    ensure_home()
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout_s
    while not _try_lock(fd):
        if time.monotonic() >= deadline:
            os.close(fd)
            return None
        time.sleep(0.05)
    return fd


def release_lock(fd: int | None) -> None:
    if fd is None:
        return
    try:
        _unlock(fd)
    finally:
        os.close(fd)


def _real(path) -> str:
    try:
        return os.path.realpath(path)
    except (OSError, ValueError):
        return str(path)


def sync(quick: bool = False, sources: list[str] | None = None, budget_s: float | None = None,
         force: bool = False, files: list[str] | None = None, refresh: bool = False) -> dict:
    """Run one sync. See the module docstring. Returns a JSON-serialisable report.

    `files` (the hooks): only the locations whose real path is one of these are read, and only sources that are
    switched on in the config as it is once the lock is held; the run leaves `last_sync` and the `forget --all` marker
    alone, so it never makes a full sync look recent. It stores and screens the texts and nothing more: the bank,
    statistics, cards, edit capture and the work queue wait for the next normal sync (or a file sync with `refresh`,
    the end-of-session hook), and the store is marked "refresh pending" meanwhile."""
    ensure_home()
    fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    if not _try_lock(fd):
        os.close(fd)
        return {"status": "busy", "message": "another sync is already running"}
    try:
        res = _sync_locked(quick, sources, budget_s, force, files, refresh)
    finally:
        try:
            _unlock(fd)
        finally:
            os.close(fd)
    due = res.pop("background_due", None)
    if due:  # after the lock is released: the background sync needs it
        res["background"] = sync_in_background(quick=False, sources=due)
    return res


def watch(every_s: float = 60.0, *, sources: list[str] | None = None, iterations: int | None = None,
          sleep=time.sleep, report=None) -> int:
    """Run a full sync every `every_s` seconds until interrupted (or `iterations` runs). Returns the run count.

    Scheduled sources still follow their own intervals; the loop only decides how often to look. Config is read
    again on every run, so a source added while watching is picked up. No scheduler is installed anywhere."""
    runs = 0
    while True:
        res = sync(quick=False, sources=sources)
        runs += 1
        if report is not None:
            report(res)
        if iterations is not None and runs >= iterations:
            return runs
        sleep(every_s)


def _effective_registers(con, unit_ids: list[str]) -> set[str]:
    regs: set[str] = set()
    for i in range(0, len(unit_ids), 500):
        part = unit_ids[i:i + 500]
        regs.update(r[0] for r in con.execute(
            f"SELECT DISTINCT coalesce(l.register, u.register) FROM units u LEFT JOIN labels l ON l.unit_id=u.unit_id "
            f"WHERE u.unit_id IN ({','.join('?' * len(part))})", part))
    return {r for r in regs if r in core.REGISTERS}


def _read_location(con, adapter, name, loc, cfg, rep, new_ids, touched, gone, untrusted=False, *,
                   screen=None) -> bool:
    """Read one location once and write its units + cursor. True when it is caught up (or failed).

    `screen` is the sync's `intake.Intake` (exclusion rules and machine-typed screening); built here when a caller
    that reads one location does not pass one, so nothing is ever stored unscreened."""
    screen = screen or intake.Intake(con, cfg)
    try:
        since = cfg.sources.get(name, {}).get("since")
        if since is not None:
            since = datetime.fromisoformat(str(since).replace("Z", "+00:00"))
            if since.tzinfo is None:
                raise ValueError("source since must include a timezone")
        units_iter, cursor = adapter.read(loc, store.get_cursor(con, name, loc.key), cfg.me)
        # Materialised in one go: write_units needs the whole batch for its one-transaction
        # guarantee (units + cursor commit together). For an adapter that could yield an
        # unbounded number of units this is a memory tradeoff accepted for now; adapters are
        # expected to bound themselves per read() call (byte/row budgets) rather than sync
        # streaming writes.
        units = list(units_iter)
        if since is not None:
            # A migration boundary skips old history under a new adapter identity.
            # Cursor progress is retained; this is not a request to erase old data.
            units = [u for u in units if u.ts > since]
        if untrusted:  # a plugin is third-party code: the engine masks and never believes a "public" flag
            for u in units:
                _sanitize_plugin_unit(u)
        cursor = dict(cursor or {})
        prune = cursor.pop("_stale_prune", None)
        delete_refs = cursor.pop("_delete_refs", None)
        problem = cursor.pop("_error", None)
        taken = screen.apply(units)
        units = taken.units
        if taken.excluded_refs:  # an excluded text must not stay stored from an earlier version of it
            delete_refs = [*(delete_refs or []), *taken.excluded_refs]
        res = store.write_units(con, name, loc.key, units, cursor, prune=prune, delete_refs=delete_refs)
    except Exception as e:  # noqa: BLE001 - one bad location must not stop the sync
        msg = f"{loc.key}: {type(e).__name__}: {e}"[:300]
        rep["errors"].append(msg)
        rep["status"] = "error"
        store.record_run_error(con, name, loc.key, msg)
        return True
    rep["new_units"] += len(res.new_ids)
    rep["updated_units"] += len(res.updated_ids)
    for k, v in (("excluded", taken.excluded), ("filtered", taken.filtered), ("held", taken.held)):
        rep[k] = rep.get(k, 0) + v
    try:  # the units are committed: counts moved, so stored units may enter or leave `held` (a failure only delays that)
        gone["registers"] |= screen.after_write(con, units)
    except Exception as e:  # noqa: BLE001
        rep["errors"].append(f"{loc.key}: rescoring held texts: {type(e).__name__}: {e}"[:300])
    rep["rejected"] += res.rejected
    new_ids += res.new_ids
    touched += res.new_ids + res.updated_ids
    gone["edited"] += res.edited_ids  # text changed after it was stored: capture pairs the new text again
    gone["kept_drafts"] += res.kept_draft_ids
    if res.deleted_ids:  # explicit deletes: the full drop (agent cards, eval pages, leak index)
        rep["deleted_units"] += len(res.deleted_ids)
        gone["n"] += len(res.deleted_ids)
        gone["sources"].add(name)
    if res.purged_ids:  # edited text / pruned chunks: unit-level data is purged, agent cards only go stale
        gone["stale"].add(name)
    gone["registers"] |= res.deleted_registers  # the registers units left: their bank is rebuilt
    soft = cursor.get("status")  # network adapters report a failed read in the cursor instead of raising
    if not problem and isinstance(soft, dict) and soft.get("error"):
        problem = soft["error"]
        retry = schedule.parse(soft.get("retry_after"))
        if retry and (not rep.get("retry_after") or retry > schedule.parse(rep["retry_after"])):
            rep["retry_after"] = retry.isoformat(timespec="seconds")
    if problem:  # the units and cursor above are committed; the source stopped at this problem
        msg = str(problem)[:300]
        rep["errors"].append(msg)
        rep["status"] = "error"
        store.record_run_error(con, name, loc.key, msg)
        return True
    return not cursor.get("more")


def _sanitize_plugin_unit(u) -> None:
    u.text = core.mask_pii(u.text)
    u.context = tuple(core.ContextLine(c.who, core.mask_pii(c.text)) for c in u.context)
    u.account, u.project, u.session_kind = (core.clean_label(u.account), core.clean_label(u.project),
                                            core.clean_label(u.session_kind))
    u.project_path = core.clean_label(u.project_path)
    if "public" in u.meta:  # only built-in adapters that verified a publication may set it
        u.meta = {k: v for k, v in u.meta.items() if k != "public"}


def _invalidate_after_delete(con, sources: set[str]) -> list[str]:
    """Deleted units can be quoted in agent cards, test pages and the leak index: drop what was built on them
    (the same reach as `forget --source`). Returns the registers whose agent card was dropped."""
    stale = []
    for r in core.REGISTERS:
        if sources & set(store.get_meta_json(con, f"card_sources:{r}", []) or []):
            cards.drop_agent_card(r)
            store.set_meta(con, f"card_needed:{r}", "1")
            store.set_meta(con, f"card_sources:{r}", None)
            stale.append(r)
    shutil.rmtree(core.home() / "eval", ignore_errors=True)  # test pages quote stored text
    return stale


def _mark_cards_stale(con, sources: set[str]) -> list[str]:
    """Edited text was part of what an agent card was written from. Keep the card (the agent rewrites it at a cost)
    and mark it stale: `card_needed` makes status say "upgrade available" and queues a new card packet."""
    marked = []
    for r in core.REGISTERS:
        if sources & set(store.get_meta_json(con, f"card_sources:{r}", []) or []):
            store.set_meta(con, f"card_needed:{r}", "1")
            marked.append(r)
    return marked


def _finish_source(con, name: str, rep: dict, entry: dict, adapter, scheduled: bool) -> None:
    """Record the run in the source's schedule state (success, or failure with backoff for scheduled sources)."""
    if rep["status"] in ("skipped_quick", "skipped_not_auto", "skipped_no_consent", "not_due"):
        return
    if rep["status"] == "ok" and not rep["errors"]:
        if rep["skipped_locations"] and not rep["locations"]:
            return  # the time budget was spent before it could start: neither a success nor a failure
        schedule.record_success(con, name, schedule.interval_s(entry, adapter))
    else:
        schedule.record_failure(con, name, schedule.interval_s(entry, adapter),
                                (rep["errors"] or [rep["status"]])[0], backoff=scheduled,
                                retry_after=schedule.parse(rep.get("retry_after")))


PENDING_KEY = "refresh_pending"
PENDING_CAP = 20_000  # ids kept per list; a pending refresh beyond this loses edit capture for the overflow, not the texts


def load_pending(con) -> dict:
    """What file syncs ingested without processing: {"new", "edited", "kept_drafts", "registers", "stale"}."""
    raw = store.get_meta_json(con, PENDING_KEY, {}) or {}
    return {k: list(raw.get(k) or []) for k in ("new", "edited", "kept_drafts", "registers", "stale")}


def refresh_pending(con=None) -> bool:
    """True when file syncs stored texts that the bank and statistics do not know yet."""
    own = con is None
    con = con or store.connect()
    try:
        return bool(store.get_meta(con, PENDING_KEY))
    finally:
        if own:
            con.close()


def _save_pending(con, pend: dict) -> None:
    empty = not any(pend.values())
    store.set_meta_json(con, PENDING_KEY, None if empty else
                        {k: list(dict.fromkeys(v))[-PENDING_CAP:] for k, v in pend.items() if v})


def _sync_locked(quick: bool, sources: list[str] | None, budget_s: float | None, force: bool = False,
                 files: list[str] | None = None, refresh_now: bool = False) -> dict:
    t0 = time.monotonic()
    budget = budget_s if budget_s is not None else (QUICK_BUDGET_S if quick else None)
    cfg = load_config()
    con = store.connect()
    # Reaching this point at all means an explicit sync was requested (CLI `sync`/`sync --background`,
    # `init`, or the detached subprocess *they* started) rather than an automatic one (the SessionStart
    # hook and the lazy MCP path check `forget_all_pending()` before ever calling in here): decision 1
    # says a `forget --all` stays quiet until the user explicitly asks for a sync again, and this is that
    # explicit ask.
    if files is None:
        store.set_meta(con, "forget_all_at", None)
    elif store.get_meta(con, "forget_all_at"):  # a partial sync is automatic too: stay quiet after `forget --all`
        con.close()
        return {"status": "skipped", "reason": "forgotten_all"}
    only = {_real(f) for f in files} if files is not None else None
    names = list(sources) if sources else cfg.enabled_sources()
    if only is not None:  # a hook never brings back a source the config switches off (checked with the lock held)
        enabled = set(cfg.enabled_sources())
        names = [n for n in names if n in enabled]
    report: dict[str, dict] = {}
    new_ids: list[str] = []
    touched: list[str] = []
    gone: dict = {"n": 0, "registers": set(), "sources": set(), "stale": set(), "edited": [], "kept_drafts": []}
    background: list[str] = []  # scheduled sources a quick sync found due: a full sync is started for them
    out_of_budget = False
    try:
        retro = exclude.apply_config_rules(con, cfg)  # [[exclude]] tables changed: delete what they match, once
        if retro:
            gone["n"] += len(retro["ids"])
            gone["registers"] |= retro["registers"]
            gone["sources"] |= retro["sources"]
        screen = intake.Intake(con, cfg)
        pend = load_pending(con)
        for name in names:
            rep = report[name] = {"status": "ok", "locations": 0, "new_units": 0, "updated_units": 0,
                                  "deleted_units": 0, "rejected": 0, "errors": [], "skipped_locations": 0,
                                  "excluded": 0, "filtered": 0, "held": 0}
            entry = cfg.sources.get(name) or {}
            is_plugin = bool(entry.get("plugin"))
            if quick and is_plugin:  # never imported here
                rep["status"] = "skipped_quick"
                if (entry.get("enabled") and schedule.run_on_session_start(entry)
                        and schedule.is_due(schedule.get(con, name))):
                    background.append(name)
                continue
            warns: list[str] = []
            try:
                adapter = adapters.load_for(name, entry, warnings=warns)
            except (ImportError, AttributeError, KeyError) as e:
                rep.update(status="adapter_missing", errors=[f"{type(e).__name__}: {e}"])
                _finish_source(con, name, rep, entry, None, is_plugin)
                continue
            except Exception as e:  # noqa: BLE001 - a broken adapter module must not stop the others
                rep.update(status="error", errors=[f"{type(e).__name__}: {e}"])
                _finish_source(con, name, rep, entry, None, is_plugin)
                continue
            if warns:
                rep["note"] = "; ".join(warns)
            network = bool(getattr(adapter, "network", False))
            scheduled = network or is_plugin
            if quick and network:  # a quick sync never touches the network
                rep["status"] = "skipped_quick"
                if (entry.get("enabled") and entry.get("allow_network") is True
                        and schedule.run_on_session_start(entry, adapter) and schedule.is_due(schedule.get(con, name))):
                    background.append(name)
                continue
            if quick and getattr(adapter, "tier", "export") != "auto":
                rep["status"] = "skipped_not_auto"
                continue
            if network and entry.get("allow_network") is not True:
                rep.update(status="skipped_no_consent",
                           note=f"needs your OK to use the network: run `mannerism sources enable {name}`")
                continue
            if scheduled and not force:
                state = schedule.get(con, name)
                if not schedule.is_due(state):
                    rep.update(status="not_due", next_due=state.get("next_due"))
                    continue
            try:
                locations = list(adapter.discover(cfg.me, cfg.for_adapter(name)))
            except Exception as e:  # noqa: BLE001 - adapters are third-party code
                rep.update(status="error", errors=[f"discover: {type(e).__name__}: {e}"])
                _finish_source(con, name, rep, entry, adapter, scheduled)
                continue
            if only is not None:
                locations = [loc for loc in locations if _real(loc.key) in only]
            for loc in locations:
                if budget is not None and time.monotonic() - t0 >= budget:
                    rep["skipped_locations"] += 1
                    out_of_budget = True
                    continue
                rep["locations"] += 1
                for _round in range(MAX_ROUNDS_PER_LOCATION):
                    done = _read_location(con, adapter, name, loc, cfg, rep, new_ids, touched, gone, is_plugin,
                                          screen=screen)
                    if done:
                        break
                    if budget is not None and time.monotonic() - t0 >= budget:
                        out_of_budget = True  # this location is not caught up; the next sync resumes from its cursor
                        break
            _finish_source(con, name, rep, entry, adapter, scheduled)
        out: dict = {"status": "ok", "quick": quick, "sources": report, "new_units": len(new_ids),
                     "deleted_units": gone["n"], "budget_exhausted": out_of_budget,
                     "excluded": sum(r["excluded"] for r in report.values()),
                     "filtered": sum(r["filtered"] for r in report.values()),
                     "held": sum(r["held"] for r in report.values())}
        rule_errors = screen.rule_errors  # config.toml rules first, then stored ones, that were skipped
        if rule_errors:
            out["exclude_errors"] = rule_errors
        if exclude.STATS["regex_timeouts"]:
            out["exclude_regex_timeouts"] = exclude.STATS["regex_timeouts"]
        defer = only is not None and not refresh_now  # a file sync stores and screens; the next normal sync processes
        try:
            if defer:
                stale_cards = _invalidate_after_delete(con, gone["sources"]) if gone["sources"] else []  # privacy: now
                _mark_cards_stale(con, gone["stale"])
                regs = (_effective_registers(con, touched) | (gone["registers"] & set(core.REGISTERS))
                        | set(stale_cards))
                for key, add in (("new", new_ids), ("edited", gone["edited"]), ("kept_drafts", gone["kept_drafts"]),
                                 ("registers", sorted(regs)), ("stale", [])):
                    pend[key] += add
                if touched or regs or gone["n"]:
                    _save_pending(con, pend)
                out["refresh_pending"] = refresh_pending(con)
            else:
                if pend["new"] or pend["edited"] or pend["registers"] or pend["stale"]:  # earlier file syncs' leftovers
                    new_ids = [*pend["new"], *new_ids]
                    gone["edited"] = [*pend["edited"], *gone["edited"]]
                    gone["kept_drafts"] = [*pend["kept_drafts"], *gone["kept_drafts"]]
                    gone["registers"] |= set(pend["registers"])
                    gone["stale"] |= set(pend["stale"])
                    touched = [*pend["new"], *touched]
                if touched or gone["n"] or gone["registers"] or gone["sources"] or gone["stale"]:
                    stale_cards = _invalidate_after_delete(con, gone["sources"]) if gone["sources"] else []
                    _mark_cards_stale(con, gone["stale"])
                    stylometrics.compute_missing(con)
                    cap = capture.capture_new_units(con, [*new_ids, *gone["edited"]], gone["edited"])
                    store.drop_unpaired_drafts(con, gone["kept_drafts"])  # an edited text that found no pair: its draft goes
                    regs = (_effective_registers(con, touched) | set(cap["registers"])
                            | (gone["registers"] & set(core.REGISTERS)) | set(stale_cards))
                    out["capture"] = {k: cap[k] for k in ("edited", "accepted", "ambiguous", "unmatched")}
                    out["refresh"] = refresh.refresh(con, sorted(regs), cfg.me)
                    out["registers_changed"] = sorted(regs)
                    if gone["sources"]:
                        refresh.maybe_build_leak(con, force=True)
                store.set_meta(con, PENDING_KEY, None)
                out["work_queued"] = packets.queue_work(con, cfg.me)
        except Exception as e:  # noqa: BLE001 - report it; the lock is released either way
            out["status"] = "error"
            out["error"] = f"post-processing: {type(e).__name__}: {e}"
            if not defer:  # keep what was stored and not processed for the next sync
                try:
                    for key, add in (("new", new_ids), ("edited", gone["edited"]), ("kept_drafts", gone["kept_drafts"]),
                                     ("registers", sorted(gone["registers"])), ("stale", sorted(gone["stale"]))):
                        pend[key] += add
                    _save_pending(con, pend)
                except Exception:  # noqa: BLE001
                    pass
        out["elapsed_s"] = round(time.monotonic() - t0, 2)
        if background:
            out["background_due"] = background
        if only is None:
            store.set_meta(con, "last_sync", datetime.now(UTC).isoformat(timespec="seconds"))
            store.set_meta_json(con, "last_sync_result", {k: v for k, v in out.items() if k != "refresh"})
        return out
    finally:
        con.close()


def _python_isolation_flags() -> list[str]:
    """`-P` (3.11+ safe-path: drops the script/cwd from sys.path, keeps venv site-packages) when
    available, else `-I` (isolated mode: also ignores PYTHONPATH and user site, but an editable install's
    .pth file still makes the package importable since the venv's own site-packages is unaffected)."""
    return ["-P"] if sys.version_info >= (3, 11) else ["-I"]


def sync_in_background(quick: bool = True, sources: list[str] | None = None, force: bool = False,
                       files: list[str] | None = None, refresh: bool = False) -> dict:
    """Start a detached `python -m mannerism.sync` and return at once. No std stream is inherited.

    Runs with `cwd=core.home()` and an isolated `sys.path` so it behaves the same regardless of the
    caller's working directory, and `-I`/`-P` so a stray PYTHONPATH or user-site package cannot shadow
    the installed `mannerism` (editable installs still resolve through the venv's own .pth file)."""
    cmd = [sys.executable, *_python_isolation_flags(), "-m", "mannerism.sync"]
    if quick:
        cmd.append("--quick")
    if force:
        cmd.append("--force")
    for s in sources or []:
        cmd += ["--source", s]
    for f in files or []:
        cmd += ["--file", f]
    if refresh:
        cmd.append("--refresh")
    try:
        home = ensure_home()  # the subprocess's cwd must exist before Popen can chdir into it
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             close_fds=True, start_new_session=True, env=dict(os.environ), cwd=str(home))
    except OSError as e:
        return {"started": False, "error": str(e)}
    return {"started": True, "pid": p.pid}


def last_sync_age_s(con=None) -> float | None:
    own = con is None
    con = con or store.connect()
    try:
        raw = store.get_meta(con, "last_sync")
    finally:
        if own:
            con.close()
    return None if not raw else (datetime.now(UTC) - datetime.fromisoformat(raw)).total_seconds()


def forget_all_pending(con=None) -> bool:
    """True after `forget --all`, until the user explicitly runs `init` or `sync` again (decision 1)."""
    own = con is None
    con = con or store.connect()
    try:
        return bool(store.get_meta(con, "forget_all_at"))
    finally:
        if own:
            con.close()


def sync_if_stale(max_age_s: float = STALE_AFTER_S) -> dict:
    """Start a quick background sync when the last one is older than `max_age_s` (the MCP server's lazy path).

    Stays quiet when `forget --all` is pending: an automatic sync would silently undo the user's
    "stop reading my machine" request."""
    if forget_all_pending():
        return {"started": False, "reason": "forgotten_all"}
    age = last_sync_age_s()
    if age is not None and age < max_age_s:
        return {"started": False, "reason": "recent"}
    return sync_in_background(quick=True)


def _sync_files(ns) -> dict:
    """The detached single-file sync: wait a bounded time for the lock, and die after a hard time limit."""
    if hasattr(signal, "SIGALRM"):
        signal.alarm(FILE_SYNC_HARD_LIMIT_S)  # default action: terminate. The units + cursor transaction rolls back.
    deadline = time.monotonic() + FILE_SYNC_LOCK_WAIT_S
    while True:
        res = sync(ns.quick, ns.sources, ns.budget, ns.force, files=ns.files, refresh=ns.refresh)
        if res.get("status") != "busy" or time.monotonic() >= deadline:
            return res
        time.sleep(0.25)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m mannerism.sync")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--source", action="append", dest="sources")
    ap.add_argument("--budget", type=float, default=None)
    ap.add_argument("--file", action="append", dest="files", help="only read the location with this real path")
    ap.add_argument("--refresh", action="store_true",
                    help="with --file: also process what was stored (bank, statistics, capture); the default leaves it for "
                         "the next normal sync")
    ns = ap.parse_args(argv)
    if ns.files:
        res = _sync_files(ns)
    else:
        res = sync(ns.quick, ns.sources, ns.budget, ns.force)
    if sys.stdout is not None and sys.stdout.isatty():
        print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
