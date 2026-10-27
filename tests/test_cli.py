from __future__ import annotations

import json

import pytest
from conftest import ingest, unit

from mannerism import __version__, capture, cli, store

TELLS = ("Let's delve into the rich tapestry of innovation — it's not just a tool, but a revolution. "
         "Furthermore, it is crucial to leverage synergy.")


def run(capsys, *argv, stdin=None, monkeypatch=None):
    if stdin is not None:
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = cli.main(list(argv))
    cap = capsys.readouterr()
    return code, cap.out, cap.err


@pytest.fixture
def populated(iso):
    con = store.connect()
    ingest(con, [unit(f"message {i} with some varied words {'x' * (i % 9)}", "work", ref=f"a{i}", days=i + 1,
                      thread_id=f"t{i}") for i in range(40)])
    con.close()


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.strip() == f"mannerism {__version__}"


def test_no_command_prints_help(capsys):
    code, out, _ = run(capsys)
    assert code == 0 and "init" in out and "forget" in out


def test_init_on_an_empty_machine_is_friendly(iso, capsys):
    code, out, err = run(capsys, "init")
    assert code == 0
    assert "No writing was found" in out and "sources add" in out and "claude_code" in out and "Traceback" not in out + err
    code, out, _ = run(capsys, "status")
    assert code == 0 and "nothing read yet" in out
    code, out, _ = run(capsys, "status", "--json")
    assert json.loads(out)["total_units"] == 0


def test_sources_enable_disable_add(iso, tmp_path, capsys):
    code, out, _ = run(capsys, "sources")
    assert code == 0 and "whatsapp" in out
    export = tmp_path / "chat.txt"
    export.write_text("x")
    assert run(capsys, "sources", "add", "whatsapp", str(export))[0] == 0
    from mannerism import config
    cfg = config.load_config()
    assert cfg.sources["whatsapp"]["enabled"] and cfg.sources["whatsapp"]["paths"] == [str(export.resolve())]
    run(capsys, "sources", "add", "whatsapp", str(export))
    assert len(config.load_config().sources["whatsapp"]["paths"]) == 1  # no duplicates
    assert run(capsys, "sources", "disable", "whatsapp")[0] == 0
    assert not config.load_config().sources["whatsapp"]["enabled"]
    assert run(capsys, "sources", "enable", "whatsapp")[0] == 0
    assert config.load_config().sources["whatsapp"]["enabled"]
    code, _, err = run(capsys, "sources", "enable", "nope")
    assert code == 2 and "unknown source" in err and "Traceback" not in err
    code, _, err = run(capsys, "sources", "add", "whatsapp", str(tmp_path / "missing.txt"))
    assert code == 2 and "path not found" in err
    json_out = run(capsys, "sources", "list", "--json")[1]
    assert {s["name"] for s in json.loads(json_out)} >= {"claude_code", "whatsapp"}


def test_lint_exit_codes_and_input_forms(iso, tmp_path, capsys, monkeypatch):
    code, out, _ = run(capsys, "lint", "--register", "close", "-", stdin="ok sounds good, see you at five",
                       monkeypatch=monkeypatch)
    assert code == 0 and "no suggestions" in out
    f = tmp_path / "draft.txt"
    f.write_text(TELLS)
    code, out, _ = run(capsys, "lint", "--register", "work", "--no-log", str(f))
    assert code == 1 and "delve" in out
    con = store.connect()
    n_before = con.execute("SELECT count(*) FROM drafts").fetchone()[0]
    con.close()
    assert n_before == 1  # the first (logged) lint only; --no-log added nothing
    code, out, _ = run(capsys, "lint", "--register", "work", "--json", str(f))
    assert code == 1 and json.loads(out)["findings"]


def test_lint_errors_are_friendly(iso, tmp_path, capsys, monkeypatch):
    f = tmp_path / "d.txt"
    f.write_text("hello there my friend")
    for argv, needle in ((["lint", str(f)], "--register"), (["lint", "--register", "bogus", str(f)], "unknown register"),
                         (["lint", "--register", "work", str(tmp_path / "none.txt")], "file not found")):
        code, out, err = run(capsys, *argv)
        assert code == 2 and needle in err and "Traceback" not in err and out == ""
    f.write_text("  \n")
    assert run(capsys, "lint", "--register", "work", str(f))[0] == 2


