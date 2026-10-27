"""The local voice page: server security, JSON shape on a synthetic store, empty states, offline assets."""
from __future__ import annotations

import http.client
import json
import re
import sqlite3
import threading
import time
from datetime import UTC, datetime, timedelta
from importlib import resources

import pytest
import uifixture
from conftest import ingest, unit

from mannerism import api, capture, cli, dashboard, store, web
from mannerism import lint as lint_mod
from mannerism.core import mask_pii


class Live:
    def __init__(self, server: web.VoiceServer):
        self.server = server
        self.port = server.server_port
        self.token = server.token

    def get(self, path="/", headers=None, method="GET"):
        con = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        hdrs = {"Host": f"127.0.0.1:{self.port}", **(headers or {})}
        con.request(method, path, headers=hdrs)
        r = con.getresponse()
        body = r.read()
        out = (r.status, dict((k.lower(), v) for k, v in r.getheaders()), body)
        con.close()
        return out

    def authed(self, path="/", **kw):
        return self.get(path, headers={web.HEADER: self.token, **kw.pop("headers", {})}, **kw)


@pytest.fixture
def live():
    srv = web.VoiceServer(0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield Live(srv)
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def _fresh_cache():
    dashboard._CACHE.clear()
    yield
    dashboard._CACHE.clear()


@pytest.fixture
def populated(home):
    uifixture.populate()


# --- security -------------------------------------------------------------------------------------------------------

def test_binds_loopback_only_on_a_random_port(live):
    assert live.server.server_address[0] == "127.0.0.1"
    assert live.port > 0 and live.server.url.startswith(f"http://127.0.0.1:{live.port}/?t=")
    assert not hasattr(web.VoiceServer(0), "host") and web.BIND == "127.0.0.1"


@pytest.mark.parametrize("host", ["evil.example", "evil.example:%d", "127.0.0.1", "localhost", "localhost.evil.com:%d",
                                  "127.0.0.1.evil.com:%d", "[::1]:%d", ""])
def test_foreign_or_portless_host_header_is_refused(live, host):
    host = host % live.port if "%d" in host else host
    for path in ("/", "/api/overview", "/static/app.js"):
        status, _h, body = live.get(path, headers={"Host": host, web.HEADER: live.token})
        assert status == 403 and b"forbidden" in body


def test_localhost_with_the_servers_port_is_accepted(live, home):
    status, *_ = live.get("/api/overview", headers={"Host": f"localhost:{live.port}", web.HEADER: live.token})
    assert status == 200


def test_missing_or_wrong_token_is_refused_everywhere(live, home):
    for path in ("/", "/static/app.css", "/static/app.js", "/api/overview", "/nothing"):
        assert live.get(path)[0] == 403
        assert live.get(path, headers={web.HEADER: "wrong"})[0] == 403
        assert live.get(path, headers={"Cookie": f"{live.server.cookie_name}=wrong"})[0] == 403
    assert live.get("/?t=wrong")[0] == 403
    assert live.get("/", headers={web.HEADER: live.token[:-1]})[0] == 403


def test_url_token_becomes_a_strict_http_only_cookie_and_is_stripped(live, home):
    status, headers, _ = live.get(f"/?t={live.token}")
    assert status == 302 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"{live.server.cookie_name}={live.token}")
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    status, headers, body = live.get("/", headers={"Cookie": f"{live.server.cookie_name}={live.token}"})
    assert status == 200 and b"<title>" in body and headers["content-type"].startswith("text/html")


def test_only_get_is_served(live, home):
    for method in ("POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"):
        status, headers, body = live.get("/api/overview", headers={web.HEADER: live.token}, method=method)
        assert status == 405 and headers["allow"] == "GET"
    assert live.authed("/api/overview")[0] == 200


def test_foreign_origin_is_refused_and_no_cors_headers_are_ever_sent(live, home):
    assert live.authed("/api/overview", headers={"Origin": "https://evil.example"})[0] == 403
    assert live.authed("/api/overview", headers={"Origin": f"http://localhost:{live.port}"})[0] == 200
    for resp in (live.authed("/api/overview"), live.authed("/"), live.get("/"), live.authed("/x", method="POST")):
        assert not any(k.startswith("access-control-") for k in resp[1])
        assert "default-src 'none'" in resp[1]["content-security-policy"]
        assert resp[1]["cache-control"] == "no-store" and resp[1]["referrer-policy"] == "no-referrer"


