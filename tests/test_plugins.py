"""Entry-point plugins: discovery from metadata only, import after `enable`, collisions, and the quick-sync rule."""
from __future__ import annotations

import importlib
import json
import sys
import textwrap

import pytest

from mannerism import adapters, cli, config, plugins, store, sync

PLUGIN_SRC = textwrap.dedent('''
    from datetime import UTC, datetime
    from mannerism.core import Location, Unit

    IMPORTED = True

    class FakeNotes:
        name = "fake-notes"
        tier = "export"

        def discover(self, me, config):
            return [Location(self.name, "feed", None, "fake feed")]

        def read(self, loc, cursor, me):
            if (cursor or {}).get("done"):
                return [], cursor
            u = Unit(self.name, "n1", datetime(2026, 10, 1, tzinfo=UTC), "a note from the plugin about plans for friday",
                     "note", register="work")
            return [u], {"done": True}

    class WrongName(FakeNotes):
        name = "something-else"
''')


def make_dist(site, dist, entry_points, version="1.0", modules=None):
    info = site / f"{dist.replace('-', '_')}-{version}.dist-info"  # wheel naming: no hyphen in the directory
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    body = "[mannerism.sources.v1]\n" + "".join(f"{k} = {v}\n" for k, v in entry_points.items())
    (info / "entry_points.txt").write_text(body)
    for mod, src in (modules or {}).items():
        (site / f"{mod}.py").write_text(src)


@pytest.fixture
def site(tmp_path, monkeypatch):
    """A temporary site directory on sys.path; its modules are forgotten again after the test."""
    d = tmp_path / "site"
    d.mkdir()
    monkeypatch.syspath_prepend(str(d))
    importlib.invalidate_caches()
    before = set(sys.modules)
    yield d
    for m in set(sys.modules) - before:
        if m.startswith("fakepkg"):
            del sys.modules[m]


def test_listing_reads_metadata_and_imports_nothing(site, capsys):
    make_dist(site, "fakepkg", {"fake-notes": "fakepkg_mod:FakeNotes"}, modules={"fakepkg_mod": PLUGIN_SRC})
    found = plugins.discover()
    assert [(p.name, p.value, p.dist, p.status) for p in found] == [("fake-notes", "fakepkg_mod:FakeNotes", "fakepkg", "ok")]
    assert "fakepkg_mod" not in sys.modules
    assert cli.main(["sources"]) == 0
    out = capsys.readouterr().out
    assert "fake-notes" in out and "plugin" in out and "not loaded until" in out
    assert "fakepkg_mod" not in sys.modules
    assert cli.main(["sources", "--json"]) == 0
    assert "fakepkg_mod:FakeNotes" in capsys.readouterr().out and "fakepkg_mod" not in sys.modules


def test_enable_imports_once_records_the_pin_and_load_then_works(site, capsys):
    make_dist(site, "fakepkg", {"fake-notes": "fakepkg_mod:FakeNotes"}, modules={"fakepkg_mod": PLUGIN_SRC})
    with pytest.raises(plugins.PluginError, match="not enabled"):
        plugins.load("fake-notes", {})
    assert "fakepkg_mod" not in sys.modules
    assert cli.main(["sources", "enable", "fake-notes"]) == 0
    out = capsys.readouterr().out
    assert "runs with your permissions" in out or "run with your permissions" in out
    assert "fakepkg_mod" in sys.modules
    entry = config.load_config().sources["fake-notes"]
    assert entry["enabled"] is True and entry["plugin"] == "fakepkg_mod:FakeNotes"
    adapter = adapters.load_for("fake-notes", entry)
    assert adapter.name == "fake-notes"


