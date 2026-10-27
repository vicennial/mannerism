"""`mannerism connect`: the generic JSON/TOML merge engine, detection, and the CLI surface.

All file operations run against `tmp_path`, never a real harness config; the `iso` fixture (from conftest) also
guarantees HOME is fake for the CLI-level tests."""
from __future__ import annotations

import json
import os
import stat
import tomllib
from pathlib import Path

import pytest

from mannerism import cli, connect

ENTRY = {"command": "uvx", "args": ["mannerism", "mcp"]}


def run(capsys, *argv):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


# --- JSON merge -------------------------------------------------------------------------------------------------

def test_merge_json_creates_a_missing_file(tmp_path):
    path = tmp_path / "sub" / "mcp.json"
    res = connect.merge_json(path, "mcpServers", ENTRY)
    assert res.changed and res.backup is None
    data = json.loads(path.read_text())
    assert data == {"mcpServers": {"mannerism": ENTRY}}


def test_merge_json_preserves_other_keys_and_file_mode(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": {"other": {"command": "foo"}}, "unrelated": 1}))
    os.chmod(path, 0o640)
    res = connect.merge_json(path, "mcpServers", ENTRY)
    assert res.changed and res.backup is not None and res.backup.exists()
    data = json.loads(path.read_text())
    assert data["mcpServers"]["other"] == {"command": "foo"}
    assert data["unrelated"] == 1
    assert data["mcpServers"]["mannerism"] == ENTRY
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    backup_data = json.loads(res.backup.read_text())
    assert "mannerism" not in backup_data["mcpServers"]


def test_merge_json_without_an_existing_key_creates_it(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"other_top_level": True}))
    res = connect.merge_json(path, "mcpServers", ENTRY)
    assert res.changed
    data = json.loads(path.read_text())
    assert data["other_top_level"] is True
    assert data["mcpServers"] == {"mannerism": ENTRY}


def test_merge_json_is_idempotent(tmp_path):
    path = tmp_path / "mcp.json"
    r1 = connect.merge_json(path, "mcpServers", ENTRY)
    assert r1.changed
    r2 = connect.merge_json(path, "mcpServers", ENTRY)
    assert not r2.changed and r2.backup is None
    assert list(tmp_path.glob("*.bak-*")) == []  # file didn't exist before the first write: no backup ever taken
    assert json.loads(path.read_text()) == {"mcpServers": {"mannerism": ENTRY}}


def test_merge_json_refuses_jsonc_with_comments(tmp_path):
    path = tmp_path / "mcp.json"
    original = '{\n  // a comment\n  "mcpServers": {}\n}\n'
    path.write_text(original)
    with pytest.raises(connect.ConnectError, match="does not parse as plain JSON"):
        connect.merge_json(path, "mcpServers", ENTRY)
    assert path.read_text() == original  # untouched


def test_merge_json_refuses_when_the_key_is_not_an_object(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps({"mcpServers": "nope"}))
    with pytest.raises(connect.ConnectError):
        connect.merge_json(path, "mcpServers", ENTRY)


