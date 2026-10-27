"""A fake network for the social-source tests: replaces `_http.urlopen`; nothing here touches a socket.

Fixtures are written by hand from the official API documentation (Bluesky getAuthorFeed, Mastodon REST, X API v2) with
fictional accounts."""
from __future__ import annotations

import io
import json
import urllib.error
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

import pytest

from mannerism.adapters import _http


class Hdrs(dict):
    def __init__(self, items=None):
        super().__init__({k.lower(): v for k, v in (items or {}).items()})

    def get(self, key, default=None):
        return super().get(key.lower(), default)


class Resp:
    def __init__(self, body, status=200, headers=None):
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.status = status
        self.headers = Hdrs(headers)

    def read(self, n=-1):
        return self._body if n is None or n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def ok(obj, headers=None) -> Resp:
    return Resp(obj, 200, headers)


def http_error(url, code, headers=None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "err", Hdrs(headers), io.BytesIO(b"{}"))


@dataclass
class Call:
    url: str
    headers: dict
    timeout: float

    @property
    def parts(self):
        return urlsplit(self.url)

    @property
    def query(self) -> dict:
        return dict(parse_qsl(self.parts.query))


class FakeNet:
    """`handler(urlparts, query, headers)` returns a Resp or raises (TimeoutError, http_error(...), ...)."""

    def __init__(self):
        self.calls: list[Call] = []
        self.handler = lambda u, q, h: (_ for _ in ()).throw(AssertionError(f"unexpected request {u.geturl()}"))

    def __call__(self, req, timeout):
        call = Call(req.full_url, {k.lower(): v for k, v in req.header_items()}, timeout)
        self.calls.append(call)
        return self.handler(call.parts, call.query, call.headers)

    def paths(self, suffix: str) -> list[Call]:
        return [c for c in self.calls if c.parts.path.endswith(suffix)]


@pytest.fixture
def net(monkeypatch) -> FakeNet:
    fake = FakeNet()
    monkeypatch.setattr(_http, "urlopen", fake)
    return fake