def test_requests_are_not_logged(live, home, capsys):
    live.get(f"/?t={live.token}")
    live.authed("/api/overview")
    out = capsys.readouterr()
    assert live.token not in out.err + out.out and out.err == ""


def test_a_failing_provider_returns_a_generic_error(home):
    def boom():
        raise RuntimeError("secret stored text")
    srv = web.VoiceServer(0, provider=boom)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        status, _h, body = Live(srv).authed("/api/overview")
        assert status == 500 and b"secret" not in body
    finally:
        srv.shutdown()
        srv.server_close()


# --- assets are offline ---------------------------------------------------------------------------------------------

def test_assets_ship_in_the_package_and_have_no_external_urls():
    pkg = resources.files("mannerism") / "webui"
    names = ("index.html", "app.css", "app.js")
    for name in names:
        text = (pkg / name).read_text(encoding="utf-8")
        urls = [u for u in re.findall(r"https?://[^\s'\"<>)]+", text) if "www.w3.org/2000/svg" not in u]
        assert not urls, (name, urls)
        assert "@import" not in text and "//cdn" not in text
    html = (pkg / "index.html").read_text(encoding="utf-8")
    assert re.findall(r'(?:src|href)="([^"]+)"', html) == [
        html.split('rel="icon" href="')[1].split('"')[0], "/static/app.css", "/static/app.js"]
    assert "<script>" not in html and "<style" not in html and " style=" not in html
    js = (pkg / "app.js").read_text(encoding="utf-8")
    assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\(|new Function|XMLHttpRequest|WebSocket",
                         js)
    assert re.findall(r"fetch\(\s*([^,)]+)", js) == ['"/api/csrf"', '"/api/review/" + kind', '"/api/review"', '"/api/overview"']
    assert js.count('method: "POST"') == 1  # the review page's two actions share one call; nothing else writes


def test_served_pages_have_no_external_references(live, populated):
    for path in ("/", "/static/app.css", "/static/app.js", "/api/overview"):
        status, _h, body = live.authed(path)
        assert status == 200
        text = body.decode()
        assert not [u for u in re.findall(r"https?://[^\s'\"<>)]+", text)
                    if "www.w3.org" not in u and "example.com" not in u], path


# --- data shape -----------------------------------------------------------------------------------------------------

def test_overview_json_shape_on_a_populated_store(live, populated):
    status, headers, body = live.authed("/api/overview")
    assert status == 200 and headers["content-type"].startswith("application/json")
    d = json.loads(body)
    assert d["ok"] and d["stage"] == "ready" and d["next_step"] is None and d["total_units"] > 400
    kinds = {k["label"]: k["units"] for k in d["sources"]["kinds"]}
    assert {"agent prompts", "commits", "Slack", "posts", "mail import", "notes", "chats"} <= set(kinds)
    assert all(k["units"] > 0 for k in d["sources"]["kinds"])
    assert d["sources"]["last_sync_age_s"] is not None and len(d["sources"]["sources"]) == 10
    regs = {c["register"]: c for c in d["cards"]["cards"]}
    assert set(regs) == {"close", "work", "formal", "agent", "public"}
    assert [w["register"] for w in d["cards"]["waiting"]] == ["group"]
    assert regs["agent"]["status"] == "upgrade" and regs["work"]["status"] == "fresh"
    assert regs["close"]["card_source"] == "stats" and regs["work"]["card_source"] == "agent"
    for c in regs.values():
        assert 3 <= len(c["habits"]) <= 5, c["register"]
        for h in c["habits"]:
            assert 0 < h["count"] < h["of"] and 0.11 < h["share"] < 0.96
            assert h["example"] and h["example"]["text"] and re.fullmatch(r"\d{4}-\d{2}", h["example"]["month"])
    learned = d["learned"]
    assert len(learned["rules"]) == 4 and learned["evidence_edits"] > 4
    newest = learned["rules"][:3]
    assert newest[0]["learned"] > newest[1]["learned"] > newest[2]["learned"]
    for r in newest:
        ops = r["pair"]["ops"]
        assert {o["t"] for o in ops} <= {"eq", "del", "ins"} and any(o["t"] != "eq" for o in ops)
    assert learned["rules"][3]["pair"] is None
    ins = d["insights"]
    t = ins["edits_per_draft"]
    assert t["ok"] and t["bucket"] == "week" and len(t["series"]) >= 8 and t["latest"] < t["first"]
    assert ins["written"]["ok"] and 0 < ins["written"]["share"] < 1
    assert ins["tells"]["ok"] and ins["tells"]["top"] and ins["tells"]["with_tells"] <= ins["tells"]["drafts"]
    assert ins["words"]["ok"] and {r["register"] for r in ins["words"]["registers"]} >= {"close", "work", "agent"}
    assert ins["months"]["ok"] and len(ins["months"]["series"]) == 12
    assert all(tile["value"] is not None for tile in ins["tiles"])
    json.dumps(d)