def test_merge_json_refuses_when_the_top_level_is_not_an_object(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text(json.dumps([1, 2, 3]))
    with pytest.raises(connect.ConnectError):
        connect.merge_json(path, "mcpServers", ENTRY)


def test_json_configured_reports_unknown_for_a_missing_or_bad_file(tmp_path):
    path = tmp_path / "mcp.json"
    assert connect.json_configured(path, "mcpServers") is None
    path.write_text("not json")
    assert connect.json_configured(path, "mcpServers") is None
    path.write_text(json.dumps({"mcpServers": {}}))
    assert connect.json_configured(path, "mcpServers") is False
    path.write_text(json.dumps({"mcpServers": {"mannerism": ENTRY}}))
    assert connect.json_configured(path, "mcpServers") is True


# --- TOML append (Codex) -----------------------------------------------------------------------------------------

def test_merge_json_writes_through_a_symlink(tmp_path):
    # dotfiles managers (stow, chezmoi) symlink configs; the link must survive and the real file get the entry
    real = tmp_path / "dotfiles" / "mcp.json"
    real.parent.mkdir()
    real.write_text(json.dumps({"mcpServers": {"other": {"command": "foo"}}}))
    link = tmp_path / "mcp.json"
    link.symlink_to(real)
    res = connect.merge_json(link, "mcpServers", ENTRY)
    assert res.changed and link.is_symlink()
    assert json.loads(real.read_text())["mcpServers"]["mannerism"] == ENTRY


def test_merge_toml_writes_through_a_symlink(tmp_path):
    real = tmp_path / "dotfiles" / "config.toml"
    real.parent.mkdir()
    real.write_text('model = "x"\n')
    link = tmp_path / "config.toml"
    link.symlink_to(real)
    connect.merge_toml_table(link, "mcp_servers", ENTRY)
    assert link.is_symlink()
    assert tomllib.loads(real.read_text())["mcp_servers"]["mannerism"] == ENTRY


def test_non_utf8_files_are_refused_and_listed_as_unknown(tmp_path):
    for name, merge, check, key in (
        ("mcp.json", connect.merge_json, connect.json_configured, "mcpServers"),
        ("config.toml", connect.merge_toml_table, connect.toml_configured, "mcp_servers"),
    ):
        path = tmp_path / name
        path.write_bytes(b"\xff\xfe{}")
        with pytest.raises(connect.ConnectError, match="not UTF-8"):
            merge(path, key, ENTRY)
        assert path.read_bytes() == b"\xff\xfe{}"
        assert check(path, key) is None


def test_merge_json_refuses_a_bom_with_an_accurate_message(tmp_path):
    path = tmp_path / "mcp.json"
    path.write_text("\ufeff{}", encoding="utf-8")
    with pytest.raises(connect.ConnectError, match="byte-order mark"):
        connect.merge_json(path, "mcpServers", ENTRY)


def test_merge_refuses_when_the_file_changes_mid_edit(tmp_path, monkeypatch):
    # the harness app saving its own config between our read and our write must not be silently overwritten
    path = tmp_path / "mcp.json"
    path.write_text("{}")
    real_backup = connect._backup

    def backup_then_app_saves(p):
        bak = real_backup(p)
        p.write_text(json.dumps({"theme": "dark"}))
        return bak

    monkeypatch.setattr(connect, "_backup", backup_then_app_saves)
    with pytest.raises(connect.ConnectError, match="changed while"):
        connect.merge_json(path, "mcpServers", ENTRY)
    assert json.loads(path.read_text()) == {"theme": "dark"}
    assert not [f for f in tmp_path.iterdir() if f.name.startswith(".mcp.json.")]


def test_goose_uses_the_appdata_path_on_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(connect.platform, "system", lambda: "Windows")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert connect._goose_config_path() == tmp_path / "Block" / "goose" / "config" / "config.yaml"


def test_merge_toml_appends_without_touching_existing_text(tmp_path):
    path = tmp_path / "config.toml"
    original = '# a comment that must survive\nmodel = "gpt-6"\n\n[mcp_servers.other]\ncommand = "foo"\nargs = ["bar"]\n'
    path.write_text(original)
    os.chmod(path, 0o600)
    res = connect.merge_toml_table(path, "mcp_servers", ENTRY)
    assert res.changed and res.backup is not None
    new_text = path.read_text()
    assert new_text.startswith(original)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    data = tomllib.loads(new_text)
    assert data["model"] == "gpt-6"
    assert data["mcp_servers"]["other"]["command"] == "foo"
    assert data["mcp_servers"]["mannerism"] == ENTRY
    assert res.backup.read_text() == original


def test_merge_toml_creates_a_missing_file(tmp_path):
    path = tmp_path / "config.toml"
    res = connect.merge_toml_table(path, "mcp_servers", ENTRY)
    assert res.changed and res.backup is None
    assert tomllib.loads(path.read_text())["mcp_servers"]["mannerism"] == ENTRY


def test_merge_toml_is_idempotent(tmp_path):
    path = tmp_path / "config.toml"
    r1 = connect.merge_toml_table(path, "mcp_servers", ENTRY)
    assert r1.changed
    text_after_first = path.read_text()
    r2 = connect.merge_toml_table(path, "mcp_servers", ENTRY)
    assert not r2.changed and r2.backup is None
    assert path.read_text() == text_after_first  # no second append


def test_merge_toml_refuses_unparseable_toml(tmp_path):
    path = tmp_path / "config.toml"
    original = "not = [valid toml\n"
    path.write_text(original)
    with pytest.raises(connect.ConnectError, match="does not parse as TOML"):
        connect.merge_toml_table(path, "mcp_servers", ENTRY)
    assert path.read_text() == original


def test_toml_configured_reports_unknown_for_a_missing_or_bad_file(tmp_path):
    path = tmp_path / "config.toml"
    assert connect.toml_configured(path, "mcp_servers") is None
    path.write_text("not = [valid\n")
    assert connect.toml_configured(path, "mcp_servers") is None
    path.write_text('[mcp_servers.other]\ncommand = "foo"\n')
    assert connect.toml_configured(path, "mcp_servers") is False


# --- YAML: never written -----------------------------------------------------------------------------------------

def test_yaml_harnesses_are_never_written(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    h = connect.HARNESSES["goose"]
    report = connect.do_write(h, project_dir=None, command=None)
    assert report["mcp"][0]["written"] is False
    assert "stdlib writer" in report["mcp"][0]["reason"]
    assert not (tmp_path / ".config" / "goose" / "config.yaml").exists()


# --- skill copying -------------------------------------------------------------------------------------------------

def test_write_skill_copies_the_bundled_file(tmp_path):
    res = connect.write_skill(tmp_path / "skills")
    assert res.changed
    text = (tmp_path / "skills" / "mannerism" / "SKILL.md").read_text()
    assert text.startswith("---\nname: mannerism")


def test_write_skill_never_overwrites_an_existing_copy(tmp_path):
    dest = tmp_path / "skills" / "mannerism" / "SKILL.md"
    dest.parent.mkdir(parents=True)
    dest.write_text("a user's local edit")
    res = connect.write_skill(tmp_path / "skills")
    assert not res.changed
    assert dest.read_text() == "a user's local edit"


def test_skill_source_resolves_to_a_real_file():
    p = connect.skill_source()
    assert p.is_file() and p.name == "SKILL.md"


# --- detection -----------------------------------------------------------------------------------------------------

def test_looks_installed_by_binary(monkeypatch):
    h = connect.Harness(name="x", label="X", docs_url="https://example.com", mcp=None,
                        skill=connect.SkillTarget("none"), hooks=False, binaries=("some-fake-binary-xyz",))
    monkeypatch.setattr(connect.shutil, "which", lambda b: None)
    assert not connect.looks_installed(h)
    monkeypatch.setattr(connect.shutil, "which", lambda b: f"/usr/bin/{b}")
    assert connect.looks_installed(h)


def test_looks_installed_by_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    h = connect.Harness(name="x", label="X", docs_url="https://example.com", mcp=None,
                        skill=connect.SkillTarget("none"), hooks=False, config_dirs=("~/.fakeharness",))
    assert not connect.looks_installed(h)
    (tmp_path / ".fakeharness").mkdir()
    assert connect.looks_installed(h)


# --- manifest: every harness is well-formed -------------------------------------------------------------------------

def test_every_harness_has_a_docs_url_and_label():
    assert len(connect.HARNESSES) >= 10
    for name, h in connect.HARNESSES.items():
        assert h.name == name
        assert h.label
        assert h.docs_url.startswith("https://"), name
        if h.mcp is not None:
            assert h.mcp.format in ("json", "toml", "yaml")
            assert h.mcp.key
        assert h.skill.kind in ("skill_dir", "rules_file", "none")


def test_entry_for_every_harness_builds_without_error():
    for h in connect.HARNESSES.values():
        if h.mcp is None:
            continue
        entry = connect.entry_for(h, None)
        assert isinstance(entry, dict) and entry


# --- CLI -------------------------------------------------------------------------------------------------------------

def test_connect_list_table_and_json(iso, capsys):
    code, out, _ = run(capsys, "connect")
    assert code == 0 and "cursor" in out and "codex" in out
    code, out, _ = run(capsys, "connect", "--json")
    rows = json.loads(out)
    names = {r["name"] for r in rows}
    assert {"cursor", "codex", "claude-code"} <= names


def test_connect_unknown_harness_is_a_friendly_error(iso, capsys):
    code, out, err = run(capsys, "connect", "not-a-real-harness")
    assert code == 2 and "unknown harness" in err and "Traceback" not in err and out == ""


def test_connect_print_snippet_shows_path_and_json(iso, capsys):
    code, out, _ = run(capsys, "connect", "cursor")
    assert code == 0
    assert ".cursor/mcp.json" in out and '"mcpServers"' in out and "uvx" in out


def test_connect_write_json_harness_then_idempotent_rerun(iso, capsys):
    home = Path(os.environ["HOME"])
    cursor_dir = home / ".cursor"
    cursor_dir.mkdir(parents=True)
    (cursor_dir / "mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "foo"}}}))
    code, out, _ = run(capsys, "connect", "cursor", "--write")
    assert code == 0 and "wrote" in out
    cfg = json.loads((cursor_dir / "mcp.json").read_text())
    assert cfg["mcpServers"]["mannerism"]["command"] == "uvx"
    assert cfg["mcpServers"]["other"] == {"command": "foo"}  # preserved
    code, out, _ = run(capsys, "connect", "cursor", "--write")
    assert code == 0 and "already configured" in out
    assert len(list(cursor_dir.glob("mcp.json.bak-*"))) == 1  # only the first write backed up


def test_connect_write_toml_harness(iso, capsys):
    home = Path(os.environ["HOME"])
    code, out, _ = run(capsys, "connect", "codex", "--write")
    assert code == 0 and "wrote" in out
    text = (home / ".codex" / "config.toml").read_text()
    assert "[mcp_servers.mannerism]" in text


def test_connect_write_yaml_harness_prints_instead_of_writing(iso, capsys):
    home = Path(os.environ["HOME"])
    code, out, _ = run(capsys, "connect", "goose", "--write")
    assert code == 0 and "no stdlib writer" in out
    assert not (home / ".config" / "goose" / "config.yaml").exists()


def test_connect_write_copies_skill_for_claude_code(iso, capsys):
    home = Path(os.environ["HOME"])
    code, out, _ = run(capsys, "connect", "claude-code", "--write")
    assert code == 0
    skill = home / ".claude" / "skills" / "mannerism" / "SKILL.md"
    assert skill.is_file()


def test_connect_rules_file_harness_is_never_written(iso, tmp_path, capsys):
    code, out, _ = run(capsys, "connect", "kiro", "--write", "--project", str(tmp_path))
    assert code == 0
    assert not (tmp_path / ".kiro" / "steering" / "mannerism.md").exists()
    assert "paste this in yourself" in out


def test_connect_command_override_replaces_uvx(iso, capsys):
    home = Path(os.environ["HOME"])
    code, out, _ = run(capsys, "connect", "cursor", "--write", "--command", "/opt/bin/mannerism")
    assert code == 0
    cfg = json.loads((home / ".cursor" / "mcp.json").read_text())
    entry = cfg["mcpServers"]["mannerism"]
    assert entry["command"] == "/opt/bin/mannerism" and entry["args"] == ["mcp"]


def test_connect_project_flag_also_writes_the_project_skill_dir(iso, tmp_path, capsys):
    home = Path(os.environ["HOME"])
    proj = tmp_path / "myproject"
    proj.mkdir()
    code, out, _ = run(capsys, "connect", "claude-code", "--write", "--project", str(proj))
    assert code == 0
    assert (proj / ".claude" / "skills" / "mannerism" / "SKILL.md").is_file()
    assert (home / ".claude" / "skills" / "mannerism" / "SKILL.md").is_file()  # user-level still written too


# --- Omnigent: print-only YAML agent-spec snippet, no fixed config file ----------------------------------------------

def test_omnigent_snippet_is_an_inline_mcp_tool_entry():
    h = connect.HARNESSES["omnigent"]
    assert h.mcp.format == "yaml" and h.mcp.user_path is None and h.mcp.project_rel is None
    assert connect.render_snippet(h, None) == (
        "tools:\n  mannerism:\n    command: uvx\n    args:\n    - mannerism\n    - mcp\n    type: mcp\n"
    )


def test_omnigent_snippet_parses_as_yaml_when_pyyaml_is_available():
    yaml = pytest.importorskip("yaml")
    body = yaml.safe_load(connect.render_snippet(connect.HARNESSES["omnigent"], None))
    assert body == {"tools": {"mannerism": {"type": "mcp", "command": "uvx", "args": ["mannerism", "mcp"]}}}


def test_omnigent_write_prints_instead_of_writing_but_copies_the_skill(iso, capsys):
    home = Path(os.environ["HOME"])
    code, out, _ = run(capsys, "connect", "omnigent", "--write")
    assert code == 0
    assert "no stdlib writer" in out and "tools:" in out and "type: mcp" in out
    assert not (home / ".omnigent").exists()  # never creates or touches Omnigent's own directory
    assert (home / ".agents" / "skills" / "mannerism" / "SKILL.md").is_file()
    assert list(home.rglob("*.yaml")) == [] and list(home.rglob("*.bak-*")) == []


def test_omnigent_info_says_where_to_paste_and_is_not_writable(iso, capsys):
    code, out, _ = run(capsys, "connect", "omnigent")
    assert code == 0 and "paste into:" in out and "print-only" in out and "agent YAML" in out
    data = connect.info(connect.HARNESSES["omnigent"])
    assert data["mcp"]["writable"] is False and data["mcp"]["targets"] == []
    assert data["tested"] is False


def test_omnigent_command_override(iso, capsys):
    code, out, _ = run(capsys, "connect", "omnigent", "--command", "/opt/bin/mannerism")
    assert code == 0 and "command: /opt/bin/mannerism" in out and "- mcp" in out
