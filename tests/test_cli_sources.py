"""`mannerism sources add files` / `check`, and the listing."""
from __future__ import annotations

import json

from conftest import make_corpus  # noqa: F401  (keeps the shared fixtures importable)

from mannerism import cli, config, core, sync

REC = {"schema_version": 1, "op": "upsert", "id": "a", "ts": "2026-10-20T12:00:00Z",
       "text": "Hello from a script, this is the text of my first record.", "kind": "post", "register": "work"}


def run(capsys, *argv):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


def test_add_files_asks_for_confirmation_and_writes_an_instance(tmp_path, capsys, monkeypatch):
    d = tmp_path / "notes"
    d.mkdir()
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    code, out, err = run(capsys, "sources", "add", "files", str(d), "--name", "my-notes", "--register", "work")
    assert code == 2 and "your own" in err and "not added" in err
    assert not (core.home() / "config.toml").exists()
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    assert run(capsys, "sources", "add", "files", str(d), "--name", "my-notes")[0] == 2
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    code, out, err = run(capsys, "sources", "add", "files", str(d), "--name", "my-notes", "--register", "work")
    assert code == 0 and "my-notes" in out
    entry = config.load_config().sources["my-notes"]
    assert entry == {"adapter": "files", "path": str(d.resolve()), "enabled": True, "register": "work"}
    assert "[sources.my-notes]" in (core.home() / "config.toml").read_text()


def test_add_files_with_yes_then_the_folder_is_watched(tmp_path, capsys):
    d = tmp_path / "notes"
    d.mkdir()
    code, out, _ = run(capsys, "sources", "add", "files", str(d), "--name", "drops", "--register", "close", "--kind", "message", "--yes")
    assert code == 0 and "picked up on every later sync" in out
    (d / "a.txt").write_text("hey are you around later today for a coffee")
    assert sync.sync()["sources"]["drops"]["new_units"] == 1
    (d / "b.txt").write_text("also bring the charger please, mine died on the train")
    assert sync.sync()["sources"]["drops"]["new_units"] == 1
    # re-adding updates the same instance instead of duplicating it
    assert run(capsys, "sources", "add", "files", str(d), "--name", "drops", "--register", "work", "--yes")[0] == 0
    assert config.load_config().sources["drops"]["register"] == "work"


def test_add_files_validates_its_arguments(tmp_path, capsys):
    d = tmp_path / "n"
    d.mkdir()
    base = ["sources", "add", "files"]
    assert "usage" in run(capsys, *base, str(d), "--yes")[2]
    assert "usage" in run(capsys, *base, "--name", "xs", "--yes")[2]
    assert "--name must be" in run(capsys, *base, str(d), "--name", "Bad Name", "--yes")[2]
    assert "already the name" in run(capsys, *base, str(d), "--name", "git", "--yes")[2]
    assert "unknown register" in run(capsys, *base, str(d), "--name", "xs", "--register", "loud", "--yes")[2]
    assert "unknown kind" in run(capsys, *base, str(d), "--name", "xs", "--kind", "email", "--yes")[2]
    assert "path not found" in run(capsys, *base, str(d / "nope"), "--name", "xs", "--yes")[2]
    assert not (core.home() / "config.toml").exists()


def test_add_files_public_register_prints_the_verification_note(tmp_path, capsys):
    d = tmp_path / "n"
    d.mkdir()
    code, out, _ = run(capsys, "sources", "add", "files", str(d), "--name", "pub", "--register", "public", "--yes")
    assert code == 0 and "verified" in out


def test_check_prints_line_numbered_errors_and_stores_nothing(tmp_path, capsys):
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join([json.dumps(REC), "not json", json.dumps({**REC, "kind": "nope"}),
                           json.dumps({"schema_version": 1, "op": "delete", "id": "a", "ts": "2026-10-21T00:00:00Z"})]) + "\n")
    code, out, _ = run(capsys, "sources", "check", str(p))
    assert code == 1
    assert f"{p}:2: invalid JSON" in out and f"{p}:3: kind must be one of" in out
    assert "2 valid record(s) (1 upsert, 1 delete), 2 error(s). Nothing was stored." in out
    assert not core.home().exists()
    code, out, _ = run(capsys, "sources", "check", str(p), "--json")
    data = json.loads(out)
    assert code == 1 and data["ok"] is False and [e["line"] for e in data["errors"]] == [2, 3]
    good = tmp_path / "good.jsonl"
    good.write_text(json.dumps(REC) + "\n")
    code, out, _ = run(capsys, "sources", "check", str(good))
    assert code == 0 and "1 valid record(s)" in out and "0 error(s)" in out
    assert run(capsys, "sources", "check", str(tmp_path / "missing.jsonl"))[0] == 2
    assert run(capsys, "sources", "check")[0] == 2


