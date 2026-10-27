"""A synthetic, fully fictional store for the voice page tests and screenshots (Mara Quill, handle mara.quill).

`populate()` writes into the store that MANNERISM_HOME points at. Nothing here is real data.
"""
from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta

from mannerism import capture, cards, refresh, schedule, store
from mannerism import config as config_mod
from mannerism.core import Unit

HANDLE = "mara.quill"
OBJECTS = ["the flaky retry test", "the sync scheduler backoff", "the card renderer", "the stats cache",
           "the onboarding copy", "the lint tell list", "the store migration", "the export script",
           "the draft log pruning", "the invoice batch", "the search latency", "the release checklist",
           "the staging deploy", "the vendor contract", "the dashboard rollout"]
VERBS = ["fix", "check", "rename", "split", "trace", "benchmark", "document", "revert", "tighten", "drop"]


def _ts(rng: random.Random, now: datetime, max_days: int = 300) -> datetime:
    return now - timedelta(days=rng.randint(1, max_days), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))


def _units(now: datetime, rng: random.Random) -> list[Unit]:
    out: list[Unit] = []
    n = 0

    def add(source, text, register, kind="message", audience="unknown", thread=None, public=False):
        nonlocal n
        n += 1
        out.append(Unit(source, f"{source}-{n}", _ts(rng, now), text, kind, register=register, thread_id=thread,
                        audience=audience, meta={"public": True} if public else {}))

    for i in range(150):  # agent prompts: lowercase, terse, no full stop
        v, o, o2 = rng.choice(VERBS), rng.choice(OBJECTS), rng.choice(OBJECTS)
        shape = rng.randint(0, 9)
        text = [f"{v} {o} and show me the diff", f"{v} {o}, keep it small", f"can you {v} {o}?",
                f"why does {o} keep failing? look at the logs first", f"ok now {v} {o2}",
                f"{v} {o} pls", "yes do it", "lgtm, ship it", f"wait, not that one. {v} {o2} instead",
                f"{v} {o} then run the suite, i'll check {o2} after"][shape]
        add(rng.choice(["claude_code", "codex"]), text, "agent", "prompt", "agent", f"a{i % 25}")
    for i in range(70):  # work: commits, capitalised, no full stop
        v, o = rng.choice(VERBS).capitalize(), rng.choice(OBJECTS)
        add("git", rng.choice([f"{v} {o[4:]}", f"{v} {o[4:]} in the sync path", f"{v} {o[4:]} and add a test",
                               f"{v} {o[4:]}: it was scanning twice"]), "work", "commit", "colleague", f"g{i % 30}")
    for i in range(55):  # work: Slack
        o, o2 = rng.choice(OBJECTS), rng.choice(OBJECTS)
        add("slack", rng.choice([f"can you take a look at {o} before standup? I'd rather not touch {o2} until Thursday",
                                 f"I'll {rng.choice(VERBS)} {o} first, then we can look at {o2}",
                                 f"{o} is done on my side. Anything blocking {o2}?", "sounds good, thanks"]),
            "work", "message", "colleague", f"s{i % 18}")
    for i in range(110):  # close: lowercase, short, laughs, emoji
        o = rng.choice(["the album", "that game", "my bike", "the pasta place", "the new show", "the ferry", "the hike"])
        t = rng.choice(["tonight", "tomorrow", "on friday", "this weekend", "after lunch"])
        text = rng.choice([f"lol did u see {o}", f"ok sounds good, {t} then", "running late, 10 mins",
                           f"haha yeah {o} was so good, we should go again {t}",
                           f"can't tomorrow but {t} works for me, i'll bring snacks", "nope", "omg yes",
                           f"are you free {t}?", f"btw {o} is on {t}"])
        if rng.random() < 0.18:
            text += " 😂"
        add("whatsapp", text, "close", "message", "close", f"c{i % 30}")
    for i in range(14):  # group: too few for a card
        add("telegram", f"who's in for {rng.choice(['pizza', 'the match', 'the hike'])} {rng.choice(['tonight', 'sunday'])}?",
            "group", "message", "group", f"gr{i % 4}")
    for i in range(90):  # formal email
        o, t = rng.choice(OBJECTS), rng.choice(["Friday", "next week", "the end of the month"])
        add("mbox", rng.choice([
            f"Thank you for your message about {o}; I confirm that I will send the signed form by {t}.",
            f"I am writing to ask whether {o} could be scheduled for {t}, and which documents you would like me to "
            f"prepare beforehand.",
            f"Please find below my questions regarding {o}, which I would like to settle before {t}.",
            f"Understood, I will confirm {o} by {t}.", f"Could you send the invoice for {o} before {t}?",
            f"Dear Ms Alder, thank you for your patience. I will have the figures ready by {t}.",
            f"Please see the attached summary of {o} and tell me whether it matches your records"]),
            "formal", "email", "unknown", f"m{i}")
    for _ in range(48):  # public posts
        o, v = rng.choice(OBJECTS), rng.choice(["shipping", "reviewing", "splitting", "benchmarking"])
        add(rng.choice(["x_archive", "bluesky"]), rng.choice([
            f"Shipped {o[4:]} today. The part that surprised me: {v} early saved a week of rework.",
            f"Hot take: {o[4:]} is never the problem. The handoff around it is.",
            f"Small win: {o[4:]} went from 4 minutes to 9 seconds. No new dependency, just deleting a loop.",
            f"{v.capitalize()} {o[4:]} taught me more than the last three books I read.",
            f"Why does {o[4:]} always break on a Friday?", f"Notes on {o[4:]}: https://example.com/notes/{rng.randint(10, 99)}",
            f"{o[4:].capitalize()} is done. Next up: the thing nobody wants to own", f"Day {rng.randint(10, 90)} of {v} {o[4:]} \U0001F525",
            f"Two lines on {o[4:]}:\nit was slow\nnow it is not", f"Rule I keep relearning: {v} {o[4:]} before you build on it",
            f"What would you cut from {o[4:]} if you had one hour?"]),
            "public", "post", "public", None, public=True)
    out.append(Unit("markdown", "notes-1", now - timedelta(days=12), f"Notes for @{HANDLE}: retry the export on Monday",
                    "note", register="work", audience="self", thread_id="notes"))
    return out


