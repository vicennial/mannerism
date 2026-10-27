"""Minimal, strict HTTPS GET for the network sources (standard library only).

Rules every network source gets from here: https only, a finite timeout, a `mannerism/<version>` User-Agent, a cap on
the JSON body, redirects followed only inside the original origin (never to another host: credentials stay put), and
errors reduced to a `NetError` that carries a kind, a status and a retry hint. Error text never contains headers,
bodies or query strings, so a credential cannot leak into a log or a stored cursor.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from .. import __version__

TIMEOUT_S = 10.0
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_REDIRECTS = 3
DEFAULT_RETRY_S = 900  # a 429 without any reset header
MAX_RETRY_S = 24 * 3600
USER_AGENT = f"mannerism/{__version__}"


class NetError(Exception):
    """A failed request. `kind`: timeout | network | http | rate_limit | redirect | too_large | bad_json | url."""

    def __init__(self, kind: str, message: str, status: int | None = None, retry_after_s: float | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status
        self.retry_after_s = retry_after_s


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ARG002 - a 3xx surfaces as an HTTPError; get_json decides
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def urlopen(req: urllib.request.Request, timeout: float):
    """The single place a socket is opened (tests replace this function)."""
    return _OPENER.open(req, timeout=timeout)


def retry_after_s(headers: Mapping | None, now: datetime | None = None) -> float:
    """Seconds to wait, from Retry-After, x-rate-limit-reset (X, epoch), ratelimit-reset (Bluesky, epoch) or
    X-RateLimit-Reset (Mastodon, ISO-8601). Falls back to DEFAULT_RETRY_S; always within [1, MAX_RETRY_S]."""
    now = now or datetime.now(UTC)
    get = (lambda k: headers.get(k)) if headers is not None else (lambda k: None)
    for key in ("retry-after", "x-rate-limit-reset", "ratelimit-reset", "x-ratelimit-reset"):
        raw = get(key)
        if raw is None:
            continue
        raw = str(raw).strip()
        try:
            x = float(raw)
            wait = x - now.timestamp() if x > 1e9 else x
        except ValueError:
            try:
                dt = parsedate_to_datetime(raw) if not raw[:4].isdigit() else datetime.fromisoformat(raw)
            except (TypeError, ValueError):
                continue
            wait = (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).timestamp() - now.timestamp()
        return min(max(wait, 1.0), float(MAX_RETRY_S))
    return float(DEFAULT_RETRY_S)


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urllib.parse.urlsplit(url)
    return p.scheme, (p.hostname or "").lower(), p.port


def build_url(host: str, path: str, params: Mapping[str, str | int | list] | None = None) -> str:
    """https://host/path?query. A list value repeats the parameter (`uris=a&uris=b`); None values are left out."""
    q = urllib.parse.urlencode(
        {k: ([str(x) for x in v] if isinstance(v, list) else str(v)) for k, v in (params or {}).items() if v is not None},
        safe=",", doseq=True)
    return f"https://{host}{path}" + (f"?{q}" if q else "")


def get_json(url: str, headers: Mapping[str, str] | None = None, *, timeout: float = TIMEOUT_S,
             max_bytes: int = MAX_JSON_BYTES) -> tuple[object, Mapping]:
    """GET `url` (https only) and return (parsed JSON, response headers). Raises NetError."""
    start = _origin(url)
    if start[0] != "https" or not start[1]:
        raise NetError("url", "refusing a non-https URL")
    hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json", **(headers or {})}
    cur = url
    for _hop in range(MAX_REDIRECTS + 1):
        host = _origin(cur)[1]
        try:
            with urlopen(urllib.request.Request(cur, headers=hdrs, method="GET"), timeout) as resp:
                body = resp.read(max_bytes + 1)
                status = getattr(resp, "status", 200)
                resp_headers = resp.headers
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308):
                target = urllib.parse.urljoin(cur, (e.headers.get("Location") if e.headers else None) or "")
                if _origin(target) != start:
                    raise NetError("redirect", f"{host}: refused a redirect to another origin", e.code) from None
                cur = target
                continue
            if e.code == 429:
                raise NetError("rate_limit", f"{host}: HTTP 429 (rate limited)", 429,
                               retry_after_s(e.headers)) from None
            raise NetError("http", f"{host}: HTTP {e.code}", e.code) from None
        except TimeoutError:
            raise NetError("timeout", f"{host}: timed out") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise NetError("network", f"{host}: {type(e).__name__}") from None
        if status in (301, 302, 303, 307, 308):  # an opener that did not raise for a redirect
            target = urllib.parse.urljoin(cur, resp_headers.get("Location") or "")
            if _origin(target) != start:
                raise NetError("redirect", f"{host}: refused a redirect to another origin", status)
            cur = target
            continue
        if status == 429:
            raise NetError("rate_limit", f"{host}: HTTP 429 (rate limited)", 429, retry_after_s(resp_headers))
        if status >= 400:
            raise NetError("http", f"{host}: HTTP {status}", status)
        if len(body) > max_bytes:
            raise NetError("too_large", f"{host}: response over {max_bytes} bytes")
        try:
            return json.loads(body.decode("utf-8")), resp_headers
        except (ValueError, UnicodeDecodeError):
            raise NetError("bad_json", f"{host}: response is not JSON") from None
    raise NetError("redirect", "too many redirects")