def test_a_full_sync_runs_the_plugin_and_a_quick_sync_never_imports_it(site, monkeypatch):
    make_dist(site, "fakepkg", {"fake-notes": "fakepkg_mod:FakeNotes"}, modules={"fakepkg_mod": PLUGIN_SRC})
    cli.main(["sources", "enable", "fake-notes"])
    cfg = config.load_config()
    cfg.sources["fake-notes"]["run_on_session_start"] = True  # a plugin is launched at session start only if asked
    config.save_config(cfg)
    for m in [m for m in sys.modules if m.startswith("fakepkg")]:
        del sys.modules[m]  # as in a fresh process
    started = []
    monkeypatch.setattr(sync, "sync_in_background", lambda **kw: started.append(kw) or {"started": True})
    quick = sync.sync(quick=True)
    assert "fakepkg_mod" not in sys.modules
    assert quick["sources"]["fake-notes"]["status"] == "skipped_quick" and quick["new_units"] == 0
    assert started == [{"quick": False, "sources": ["fake-notes"]}]  # at most: start the background sync
    full = sync.sync()
    assert "fakepkg_mod" in sys.modules and full["sources"]["fake-notes"]["new_units"] == 1
    con = store.connect()
    assert con.execute("SELECT count(*) FROM units WHERE source='fake-notes'").fetchone()[0] == 1


def test_names_must_not_collide_with_built_ins_or_each_other(site, capsys):
    make_dist(site, "pkg-a", {"markdown": "fakepkg_a:X", "dup": "fakepkg_a:X", "ok-one": "fakepkg_a:X", "Bad Name": "fakepkg_a:X"})
    make_dist(site, "pkg-b", {"dup": "fakepkg_b:Y"})
    by = {p.name + ":" + p.dist: p for p in plugins.discover()}
    assert by["markdown:pkg-a"].status == "builtin_name"
    assert by["dup:pkg-a"].status == "ambiguous" and by["dup:pkg-b"].status == "ambiguous"
    assert by["ok-one:pkg-a"].status == "ok"
    assert by["Bad Name:pkg-a"].status == "bad_name"
    assert plugins.names() >= {"dup", "ok-one", "markdown"}
    with pytest.raises(plugins.PluginError, match="ambiguous|refused"):
        plugins.find("dup")
    assert cli.main(["sources", "enable", "dup"]) == 2
    assert "refused" in capsys.readouterr().err
    assert "dup" not in config.load_config().sources or not config.load_config().sources["dup"].get("enabled")
    assert cli.main(["sources"]) == 0
    assert "ambiguous" in capsys.readouterr().out
    assert "fakepkg_a" not in sys.modules and "fakepkg_b" not in sys.modules


def test_enable_refuses_a_plugin_that_is_not_a_source_or_misnamed(site, capsys):
    make_dist(site, "fakepkg", {"fake-notes": "fakepkg_mod:FakeNotes", "wrong": "fakepkg_mod:WrongName", "nothing": "fakepkg_mod:IMPORTED",
                                "missing": "fakepkg_nomodule:X"}, modules={"fakepkg_mod": PLUGIN_SRC})
    for name, msg in (("wrong", "must equal the entry point name"), ("nothing", "not a Source"), ("missing", "failed to import")):
        assert cli.main(["sources", "enable", name]) == 2
        assert msg in capsys.readouterr().err
        assert not config.load_config().sources.get(name, {}).get("enabled")


def test_a_plugin_is_never_launched_at_session_start_by_default(site, monkeypatch):
    make_dist(site, "fakepkg", {"fake-notes": "fakepkg_mod:FakeNotes"}, modules={"fakepkg_mod": PLUGIN_SRC})
    cli.main(["sources", "enable", "fake-notes"])
    started = []
    monkeypatch.setattr(sync, "sync_in_background", lambda **kw: started.append(kw) or {"started": True})
    sync.sync(quick=True)
    assert started == []


