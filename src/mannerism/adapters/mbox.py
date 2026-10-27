"""mbox files (Gmail Takeout `*.mbox`, any mbox): own sent mail.

Own = `From` address in me.emails (nothing is read without emails). Prefers the text/plain part, falls back to text
stripped
from HTML. Quoted replies (`On ... wrote:` and its German/French/Spanish/Dutch forms, `-----Original Message-----`,
header blocks,
`>` lines), signatures (`-- `) and mobile signatures are removed. Auto-generated mail (Auto-Submitted, Precedence
bulk/list/junk,
List-Id) is skipped. Register is `?` (work vs formal vs close is decided by labelling). Cursor: {path: [size,
mtime_ns]}; changed
files are re-read and the engine dedupes by Message-ID (hashed; the engine never sees the real one, P1-2/P2).

R3 (P1-2): a Gmail Takeout "All Mail" export has every message once, labelled by `X-Gmail-Labels` -- including
messages a spoofer sent FROM the user's own address that Gmail filed as Spam, which `From in me.emails` alone
would not catch. So when that header is present at all, a message is kept only with a "Sent" label, and never
with a "Spam"/"Trash" label even if "Sent" is also (oddly) present. mbox files without the header (anything
that is not a Gmail Takeout export) are unaffected: the From check is all they ever had. Separately, an
`Authentication-Results` SPF/DMARC fail is the same spoofing signal on any mbox, Gmail or not, and skips the
message too.
"""

from __future__ import annotations

import email.policy
import email.utils
import mailbox
import re
from collections.abc import Iterable
from datetime import UTC
from html.parser import HTMLParser

from ..core import Location, Me, Unit, hash_ref
from ._util import export_candidates, own_unit, sha

_ATTRIB = re.compile(
    r"^\s*(?:On|Am|Le|El|Op)\s.{3,400}?\s(?:wrote|schrieb|a écrit|escribió|schreef)\b.{0,160}?:\s*$",
    re.IGNORECASE | re.DOTALL,
)
_ORIG = re.compile(
    r"^\s*(?:-{2,}\s*(?:Original Message|Forwarded message|Ursprüngliche Nachricht|Message d'origine)\s*-{2,}|"
    r"Begin forwarded message:|_{10,})\s*$",
    re.IGNORECASE,
)
_HDR_START = re.compile(r"^\s*(?:From|Von|De|Da)\s*:\s", re.IGNORECASE)
_HDR_NEXT = re.compile(
    r"^\s*(?:Sent|Date|To|Subject|Gesendet|An|Betreff|Envoyé|Objet|Enviado|Asunto)\s*:", re.IGNORECASE
)
_MOBILE = re.compile(
    r"^\s*(?:Sent from my .{2,40}|Sent from (?:Mail|Outlook|Yahoo Mail|Gmail|Proton Mail).{0,40}|"
    r"Get Outlook for .{2,20}|"
    r"Envoyé de mon .{2,30}|Von meinem .{2,30} gesendet|Enviado desde mi .{2,30})\s*$",
    re.IGNORECASE,
)
_AUTH_FAIL = re.compile(r"\b(?:spf|dmarc)\s*=\s*fail\b", re.IGNORECASE)


def gmail_labels(msg) -> set[str] | None:
    """Lower-cased `X-Gmail-Labels` values, or None when the header is absent (a non-Gmail mbox)."""
    raw = msg.get("X-Gmail-Labels")
    return {s.strip().lower() for s in str(raw).split(",") if s.strip()} if raw is not None else None


def looks_spoofed(msg) -> bool:
    """An SPF/DMARC fail in `Authentication-Results` -- someone else sent this with our address in `From`."""
    return bool(_AUTH_FAIL.search(str(msg.get("Authentication-Results") or "")))


