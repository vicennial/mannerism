import json
import shutil
import subprocess
from pathlib import Path

from mannerism.adapters import aider as aider_mod
from mannerism.adapters.aider import HISTORY_FILE, Aider

from .conftest import run, write_jsonl

FIXTURE = Path(__file__).parent / "fixtures" / "aider" / "real-0.86.2.chat.history.md"


def user(*lines):
    """A prompt as aider logs it: a blank line, then one `#### ` line per prompt line."""
    return "\n" + "".join(f"#### {ln}  \n" for ln in lines)


def history(*parts):
    return "".join(parts)


SESSION = "\n# aider chat started at 2026-03-01 10:00:00\n\n> Aider v0.86.2  \n> Model: x with whole edit format  \n\n"
REPLY = "\nWhich file should I change? Mail bot@example.org\n\n> Tokens: 20 sent, 20 received.  \n\n"


def repo_file(tmp_path, text, name="proj"):
    repo = tmp_path / name
    repo.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    f = repo / HISTORY_FILE
    f.write_text(text, "utf-8")
    return f


def test_real_run(home, me, tmp_path):
    """Written by aider 0.86.2 against a mock server: /add, /ask, a multi-line prompt, /run and !shell."""
    f = repo_file(tmp_path, "")
    shutil.copy(FIXTURE, f)
    src = Aider()
    locs = src.discover(me, {"paths": [str(tmp_path)]})
    assert [lc.path for lc in locs] == [f]
    units, cur = run(src, locs[0], None, me)
    assert [u.text for u in units] == [
        "please rename greet to welcome, keep the string",
        "why is it returning a string?",  # /ask logs the command, then the question it passes on
        "and add a docstring",
        "thanks, that is all",
        "first line of a long prompt,\n\nthen a line after a blank one",
        "last one: thanks",
    ]
    assert all((u.kind, u.audience, u.register) == ("prompt", "agent", "agent") for u in units)
    assert units[0].ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") == "2026-10-21 21:26:14"
    assert len({u.ts for u in units[:4]}) == 1 and units[4].ts > units[0].ts  # one time per session
    assert units[0].context == () and "nothing to change" in units[1].context[0].text
    assert len({u.unit_id for u in units}) == 6 and cur["offset"] == f.stat().st_size and cur["n"] == 5


def test_prompt_blocks_and_noise(home, me, tmp_path):
    text = history(
        SESSION,
        user("please rename foo to bar, mail jo.dev@example.org"),
        REPLY,
        "#### Heading the model wrote\n",  # a reply heading never ends in two spaces
        "# A markdown title in a reply\n\n",
        user("/add src/app.py"),
        "> Added src/app.py to the chat  \n",
        user("<blank>"),
        user("!ls -la"),
        user("Add src/app.py to the chat? (Y)es/(N)o [Yes]: all"),
        user("line one", "", "line three"),
        "\nDone.\n\n",
        user("https://example.org/page"),
        user("/clear"),
        user("last real prompt"),
    )
    f = repo_file(tmp_path, text)
    src = Aider()
    units, cur = run(src, src.discover(me, {"paths": [str(f)]})[0], None, me)
    assert [u.text for u in units] == [
        "please rename foo to bar, mail [email]",
        "line one\n\nline three",
        "last real prompt",
    ]
    assert units[0].context == ()
    ctx = units[1].context[0].text
    assert "Which file should I change? Mail [email]" in ctx and "Heading the model wrote" in ctx
    assert "markdown title" not in ctx and "Tokens" not in ctx
    assert units[2].context == ()  # "Done." answered the pure-URL prompt, which was dropped
    assert units[0].meta["repo_hash"] and str(tmp_path) not in str(units[0].meta) + str(units[0].thread_id)


def test_sessions_use_their_own_header_time_and_crlf(home, me, tmp_path):
    text = history(SESSION, user("morning prompt"), "\n# aider chat started at 2026-03-02 18:30:05\n\n",
                   user("evening prompt")).replace("\n", "\r\n")
    f = repo_file(tmp_path, "")
    f.write_bytes(text.encode())
    src = Aider()
    units, _ = run(src, src.discover(me, {"paths": [str(f)]})[0], None, me)
    assert [u.text for u in units] == ["morning prompt", "evening prompt"]
    assert units[0].ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") == "2026-03-01 10:00:00"
    assert units[1].ts.astimezone().strftime("%Y-%m-%d %H:%M:%S") == "2026-03-02 18:30:05"
    assert units[1].context == ()  # a new session does not inherit the last reply
    assert units[0].source_ref != units[1].source_ref and units[0].thread_id != units[1].thread_id


