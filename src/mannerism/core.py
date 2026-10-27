"""Shared unit model, identity and the source-adapter contract.

Every adapter yields `Unit`s: one coherent piece of text the user wrote
themselves. Other people's text may appear only in `context` (masked, at most
CONTEXT_LINES lines), and is never used as a style example.

This module is the contract between adapters and the engine. Adapters import
only from here (and the standard library).
"""
from __future__ import annotations

import hashlib
import math
import os
import re
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal, Protocol

KINDS = ("message", "email", "prompt", "commit", "comment", "post", "reply", "note", "dictation", "document")
AUDIENCES = ("close", "group", "colleague", "service", "agent", "public", "self", "unknown")
REGISTERS = ("close", "group", "work", "formal", "agent", "public")  # generation registers
UNKNOWN_REGISTER = "?"  # adapters use this when metadata cannot decide; the agent labels it later
SKIP_REGISTER = "skip"  # not usable style evidence (pastes, code, logs)
DEFAULT_REGISTERS = (*REGISTERS, UNKNOWN_REGISTER, SKIP_REGISTER)

CONTEXT_LINES = 3
CONTEXT_CHARS = 200


def home() -> Path:
    """mannerism's data directory: $MANNERISM_HOME or ~/.mannerism."""
    return Path(os.environ.get("MANNERISM_HOME") or Path.home() / ".mannerism")


@dataclass
class Me:
    """Who "the user" is, for adapters that must tell own text from other people's.

    Adapters use the strongest signal they have first (a role field, an is_from_me
    flag, a sender id) and fall back to names only when nothing else exists."""
    names: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    handles: dict[str, str] = field(default_factory=dict)  # service -> handle or id: {"x": "jo.dev"}
    phone_suffixes: list[str] = field(default_factory=list)  # last 4+ digits only

    def has_email(self, addr: str | None) -> bool:
        return bool(addr) and addr.strip().lower() in {e.lower() for e in self.emails}

    def has_name(self, name: str | None) -> bool:
        return bool(name) and name.strip().casefold() in {n.casefold() for n in self.names}


@dataclass(frozen=True)
class ContextLine:
    """One preceding message in the thread. `who` is a masked role, never a name."""
    who: str  # "me" | "other1" | "other2" | "agent" ...
    text: str


def unit_id_for(source: str, source_ref: str) -> str:
    """The stored id of the unit a source yields for `source_ref` (also how a delete finds its unit)."""
    return hashlib.sha1(f"{source}:{source_ref}".encode()).hexdigest()[:20]