class _Text(HTMLParser):
    _BLOCK = frozenset({"p", "div", "br", "tr", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"})

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t\xa0]+", " ", "".join(p.out))).strip()


def strip_quotes_and_sigs(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    kept: list[str] = []
    for i, ln in enumerate(lines):
        two = ln + " " + lines[i + 1] if i + 1 < len(lines) else ln
        if _ATTRIB.match(ln) or _ATTRIB.match(two) or _ORIG.match(ln):
            break
        if _HDR_START.match(ln) and any(_HDR_NEXT.match(x) for x in lines[i + 1 : i + 5]):
            break
        if re.match(r"^--\s?$", ln):  # signature delimiter
            break
        if ln.lstrip().startswith(">") or _MOBILE.match(ln):
            continue
        kept.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


def body_text(msg) -> str:
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    content = part.get_content()
    return html_to_text(content) if part.get_content_type() == "text/html" else content


class Mbox:
    name = "mbox"
    tier = "export"

    def discover(self, me: Me, config: dict) -> list[Location]:
        try:
            return [
                Location(self.name, str(p), p, "mbox mail export")
                for p in export_candidates(self.name, config, (".mbox", ".mbx"))
            ]
        except Exception:  # noqa: BLE001
            return []

    def read(self, loc: Location, cursor: dict | None, me: Me) -> tuple[Iterable[Unit], dict]:
        cursor = cursor or {}
        if not me.emails:
            return [], cursor
        try:
            st = loc.path.stat()
        except OSError:
            return [], cursor
        sig = [st.st_size, st.st_mtime_ns]
        if cursor.get(loc.key) == sig:
            return [], cursor
        own = {e.strip().lower() for e in me.emails}
        units: list[Unit] = []
        skipped = 0
        try:
            box = mailbox.mbox(str(loc.path), factory=None, create=False)
            keys = list(box.iterkeys())
        except (OSError, mailbox.Error):
            return [], cursor
        for key in keys:
            try:
                msg = email.message_from_bytes(box.get_bytes(key), policy=email.policy.default)
                frm = email.utils.parseaddr(str(msg.get("From", "")))[1].lower()
                if frm not in own or looks_spoofed(msg):
                    continue
                labels = gmail_labels(msg)
                if labels is not None and ("sent" not in labels or labels & {"spam", "trash"}):
                    continue
                auto = str(msg.get("Auto-Submitted", "no")).strip().lower()
                if (
                    auto != "no"
                    or str(msg.get("Precedence", "")).strip().lower() in ("bulk", "list", "junk")
                    or msg.get("List-Id")
                ):
                    continue
                text = strip_quotes_and_sigs(body_text(msg))
                ts = email.utils.parsedate_to_datetime(str(msg.get("Date")))
                ts = ts.replace(tzinfo=UTC) if ts.tzinfo is None else ts.astimezone(UTC)
                mid = str(msg.get("Message-ID") or "").strip().strip("<>")
                refs = re.findall(r"<([^>]+)>", str(msg.get("References") or "")) or re.findall(
                    r"<([^>]+)>", str(msg.get("In-Reply-To") or "")
                )
                root = refs[0] if refs else mid
                recips = sorted(
                    {
                        a.lower()
                        for _, a in email.utils.getaddresses(
                            [str(h) for h in (msg.get_all("To") or []) + (msg.get_all("Cc") or [])]
                        )
                        if a
                    }
                )
                # The real Message-ID never leaves this adapter: it can embed the sending host/client, so
                # the engine only ever sees its hash as source_ref (still a stable dedupe key, P1-2/P2).
                ref = f"mid:{sha(mid)}" if mid else f"nomid:{sha(str(msg.get('Date')) + str(msg.get('Subject')) + text[:200])}"
                u = own_unit(
                    source=self.name,
                    ref=ref,
                    ts=ts,
                    text=text,
                    kind="email",
                    register="?",
                    audience="unknown",
                    thread_id=hash_ref(root or ref),
                    audience_ref=hash_ref(",".join(recips)) if recips else None,
                    meta={"recipients": len(recips)},
                )
            except Exception:  # noqa: BLE001
                skipped += 1
                continue
            if u is not None:
                units.append(u)
        return units, {loc.key: sig, "skipped": skipped}
