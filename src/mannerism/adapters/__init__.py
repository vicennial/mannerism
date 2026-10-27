"""Source adapters. One module per source; each defines a class implementing `mannerism.core.Source`.

Adapters keep only text the user wrote. Other people's text goes only into Unit.context, masked
with `core.context_line`, at most `core.CONTEXT_LINES` lines. Adapters use the standard library only.
"""

from __future__ import annotations

from importlib import import_module

from ..core import Source

# name -> "module:Class". Order is the order `init` shows sources in.
REGISTRY = {
    "claude_code": "claude_code:ClaudeCode",
    "codex": "codex:Codex",
    "git": "git:Git",
    "gh": "gh:GitHub",
    "bluesky": "bluesky:Bluesky",
    "mastodon": "mastodon:Mastodon",
    "x": "x_api:XApi",
    "slack": "slack:Slack",
    "markdown": "markdown:Markdown",
    "files": "files:Files",
    "chatgpt": "chatgpt:ChatGPT",
    "telegram": "telegram:Telegram",
    "whatsapp": "whatsapp:WhatsApp",
    "whatsapp_db": "whatsapp_db:WhatsAppDB",
    "hermes": "hermes:Hermes",
    "x_archive": "x_archive:XArchive",
    "mbox": "mbox:Mbox",
    "imessage": "imessage:IMessage",
    "wispr": "wispr:Wispr",
    "opencode": "opencode:OpenCode",
    "qwen_code": "qwen_code:QwenCode",
    "goose": "goose:Goose",
    "aider": "aider:Aider",
    "crush": "crush:Crush",
    "pi": "pi:Pi",
    "continue": "continue_dev:Continue",
    "cline": "cline:Cline",
    "kilo": "kilo:Kilo",
}


# Built-in adapters that can be configured as several named instances: `[sources.<instance>]` with
# `adapter = "<key>"`. The instance name becomes the source name (units, cursors and `forget --source`
# all use it), so an instance name must not equal a built-in source or an installed plugin.
INSTANCE_ADAPTERS = {"files": "files:Files"}


def load(name: str) -> Source:
    mod, cls = REGISTRY[name].split(":")
    return getattr(import_module(f"{__name__}.{mod}"), cls)()


def load_for(name: str, entry: dict | None = None, *, warnings: list[str] | None = None) -> Source:
    """The source for config entry `[sources.<name>]`: a built-in, an instance of one, or an enabled plugin.

    Plain built-ins go through `load(name)`. A plugin is imported here, so callers that must never run plugin
    code (`sync --quick`) check `entry.get("plugin")` before calling."""
    entry = entry or {}
    if entry.get("plugin"):
        from .. import plugins

        try:
            return plugins.load(name, entry, warnings=warnings)
        except plugins.PluginError as e:
            raise ImportError(str(e)) from None
    kind = entry.get("adapter")
    if kind:
        if kind not in INSTANCE_ADAPTERS:
            raise KeyError(f"adapter {kind!r} cannot be configured as an instance")
        mod, cls = INSTANCE_ADAPTERS[kind].split(":")
        return getattr(import_module(f"{__name__}.{mod}"), cls)(name)
    return load(name)


def all_sources() -> list[Source]:
    out = []
    for name in REGISTRY:
        try:
            out.append(load(name))
        except (ImportError, AttributeError):
            continue  # not implemented yet
    return out
