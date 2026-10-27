"""R3 regressions, adapted from the reviewer's repro tests (test_repro.py A-G, test_repro2.py H-I).

Those repro files demonstrated nine bugs by asserting the BUGGY behavior (see each test's docstring for
the original assertion). Every test here asserts the CORRECTED behavior instead, using only synthetic,
fictional data, isolated from the real user's machine by the autouse `_isolated_env` fixture in
tests/conftest.py (every test in this file inherits that isolation automatically)."""

import json
import mailbox
import os
import subprocess
from datetime import UTC, datetime, timedelta

from mannerism.core import Location, Me, Unit


def test_A_whatsapp_autodetect_flags_its_guess_as_a_guess(_isolated_env, home, tmp_path):
    """Original bug: the self-detection intersection picked Bob (not Jo) as "self" with no way to tell,
    so Bob's private "quit my job" message was stored as the USER'S OWN text, silently, as settled fact.

    This exact two-file case is genuinely unsolvable without more evidence (Jo lurks in the family chat
    and never sends a message there, so nothing distinguishes Bob from the real self from these two files
    alone) -- P0-1's fix is not to magically solve it, but to never again treat the guess as settled fact:
    it is used (mannerism stays zero-question), but every unit from it carries meta.self_detected=true so
    `status` can surface it for confirmation. The fuller case where genuine corroborating evidence lets
    the fix pick correctly despite a lurker is tests/adapters/test_whatsapp.py's lurker-group test."""
    from mannerism.adapters.whatsapp import WhatsApp

    d = tmp_path / "exports"
    d.mkdir()
    (d / "bob.txt").write_text("[05/03/2026, 11:00:00] Bob Example: are you coming tonight?\n"
                               "[05/03/2026, 11:01:00] Jo: yes see you at eight\n")
    (d / "family.txt").write_text(
        "[06/03/2026, 09:00:00] Bob Example: my secret plan is to quit my job on monday\n"
        "[06/03/2026, 09:05:00] Carol Example: wow, tell nobody please\n"
    )
    src = WhatsApp()
    anon = Me(names=["Jo Dev"])  # git-derived name differs from the WhatsApp display name
    units = []
    for loc in src.discover(anon, {"paths": [str(d)]}):
        got, _ = src.read(loc, None, anon)
        units += list(got)
    texts = [u.text for u in units]
    assert any("quit my job" in t for t in texts), texts
    # FIXED: it is flagged as a guess, not asserted as fact.
    assert all(u.meta.get("self_detected") is True for u in units)


def test_B_ambiguous_revision_no_longer_keeps_ai_text_in_bank(home):
    """Original bug: `cap["ambiguous"] == 1 and in_bank == 1` -- a near-verbatim AI draft that could not be
    matched unambiguously to one specific prior draft still ended up admitted to the bank as if it were
    the user's own voice. P1-4 fixes this: the best raw ratio across every candidate draft is computed,
    and checked against AI_RATIO, BEFORE the ambiguity check -- so staying out of the bank never depended
    on the match being resolvable."""
    from mannerism import capture, refresh, store

    con = store.connect()
    # Two genuinely distinct redrafts (ratio to each other kept under capture.DUP_RATIO, so P1-4's own
    # near-duplicate draft-log collapse does not merge them into one row before matching even runs) that
    # each land close enough to the eventually-published text to be within capture.AMBIGUOUS_GAP of one
    # another -- the scenario the original bug report called "ambiguous".
    v1 = "I will go over the vendor contract first and then we can look at the hiring plan unless something comes up."
    v2 = "I will check the vendor contract first and then we can go over the hiring plan unless something blocks us."
    final_text = "I will review the vendor contract first and then we can look at the hiring plan unless something blocks us."
    now = datetime.now(UTC)
    capture.log_draft(v1, "work", con=con, now=now - timedelta(hours=2))
    capture.log_draft(v2, "work", con=con, now=now - timedelta(hours=1))
    assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 2  # not collapsed into one draft
    u = Unit("s1", "x1", now, final_text, "comment", register="work", thread_id="t1", audience="colleague")
    res = store.write_units(con, "s1", "loc", [u], {})
    cap = capture.capture_new_units(con, res.new_ids)
    refresh.refresh(con, ["work"])
    in_bank = con.execute("SELECT count(*) FROM bank WHERE unit_id=?", (u.unit_id,)).fetchone()[0]
    assert cap["ambiguous"] == 1  # still ambiguous: that part is unchanged and correct
    assert in_bank == 0  # FIXED: the near-verbatim AI text never enters the bank regardless


