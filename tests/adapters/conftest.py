import json
import os
from pathlib import Path

import pytest

from mannerism.core import Me

from .fakenet import net  # noqa: F401 - the fake-network fixture, shared by the social-source tests


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path, monkeypatch):
    """See tests/conftest.py: the same guarantee, repeated here because adapter tests live in their
    own conftest tree and must never inherit this shell's CODEX_HOME / CLAUDE_CONFIG_DIR."""
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
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("MANNERISM_HOME", str(h / ".mannerism"))
    for var in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "APPDATA"):
        monkeypatch.delenv(var, raising=False)
    return h


@pytest.fixture
def me():
    return Me(names=["Jo Dev"], emails=["jo.dev@example.org"], handles={"x": "jo.dev"})


def run(src, loc, cursor, me):
    units, cur = src.read(loc, cursor, me)
    units = list(units)
    for u in units:
        u.validate()
    return units, cur


def write_jsonl(path: Path, objs, newline_end=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(o if isinstance(o, str) else json.dumps(o) for o in objs)
    path.write_text(body + ("\n" if newline_end else ""), "utf-8")


def append_jsonl(path: Path, objs, newline_end=True):
    body = "\n".join(o if isinstance(o, str) else json.dumps(o) for o in objs)
    with open(path, "a", encoding="utf-8") as f:
        f.write(body + ("\n" if newline_end else ""))
