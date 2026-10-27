"""The `mannerism` command line. A thin layer over `mannerism.api`; the heavy modules load only when a command needs them.

Human-readable output by default, `--json` where a script might want it. Expected problems (unknown register,
missing file, bad packet) print one friendly line to stderr and exit 2; nothing prints a traceback for those.
Exit codes: 0 ok, 1 `lint` found something to fix, 2 error or aborted.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__

EXIT_OK, EXIT_FINDINGS, EXIT_ERROR = 0, 1, 2
HOOK_EVENTS = ("session-start", "prompt", "stop", "session-end")
REGISTERS = ("close", "group", "work", "formal", "agent", "public")


class CliError(Exception):
    """An expected failure: printed as `mannerism: <message>` and exit 2."""


# --- small helpers ----------------------------------------------------------------------------------------------------

def table(headers: list[str], rows: list[list]) -> str:
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip()  # noqa: E731
    return "\n".join([line(headers), line(["-" * w for w in widths]), *(line(r) for r in cells)])


def out(text: str = "") -> None:
    print(text)


def dump_json(data) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def need(res: dict) -> dict:
    """Unwrap an api result: raise CliError when it reports failure."""
    if not res.get("ok", True):
        raise CliError(res.get("error") or "that did not work")
    return res


def read_text_arg(arg: str | None, what: str) -> str:
    """Contents of FILE, or of stdin for `-` / no argument."""
    if arg in (None, "-"):
        if sys.stdin is None or sys.stdin.isatty():
            raise CliError(f"no {what} given: pass a FILE, or pipe text in and use '-'")
        return sys.stdin.read()
    p = Path(arg).expanduser()
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CliError(f"file not found: {arg}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise CliError(f"cannot read {arg}: {e}") from None


def check_register(register: str) -> str:
    if register not in REGISTERS:
        raise CliError(f"unknown register {register!r}; use one of: {', '.join(REGISTERS)}")
    return register


def ago(seconds) -> str:
    if seconds is None:
        return "never"
    s = int(seconds)
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60)):
        if s >= n:
            return f"{s // n} {unit} ago"
    return "just now"


def _iso_age(iso, *, future: bool = False) -> str:
    """`ago`-style text for a stored ISO timestamp; for a due time, "in 5 min" or "now"."""
    from datetime import UTC, datetime
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "-"
    delta = (dt - datetime.now(UTC)).total_seconds() if future else (datetime.now(UTC) - dt).total_seconds()
    if future:
        return "now" if delta <= 0 else "in " + ago(delta).replace(" ago", "").replace("just now", "under a min")
    return ago(max(delta, 0))


# --- commands ---------------------------------------------------------------------------------------------------------

def cmd_init(ns) -> int:
    from . import api
    if not ns.json:
        print("Looking for your writing on this machine (this can take a minute) ...", file=sys.stderr)
    res = need(api.setup(ns.source or None))
    if ns.json:
        dump_json(res)
        return EXIT_OK
    rows = []
    for s in res["sources"]:
        if not s["available"]:
            continue
        found = f"{s['locations']} found" if s["locations"] else "-"
        rows.append([s["name"], s.get("tier") or "-", found, "yes" if s["enabled"] else "no", s.get("units", 0)])
    out(f"mannerism {res['version']}   data: {res['home']}\n")
    out(table(["source", "kind", "found", "reading", "texts"], rows))
    total = sum(s.get("units", 0) for s in res["sources"])
    out()
    paths = res.get("paths_read") or []
    if paths:
        # Root finding (f): say exactly what was read, so nothing is a surprise.
        out(f"Read {len(paths)} path(s):")
        for p in paths:
            out(f"- {p['source']}: {p['location']}")
        out()
    if total:
        out(table(["register", "texts", "status"],
                  [[r, v["units"], v["state"]] for r, v in res["registers"].items()]))
        out()
    sync = res.get("sync") or {}
    if sync.get("budget_exhausted"):
        out("Sync stopped early on its time budget; run `mannerism sync` to finish.\n")
    if not total:
        out("No writing was found on this machine yet. That is fine: mannerism works from what you give it.")
        out("- Add an export (chat app, mail, X archive): mannerism sources add NAME PATH   (see `mannerism sources`)")
        out("- Or keep using your coding agents, then run: mannerism sync")
    else:
        pend = (res.get("registers") or {})
        out("Next:")
        out("- Try it: mannerism context work   (or any register: close, group, work, formal, agent, public)")
        out("- Check a draft: mannerism lint --register work draft.txt")
        if any(v["state"].startswith("insufficient") for v in pend.values()):
            out("- Registers with too little text use universal checks only; they improve as you write and sync.")
        out("- With the MCP server or plugin, tell your agent: \"learn my voice\" to sharpen the cards.")
    return EXIT_OK


def _all_source_names(cfg=None) -> list[str]:
    """Built-in sources, configured instances of one (`adapter = "files"`) and installed plugins (metadata only)."""
    from . import adapters, config, plugins
    cfg = cfg or config.load_config()
    names = list(adapters.REGISTRY)
    names += [n for n, e in cfg.sources.items() if e.get("adapter") and n not in names]
    names += [n for n in sorted(plugins.names()) if n not in names]
    return names


def _known_source(name: str, cfg=None) -> str:
    names = _all_source_names(cfg)
    if name not in names:
        raise CliError(f"unknown source {name!r}; known sources: {', '.join(names)}")
    return name


def _confirm(ns, what: str) -> None:
    """Ask before something that can cost money or contact a service; `--yes` answers for scripts."""
    if ns is not None and ns.yes:
        return
    print(what, file=sys.stderr)
    try:
        answer = input("Continue? Type 'yes': ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        raise CliError("not enabled: confirm to continue (type 'yes', or pass --yes in a script)")


def _confirm_own_text(ns, what: str) -> None:
    """mannerism learns only from the user's own writing: ask, unless `--yes`."""
    if ns.yes:
        return
    print(f"mannerism learns from text YOU wrote. {what} must contain only your own writing, not other people's "
          f"messages, documents or quotes.", file=sys.stderr)
    try:
        answer = input("Is all of it your own text? Type 'yes' to continue: ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        raise CliError("not added: confirm the text is yours (type 'yes', or pass --yes in a script)")


def _check_interval(ns) -> int | None:
    if ns.interval is None:
        return None
    if ns.interval < 0:
        raise CliError("--interval must be 0 (every sync) or more seconds")
    return ns.interval


def _enable_source(cfg, name: str, interval: int | None, confirm=None) -> None:
    """Enable `name`: a built-in, a configured instance or an installed plugin (imported now, once, because the
    user asked for it). A network source also gets the user's consent recorded, after the destinations are shown."""
    from . import adapters, plugins
    entry = cfg.sources.setdefault(name, {"enabled": False, "paths": []})
    if not entry.get("adapter") and name not in adapters.REGISTRY:
        try:
            plugin, adapter = plugins.enable_check(name)
        except plugins.PluginError as e:
            raise CliError(str(e)) from None
        out(f"{name}: plugin '{plugin.value}' from {plugin.dist or 'an installed package'}. A plugin is ordinary "
            f"Python and runs with your permissions, with no sandbox: enable only code you trust.")
        entry.update(plugins.pin(plugin))
    else:
        try:
            adapter = adapters.load_for(name, entry)
        except (ImportError, AttributeError, KeyError) as e:
            raise CliError(f"cannot load source {name!r}: {e}") from None
    network = bool(getattr(adapter, "network", False))
    if network:
        entry["allow_network"] = True  # (saved only if enabling goes through) so the adapter's own hint sees consent
        adapter_cfg = cfg.for_adapter(name)
        # An adapter whose destination depends on configuration (a Mastodon instance) offers hosts(config).
        hosts_fn = getattr(adapter, "hosts", None)
        hosts = tuple((hosts_fn(adapter_cfg) if callable(hosts_fn) else getattr(adapter, "network_hosts", ())) or ())
        out(f"{name}: uses the network; it will contact: {', '.join(hosts) if hosts else '(destinations not declared)'}")
        if callable(getattr(adapter, "status_message", None)):
            msg = adapter.status_message(adapter_cfg)
            if msg:
                out(f"{name}: {msg}")
        if callable(getattr(adapter, "estimate", None)):  # a metered service: show the cost, ask first
            posts, usd = adapter.estimate(adapter_cfg)
            out(f"{name}: first read about {posts} posts, roughly ${usd:.2f}")
            ceiling = _monthly_ceiling(adapter, adapter_cfg)
            if ceiling:
                out(f"{name}: monthly ceiling {ceiling}")
            caveat = getattr(adapter, "rate_caveat", None)
            caveat = caveat(adapter_cfg) if callable(caveat) else caveat
            if isinstance(caveat, str) and caveat.strip():
                out(f"{name}: {caveat.strip()}")
            _confirm(confirm, f"{name} can cost money.")
    if network or entry.get("plugin"):
        from . import schedule
        on = schedule.run_on_session_start(entry, None if entry.get("plugin") else adapter)
        out(f"{name}: session start: "
            + ("a session start may launch a background sync for it when it is due (run_on_session_start = true)"
               if on else "a session start never launches it; it runs on `mannerism sync` or `sync --watch` "
                          f"(set run_on_session_start = true in [sources.{name}] to allow a background start)"))
    if interval is not None:
        entry["interval_s"] = interval
    entry["enabled"] = True


def _monthly_ceiling(adapter, adapter_cfg) -> str:
    """The adapter's monthly ceiling as text, from a `monthly_ceiling(config)` method or, failing that, a module
    function of the same name. It may return USD or (posts, USD). Empty when there is none or it fails."""
    import sys
    fn = getattr(adapter, "monthly_ceiling", None)
    if not callable(fn):
        fn = getattr(sys.modules.get(type(adapter).__module__), "monthly_ceiling", None)
    if not callable(fn):
        return ""
    try:
        val = fn(adapter_cfg)
        if isinstance(val, (tuple, list)) and len(val) == 2:
            return f"about ${float(val[1]):.2f} ({int(val[0])} posts)"
        return f"about ${float(val):.2f}"
    except Exception:  # noqa: BLE001 - a broken estimate must not stop the enable flow
        return ""


def _sources_list(ns, cfg) -> int:
    from . import adapters, core, plugins, store
    con = store.connect() if (core.home() / "corpus.db").exists() else None
    counts = {}
    if con is not None:
        try:
            counts = {r[0]: r[1] for r in con.execute("SELECT source, count(*) FROM units GROUP BY source")}
        finally:
            con.close()
    rows, data = [], []

    def add_row(name, kind, found, entry, extra=None):
        on = bool(entry.get("enabled"))
        rows.append([name, kind, found or "-", "yes" if on else "no", counts.get(name, 0)])
        data.append({"name": name, "tier": kind, "locations": found, "enabled": on, "units": counts.get(name, 0),
                     **(extra or {})})

    for name in adapters.REGISTRY:
        entry = cfg.sources.get(name, {})
        try:
            adapter = adapters.load(name)
            tier = getattr(adapter, "tier", "-")
            net = bool(getattr(adapter, "network", False))
            # A network adapter's discover is never called by a listing: finding places must not touch the network.
            found = 0 if net else len(list(adapter.discover(cfg.me, cfg.for_adapter(name))))
        except Exception:  # noqa: BLE001 - a broken adapter must not break the listing
            tier, found, net = "-", 0, False
        add_row(name, "network" if net else tier, found, entry, {"network": net, "allow_network": bool(entry.get("allow_network"))})
    for name, entry in cfg.sources.items():
        if entry.get("adapter") and name not in adapters.REGISTRY:
            try:
                found = len(list(adapters.load_for(name, entry).discover(cfg.me, cfg.for_adapter(name))))
            except Exception:  # noqa: BLE001
                found = 0
            add_row(name, f"{entry['adapter']} (yours)", found, entry, {"adapter": entry["adapter"], "path": entry.get("path"),
                                                                       "register": entry.get("register")})
    for p in plugins.discover():  # from package metadata only: nothing is imported
        entry = cfg.sources.get(p.name, {})
        label = "plugin" if p.usable else f"plugin ({p.status})"
        add_row(p.name, label, 0, entry, {"plugin": {"dist": p.dist, "value": p.value, "status": p.status,
                                                    "detail": p.detail}})
    if ns.json:
        dump_json(data)
    else:
        out(table(["source", "kind", "found", "reading", "texts"], rows))
        bad = [p for p in plugins.discover() if not p.usable]
        for p in bad:
            out(f"- plugin {p.name}: refused ({p.detail})")
        if any(p.usable for p in plugins.discover()):
            out("Plugins are listed from package metadata and are not loaded until `mannerism sources enable NAME`.")
    return EXIT_OK


def _sources_add_files(ns, cfg) -> int:
    from . import adapters, plugins
    from .adapters import files as files_mod
    if not ns.path or not ns.instance:
        raise CliError("usage: mannerism sources add files PATH --name INSTANCE [--register R] [--kind K] [--yes]")
    name = ns.instance
    if not files_mod.NAME_RE.match(name):
        raise CliError("--name must be lowercase letters, digits, '-' or '_', start with a letter, 32 characters at most")
    if name in adapters.REGISTRY or name in plugins.names():
        raise CliError(f"{name!r} is already the name of a built-in source or an installed plugin; pick another --name")
    entry = cfg.sources.get(name)
    if entry is not None and entry.get("adapter") != "files":
        raise CliError(f"{name!r} is already a source with its own settings; pick another --name")
    if ns.register:
        check_register(ns.register)
    if ns.kind and ns.kind not in files_mod.KINDS:
        raise CliError(f"unknown kind {ns.kind!r}; use one of: {', '.join(files_mod.KINDS)}")
    path = Path(ns.path).expanduser().resolve()
    if not path.exists():
        raise CliError(f"path not found: {ns.path}")
    _confirm_own_text(ns, str(path))
    entry = dict(entry or {})
    entry.update(adapter="files", path=str(path), enabled=True)
    entry.pop("paths", None)
    if ns.register:
        entry["register"] = ns.register
    if ns.kind:
        entry["kind"] = ns.kind
    interval = _check_interval(ns)
    if interval is not None:
        entry["interval_s"] = interval
    cfg.sources[name] = entry
    from . import config
    config.save_config(cfg)
    out(f"{name}: reading {path}"
        + (f" as {entry['register']} text" if entry.get("register") else " (no register given: your agent labels each text later)")
        + f"; run `mannerism sync --source {name}`. New and changed files are picked up on every later sync.")
    if entry.get("register") == "public":
        out("note: public-register text from files is kept, but public drafts learn only from verified "
            "publications, so it is not used for them (docs/custom-sources.md).")
    return EXIT_OK


def _check_network_source(ns, cfg) -> int:
    """`sources check NAME` for a source whose adapter offers `check(config) -> (ok, message)` (Slack: validates the
    token with auth.test). It reads no text and stores nothing."""
    from . import adapters
    ok, message = adapters.load(ns.name).check(cfg.for_adapter(ns.name))
    if ns.json:
        dump_json({"source": ns.name, "ok": ok, "message": message})
    else:
        out(message)
    return EXIT_OK if ok else EXIT_FINDINGS


def _sources_check(ns, cfg) -> int:
    from . import adapters
    from .adapters import files as files_mod
    if not ns.name:
        raise CliError("usage: mannerism sources check FILE.jsonl | SOURCE")
    path = Path(ns.name).expanduser()
    if ns.name in adapters.REGISTRY and not path.exists() and callable(getattr(adapters.load(ns.name), "check", None)):
        return _check_network_source(ns, cfg)
    try:
        rep = files_mod.check_file(path)
    except FileNotFoundError:
        raise CliError(f"file not found: {ns.name}") from None
    except OSError as e:
        raise CliError(f"cannot read {ns.name}: {e}") from None
    if ns.json:
        dump_json({"file": str(path), "records": rep.records, "upserts": rep.upserts, "deletes": rep.deletes,
                   "public": rep.public, "errors": [{"line": n, "reason": r} for n, r in rep.errors],
                   "more_errors": rep.more_errors, "ok": not rep.errors and not rep.more_errors})
    else:
        for n, reason in rep.errors:
            out(f"{path}:{n}: {reason}")
        if rep.more_errors:
            out(f"... and {rep.more_errors} more errors")
        bad = len(rep.errors) + rep.more_errors
        out(f"{rep.records} valid record(s) ({rep.upserts} upsert, {rep.deletes} delete), {bad} error(s). Nothing was stored.")
        if rep.public:
            out(f"note: {rep.public} record(s) use register public; they are kept but not used for public drafts "
                f"until verified as published.")
        if bad:
            out("A sync stops a file at its first bad line; fix the lines above, then sync again.")
    return EXIT_FINDINGS if (rep.errors or rep.more_errors) else EXIT_OK


def cmd_sources(ns) -> int:
    from . import config
    action = ns.action or "list"
    cfg = config.load_config()
    if action == "list":
        return _sources_list(ns, cfg)
    if action == "check":
        return _sources_check(ns, cfg)
    if not ns.name:
        raise CliError(f"`sources {action}` needs a source name")
    if action == "add" and ns.name == "files":
        return _sources_add_files(ns, cfg)
    name = _known_source(ns.name, cfg)
    interval = _check_interval(ns)
    if action == "enable":
        _enable_source(cfg, name, interval, ns)
        config.save_config(cfg)
        out(f"{name}: now read on sync")
    elif action == "disable":
        entry = cfg.sources.setdefault(name, {"enabled": False, "paths": []})
        entry["enabled"] = False
        entry.pop("allow_network", None)  # consent to the network ends with the source
        config.save_config(cfg)
        out(f"{name}: no longer read (already stored texts stay; `mannerism forget --source {name}` removes them)")
    elif action == "add":
        if not ns.path:
            raise CliError("usage: mannerism sources add NAME PATH")
        path = Path(ns.path).expanduser().resolve()
        if not path.exists():
            raise CliError(f"path not found: {ns.path}")
        entry = cfg.sources.setdefault(name, {"enabled": False, "paths": []})
        if entry.get("adapter") == "files":  # adding a folder of your own files needs the same confirmation
            _confirm_own_text(ns, str(path))
        paths = entry.setdefault("paths", [])
        if str(path) not in paths:
            paths.append(str(path))
        _enable_source(cfg, name, interval, ns)
        config.save_config(cfg)
        out(f"{name}: added {path} and enabled it; run `mannerism sync --source {name}` to read it")
    return EXIT_OK


def _print_sync(res: dict) -> None:
    rows = [[n, r["status"], r["locations"], r["new_units"], "; ".join(r["errors"][:1]) or r.get("note", "")]
            for n, r in (res.get("sources") or {}).items() if not r["status"].startswith("skipped_") or r.get("note")]
    if rows:
        out(table(["source", "status", "locations", "new texts", "errors"], rows))
    gone = f", {res['deleted_units']} removed" if res.get("deleted_units") else ""
    skipped = [f"{res[k]} {label}" for k, label in (("excluded", "excluded by your rules"),
                                                    ("filtered", "dropped as machine-typed"),
                                                    ("held", "held for review")) if res.get(k)]
    if skipped:
        gone += " (" + ", ".join(skipped) + ")"
    for msg in res.get("exclude_errors") or []:
        print(f"mannerism: config.toml {msg}", file=sys.stderr)
    out(f"{res['new_units']} new texts{gone} in {res.get('elapsed_s', 0)} s"
        + ("; stopped on its time budget, run again to continue" if res.get("budget_exhausted") else ""))
    if res.get("background"):
        out("network and plugin sources are due: a full sync was started in the background")


def cmd_sync(ns) -> int:
    from . import sync
    if ns.watch:
        if ns.quick or ns.background or ns.force:
            raise CliError("--watch runs full syncs on a schedule: do not combine it with --quick, --background or --force")
        if ns.every < 1:
            raise CliError("--every must be at least 1 second")
        out(f"watching: a full sync every {ns.every:g} s (sources follow their own intervals). Ctrl-C to stop.")
        try:
            sync.watch(ns.every, sources=ns.source or None, report=lambda r: _print_sync(r) if not ns.json else dump_json(r))
        except KeyboardInterrupt:
            out("stopped")
        return EXIT_OK
    if ns.background:
        res = sync.sync_in_background(quick=ns.quick, sources=ns.source or None, force=ns.force)
        if not res.get("started"):
            raise CliError(f"could not start the background sync: {res.get('error')}")
        out(f"sync started in the background (pid {res['pid']})")
        return EXIT_OK
    res = sync.sync(quick=ns.quick, sources=ns.source or None, force=ns.force)
    if ns.json:
        dump_json(res)
        return EXIT_OK
    if res.get("status") == "busy":
        out(res["message"])
        return EXIT_OK
    _print_sync(res)
    if res.get("error"):
        raise CliError(res["error"])
    return EXIT_OK


def cmd_status(ns) -> int:
    from . import api
    if os.environ.get("MANNERISM_REMOTE_URL"):
        res = _remote("voice_status", {})
        dump_json(res) if ns.json else out(f"public snapshot: {res['posts']} examples; built {res['built_at']}")
        return EXIT_OK
    res = need(api.status())
    if ns.json:
        dump_json(res)
        return EXIT_OK
    out(f"mannerism {res['version']}   data: {res['home']}")
    out(f"last sync: {ago(res['last_sync_age_s'])}   texts: {res['total_units']}   "
        f"unlabelled: {res['unlabelled']}   rules: {res['rules']}")
    if res.get("held"):
        out(f"{res['held']} texts look machine-typed and are held back until you confirm them: `mannerism review`")
    out()
    srcs = [[n, "yes" if s["enabled"] else "no", s["units"],
             _iso_age(s.get("last_success")) if s.get("last_success") else ("never" if s["enabled"] else "-"),
             _iso_age(s["next_due"], future=True) if s.get("next_due") else "-",
             s.get("last_error") or "; ".join(s["last_errors"][:1])]
            for n, s in sorted(res["sources"].items())]
    if srcs:
        out(table(["source", "reading", "texts", "last success", "next due", "last error"], srcs))
        out()
    out(table(["register", "texts", "status"], [[r, v["units"], v["state"]] for r, v in res["registers"].items()]))
    pend = res["pending_work"]
    todo = [f"{pend['label']} texts to label" if pend["label"] else "",
            f"cards: {', '.join(pend['card'])}" if pend["card"] else "",
            f"rules to distill: {', '.join(pend['distill'])}" if pend["distill"] else "",
            f"blind test: {pend['blind']}" if pend["blind"] else ""]
    todo = [t for t in todo if t]
    out()
    out("pending work for your agent: " + ("; ".join(todo) if todo else "none"))
    if not res["total_units"]:
        out("nothing read yet: run `mannerism init`")
    return EXIT_OK


def cmd_context(ns) -> int:
    from . import api
    args = {"register": check_register(ns.register), "task": ns.task, "k": ns.k}
    res = _remote("voice_context", args) if os.environ.get("MANNERISM_REMOTE_URL") else need(api.context(**args))
    if ns.json:
        dump_json(res)
    else:
        out(res["text"])
        for note in res["notes"]:
            print(f"note: {note}", file=sys.stderr)
    return EXIT_OK


def cmd_lint(ns) -> int:
    from . import api
    text = read_text_arg(ns.file, "text")
    if not text.strip():
        raise CliError("nothing to lint: the text is empty")
    if not ns.register:
        raise CliError(f"say who the text is for with --register ({', '.join(REGISTERS)})")
    args = {"text": text, "register": check_register(ns.register), "is_draft": not ns.no_log}
    res = _remote("voice_lint", args) if os.environ.get("MANNERISM_REMOTE_URL") else need(api.lint(**args))
    if ns.json:
        dump_json(res)
    else:
        out(res["summary"])
        logged = ("; draft remembered" + (f" as #{res['draft_id']}" if res['draft_id'] is not None else "")) if res["logged"] else ""
        out(f"({res['note']}{logged})")
    return EXIT_FINDINGS if res["findings"] else EXIT_OK


def cmd_feedback(ns) -> int:
    from . import api
    draft = read_text_arg(ns.draft, "draft")
    final = read_text_arg(ns.final, "final text")
    args = {"draft": draft, "final": final, "register": check_register(ns.register), "rejected": ns.rejected}
    res = _remote("voice_feedback", args) if os.environ.get("MANNERISM_REMOTE_URL") else need(api.feedback(**args))
    if ns.json:
        dump_json(res)
    else:
        out("feedback recorded" if os.environ.get("MANNERISM_REMOTE_URL") else
            "edit recorded" if res.get("created") else "that edit was already known")
        if res.get("distill_ready"):
            out(f"enough edits for rules in {ns.register}: your agent can call `work next --kind distill`")
    return EXIT_OK


def cmd_work(ns) -> int:
    from . import api
    if ns.action == "next":
        dump_json(need(api.work_next(ns.kind)))
        return EXIT_OK
    raw = read_text_arg(ns.result, "result")
    try:
        result = json.loads(raw)
    except ValueError as e:
        raise CliError(f"the result is not valid JSON: {e}") from None
    dump_json(need(api.work_submit(ns.kind, ns.packet_id, result)))
    return EXIT_OK


def cmd_rules(ns) -> int:
    from . import api
    if ns.action == "rm":
        res = api.delete_rule(ns.rule_id)
        if not res["ok"]:
            raise CliError(f"no rule with id {ns.rule_id}")
        out(f"rule {ns.rule_id} deleted")
        return EXIT_OK
    rules = need(api.list_rules(ns.register))["rules"]
    if ns.json:
        dump_json(rules)
    elif not rules:
        out("no rules yet: they are learned from your edits to drafts")
    else:
        out(table(["id", "register", "support", "rule"],
                  [[r["rule_id"], r["register"], r["support"], r["text"] + (" (hidden)" if r["hidden"] else "")]
                   for r in rules]))
    return EXIT_OK


def cmd_blind(ns) -> int:
    from . import api
    res = need(api.blind_score(ns.code))["result"]
    if ns.json:
        dump_json(res)
        return EXIT_OK
    lo, hi = res["ci95"]
    out(f"You picked the real message {res['correct']} of {res['answered']} times "
        f"({int(round(res['spot_rate'] * 100)) if res['spot_rate'] is not None else '-'}%, 95% range {lo:.0%} to {hi:.0%}).")
    out("50% means you could not tell; lower means the generated text fooled you more often.")
    if res["by_register"]:
        out()
        out(table(["register", "correct", "answered"],
                  [[r, v["correct"], v["answered"]] for r, v in res["by_register"].items()]))
    return EXIT_OK


def _rule_fields(ns) -> dict:
    return {k: v for k, v in (("source", ns.source), ("account", ns.account), ("project", ns.project),
                              ("kind", ns.kind), ("text", ns.text), ("group", ns.group)) if v}


def _confirm_delete(ns, what: str) -> None:
    if ns.yes:
        return
    print(f"This will permanently delete {what}.", file=sys.stderr)
    try:
        answer = input("Type 'yes' to continue: ").strip().lower()
    except EOFError:
        answer = ""
    if answer not in ("y", "yes"):
        print("Nothing deleted.", file=sys.stderr)
        raise _Aborted


class _Aborted(Exception):
    pass


def _by_source(res: dict) -> str:
    return ", ".join(f"{k}: {v}" for k, v in sorted((res.get("by_source") or {}).items())) or "none"


def _delete_where(ns, add_rule: bool) -> int:
    """`forget --where` and `exclude add`: count first, confirm, then delete (and for `exclude add` keep the rule)."""
    from . import api
    fields = _rule_fields(ns)
    force = bool(getattr(ns, "force", False))
    call = api.exclude_add if add_rule else api.forget_where
    probe = need(call(dry_run=True, force=force, **fields))
    if ns.dry_run:
        if ns.json:
            dump_json(probe)
        else:
            if probe.get("warning"):
                print(f"mannerism: {probe['warning']}", file=sys.stderr)
            out(f"Would remove {probe['would_remove']} stored texts ({_by_source(probe)}), and what was derived from them. "
                f"Nothing changed.")
        return EXIT_OK
    if probe.get("warning"):  # a mixed group without --force
        raise CliError(probe["warning"])
    try:
        if probe["would_remove"]:
            _confirm_delete(ns, f"{probe['would_remove']} stored texts matching {probe['rule']} ({_by_source(probe)}), "
                         f"and what was derived from them")
    except _Aborted:
        return EXIT_ERROR
    res = need(call(force=force, **fields))
    if ns.json:
        dump_json(res)
        return EXIT_OK
    if add_rule:
        out(f"Rule {res['rule_id']} {'added' if res['created'] else 'already existed'}: {res['rule']}. "
            f"New texts that match are not stored.")
    out(f"Removed {res['removed']} stored texts ({_by_source(res)}).")
    if res["stale_cards"]:
        out(f"Agent cards for {', '.join(res['stale_cards'])} were built on that data and have been dropped.")
    if res.get("note"):
        out(res["note"])
    return EXIT_OK


def cmd_forget(ns) -> int:
    from . import api
    crit = [f for f in ("account", "project", "kind", "text", "group") if getattr(ns, f)]
    if ns.where:
        if ns.drafts or ns.all:
            ns._parser.error("--where cannot be combined with --drafts or --all")
        if not (crit or ns.source):
            ns._parser.error("--where needs at least one of --source, --account, --project, --kind, --text, --group")
        return _delete_where(ns, add_rule=False)
    if crit or ns.dry_run:
        ns._parser.error("--account, --project, --kind, --text, --group and --dry-run go with --where")
    if sum(map(bool, (ns.source, ns.drafts, ns.all))) != 1:
        ns._parser.error("say what to forget: one of --source, --drafts, --all or --where")
    what = ("everything mannerism stores (texts, drafts, edits, rules, cards, stats)" if ns.all else
            "your logged drafts, captured edits and learned rules" if ns.drafts else
            f"all texts read from {ns.source}, plus the drafts and edits linked to them")
    try:
        _confirm_delete(ns, what)
    except _Aborted:
        return EXIT_ERROR
    res = need(api.forget(source=ns.source, drafts=ns.drafts, all=ns.all))
    if ns.json:
        dump_json(res)
        return EXIT_OK
    deleted = ", ".join(f"{k}: {v}" for k, v in (res["deleted"] or {}).items() if v) or "nothing to delete"
    out(f"Deleted ({deleted}).")
    if res["stale_cards"]:
        out(f"Agent cards for {', '.join(res['stale_cards'])} were built on that data and have been dropped.")
    out(res["note"])
    return EXIT_OK


def cmd_exclude(ns) -> int:
    from . import api
    action = ns.action or "list"
    if action == "add":
        if not (_rule_fields(ns)):
            ns._parser.error("exclude add needs at least one of --source, --account, --project, --kind, --text, --group")
        return _delete_where(ns, add_rule=True)
    if action == "rm":
        if ns.rule_id is None:
            ns._parser.error("exclude rm needs the rule id: `mannerism exclude rm ID` (ids are in `mannerism exclude list`)")
        res = need(api.exclude_rm(ns.rule_id))
        out(f"rule {res['rule_id']} removed. {res['note']}")
        return EXIT_OK
    res = need(api.exclude_list())
    if ns.json:
        dump_json(res)
        return EXIT_OK
    for msg in res["errors"]:
        print(f"mannerism: config.toml {msg}", file=sys.stderr)
    if not res["rules"]:
        out("no exclusion rules. Add one with `mannerism exclude add --project 'scratch-*'` "
            "(see `mannerism scan` for what is on this machine).")
        return EXIT_OK
    out(table(["id", "from", "rule"], [[r["rule_id"] if r["rule_id"] is not None else "-",
                                         "config.toml" if r["origin"] == "config" else "store", r["rule"]]
                                        for r in res["rules"]]))
    return EXIT_OK


def cmd_scan(ns) -> int:
    from . import api, scan
    if not ns.json:
        print("Scanning (reads your history, stores nothing; this can take a minute) ...", file=sys.stderr)
    if not 0 <= ns.samples <= scan.MAX_SAMPLES:
        raise CliError(f"--samples must be between 0 and {scan.MAX_SAMPLES}")
    res = need(api.scan(sources=ns.source or None, samples=ns.samples, network=ns.network, budget_s=ns.budget))
    if ns.json:
        dump_json(res)
    else:
        out(scan.format_text(res, top=ns.top))
    return EXIT_OK


def cmd_review(ns) -> int:
    from . import api
    action = ns.action or "list"
    if action == "keep":
        res = need(api.review_keep(ns.group_id))
        out(f"{res['released']} texts of {res['group_id']} are in normal use now; later texts of this group are not held.")
        return EXIT_OK
    if action == "rescan":
        res = need(api.review_rescan())
        if ns.json:
            dump_json(res)
            return EXIT_OK
        org = res.get("origins") or {}
        if org.get("filled"):
            out(f"filled in where {org['filled']} older texts came from ("
                + ", ".join(f"{k}: {v}" for k, v in sorted(org["sources"].items())) + ")")
        for name, why in sorted((org.get("skipped") or {}).items()):
            out(f"{name}: origin not filled in ({why})")
        if org.get("budget_exhausted"):
            out("reading the sources again stopped on its time budget: run `mannerism review rescan` again to continue")
        out(f"scored {res['scored']} stored texts that were read before machine-typed detection; {res['held']} held for review")
        return EXIT_OK
    res = need(api.review_groups(ns.samples))
    if ns.json:
        dump_json(res)
        return EXIT_OK
    held = [g for g in res["groups"] if g["held"]]
    if not held:
        out("nothing is held for review.")
        return EXIT_OK
    out(f"{res['held_total']} texts are held back (kept out of your style cards and examples) until you decide.")
    for g in held:
        where = " ".join(x for x in (g["source"], g["account"], g["project"], g["session_kind"]) if x) or "repeated opening"
        out(f"\n[{g['group_id']}]  {where}: {g['held']} held of {g['units']} texts")
        for sample in g["samples"]:
            out(f"    > {sample}")
        if g.get("mixed"):
            out(f"    older texts from several places: sort them first with `mannerism review rescan`; "
                f"it is you: mannerism review keep {g['group_id']}")
        else:
            out(f"    it is you: mannerism review keep {g['group_id']}      not you: mannerism exclude add --group {g['group_id']}")
    return EXIT_OK


def _port(text: str) -> int:
    try:
        port = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a port number") from None
    if not 0 <= port <= 65535:
        raise argparse.ArgumentTypeError("a port is a number from 0 to 65535")
    return port


def cmd_ui(ns) -> int:
    from . import web
    try:
        return web.run(ns.port, open_browser=not ns.no_open)
    except OSError as e:
        raise CliError(f"cannot start the page: {e} (try --port)") from None


def cmd_mcp(ns) -> int:
    if os.environ.get("MANNERISM_REMOTE_URL"):
        from .remote import serve_stdio
        serve_stdio()
        return EXIT_OK
    from . import mcp_server
    mcp_server.main()
    return EXIT_OK


def _remote(tool: str, args: dict) -> dict:
    from . import remote
    try:
        return need(remote.call(tool, args))
    except Exception as exc:
        raise CliError("public connection failed; check the tunnel, token and public register") from exc


def cmd_public(ns) -> int:
    from . import public, public_server, store
    try:
        if ns.action == "serve":
            public_server.serve(ns.port, Path(ns.token_file) if ns.token_file else None)
        elif ns.action == "build":
            con = store.connect()
            try:
                dump_json(public.build_snapshot(con))
            finally:
                con.close()
        else:
            dump_json(public.status())
    except (OSError, ValueError, RuntimeError) as exc:
        raise CliError(str(exc)) from exc
    return EXIT_OK


def cmd_remote(ns) -> int:
    from . import remote
    remote.serve_stdio()
    return EXIT_OK


def cmd_import_legacy(ns) -> int:
    from . import legacy
    try:
        dump_json(legacy.import_voicekit(Path(ns.path), apply=ns.apply))
    except (OSError, ValueError, RuntimeError) as exc:
        raise CliError(str(exc)) from exc
    return EXIT_OK


def _known_harness(name: str) -> str:
    from . import connect
    if name not in connect.HARNESSES:
        raise CliError(f"unknown harness {name!r}; known harnesses: {', '.join(connect.HARNESSES)}")
    return name


def cmd_connect(ns) -> int:
    from . import connect
    project_dir = Path(ns.project).expanduser().resolve() if ns.project else None
    if not ns.harness:
        rows = connect.list_harnesses(project_dir or Path.cwd())
        if ns.json:
            dump_json(rows)
            return EXIT_OK

        def yn(v):
            return "yes" if v else ("no" if v is False else "-")

        out(table(["harness", "installed", "configured (user)", "configured (project)", "hooks"],
                  [[r["name"], yn(r["installed"]), yn(r["configured"]["user"]), yn(r["configured"]["project"]),
                    yn(r["hooks"])] for r in rows]))
        out()
        out("mannerism connect NAME            show the MCP snippet and skill/rules step for one harness")
        out("mannerism connect NAME --write    wire it in (idempotent; backs up first)")
        return EXIT_OK
    name = _known_harness(ns.harness)
    h = connect.HARNESSES[name]
    if ns.write:
        try:
            res = connect.do_write(h, project_dir=project_dir, command=ns.command)
        except connect.ConnectError as e:
            raise CliError(str(e)) from None
        if ns.json:
            dump_json(res)
            return EXIT_OK
        out(f"{h.label} ({h.docs_url})")
        for m in res["mcp"]:
            if m["written"]:
                out(f"- MCP ({m['scope']}): wrote {m['path']}" + (f"  (backup: {m['backup']})" if m["backup"] else ""))
            else:
                out(f"- MCP ({m['scope']}): {m['path']} -- {m['reason']}")
                if m["reason"] and "stdlib writer" in m["reason"]:
                    out(connect.render_snippet(h, ns.command))
        skill = res["skill"]
        if skill and skill["kind"] == "skill_dir":
            for t in skill["targets"]:
                out(f"- skill: {'copied to ' + t['path'] if t['written'] else t['path'] + ' -- ' + t['reason']}")
        elif skill and skill["kind"] == "rules_file":
            out(f"- rules file ({skill['path_hint']}): not written automatically -- paste this in yourself:")
            out(skill["snippet"])
        return EXIT_OK
    data = connect.info(h, project_dir=project_dir, command=ns.command)
    if ns.json:
        dump_json(data)
        return EXIT_OK
    out(f"{h.label}   docs: {h.docs_url}")
    out(f"installed on this machine: {'yes' if data['installed'] else 'no'}"
        + ("" if not h.note else f"\nnote: {h.note}"))
    out()
    if data["mcp"] is None:
        out("no MCP client support")
    else:
        m = data["mcp"]
        out(f"MCP config: {m['format']} file, key {m['key']!r}" + ("" if m["writable"] else " (print-only: no stdlib "
                                                                                              "writer for this format)"))
        for t in m["targets"]:
            out(f"  {t['scope']}: {t['path']}")
        if m["paste_into"]:
            out(f"  paste into: {m['paste_into']}")
        out()
        out(m["snippet"])
    out()
    skill = data["skill"]
    if skill["kind"] == "skill_dir":
        out("skill: copy skills/mannerism/SKILL.md into:")
        for t in skill["targets"]:
            out(f"  {t['scope']}: {t['path']}")
        out("(`--write` does this for you)")
    elif skill["kind"] == "rules_file":
        out(f"no Agent Skills support: add this to {skill['path_hint']} in your project (never created or "
            f"overwritten automatically):")
        out(skill["snippet"])
    else:
        out("no skill or rules file for this harness")
    return EXIT_OK


def cmd_hook(ns) -> int:
    from . import hooks
    return hooks.run(ns.event)


def cmd_capture_posts(ns) -> int:
    from .published import capture_posts
    try:
        dump_json(capture_posts(Path(ns.path), candidates_paths=ns.candidates or ()))
    except (OSError, ValueError, RuntimeError) as exc:
        raise CliError(f"published capture failed ({type(exc).__name__})") from None
    return EXIT_OK


# --- parser -----------------------------------------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mannerism", description="Your agents write in your voice.")
    ap.add_argument("--version", action="version", version=f"mannerism {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")

    def add(name, func, help_, json_flag=True):
        p = sub.add_parser(name, help=help_, description=help_)
        p.set_defaults(func=func)
        if json_flag:
            p.add_argument("--json", action="store_true", help="machine-readable output")
        return p

    p = add("init", cmd_init, "find your writing, read it and build first style cards")
    p.add_argument("--source", action="append", metavar="NAME", help="also enable this source (repeatable)")

    p = add("sources", cmd_sources, "list sources, enable / disable / add one, or check a JSONL file or a source's token")
    p.add_argument("action", nargs="?", choices=["list", "enable", "disable", "add", "check"])
    p.add_argument("name", nargs="?", help="source name (for `check`: the JSONL file, or a source such as slack)")
    p.add_argument("path", nargs="?")
    p.add_argument("--name", dest="instance", metavar="INSTANCE", help="`add files`: name of this files source")
    p.add_argument("--register", help=f"`add files`: the register of the text ({', '.join(REGISTERS)})")
    p.add_argument("--kind", help="`add files`: message, post, comment or document (default document)")
    p.add_argument("--interval", type=int, metavar="SECONDS", help="seconds between runs (0: every sync)")
    p.add_argument("--yes", action="store_true", help="`add files`: confirm the text is yours, do not ask")

    p = add("sync", cmd_sync, "read new text from the enabled sources")
    p.add_argument("--quick", action="store_true", help="auto sources only, 5 s budget")
    p.add_argument("--source", action="append", metavar="S", help="only this source (repeatable)")
    p.add_argument("--background", action="store_true", help="start a detached sync and return")
    p.add_argument("--force", action="store_true", help="ignore source intervals and backoff (not consent or budgets)")
    p.add_argument("--watch", action="store_true", help="keep running full syncs until Ctrl-C")
    p.add_argument("--every", type=float, default=60.0, metavar="SECONDS", help="with --watch: seconds between runs")

    add("status", cmd_status, "sources, texts per register, card state, pending work")

    p = add("capture-posts", cmd_capture_posts, "capture an explicitly supplied own-post JSON export (never publishes)")
    p.add_argument("path")
    p.add_argument("--candidates", action="append", metavar="JSONL", help="local public draft candidates, repeatable")

    p = add("context", cmd_context, "style card, learned rules and real examples for a register")
    p.add_argument("register")
    p.add_argument("--task", default="")
    p.add_argument("--k", type=int, default=6)

    p = add("lint", cmd_lint, "check a draft (exit 1 when there are findings)")
    p.add_argument("--register")
    p.add_argument("--no-log", action="store_true", help="do not remember the text as a draft (use for your own text)")
    p.add_argument("file", nargs="?", metavar="FILE", help="file to read, or - for stdin")

    p = add("feedback", cmd_feedback, "record an edit: the agent's draft and the version you used")
    p.add_argument("--register", required=True)
    p.add_argument("--rejected", action="store_true", help="keep the rejection without treating it as style evidence")
    p.add_argument("draft", metavar="DRAFT_FILE")
    p.add_argument("final", metavar="FINAL_FILE")

    p = add("work", cmd_work, "the agent work queue (labels, cards, rules, blind test)", json_flag=False)
    ws = p.add_subparsers(dest="action", required=True, metavar="next|submit")
    n = ws.add_parser("next", help="print the next packet as JSON")
    n.add_argument("--kind", choices=["label", "card", "distill", "blind", "blind_gist", "blind_write"])
    s = ws.add_parser("submit", help="store the answer to a packet")
    s.add_argument("kind")
    s.add_argument("packet_id")
    s.add_argument("result", metavar="RESULT_FILE", help="JSON file, or - for stdin")

    p = add("rules", cmd_rules, "list learned rules, or `rules rm ID`")
    p.add_argument("--register")
    rs = p.add_subparsers(dest="action", metavar="rm")
    rm = rs.add_parser("rm", help="delete a rule")
    rm.add_argument("rule_id", type=int)

    p = add("blind", cmd_blind, "score a blind-test result code", json_flag=False)
    bs = p.add_subparsers(dest="action", required=True, metavar="score")
    sc = bs.add_parser("score", help="score the code shown on the test page")
    sc.add_argument("code")
    sc.add_argument("--json", action="store_true")

    def rule_flags(p, source_help):
        p.add_argument("--source", metavar="S", help=source_help)
        p.add_argument("--account", metavar="GLOB", help="config / home dir the text came from, e.g. '~/.claude-work*'")
        p.add_argument("--project", metavar="GLOB", help="working directory name or path, e.g. 'scratch-*'")
        p.add_argument("--kind", metavar="K", help="how the session was driven: interactive, ide, ...")
        p.add_argument("--text", metavar="REGEX", help="regular expression searched in the text")
        p.add_argument("--group", metavar="ID", help="a group id from `mannerism scan` / `mannerism review`")

    p = add("forget", cmd_forget, "delete stored data")
    p.set_defaults(_parser=p)
    rule_flags(p, "forget everything read from this source (with --where: only texts of this source that match)")
    p.add_argument("--drafts", action="store_true")
    p.add_argument("--all", action="store_true")
    p.add_argument("--where", action="store_true",
                   help="delete only the texts matching --source/--account/--project/--kind/--text/--group (all given must match)")
    p.add_argument("--dry-run", action="store_true", help="with --where: say how many texts would go, delete nothing")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    p.add_argument("--force", action="store_true", help="with --where --group: allow a group of older texts with no recorded origin")

    p = add("exclude", cmd_exclude, "never store text that matches a rule (and remove what is stored): add, list, rm")
    p.set_defaults(_parser=p)
    p.add_argument("action", nargs="?", choices=["add", "list", "rm"])
    p.add_argument("rule_id", nargs="?", type=int, help="`rm`: the rule id from `exclude list`")
    rule_flags(p, "exact source name, e.g. claude_code")
    p.add_argument("--dry-run", action="store_true", help="`add`: say how many stored texts would be removed, change nothing")
    p.add_argument("--yes", action="store_true", help="`add`: do not ask for confirmation")
    p.add_argument("--force", action="store_true",
                   help="`add --group`: remove a group of older texts with no recorded origin as it is, although it can mix "
                        "texts from several places (run `mannerism review rescan` first instead)")

    p = add("scan", cmd_scan, "show what a sync would read and how much looks machine-typed, storing nothing")
    p.add_argument("--source", action="append", metavar="S", help="only this source (repeatable)")
    p.add_argument("--samples", type=int, default=3, metavar="N", help="masked sample texts per group (0 to 20, default 3)")
    p.add_argument("--network", action="store_true", help="also read network sources that you already allowed")
    p.add_argument("--budget", type=float, default=None, metavar="SECONDS", help="stop starting new reads after this long (default 120)")
    p.add_argument("--top", type=int, default=25, metavar="N", help="groups to print per source (default 25)")

    p = add("review", cmd_review, "texts held back as machine-typed: list them, `keep GROUP` when they are yours, `rescan`")
    p.add_argument("action", nargs="?", choices=["list", "keep", "rescan"])
    p.add_argument("group_id", nargs="?", help="`keep`: the group id from `review`")
    p.add_argument("--samples", type=int, default=3, metavar="N")

    add("mcp", cmd_mcp, "run the MCP server on stdio", json_flag=False)

    p = add("public", cmd_public, "published-only snapshot and loopback MCP service")
    p.add_argument("action", choices=["build", "serve", "status"])
    p.add_argument("--port", type=_port, default=3345)
    p.add_argument("--token-file")

    p = add("remote", cmd_remote, "run a public-only stdio client over a loopback tunnel", json_flag=False)
    p.add_argument("action", choices=["mcp"])

    p = add("import-voicekit", cmd_import_legacy, "preview or apply a local legacy Voice Kit migration")
    p.add_argument("path")
    p.add_argument("--apply", action="store_true", help="write to the Mannerism destination; default is preview only")

    p = add("ui", cmd_ui, "open your voice cards and insights in the browser (read-only, 127.0.0.1)", json_flag=False)
    p.add_argument("--port", type=_port, default=0, help="port to listen on (default: a random free one)")
    p.add_argument("--no-open", action="store_true", help="print the address but do not open a browser")

    p = add("connect", cmd_connect, "wire mannerism into another agent harness (no argument: list harnesses)")
    p.add_argument("harness", nargs="?", help="e.g. cursor, codex, gemini-cli (see `mannerism connect` for the list)")
    p.add_argument("--write", action="store_true", help="merge the MCP entry and copy the skill (idempotent)")
    p.add_argument("--project", metavar="DIR", help="also target this project's config/skill/rules location")
    p.add_argument("--command", metavar="PATH",
                   help="absolute path to the mannerism executable, for a pip/pipx install instead of uvx")

    p = add("hook", cmd_hook, "harness hook entry points (print nothing, always exit 0)", json_flag=False)
    p.add_argument("event", choices=list(HOOK_EVENTS))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["hook"] and len(args) == 2 and args[1] in HOOK_EVENTS:
        if os.environ.get("MANNERISM_REMOTE_URL"):
            return EXIT_OK  # remote clients never capture local prompts or discover sources
        from . import hooks  # skip argparse and every other import: hooks run on every prompt
        return hooks.run(args[1])
    ap = build_parser()
    ns = ap.parse_args(args)
    if not getattr(ns, "func", None):
        ap.print_help()
        return EXIT_OK
    try:
        if os.environ.get("MANNERISM_REMOTE_URL") and ns.command not in {"context", "lint", "feedback", "status", "mcp", "remote"}:
            raise CliError("remote mode permits only public context, lint, feedback, status and MCP")
        return ns.func(ns)
    except CliError as e:
        print(f"mannerism: {e}", file=sys.stderr)
        return EXIT_ERROR
    except BrokenPipeError:
        try:
            sys.stdout = open(os.devnull, "w")  # noqa: SIM115 - avoid a second error at interpreter exit
        except OSError:
            pass
        return EXIT_OK
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