def test_stored_text_is_returned_as_data_and_stays_masked(live, home):
    uifixture.populate()
    con = store.connect()
    text = mask_pii("<script>alert(1)</script> mail me at jo@example.org please")  # adapters mask before storing
    ingest(con, [unit(text, "agent", ref=f"x{i}", days=i + 1, source="codex") for i in range(35)])
    con.close()
    d = json.loads(live.authed("/api/overview")[2])
    text = json.dumps(d)
    assert "jo@example.org" not in text
    assert any("<script>" in h["example"]["text"] for c in d["cards"]["cards"] for h in c["habits"] if h["example"])


def test_unavailable_figures_say_so_instead_of_inventing_numbers(home):
    con = store.connect()
    ingest(con, [unit(f"fix the thing number {i} for me", "agent", ref=f"a{i}", days=i + 1, source="codex")
                 for i in range(40)])
    capture.record_feedback("Hi Sam, I hope you are well. Please fix it.", "sam please fix it", "work", con=con)
    con.close()
    d = dashboard.overview()
    ins = d["insights"]
    assert ins["edits_per_draft"]["ok"] is False and "6 drafts" in ins["edits_per_draft"]["need"]
    assert ins["written"]["ok"] is False and ins["tells"]["ok"] is False
    assert ins["months"]["ok"] is True
    tiles = {t["key"]: t for t in ins["tiles"]}
    assert tiles["written"]["value"] is None and tiles["edits"]["value"] is None
    assert not tiles["written"]["available"] and not tiles["edits"]["available"]
    assert tiles["rules"]["value"] == 0 and tiles["rules"]["available"]  # a real zero is shown as 0
    assert d["learned"]["rules"] == [] and len(d["learned"]["recent_pairs"]) == 1


# --- empty and early states ----------------------------------------------------------------------------------------

def test_new_install_shows_one_next_step(live, home):
    d = json.loads(live.authed("/api/overview")[2])
    assert d["ok"] and d["stage"] == "empty" and d["total_units"] == 0
    assert d["next_step"].count("\n") == 0 and "mannerism init" in d["next_step"]
    assert d["cards"] == {"cards": [], "waiting": []} and d["sources"]["kinds"] == []
    assert all(t["value"] in (None, 0) for t in d["insights"]["tiles"])
    status, _h, body = live.authed("/")
    assert status == 200 and b'id="main"' in body


def test_few_texts_explain_what_is_missing(home):
    con = store.connect()
    ingest(con, [unit(f"note {i} about the plan for people", "work", ref=f"w{i}", days=i + 1) for i in range(5)])
    con.close()
    d = dashboard.overview()
    assert d["stage"] == "early" and "no register has 30 messages yet" in d["next_step"]
    assert d["cards"]["cards"] == [] and d["cards"]["waiting"][0]["register"] == "work"


# --- forget is respected ---------------------------------------------------------------------------------------------

def test_forget_removes_a_source_from_the_page(populated):
    before = dashboard.overview()
    assert any(s["name"] == "git" and s["units"] for s in before["sources"]["sources"])
    assert api.forget(source="git")["ok"]
    after = dashboard.overview()
    git = next(s for s in after["sources"]["sources"] if s["name"] == "git")
    assert git["units"] == 0 and git["enabled"] is False
    assert "commits" not in {k["label"] for k in after["sources"]["kinds"]}
    assert api.forget(all=True)["ok"]
    gone = dashboard.overview()
    assert gone["stage"] == "empty" and gone["learned"]["rules"] == [] and gone["cards"]["cards"] == []
    assert gone["insights"]["edits_per_draft"]["ok"] is False


# --- helpers ---------------------------------------------------------------------------------------------------------

