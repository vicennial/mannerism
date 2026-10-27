"""The exclusion, forget --where, scan and review commands."""
from __future__ import annotations

import json

import pytest
from conftest import ingest
from test_exclude import mku
from transcripts import world

from mannerism import cli, config, groups, store, sync


def run(capsys, *argv):
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.fixture
def populated(iso):
    con = store.connect()
    ingest(con, [mku(i, f"note {i} about the {'plan' * (i % 3 + 1)} for the roadmap this week", project="app" if i < 6 else "scratch-x")
                 for i in range(10)])
    con.close()


def test_exclude_add_confirms_then_deletes_and_lists(populated, capsys, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_: "no")
    code, _, err = run(capsys, "exclude", "add", "--project", "scratch-*")
    assert code == 2 and "Nothing deleted" in err and "4 stored texts" in err
    assert store.connect().execute("SELECT count(*) FROM units").fetchone()[0] == 10
    assert store.connect().execute("SELECT count(*) FROM exclusions").fetchone()[0] == 0
    code, out, _ = run(capsys, "exclude", "add", "--project", "scratch-*", "--yes")
    assert code == 0 and "Rule 1 added" in out and "Removed 4 stored texts (agentx: 4)" in out
    code, out, _ = run(capsys, "exclude", "list")
    assert code == 0 and "project='scratch-*'" in out and "store" in out
    code, out, _ = run(capsys, "exclude", "rm", "1")
    assert code == 0 and "rule 1 removed" in out
    assert "no exclusion rules" in run(capsys, "exclude")[1]
    assert run(capsys, "exclude", "rm", "9")[0] == 2


def test_exclude_add_dry_run_changes_nothing(populated, capsys):
    code, out, _ = run(capsys, "exclude", "add", "--project", "app", "--dry-run")
    assert code == 0 and "Would remove 6 stored texts (agentx: 6)" in out and "Nothing changed" in out
    assert store.connect().execute("SELECT count(*) FROM units").fetchone()[0] == 10
    assert store.connect().execute("SELECT count(*) FROM exclusions").fetchone()[0] == 0
    code, out, _ = run(capsys, "exclude", "add", "--project", "app", "--dry-run", "--json")
    assert json.loads(out)["would_remove"] == 6


def test_exclude_add_needs_a_field_and_rejects_bad_ones(populated, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["exclude", "add"])
    assert e.value.code == 2
    code, _, err = run(capsys, "exclude", "add", "--text", "(a+)+$", "--yes")
    assert code == 2 and "nested quantifiers" in err
    code, _, err = run(capsys, "exclude", "add", "--group", "nope", "--yes")
    assert code == 2 and "not a group id" in err


def test_forget_where(populated, capsys, monkeypatch):
    code, out, _ = run(capsys, "forget", "--where", "--project", "scratch-*", "--dry-run")
    assert code == 0 and "Would remove 4" in out
    monkeypatch.setattr("builtins.input", lambda *_: "yes")
    code, out, _ = run(capsys, "forget", "--where", "--project", "scratch-*")
    assert code == 0 and "Removed 4 stored texts" in out and "outside local deletion" in out
    assert store.connect().execute("SELECT count(*) FROM exclusions").fetchone()[0] == 0  # no rule kept
    assert store.connect().execute("SELECT count(*) FROM units").fetchone()[0] == 6
    code, out, _ = run(capsys, "forget", "--where", "--source", "agentx", "--kind", "ide", "--yes")
    assert code == 0 and "Removed 0" in out


def test_forget_flag_combinations(populated, capsys):
    for argv in (["forget", "--where"], ["forget", "--where", "--all", "--project", "x"],
                 ["forget", "--project", "x"], ["forget", "--source", "a", "--all"], ["forget"],
                 ["forget", "--source", "a", "--dry-run"]):
        with pytest.raises(SystemExit) as e:
            cli.main(argv)
        assert e.value.code == 2, argv


def test_forget_source_still_works_as_before(populated, capsys):
    code, out, _ = run(capsys, "forget", "--source", "agentx", "--yes")
    assert code == 0 and "units: 10" in out
    assert store.connect().execute("SELECT count(*) FROM units").fetchone()[0] == 0


def test_review_lists_held_groups_keep_and_the_not_me_command(_isolated_env, capsys):
    paths = world(_isolated_env)
    cfg = config.load_config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": [], "dirs": [str(paths["b"])]}
    config.save_config(cfg)
    code, out, _ = run(capsys, "review")
    assert code == 0 and "nothing is held" in out
    run(capsys, "sync")
    code, out, _ = run(capsys, "status")
    assert "held back until you confirm" in out
    code, out, _ = run(capsys, "review")
    assert code == 0 and "texts are held back" in out and "mannerism review keep t_" in out
    assert "mannerism exclude add --group t_" in out
    code, data, _ = run(capsys, "review", "--json")
    held = next(g for g in json.loads(data)["groups"] if g["kind"] == "template")
    code, out, _ = run(capsys, "review", "keep", held["group_id"])
    assert code == 0 and "14 texts" in out
    assert run(capsys, "review", "keep", "junk")[0] == 2
    code, out, _ = run(capsys, "review", "rescan")
    assert code == 0 and "scored 0" in out


def test_sync_output_mentions_what_was_held_or_excluded(_isolated_env, capsys):
    paths = world(_isolated_env)
    cfg = config.load_config()
    cfg.sources["claude_code"] = {"enabled": True, "paths": [], "dirs": [str(paths["b"])]}
    cfg.exclude = [{"project": "site"}, {"bogus": 1}]
    config.save_config(cfg)
    code, out, err = run(capsys, "sync")
    assert code == 0 and "excluded by your rules" in out and "held for review" in out
    assert "config.toml [[exclude]] #2" in err
    assert groups.kept_groups(store.connect()) == set()
    assert sync.sync()["new_units"] == 0
