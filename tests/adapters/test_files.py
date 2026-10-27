"""The built-in `files` source: JSONL v1 validation, cursors, deletes, txt/md import, watched folders, masking."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mannerism import bank, config, store, sync
from mannerism.adapters import files as F
from mannerism.adapters.files import Files, RecordError, parse_record
from mannerism.core import Location

from .conftest import append_jsonl, run, write_jsonl

SECRET = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"


def rec(i, text="We should ship the invoice batch before Thursday, and then review it.", **kw):
    d = {"schema_version": 1, "op": "upsert", "id": f"p:{i}", "ts": "2026-10-20T12:00:00Z", "text": text,
         "kind": "post", "register": "work"}
    d.update(kw)
    return d


def dele(i, **kw):
    d = {"schema_version": 1, "op": "delete", "id": f"p:{i}", "ts": "2026-10-21T08:00:00+02:00"}
    d.update(kw)
    return d


def line(obj) -> bytes:
    return json.dumps(obj).encode()


def reason(obj) -> str:
    with pytest.raises(RecordError) as e:
        parse_record(obj if isinstance(obj, bytes) else line(obj))
    return str(e.value)


def add_instance(name, path, **kw):
    cfg = config.load_config()
    cfg.sources[name] = {"adapter": "files", "path": str(path), "enabled": True, **kw}
    config.save_config(cfg)


def units(source=None):
    con = store.connect()
    q = "SELECT source_ref, text, register, kind, meta_json, thread_id, audience_ref, context_json FROM units"
    rows = con.execute(q + (" WHERE source=?" if source else ""), (source,) if source else ()).fetchall()
    con.close()
    return rows


def refs(source=None):
    return sorted(r["source_ref"] for r in units(source))


# --- validation ------------------------------------------------------------------------------------------------------


def test_a_minimal_and_a_full_record_are_valid():
    r = parse_record(line(rec(1)))
    assert isinstance(r, F.Upsert) and r.id == "p:1" and r.updated_at is None and r.origin is None
    full = rec(2, audience="Dana", thread_id="t-9", context=["a", "b", "c"], updated_at="2026-10-21T09:30:00.5+02:00",
               origin={"provider": "example", "account_id": "a1", "native_id": "123", "url": "https://example.org/p/123"})
    r = parse_record(line(full))
    assert r.updated_at.utcoffset().total_seconds() == 0 and r.updated_at.hour == 7
    assert r.origin["native_id"] == "123"


def test_delete_record_is_exact():
    d = parse_record(line(dele(1)))
    assert isinstance(d, F.Delete) and d.id == "p:1" and d.ts.hour == 6  # +02:00 becomes UTC
    assert reason(dele(1, text="x")) == "unknown field in a delete record"
    for missing in ("id", "ts", "schema_version"):
        bad = dele(1)
        del bad[missing]
        assert missing in reason(bad)


@pytest.mark.parametrize("missing", ["schema_version", "op", "id", "ts", "text", "kind", "register"])
def test_each_required_field_is_required(missing):
    bad = rec(1)
    del bad[missing]
    assert missing in reason(bad)


@pytest.mark.parametrize("version", [2, 0, "1", True, 1.0, None])
def test_schema_version_must_be_the_integer_one(version):
    assert "schema_version must be 1" in reason(rec(1, schema_version=version))


def test_unknown_fields_are_rejected_including_meta_and_public():
    for extra in ("meta", "public", "source", "unit_id", "whatever"):
        assert reason(rec(1, **{extra: True})) == "unknown field"  # the name is never echoed
    assert "bad name" not in reason(rec(1, **{"bad name!": 1}))


def test_op_kind_register_are_enumerations():
    assert "op must be" in reason(rec(1, op="replace"))
    assert "kind must be" in reason(rec(1, kind="email"))
    assert "register must be" in reason(rec(1, register="?"))
    assert "register must be" in reason(rec(1, register="skip"))
    for kind in ("message", "post", "comment", "document"):
        parse_record(line(rec(1, kind=kind)))
    for register in ("close", "group", "work", "formal", "agent", "public"):
        parse_record(line(rec(1, register=register)))


def test_id_rules():
    for bad in ("", "   ", 5, None, "x" * 513):
        assert "id" in reason(rec(1, id=bad))
    assert parse_record(line(rec(1, id="x" * 512))).id == "x" * 512


@pytest.mark.parametrize("ts", ["2026-10-20T12:00:00", "2026-10-20", "2026-10-20 12:00:00Z", "yesterday", 1760961600,
                                "2026-13-20T12:00:00Z", "2026-10-20T12:00:00+0200", "2026-10-20T25:00:00Z"])
def test_timestamps_need_an_explicit_offset(ts):
    assert "ts must be" in reason(rec(1, ts=ts)) or "ts is not a real" in reason(rec(1, ts=ts))


def test_timestamps_become_utc_and_updated_at_is_checked_too():
    assert parse_record(line(rec(1, ts="2026-10-20T14:00:00+02:00"))).ts.hour == 12
    assert parse_record(line(rec(1, ts="2026-10-20T12:00:00z"))).ts.hour == 12
    assert "updated_at must be" in reason(rec(1, updated_at="2026-10-20T12:00:00"))
    assert parse_record(line(rec(1, updated_at=None))).updated_at is None


def test_text_must_be_a_nonempty_string():
    for bad in ("", "  \n ", 3, None, ["a"]):
        assert "text" in reason(rec(1, text=bad))


def test_audience_and_thread_id_bounds():
    assert "audience" in reason(rec(1, audience="x" * 201))
    assert "audience" in reason(rec(1, audience=5))
    assert "thread_id" in reason(rec(1, thread_id=""))
    assert "thread_id" in reason(rec(1, thread_id="x" * 201))
    assert parse_record(line(rec(1, thread_id=None))).thread_id is None


def test_context_bounds():
    assert "context must be" in reason(rec(1, context=["a"] * 4))
    assert "context must be" in reason(rec(1, context="a"))
    assert "each context item" in reason(rec(1, context=["a" * 241]))
    assert "each context item" in reason(rec(1, context=[5]))
    assert parse_record(line(rec(1, context=["a" * 240] * 3))).context[2] == "a" * 240


def test_origin_is_null_or_exactly_four_strings_with_an_https_url():
    good = {"provider": "example", "account_id": "a", "native_id": "1", "url": "https://example.org/x"}
    assert parse_record(line(rec(1, origin=None))).origin is None
    assert parse_record(line(rec(1, origin=good))).origin == good
    assert "origin must be" in reason(rec(1, origin={**good, "extra": "x"}))
    assert "origin must be" in reason(rec(1, origin={k: v for k, v in good.items() if k != "url"}))
    assert "origin must be" in reason(rec(1, origin="https://example.org"))
    for url in ("http://example.org/x", "/relative/path", "ftp://example.org", "https://", "example.org"):
        assert "https URL" in reason(rec(1, origin={**good, "url": url}))
    assert "origin.provider" in reason(rec(1, origin={**good, "provider": ""}))
    assert "origin.native_id" in reason(rec(1, origin={**good, "native_id": 12}))


def test_json_level_problems():
    assert "not valid UTF-8" in reason(b"\xff\xfe{}")
    assert "invalid JSON" in reason(b"{nope")
    assert "must be a JSON object" in reason(b"[1, 2]")
    assert "duplicate field" in reason(b'{"schema_version": 1, "schema_version": 1}')
    assert "nested too deeply" in reason(b"[" * 100000)


def test_a_record_over_one_mebibyte_is_rejected():
    big = line(rec(1, text="word " * 250_000))
    assert len(big) > 1 << 20 and "larger than 1 MiB" in reason(big)


def test_reasons_never_contain_record_text():
    for bad in (rec(1, text=f"my token is {SECRET}", kind="nope"), rec(1, text=SECRET, bogus=SECRET),
                rec(1, text=SECRET, context=[SECRET] * 5), rec(1, text=SECRET, origin={"url": SECRET}),
                rec(1, id=SECRET, ts=SECRET), rec(1, text=SECRET, schema_version=SECRET)):
        assert SECRET not in reason(bad)
    assert SECRET not in reason(f'{{"text": "{SECRET}" oops'.encode())


# --- reading JSONL ---------------------------------------------------------------------------------------------------


def jsonl_loc(path):
    return Location("notes", str(path), path, "t")


def test_read_upserts_masks_and_never_trusts_the_record(tmp_path, me):
    p = tmp_path / "in" / "a.jsonl"
    write_jsonl(p, [rec(1, f"mail jo@example.org about {SECRET}", register="public", audience="Dana R.", thread_id="jo@example.org",
                        context=["call me on +44 20 7946 0958", "see jo@example.org"],
                        origin={"provider": "example", "account_id": "jo@example.org", "native_id": "1",
                                "url": "https://example.org/p?token=abc123"},
                        updated_at="2026-10-21T00:00:00Z")])
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    us, cur = run(src, jsonl_loc(p), None, me)
    assert len(us) == 1
    u = us[0]
    assert "jo@example.org" not in u.text and SECRET not in u.text and "[email]" in u.text and "[secret]" in u.text
    assert u.register == "public" and u.meta.get("public") is None  # never a trusted public flag
    assert set(u.meta) == {"origin", "updated_at"}
    assert u.meta["origin"]["account_id"] == "[email]" and "abc123" not in u.meta["origin"]["url"]
    assert [c.text for c in u.context] == ["call me on [phone]", "see [email]"]
    assert u.thread_id and "jo@" not in u.thread_id and u.audience_ref and "Dana" not in u.audience_ref
    assert u.audience == "public" and u.source == "notes"


def test_an_id_that_masks_differently_is_stored_hashed_and_deletes_find_it(tmp_path, me):
    p = tmp_path / "a.jsonl"
    write_jsonl(p, [rec(1, id="mail:jo@example.org")])
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    us, cur = run(src, jsonl_loc(p), None, me)
    assert us[0].source_ref.startswith("id:") and "example" not in us[0].source_ref
    assert F.ref_for("mail:jo@example.org") == us[0].source_ref
    append_jsonl(p, [dele(1, id="mail:jo@example.org")])
    us2, cur2 = run(src, jsonl_loc(p), cur, me)
    assert us2 == [] and cur2["_delete_refs"] == [us[0].source_ref]


def test_cursor_resumes_at_the_offset_and_is_idempotent(tmp_path, me):
    p = tmp_path / "a.jsonl"
    write_jsonl(p, [rec(1), rec(2)])
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    us, cur = run(src, jsonl_loc(p), None, me)
    assert [u.source_ref for u in us] == ["p:1", "p:2"] and cur["offset"] == p.stat().st_size and cur["lines"] == 2
    again, cur2 = run(src, jsonl_loc(p), cur, me)
    assert again == [] and cur2 == cur  # unchanged: no work at all
    append_jsonl(p, [rec(3)])
    more, cur3 = run(src, jsonl_loc(p), cur2, me)
    assert [u.source_ref for u in more] == ["p:3"] and cur3["gen"] == cur["gen"] and cur3["lines"] == 3


def test_a_last_line_without_a_newline_is_accepted_and_its_newline_is_not_a_line(tmp_path, me):
    p = tmp_path / "a.jsonl"
    write_jsonl(p, [rec(1), rec(2)], newline_end=False)
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    us, cur = run(src, jsonl_loc(p), None, me)
    assert [u.source_ref for u in us] == ["p:1", "p:2"] and cur["lines"] == 2 and "_error" not in cur
    with open(p, "a") as f:
        f.write("\n")
    nothing, cur2 = run(src, jsonl_loc(p), cur, me)
    assert nothing == [] and cur2["lines"] == 2 and cur2["offset"] == p.stat().st_size
    append_jsonl(p, [rec(3)])
    more, cur3 = run(src, jsonl_loc(p), cur2, me)
    assert [u.source_ref for u in more] == ["p:3"] and cur3["lines"] == 3


def test_a_half_written_last_line_waits_without_an_error(tmp_path, me):
    p = tmp_path / "a.jsonl"
    write_jsonl(p, [rec(1)])
    with open(p, "a") as f:
        f.write(json.dumps(rec(2))[:30])  # the producer is mid-write
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    us, cur = run(src, jsonl_loc(p), None, me)
    assert [u.source_ref for u in us] == ["p:1"] and "_error" not in cur
    with open(p, "a") as f:
        f.write(json.dumps(rec(2))[30:] + "\n")
    more, cur2 = run(src, jsonl_loc(p), cur, me)
    assert [u.source_ref for u in more] == ["p:2"]


def test_a_malformed_line_stops_the_file_there_and_keeps_the_cursor_on_it(tmp_path, me):
    p = tmp_path / "a.jsonl"
    write_jsonl(p, [rec(1), "{ not json", rec(3)])
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    us, cur = run(src, jsonl_loc(p), None, me)
    assert [u.source_ref for u in us] == ["p:1"]
    assert cur["_error"].startswith(f"{p}:2:invalid JSON") and cur["lines"] == 1
    assert cur["offset"] == len((json.dumps(rec(1)) + "\n").encode())
    # the problem stays reported until the file is fixed, and nothing after it is read
    us2, cur2 = run(src, jsonl_loc(p), {k: v for k, v in cur.items() if k != "_error"}, me)
    assert us2 == [] and cur2["_error"].startswith(f"{p}:2:")


def test_blank_lines_a_bom_and_crlf_are_fine(tmp_path, me):
    p = tmp_path / "a.jsonl"
    p.write_bytes(b"\xef\xbb\xbf" + line(rec(1)) + b"\r\n\r\n" + line(rec(2)) + b"\r\n")
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    us, cur = run(src, jsonl_loc(p), None, me)
    assert [u.source_ref for u in us] == ["p:1", "p:2"] and cur["lines"] == 3 and "_error" not in cur


def test_an_oversized_line_is_an_error_not_a_hang(tmp_path, me):
    p = tmp_path / "a.jsonl"
    p.write_bytes(line(rec(1)) + b"\n" + b'{"schema_version": 1, "text": "' + b"x" * (2 << 20) + b'"}\n' + line(rec(3)) + b"\n")
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    us, cur = run(src, jsonl_loc(p), None, me)
    assert [u.source_ref for u in us] == ["p:1"] and cur["_error"].endswith("record is larger than 1 MiB")


def test_a_replaced_file_starts_a_new_generation_and_is_read_again(tmp_path, me):
    p = tmp_path / "a.jsonl"
    write_jsonl(p, [rec(1), rec(2)])
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    _, cur = run(src, jsonl_loc(p), None, me)
    tmp = tmp_path / ".a.jsonl.tmp"
    write_jsonl(tmp, [rec(1, "A newer wording of the first post, written after the rename."), rec(2), rec(9)])
    os.replace(tmp, p)  # the documented way to publish a file: new inode
    us, cur2 = run(src, jsonl_loc(p), cur, me)
    assert cur2["gen"] == cur["gen"] + 1 and [u.source_ref for u in us] == ["p:1", "p:2", "p:9"]
    assert cur2["offset"] == p.stat().st_size


def test_a_truncated_or_rewritten_head_is_a_new_generation(tmp_path, me):
    p = tmp_path / "a.jsonl"
    write_jsonl(p, [rec(i) for i in range(5)])
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    _, cur = run(src, jsonl_loc(p), None, me)
    write_jsonl(p, [rec(0)])  # same inode, shorter
    us, cur2 = run(src, jsonl_loc(p), cur, me)
    assert cur2["gen"] == cur["gen"] + 1 and [u.source_ref for u in us] == ["p:0"]
    write_jsonl(p, [rec(7, "completely different first line of the same inode"), rec(8)])
    us, cur3 = run(src, jsonl_loc(p), cur2, me)
    assert cur3["gen"] == cur2["gen"] + 1 and [u.source_ref for u in us] == ["p:7", "p:8"]


def test_the_read_budget_pages_through_a_big_file(tmp_path, me, monkeypatch):
    monkeypatch.setattr(F, "READ_BUDGET", 600)
    p = tmp_path / "a.jsonl"
    write_jsonl(p, [rec(i) for i in range(12)])
    src = Files("notes")
    src.discover(me, {"path": str(p)})
    seen, cur = [], None
    for _ in range(40):
        us, cur = run(src, jsonl_loc(p), cur, me)
        seen += [u.source_ref for u in us]
        assert "_error" not in cur
        if not cur.get("more"):
            break
    assert seen == [f"p:{i}" for i in range(12)]


# --- through the sync: stored units, deletes, isolation -----------------------------------------------------------


def test_sync_upsert_is_idempotent_and_an_edit_updates_the_unit(tmp_path):
    drop = tmp_path / "drop"
    write_jsonl(drop / "a.jsonl", [rec(1), rec(2, "Second post, which says something else entirely about rollout plans.")])
    add_instance("notes", drop)
    first = sync.sync()
    assert first["sources"]["notes"]["new_units"] == 2 and first["status"] == "ok"
    assert sync.sync()["new_units"] == 0 and refs("notes") == ["p:1", "p:2"]
    # an edit in place: same id, new text, the file replaced by rename
    tmp = drop / ".a.jsonl.part"
    write_jsonl(tmp, [rec(1, "The first post, now edited to say a good deal more than it did before."), rec(2, "Second post, which says something else entirely about rollout plans.")])
    os.replace(tmp, drop / "a.jsonl")
    res = sync.sync()
    assert res["sources"]["notes"]["updated_units"] == 2 and res["new_units"] == 0
    texts = {r["source_ref"]: r["text"] for r in units("notes")}
    assert texts["p:1"].startswith("The first post, now edited") and len(texts) == 2
    # an edit appended to the same file also replaces the unit
    append_jsonl(drop / "a.jsonl", [rec(1, "A third wording of the first post that arrives as an appended record.")])
    sync.sync()
    assert {r["source_ref"]: r["text"] for r in units("notes")}["p:1"].startswith("A third wording")


def test_an_edit_recomputes_features_and_rebuilds_the_bank(tmp_path):
    drop = tmp_path / "drop"
    write_jsonl(drop / "a.jsonl", [rec(1, "short note about the plan today ok")])
    add_instance("notes", drop)
    sync.sync()
    con = store.connect()
    before = con.execute("SELECT features_json FROM unit_features").fetchone()[0]
    assert con.execute("SELECT count(*) FROM bank WHERE text LIKE 'short note%'").fetchone()[0] == 1
    con.close()
    append_jsonl(drop / "a.jsonl", [rec(1, "a much longer rewritten note about the plan for today which now has many more words in it")])
    sync.sync()
    con = store.connect()
    after = con.execute("SELECT features_json FROM unit_features").fetchone()[0]
    assert after != before and json.loads(after)["words"] > json.loads(before)["words"]
    assert con.execute("SELECT count(*) FROM bank WHERE text LIKE 'short note%'").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM bank WHERE text LIKE 'a much longer%'").fetchone()[0] == 1


def test_a_delete_record_removes_the_unit_and_everything_derived(tmp_path):
    drop = tmp_path / "drop"
    write_jsonl(drop / "a.jsonl", [rec(1, "We should ship the invoice batch before Thursday and then review it."),
                                   rec(2, "Keep this other post, it stays stored and keeps counting in the bank.")])
    add_instance("notes", drop)
    sync.sync()
    con = store.connect()
    uid = con.execute("SELECT unit_id FROM units WHERE source_ref='p:1'").fetchone()[0]
    other = con.execute("SELECT unit_id FROM units WHERE source_ref='p:2'").fetchone()[0]
    con.execute("INSERT INTO labels VALUES (?,?,?,?,?,?)", (uid, "work", 4, "en", "agent", store.now_iso()))
    con.execute("INSERT INTO labels VALUES (?,?,?,?,?,?)", (other, "work", 4, "en", "agent", store.now_iso()))
    con.execute("INSERT INTO drafts(ts, register, origin, sha256, text) VALUES (?,?,?,?,?)",
                (store.now_iso(), "work", "t", "abc", "draft"))
    con.execute("INSERT INTO edits(ts, register, draft, final, status, origin, unit_id, draft_id, final_sha) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (store.now_iso(), "work", "d", "f", "edited", "t", uid, 1, "x"))
    con.execute("INSERT INTO rules(register, text, created_at, last_seen, edit_ids_json) VALUES (?,?,?,?,?)",
                ("work", "drop greetings", store.now_iso(), store.now_iso(), "[1]"))
    con.execute("INSERT INTO ai_units VALUES (?,?,?)", (uid, 0.9, store.now_iso()))
    con.execute("INSERT INTO packets(packet_id, kind, register, created_at, payload_json, schema_json) "
                "VALUES ('p1','card','work',?,'{}','{}')", (store.now_iso(),))
    store.set_meta_json(con, "card_sources:work", ["notes"])
    store.set_meta(con, "blind:r1", "{}")
    assert con.execute("SELECT count(*) FROM bank WHERE unit_id=?", (uid,)).fetchone()[0] == 1
    con.close()
    append_jsonl(drop / "a.jsonl", [dele(1)])
    res = sync.sync()
    assert res["deleted_units"] == 1 and res["sources"]["notes"]["deleted_units"] == 1 and res["status"] == "ok"
    con = store.connect()
    for table, col in (("units", "unit_id"), ("labels", "unit_id"), ("unit_features", "unit_id"), ("bank", "unit_id"),
                       ("ai_units", "unit_id"), ("edits", "unit_id")):
        assert con.execute(f"SELECT count(*) FROM {table} WHERE {col}=?", (uid,)).fetchone()[0] == 0, table
    assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM rules").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM packets WHERE packet_id='p1'").fetchone()[0] == 0
    assert store.get_meta(con, "blind:r1") is None
    assert store.get_meta(con, "card_needed:work") == "1" and store.get_meta_json(con, "card_sources:work") is None
    assert con.execute("SELECT count(*) FROM units WHERE unit_id=?", (other,)).fetchone()[0] == 1  # the other one stays
    assert con.execute("SELECT count(*) FROM bank WHERE unit_id=?", (other,)).fetchone()[0] == 1
    assert bank.bank_counts(con).get("work") == 1
    con.close()
    # deleting an id that is already gone is not an error; the same id can come back later
    append_jsonl(drop / "a.jsonl", [dele(1), rec(1, "Back again with brand new wording for the first post of the day.")])
    again = sync.sync()
    assert again["status"] == "ok" and again["deleted_units"] == 0 and refs("notes") == ["p:1", "p:2"]


def test_upsert_then_delete_in_one_read_leaves_nothing(tmp_path):
    drop = tmp_path / "drop"
    write_jsonl(drop / "a.jsonl", [rec(1), dele(1), rec(2), rec(2, "Second wording of post two, which is the one that wins in the end."), ])
    add_instance("notes", drop)
    sync.sync()
    assert refs("notes") == ["p:2"] and units("notes")[0]["text"].startswith("Second wording")


def test_a_malformed_line_stops_that_file_but_other_files_and_sources_continue(tmp_path):
    drop = tmp_path / "drop"
    write_jsonl(drop / "a.jsonl", [rec(1), rec(2, SECRET, kind="nope"), rec(3)])
    write_jsonl(drop / "b.jsonl", [rec(10), rec(11)])
    other = tmp_path / "other"
    write_jsonl(other / "c.jsonl", [rec(20)])
    add_instance("notes", drop)
    add_instance("more", other)
    res = sync.sync()
    assert res["status"] == "ok" and res["sources"]["notes"]["status"] == "error"
    assert refs("notes") == ["p:1", "p:10", "p:11"] and refs("more") == ["p:20"]
    err = res["sources"]["notes"]["errors"]
    assert len(err) == 1 and err[0].endswith("a.jsonl:2:kind must be one of: message, post, comment, document")
    assert SECRET not in json.dumps(res)
    con = store.connect()
    assert SECRET not in json.dumps([dict(r) for r in con.execute("SELECT * FROM ingest_runs")])
    assert SECRET not in json.dumps([dict(r) for r in con.execute("SELECT * FROM meta")])
    cur = store.get_cursor(con, "notes", str((drop / "a.jsonl").resolve()))
    assert cur["lines"] == 1 and "_error" not in cur  # the cursor waits on the bad line
    con.close()
    # the same error is reported again until the file is fixed, then the rest of the file is read
    assert sync.sync()["sources"]["notes"]["errors"][0].endswith("a.jsonl:2:kind must be one of: message, post, comment, document")
    write_jsonl(drop / "a.jsonl.new", [])  # a non-matching name is ignored
    tmp = drop / ".a.fixed"
    write_jsonl(tmp, [rec(1), rec(2), rec(3)])
    os.replace(tmp, drop / "a.jsonl")
    fixed = sync.sync()
    assert fixed["sources"]["notes"]["status"] == "ok" and refs("notes") == ["p:1", "p:10", "p:11", "p:2", "p:3"]


def test_two_instances_do_not_share_units_and_forget_is_per_instance(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_jsonl(a / "x.jsonl", [rec(1, "From instance a: the very first post to be stored here.")])
    write_jsonl(b / "x.jsonl", [rec(1, "From instance b: the very first post to be stored here.")])
    add_instance("alpha", a)
    add_instance("beta", b)
    sync.sync()
    assert refs("alpha") == ["p:1"] and refs("beta") == ["p:1"]
    from mannerism import api
    api.forget(source="alpha")
    assert refs("alpha") == [] and refs("beta") == ["p:1"]
    assert config.load_config().sources["alpha"]["enabled"] is False


def test_public_register_is_stored_but_never_meta_public_and_never_in_the_public_bank(tmp_path):
    drop = tmp_path / "drop"
    write_jsonl(drop / "a.jsonl", [rec(i, f"Shipped the rollout number {i} today and the part that surprised me was the speed of it.",
                                       register="public") for i in range(12)])
    add_instance("notes", drop)
    sync.sync()
    rows = units("notes")
    assert len(rows) == 12 and {r["register"] for r in rows} == {"public"}
    assert all("public" not in json.loads(r["meta_json"]) for r in rows)
    con = store.connect()
    assert con.execute("SELECT count(*) FROM bank WHERE register='public'").fetchone()[0] == 0
    con.close()


# --- txt / md, folders ---------------------------------------------------------------------------------------------


def test_txt_and_md_are_one_unit_per_file_with_the_instance_register_and_kind(tmp_path, me):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "note.txt").write_text("Thanks for the update on the invoices.\n\nI will review them tomorrow morning.\n")
    (d / "post.md").write_text("---\ntitle: T\ndate: 2026-09-01\npublic: true\n---\n# Heading\n\nSome words about the **rollout**, "
                               "see [the plan](http://x.test/plan).\n\n```\ncode()\n```\n")
    (d / ".hidden.txt").write_text("hidden")
    (d / "scratch.txt.tmp").write_text("tmp")
    (d / "data.json").write_text("{}")
    src = Files("docs")
    locs = src.discover(me, {"path": str(d), "register": "work", "kind": "comment"})
    assert [loc.path.name for loc in locs] == ["note.txt", "post.md"]
    note, _ = run(src, locs[0], None, me)
    post, _ = run(src, locs[1], None, me)
    assert len(note) == 1 and note[0].register == "work" and note[0].kind == "comment" and "tomorrow morning" in note[0].text
    assert note[0].text.count("\n\n") == 1  # paragraphs are kept inside the unit
    assert len(post) == 1 and post[0].ts.year == 2026 and post[0].ts.month == 9
    assert "title:" not in post[0].text and "code()" not in post[0].text and "the plan" in post[0].text and "http" not in post[0].text
    assert post[0].meta == {}  # frontmatter `public: true` is not a trusted flag


def test_a_long_document_is_chunked_and_defaults_apply(tmp_path, me):
    p = tmp_path / "long.txt"
    para = " ".join(["word"] * 120)
    p.write_text("\n\n".join([para] * 7))
    src = Files("docs")
    loc = src.discover(me, {"path": str(p)})[0]
    us, _ = run(src, loc, None, me)
    assert len(us) == 4 and all(u.register == "?" and u.kind == "document" and u.audience == "unknown" for u in us)
    assert len({u.thread_id for u in us}) == 1


def test_invalid_register_or_kind_in_config_fall_back(tmp_path, me):
    p = tmp_path / "a.txt"
    p.write_text("hello there my friend how are you today")
    src = Files("docs")
    loc = src.discover(me, {"path": str(p), "register": "nonsense", "kind": "email"})[0]
    us, _ = run(src, loc, None, me)
    assert us[0].register == "?" and us[0].kind == "document"


def test_a_changed_document_replaces_its_old_chunks(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "n.txt").write_text("First version of this note, with enough words to be a unit.")
    add_instance("docs", d, register="work")
    sync.sync()
    assert len(units("docs")) == 1
    (d / "n.txt").write_text("Second version of the note, written over the first one entirely.")
    res = sync.sync()
    assert res["sources"]["docs"]["new_units"] == 1
    assert [r["text"] for r in units("docs")] == ["Second version of the note, written over the first one entirely."]
    assert sync.sync()["new_units"] == 0  # untouched file: nothing to do


def test_a_folder_is_watched_new_and_changed_files_are_picked_up_on_each_sync(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    add_instance("watched", drop, register="close")
    assert sync.sync()["new_units"] == 0
    (drop / "one.txt").write_text("hey did you see the match last night, it was wild")
    assert sync.sync()["sources"]["watched"]["new_units"] == 1
    sub = drop / "sub" / "deeper"
    sub.mkdir(parents=True)
    write_jsonl(sub / "feed.jsonl", [rec(1, "a post from a script that drops files into the folder")])
    (drop / "two.md").write_text("second note, a little longer than the first one was")
    assert sync.sync()["sources"]["watched"]["new_units"] == 2
    append_jsonl(sub / "feed.jsonl", [rec(2, "another post that the script appended later on")])
    assert sync.sync()["sources"]["watched"]["new_units"] == 1
    assert sync.sync()["new_units"] == 0
    assert sorted(r["source_ref"] for r in units("watched") if r["source_ref"].startswith("p:")) == ["p:1", "p:2"]


def test_the_default_inbox_belongs_to_the_built_in_files_source_only(tmp_path, me, home):
    inbox = home / ".mannerism" / "inbox" / "files"
    inbox.mkdir(parents=True)
    (inbox / "x.txt").write_text("a note that was dropped in the default inbox")
    assert [loc.path.name for loc in Files().discover(me, {})] == ["x.txt"]
    assert Files("custom").discover(me, {}) == []
    assert not (tmp_path / "nothing").exists()


def test_discover_creates_nothing_and_survives_junk(home, me):
    assert Files().discover(me, {"path": 5, "paths": [None, 7, "\x00bad"]}) == []
    assert not (home / ".mannerism").exists()


def test_a_document_that_is_not_utf8_is_an_error_for_that_file_only(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "bad.txt").write_bytes(b"\xff\xfe not utf8")
    (d / "good.txt").write_text("a perfectly fine note with some words in it")
    add_instance("docs", d)
    res = sync.sync()
    assert res["sources"]["docs"]["new_units"] == 1 and "not valid UTF-8" in res["sources"]["docs"]["errors"][0]


def test_check_file_reports_every_bad_line_and_stores_nothing(tmp_path, home):
    p = tmp_path / "x.jsonl"
    write_jsonl(p, [rec(1), "oops", rec(3, kind="nope"), dele(4), rec(5, register="public"), "", dele(6, text="x")])
    rep = F.check_file(p)
    assert (rep.records, rep.upserts, rep.deletes, rep.public) == (3, 2, 1, 1)
    assert [n for n, _ in rep.errors] == [2, 3, 7]
    assert not (home / ".mannerism").exists()
    assert isinstance(Path(p), Path)


# --- review fixes ----------------------------------------------------------------------------------------------------

WORDS = "Shipped the invoice batch today and the part that surprised me most was how reviewing early saved a whole week"


def put_jsonl(path, records):
    tmp = path.with_name(".tmp-" + path.name)
    write_jsonl(tmp, records)
    os.replace(tmp, path)


def bank_rows():
    con = store.connect()
    try:
        return [(r["register"], r["text"][:10]) for r in con.execute("SELECT register, text FROM bank")]
    finally:
        con.close()


def test_changing_a_units_register_refreshes_the_old_registers_bank_too(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    add_instance("feed", drop)
    put_jsonl(drop / "feed.jsonl", [rec("a", WORDS, register="work")])
    sync.sync()
    assert bank_rows() == [("work", "Shipped th")]
    put_jsonl(drop / "feed.jsonl", [rec("a", WORDS, register="close")])  # same text, another register
    sync.sync()
    assert bank_rows() == [("close", "Shipped th")]  # the work bank no longer holds it


def test_an_edit_that_changes_text_and_register_leaves_nothing_of_the_old_text_in_the_old_bank(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    add_instance("feed", drop)
    put_jsonl(drop / "feed.jsonl", [rec("a", "OLDSECRET " + WORDS, register="work")])
    sync.sync()
    put_jsonl(drop / "feed.jsonl", [rec("a", "NEWTEXT " + WORDS, register="close")])
    sync.sync()
    assert bank_rows() == [("close", "NEWTEXT Sh")]
    con = store.connect()
    assert con.execute("SELECT count(*) FROM bank_fts WHERE bank_fts MATCH 'OLDSECRET'").fetchone()[0] == 0
    con.close()


def seed_derived(uid, source):
    con = store.connect()
    con.execute("INSERT INTO labels VALUES (?,?,?,?,?,?)", (uid, "work", 4, "en", "agent", store.now_iso()))
    con.execute("INSERT INTO drafts(ts, register, origin, sha256, text) VALUES (?,?,?,?,?)",
                (store.now_iso(), "work", "t", "abc", "draft"))
    con.execute("INSERT INTO edits(ts, register, draft, final, status, origin, unit_id, draft_id, final_sha) "
                "VALUES (?,?,?,?,?,?,?,?,?)", (store.now_iso(), "work", "d", "f", "edited", "t", uid, 1, "x"))
    con.execute("INSERT INTO rules(register, text, created_at, last_seen, edit_ids_json) VALUES (?,?,?,?,?)",
                ("work", "drop greetings", store.now_iso(), store.now_iso(), "[1]"))
    con.execute("INSERT INTO packets(packet_id, kind, register, created_at, payload_json, schema_json) "
                "VALUES ('p1','card','work',?,'{}','{}')", (store.now_iso(),))
    store.set_meta_json(con, "card_sources:work", [source])
    con.close()
    cards = core_home() / "cards"
    cards.mkdir(parents=True, exist_ok=True)
    (cards / "work.agent.md").write_text("an agent card")
    (cards / "work.md").write_text("an agent card")
    ev = core_home() / "eval"
    ev.mkdir(parents=True, exist_ok=True)
    (ev / "page.html").write_text("quotes OLDSECRET")


def core_home():
    from mannerism import core
    return core.home()


def assert_purged(uid, *, full: bool):
    """Unit-level data is gone either way. `full` (an explicit delete) also drops the agent card, eval/ and card_sources;
    an edit keeps the (expensive) agent card and only marks it stale."""
    con = store.connect()
    assert con.execute("SELECT count(*) FROM labels WHERE unit_id=?", (uid,)).fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM edits").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM drafts").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM rules").fetchone()[0] == 0
    assert con.execute("SELECT count(*) FROM packets WHERE packet_id='p1'").fetchone()[0] == 0
    assert store.get_meta(con, "card_needed:work") == "1"
    assert (store.get_meta_json(con, "card_sources:work") is None) == full
    con.close()
    assert (not (core_home() / "eval").exists()) == full
    assert (core_home() / "cards" / "work.agent.md").exists() == (not full)


def test_an_edited_text_is_purged_like_a_delete_and_insert(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    add_instance("feed", drop)
    put_jsonl(drop / "feed.jsonl", [rec("a", "OLDSECRET " + WORDS)])
    sync.sync()
    con = store.connect()
    uid = con.execute("SELECT unit_id FROM units").fetchone()[0]
    con.close()
    seed_derived(uid, "feed")
    put_jsonl(drop / "feed.jsonl", [rec("a", "NEWTEXT " + WORDS)])
    res = sync.sync()
    assert res["sources"]["feed"]["updated_units"] == 1
    assert_purged(uid, full=False)
    assert [r["text"][:7] for r in units("feed")] == ["NEWTEXT"]
    assert bank_rows() == [("work", "NEWTEXT Sh")]


def test_an_unchanged_text_keeps_its_label_and_derived_data(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    add_instance("feed", drop)
    put_jsonl(drop / "feed.jsonl", [rec("a", WORDS, ts="2026-10-20T12:00:00Z")])
    sync.sync()
    con = store.connect()
    uid = con.execute("SELECT unit_id FROM units").fetchone()[0]
    con.execute("INSERT INTO labels VALUES (?,?,?,?,?,?)", (uid, "work", 4, "en", "agent", store.now_iso()))
    con.close()
    put_jsonl(drop / "feed.jsonl", [rec("a", WORDS, ts="2026-10-21T12:00:00Z")])  # only the time changed
    sync.sync()
    con = store.connect()
    assert con.execute("SELECT count(*) FROM labels").fetchone()[0] == 1
    con.close()


def test_a_changed_document_chunk_prune_purges_derived_data_too(tmp_path):
    d = tmp_path / "docs"
    d.mkdir()
    (d / "n.txt").write_text("OLDSECRET " + WORDS)
    add_instance("docs", d, register="work")
    sync.sync()
    con = store.connect()
    uid = con.execute("SELECT unit_id FROM units").fetchone()[0]
    con.close()
    seed_derived(uid, "docs")
    (d / "n.txt").write_text("NEWTEXT replaced entirely by a new text that has no chunk in common at all")
    sync.sync()
    assert_purged(uid, full=False)
    assert [r["text"][:7] for r in units("docs")] == ["NEWTEXT"]
    assert bank_rows() == [("work", "NEWTEXT re")]


def test_same_named_files_under_two_roots_never_prune_each_other(tmp_path):
    a, b = tmp_path / "a" / "notes", tmp_path / "b" / "notes"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    (a / "todo.txt").write_text("alpha note about the first project and its plans for the week")
    (b / "todo.txt").write_text("bravo note about the other project and its plans for the week")
    add_instance("docs", a, paths=[str(b)])
    sync.sync()
    assert len(units("docs")) == 2
    (a / "todo.txt").write_text("alpha note rewritten with different words altogether this time")
    sync.sync()
    texts = sorted(r["text"][:5] for r in units("docs"))
    assert texts == ["alpha", "bravo"]  # b's unit survived a's edit
    (b / "todo.txt").write_text("bravo note rewritten too, also with different words altogether")
    sync.sync()
    assert len(units("docs")) == 2


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs mkfifo")
def test_a_fifo_is_never_opened_and_cannot_hang_a_sync(tmp_path, me):
    import threading
    drop = tmp_path / "drop"
    drop.mkdir()
    fifo = drop / "pipe.jsonl"
    os.mkfifo(fifo)
    os.symlink(fifo, drop / "link.txt")
    (drop / "ok.txt").write_text("a regular note that is read as usual by the sync")
    assert [loc.path.name for loc in Files("d").discover(me, {"path": str(drop)})] == ["ok.txt"]
    add_instance("d", drop)
    out = {}

    def go():
        out["res"] = sync.sync()

    t = threading.Thread(target=go, daemon=True)
    t.start()
    t.join(10)
    try:
        assert not t.is_alive(), "sync blocked on a FIFO"
    finally:
        if t.is_alive():
            os.close(os.open(fifo, os.O_RDWR))  # release a blocked open so the thread can end
    assert out["res"]["sources"]["d"]["new_units"] == 1
    # even a location handed straight to read() (a file swapped for a FIFO after discovery) is skipped
    src = Files("d")
    for name in ("pipe.jsonl", "link.txt"):
        us, cur = src.read(Location("d", str(drop / name), drop / name, "x"), None, me)
        assert list(us) == [] and "_error" not in cur
    only_fifo = tmp_path / "only.jsonl"
    os.mkfifo(only_fifo)
    assert Files("d").discover(me, {"path": str(only_fifo)}) == []


def test_a_bom_on_a_single_unterminated_line_is_accepted(tmp_path, me):
    p = tmp_path / "one.jsonl"
    p.write_bytes(b"\xef\xbb\xbf" + line(rec("a")))  # Notepad: BOM, no trailing newline
    src = Files("t")
    us, cur = run(src, jsonl_loc(p), None, me)
    assert [u.source_ref for u in us] == ["p:a"] and "_error" not in cur and cur["offset"] == p.stat().st_size
    rep = F.check_file(p)
    assert (rep.records, rep.errors) == (1, [])


def test_an_unterminated_last_line_that_is_valid_json_but_not_a_record_is_an_error_not_a_wait(tmp_path, me):
    p = tmp_path / "one.jsonl"
    p.write_bytes(line(rec("a")) + b"\n" + line(rec("b", kind="nope")))
    src = Files("t")
    us, cur = run(src, jsonl_loc(p), None, me)
    assert [u.source_ref for u in us] == ["p:a"] and cur["_error"].endswith(":2:kind must be one of: message, post, comment, document")


def test_jsonl_ids_are_per_instance_a_delete_in_any_file_removes_the_id(tmp_path):
    drop = tmp_path / "drop"
    drop.mkdir()
    add_instance("feed", drop)
    write_jsonl(drop / "a.jsonl", [rec("x", WORDS)])
    write_jsonl(drop / "b.jsonl", [dele("x")])
    sync.sync()
    assert refs("feed") == []  # b.jsonl deleted what a.jsonl stored: the id belongs to the instance, not the file


def test_an_edit_keeps_the_agent_card_and_marks_it_stale_but_a_jsonl_delete_drops_it(tmp_path):
    from mannerism import api, cards
    drop = tmp_path / "drop"
    drop.mkdir()
    add_instance("feed", drop)
    put_jsonl(drop / "feed.jsonl", [rec("a", "OLDSECRET " + WORDS), rec("b", "Another post " + WORDS)])
    sync.sync()
    con = store.connect()
    uid = con.execute("SELECT unit_id FROM units WHERE source_ref='p:a'").fetchone()[0]
    con.close()
    seed_derived(uid, "feed")
    put_jsonl(drop / "feed.jsonl", [rec("a", "NEWTEXT " + WORDS), rec("b", "Another post " + WORDS)])
    sync.sync()
    assert cards.card_source("work") == "agent" and cards.load_card("work") == "an agent card"
    assert (core_home() / "eval" / "page.html").exists()  # eval pages are not wiped by an edit
    assert api.status()["registers"]["work"]["card_source"] == "agent"
    con = store.connect()
    assert store.get_meta(con, "card_needed:work") == "1" and store.get_meta_json(con, "card_sources:work") == ["feed"]
    con.close()
    assert api._card_state(store.connect(), "work", 99, set()) == "upgrade available"
    # an explicit delete record still drops the card, eval/ and the sources record
    append_jsonl(drop / "feed.jsonl", [dele("b")])
    sync.sync()
    assert cards.card_source("work") != "agent" and not (core_home() / "eval").exists()
    con = store.connect()
    assert store.get_meta_json(con, "card_sources:work") is None and store.get_meta(con, "card_needed:work") == "1"
    con.close()