@dataclass
class Unit:
    source: str
    source_ref: str  # stable native id; for merged bursts, "first..last"
    ts: datetime  # timezone-aware
    text: str  # own text, already PII-masked by the adapter (use mask_pii)
    kind: str
    register: str = UNKNOWN_REGISTER  # metadata default; see DEFAULT_REGISTERS
    thread_id: str | None = None
    audience: str = "unknown"
    audience_ref: str | None = None  # hash_ref() of the counterpart, never a raw address or number
    parts: int = 1
    context: Sequence[ContextLine] = ()
    meta: dict = field(default_factory=dict)
    # Where the text came from, for exclusion rules and the "is this you?" review. Adapters that know it set these
    # (`own_unit` masks them); a source without them leaves them None and only source / text rules can match it.
    account: str | None = None  # config dir or home the text came from, $HOME shown as "~"
    project: str | None = None  # working directory basename
    project_path: str | None = None  # working directory, $HOME shown as "~"
    session_kind: str | None = None  # how the session was driven: "interactive", "ide", ... (the adapter's label)
    session: str | None = None  # hash_ref() of the session id
    gap_s: float | None = None  # seconds since the previous turn ended (not stored; feeds the machine-typed score)
    # Set by the engine at ingest (humanness.py), never by an adapter:
    template: str | None = None
    human_score: float | None = None
    held: bool = False
    score_info: dict | None = None

    @property
    def unit_id(self) -> str:
        return unit_id_for(self.source, self.source_ref)

    def validate(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown kind {self.kind!r}")
        if self.audience not in AUDIENCES:
            raise ValueError(f"unknown audience {self.audience!r}")
        if self.register not in DEFAULT_REGISTERS:
            raise ValueError(f"unknown register {self.register!r}")
        if self.ts.tzinfo is None:
            raise ValueError("ts must be timezone-aware")
        if not self.text.strip():
            raise ValueError("empty text")
        if len(self.context) > CONTEXT_LINES:
            raise ValueError("too many context lines")
        for name in ("account", "project", "project_path", "session_kind", "session"):
            if getattr(self, name) is not None and not isinstance(getattr(self, name), str):
                raise ValueError(f"{name} must be a string")


@dataclass(frozen=True)
class Location:
    """One readable place for a source: a file, a directory, a database or an API scope."""
    source: str
    key: str  # stable cursor key, usually the absolute path
    path: Path | None = None
    label: str = ""  # human-readable, shown in the init checklist


class Source(Protocol):
    name: str
    tier: Literal["auto", "export", "macos"]  # auto = zero setup; export = user points at a file; macos = needs FDA

    def discover(self, me: Me, config: dict) -> list[Location]:
        """Places this source can read on this machine. Must be fast (< 1 s) and never raise."""

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        """Units written since `cursor` and the cursor to store once they are saved.

        The engine stores the new cursor in the same transaction as the units, so a
        crash never skips text. A source that cannot resume returns cursor {} and the
        engine dedupes by unit_id."""


# --- masking -------------------------------------------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]{1,64}@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"\+?\d[\d\s().\-]{6,}\d")
_DATE = re.compile(r"^\d{1,4}[./-]\d{1,2}[./-]\d{1,4}$")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s?[A-Z0-9]{4}){3,7}(?:\s?[A-Z0-9]{1,3})?\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_SECRETS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
    re.compile(r"-----BEGIN PGP PRIVATE KEY BLOCK-----.*?-----END PGP PRIVATE KEY BLOCK-----", re.S),
    re.compile(r"\beyJ[\w-]{8,}\.[\w-]{8,}\.[\w-]{8,}\b"),  # JWT
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}\b"),  # OpenAI-style, dash separated
    re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),  # Stripe
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr|github_pat)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),  # GitLab personal access token
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),  # Hugging Face token
    re.compile(r"\bya29\.[\w-]{20,}\b"),  # Google OAuth access token
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),
)
_TOKEN_URL = re.compile(r"(https?://\S+?[?&](?:token|key|sig|signature|auth|code|access_token|api_key)=)[^\s&]+", re.I)
_URL_CRED = re.compile(r"([A-Za-z][\w+.-]{0,30}://[^\s/@:]{1,256}:)([^\s@]{1,256})(@)")  # scheme://user:PASS@host
# A key name containing "password", "secret", "token" or "api[_-]key" (as part of the identifier, so
# aws_secret_access_key qualifies too) immediately followed by ':' or '=' and a value: the value is the
# secret, so only it is masked. This also catches AWS-style "secret access key" assignments, which have
# no distinctive prefix of their own.
_KV_SECRET = re.compile(r"(?i)\b(\w*(?:password|passwd|secret|token|api[_-]?key)\w*\s*[:=]\s*)\S+")
# A generic high-entropy token (base64/hex-ish, >= 24 chars, at least two character classes): a plausible
# unlabelled secret. Deliberately a bit eager (a 40-char git SHA is high-entropy hex and gets masked too;
# that is an acceptable false positive, not a bug: see docs/design.md).
_TOKEN_CANDIDATE = re.compile(r"\b[A-Za-z0-9+/_.=-]{24,}\b")


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def _mask_high_entropy_token(m: re.Match) -> str:
    tok = m.group(0)
    classes = sum((any(c.islower() for c in tok), any(c.isupper() for c in tok), any(c.isdigit() for c in tok)))
    if classes < 2:
        return tok  # a plain word, path or run of one character class: not token-shaped
    if tok.count("/") > 1 or tok.count(".") > 3:
        return tok  # looks like a path or dotted name, not a token
    return "[secret]" if _shannon_entropy(tok) >= 3.5 else tok


