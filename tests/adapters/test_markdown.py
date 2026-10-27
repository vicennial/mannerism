import json
import os

from mannerism.adapters.markdown import Markdown

from .conftest import run

NOTE = """---
title: Draft
tags: [a]
---
# Heading stays out

I think the best notes are short. See [[Other note|the other one]] and [[plain]].

![[embedded.png]]

```python
print("code is dropped")
```

<!-- private comment -->
Second paragraph with my email jo.dev@example.org in it.
"""


def test_markdown_private_and_public(home, me, tmp_path):
    vault = tmp_path / "vault"
    (vault / ".obsidian").mkdir(parents=True)
    (vault / ".obsidian" / "x.md").write_text("hidden config note words here")
    (vault / "node_modules").mkdir()
    (vault / "node_modules" / "p.md").write_text("module readme words here")
    (vault / "blog").mkdir()
    (vault / "blog" / "post.md").write_text("A public blog paragraph with enough words.")
    (vault / "pub.md").write_text("---\npublic: true\n---\nPublished by frontmatter flag, with many words.")
    (vault / "big.md").write_text("x " * 600_000)
    (vault / "note.md").write_text(NOTE)
    cfg = home / ".config" / "obsidian"
    cfg.mkdir(parents=True)
    (cfg / "obsidian.json").write_text(json.dumps({"vaults": {"abc": {"path": str(vault)}}}))
    src = Markdown()
    locs = src.discover(me, {})
    assert [lc.path for lc in locs] == [vault]
    units, cur = run(src, locs[0], None, me)
    # a `blog` folder name alone does not make text public
    blog = next(u for u in units if u.text.startswith("A public blog"))
    assert (blog.register, blog.audience, blog.kind) == ("?", "self", "note")
    units = [u for u in units if u is not blog]
    by = {u.text.split()[0]: u for u in units}
    note = next(u for u in units if u.kind == "note")
    assert "the other one" in note.text and "plain" in note.text and "[[" not in note.text
    assert "embedded" not in note.text and "code is dropped" not in note.text and "private comment" not in note.text
    assert "Heading stays out" not in note.text and "[email]" in note.text and "title:" not in note.text
    assert (note.register, note.audience, note.kind) == ("?", "self", "note")
    pubs = [u for u in units if u.register == "public"]
    assert len(pubs) == 1 and all(u.meta["public"] and u.kind == "post" and u.audience == "public" for u in pubs)
    assert len(units) == 2 and by
    # unchanged: nothing new; modified file: only the changed paragraph is new
    again, cur2 = run(src, locs[0], cur, me)
    assert again == []
    p = vault / "note.md"
    p.write_text(NOTE + "\nA brand new closing paragraph appended later.\n")
    os.utime(p, (p.stat().st_atime + 5, p.stat().st_mtime + 5))
    changed, _ = run(src, locs[0], cur2, me)
    assert len(changed) == 1 and "closing paragraph" in changed[0].text


def test_markdown_public_dirs_config(home, me, tmp_path):
    root = tmp_path / "notes"
    (root / "writing").mkdir(parents=True)
    (root / "private").mkdir()
    (root / "writing" / "essay.md").write_text("An essay paragraph meant for readers.")
    (root / "private" / "diary.md").write_text("A diary paragraph meant for nobody.")
    src = Markdown()
    loc = src.discover(me, {"paths": [str(root)], "public_dirs": ["writing"]})[0]
    units, cur = run(src, loc, None, me)
    by = {u.text.split()[1]: u for u in units}
    assert by["essay"].register == "public" and by["essay"].meta["public"] and by["essay"].audience == "public"
    assert by["diary"].register == "?" and by["diary"].audience == "self"
    assert run(src, loc, cur, me)[0] == []
    # changing public_dirs re-reads every file with the new registers
    src2 = Markdown()
    loc = src2.discover(me, {"paths": [str(root)], "public_dirs": [str(root / "private")]})[0]
    units, _ = run(src2, loc, cur, me)
    by = {u.text.split()[1]: u for u in units}
    assert by["diary"].register == "public" and by["essay"].register == "?"


def test_markdown_configured_paths_and_grouping(home, me, tmp_path):
    d = tmp_path / "notes"
    d.mkdir()
    (d / "long.md").write_text("\n\n".join(" ".join(["word"] * 120) for _ in range(5)))
    src = Markdown()
    locs = src.discover(me, {"paths": [str(d), str(tmp_path / "missing")]})
    assert len(locs) == 1
    units, _ = run(src, locs[0], None, me)
    assert [len(u.text.split()) for u in units] == [240, 240, 120]


def test_markdown_prunes_stale_units_when_a_paragraph_group_changes(home, me, tmp_path):
    """P2-17: a unit's id is derived from its paragraph group's own hash, so editing that group's content
    gives it a brand new unit_id -- the OLD one must not just linger in the store forever. The engine side
    (store.write_units' `prune`) already existed for this; the adapter must actually emit the hint."""
    from mannerism import store

    d = tmp_path / "notes"
    d.mkdir()
    p = d / "note.md"
    p.write_text("The original single paragraph about a trip plan, with quite a few words in it today.")
    src = Markdown()
    loc = src.discover(me, {"paths": [str(d)]})[0]
    con = store.connect()
    units, cur = run(src, loc, None, me)
    res = store.write_units(con, "markdown", loc.key, units, cur)
    assert res.new_ids
    old_id = res.new_ids[0]
    assert con.execute("SELECT count(*) FROM units WHERE unit_id=?", (old_id,)).fetchone()[0] == 1

    p.write_text("A completely rewritten paragraph about something else entirely, with different words today.")
    os.utime(p, (p.stat().st_atime + 5, p.stat().st_mtime + 5))
    units2, cur2 = run(src, loc, cur, me)
    cur2 = dict(cur2)
    prune = cur2.pop("_stale_prune", None)
    assert prune  # the adapter flagged this (re-read, changed) file's thread for pruning
    res2 = store.write_units(con, "markdown", loc.key, units2, cur2, prune=prune)
    assert res2.pruned == 1
    assert con.execute("SELECT count(*) FROM units WHERE unit_id=?", (old_id,)).fetchone()[0] == 0


def test_markdown_discover_never_raises(home, me):
    (home / ".config" / "obsidian").mkdir(parents=True)
    (home / ".config" / "obsidian" / "obsidian.json").write_text("{not json")
    assert Markdown().discover(me, {}) == []
    assert Markdown().discover(me, {"paths": [None]}) == []