def test_diff_ops_and_hunks():
    ops = dashboard.diff_ops("Hi Sam, I hope you are well. please fix it.", "sam please fix it")
    assert ops == [{"t": "del", "s": "Hi Sam, I hope you are well."}, {"t": "ins", "s": "sam"},
                   {"t": "eq", "s": "please fix"}, {"t": "del", "s": "it."}, {"t": "ins", "s": "it"}]
    assert dashboard.edit_hunks("a b c", "a b c") == 0
    assert dashboard.edit_hunks("a b c", "a x c") == 1
    assert dashboard.edit_hunks("a b c d", "x b c y") == 2


def test_habits_never_report_a_blanket_rate(home):
    con = store.connect()
    ingest(con, [unit(f"Ship the thing {i}.", "work", ref=f"w{i}", days=i + 1) for i in range(40)])
    rows = con.execute("SELECT source, ts, text, features_json FROM bank WHERE register='work'").fetchall()
    con.close()
    assert dashboard.habits_for(rows, None) == []  # every message capitalised and ending in a full stop: no 100% habits


# --- CLI ---------------------------------------------------------------------------------------------------------------

def test_ui_command_passes_its_options(monkeypatch):
    seen = {}
    monkeypatch.setattr(web, "run", lambda port, open_browser: seen.update(port=port, open=open_browser) or 0)
    assert cli.main(["ui", "--no-open", "--port", "19876"]) == 0
    assert seen == {"port": 19876, "open": False}
    assert cli.main(["ui"]) == 0 and seen == {"port": 0, "open": True}


def test_ui_command_reports_a_busy_port(monkeypatch, capsys):
    def busy(port, open_browser):
        raise OSError(98, "Address already in use")
    monkeypatch.setattr(web, "run", busy)
    assert cli.main(["ui", "--port", "19877"]) != 0
    assert "cannot start the page" in capsys.readouterr().err