def test_incremental_appends_new_sessions_and_truncation(home, me, tmp_path):
    f = repo_file(tmp_path, history(SESSION, user("first prompt here"), REPLY))
    src = Aider()
    loc = src.discover(me, {"paths": [str(f)]})[0]
    units, cur = run(src, loc, None, me)
    assert len(units) == 1 and "[email]" in cur["asst"]
    units, cur2 = run(src, loc, cur, me)
    assert units == [] and cur2["offset"] == cur["offset"]
    with open(f, "a") as fh:
        fh.write("#### second prompt here  \n")  # a prompt written without its trailing reply yet
        fh.write("#### and a second line  ")  # no newline: not a complete line, not read
    units, cur3 = run(src, loc, cur2, me)
    assert [u.text for u in units] == ["second prompt here"] and units[0].context[0].text.startswith("Which file")
    with open(f, "a") as fh:
        fh.write("\n\nok\n" + "\n# aider chat started at 2026-03-03 08:00:00\n\n" + user("third, new session"))
    units, cur4 = run(src, loc, cur3, me)
    assert [u.text for u in units] == ["and a second line", "third, new session"]
    assert cur4["n"] == 1
    f.write_text(history(SESSION, user("brand new content")), "utf-8")
    units, _ = run(src, loc, cur4, me)
    assert [u.text for u in units] == ["brand new content"]


def test_prompt_cut_by_the_read_budget_is_carried(home, me, tmp_path, monkeypatch):
    f = repo_file(tmp_path, history(SESSION, user("one", "two", "mail jo.dev@example.org", "four"), REPLY))
    real = aider_mod.read_new_lines
    monkeypatch.setattr(aider_mod, "read_new_lines", lambda p, c: real(p, c, budget=len(SESSION) + 40))
    src = Aider()
    loc = src.discover(me, {"paths": [str(f)]})[0]
    units, cur = run(src, loc, None, me)
    got = list(units)
    guard = 0
    while cur.get("more") and guard < 20:
        guard += 1
        if cur["block"]:
            assert "@" not in " ".join(cur["block"]) or "[email]" in " ".join(cur["block"])  # masked in the cursor
        units, cur = run(src, loc, cur, me)
        got += units
    assert [u.text for u in got] == ["one\ntwo\nmail [email]\nfour"]


def test_discovery_sources(home, me, tmp_path, monkeypatch):
    src = Aider()
    assert src.discover(me, {}) == []
    (home / HISTORY_FILE).write_text(history(SESSION, user("run from the home folder")))
    code = home / "code" / "proj"
    (code / ".git").mkdir(parents=True)
    (code / HISTORY_FILE).write_text(history(SESSION, user("a repo under ~/code")))
    loose = tmp_path / "scratch"  # no git: aider writes into the current folder
    loose.mkdir()
    (loose / HISTORY_FILE).write_text(history(SESSION, user("outside git")))
    write_jsonl(home / ".claude" / "projects" / "-s" / "s.jsonl",
                [{"type": "user", "cwd": str(loose), "message": {"role": "user", "content": "hi"}}])
    moved = tmp_path / "moved.md"
    moved.write_text(history(SESSION, user("custom file")))
    monkeypatch.setenv("AIDER_CHAT_HISTORY_FILE", str(moved))
    deep = tmp_path / "deep" / "a" / "b"
    deep.mkdir(parents=True)
    (deep / HISTORY_FILE).write_text(history(SESSION, user("two levels down")))
    paths = {lc.path.resolve() for lc in src.discover(me, {"paths": [str(tmp_path / "deep")]})}
    assert paths == {(home / HISTORY_FILE).resolve(), (code / HISTORY_FILE).resolve(), (loose / HISTORY_FILE).resolve(),
                     moved.resolve(), (deep / HISTORY_FILE).resolve()}


def test_discovery_and_read_never_raise(home, me, monkeypatch, tmp_path):
    monkeypatch.setenv("AIDER_CHAT_HISTORY_FILE", "/nonexistent/nowhere/h.md")
    src = Aider()
    assert src.discover(me, {"paths": [123, None, "/nonexistent"]}) == []
    from mannerism.core import Location
    gone = Location("aider", str(tmp_path / "gone.md"), tmp_path / "gone.md")
    units, cur = src.read(gone, {"offset": 5}, me)
    assert list(units) == [] and cur == {"offset": 5}


def read_all(tmp_path, me, text, name="proj"):
    f = repo_file(tmp_path, text, name)
    src = Aider()
    units, cur = run(src, src.discover(me, {"paths": [str(f)]})[0], None, me)
    return f, src, units, cur


PLAN = ("I will rename greet to welcome in hello.py and update both call sites, then add a one line docstring "
        "that says what the function returns.")