def test_listing_shows_the_files_source_and_your_instances(tmp_path, capsys):
    d = tmp_path / "n"
    d.mkdir()
    run(capsys, "sources", "add", "files", str(d), "--name", "mine", "--yes")
    code, out, _ = run(capsys, "sources")
    assert code == 0 and "mine" in out and "files (yours)" in out and "\nfiles " in out
    data = json.loads(run(capsys, "sources", "--json")[1])
    assert {"files", "mine"} <= {s["name"] for s in data}
    assert next(s for s in data if s["name"] == "mine")["path"] == str(d.resolve())
    # a configured instance can be disabled / enabled like any source
    assert run(capsys, "sources", "disable", "mine")[0] == 0 and not config.load_config().sources["mine"]["enabled"]
    assert run(capsys, "sources", "enable", "mine")[0] == 0 and config.load_config().sources["mine"]["adapter"] == "files"


def test_init_never_enables_files_by_itself(tmp_path, capsys):
    from mannerism import api
    inbox = core.home() / "inbox" / "files"
    inbox.mkdir(parents=True)
    (inbox / "x.txt").write_text("a note dropped in the inbox before init was run")
    api.setup()
    assert not config.load_config().sources.get("files", {}).get("enabled")
    assert run(capsys, "sources", "enable", "files")[0] == 0
    assert sync.sync()["sources"]["files"]["new_units"] == 1


def test_adding_a_path_to_a_files_instance_asks_to_confirm_it_is_yours(tmp_path, capsys, monkeypatch):
    first, second = tmp_path / "one", tmp_path / "two"
    first.mkdir()
    second.mkdir()
    assert run(capsys, "sources", "add", "files", str(first), "--name", "mine", "--yes")[0] == 0
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    code, _, err = run(capsys, "sources", "add", "mine", str(second))
    assert code == 2 and "your own" in err and "not added" in err
    assert "paths" not in config.load_config().sources["mine"] or str(second.resolve()) not in config.load_config().sources["mine"]["paths"]
    assert run(capsys, "sources", "add", "mine", str(second), "--yes")[0] == 0
    assert str(second.resolve()) in config.load_config().sources["mine"]["paths"]
    monkeypatch.setattr("builtins.input", lambda *a: "yes")
    third = tmp_path / "three"
    third.mkdir()
    assert run(capsys, "sources", "add", "mine", str(third))[0] == 0


def test_enabling_the_real_x_adapter_prints_ceiling_and_hosts_not_the_allow_network_hint(monkeypatch, capsys):
    """The real XApi with a fake environment; nothing touches the network (enable never reads)."""
    monkeypatch.setenv("X_BEARER_TOKEN", "fake-token-for-test")
    cfg = config.load_config()
    cfg.sources["x"] = {"username": "jo_dev"}
    config.save_config(cfg)
    monkeypatch.setattr("builtins.input", lambda *a: (_ for _ in ()).throw(EOFError()))
    assert run(capsys, "sources", "enable", "x")[0] == 2  # asks first: paid
    code, out, err = run(capsys, "sources", "enable", "x", "--yes")
    assert code == 0
    assert "api.x.com" in out and "first read about" in out and "monthly ceiling about $" in out
    assert "set allow_network" not in out and "set allow_network" not in err
    assert "never launches it" in out  # a paid adapter does not start from a session start
    entry = config.load_config().sources["x"]
    assert entry["enabled"] is True and entry["allow_network"] is True and "run_on_session_start" not in entry


def test_enable_declined_leaves_no_consent_behind(monkeypatch, capsys):
    monkeypatch.setenv("X_BEARER_TOKEN", "fake-token-for-test")
    cfg = config.load_config()
    cfg.sources["x"] = {"username": "jo_dev"}
    config.save_config(cfg)
    monkeypatch.setattr("builtins.input", lambda *a: "no")
    assert run(capsys, "sources", "enable", "x")[0] == 2
    assert "allow_network" not in config.load_config().sources["x"]
