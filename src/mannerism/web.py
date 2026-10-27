"""The local voice page: ``mannerism ui``.

A small local web page that shows the voice cards, the rules learned from edits and a few honest
insights, and lets you answer "Is this you?" for groups of stored texts. It is read-only except for two
actions (see "Writes" below). It follows the same security posture as the other local pages we ship:

- binds to 127.0.0.1 only and has no host option; the port is chosen at random unless ``--port`` is given;
- every request must carry a ``Host`` of this server (127.0.0.1 or localhost with its port), which stops a
  DNS-rebinding page from reading it through the user's browser;
- every request must also carry this launch's random token: the URL printed at start has it, the first visit
  turns it into an HttpOnly, SameSite=Strict cookie and redirects to a URL without it;
- everything is GET and read-only, except the two POST endpoints below; any other method or POST path gets 405,
  no CORS headers are ever sent, a foreign ``Origin`` is refused;
- the page and its script, style and data come from this process, under a Content-Security-Policy that allows
  nothing else, so it works offline and cannot make a request to another site;
- request lines are never logged (the token is in the first URL; page data is never logged);
- the browser is opened on a one-time nonce URL (valid for 60 seconds, accepted once) instead of the token URL, so the
  long-lived token never appears in the process list; the nonce is exchanged for the same HttpOnly cookie. The address
  printed in the terminal keeps the token, so it can be pasted into another browser (a nonce would be spent).

Writes (the first the page has): ``POST /api/review/not-me`` and ``POST /api/review/its-me``, each with a JSON body
``{"group_id": "g_..."}``. A POST is refused unless ALL of these hold: the Host is this server's; the session cookie is
present and right (the header token that GET also accepts is not enough); ``Origin`` is present and is exactly the
page's own origin (``http://<Host>``), and ``Referer``, when sent, is on that origin; the ``X-Mannerism-Csrf`` header
carries this launch's CSRF token (a second random secret, handed out ONCE per launch by ``GET /api/csrf`` behind the
cookie: the first successful fetch wins, every later one is refused with ``{"ok": false, "error": "used"}``; the page
keeps it in its tab's ``sessionStorage``; it is never put in a URL, a cookie or the markup); the content type is
``application/json``; the body is at most ``MAX_BODY`` bytes with an ASCII-digit ``Content-Length`` and no chunking; the
group id has the ``g_``/``t_`` + 12 hex form, names a group that exists, and an optional ``expected_units`` must equal
that group's current size (so a stale page cannot delete a group that grew). The work itself is ``groups.not_me`` / ``groups.its_me``, which take the sync lock and use
their own short-lived write connection; the connection that serves GET stays read-only (``mode=ro``).

A cookie is not scoped to a port, so the browser also sends it to other servers on 127.0.0.1, and a program on this
computer that holds the cookie can also set any Origin header. Reading the page was always possible for such a program;
since the review actions exist, a holder of the cookie AND the CSRF token can delete stored texts. The one-time CSRF token
narrows that to whoever fetches it first (the page does, at load; a refused second fetch or a spent ``?k=`` link prints a
warning line in the terminal), but it does not remove it. Do not run ``mannerism ui`` on a shared machine.
"""
from __future__ import annotations

import hmac
import json
import os
import secrets
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlparse

from . import dashboard, groups

BIND = "127.0.0.1"
HEADER = "X-Mannerism-Token"
CSRF_HEADER = "X-Mannerism-Csrf"
MAX_BODY = 1024  # a write body is one short JSON object: {"group_id": "g_0123456789ab", "expected_units": 17}
MAX_UNITS = 10_000_000
POST_ACTIONS = {"/api/review/not-me": "not_me", "/api/review/its-me": "its_me"}
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/static/app.css": ("app.css", "text/css; charset=utf-8"),
          "/static/app.js": ("app.js", "application/javascript; charset=utf-8")}
NONCE_TTL_S = 60
CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src data:; connect-src 'self'; "
       "base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def _asset(name: str) -> bytes:
    return (resources.files("mannerism") / "webui" / name).read_bytes()


class VoiceServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, port: int = 0, token: str | None = None,
                 provider: Callable[[], dict] = dashboard.overview,
                 review_provider: Callable[[], dict] = dashboard.review,
                 group_lookup: Callable[[str], dict | None] = dashboard.group_info):
        super().__init__((BIND, port), Handler)
        self.token = token or secrets.token_urlsafe(24)
        self.csrf = secrets.token_urlsafe(24)  # a second secret, only ever sent in the body of GET /api/csrf
        self.provider = provider
        self.review_provider = review_provider
        self.group_lookup = group_lookup
        self._csrf_spent = False
        self._warned: set[str] = set()
        self._guard = threading.Lock()
        self.allowed_hosts = {f"127.0.0.1:{self.server_port}", f"localhost:{self.server_port}"}
        self.allowed_origins = {f"http://{h}" for h in self.allowed_hosts}
        self.cookie_name = f"mannerism_ui_{self.server_port}"
        self._nonces: dict[str, float] = {}
        self._nonce_lock = threading.Lock()

    def take_csrf(self) -> bool:
        """True for the first caller only: the CSRF token is handed out once per launch."""
        with self._guard:
            first, self._csrf_spent = not self._csrf_spent, True
            return first

    def warn_once(self, kind: str, message: str) -> None:
        """One line on the terminal per kind of refusal per launch (never the token, a URL or a header)."""
        with self._guard:
            if kind in self._warned:
                return
            self._warned.add(kind)
        print(f"mannerism ui: {message}", file=sys.stderr, flush=True)

    def issue_nonce(self, ttl: float = NONCE_TTL_S) -> str:
        """A one-time key for `?k=`: good for `ttl` seconds and for one visit."""
        nonce = secrets.token_urlsafe(16)
        now = time.monotonic()
        with self._nonce_lock:
            self._nonces = {k: t for k, t in self._nonces.items() if t > now}
            self._nonces[nonce] = now + ttl
        return nonce

    def use_nonce(self, candidate: str) -> bool:
        """True once for a live nonce; the nonce is spent. A wrong or expired one changes nothing."""
        now = time.monotonic()
        with self._nonce_lock:
            hit = next((k for k, t in self._nonces.items() if t > now and hmac.compare_digest(
                k.encode(), candidate.encode())), None) if candidate else None
            if hit is None:
                return False
            del self._nonces[hit]
            return True

    @property
    def open_url(self) -> str:
        """The address to hand a browser: it carries a nonce, never the token."""
        return f"http://127.0.0.1:{self.server_port}/?k={self.issue_nonce()}"

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/?t={self.token}"


