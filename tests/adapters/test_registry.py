import pytest

from mannerism.adapters import REGISTRY, all_sources, load


def test_registry_loads_all_sources():
    names = [s.name for s in all_sources()]
    assert names == list(REGISTRY)
    for n in REGISTRY:
        s = load(n)
        assert s.name == n and s.tier in ("auto", "export", "macos")


@pytest.mark.parametrize("name", list(REGISTRY))
def test_discover_never_raises(name, home, me):
    src = load(name)
    assert src.discover(me, {}) == [] or isinstance(src.discover(me, {}), list)
    junk = {"paths": [None, 5, "/nonexistent/x", "\x00bad"], "roots": [None, 7, "/nonexistent/y"]}
    assert isinstance(src.discover(me, junk), list)
    assert isinstance(src.discover(me, None), list)


@pytest.mark.parametrize("name", ["telegram", "whatsapp", "chatgpt", "x_archive", "mbox"])
def test_export_sources_empty_without_files(name, home, me):
    assert load(name).discover(me, {}) == []
    assert not (home / ".mannerism").exists()  # discover creates nothing
