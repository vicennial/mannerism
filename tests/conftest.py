"""Synthetic corpus and fake adapters shared by the engine tests. Everything here is fictional."""
from __future__ import annotations

import os
import random
from datetime import UTC, datetime, timedelta

import pytest

from mannerism import adapters
from mannerism.core import Location, Unit


def NOW():  # noqa: N802
    return datetime.now(UTC)

THINGS = ["the album", "that game", "my bike", "the pasta place", "the new show", "her cat", "the ferry", "that cafe",
          "the museum", "your playlist", "the match", "the hike"]
TIMES = ["tonight", "tomorrow", "on friday", "this weekend", "in ten mins", "after lunch", "next week"]
TOPICS = ["the invoice batch", "the onboarding flow", "the staging deploy", "the quarterly review", "the vendor contract",
          "the search latency", "the release checklist", "the retro notes", "the hiring plan", "the dashboard rollout"]
VERBS = ["check", "review", "ship", "move", "rewrite", "split", "close", "reopen", "benchmark", "document"]
FORMAL = [("I am writing to ask whether the {t} could be scheduled for {w}, and which documents you would like me to "
           "prepare beforehand."),
          "Thank you for your message about the {t}; I confirm that I will send the signed form by {w}.",
          "Please find below my questions regarding the {t}, which I would like to settle before {w}."]


def make_corpus(seed: int = 7, per: int | None = None) -> dict[str, list[Unit]]:
    """Units per fake source, varied in length and style, spread over the last ~300 days."""
    rng = random.Random(seed)
    now = NOW()

    def ts() -> datetime:
        return now - timedelta(days=rng.randint(1, 300), hours=rng.randint(0, 23), minutes=rng.randint(0, 59))

    chat: list[Unit] = []
    n = 0

    def add(text, register, kind="message", audience="close", thread=None, meta=None, source="fakechat", out=chat):
        nonlocal n
        n += 1
        out.append(Unit(source, f"r{n}", ts(), text, kind, register=register, thread_id=thread, audience=audience,
                        meta=meta or {}))

    for i in range(70):  # close: lowercase, short, no em dashes
        t, w = rng.choice(THINGS), rng.choice(TIMES)
        shape = rng.randint(0, 5)
        text = [f"lol did u see {t}", f"ok sounds good, {w} then", f"running late, be there in {rng.randint(3, 40)} mins",
                f"haha yeah {t} was so good, we should go again {w} if ur free",
                f"can't tomorrow but {w} works for me, i'll bring snacks and the speaker for {t}",
                "nope"][shape]
        if i % 9 == 0:
            text += " 😂"
        add(text, "close", thread=f"c{i % 30}")
    for i in range(30):  # group
        add(f"who's in for {rng.choice(THINGS)} {rng.choice(TIMES)}?" if i % 2 else f"lmao {rng.choice(THINGS)} again",
            "group", audience="group", thread=f"g{i % 10}")
    for i in range(50):  # work: capitalised, em dashes in 40% of messages
        a, b = rng.choice(TOPICS), rng.choice(TOPICS)
        v = rng.choice(VERBS)
        if i % 5 < 2:
            text = f"I will {v} {a} first {chr(0x2014)} then we can look at {b}, unless something blocks us."
        else:
            text = f"Can you {v} {a} before standup? I would rather not touch {b} until Thursday."
        if i % 7 == 0:
            text += " Let me know what you find, and whether the numbers look right to you."
        add(text, "work", audience="colleague", thread=f"w{i % 20}", kind="comment")
    for i in range(15):  # formal
        add(rng.choice(FORMAL).format(t=rng.choice(TOPICS), w=rng.choice(TIMES)), "formal", kind="email",
            audience="unknown", thread=f"f{i}")
    for i in range(30):  # agent prompts
        add(f"{rng.choice(VERBS)} {rng.choice(TOPICS)} and then show me the diff, keep it small",
            "agent", kind="prompt", audience="agent", thread=f"a{i % 8}")
    public: list[Unit] = []
    for _ in range(14):
        add(f"Shipped {rng.choice(TOPICS)} today. The part that surprised me: {rng.choice(VERBS)}ing early saved a week "
            f"of rework on {rng.choice(TOPICS)}.", "public", kind="post", audience="public",
            meta={"public": True}, source="fakepub", out=public)
    mail: list[Unit] = []
    for i in range(12):  # email: ambiguous, awaiting a label
        add(f"Hi, quick one about {rng.choice(TOPICS)}: could you confirm the date for {rng.choice(TIMES)}? Thanks, Jo",
            "?", kind="email", audience="unknown", thread=f"m{i}", source="fakemail", out=mail)
    return {"fakechat": chat, "fakepub": public, "fakemail": mail}


