"""`mannerism scan`: grouped counts, machine-typed shares, samples, the exclude commands it suggests, and storing nothing."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from transcripts import world

from mannerism import api, cli, config, groups, scan, store, sync


@pytest.fixture
def machine(_isolated_env, monkeypatch):
    """The synthetic machine of tests/transcripts.py under the fake HOME; the second Claude account via [claude_code].dirs."""
    paths = world(_isolated_env)
    cfg = config.load_config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": [], "dirs": [str(paths["b"])]}
    cfg.sources["codex"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    return paths


def snapshot(root: Path) -> dict[str, str]:
    """Every file under `root` with a digest of its bytes (and directories), to prove nothing changed."""
    out = {}
    if not root.exists():
        return {"<absent>": ""}
    for p in sorted(root.rglob("*")):
        out[str(p.relative_to(root))] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<dir>"
    return out


def by_project(res, source):
    return {(g["account"], g["project"]): g for g in res["groups"] if g["source"] == source}


def test_scan_groups_by_source_account_project_and_kind(machine, _isolated_env):
    res = scan.scan(samples=2)
    cc = by_project(res, "claude_code")
    app = cc[("~/.claude", "app")]
    assert app["texts"] == 12 and app["sessions"] == 6 and app["session_kind"] == "interactive"
    assert app["group_id"] == groups.origin_group_id("claude_code", "~/.claude", "app", "interactive")
    assert app["first"] == "2026-05-01" and app["held"] == 0 and app["filtered"] == 0 and app["flagged_share"] == 0
    assert cc[("~/.claude", "site")]["texts"] == 9
    assert sum(g["texts"] for g in cc.values() if g["account"] == "~/.claude-work") == 28 - sum(
        g["filtered"] for g in cc.values())
    cx = by_project(res, "codex")
    assert cx[("~/.codex", "app")]["texts"] == 4  # the headless "exec" session is not read, as in a sync
    assert res["sources"]["codex"]["status"] == "ok"


def test_the_orchestrator_is_flagged_and_the_person_is_not(machine):
    res = scan.scan()
    cc = by_project(res, "claude_code")
    orch = [g for k, g in cc.items() if k[0] == "~/.claude-work"]
    flagged = sum(g["held"] + g["filtered"] for g in orch)
    assert flagged >= 24  # the 14 repeated tickets and most status blocks
    assert all(g["held"] + g["filtered"] == 0 for k, g in cc.items() if k[0] == "~/.claude")
    assert res["totals"]["held"] + res["totals"]["filtered"] == flagged
    tmpl = [t for t in res["templates"] if t["sessions"] == 14]
    assert tmpl and tmpl[0]["group_id"].startswith("t_") and tmpl[0]["texts"] == 14


def test_suggestions_are_copyable_commands_for_the_noisiest_groups(machine):
    res = scan.scan()
    sugg = res["suggestions"]
    cmds = [s["command"] for s in sugg]
    assert cmds[0] == "mannerism exclude add --source claude_code --account '~/.claude-work'"  # the whole orchestrator
    assert all(c.startswith("mannerism exclude add --") for c in cmds)
    assert "claude_code ~/.claude-work" in sugg[0]["why"] and "machine-typed" in sugg[0]["why"]
    group_cmds = {c.rsplit(" ", 1)[1] for c in cmds if "--group" in c}
    template_ids = {t["group_id"] for t in res["templates"]}
    assert group_cmds <= template_ids
    assert not any("~/.claude'" in c or "--account '~/.claude'" in c for c in cmds)  # the person's own account is never suggested
    text = scan.format_text(res)
    assert "mannerism init" in text and "mannerism sync" in text and cmds[0] in text and "Nothing was stored" in text


def test_a_suggested_command_excludes_those_texts_after_a_sync(machine):
    cmd = scan.scan()["suggestions"][0]["command"]
    assert cli.main(["sync"]) == 0
    con = store.connect()
    before = con.execute("SELECT count(*) FROM units").fetchone()[0]
    con.close()
    import shlex
    assert cli.main(shlex.split(cmd)[1:] + ["--yes"]) == 0
    after = scan.scan()
    assert "~/.claude-work" not in {g["account"] for g in after["groups"]}
    con = store.connect()
    left = con.execute("SELECT count(*) FROM units").fetchone()[0]
    assert left < before and con.execute("SELECT count(*) FROM units WHERE account='~/.claude-work'").fetchone()[0] == 0
    assert cli.main(["sync"]) == 0
    assert store.connect().execute("SELECT count(*) FROM units").fetchone()[0] == left  # stays out


def test_samples_are_masked_bounded_and_deterministic(machine):
    res = scan.scan(samples=2)
    for g in res["groups"]:
        assert len(g["samples"]) <= 2 and all(len(s) <= groups.SAMPLE_CHARS for s in g["samples"])
    assert "jo.dev@example.org" not in json.dumps(res)
    assert scan.scan(samples=2)["groups"] == res["groups"]
    assert all(g["samples"] == [] for g in scan.scan(samples=0)["groups"])
    assert max(len(g["samples"]) for g in scan.scan(samples=scan.MAX_SAMPLES)["groups"]) <= 20


def test_scan_stores_nothing_and_leaves_the_data_directory_byte_identical(machine, _isolated_env):
    data = _isolated_env / ".mannerism"
    # 1. only a config file in the data directory
    assert sorted(p.name for p in data.iterdir()) == ["config.toml"]
    first = snapshot(data)
    scan.scan()
    assert snapshot(data) == first
    # 2. a populated data directory (store, config, cursors), including a stored rule and a decision
    sync.sync()
    api.exclude_add(project="site")
    con = store.connect()
    con.execute("INSERT INTO group_decisions(group_id, decision, decided_at) VALUES ('g_000000000000','keep','2026-01-01')")
    con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    con.close()
    before = snapshot(data)
    claude_before = snapshot(_isolated_env / ".claude")
    res = scan.scan(samples=3)
    assert res["ok"] and res["totals"]["texts"] > 0
    assert snapshot(data) == before
    assert snapshot(_isolated_env / ".claude") == claude_before
    # 3. through the CLI, text and JSON
    assert cli.main(["scan", "--json"]) == 0
    assert cli.main(["scan"]) == 0
    assert snapshot(data) == before


def test_scan_applies_the_same_exclusion_rules_as_a_sync(machine):
    cfg = config.load_config()
    cfg.exclude = [{"project": "site"}]
    config.save_config(cfg)
    api.exclude_add(account="~/.claude-work")
    res = scan.scan()
    projects = {(g["account"], g["project"]) for g in res["groups"] if g["source"] == "claude_code"}
    assert projects == {("~/.claude", "app")}
    assert res["totals"]["excluded"] == 9 + 28


def test_scan_respects_a_confirmed_group(machine):
    first = scan.scan()
    t = first["templates"][0]
    assert t["held"] + t["filtered"] > 0
    con = store.connect()
    con.execute("INSERT INTO group_decisions(group_id, decision, decided_at) VALUES (?, 'keep', '2026-01-01')",
                (t["group_id"],))
    con.close()
    again = scan.scan()
    t2 = next(x for x in again["templates"] if x["group_id"] == t["group_id"])
    assert t2["held"] == 0  # held texts of a confirmed group are normal again (dropped ones stay dropped)


def test_network_sources_are_skipped_unless_asked_and_consented(machine, monkeypatch):
    from mannerism import adapters

    class Net:
        tier = "auto"
        network = True
        calls = 0

        def discover(self, me, c):
            Net.calls += 1
            return []

        def read(self, loc, cur, me):
            return [], {}

    monkeypatch.setattr(adapters, "REGISTRY", {**adapters.REGISTRY, "fakenet": "fake:fakenet"})
    real = adapters.load
    monkeypatch.setattr(adapters, "load", lambda n: Net() if n == "fakenet" else real(n))
    res = scan.scan(sources=["fakenet"])
    assert res["sources"]["fakenet"]["status"] == "skipped_network" and Net.calls == 0
    res = scan.scan(sources=["fakenet"], network=True)
    assert res["sources"]["fakenet"]["status"] == "skipped_no_consent" and Net.calls == 0


def test_a_machine_with_no_history_scans_cleanly(_isolated_env):
    res = scan.scan()
    assert res["ok"] and res["totals"]["texts"] == 0 and res["groups"] == []
    assert "Scanned 0 texts" in scan.format_text(res)
    assert not (_isolated_env / ".mannerism").exists()


def test_time_budget_is_reported(machine):
    res = scan.scan(budget_s=0)
    assert res["budget_exhausted"] is True and res["totals"]["texts"] == 0
    assert "time budget" in scan.format_text(res)


def test_text_output_is_per_source_account_project(machine):
    text = scan.format_text(scan.scan(samples=1))
    assert "claude_code:" in text and "account ~/.claude-work" in text and "account ~/.claude\n" in text
    assert "project app" in text and "[g_" in text and "% machine-typed" in text
    assert "codex:" in text


def test_the_cli_scan_flags(machine, capsys):
    assert cli.main(["scan", "--source", "codex", "--json", "--samples", "1"]) == 0
    res = json.loads(capsys.readouterr().out)
    assert set(res["sources"]) == {"codex"} and all(len(g["samples"]) <= 1 for g in res["groups"])
    assert cli.main(["scan", "--samples", "99"]) == 2
    assert "between 0 and 20" in capsys.readouterr().err


def test_scan_and_sync_agree_on_what_is_machine_typed(machine):
    """A sync drops only what is clear when the text arrives and holds the rest once later sessions confirm the
    repetition, so what it does not keep as normal text is the same set a scan flags."""
    res = scan.scan()
    synced = sync.sync()
    con = store.connect()
    held_now = con.execute("SELECT count(*) FROM units WHERE held=1").fetchone()[0]
    assert held_now + synced["filtered"] == res["totals"]["held"] + res["totals"]["filtered"]
    assert api.status()["held"] == held_now
    assert con.execute("SELECT count(*) FROM units").fetchone()[0] == res["totals"]["texts"] + res["totals"]["filtered"] - synced["filtered"]
    assert sync.sync()["new_units"] == 0 and sync.sync()["held"] == 0