def test_run_prints_the_token_address_but_opens_a_nonce_address(home, capsys, monkeypatch):
    srv = web.VoiceServer(0)
    monkeypatch.setattr(srv, "serve_forever", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    opened = []
    monkeypatch.setattr(web, "open_in_browser", lambda url: opened.append(url) or True)
    assert web.run(server=srv, open_browser=True) == 0
    assert srv.url in capsys.readouterr().out  # the terminal keeps the reusable token address
    assert len(opened) == 1 and "?k=" in opened[0] and srv.token not in opened[0]
    assert web.can_open_browser({"DISPLAY": ":0"}, "linux") and not web.can_open_browser({}, "linux")


def test_browser_argv_holds_only_the_nonce_url(home, monkeypatch):
    monkeypatch.setattr(web, "can_open_browser", lambda *a, **k: True)
    argv = []
    monkeypatch.setattr(web.webbrowser, "open", lambda target: argv.append(target) or True)
    srv = web.VoiceServer(0)
    monkeypatch.setattr(srv, "serve_forever", lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert web.run(server=srv, open_browser=True) == 0
    assert len(argv) == 1 and argv[0].startswith(f"http://127.0.0.1:{srv.server_port}/?k=")
    assert srv.token not in argv[0] and "file://" not in argv[0]


@pytest.mark.parametrize("failure", ["raises", "false", "no_display"])
def test_a_failed_browser_open_never_stops_the_server(home, monkeypatch, capsys, failure):
    monkeypatch.setattr(web, "can_open_browser", lambda *a, **k: failure != "no_display")

    def fake(target):
        if failure == "raises":
            raise RuntimeError("no browser")
        return False
    monkeypatch.setattr(web.webbrowser, "open", fake)
    srv = web.VoiceServer(0)
    served = []
    monkeypatch.setattr(srv, "serve_forever", lambda: served.append(1))
    assert web.run(server=srv, open_browser=True) == 0
    out = capsys.readouterr().out
    assert served == [1] and srv.url in out and "Could not open a browser" in out


def test_nonce_is_single_use_sets_the_cookie_and_redirects_clean(live, home):
    nonce = live.server.issue_nonce()
    status, headers, _ = live.get(f"/?k={nonce}")
    assert status == 302 and headers["location"] == "/"
    cookie = headers["set-cookie"]
    assert cookie.startswith(f"{live.server.cookie_name}={live.token}") and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert live.get(f"/?k={nonce}")[0] == 403  # spent
    assert live.get("/", headers={"Cookie": f"{live.server.cookie_name}={live.token}"})[0] == 200


def test_wrong_nonce_is_refused_and_does_not_burn_a_live_one(live, home):
    nonce = live.server.issue_nonce()
    for bad in ("nope", nonce[:-1], nonce + "x", "", live.token):
        assert live.get(f"/?k={bad}")[0] == 403
    assert live.get("/?k=")[0] == 403
    assert live.get(f"/?k={nonce}")[0] == 302


def test_nonce_expires(live, home):
    assert web.NONCE_TTL_S == 60
    nonce = live.server.issue_nonce(ttl=0.05)
    time.sleep(0.15)
    assert live.get(f"/?k={nonce}")[0] == 403
    fresh = live.server.issue_nonce(ttl=60)
    assert live.get(f"/?k={fresh}")[0] == 302
    assert live.server._nonces == {}  # spent and expired nonces are not kept


def test_nonce_needs_the_right_host_and_only_opens_the_front_page(live, home):
    nonce = live.server.issue_nonce()
    assert live.get(f"/?k={nonce}", headers={"Host": "evil.example"})[0] == 403
    assert live.get(f"/api/overview?k={nonce}")[0] == 403 and live.get(f"/static/app.js?k={nonce}")[0] == 403
    assert live.get(f"/?k={nonce}")[0] == 302  # still unspent after the refused tries


@pytest.mark.parametrize("bad", ["70000", "-1", "abc", "65536"])
def test_ui_rejects_a_port_outside_the_range(bad, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["ui", "--port", bad])
    assert e.value.code == 2 and "port" in capsys.readouterr().err
    assert cli.build_parser().parse_args(["ui", "--port", "65535"]).port == 65535


# --- read-only, honest figures, bounded cost -------------------------------------------------------------------------

def test_a_fresh_home_stays_absent(home):
    assert not home.exists()
    d = dashboard.overview()
    assert d["ok"] and d["stage"] == "empty" and d["insights"]["months"]["ok"] is False
    assert not home.exists(), "overview() must not create the data directory or corpus.db"
    srv = web.VoiceServer(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert Live(srv).authed("/api/overview")[0] == 200
    finally:
        srv.shutdown()
        srv.server_close()
    assert not home.exists()


def test_an_existing_store_is_not_touched(populated, home):
    db = home / "corpus.db"
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    cfg_before = (home / "config.toml").stat().st_mtime_ns
    listing = sorted(p.name for p in home.iterdir())
    for _ in range(2):
        assert dashboard.overview()["ok"]
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before
    assert (home / "config.toml").stat().st_mtime_ns == cfg_before
    assert sorted(p.name for p in home.iterdir()) == listing or {"corpus.db-shm", "corpus.db-wal"} >= set(
        sorted(p.name for p in home.iterdir())) ^ set(listing)


def test_the_page_connection_cannot_write(populated):
    con = dashboard._open_readonly()
    try:
        with pytest.raises(Exception, match="readonly|read-only|attempt to write"):
            con.execute("DELETE FROM units")
    finally:
        con.close()


def test_an_old_schema_is_reported_not_migrated(home):
    store.connect().close()
    con = store.connect()
    con.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    con.close()
    d = dashboard.overview()
    assert d["ok"] is False and "upgrade" in d["error"]
    store.connect().close()


def test_a_store_one_schema_behind_still_opens(populated):
    con = store.connect()
    con.execute("UPDATE meta SET value=? WHERE key='schema_version'", (str(dashboard.PAGE_MIN_SCHEMA),))
    con.close()
    assert dashboard.overview()["ok"] is True
    con = sqlite3.connect(store.db_path())
    assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == str(dashboard.PAGE_MIN_SCHEMA)
    con.close()


def test_real_zeros_are_values_not_missing_data(home):
    con = store.connect()
    ingest(con, [unit(f"fix the thing number {i} for me", "agent", ref=f"a{i}", days=i + 1, source="codex")
                 for i in range(40)])
    plain = ["pushed the rename, nothing else touched today", "staging is green, going live after lunch",
             "the vendor contract needs one more pass tonight", "dashboard rollout moved to thursday, no blockers",
             "release checklist updated, two items left for you", "invoice batch checked, sending fixes shortly",
             "retry backoff merged, please rerun the suite", "export script shipped, runs in nine seconds now"]
    for i, text in enumerate(plain):  # every draft was sent exactly as written, none has a tell
        capture.add_edit(con, "work", text, text, "feedback", thread_key=f"t{i}",
                         now=datetime.now(UTC) - timedelta(days=20 - 2 * i))
        capture.log_draft(text, "work", con=con)
    con.close()
    tiles = {t["key"]: t for t in dashboard.overview()["insights"]["tiles"]}
    assert tiles["edits"]["value"] == 0 and tiles["edits"]["available"]
    assert tiles["tells"]["value"] == 0 and tiles["tells"]["available"]
    assert tiles["written"]["value"] == 100 and tiles["written"]["available"]
    js = (resources.files("mannerism") / "webui" / "app.js").read_text(encoding="utf-8")
    assert "const ok = t.available;" in js and "!== 0" not in js  # the page trusts the server's flag, not the number


def test_edits_per_draft_uses_the_newest_pairs(home, monkeypatch):
    monkeypatch.setattr(dashboard, "EDIT_SCAN", 24)
    con = store.connect()
    now = datetime.now(UTC)
    for i in range(70):  # the oldest 46 are heavily edited, the newest 24 were sent as written
        text = f"alpha beta gamma delta epsilon zeta eta theta iota kappa n{i}"
        draft = text if i >= 46 else "Hi there, I hope you are well. " + text + " Best regards, and let me know."
        capture.add_edit(con, "work", draft, text, "feedback", thread_key=f"t{i}", now=now - timedelta(days=70 - i))
    con.close()
    t = dashboard.edits_trend(store.connect())
    assert t["ok"] and t["pairs"] == 24 and t["latest"] == 0 and t["first"] == 0


def test_the_overview_is_computed_once_per_change(populated, monkeypatch):
    calls = []
    real = lint_mod.lint
    monkeypatch.setattr(lint_mod, "lint", lambda *a, **k: calls.append(1) or real(*a, **k))
    first = dashboard.overview()
    n = len(calls)
    assert n >= 5
    again = dashboard.overview()
    assert len(calls) == n and again["insights"] == first["insights"]
    assert again["generated_at"] >= first["generated_at"]
    capture.log_draft("Certainly! I hope this helps with the vendor contract, a brand new draft today", "work")
    dashboard.overview()
    assert len(calls) > n  # new data invalidates the remembered result


def test_concurrent_loads_share_one_computation(home, monkeypatch):
    running, peak, runs = [0], [0], []
    lock = threading.Lock()

    def slow():
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
            runs.append(1)
        time.sleep(0.3)
        with lock:
            running[0] -= 1
        return {"ok": True, "sources": {"last_sync": None}, "generated_at": "x"}

    monkeypatch.setattr(dashboard, "_compute", slow)
    threads = [threading.Thread(target=dashboard.overview) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(runs) == 1 and peak[0] == 1


def test_drafts_over_the_cap_are_skipped_and_counted_not_cut(home, monkeypatch):
    con = store.connect()
    ingest(con, [unit(f"fix the thing number {i} for me", "agent", ref=f"a{i}", days=i + 1) for i in range(40)])
    topics = ["invoices", "staging", "contracts", "dashboards", "checklists", "backoff"]
    for i, t in enumerate(topics):  # oldest first, more than 48 hours apart
        capture.log_draft(f"I hope this helps with {t}. " + f"{t} filler words go here " * 10, "work", con=con,
                          now=datetime.now(UTC) - timedelta(days=30 - 3 * i))
    capture.log_draft("Certainly! delve into this. " + "a very long draft padded out with words " * 120, "work", con=con)
    con.close()
    seen = []
    real = lint_mod.lint
    monkeypatch.setattr(lint_mod, "lint", lambda text, *a, **k: seen.append(len(text)) or real(text, *a, **k))
    tells = dashboard.overview()["insights"]["tells"]
    assert tells["ok"] and tells["drafts"] == 6 and tells["skipped_long"] == 1
    assert seen and max(seen) <= dashboard.TELL_CHARS and "delve" not in str(tells["top"])


def test_over_cap_edit_pairs_are_left_out_not_truncated(home, monkeypatch):
    monkeypatch.setattr(dashboard, "EDIT_CHARS", 200)
    con = store.connect()
    now = datetime.now(UTC)
    for i in range(8):
        text = f"alpha beta gamma delta epsilon zeta eta theta n{i}"
        capture.add_edit(con, "work", text, text, "feedback", thread_key=f"t{i}", now=now - timedelta(days=9 - i))
    long = " ".join(f"word{j}" for j in range(80))  # over the 200-character cap; identical draft and final
    for i in range(3):
        capture.add_edit(con, "work", f"{long} {i}", f"{long} {i}", "feedback", thread_key=f"l{i}", now=now)
    t = dashboard.edits_trend(con)
    con.close()
    assert t["ok"] and t["pairs"] == 8 and t["latest"] == 0 and t["first"] == 0  # no false hunks from a cut


def test_a_near_total_share_is_never_shown_as_100(home):
    con = store.connect()
    rows = [(store.ts_iso(datetime.now(UTC) - timedelta(minutes=i)), "work", f"d{i}", f"f{i}",
             "edited" if i == 0 else "accepted", "feedback", f"sha{i}") for i in range(2500)]
    con.executemany("INSERT INTO edits(ts, register, draft, final, status, origin, final_sha) VALUES (?,?,?,?,?,?,?)",
                    rows)
    sw = dashboard.written_share(con)
    con.close()
    assert sw["as_written"] == 2499 and sw["share"] == 2499 / 2500 < 1
    assert dashboard._pct(sw["share"]) == 99
    js = (resources.files("mannerism") / "webui" / "app.js").read_text(encoding="utf-8")
    assert "Math.min(99" in js


def test_a_same_size_change_that_only_reached_the_wal_still_refreshes(populated, home):
    first = dashboard.overview()
    text = first["learned"]["rules"][0]["text"]
    new = ("X" + text[1:]) if not text.startswith("X") else ("Y" + text[1:])
    writer = store.connect()  # stays open: the change lives in corpus.db-wal, not in corpus.db
    db = home / "corpus.db"
    before = (db.stat().st_mtime_ns, db.stat().st_size)
    sig = dashboard._signature()
    writer.execute("UPDATE rules SET text=? WHERE text=?", (new, text))
    assert (db.stat().st_mtime_ns, db.stat().st_size) == before and len(new) == len(text)
    assert dashboard._signature() != sig
    assert dashboard.overview()["learned"]["rules"][0]["text"] == new
    writer.close()


def test_labels_and_blanket_percentages(populated):
    assert [dashboard._pct(x) for x in (0, 0.004, 0.5, 0.996, 1)] == [0, 1, 50, 99, 100]
    d = dashboard.overview()
    tiles = {t["key"]: t for t in d["insights"]["tiles"]}
    assert "90%" in tiles["written"]["label"] and "matched drafts" in tiles["written"]["sub"]
    assert d["insights"]["written"]["drafts"] > 0
    js = (resources.files("mannerism") / "webui" / "app.js").read_text(encoding="utf-8")
    assert "uncalibrated" in js and "matched" in js and "getUTCDate" in js


def test_tells_panel_names_the_registers_with_too_little_data(home):
    con = store.connect()
    ingest(con, [unit(f"fix the thing number {i} for me", "agent", ref=f"a{i}", days=i + 1) for i in range(40)])
    for i, t in enumerate(["invoices", "staging", "contracts", "dashboards", "checklists"]):
        capture.log_draft(f"Certainly! I hope this helps with the {t} and everything around them today", "formal",
                          con=con, now=datetime.now(UTC) - timedelta(days=30 - 3 * i))
    con.close()
    tells = dashboard.overview()["insights"]["tells"]
    assert tells["ok"] and tells["uncalibrated"] == ["formal"] and tells["with_tells"] == 5


def test_rule_edit_counts_ignore_forgotten_edits(populated):
    before = dashboard.overview()
    rule = next(r for r in before["learned"]["rules"] if r["pair"])
    con = store.connect()
    row = con.execute("SELECT edit_ids_json FROM rules WHERE rule_id=?", (rule["rule_id"],)).fetchone()
    gone = json.loads(row[0])[:3]
    con.execute(f"DELETE FROM edits WHERE edit_id IN ({','.join('?' * len(gone))})", gone)
    con.close()
    after = dashboard.overview()
    again = next(r for r in after["learned"]["rules"] if r["rule_id"] == rule["rule_id"])
    assert again["edits"] == rule["edits"] - 3 and after["learned"]["evidence_edits"] < before["learned"]["evidence_edits"]
