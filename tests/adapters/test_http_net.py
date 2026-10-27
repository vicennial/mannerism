import json
import time

import pytest

from mannerism import __version__
from mannerism.adapters import _http
from mannerism.adapters._http import NetError, build_url, get_json, retry_after_s

from .fakenet import Resp, http_error, ok


def test_get_json_sets_user_agent_timeout_and_returns_headers(net):
    net.handler = lambda u, q, h: ok({"a": 1}, {"X-Thing": "z"})
    data, hdrs = get_json("https://api.example.com/x?y=1", {"Authorization": "Bearer t"})
    assert data == {"a": 1} and hdrs.get("x-thing") == "z"
    c = net.calls[0]
    assert c.timeout == 10.0 and c.headers["user-agent"] == f"mannerism/{__version__}"
    assert c.headers["authorization"] == "Bearer t"


def test_non_https_refused_without_a_request(net):
    with pytest.raises(NetError) as e:
        get_json("http://api.example.com/x")
    assert e.value.kind == "url" and net.calls == []


def test_redirect_to_another_host_refused_and_credentials_not_sent(net):
    def handler(u, q, h):
        raise http_error(u.geturl(), 302, {"Location": "https://evil.example.net/steal"})

    net.handler = handler
    with pytest.raises(NetError) as e:
        get_json("https://api.example.com/x", {"Authorization": "Bearer secret"})
    assert e.value.kind == "redirect"
    assert [c.parts.hostname for c in net.calls] == ["api.example.com"]  # the second host was never contacted


def test_redirect_to_http_or_other_port_refused(net):
    for target in ("http://api.example.com/x", "https://api.example.com:8443/x"):
        net.calls.clear()
        net.handler = lambda u, q, h, t=target: (_ for _ in ()).throw(http_error(u.geturl(), 301, {"Location": t}))
        with pytest.raises(NetError):
            get_json("https://api.example.com/x")
        assert len(net.calls) == 1


def test_same_origin_redirect_followed(net):
    def handler(u, q, h):
        if u.path == "/old":
            raise http_error(u.geturl(), 301, {"Location": "/new"})
        return ok({"ok": True})

    net.handler = handler
    assert get_json("https://api.example.com/old")[0] == {"ok": True}
    assert net.calls[1].headers["user-agent"].startswith("mannerism/")


def test_json_size_cap(net, monkeypatch):
    net.handler = lambda u, q, h: Resp(b'{"a": "' + b"x" * 100 + b'"}')
    with pytest.raises(NetError) as e:
        get_json("https://api.example.com/x", max_bytes=50)
    assert e.value.kind == "too_large"


def test_bad_json_timeout_and_http_errors(net):
    net.handler = lambda u, q, h: Resp(b"<html>")
    with pytest.raises(NetError) as e:
        get_json("https://api.example.com/x")
    assert e.value.kind == "bad_json"

    def boom(u, q, h):
        raise TimeoutError("slow")

    net.handler = boom
    with pytest.raises(NetError) as e:
        get_json("https://api.example.com/x")
    assert e.value.kind == "timeout"

    net.handler = lambda u, q, h: (_ for _ in ()).throw(http_error(u.geturl(), 503))
    with pytest.raises(NetError) as e:
        get_json("https://api.example.com/x?token=SECRET")
    assert e.value.kind == "http" and e.value.status == 503 and "SECRET" not in str(e.value)


def test_429_carries_retry_hint(net):
    reset = int(time.time()) + 120
    net.handler = lambda u, q, h: (_ for _ in ()).throw(http_error(u.geturl(), 429, {"x-rate-limit-reset": str(reset)}))
    with pytest.raises(NetError) as e:
        get_json("https://api.example.com/x")
    assert e.value.kind == "rate_limit" and 100 < e.value.retry_after_s <= 121


def test_retry_after_forms():
    assert retry_after_s({"retry-after": "30"}) == 30
    assert retry_after_s({}) == _http.DEFAULT_RETRY_S
    assert retry_after_s({"retry-after": "999999999"}) <= 1e9  # absurd values still clamp below
    assert retry_after_s({"x-rate-limit-reset": str(int(time.time()) + 50)}) == pytest.approx(50, abs=2)
    assert retry_after_s({"ratelimit-reset": str(int(time.time()) + 70)}) == pytest.approx(70, abs=2)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 90))
    assert retry_after_s({"x-ratelimit-reset": iso}) == pytest.approx(90, abs=2)
    assert retry_after_s({"retry-after": "0"}) == 1


def test_build_url_skips_none_and_encodes():
    url = build_url("a.example.com", "/p", {"q": "x y", "n": None, "k": 3, "c": "a,b"})
    assert url == "https://a.example.com/p?q=x+y&k=3&c=a,b"
    assert json.loads(json.dumps({"ok": 1}))
