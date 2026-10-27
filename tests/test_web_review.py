"""The "Is this you?" page: its first writes (POST) and the checks that guard them, end to end on a synthetic store."""
from __future__ import annotations

import hashlib
import http.client
import json
import re
import shutil
import sqlite3
import threading
from importlib import resources

import pytest
from transcripts import world

from mannerism import config, dashboard, groups, store, sync, web


@pytest.fixture(autouse=True)
def _fresh_cache():
    dashboard._CACHE.clear()
    yield
    dashboard._CACHE.clear()


@pytest.fixture(scope="module")
def _synced_template(tmp_path_factory):
    """Read the synthetic machine once (a sync is the slow part); each test then works on its own copy."""
    root = tmp_path_factory.mktemp("review-template")
    home = root / "home"
    home.mkdir()
    mp = pytest.MonkeyPatch()
    mp.setenv("HOME", str(home))
    mp.setenv("MANNERISM_HOME", str(home / ".mannerism"))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "APPDATA"):
        mp.delenv(var, raising=False)
    try:
        paths = world(home)
        cfg = config.load_config()
        cfg.sources["claude_code"] = {"enabled": True, "paths": [], "dirs": [str(paths["b"])]}
        cfg.sources["codex"] = {"enabled": True, "paths": []}
        config.save_config(cfg)
        sync.sync()
    finally:
        mp.undo()
    return home


@pytest.fixture
def synced(_synced_template, tmp_path, monkeypatch):
    home = tmp_path / "copy"
    shutil.copytree(_synced_template, home)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("MANNERISM_HOME", str(home / ".mannerism"))
    return home