def test_C_forget_clears_packets_holding_the_forgotten_text(home):
    """Original bug: `leftover` (packets whose payload_json still contained "divorce") was truthy --
    forgetting a source left its private text sitting in a queued work packet. P1-6 fixes this: forget
    (for a specific source, same as for --all/--drafts) also clears the packets table."""
    from mannerism import api, packets, store

    con = store.connect()
    secret_note = "Private note about my divorce lawyer meeting next tuesday afternoon downtown"
    u = Unit("markdown", "n1", datetime.now(UTC) - timedelta(days=1), secret_note, "note", register="?",
             audience="self")
    store.write_units(con, "markdown", "loc", [u], {"x": 1})
    pk = packets.next_packet("label", con=con, me=Me())
    assert pk and pk["kind"] == "label"
    api.forget(source="markdown")
    con2 = store.connect()
    leftover = [r[0] for r in con2.execute("SELECT payload_json FROM packets") if "divorce" in r[0]]
    assert not leftover  # FIXED: nothing left referencing the forgotten text


def test_C2_forget_source_also_drops_rules_wholly_derived_from_it(home):
    """Original bug: a rule whose ONLY supporting edits all came from the just-forgotten source still
    "survived" forget -- its evidence was gone but the rule stayed. P1-6 fixes this: forget deletes any
    rule whose edit_ids intersect the deleted edits."""
    from mannerism import api, capture, rules, store

    con = store.connect()
    now = datetime.now(UTC)
    topics = ["the invoice batch export", "the onboarding flow redesign", "the staging deploy freeze",
              "the quarterly review deck", "the vendor contract renewal", "the search latency regression"]
    for i, topic in enumerate(topics):
        d = f"Hi team, I wanted to follow up on {topic} and confirm the plan for this week. Thanks so much!"
        capture.log_draft(d, "work", con=con, now=now - timedelta(hours=3))
        f = f"following up on {topic}, confirm the plan for this week?"
        u = Unit("gitx", f"c{i}", now - timedelta(hours=1), f, "comment", register="work", thread_id=f"t{i}",
                 audience="colleague")
        res = store.write_units(con, "gitx", "loc", [u], {})
        capture.capture_new_units(con, res.new_ids)
    pk = rules.distill_packet(con, "work")
    assert pk
    con.execute("INSERT INTO packets(packet_id, kind, register, created_at, payload_json, schema_json, expires_at) "
                "VALUES ('pk_x','distill','work','2026-01-01T00:00:00+00:00',?,?, '2999-01-01T00:00:00+00:00')",
                (json.dumps({"instructions": "", "payload": pk["payload"]}), json.dumps(pk["schema"])))
    ids = [p["edit_id"] for p in pk["payload"]["pairs"]]
    rules.submit_rules(con, "pk_x", {"rules": [{"text": "Leave out greetings and sign-offs", "edit_ids": ids}]})
    api.forget(source="gitx")
    con2 = store.connect()
    assert con2.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert not rules.list_rules(con2)  # FIXED: a rule with no surviving evidence does not survive either


def test_D_claude_cursor_no_longer_stores_unmasked_agent_text(_isolated_env, home):
    """Original bug: the cursor_json for the claude_code source contained the agent's raw last message,
    unmasked -- `cur` literally contained the user's email address and a live-looking secret key. P1-7
    fixes this: last_agent is PII-masked (and clipped) before it ever reaches the cursor."""
    from mannerism import config, store, sync

    p = _isolated_env / ".claude" / "projects" / "-x" / "s1.jsonl"
    p.parent.mkdir(parents=True)
    lines = [{"type": "assistant",
              "message": {"role": "assistant",
                          "content": [{"type": "text",
                                       "text": "Your key is sk-ABCDEFGHIJKLMNOPQRSTUVWX and mail bob@example.org"}]},
              "timestamp": "2026-10-01T10:00:00Z"}]
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    cfg = config.Config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    sync.sync(sources=["claude_code"])
    con = store.connect()
    cur = con.execute("SELECT cursor_json FROM cursors WHERE source='claude_code'").fetchone()[0]
    assert "bob@example.org" not in cur and "sk-ABCDEFGHIJKLMNOPQRSTUVWX" not in cur  # FIXED
    assert "[email]" in cur and "[secret]" in cur  # masked, not just dropped