# (final, register) pool for the edit pairs; always contains a comma so a clause can be inserted mid-sentence
FINALS = [
    ("checked the invoice batch, two rows are off, sending fixes in a bit", "work"),
    ("retry backoff is merged, can you rerun the suite when you get a sec", "work"),
    ("pushed the rename, nothing else touched", "work"),
    ("the vendor contract needs one more pass, i'll do it tonight", "work"),
    ("dashboard rollout moved to thursday, no blockers", "work"),
    ("staging deploy is green, going live after lunch", "work"),
    ("release checklist is updated, two items left for you", "work"),
    ("running late, be there in ten", "close"),
    ("yes please, friday works and i'll bring the speaker", "close"),
    ("can't make it tonight, saturday instead?", "close"),
    ("loved the album, the second half is the best part", "close"),
    ("shipped the export script today, deleted a loop and it went from 4 minutes to 9 seconds", "public"),
]
NAMES = ["Sam", "Priya", "Leo", "Ana", "Jonas"]


def _edit_pairs(con, now: datetime, rng: random.Random) -> list[int]:
    ids = []
    for i in range(60):
        week = i // 6
        final, reg = FINALS[i % len(FINALS)]
        final = final + ("" if i < len(FINALS) else f" ({rng.choice(['mon', 'tue', 'wed', 'thu', 'fri'])})")
        p = max(0.07, 0.96 - 0.1 * week)
        draft = final
        head, _, tail = draft.partition(", ")
        if rng.random() < p:  # filler clause in the middle
            draft = head + ", and it is worth noting that " + tail
        if rng.random() < p:  # capital and full stop
            draft = draft[0].upper() + draft[1:] + "."
        if rng.random() < p:
            draft = f"Hi {rng.choice(NAMES)}, I hope you're doing well. " + draft
        if rng.random() < p:
            draft += " Let me know if you have any questions!"
        when = now - timedelta(days=70 - week * 7 - rng.randint(0, 5), hours=rng.randint(0, 8))
        eid, _status, created = capture.add_edit(con, reg, draft, final, "feedback", thread_key=f"t{i}", now=when)
        if created:
            ids.append(eid)
    return ids