def test_context_and_status(populated, capsys):
    code, out, err = run(capsys, "context", "work", "--task", "ship it", "--k", "3")
    assert code == 0 and '<voice register="work">' in out and "data for style" in out
    code, out, _ = run(capsys, "context", "work", "--json")
    assert json.loads(out)["register"] == "work"
    assert run(capsys, "context", "bogus")[0] == 2
    code, out, _ = run(capsys, "status")
    assert code == 0 and "stats card ready" in out


def test_feedback_work_rules_and_blind(populated, tmp_path, capsys, monkeypatch):
    d, fin = tmp_path / "d.txt", tmp_path / "f.txt"
    d.write_text("Hi there, I hope you are well. Please review the plan when you can, thanks so much")
    fin.write_text("please review the plan when you can")
    code, out, _ = run(capsys, "feedback", "--register", "work", str(d), str(fin))
    assert code == 0 and "edit recorded" in out
    assert run(capsys, "feedback", "--register", "work", str(d), str(tmp_path / "nope"))[0] == 2

    code, out, _ = run(capsys, "work", "next")
    assert code == 0 and "packet" in json.loads(out)
    code, out, err = run(capsys, "work", "submit", "label", "pk_nope", "-", stdin='{"labels": []}', monkeypatch=monkeypatch)
    assert code == 2 and "unknown packet" in err
    code, _, err = run(capsys, "work", "submit", "label", "pk_x", "-", stdin="{bad", monkeypatch=monkeypatch)
    assert code == 2 and "not valid JSON" in err

    code, out, _ = run(capsys, "rules")
    assert code == 0 and "no rules yet" in out
    con = store.connect()
    con.execute("INSERT INTO rules(register, text, support, contradicted, created_at, last_seen, edit_ids_json) "
                "VALUES ('work', 'drops greetings', 3, 0, 'x', 'x', '[]')")
    rid = con.execute("SELECT rule_id FROM rules").fetchone()[0]
    con.close()
    assert "drops greetings" in run(capsys, "rules", "--register", "work")[1]
    assert run(capsys, "rules", "rm", str(rid))[0] == 0
    code, _, err = run(capsys, "rules", "rm", str(rid))
    assert code == 2 and "no rule" in err

    code, _, err = run(capsys, "blind", "score", "nonsense")
    assert code == 2 and "result code" in err and "Traceback" not in err


def test_forget_confirms_unless_yes(populated, capsys, monkeypatch):
    con = store.connect()
    capture.log_draft("a draft that should vanish when forgotten, please", "work", con=con)
    con.close()
    monkeypatch.setattr("builtins.input", lambda *_: "no")
    code, _, err = run(capsys, "forget", "--drafts")
    assert code == 2 and "Nothing deleted" in err
    con = store.connect()
    assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 1
    con.close()
    monkeypatch.setattr("builtins.input", lambda *_: "yes")
    code, out, _ = run(capsys, "forget", "--drafts")
    assert code == 0 and "Deleted" in out
    code, out, _ = run(capsys, "forget", "--all", "--yes")
    assert code == 0 and "outside local deletion" in out


def test_forget_requires_a_scope():
    with pytest.raises(SystemExit) as e:
        cli.main(["forget"])
    assert e.value.code == 2


def test_forget_eof_aborts(populated, capsys, monkeypatch):
    def eof(*_):
        raise EOFError
    monkeypatch.setattr("builtins.input", eof)
    assert run(capsys, "forget", "--all")[0] == 2


def test_sync_background_and_quick(iso, capsys, monkeypatch):
    from mannerism import sync
    # A scoped patch: monkeypatch.undo() would also undo the isolation fixtures and run against the real HOME.
    with monkeypatch.context() as m:
        m.setattr(sync, "sync_in_background", lambda **k: {"started": True, "pid": 1})
        assert "background" in run(capsys, "sync", "--background")[1]
    code, out, _ = run(capsys, "sync", "--quick")
    assert code == 0 and "new texts" in out