def test_architect_plan_logged_as_a_prompt_is_not_typed(home, me, tmp_path):
    """P0-1: ArchitectCoder runs the editor with `with_message=<the plan>`; aider logs it as `#### ` lines."""
    plan_block = "".join(f"#### {ln}  \n" for ln in ["I will rename greet to welcome in hello.py and update both call sites,",
                                                     "", "then add a one line docstring that says what the function returns."])
    text = history(
        SESSION,
        user("please rename greet to welcome"),
        "\n" + PLAN.replace("sites, then", "sites,\n\nthen") + "\n\n> Tokens: 20 sent, 20 received.  \n",
        "\n" + plan_block,
        "\nI made those changes to the files.\n\n> Applied edit to hello.py  \n",
        user("and now add tests"),
    )
    _, _, units, _ = read_all(tmp_path, me, text)
    assert [u.text for u in units] == ["please rename greet to welcome", "and now add tests"]
    assert "I made those changes" in units[1].context[0].text


def test_architect_plan_with_markdown_and_across_reads(home, me, tmp_path):
    """The plan keeps its `# ` headings and `> ` quotes in the reply; and the read may end between reply and plan."""
    reply = "\n# Plan\n\n> keep the public name stable\n\nRename greet to welcome and keep an alias for the old name.\n\n"
    block = "".join(f"#### {ln}  \n" for ln in ["# Plan", "", "> keep the public name stable", "",
                                                "Rename greet to welcome and keep an alias for the old name."])
    f = repo_file(tmp_path, history(SESSION, user("rename it"), reply))
    src = Aider()
    loc = src.discover(me, {"paths": [str(f)]})[0]
    units, cur = run(src, loc, None, me)
    assert [u.text for u in units] == ["rename it"] and cur["reply"]
    with open(f, "a") as fh:
        fh.write("\n" + block + "\nDone.\n")
        fh.write(user("a real follow up"))
    units, _ = run(src, loc, cur, me)
    assert [u.text for u in units] == ["a real follow up"]


def test_short_typed_prompt_that_appears_in_the_reply_is_kept(home, me, tmp_path):
    text = history(SESSION, user("explain it"), "\nSure, I can explain it. Thanks for asking, ok?\n\n", user("thanks"))
    _, _, units, _ = read_all(tmp_path, me, text)
    assert [u.text for u in units] == ["explain it", "thanks"]


def test_lint_and_help_generated_blocks_are_not_typed(home, me, tmp_path):
    lint = "# Fix any errors below, if possible.\n\n## Running: flake8 a.py\n\na.py:1:1: F401 'os' imported but unused"
    helped = "# Question: how do I add files?\n\n# Relevant docs:\n\n<doc>/add adds files</doc>\n\n# Announcement lines from when this session of aider was launched:\n\nAider v0.86"
    text = history(SESSION, user("/lint"), user(*lint.splitlines()), "\nFixed.\n\n", user("/help how do I add files?"),
                   user(*helped.splitlines()), "\nUse /add.\n\n", user("a real question"))
    _, _, units, _ = read_all(tmp_path, me, text)
    assert [u.text for u in units] == ["a real question"]


def test_history_tracked_by_git_is_someone_elses(home, me, tmp_path):
    """P1-2: a cloned repository can ship its author's committed .aider.chat.history.md."""
    repo = home / "code" / "someone-elses-oss"
    repo.mkdir(parents=True)
    f = repo / HISTORY_FILE
    f.write_text(history(SESSION, user("hey, add retry logic to the uploader, my boss wants it today")))
    git = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=Other", "-c", "user.email=other@example.com", "commit", "-qm", "init")
    mine = home / "code" / "mine"
    mine.mkdir()
    (mine / ".git").mkdir()
    (mine / HISTORY_FILE).write_text(history(SESSION, user("my own prompt")))
    src = Aider()
    found = {lc.path.parent.name for lc in src.discover(me, {})}
    assert found == {"mine"}
    # a file the user names themselves is theirs even if it is committed
    assert f in [lc.path for lc in src.discover(me, {"paths": [str(f)]})]


def test_tracked_by_git_never_raises(tmp_path, monkeypatch):
    assert aider_mod.tracked_by_git(tmp_path / "nowhere" / HISTORY_FILE) is None  # git cannot even enter the folder
    (tmp_path / "plain").mkdir()
    assert aider_mod.tracked_by_git(tmp_path / "plain" / HISTORY_FILE) is False  # not in a repository: git answered

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(aider_mod.subprocess, "run", boom)
    assert aider_mod.tracked_by_git(tmp_path / HISTORY_FILE) is None