def test_a_plugin_is_pinned_by_distribution_a_new_version_warns_a_new_distribution_refuses(site, capsys):
    make_dist(site, "fakepkg", {"fake-notes": "fakepkg_mod:FakeNotes"}, modules={"fakepkg_mod": PLUGIN_SRC})
    cli.main(["sources", "enable", "fake-notes"])
    entry = config.load_config().sources["fake-notes"]
    assert entry["plugin_dist"] == "fakepkg" and entry["plugin_version"] == "1.0"
    for m in [m for m in sys.modules if m.startswith("fakepkg")]:
        del sys.modules[m]
    # same distribution, new version and a moved target: loads, with a warning
    import shutil
    shutil.rmtree(site / "fakepkg-1.0.dist-info")
    make_dist(site, "fakepkg", {"fake-notes": "fakepkg_mod:Other"}, version="2.0",
              modules={"fakepkg_mod": PLUGIN_SRC + "\nclass Other(FakeNotes):\n    pass\n"})
    importlib.invalidate_caches()
    warns = []
    assert plugins.load("fake-notes", entry, warnings=warns).name == "fake-notes"
    assert len(warns) == 1 and "1.0 -> 2.0" in warns[0] and "target" in warns[0]
    res = sync.sync()
    assert res["sources"]["fake-notes"]["new_units"] == 1 and "1.0 -> 2.0" in res["sources"]["fake-notes"]["note"]
    # another distribution now provides the name: refused until the user enables it again
    shutil.rmtree(site / "fakepkg-2.0.dist-info")
    make_dist(site, "evilpkg", {"fake-notes": "fakepkg_mod:FakeNotes"})
    importlib.invalidate_caches()
    with pytest.raises(plugins.PluginError, match="'evilpkg'.*not the 'fakepkg' you enabled|not the 'fakepkg'"):
        plugins.load("fake-notes", entry)
    assert sync.sync()["sources"]["fake-notes"]["status"] == "adapter_missing"


MALICIOUS = textwrap.dedent('''
    from datetime import UTC, datetime
    from mannerism.core import ContextLine, Location, Unit

    class Leaky:
        name = "leaky"
        tier = "export"

        def discover(self, me, config):
            return [Location(self.name, "feed", None, "feed")]

        def read(self, loc, cursor, me):
            u = Unit(self.name, "p1", datetime(2026, 10, 1, tzinfo=UTC),
                     "mail me at jo@example.org or call +44 20 7946 0958 about the launch plan today",
                     "post", register="public", audience="public", meta={"public": True, "keep": 1},
                     context=(ContextLine("other1", "ping a@b.org please"),))
            return [u], {}
''')


def test_plugin_units_are_masked_by_the_engine_and_can_never_be_public(site):
    make_dist(site, "fakepkg", {"leaky": "fakepkg_leak:Leaky"}, modules={"fakepkg_leak": MALICIOUS})
    assert cli.main(["sources", "enable", "leaky"]) == 0
    res = sync.sync()
    assert res["sources"]["leaky"]["new_units"] == 1
    con = store.connect()
    row = con.execute("SELECT text, meta_json, context_json FROM units WHERE source='leaky'").fetchone()
    assert "jo@example.org" not in row["text"] and "[email]" in row["text"] and "[phone]" in row["text"]
    assert "a@b.org" not in row["context_json"]
    meta = json.loads(row["meta_json"])
    assert "public" not in meta and meta == {"keep": 1}
    assert con.execute("SELECT count(*) FROM bank WHERE register='public'").fetchone()[0] == 0


def test_an_uninstalled_plugin_fails_that_source_only(site, fakes):
    cfg = config.load_config()
    cfg.sources["gone"] = {"enabled": True, "plugin": "fakepkg_mod:FakeNotes"}
    cfg.sources["fakechat"] = {"enabled": True, "paths": []}
    config.save_config(cfg)
    res = sync.sync()
    assert res["sources"]["gone"]["status"] == "adapter_missing" and "no longer installed" in res["sources"]["gone"]["errors"][0]
    assert res["sources"]["fakechat"]["new_units"] == 195


def test_instance_names_cannot_take_a_plugin_or_built_in_name(site, tmp_path, capsys):
    make_dist(site, "fakepkg", {"fake-notes": "fakepkg_mod:FakeNotes"}, modules={"fakepkg_mod": PLUGIN_SRC})
    d = tmp_path / "d"
    d.mkdir()
    for name in ("fake-notes", "markdown", "files"):
        assert cli.main(["sources", "add", "files", str(d), "--name", name, "--yes"]) == 2
        assert "already the name" in capsys.readouterr().err
    assert config.load_config().sources == {}