class FakeSource:
    """An in-memory adapter: yields units from a list, cursor = how many it has already delivered."""
    tier = "auto"

    def __init__(self, name: str, units: list[Unit]):
        self.name = name
        self.units = units
        self.crash_after: int | None = None  # raise after yielding this many units (simulates a crash mid-read)
        self.reads = 0

    def discover(self, me, config):
        return [Location(self.name, f"{self.name}-loc", None, f"{self.name} (fake)")]

    def read(self, loc, cursor, me):
        self.reads += 1
        start = (cursor or {}).get("i", 0)
        batch = self.units[start:]
        crash = self.crash_after

        def gen():
            for k, u in enumerate(batch):
                if crash is not None and k >= crash:
                    raise RuntimeError("boom")
                yield u

        return gen(), {"i": len(self.units)}


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """Every test, in every file under `tests/`, runs fully isolated from the real user's machine.

    This shell exports CODEX_HOME and CLAUDE_CONFIG_DIR pointing at the real user's data; a test that
    forgot to unset them could read or (worse) write real transcripts. So this is autouse, not opt-in:
    a fake HOME and MANNERISM_HOME, no CODEX_HOME / CLAUDE_CONFIG_DIR / XDG_CONFIG_HOME, and git
    pointed at /dev/null for both its global and system config. A test's own `home`/`iso` fixture may
    still pick a more specific HOME; this just guarantees the dangerous vars are never inherited."""
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("MANNERISM_HOME", str(h / ".mannerism"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME", "APPDATA"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MANNERISM_HOME", str(tmp_path / "mh"))
    return tmp_path / "mh"


@pytest.fixture
def fakes(home, monkeypatch):
    """Register three fake sources and return {name: FakeSource}."""
    corpus = make_corpus()
    sources = {n: FakeSource(n, u) for n, u in corpus.items()}
    monkeypatch.setattr(adapters, "REGISTRY", {n: f"fake:{n}" for n in sources})
    monkeypatch.setattr(adapters, "load", lambda name: sources[name])
    return sources


def ingest(con, units, source=None, refresh_all=True):
    """Write units straight into the store and rebuild everything derived (no adapter involved)."""
    from mannerism import refresh, store
    by_source: dict[str, list[Unit]] = {}
    for u in units:
        by_source.setdefault(u.source, []).append(u)
    ids = []
    for src, us in by_source.items():
        ids += store.write_units(con, src, f"{src}-loc", us, {"i": len(us)}).new_ids
    if refresh_all:
        refresh.refresh(con, None)
    return ids


def unit(text, register="close", ref=None, days=10, source="s1", **kw):
    ref = ref or f"u{abs(hash((text, source, days))) % 10**8}"
    return Unit(source, ref, NOW() - timedelta(days=days), text, kw.pop("kind", "message"), register=register, **kw)


@pytest.fixture
def iso(tmp_path, monkeypatch):
    """A fully isolated environment for CLI, hook and MCP tests: fake HOME, own data dir, and no way to reach the
    real Claude Code / Codex / git configuration. Returns the env dict to hand to subprocesses too."""
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    env = {"HOME": str(h), "MANNERISM_HOME": str(h / ".mannerism"), "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_CONFIG_SYSTEM": os.devnull}
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    for k in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(k, raising=False)
    return env


_REAL_HOME_DATA = os.path.join(os.path.expanduser("~"), ".mannerism")


def pytest_sessionstart(session):
    session.config._mannerism_home_existed = os.path.exists(_REAL_HOME_DATA)


def pytest_sessionfinish(session, exitstatus):
    """Fail the run if any test wrote to the real ~/.mannerism (an isolation leak)."""
    if not session.config._mannerism_home_existed and os.path.exists(_REAL_HOME_DATA):
        session.exitstatus = 1
        raise pytest.UsageError(f"isolation leak: a test created {_REAL_HOME_DATA}")