RULES = [  # register, text, marker found in the draft of its evidence, days ago learned
    ("work", "Drop greetings and sign-offs; start with the point.", "Hi ", 9),
    ("work", "Write in lowercase and leave off the closing full stop.", ".", 21),
    ("close", "Skip 'Let me know if you have any questions'.", "Let me know", 33),
    ("work", "Cut filler clauses like 'it is worth noting that'.", "worth noting", 52),
]


def _rules(con, now: datetime) -> None:
    for reg, text, marker, days in RULES:
        rows = con.execute("SELECT edit_id FROM edits WHERE register=? AND instr(draft, ?) > 0 ORDER BY edit_id LIMIT 9",
                           (reg, marker)).fetchall()
        ids = [r[0] for r in rows] or [con.execute("SELECT min(edit_id) FROM edits").fetchone()[0]]
        stamp = store.ts_iso(now - timedelta(days=days))
        con.execute("INSERT INTO rules(register, text, support, contradicted, created_at, last_seen, edit_ids_json) "
                    "VALUES (?,?,?,?,?,?,?)", (reg, text, len(ids), 0, stamp, stamp, json.dumps(ids)))
        con.execute("UPDATE edits SET distilled=1 WHERE edit_id IN ({})".format(",".join("?" * len(ids))), ids)


TELL_DRAFTS = [
    ("Certainly! I hope this helps with the vendor contract. Let me know if you need anything else.", "work"),
    ("It's worth noting that the migration is not just a rename but a rethink of the store layout.", "work"),
    ("Let's delve into the invoice batch and unpack the rich tapestry of failures.", "work"),
    ("Great question! The retry logic is robust and seamless, and I hope this helps.", "work"),
    ("Absolutely! Here's a comprehensive overview of the release checklist for your review.", "work"),
    ("I hope this message finds you well. Please let me know if you have any further questions.", "formal"),
    ("Excited to share: we're thrilled to unveil a game-changing tapestry of features today.", "public"),
    ("Hey! Hope you're having a wonderful day. Just wanted to reach out about the weekend plans.", "close"),
]


def _drafts(con, now: datetime, rng: random.Random) -> None:
    n = 0
    for text, reg in TELL_DRAFTS * 3:
        n += 1
        capture.log_draft(f"{text} Ref {n}-{rng.randint(100, 999)}: {rng.choice(OBJECTS)}", reg, con=con,
                          now=now - timedelta(days=rng.randint(1, 38), hours=rng.randint(0, 20)))
    for text, reg in (("pushed the rename, nothing else touched, can you rerun the suite?", "work"),
                      ("the export script is done, deleted a loop and it is much faster now", "public")) * 3:
        n += 1
        capture.log_draft(f"{text} #{n}", reg, con=con, now=now - timedelta(days=rng.randint(1, 38)))


def populate(*, seed: int = 7, with_error: bool = False) -> None:
    """Fill the current MANNERISM_HOME with a lived-in store."""
    rng = random.Random(seed)
    now = datetime.now(UTC)
    config_mod.ensure_home()
    cfg = config_mod.load_config()
    cfg.me.names = ["Mara Quill"]
    cfg.me.handles = {"x": HANDLE, "bluesky": f"{HANDLE}.dev"}
    units = _units(now, rng)
    for name in sorted({u.source for u in units}):
        cfg.sources[name] = {"enabled": True, "paths": []}
    config_mod.save_config(cfg)
    con = store.connect()
    try:
        by: dict[str, list[Unit]] = {}
        for u in units:
            by.setdefault(u.source, []).append(u)
        for src, us in by.items():
            store.write_units(con, src, f"{src}-loc", us, {"i": len(us)})
        refresh.refresh(con, None, cfg.me)
        _edit_pairs(con, now, rng)
        _rules(con, now)
        _drafts(con, now, rng)
        cards.save_agent_card("work", "Writes short, plain messages. Opens with the point, not a greeting. Capital letter "
                              "for commits, lowercase in chat. Prefers 'use' to 'leverage'. Asks one clear question "
                              "at a time and leaves off the closing full stop when the message is short.", cfg.me)
        cards.save_agent_card("public", "Plain declarative posts. States the result first, then the surprise. No "
                              "hashtags, no calls to action, no exclamation marks. Short sentences with concrete "
                              "numbers.", cfg.me)
        cards.save_agent_card("agent", "Terse lowercase prompts, often a bare verb and the thing to change. Asks 'why' "
                              "before asking for a fix. Says 'pls' when in a hurry. Wants a small diff.", cfg.me)
        store.set_meta(con, "card_needed:agent", "1")  # new prompts arrived since that card: upgrade available
        for i, src in enumerate(sorted(by)):
            schedule.record_success(con, src, 0, now=now - timedelta(minutes=7 + 23 * i))
        if with_error:
            schedule.record_failure(con, "bluesky", 3600, "HTTP 429: try again later", backoff=True, now=now)
        store.set_meta(con, "last_sync", (now - timedelta(minutes=7)).isoformat(timespec="seconds"))
    finally:
        con.close()