def test_relayed_text_logged_as_a_prompt_is_dropped(home, me, tmp_path):
    text = history(SESSION, user("<task-notification>build finished</task-notification>"), user("a real prompt"))
    _, _, units, _ = read_all(tmp_path, me, text)
    assert [u.text for u in units] == ["a real prompt"]


def long_reply(letters):
    words = [f"step{i} rename the helper and update call site number {i}." for i in range(letters // 30)]
    return " ".join(words)


def test_architect_plan_longer_than_any_tail_window_is_detected(home, me, tmp_path):
    """Follow-up P2-a: 40,000 letters of plan, in the same read and across two reads."""
    plan = long_reply(40_000)
    block = "".join(f"#### {ln}  \n" for ln in plan.split(". "))
    f = repo_file(tmp_path, history(SESSION, user("do the big refactor"), "\n" + plan + "\n\n> Tokens: 1 sent.  \n"))
    src = Aider()
    loc = src.discover(me, {"paths": [str(f)]})[0]
    units, cur = run(src, loc, None, me)
    assert [u.text for u in units] == ["do the big refactor"]
    assert len(json.dumps(cur)) < 4000  # P2-f: a summary, not the reply
    with open(f, "a") as fh:  # the plan echo arrives in a later read
        fh.write("\n" + block + "\nDone.\n" + user("a real follow up"))
    units, _ = run(src, loc, cur, me)
    assert [u.text for u in units] == ["a real follow up"]


def test_prompt_that_quotes_the_models_suggestion_is_kept(home, me, tmp_path):
    """Follow-up P2-b: a typed prompt that repeats one sentence of the reply is not the plan."""
    reply = ("\nI suggest we split the loader into a reader and a parser, move the retry loop into the client, "
             "add a timeout parameter, and write tests for every branch of the new retry loop. Shall I start?\n\n")
    text = history(SESSION, user("how should we restructure it?"), reply,
                   user("move the retry loop into the client"))
    _, _, units, _ = read_all(tmp_path, me, text)
    assert [u.text for u in units] == ["how should we restructure it?", "move the retry loop into the client"]


def test_reply_kept_between_reads_is_a_short_summary(home, me, tmp_path):
    f = repo_file(tmp_path, history(SESSION, user("explain it"), "\n" + long_reply(20_000) + "\n\n"))
    src = Aider()
    units, cur = run(src, src.discover(me, {"paths": [str(f)]})[0], None, me)
    blob = json.dumps(cur)
    assert len(blob) < 4000 and "step5 rename" not in blob.replace(" ", "") and set(cur["reply"]) >= {"len_main", "prefix"}
    assert len(cur["reply"]["prefix"]) <= 600


def _repo_with_history(home, name="found"):
    d = home / "code" / name
    d.mkdir(parents=True)
    (d / ".git").mkdir()
    (d / HISTORY_FILE).write_text(history(SESSION, user("my prompt")))
    return d


def test_git_that_cannot_answer_fails_closed_for_found_files_only(home, me, monkeypatch):
    """Follow-up P2-g: no git, "dubious ownership", a timeout -> a folder-searched file is skipped; a named one is kept."""
    d = _repo_with_history(home)
    named = home / "named.md"
    named.write_text(history(SESSION, user("named prompt")))
    src = Aider()
    cfg = {"paths": [str(named)]}
    real = subprocess.run

    def fails(kind):
        def run_(*a, **k):
            if kind == "missing":
                raise FileNotFoundError("git")
            if kind == "timeout":
                raise subprocess.TimeoutExpired("git", 5)
            return subprocess.CompletedProcess(a[0], 128, b"", b"fatal: detected dubious ownership in repository at x")
        return run_

    assert {lc.path for lc in src.discover(me, cfg)} == {d / HISTORY_FILE, named}  # git works: both
    for kind in ("missing", "timeout", "dubious"):
        monkeypatch.setattr(aider_mod.subprocess, "run", fails(kind))
        assert [lc.path for lc in src.discover(me, cfg)] == [named], kind
    monkeypatch.setattr(aider_mod.subprocess, "run", real)


def test_git_is_asked_without_an_inherited_git_dir(home, me, monkeypatch, tmp_path):
    seen = {}
    real = subprocess.run

    def spy(cmd, **k):
        seen.update(k["env"])
        return real(cmd, **k)

    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setattr(aider_mod.subprocess, "run", spy)
    d = _repo_with_history(home)
    assert aider_mod.tracked_by_git(d / HISTORY_FILE) is False
    assert "GIT_DIR" not in seen and "GIT_WORK_TREE" not in seen and seen["LC_ALL"] == "C"
