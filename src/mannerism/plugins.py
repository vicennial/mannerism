"""Python entry-point plugins: third-party sources installed with `pip`.

A package offers a source by declaring an entry point in the group `mannerism.sources.v1` that resolves to a class
implementing `mannerism.core.Source`:

```toml
[project.entry-points."mannerism.sources.v1"]
my-notes = "my_pkg.sources:MyNotes"
```

Two steps, so that installing a package never runs its code inside mannerism:

1. **Listing** (`mannerism sources`) reads only the installed packages' metadata (`importlib.metadata`). Nothing
   is imported.
2. **Enabling** (`mannerism sources enable NAME`) imports the entry point once to check it, then records
   `plugin = "<entry point value>"`, `plugin_dist` and `plugin_version` in `[sources.NAME]`. Only enabled plugins
   are ever imported, and only by a full sync: `sync --quick` (the session hook) never imports one. If a different
   distribution later provides the name, the plugin is not loaded until the user enables it again. A new version
   of the same distribution (or a changed target in it) still loads, with a warning in the sync report.

A plugin is ordinary Python and runs with the user's permissions: there is no sandbox. The name must be unique:
it must not equal a built-in source or another installed plugin's name (such a name is "ambiguous" and refused).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import metadata

from . import adapters

GROUP = "mannerism.sources.v1"
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class PluginError(Exception):
    """A plugin cannot be listed as usable, enabled or loaded. The message is safe to show the user."""


@dataclass(frozen=True)
class Plugin:
    name: str
    value: str  # the entry point target, "package.module:Class"
    dist: str  # distribution that provides it ("" when unknown)
    status: str  # "ok" | "ambiguous" | "builtin_name" | "bad_name"
    detail: str = ""
    version: str = ""  # the providing distribution's version ("" when unknown)

    @property
    def usable(self) -> bool:
        return self.status == "ok"


def _entry_points() -> list[tuple[Plugin, metadata.EntryPoint]]:
    """Every installed entry point of the group with its verdict. Reads metadata only; imports nothing."""
    try:
        eps = list(metadata.entry_points(group=GROUP))
    except Exception:  # noqa: BLE001 - a broken package index must not break listing
        return []
    counts: dict[str, int] = {}
    for ep in eps:
        counts[ep.name] = counts.get(ep.name, 0) + 1
    builtin = set(adapters.REGISTRY)
    out = []
    for ep in sorted(eps, key=lambda e: (e.name, e.value)):
        dist = ""
        try:
            dist = (ep.dist.name if ep.dist is not None else "") or ""
        except Exception:  # noqa: BLE001
            pass
        if not NAME_RE.match(ep.name):
            status, detail = "bad_name", "names are lowercase letters, digits, '-' and '_', starting with a letter"
        elif ep.name in builtin:
            status, detail = "builtin_name", f"{ep.name!r} is a built-in source"
        elif counts[ep.name] > 1:
            status, detail = "ambiguous", f"{counts[ep.name]} installed packages provide {ep.name!r}"
        else:
            status, detail = "ok", ""
        version = ""
        try:
            version = (ep.dist.version if ep.dist is not None else "") or ""
        except Exception:  # noqa: BLE001
            pass
        out.append((Plugin(ep.name, ep.value, dist, status, detail, version), ep))
    return out


def discover() -> list[Plugin]:
    """Installed plugins, from package metadata only (nothing is imported)."""
    return [p for p, _ in _entry_points()]


def names() -> set[str]:
    """Every installed plugin name, usable or not (a new source instance must not take one of these)."""
    return {p.name for p in discover()}


def find(name: str) -> Plugin:
    """The usable plugin called `name`, or PluginError saying why there is none."""
    matches = [p for p in discover() if p.name == name]
    if not matches:
        raise PluginError(f"no installed plugin {name!r}")
    if not matches[0].usable:
        raise PluginError(f"plugin {name!r} is refused: {matches[0].detail}")
    return matches[0]


def _instantiate(name: str, ep: metadata.EntryPoint):
    try:
        target = ep.load()
    except (Exception, SystemExit) as e:  # noqa: BLE001 - a plugin may raise anything, even SystemExit, on import
        raise PluginError(f"plugin {name!r} failed to import: {type(e).__name__}: {str(e)[:160]}") from None
    try:
        obj = target() if isinstance(target, type) else target
    except Exception as e:  # noqa: BLE001
        raise PluginError(f"plugin {name!r} failed to start: {type(e).__name__}: {str(e)[:160]}") from None
    if not (callable(getattr(obj, "discover", None)) and callable(getattr(obj, "read", None))):
        raise PluginError(f"plugin {name!r} is not a Source: it needs discover() and read()")
    if getattr(obj, "name", None) != name:
        raise PluginError(f"plugin {name!r}: the class sets name={getattr(obj, 'name', None)!r}; it must equal the "
                          f"entry point name, because stored texts are filed under it")
    return obj


def pin(plugin: Plugin) -> dict:
    """The `[sources.NAME]` keys that record which plugin the user enabled."""
    return {"plugin": plugin.value, "plugin_dist": plugin.dist, "plugin_version": plugin.version}


def load(name: str, entry: dict | None = None, *, warnings: list[str] | None = None):
    """Import and start the enabled plugin `name`. `entry` is its `[sources.NAME]` table.

    Refuses unless the plugin was enabled and is still provided by the same distribution. A new version of that
    distribution, or a changed entry point target inside it, is loaded and reported through `warnings`."""
    entry = entry or {}
    pinned = entry.get("plugin")
    if not pinned:
        raise PluginError(f"plugin {name!r} is not enabled: run `mannerism sources enable {name}`")
    for plugin, ep in _entry_points():
        if plugin.name != name:
            continue
        if not plugin.usable:
            raise PluginError(f"plugin {name!r} is refused: {plugin.detail}")
        dist = entry.get("plugin_dist")
        if dist is not None:
            if plugin.dist != dist:
                raise PluginError(f"plugin {name!r} is now provided by {plugin.dist or 'another package'!r}, not the "
                                  f"{dist!r} you enabled; run `mannerism sources enable {name}` again if you trust it")
        elif plugin.value != pinned:  # an entry written before distributions were recorded: pin the target
            raise PluginError(f"plugin {name!r} now points at {plugin.value!r}, not the {pinned!r} you enabled; "
                              f"run `mannerism sources enable {name}` again if you trust it")
        note = []
        if entry.get("plugin_version") not in (None, plugin.version):
            note.append(f"version {entry['plugin_version']} -> {plugin.version}")
        if plugin.value != pinned:
            note.append(f"target {pinned} -> {plugin.value}")
        if note and warnings is not None:
            warnings.append(f"plugin {name!r} changed since you enabled it ({'; '.join(note)}); "
                            f"run `mannerism sources enable {name}` to accept")
        return _instantiate(name, ep)
    raise PluginError(f"plugin {name!r} is no longer installed")


def enable_check(name: str):
    """Import the plugin once for `sources enable`, after the user asked for it. Returns (Plugin, instance)."""
    plugin = find(name)
    for p, ep in _entry_points():
        if p.name == name:
            return plugin, _instantiate(name, ep)
    raise PluginError(f"no installed plugin {name!r}")