def mask_credentials(text: str) -> str:
    """Mask secrets, token URLs and emails only (the part of `mask_pii` that is safe on a path or a name: no number
    masking, which would turn a directory like /mnt/vol_1234567 into [phone])."""
    for rx in _SECRETS:
        text = rx.sub("[secret]", text)
    text = _TOKEN_URL.sub(r"\1[secret]", text)
    text = _URL_CRED.sub(lambda m: m.group(1) + "[secret]" + m.group(3), text)
    text = _KV_SECRET.sub(r"\1[secret]", text)
    text = _TOKEN_CANDIDATE.sub(_mask_high_entropy_token, text)
    return _EMAIL.sub("[email]", text)


def mask_pii(text: str) -> str:
    """Mask secrets, token URLs, emails, IBANs, card numbers and phone numbers. Idempotent."""
    text = mask_credentials(text)
    text = _IBAN.sub("[iban]", text)

    def card(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "[card]" if 13 <= len(digits) <= 19 and _luhn(digits) else m.group(0)

    text = _CARD.sub(card, text)

    def phone(m: re.Match) -> str:
        s = m.group(0)
        if len(re.sub(r"\D", "", s)) < 8 or _DATE.match(s.strip()):
            return s
        return "[phone]"

    return _PHONE.sub(phone, text)


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def mask_names(text: str, names: dict[str, str]) -> str:
    """Replace each known name (case-insensitive, whole word) with its role tag, e.g. {"Dana": "other1"}."""
    for name in sorted(names, key=len, reverse=True):
        if len(name) < 2:
            continue
        text = re.sub(rf"(?i)\b{re.escape(name)}\b", f"[{names[name]}]", text)
    return text


def context_line(who: str, text: str, names: dict[str, str] | None = None) -> ContextLine:
    """A masked, truncated context line for another party's (or the agent's) message."""
    text = mask_pii(" ".join(text.split()))
    if names:
        text = mask_names(text, names)
    return ContextLine(who, text[:CONTEXT_CHARS])


LABEL_CHARS = 200


def clean_label(value) -> str | None:
    """A metadata label (account, project, kind) safe to store: masked, one line, bounded. None for empty / non-text."""
    if not isinstance(value, str) or not value.strip():
        return None
    return mask_credentials(" ".join(value.split()))[:LABEL_CHARS] or None


def home_label(path) -> str | None:
    """A path as a label with the home directory shown as "~" (so it names the place, not the user)."""
    if not path:
        return None
    text = str(path)
    h = str(Path.home())
    if h and h != "/" and (text == h or text.startswith(h + os.sep)):
        text = "~" + text[len(h):]
    return clean_label(text)


def hash_ref(value: str, salt: str = "mannerism") -> str:
    """Stable pseudonymous id for a recipient, address or chat."""
    return hashlib.sha256(f"{salt}:{value}".encode()).hexdigest()[:16]


def merge_bursts(
    events: Iterable[tuple[bool, datetime, str, str]], gap_s: int = 120
) -> Iterator[tuple[datetime, str, list[str]]]:
    """Merge consecutive own messages in one thread.

    `events` are (is_own, ts, ref, text) in time order for ONE thread. Own messages
    merge while they are within `gap_s` of the previous own message and no other
    party's message falls between them. Yields (first_ts, joined_text, refs)."""
    cur: list[tuple[datetime, str, str]] = []
    for is_own, ts, ref, text in events:
        if not is_own:
            if cur:
                yield cur[0][0], "\n".join(t for _, _, t in cur), [r for _, r, _ in cur]
                cur = []
            continue
        if cur and (ts - cur[-1][0]).total_seconds() > gap_s:
            yield cur[0][0], "\n".join(t for _, _, t in cur), [r for _, r, _ in cur]
            cur = []
        if cur and text.strip() == cur[-1][2].strip():
            continue  # a resend of the same text is one message
        cur.append((ts, ref, text))
    if cur:
        yield cur[0][0], "\n".join(t for _, _, t in cur), [r for _, r, _ in cur]


_CODEY = re.compile(r"```|^\s{4,}\S|^\s*(?:Traceback|\$ |>>> |at [\w.$]+\()", re.M)


def looks_pasted(text: str, max_lines: int = 40) -> bool:
    """True for text that is mostly a paste (code blocks, logs, stack traces, very long blocks), not prose."""
    lines = text.splitlines()
    if len(lines) > max_lines:
        return True
    hits = len(_CODEY.findall(text))
    return hits >= 3 or (hits >= 1 and len(lines) > 12)