class Live:
    def __init__(self, server: web.VoiceServer):
        self.server = server
        self.port = server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def request(self, method, path, headers, body=None):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        con.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        for k, v in headers.items():
            con.putheader(k, v)
        if body is None:
            con.endheaders()
        else:
            con.endheaders(body if isinstance(body, bytes) else body.encode())
        r = con.getresponse()
        raw = r.read()
        out = (r.status, {k.lower(): v for k, v in r.getheaders()}, raw)
        con.close()
        return out

    def cookie(self):
        return f"{self.server.cookie_name}={self.server.token}"

    def get(self, path, **over):
        h = {"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie(), **over}
        return self.request("GET", path, {k: v for k, v in h.items() if v is not None})

    def csrf(self):
        return json.loads(self.get("/api/csrf")[2])["csrf"]

    def post(self, action, body=None, *, raw=None, **over):
        """A well-formed browser POST, with any header replaced (value) or dropped (None) through `over`."""
        data = raw if raw is not None else json.dumps(body if body is not None else {}).encode()
        h = {"Host": f"127.0.0.1:{self.port}", "Cookie": self.cookie(), "Origin": self.origin,
             web.CSRF_HEADER: self.server.csrf, "Content-Type": "application/json",
             "Content-Length": str(len(data)), **over}
        return self.request("POST", f"/api/review/{action}", {k: v for k, v in h.items() if v is not None}, data)


@pytest.fixture
def live():
    srv = web.VoiceServer(0)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    yield Live(srv)
    srv.shutdown()
    srv.server_close()


def template_group():
    con = store.connect()
    try:
        return next(g for g in groups.list_groups(con) if g["kind"] == "template")
    finally:
        con.close()


def counts():
    con = store.connect()
    try:
        return {"units": con.execute("SELECT count(*) FROM units").fetchone()[0],
                "held": con.execute("SELECT count(*) FROM units WHERE held=1").fetchone()[0],
                "excl": con.execute("SELECT count(*) FROM exclusions").fetchone()[0],
                "decisions": con.execute("SELECT count(*) FROM group_decisions").fetchone()[0]}
    finally:
        con.close()


# --- the CSRF token and the read endpoints ----------------------------------------------------------------------------

def test_csrf_token_comes_only_with_the_session_cookie(live, synced):
    assert live.get("/api/csrf", Cookie=None)[0] == 403
    assert live.get("/api/csrf", Cookie=f"{live.server.cookie_name}=wrong")[0] == 403
    assert live.get("/api/csrf", Cookie=None, **{web.HEADER: live.server.token})[0] == 403  # the header alone is not a session
    assert live.get("/api/csrf", Host="evil.example")[0] == 403
    status, headers, body = live.get("/api/csrf")  # none of the refusals above used up the one-time token
    d = json.loads(body)
    assert status == 200 and d["csrf"] == live.server.csrf and d["csrf"] != live.server.token
    assert headers["cache-control"] == "no-store" and "set-cookie" not in headers


def test_csrf_token_is_handed_out_once_per_launch(live, synced, capsys):
    assert live.get("/api/csrf")[0] == 200
    for _ in range(3):
        st, headers, body = live.get("/api/csrf")
        assert st == 403 and json.loads(body) == {"ok": False, "error": "used"} and live.server.csrf.encode() not in body
    err = capsys.readouterr().err
    assert err.count("one-time key") == 1 and live.server.csrf not in err and live.server.token not in err  # one warning line
    assert live.post("not-me", {"group_id": template_group()["group_id"]})[0] == 200  # the first holder can still write


def test_a_spent_page_link_warns_once_on_the_terminal(live, synced, capsys):
    nonce = live.server.issue_nonce()
    assert live.request("GET", f"/?k={nonce}", {"Host": f"127.0.0.1:{live.port}"})[0] == 302
    assert capsys.readouterr().err == ""
    for _ in range(2):
        assert live.request("GET", f"/?k={nonce}", {"Host": f"127.0.0.1:{live.port}"})[0] == 403
    err = capsys.readouterr().err
    assert err.count("already used or has expired") == 1 and nonce not in err


def test_the_csrf_token_appears_nowhere_else(live, synced):
    for path in ("/", "/static/app.js", "/static/app.css", "/api/overview", "/api/review"):
        status, headers, body = live.get(path)
        assert status == 200
        assert live.server.csrf.encode() not in body and live.server.csrf not in "".join(headers.values()), path
    assert live.server.csrf != live.server.token
    assert web.VoiceServer(0).csrf != web.VoiceServer(0).csrf  # one per launch


def test_review_endpoint_lists_groups_held_first_and_needs_the_session(live, synced):
    assert live.get("/api/review", Cookie=None)[0] == 403
    assert live.get("/api/review", Host="evil.example")[0] == 403
    status, headers, body = live.get("/api/review")
    d = json.loads(body)
    assert status == 200 and d["ok"] and d["held_total"] > 0 and d["groups_total"] == len(d["groups"])
    flags = [g["held"] > 0 for g in d["groups"]]
    assert flags == sorted(flags, reverse=True) and flags[0]
    assert {g["kind"] for g in d["groups"]} == {"origin", "template"}
    assert all(set(g) >= {"group_id", "source", "account", "project", "session_kind", "units", "held", "first", "last",
                          "samples"} for g in d["groups"])
    assert "jo.dev@example.org" not in body.decode()
    assert "default-src 'none'" in headers["content-security-policy"]


def test_review_is_capped_and_counts_the_rest(synced, monkeypatch):
    monkeypatch.setattr(dashboard, "REVIEW_MAX_GROUPS", 2)
    d = dashboard.review()
    assert len(d["groups"]) == 2 and d["groups_total"] > 2 and d["groups"][0]["held"] > 0


def test_review_on_a_fresh_home_is_empty_and_creates_nothing(home):
    assert dashboard.review() == {"ok": True, "groups_total": 0, "held_groups": 0, "groups": [], "held_total": 0,
                                  "decisions": []}
    assert dashboard.group_info("g_0123456789ab") is None
    assert not home.exists()


def test_an_old_store_is_reported_not_migrated(synced):
    con = store.connect()
    con.execute("UPDATE meta SET value='3' WHERE key='schema_version'")
    con.close()
    d = dashboard.review()
    assert d["ok"] is False and "upgrade" in d["error"]


def test_get_endpoints_are_read_only(live, synced):
    db = store.db_path()
    digest = lambda: hashlib.sha256(db.read_bytes()).hexdigest()  # noqa: E731
    before, rows = digest(), counts()
    for path in ("/", "/api/overview", "/api/review", "/api/csrf", "/static/app.js"):
        assert live.get(path)[0] == 200
    assert live.get("/api/review/not-me")[0] == 404  # a GET on a write path does nothing
    assert digest() == before and counts() == rows
    con = dashboard._open_review()
    try:
        for sql in ("DELETE FROM units", "UPDATE units SET held=0", "INSERT INTO group_decisions VALUES ('g_0123456789ab','keep','x')"):
            with pytest.raises(sqlite3.OperationalError, match="readonly|read-only|attempt to write"):
                con.execute(sql)
    finally:
        con.close()


# --- every way a POST can be refused ----------------------------------------------------------------------------------

GOOD = "g_0123456789ab"


@pytest.mark.parametrize("name,over,status", [
    ("no cookie", {"Cookie": None}, 403),
    ("header token instead of cookie", {"Cookie": None, web.HEADER: "%TOKEN%"}, 403),
    ("right cookie name, wrong value", {"Cookie": "%COOKIE%=wrong"}, 403),
    ("a cookie of another name", {"Cookie": "other=%TOKEN%"}, 403),
    ("no origin", {"Origin": None}, 403),
    ("foreign origin", {"Origin": "https://evil.example"}, 403),
    ("null origin", {"Origin": "null"}, 403),
    ("localhost origin on a 127.0.0.1 page", {"Origin": "http://localhost:%PORT%"}, 403),
    ("origin with a path", {"Origin": "http://127.0.0.1:%PORT%/"}, 403),
    ("https origin", {"Origin": "https://127.0.0.1:%PORT%"}, 403),
    ("foreign referer", {"Referer": "https://evil.example/x"}, 403),
    ("lookalike referer", {"Referer": "http://127.0.0.1:%PORT%.evil.example/x"}, 403),
    ("no csrf header", {web.CSRF_HEADER: None}, 403),
    ("wrong csrf", {web.CSRF_HEADER: "wrong"}, 403),
    ("empty csrf", {web.CSRF_HEADER: ""}, 403),
    ("session token as csrf", {web.CSRF_HEADER: "%TOKEN%"}, 403),
    ("foreign host", {"Host": "evil.example"}, 403),
    ("host without port", {"Host": "127.0.0.1"}, 403),
    ("form content type", {"Content-Type": "application/x-www-form-urlencoded"}, 415),
    ("text/plain", {"Content-Type": "text/plain"}, 415),
    ("multipart", {"Content-Type": "multipart/form-data; boundary=x"}, 415),
    ("no content type", {"Content-Type": None}, 415),
    ("no content length", {"Content-Length": None}, 411),
    ("chunked", {"Transfer-Encoding": "chunked"}, 411),
    ("bad content length", {"Content-Length": "abc"}, 411),
    ("non-ASCII digit content length", {"Content-Length": "\u00b2"}, 411),
    ("signed content length", {"Content-Length": "+5"}, 411),
    ("negative content length", {"Content-Length": "-5"}, 411),
    ("two content lengths in one", {"Content-Length": "5, 5"}, 411),
])
@pytest.mark.parametrize("action", ["not-me", "its-me"])
def test_a_post_that_breaks_a_rule_is_refused_and_changes_nothing(live, synced, name, over, status, action):
    gid = template_group()["group_id"]
    rows = counts()
    over = {k: (v.replace("%PORT%", str(live.port)).replace("%TOKEN%", live.server.token).replace("%COOKIE%", live.server.cookie_name) if isinstance(v, str) else v)
            for k, v in over.items()}
    st, headers, body = live.post(action, {"group_id": gid}, **over)
    assert st == status, (name, st, body)
    assert counts() == rows, name
    assert not any(k.startswith("access-control-") for k in headers) and "set-cookie" not in headers


@pytest.mark.parametrize("action", ["not-me", "its-me"])
def test_a_referer_on_the_page_origin_is_fine(live, synced, action):
    gid = template_group()["group_id"]
    st, *_ = live.post(action, {"group_id": gid}, Referer=f"{live.origin}/?x=1")
    assert st == 200


def test_a_post_that_is_too_big_is_refused_before_it_is_read(live, synced):
    rows = counts()
    big = json.dumps({"group_id": GOOD, "pad": "x" * (web.MAX_BODY + 10)}).encode()
    st, _h, body = live.post("not-me", raw=big)
    assert st == 413 and counts() == rows
    st, *_ = live.post("not-me", raw=b"{" * 50, **{"Content-Length": str(web.MAX_BODY + 1)})
    assert st == 413


@pytest.mark.parametrize("payload", [
    b"", b"not json", b"[]", b'"g_0123456789ab"', b"null", b"{}", b'{"group_id": 5}', b'{"group_id": null}',
    b'{"group_id": ["g_0123456789ab"]}', b'{"group_id": "g_0123456789ab", "extra": 1}', b'{"id": "g_0123456789ab"}',
    b'{"group_id": ""}', b'{"group_id": "G_0123456789AB"}', b'{"group_id": "g_0123456789a"}',
    b'{"group_id": "g_0123456789abc"}', b'{"group_id": "x_0123456789ab"}', b'{"group_id": "g_0123456789ag"}',
    b'{"group_id": "../../etc/passwd"}', b'{"group_id": "g_0123456789ab\\n"}', b'{"group_id": "g_0123456789ab; DROP"}',
    b"[" * 1000, b'{"group_id": "' + b"a" * 900 + b'"}',
    b'{"group_id": "g_0123456789ab", "expected_units": "5"}', b'{"group_id": "g_0123456789ab", "expected_units": 5.5}',
    b'{"group_id": "g_0123456789ab", "expected_units": true}', b'{"group_id": "g_0123456789ab", "expected_units": -1}',
    b'{"group_id": "g_0123456789ab", "expected_units": 10000001}', b'{"group_id": "g_0123456789ab", "expected_units": null, "x": 1}',
    b'{"group_id": "g_0123456789ab", "expected_units": [5]}', b'{"expected_units": 5}', b'{"group_id": "g_0123456789ab", "force": true}',
], ids=lambda p: repr(p)[:40])
@pytest.mark.parametrize("action", ["not-me", "its-me"])
def test_a_bad_body_or_group_id_is_refused_before_anything_runs(live, synced, payload, action):
    rows = counts()
    st, _h, body = live.post(action, raw=payload)
    assert st == 400 and json.loads(body)["ok"] is False and counts() == rows
    assert "etc/passwd" not in body.decode() and "DROP" not in body.decode()  # input is never echoed


@pytest.mark.parametrize("action", ["not-me", "its-me"])
def test_a_well_formed_id_of_no_group_is_a_404_and_stores_no_rule(live, synced, action):
    rows = counts()
    st, _h, body = live.post(action, {"group_id": GOOD})
    assert st == 404 and json.loads(body)["ok"] is False and counts() == rows


def test_nothing_else_accepts_a_post_or_any_other_write_method(live, synced):
    rows = counts()
    base = {"Host": f"127.0.0.1:{live.port}", "Cookie": live.cookie(), "Origin": live.origin, web.CSRF_HEADER: live.server.csrf,
            "Content-Type": "application/json", "Content-Length": "2"}
    for path in ("/api/overview", "/api/review", "/api/csrf", "/", "/static/app.js", "/api/review/other", "/api/review/not-me?x=1",
                 "/api/review/not-me/", "/nothing"):
        st, headers, _ = live.request("POST", path, base, b"{}")
        assert st == 405 and headers["allow"] == "GET", path
    for method in ("PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
        for path in ("/api/review/not-me", "/api/review/its-me", "/api/overview"):
            st, headers, _ = live.request(method, path, base, b"{}")
            assert st == 405 and headers["allow"] == "GET", (method, path)
    assert counts() == rows


# --- both actions, end to end ------------------------------------------------------------------------------------------

def test_not_me_deletes_the_group_stores_a_rule_and_the_page_data_follows(live, synced):
    g = template_group()
    rows = counts()
    assert g["units"] == 14 and rows["held"] >= 14
    st, headers, body = live.post("not-me", {"group_id": g["group_id"]})
    res = json.loads(body)
    assert st == 200 and res["ok"] and res["removed"] == 14 and res["created"] is True
    assert "default-src 'none'" in headers["content-security-policy"] and headers["cache-control"] == "no-store"
    after = counts()
    assert after["units"] == rows["units"] - 14 and after["excl"] == rows["excl"] + 1
    con = store.connect()
    assert con.execute("SELECT grp FROM exclusions").fetchone()[0] == g["group_id"]
    assert con.execute("SELECT count(*) FROM units WHERE template=?", (g["group_id"][2:],)).fetchone()[0] == 0
    con.close()
    d = json.loads(live.get("/api/review")[2])
    assert g["group_id"] not in {x["group_id"] for x in d["groups"]} and d["held_total"] < rows["held"]
    assert json.loads(live.get("/api/overview")[2])["total_units"] == after["units"]  # the cached overview was dropped
    sync.sync()
    assert counts()["units"] == after["units"]  # and a later sync does not bring it back
    st, _h, body = live.post("not-me", {"group_id": g["group_id"]})
    assert st == 404  # already gone


def test_not_me_on_an_origin_group_deletes_only_that_group(live, synced):
    con = store.connect()
    target = next(x for x in groups.list_groups(con) if x["kind"] == "origin" and x["project"] == "app" and x["source"] == "codex")
    others = con.execute("SELECT count(*) FROM units").fetchone()[0] - target["units"]
    con.close()
    st, _h, body = live.post("not-me", {"group_id": target["group_id"]})
    assert st == 200 and json.loads(body)["removed"] == target["units"]
    assert counts()["units"] == others


def test_its_me_releases_held_texts_and_remembers_the_answer(live, synced):
    g = template_group()
    rows = counts()
    st, _h, body = live.post("its-me", {"group_id": g["group_id"]})
    res = json.loads(body)
    assert st == 200 and res["ok"] and res["released"] == 14
    after = counts()
    assert after["units"] == rows["units"] and after["held"] == rows["held"] - 14
    assert after["decisions"] == 1 and after["excl"] == rows["excl"]
    d = json.loads(live.get("/api/review")[2])
    assert d["decisions"] == [g["group_id"]]
    again = next(x for x in d["groups"] if x["group_id"] == g["group_id"])
    assert again["held"] == 0 and again["decision"] == "keep" and again["units"] == 14
    sync.sync()  # later texts of the group are never held again
    assert counts()["held"] == after["held"]


def test_an_engine_failure_gives_a_generic_message(live, synced, monkeypatch):
    def boom(_gid):
        raise RuntimeError("secret text from the store")
    monkeypatch.setattr(groups, "not_me", boom)
    st, _h, body = live.post("not-me", {"group_id": template_group()["group_id"]})
    assert st == 500 and b"secret" not in body and json.loads(body)["ok"] is False


def test_a_busy_sync_lock_is_reported_not_forced(live, synced, monkeypatch):
    rows = counts()
    monkeypatch.setattr(sync, "acquire_lock", lambda *a, **k: None)
    for action in ("not-me", "its-me"):
        st, _h, body = live.post(action, {"group_id": template_group()["group_id"]})
        assert st == 409 and "sync" in json.loads(body)["error"]
    assert counts() == rows


def test_writes_use_their_own_connection_not_the_page_read_connection(live, synced, monkeypatch):
    seen = []
    real = groups.not_me
    monkeypatch.setattr(groups, "not_me", lambda gid: (seen.append(gid), real(gid))[1])
    opened = []
    real_ro = dashboard._open_readonly
    monkeypatch.setattr(dashboard, "_open_readonly", lambda: (opened.append(1), real_ro())[1])
    gid = template_group()["group_id"]
    assert live.post("not-me", {"group_id": gid})[0] == 200
    assert seen == [gid] and opened  # the existence check read through the read-only connection, the write did not
    con = real_ro()
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM exclusions")
    con.close()


# --- the page itself ----------------------------------------------------------------------------------------------------

def test_the_page_script_posts_only_json_with_the_csrf_header_and_builds_the_dom_from_text():
    js = (resources.files("mannerism") / "webui" / "app.js").read_text(encoding="utf-8")
    assert js.count('method: "POST"') == 1 and "X-Mannerism-Csrf" in js and '"Content-Type": "application/json"' in js
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|localStorage\.setItem\(\s*[\"']mannerism-csrf",
                         js)
    assert js.count("Not me") >= 1 and "It's me" in js and "Is this you?" in js
    for text in ("Nothing needs a second look", "Delete "):
        assert text in js
    html = (resources.files("mannerism") / "webui" / "index.html").read_text(encoding="utf-8")
    assert "<script>" not in html and web.CSP.count("'unsafe-inline'") == 0 and "script-src 'self'" in web.CSP


# --- a richer synthetic store (the one the screenshots use) -------------------------------------------------------------

def test_the_fixture_store_lists_held_groups_first_with_masked_samples(live, home):
    import uifixture
    uifixture.populate()
    uifixture.populate_review()
    d = json.loads(live.get("/api/review")[2])
    held = [g for g in d["groups"] if g["held"]]
    assert held and [g["held"] > 0 for g in d["groups"]][:len(held)] == [True] * len(held)
    assert {g["session_kind"] for g in held if g["kind"] == "origin"} == {"headless", "relay"}
    assert any(g["kind"] == "template" and g["units"] == 16 for g in held)
    assert "mara.quill@example.org" not in live.get("/api/review")[2].decode()
    orch = next(g for g in held if g["kind"] == "origin" and g["project"] == "orchestrator")
    assert orch["account"] == "~/.claude-work" and orch["units"] == 17 and orch["held"] == 17
    before = counts()
    assert json.loads(live.post("its-me", {"group_id": orch["group_id"]})[2])["released"] == 17
    assert counts()["held"] == before["held"] - 17
    relay = next(g for g in held if g["project"] == "relay")
    res = json.loads(live.post("not-me", {"group_id": relay["group_id"]})[2])
    assert res["ok"] and res["removed"] == 9


# --- review round: expected_units, short bodies, cookies, mixed groups, breakdown, errors ---------------------------------

def test_expected_units_must_match_or_the_write_is_refused(live, synced):
    g = template_group()
    rows = counts()
    st, _h, body = live.post("not-me", {"group_id": g["group_id"], "expected_units": g["units"] + 1})
    assert st == 409 and "changed" in json.loads(body)["error"] and counts() == rows
    st, _h, body = live.post("its-me", {"group_id": g["group_id"], "expected_units": 0})
    assert st == 409 and counts() == rows
    st, _h, body = live.post("not-me", {"group_id": g["group_id"], "expected_units": g["units"]})
    assert st == 200 and json.loads(body)["removed"] == g["units"]


def test_a_body_shorter_than_its_content_length_is_refused_not_waited_for(live, synced, monkeypatch):
    monkeypatch.setattr(web.Handler, "timeout", 0.4)
    rows = counts()
    body = json.dumps({"group_id": template_group()["group_id"]}).encode()
    st, _h, out = live.post("not-me", raw=body, **{"Content-Length": str(len(body) + 40)})
    assert st == 400 and json.loads(out)["ok"] is False and counts() == rows


def test_any_cookie_of_the_right_name_may_carry_the_session(live, synced):
    name, good = live.server.cookie_name, live.server.token
    assert live.get("/api/review", Cookie=f"{name}=stale; {name}={good}")[0] == 200  # another local server set a stale one
    assert live.get("/api/review", Cookie=f"{name}={good}; {name}=stale")[0] == 200
    assert live.get("/api/review", Cookie=f"{name}=stale; {name}=also-stale")[0] == 403
    gid = template_group()["group_id"]
    assert live.post("its-me", {"group_id": gid}, Cookie=f"{name}=stale; {name}={good}")[0] == 200


def test_review_sends_no_rule_text_or_scores_and_template_groups_say_where_they_live(live, synced):
    d = json.loads(live.get("/api/review")[2])
    assert "rules" not in d and all("mean_score" not in g for g in d["groups"])
    tmpl = next(g for g in d["groups"] if g["kind"] == "template")
    b = tmpl["breakdown"]
    assert len(b["by_origin"]) == dashboard.BREAKDOWN_ROWS and b["more"] == 14 - dashboard.BREAKDOWN_ROWS  # 14 projects
    assert sum(o["units"] for o in b["by_origin"]) + b["more"] == tmpl["units"]  # one text per project here
    assert {o["source"] for o in b["by_origin"]} == {"claude_code"} and all(o["project"].startswith("run") for o in b["by_origin"])
    assert all(set(o) == {"source", "project", "units"} for o in b["by_origin"])
    assert all("breakdown" not in g for g in d["groups"] if g["kind"] == "origin")


def test_the_breakdown_is_capped(synced, monkeypatch):
    monkeypatch.setattr(dashboard, "BREAKDOWN_ROWS", 1)
    tmpl = next(g for g in dashboard.review()["groups"] if g["kind"] == "template")
    assert len(tmpl["breakdown"]["by_origin"]) == 1 and tmpl["breakdown"]["more"] >= 0
    assert sum(o["units"] for o in tmpl["breakdown"]["by_origin"]) <= tmpl["units"]


@pytest.mark.parametrize("message,upgrade", [("no such column: account", True), ("no such table: group_decisions", True),
                                             ("database is locked", False), ("disk I/O error", False)])
def test_only_a_schema_problem_is_reported_as_an_upgrade(synced, monkeypatch, message, upgrade):
    def boom(*_a, **_k):
        raise sqlite3.OperationalError(message)
    monkeypatch.setattr(groups, "review_state", boom)
    d = dashboard.review()
    assert d["ok"] is False and ("upgrade" in d["error"]) is upgrade
    assert message not in d["error"]


def test_a_mixed_group_is_never_deleted_from_the_page(synced, monkeypatch):
    """The engine will flag groups built from older texts without account / project as `mixed`; the page never forces."""
    real = dashboard.group_info
    stub = lambda gid: {**real(gid), "mixed": True}  # noqa: E731
    srv = web.VoiceServer(0, group_lookup=stub)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()
    try:
        live = Live(srv)
        gid = template_group()["group_id"]
        rows = counts()
        calls = []
        monkeypatch.setattr(groups, "not_me", lambda g: calls.append(g) or {"ok": True})
        st, _h, body = live.post("not-me", {"group_id": gid})
        assert st == 409 and "rescan" in json.loads(body)["error"] and calls == [] and counts() == rows
        assert live.post("its-me", {"group_id": gid})[0] == 200  # releasing held texts is still fine
    finally:
        srv.shutdown()
        srv.server_close()


def test_an_engine_refusal_is_a_plain_409(live, synced, monkeypatch):
    monkeypatch.setattr(groups, "not_me", lambda gid: {"ok": False, "error": "refused: mixed group"})
    st, _h, body = live.post("not-me", {"group_id": template_group()["group_id"]})
    assert st == 409 and json.loads(body) == {"ok": False, "error": "refused: mixed group"}


def test_the_page_script_handles_tokens_tabs_and_stale_replies():
    js = (resources.files("mannerism") / "webui" / "app.js").read_text(encoding="utf-8")
    assert "sessionStorage" in js and js.count("sessionStorage.") == 3
    for call in ("getItem", "setItem", "removeItem"):  # every storage access sits in a try
        i = js.index(f"sessionStorage.{call}")
        assert "try {" in js[max(0, i - 80):i]
    assert "rv.seq" in js and "mine !== rv.seq" in js  # older review replies are ignored
    assert "rv.busy = new Set()" in js or "busy: new Set()" in js  # busy is per group
    assert "againReview" in js and "expected_units" in js
    assert "g.mixed" in js and "mannerism review rescan" in js and 'error === "used"' in js
    assert "This page session has ended" in js and "no token" not in js
    assert "scrollIntoView" in js
