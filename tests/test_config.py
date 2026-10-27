import os
import stat
import tomllib

from mannerism import config
from mannerism.core import Me


def test_roundtrip_and_permissions(home):
    cfg = config.Config(
        me=Me(names=['Jo "J" Rivera'], emails=["jo@example.org"], handles={"x": "jo.dev"}, phone_suffixes=["0123"]),
        sources={"claude_code": {"enabled": True, "paths": ["/tmp/a b", "c\\d"]}, "gh": {"enabled": False}},
        contacts_work=["abc123"], lint={"tell_threshold": 0.05}, extra={"git": {"roots": ["/src"]}})
    p = config.save_config(cfg)
    back = config.load_config()
    assert back.me == cfg.me and back.sources == cfg.sources and back.contacts_work == ["abc123"]
    assert back.tell_threshold == 0.05 and back.extra == {"git": {"roots": ["/src"]}}
    assert back.enabled_sources() == ["claude_code"]
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600 and stat.S_IMODE(os.stat(p.parent).st_mode) == 0o700
    assert tomllib.loads(p.read_text())["me"]["handles"] == {"x": "jo.dev"}


def test_missing_or_broken_file_gives_defaults(home):
    assert config.load_config().tell_threshold == config.DEFAULT_TELL_THRESHOLD
    config.ensure_home()
    config.config_path().write_text("this is [not toml")
    assert config.load_config().sources == {}


def test_for_adapter_offers_every_spelling():
    cfg = config.Config(sources={"git": {"enabled": True, "roots": ["/r"]}}, contacts_work=["h"],
                        extra={"markdown": {"dirs": ["/notes"]}})
    d = cfg.for_adapter("git")
    assert d["roots"] == ["/r"] and d["sources"]["git"]["roots"] == ["/r"] and d["contacts"]["work"] == ["h"]
    assert cfg.for_adapter("markdown")["dirs"] == ["/notes"]


def test_identity_guess_never_raises(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))  # no git binary
    me = config.identity_guess()
    assert me.names == [] and me.emails == []


def test_identity_guess_unions_repo_local_identity(_isolated_env):
    """Root finding (c): plenty of machines have no global git identity at all, but a repo the user
    actually commits to has its own (repo-local) user.name/user.email -- still their own config.

    Uses `_isolated_env` directly, not the `home` fixture: that one is MANNERISM_HOME, a different
    directory from the real (isolated) $HOME that git.py's repo discovery climbs from."""
    import subprocess

    repo = _isolated_env / "code" / "proj"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Jo Dev"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "jo.dev@example.org"], check=True)
    me = config.identity_guess()
    assert "Jo Dev" in me.names and "jo.dev@example.org" in me.emails
