"""A file bigger than the per-read byte budget is caught up within one sync, and a single line bigger than the
budget is skipped instead of stalling the file forever."""
from mannerism.adapters._util import read_new_lines


def test_oversized_line_is_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b"a\n" + b"x" * 5000 + b"\nb\n")
    lines, cur = read_new_lines(p, None, budget=1000)
    assert lines == ["a"] and cur["more"]
    lines, cur = read_new_lines(p, cur, budget=1000)
    assert lines == [] and cur["oversized_lines"] == 1 and cur["more"]
    lines, cur = read_new_lines(p, cur, budget=1000)
    assert lines == ["b"] and not cur["more"]


def test_more_flag_and_resume(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b"".join(f"line{i}\n".encode() for i in range(100)))
    seen, cur = [], None
    for _ in range(50):
        lines, cur = read_new_lines(p, cur, budget=64)
        seen += lines
        if not cur["more"]:
            break
    assert seen == [f"line{i}" for i in range(100)]


def test_sync_catches_up_a_big_transcript_in_one_call(monkeypatch, tmp_path):
    import json

    from mannerism import api, sync
    from mannerism.adapters import _util

    monkeypatch.setattr(_util, "READ_BUDGET", 2048)
    monkeypatch.setattr(_util.read_new_lines, "__defaults__", (2048,))
    d = tmp_path / "home" / ".claude" / "projects" / "-work-x"
    d.mkdir(parents=True)
    rows = []
    for i in range(300):
        rows.append({"type": "user", "uuid": f"u{i}", "sessionId": "s1", "timestamp": f"2026-10-01T10:{i // 60:02d}:{i % 60:02d}Z",
                     "cwd": "/work/x", "message": {"role": "user", "content": f"please fix the parser case number {i} today"}})
    (d / "s1.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    api.setup()
    from mannerism import store
    con = store.connect()
    assert con.execute("select count(*) from units where source='claude_code'").fetchone()[0] == 300
    assert sync.MAX_ROUNDS_PER_LOCATION >= 100