def test_E_private_note_no_longer_leaks_into_public_rules(home):
    """Original bug: a PRIVATE markdown note (register "?", no meta.public) that happened to read like an
    edited version of a public-register draft got matched to it, and its private client name ("Acme
    Corp") ended up in the public distill packet's pairs. P1-5 fixes this at the source: a draft logged
    under the public register can only ever match a unit the adapter itself marked meta.public=true, so
    this private note is never even a capture candidate for that draft."""
    from mannerism import capture, rules, store

    con = store.connect()
    now = datetime.now(UTC)
    topics = ["the invoice batch export", "the onboarding flow redesign", "the staging deploy freeze",
              "the quarterly review deck", "the vendor contract renewal"]
    for i, topic in enumerate(topics):
        d = f"Shipped {topic} today. The part that surprised me was how early profiling paid off for everyone."
        capture.log_draft(d, "public", con=con, now=now - timedelta(hours=3))
        f = f"shipped {topic} today. surprised how early profiling paid off for my private client Acme Corp"
        u = Unit("markdown", f"n{i}", now - timedelta(hours=1), f, "note", register="?", thread_id=f"t{i}",
                 audience="self")
        res = store.write_units(con, "markdown", "loc", [u], {})
        out = capture.capture_new_units(con, res.new_ids)
        assert out["edited"] == 0 and out["accepted"] == 0  # FIXED: never even matched
    regs = rules.distill_registers(con)
    pk = rules.distill_packet(con, "public")
    assert "public" not in regs  # FIXED: no qualifying public-register edits exist at all
    assert pk is None or "Acme" not in json.dumps(pk)


def test_F_mbox_spoofed_spam_is_no_longer_treated_as_own_sent_mail(home, tmp_path):
    """Original bug: a message spoofing the user's own address in `From`, filed by Gmail under Spam (no
    "Sent" label), was read as the user's own sent mail. P1-2 fixes this: when X-Gmail-Labels is present
    at all, a message is kept only with a "Sent" label and never with a Spam/Trash label."""
    from mannerism.adapters.mbox import Mbox

    p = tmp_path / "All mail.mbox"
    box = mailbox.mbox(str(p))
    box.lock()
    msg = mailbox.mboxMessage()
    msg["From"] = "jo.dev@example.org"
    msg["To"] = "jo.dev@example.org"
    msg["Subject"] = "Your account was hacked"
    msg["Date"] = "Mon, 05 Oct 2026 10:00:00 +0000"
    msg["Message-ID"] = "<spam1@evil.example>"
    msg["X-Gmail-Labels"] = "Spam,Inbox"
    msg.set_payload("I have access to your device. Pay 1000 USD in bitcoin within 48 hours or else.")
    box.add(msg)
    box.flush()
    box.unlock()
    box.close()
    src = Mbox()
    me = Me(emails=["jo.dev@example.org"])
    loc = Location("mbox", str(p), p, "x")
    units, _ = src.read(loc, None, me)
    assert units == []  # FIXED: never attributed to the user