def populate_review(*, seed: int = 11) -> None:
    """Add what the "Is this you?" page needs on top of `populate()`: texts with an origin (account, project, session
    kind), some machine-typed ones held for review, and a template repeated across sessions. All fictional."""
    from mannerism import humanness

    rng = random.Random(seed)
    now = datetime.now(UTC)
    HUMAN = ["add a retry to the upload step", "why is the thumbnail job so slow", "rename it to ingest_queue", "lgtm",
             "fix the flaky test", "can you split that function in two?", "show me the diff first", "ok ship it",
             "use the smaller model for the tagging pass", "the empty state reads cold, warm it up",
             "no, keep the old name", "try again with the fixture from yesterday"]
    TICKET = ("<task-notification from=planner> Ticket QA-{n}: summarise the open items for the release board and post a "
              "status update to the team channel. Reply with DONE or BLOCKED. </task-notification>")
    STATUS = "status: running\nstage: {stage}\nowner: relay-{n}\nnext: wait for review\nnotes: nothing to report"
    out: dict[str, list[Unit]] = {"claude_code": [], "codex": []}

    def add(source, i, text, ts, account, project, kind, session, template=None, held=False, score=0.9):
        out[source].append(Unit(source, f"{source}-rv-{i}", ts, text, "prompt", register="agent", thread_id=session,
                                audience="agent", account=account, project=project, project_path=f"~/work/{project}",
                                session_kind=kind, session=session, template=template, human_score=score, held=held))

    i = 0
    for k in range(34):  # a person, in their own project
        i += 1
        add("claude_code", i, rng.choice(HUMAN) + (" " if k % 5 == 0 else ""), now - timedelta(days=rng.randint(1, 120)),
            "~/.claude", "recipe-app", "interactive", f"h{k % 9:02d}")
    for k in range(18):
        i += 1
        add("codex", i, rng.choice(HUMAN), now - timedelta(days=rng.randint(1, 90)), "~/.codex", "site", "interactive",
            f"c{k % 6:02d}")
    tmpl = humanness.template_of(TICKET.format(n=100))[0]
    for k in range(16):  # an orchestrator: the same ticket prompt in many sessions
        i += 1
        add("claude_code", i, TICKET.format(n=4800 + k), now - timedelta(days=60 - k * 2), "~/.claude-work", "orchestrator",
            "headless", f"o{k:02d}", template=tmpl, held=True, score=0.22)
    for k in range(9):  # status blocks from a relay
        i += 1
        add("claude_code", i, STATUS.format(stage=rng.choice(["build", "test", "deploy"]), n=k), now - timedelta(days=30 - k),
            "~/.claude-work", "relay", "relay", f"r{k % 4:02d}", held=True, score=0.31)
    i += 1
    add("claude_code", i, "mail me at mara.quill@example.org when it is done", now - timedelta(days=3), "~/.claude-work",
        "orchestrator", "headless", "o99", held=True, score=0.4)
    con = store.connect()
    try:
        for src, us in out.items():
            store.write_units(con, src, f"{src}-review-loc", us, {"i": len(us)})
        refresh.refresh(con, None)
    finally:
        con.close()
