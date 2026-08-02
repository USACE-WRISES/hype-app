"""hype_app.recents — the desktop welcome dialog's recent-projects store.

Everything must be non-fatal: a broken data root degrades to an empty list, never an
exception (recents.touch runs inside project adoption)."""
import json
import os

import pytest

from hype_app import recents


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPE_DATA_ROOT", str(tmp_path))
    return tmp_path


def _mk(tmp_path, name):
    p = tmp_path / f"{name}.hype"
    p.write_bytes(b"PK\x05\x06" + b"\0" * 18)      # minimal empty zip
    return p


def test_touch_then_load_roundtrip(root, tmp_path):
    p = _mk(tmp_path, "SiteA")
    recents.touch(p)
    items = recents.load()
    assert len(items) == 1
    assert items[0]["name"] == "SiteA"
    assert os.path.normcase(items[0]["path"]) == os.path.normcase(str(p.resolve()))
    assert items[0]["last_opened"]              # iso stamp present
    assert (root / "recent_projects.json").is_file()


def test_dedupe_moves_to_front(root, tmp_path):
    a, b = _mk(tmp_path, "A"), _mk(tmp_path, "B")
    recents.touch(a)
    recents.touch(b)
    recents.touch(a)                             # re-open A -> front, no duplicate
    names = [it["name"] for it in recents.load()]
    assert names == ["A", "B"]


def test_cap_at_max(root, tmp_path):
    for i in range(recents.MAX_RECENTS + 5):
        recents.touch(_mk(tmp_path, f"p{i:02d}"))
    items = recents.load()
    assert len(items) == recents.MAX_RECENTS
    assert items[0]["name"] == f"p{recents.MAX_RECENTS + 4:02d}"   # newest first


def test_load_prunes_missing_files(root, tmp_path):
    keep, gone = _mk(tmp_path, "keep"), _mk(tmp_path, "gone")
    recents.touch(keep)
    recents.touch(gone)
    gone.unlink()
    assert [it["name"] for it in recents.load()] == ["keep"]
    # Prune is display-only: the file still holds both until the next touch rewrites it.
    raw = json.loads((root / "recent_projects.json").read_text(encoding="utf-8"))
    assert len(raw["projects"]) == 2


def test_forget_removes_entry(root, tmp_path):
    a, b = _mk(tmp_path, "A"), _mk(tmp_path, "B")
    recents.touch(a)
    recents.touch(b)
    recents.forget(a)
    assert [it["name"] for it in recents.load()] == ["B"]
    # Unlike load()'s display-only prune, forget rewrites the store.
    raw = json.loads((root / "recent_projects.json").read_text(encoding="utf-8"))
    assert [it["name"] for it in raw["projects"]] == ["B"]


def test_forget_unknown_path_keeps_list(root, tmp_path):
    recents.touch(_mk(tmp_path, "A"))
    recents.forget(tmp_path / "never-added.hype")
    assert [it["name"] for it in recents.load()] == ["A"]


def test_forget_with_no_store_is_nonfatal(root, tmp_path):
    recents.forget(tmp_path / "whatever.hype")   # must not raise
    assert recents.load() == []


@pytest.mark.skipif(os.path.normcase("A") == "A",
                    reason="needs a case-folding path convention (Windows)")
def test_forget_matches_case_insensitively(root, tmp_path):
    p = _mk(tmp_path, "Site")
    recents.touch(p)
    recents.forget(str(p).upper())
    assert recents.load() == []


def test_atomic_write_leaves_no_tmp_litter(root, tmp_path):
    recents.touch(_mk(tmp_path, "A"))
    recents.touch(_mk(tmp_path, "B"))
    assert not list(root.glob(".recents-*")), "tmp files must be renamed or removed"


def test_corrupt_file_is_survivable(root, tmp_path):
    (root / "recent_projects.json").write_text("{not json", encoding="utf-8")
    assert recents.load() == []
    recents.touch(_mk(tmp_path, "A"))            # touch heals the store
    assert [it["name"] for it in recents.load()] == ["A"]


def test_touch_is_nonfatal_on_broken_root(monkeypatch, tmp_path):
    # Point the root AT A FILE so mkdir/replace must fail — touch must swallow it.
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    monkeypatch.setenv("HYPE_DATA_ROOT", str(blocker))
    recents.touch(_mk(tmp_path, "A"))            # must not raise
    assert recents.load() == []


def test_data_root_resolution_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPE_DATA_ROOT", str(tmp_path))
    assert recents.data_root() == tmp_path
    monkeypatch.delenv("HYPE_DATA_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
    assert recents.data_root() == tmp_path / "lad" / "HYPE"
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    assert recents.data_root().name == ".hype"   # home fallback (non-Windows dev shells)
