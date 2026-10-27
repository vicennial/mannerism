"""Masking for work packets and leak checks for agent-written text.

Packets handed to the user's agent contain only masked text (`mask_for_packet`). Text the agent writes
back into mannerism (style cards, rules) is checked with `find_leaks` so private identifiers never get
stored in something that is later quoted into prompts and drafts.
"""
from __future__ import annotations

import re

from . import stats as stats_mod
from .core import Me, mask_names, mask_pii

_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+")
_HANDLE = re.compile(r"(?<![\w.])@[A-Za-z0-9_][A-Za-z0-9_.]{1,30}")
_URL = re.compile(r"https?://\S+|\bwww\.\S+", re.IGNORECASE)
_PHONE = re.compile(r"\+?\d[\d\s().\-]{7,}\d")
_MASK_TOKEN = re.compile(r"\[(?:email|phone|secret|iban|card|name|handle|link)\]")


# Imperative instructions aimed at the *reading* agent, not honest vocabulary. A card describing how
# the user writes may legitimately say "they often say 'just execute it'" or mention shells and
# terminals; only an actual attempt to redirect the model is rejected (ignore-previous, a tool-call
# phrasing, a backtick command, or a direct imperative like "you must now").
_TOOLISH = (r"ignore (?:all |any |the |your )?(?:previous|prior|above) instructions?|"
            r"disregard (?:all |any |the |your )?(?:previous|prior|above)|system prompt|"
            r"call (?:the |a )?(?:tool|function|api)\b|(?:tool|function|api) call\b|"
            r"(?:use|call) the \w+ tool\b|"
            r"run `|\bsudo\b|\bcurl\b|\bwget\b|rm -rf|you must now\b|you are now\b|new instructions:")
_TOOL_CARD = re.compile(rf"(?i)\b(?:{_TOOLISH})")
_TOOL_RULE = re.compile(rf"(?i)\brun\b|\b(?:{_TOOLISH})")
_FENCE = re.compile(r"```|~~~")


def find_injection(text: str, rule: bool = False) -> list[str]:
    """Code fences and actual instruction language aimed at the reading model: stored prose is later
    pasted into prompts, so it must not be able to redirect it. Honest vocabulary ("execute", "shell",
    "bash", "terminal", "invoke") is allowed; only imperative phrasing is rejected. `rule=True` is
    stricter (any bare "run")."""
    problems = []
    if _FENCE.search(text):
        problems.append("contains a code fence")
    if (_TOOL_RULE if rule else _TOOL_CARD).search(text):
        problems.append("contains tool or command language")
    return problems


def name_tokens(me: Me) -> list[str]:
    """Full names plus their parts of 4+ letters (short parts like "Jo" are too common to treat as identifying)."""
    out: list[str] = []
    for n in me.names:
        n = n.strip()
        if len(n) >= 2:
            out.append(n)
        out.extend(p for p in re.split(r"\W+", n) if len(p) >= 4)
    return sorted(set(out), key=len, reverse=True)


def mask_for_packet(text: str, me: Me | None = None, limit: int | None = None) -> str:
    """PII-masked (again; adapters already did it), the user's own names and handles replaced, URLs shortened."""
    text = mask_pii(text)
    text = _URL.sub("[link]", text)
    if me:
        text = mask_names(text, {n: "name" for n in name_tokens(me)})
        for h in me.handles.values():
            if len(h) >= 3:
                text = re.sub(rf"(?i)@?{re.escape(h)}", "[handle]", text)
    text = _HANDLE.sub("[handle]", text)
    if limit and len(text) > limit:
        text = text[:limit].rstrip() + " [...]"
    return text


def find_leaks(text: str, me: Me | None = None, use_index: bool = True) -> list[str]:
    """Problems that make `text` unsafe to store as a card or rule: emails, @handles, URLs, phone numbers,
    the user's names/handles, and capitalised names that occur only in private units."""
    problems: list[str] = []
    if _EMAIL.search(text):
        problems.append("contains an email address")
    if _HANDLE.search(text):
        problems.append("contains an @handle")
    if _URL.search(text):
        problems.append("contains a URL")
    if _PHONE.search(text):
        problems.append("contains a phone-number-like string")
    if me:
        low = text.lower()
        for n in name_tokens(me):
            if re.search(rf"(?i)\b{re.escape(n)}\b", text):
                problems.append("contains the user's name")
                break
        for h in me.handles.values():
            if len(h) >= 3 and h.lower() in low:
                problems.append("contains the user's handle")
                break
    if use_index:
        idx = stats_mod.load_leak_index()
        hits = sorted(e for e in stats_mod.entities(text) if e in idx)
        if hits:
            problems.append("contains names or handles that appear in private text: " + ", ".join(hits[:5]))
    return problems