class Handler(BaseHTTPRequestHandler):
    server: VoiceServer
    server_version = "mannerism"
    sys_version = ""
    timeout = 15  # a client that stops sending cannot hold a thread for ever

    def log_message(self, fmt: str, *args) -> None:  # request lines hold the token and the page data: log nothing
        return

    # -- plumbing ---------------------------------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", CSP)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _forbidden(self) -> None:
        self._json(403, {"ok": False, "message": "forbidden"})

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "").lower() in self.server.allowed_hosts

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        return origin is None or origin in self.server.allowed_origins

    def _cookies(self) -> list[str]:
        """Every value sent under this server's cookie name (another local server can set one of the same name)."""
        out = []
        for header in self.headers.get_all("Cookie", []):
            for part in header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == self.server.cookie_name:
                    out.append(value)
        return out

    def _token_ok(self, candidate: str) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.server.token.encode())

    def _cookie_ok(self) -> bool:
        return any(self._token_ok(v) for v in self._cookies())

    def _authed(self) -> bool:
        return self._token_ok(self.headers.get(HEADER, "")) or self._cookie_ok()

    # -- routes -----------------------------------------------------------------------------------------------
    def do_GET(self) -> None:
        if not (self._host_ok() and self._origin_ok()):
            return self._forbidden()
        url = urlparse(self.path)
        query = parse_qs(url.query)
        key = (query.get("k") or [""])[0]
        by_token = url.path == "/" and self._token_ok((query.get("t") or [""])[0])
        by_nonce = url.path == "/" and not by_token and bool(key) and self.server.use_nonce(key)
        if url.path == "/" and key and not (by_token or by_nonce):
            self.server.warn_once("nonce", "refused a page link that was already used or has expired. If you did not "
                                  "open it twice, another program on this computer may be probing the page.")
        if by_token or by_nonce:
            cookie = f"{self.server.cookie_name}={self.server.token}; HttpOnly; SameSite=Strict; Path=/"
            return self._send(302, b"", "text/plain", {"Location": "/", "Set-Cookie": cookie})
        if not self._authed():
            return self._forbidden()
        if url.path in STATIC:
            name, ctype = STATIC[url.path]
            return self._send(200, _asset(name), ctype)
        if url.path == "/api/overview":
            try:
                payload = self.server.provider()
            except Exception:  # noqa: BLE001 - the page shows a generic error; no stored text goes into a message
                return self._json(500, {"ok": False, "error": "could not read the store"})
            return self._json(200, payload)
        if url.path == "/api/review":
            try:
                payload = self.server.review_provider()
            except Exception:  # noqa: BLE001 - generic error only
                return self._json(500, {"ok": False, "error": "could not read the store"})
            return self._json(200, payload)
        if url.path == "/api/csrf":  # only with the session cookie: the header token alone is not a browser session
            if not self._cookie_ok():
                return self._forbidden()
            if not self.server.take_csrf():
                self.server.warn_once("csrf", "refused a second request for the page's one-time key. A second tab is "
                                      "read-only; if you did not open one, another program may hold your session cookie.")
                return self._json(403, {"ok": False, "error": "used"})
            return self._json(200, {"ok": True, "csrf": self.server.csrf})
        self._json(404, {"ok": False, "message": "not found"})

    # -- writes: the two review actions, nothing else ---------------------------------------------------------
    def _origin_exact(self) -> bool:
        """Origin is present and is exactly this page's origin; Referer, when sent, is on it too."""
        page = "http://" + self.headers.get("Host", "").lower()
        if self.headers.get("Origin") != page:
            return False
        referer = self.headers.get("Referer")
        if referer is None:
            return True
        parts = urlparse(referer)
        return f"{parts.scheme}://{parts.netloc}".lower() == page

    def _body(self) -> bytes | None:
        """The JSON body, or None after an error response has been sent."""
        if self.headers.get("Transfer-Encoding"):
            self._json(411, {"ok": False, "message": "send a Content-Length"})
            return None
        raw = self.headers.get("Content-Length", "")
        if not (raw.isascii() and raw.isdigit()) or len(raw) > 6:  # str.isdigit() also accepts "²" and Arabic digits
            self._json(411, {"ok": False, "message": "send a Content-Length"})
            return None
        size = int(raw)
        if size > MAX_BODY:
            self._json(413, {"ok": False, "message": "body too large"})
            return None
        try:
            data = self.rfile.read(size)
        except OSError:  # the client sent fewer bytes than it promised and the socket timed out
            data = b""
        if len(data) != size:
            self.close_connection = True
            self._json(400, {"ok": False, "message": "the body is shorter than its Content-Length"})
            return None
        return data

    def do_POST(self) -> None:
        url = urlparse(self.path)
        action = POST_ACTIONS.get(url.path)
        if action is None or url.query:
            return self._no_method()
        # Cheapest checks first, and no body is read before every header has passed.
        if not self._host_ok():
            return self._forbidden()
        if not self._cookie_ok() or not self._origin_exact():
            return self._forbidden()
        csrf = self.headers.get(CSRF_HEADER, "")
        if not csrf or not hmac.compare_digest(csrf.encode(), self.server.csrf.encode()):
            return self._forbidden()
        if self.headers.get("Content-Type", "").split(";")[0].strip().lower() != "application/json":
            return self._json(415, {"ok": False, "message": "send application/json"})
        body = self._body()
        if body is None:
            return
        try:
            data = json.loads(body)
        except (ValueError, RecursionError):
            data = None
        shape = isinstance(data, dict) and "group_id" in data and set(data) <= {"group_id", "expected_units"}
        gid = data["group_id"] if shape else None
        expected = data.get("expected_units") if shape else None
        if (not isinstance(gid, str) or not groups.is_group_id(gid)
                or (expected is not None and (type(expected) is not int or not 0 <= expected <= MAX_UNITS))):
            return self._json(400, {"ok": False, "error": "group_id must look like g_0123456789ab"})
        try:
            info = self.server.group_lookup(gid)
            if info is None:
                return self._json(404, {"ok": False, "error": "that group is not in the store (it may already be gone)"})
            if action == "not_me" and info.get("mixed"):  # the page never forces this; see groups.not_me
                return self._json(409, {"ok": False, "error": "these older texts come from several places; run "
                                        "`mannerism review rescan` to sort them first"})
            if expected is not None and info["units"] != expected:
                return self._json(409, {"ok": False, "error": "this group changed, refresh"})
            kwargs = {"expected_units": expected} if expected is not None else {}
            result = getattr(groups, action)(gid, **kwargs)
        except Exception:  # noqa: BLE001 - generic error only; stored text never goes into a message
            return self._json(500, {"ok": False, "error": "could not complete that action"})
        dashboard.invalidate()
        self._json(200 if result.get("ok") else 409, result)

    def _no_method(self) -> None:
        self._send(405, b'{"ok": false, "message": "read-only"}', "application/json; charset=utf-8",
                   {"Allow": "GET"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _no_method


def can_open_browser(env=None, platform: str | None = None) -> bool:
    """False in a headless session, where opening a browser would fail or start a text browser in this terminal."""
    env = os.environ if env is None else env
    platform = sys.platform if platform is None else platform
    if platform in ("darwin", "win32", "cygwin"):
        return True
    return bool(env.get("DISPLAY") or env.get("WAYLAND_DISPLAY"))


def open_in_browser(url: str) -> bool:
    """Ask the default browser to open ``url``. Never raises; False when there is no display or the open failed.

    ``webbrowser`` puts its argument in the process list, so callers pass a nonce URL (`VoiceServer.open_url`)."""
    if not can_open_browser():
        return False
    try:
        return bool(webbrowser.open(url))
    except Exception:  # noqa: BLE001 - the address is printed anyway; a failed open must not stop the server
        return False


def run(port: int = 0, open_browser: bool = True, server: VoiceServer | None = None) -> int:
    """Serve the page until Ctrl-C. Prints the address; with ``open_browser`` also opens it once listening."""
    server = server or VoiceServer(port)
    print(f"mannerism voice page: {server.url}  (Ctrl-C to stop)", flush=True)
    try:
        if open_browser and not open_in_browser(server.open_url):
            print("Could not open a browser here. Open the address above in your browser.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