def test_G_git_ai_coauthored_commit_is_skipped_by_default(_isolated_env, home, tmp_path):
    """Original bug: a commit with a `Co-Authored-By: Claude ...` trailer became the user's own unit
    (with the trailer merely stripped from the text). Decision 4 / P1-3 fixes this: such a commit is
    skipped entirely by default; [git].include_ai_commits=true restores it, flagged meta.ai=true (kept
    out of the bank, see tests/test_bank.py's coverage of that exclusion)."""
    from mannerism.adapters.git import Git

    r = tmp_path / "repo"
    r.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="Jo", GIT_AUTHOR_EMAIL="jo.dev@example.org", GIT_COMMITTER_NAME="Jo",
               GIT_COMMITTER_EMAIL="jo.dev@example.org", GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
    subprocess.run(["git", "init", "-q", str(r)], check=True, env=env)
    msg = ("Refactor the ingestion pipeline to leverage a robust, seamless cursor model\n\n"
           "This change streamlines incremental sync and ensures comprehensive coverage.\n\n"
           "Co-Authored-By: Claude Opus <noreply@anthropic.com>\n")
    subprocess.run(["git", "-C", str(r), "commit", "-q", "--allow-empty", "-m", msg], check=True, env=env)
    me = Me(emails=["jo.dev@example.org"])
    src = Git()
    units, _ = src.read(Location("git", str(r), r, "r"), None, me)
    assert units == []  # FIXED: skipped outright, not merely de-attributed
    # opting back in keeps it, visibly flagged
    src2 = Git()
    src2.discover(me, {"roots": [str(r)], "include_ai_commits": True})
    units2, _ = src2.read(Location("git", str(r), r, "r"), None, me)
    assert units2 and "leverage" in units2[0].text and "Claude" not in units2[0].text
    assert units2[0].meta.get("ai") is True


def test_H_forget_source_stays_forgotten_across_the_next_plain_sync(_isolated_env, home):
    """Original bug: `n1 == 1, n2 == 0, n3 == 1` -- forgetting a source deleted its units, but the very
    next ordinary sync (what the SessionStart hook / lazy MCP path runs) silently re-read the same file
    and brought the unit straight back. Decision 1 fixes this: forget --source also disables that source
    in config, so it is left out of the next sync's enabled_sources() until the user re-enables it."""
    from mannerism import api, config, store, sync

    p = _isolated_env / ".claude" / "projects" / "-x" / "s1.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"type": "user",
                              "message": {"role": "user",
                                          "content": "please refactor the parser and keep the diff small"},
                              "timestamp": "2026-10-01T10:00:00Z", "uuid": "u1", "sessionId": "s1"}) + "\n")
    cfg = config.Config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    sync.sync(quick=True)
    con = store.connect()
    n1 = con.execute("SELECT count(*) FROM units").fetchone()[0]
    con.close()
    api.forget(source="claude_code")
    con = store.connect()
    n2 = con.execute("SELECT count(*) FROM units").fetchone()[0]
    con.close()
    sync.sync(quick=True)  # what the SessionStart hook / lazy MCP sync does next
    con = store.connect()
    n3 = con.execute("SELECT count(*) FROM units").fetchone()[0]
    assert n1 == 1 and n2 == 0
    assert n3 == 0  # FIXED: stays forgotten; the source was disabled, so this plain sync skips it
    assert not config.load_config().sources.get("claude_code", {}).get("enabled")
    api.forget(all=True)
    assert sync.last_sync_age_s() is None  # => sync_if_stale() starts a background sync right away


def test_I_blind_run_meta_no_longer_survives_source_forget(home):
    """Original bug: `blind:*` meta rows kept the actual compared text (e.g. "harbour cafe") verbatim even
    after the source those units came from was forgotten -- the text survived in a side channel the
    engine-level unit deletion never touched. P1-6 fixes this: forget also clears all blind:* meta."""
    from mannerism import api, blind, refresh, store

    con = store.connect()
    now = datetime.now(UTC)
    us = [Unit("chatx", f"r{i}", now - timedelta(days=i + 1),
               f"meet me at the old harbour cafe number {i} after the dentist ok", "message", register="close",
               thread_id=f"t{i}", audience="close") for i in range(8)]
    store.write_units(con, "chatx", "loc", us, {})
    refresh.refresh(con, None)
    blind.blind_packet(con, 6, Me())
    api.forget(source="chatx")
    con2 = store.connect()
    rows = [r[0] for r in con2.execute("SELECT key FROM meta WHERE key LIKE 'blind:%' AND value LIKE '%harbour cafe%'")]
    assert not rows  # FIXED: no leftover blind-test metadata holding the forgotten text
