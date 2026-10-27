import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from conftest import ingest, unit

from mannerism import api, bank, blind, evaluate, store


def test_wilson_values():
    lo, hi = evaluate.wilson(7, 10)
    assert (round(lo, 4), round(hi, 4)) == (0.3968, 0.8922)
    assert evaluate.wilson(0, 0) == (0.0, 1.0)
    lo, hi = evaluate.wilson(10, 10)
    assert round(lo, 3) == 0.722 and hi == 1.0
    lo, hi = evaluate.wilson(0, 10)
    assert lo == 0.0 and round(hi, 3) == 0.278
    lo, hi = evaluate.wilson(50, 100)
    assert 0.40 < lo < 0.41 and 0.59 < hi < 0.60


def test_burrows_delta_orders_similarity():
    a = ["the cat sat on the mat and it was happy with the day", "we went to the park and we had a lot of fun there"] * 10
    same = ["the dog sat on the rug and it was glad with the night", "we went to the shop and we had a lot of joy there"] * 10
    other = [("Whereas the party of the first part shall indemnify the party of the second part, notwithstanding "
              "any provision herein to the contrary, upon written notice")] * 20
    assert evaluate.burrows_delta(a, same) < evaluate.burrows_delta(a, other)
    assert evaluate.burrows_delta(a, a) == 0


def run_blind(fakes, messages=None):
    api.setup()
    gp = api.work_next("blind")["packet"]
    assert gp["kind"] == "blind_gist"
    gists = {"gists": [{"id": m["id"], "gist": f"They talk about plan {m['id']} with someone"} for m in gp["payload"]["messages"]]}
    res = api.work_submit("blind_gist", gp["packet_id"], gists)["result"]
    wp = res["next_packet"]
    return gp, wp


def test_blind_two_step_flow_and_scoring(fakes):
    gp, wp = run_blind(fakes)
    assert len(gp["payload"]["messages"]) == 10
    real = [m["text"] for m in gp["payload"]["messages"]]
    # step 2 never contains the real text, and the held-out units are not used as examples
    body = json.dumps(wp)
    assert wp["kind"] == "blind_write" and not any(t in body for t in real)
    run = blind.load_run(store.connect(), gp["payload"]["run_id"])
    held = {i["unit_id"] for i in run["items"]}
    con = store.connect()
    for reg in ("close", "work", "agent"):
        assert not held & {e.unit_id for e in bank.retrieve(con, reg, k=8)}
    msgs = {"messages": [{"id": i["id"], "text": f"generated message for {i['id']} <script>alert(1)</script> http://x.example/a"}
                         for i in wp["payload"]["items"]]}
    out = api.work_submit("blind_write", wp["packet_id"], msgs)["result"]
    with open(out["page"]) as fh:
        html = fh.read()
    assert not re.search(r"https?://", html) and "<script>alert" not in html and "src=" not in html and "href=" not in html
    assert html.count("</script>") == 1 and "textContent" in html
    assert api.work_next("blind")["packet"]["status"] == "page_ready"
    run = blind.load_run(store.connect(), gp["payload"]["run_id"])
    # the user spots 7 of 10 and can't tell on one
    answers = ""
    for n, it in enumerate(run["items"]):
        wrong = "A" if it["real_side"] == "B" else "B"
        answers += it["real_side"] if n < 7 else ("=" if n == 7 else wrong)
    bad = api.blind_score("bt00000000." + answers)
    assert not bad["ok"]
    score = api.blind_score(f"{run['run_id']}.{answers}")["result"]
    assert score["correct"] == 7 and score["answered"] == 10 and score["cant_tell"] == 1
    assert score["ci95"] == [0.397, 0.892] and score["fooled_rate"] == 0.3
    assert store.get_meta(store.connect(), "blind_exclude") is None  # the test is over: units are released


def test_blind_needs_enough_text(home):
    with pytest.raises(ValueError, match="not enough"):
        blind.blind_packet(store.connect(), 10)
    assert not api.work_next("blind")["ok"]


def test_page_is_self_contained_even_with_hostile_text():
    run = {"run_id": "bt12345678", "items": [
        {"register": "close", "gist": "g </script><b>x</b>", "real": "see https://a.example/x", "fake": "fake & <img src=x onerror=1>",
         "real_side": "A"}] * 3}
    html = blind.page_html(run)
    assert not re.search(r"https?://", html)
    assert "</script><b>" not in html and "<img" not in html and "\\u003c" in html


def test_page_substitution_does_not_corrupt_text_that_looks_like_a_placeholder():
    """P2-17: the template placeholders were substituted in an order where the user's own (untrusted)
    text went in first, so a later .replace() call for __N__/__RUN__ could match that same literal
    substring if it happened to occur inside the text and corrupt the embedded JSON. Order the text last
    instead, and prove a literal "__N__" in the user's own writing survives intact."""
    run = {"run_id": "bt12345678", "items": [
        {"register": "close", "gist": "fine", "real": "my password is __N__ and the var is __RUN__ today",
         "fake": "a made up alternative line of about the same length", "real_side": "A"}] * 2}
    html = blind.page_html(run)
    assert "__N__ and the var is __RUN__" in html  # the user's own text, untouched
    assert "2 pairs" in html  # the REAL placeholder substitutions still happened correctly elsewhere
    assert 'RUN="bt12345678"' in html


def _twin_corpus():
    """Every text exists twice, as two units in different threads ("ok sounds good" sent twice). A text held
    out of the blind test therefore still has a twin under a different unit id."""
    texts = {"close": [f"ok that works for me, see you {w} then, bring the {t} along" for w, t in
                       zip(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "tonight",
                            "tomorrow", "later", "soon", "early"),
                           ("charger", "speaker", "snacks", "maps", "tickets", "umbrella", "camera", "book", "keys",
                            "cards", "bag", "coat"))],
             "work": [f"I will review item {i} before standup and then post the summary in the channel"
                      for i in range(8)]}
    units = []
    for reg, ts_ in texts.items():
        for i, t in enumerate(ts_):
            for twin in (0, 1):
                units.append(unit(t, register=reg, ref=f"{reg}{i}-{twin}", days=10 + i, thread_id=f"{reg}{i}-{twin}"))
    return units


@pytest.mark.parametrize("offset", range(0, 60, 2))
def test_blind_write_packet_excludes_duplicate_text_of_held_out_items(home, offset):
    """Regression: held-out units were excluded from the write packet's examples by unit id only, so a unit with
    the same text (a different unit id) could carry a real message into the packet. This forces that collision
    on every date, because every text in the bank has a twin."""
    con = store.connect()
    ingest(con, _twin_corpus())
    now = datetime.now(UTC) + timedelta(days=offset)
    gp = blind.blind_packet(con, 6, now=now)
    run_id = gp["payload"]["run_id"]
    real = [m["text"] for m in gp["payload"]["messages"]]
    assert len(real) == 6 and len(set(real)) == 6
    gists = {"gists": [{"id": m["id"], "gist": f"They talk about plan {m['id']} with someone"}
                       for m in gp["payload"]["messages"]]}
    wp = blind.submit_gists(con, run_id, gists)
    body = json.dumps(wp)
    assert wp["kind"] == "blind_write" and not any(t in body for t in real)
    held_texts = {bank._norm(t) for t in real}
    # the same exclusion applies to every other retrieval while the test is pending
    for reg in ("close", "work"):
        assert not held_texts & {bank._norm(e.text) for e in bank.retrieve(con, reg, k=8)}
    # picking stays deterministic for a given run date
    again = blind.pick_items(con, 6, now)
    assert [i["unit_id"] for i in again] == [i["unit_id"] for i in blind.load_run(con, run_id)["items"]]


def test_blind_card_lines_quoting_a_held_out_message_are_dropped():
    held = {bank._norm("ok that works for me, see you monday then")}
    card = "Short, lowercase.\nExample: \"OK that works for me,  see you Monday then\"\nNo greetings."
    assert blind._drop_held_lines(card, held) == "Short, lowercase.\nNo greetings."
