"""Desktop project-folder primitives: in-place open/save, bundle classification, and the
path tokenizers that make a project folder survive a move/rename (GMS-style workflow)."""
import json
import os
import zipfile
from pathlib import Path

import pytest

from hype_app import bundle

_FEATURE = {"type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {}}


def _make_workspace(root):
    (root / "inputs").mkdir(parents=True)
    (root / "inputs" / "dem.tif").write_bytes(b"FAKE-DEM")
    (root / "data_sources" / "usgs").mkdir(parents=True)
    (root / "data_sources" / "usgs" / "delineate.json").write_text('{"ok": true}')
    (root / "model" / "gwf_workspace").mkdir(parents=True)
    (root / "model" / "gwf_workspace" / "sim.nam").write_text("nam")
    (root / "ras").mkdir()
    (root / "ras" / "project.prj").write_text("RAS")


def _snapshot(root):
    """Every (path, mtime_ns, size) under root — proves a call wrote nothing."""
    out = {}
    for p in sorted(Path(root).rglob("*")):
        if p.is_file():
            st = p.stat()
            out[str(p)] = (st.st_mtime_ns, st.st_size)
    return out


# ---------------------------------------------------------------- restore_in_place

def test_restore_in_place_matches_restore_workspace(tmp_path):
    src = tmp_path / "session"
    src.mkdir()
    _make_workspace(src)
    state = {"format_version": 2, "sel_node": "gw.res", "desktop_project": True}
    zip_path = bundle.zip_workspace(
        src, vectors={"reach": _FEATURE, "k_zones": [_FEATURE]}, params={"kh": 7.5},
        run_config={"working_crs": {"epsg": 26919}}, state=state,
        assessment_input={"assessment_id": "A1"}, scoring_profile={"profile_id": "p"})

    dst = tmp_path / "extract_out"
    dst.mkdir()
    extracted = bundle.restore_workspace(zip_path, dst)
    in_place = bundle.restore_in_place(zip_path)

    for key in ("state", "vectors", "params", "run_config", "assessment_input",
                "scoring_profile"):
        assert in_place[key] == extracted[key], key
    assert in_place["extracted"] == 0
    assert in_place["restored"] is None


def test_restore_in_place_writes_nothing(tmp_path):
    folder = tmp_path / "SiteA"
    folder.mkdir()
    _make_workspace(folder)
    main = folder / "SiteA.hype"
    bundle.save_bundle_to(folder, main, vectors={"reach": _FEATURE},
                          state={"format_version": 2, "desktop_project": True})

    before = _snapshot(folder)
    bundle.restore_in_place(main)
    assert _snapshot(folder) == before


def test_restore_in_place_rejects_foreign_zip(tmp_path):
    bogus = tmp_path / "bogus.hype"
    with zipfile.ZipFile(bogus, "w") as zf:
        zf.writestr("random.txt", "nope")
    with pytest.raises(bundle.ProjectError):
        bundle.restore_in_place(bogus)


# ---------------------------------------------------------------- classify_bundle

def test_classify_bundle(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    marked = bundle.zip_workspace(ws, vectors={},
                                  state={"format_version": 2, "desktop_project": True},
                                  include_computed=False)
    plain = bundle.zip_workspace(ws, vectors={}, state={"format_version": 2},
                                 include_computed=False)
    try:
        assert bundle.classify_bundle(marked) == "project"
        assert bundle.classify_bundle(plain) == "standalone"
    finally:
        os.unlink(marked)
        os.unlink(plain)

    corrupt = tmp_path / "corrupt.hype"
    corrupt.write_bytes(b"not a zip at all")
    with pytest.raises(bundle.ProjectError):
        bundle.classify_bundle(corrupt)


# ---------------------------------------------------------------- save_bundle_to

def test_save_bundle_to_writes_openable_project(tmp_path):
    folder = tmp_path / "SiteA"
    folder.mkdir()
    _make_workspace(folder)
    main = folder / "SiteA.hype"
    bundle.save_bundle_to(folder, main, vectors={"reach": _FEATURE},
                          state={"format_version": 2, "desktop_project": True,
                                 "sel_node": "reach"})
    assert main.is_file()
    assert bundle.classify_bundle(main) == "project"
    out = bundle.restore_in_place(main)
    assert out["state"]["sel_node"] == "reach"
    assert out["vectors"]["reach"] == _FEATURE


def test_save_bundle_to_overwrites_atomically_no_residue(tmp_path):
    folder = tmp_path / "SiteA"
    folder.mkdir()
    main = folder / "SiteA.hype"
    bundle.save_bundle_to(folder, main, vectors={},
                          state={"format_version": 2, "desktop_project": True, "rev": 1})
    first = main.read_bytes()
    bundle.save_bundle_to(folder, main, vectors={},
                          state={"format_version": 2, "desktop_project": True, "rev": 2})
    assert main.read_bytes() != first
    assert bundle.restore_in_place(main)["state"]["rev"] == 2
    # atomic swap leaves no sidecar temp behind
    assert list(folder.glob("*.tmp")) == []


def test_save_bundle_to_swaps_via_sibling(tmp_path, monkeypatch):
    """The zip is staged NEXT TO the target and swapped with a same-directory os.replace —
    the cross-volume-safe design (zip_workspace's temp file lives on %TEMP%)."""
    folder = tmp_path / "SiteA"
    folder.mkdir()
    main = folder / "SiteA.hype"
    calls = []
    real_replace = os.replace

    def spy(src, dst, *a, **kw):
        calls.append((Path(src), Path(dst)))
        return real_replace(src, dst, *a, **kw)

    monkeypatch.setattr(bundle.os, "replace", spy)
    bundle.save_bundle_to(folder, main, vectors={},
                          state={"format_version": 2, "desktop_project": True})
    swap = [c for c in calls if c[1] == main]
    assert swap, "no os.replace onto the target"
    src, dst = swap[-1]
    assert src.parent == dst.parent          # same directory => same volume => atomic
    assert src.name == main.name + ".tmp"


# ---------------------------------------------------------------- tokenizers

def test_tokenize_roundtrip_across_bases(tmp_path):
    """A project moved to a different folder (or machine) must re-land every stored path
    under the new root — the sens/soil/flow shapes are exactly what state.json carries."""
    base_a = tmp_path / "old" / "SiteA"
    base_b = tmp_path / "new_home" / "SiteA_renamed"
    obj = {
        "dir": str(base_a / "sensitivity" / "s01"),
        "hz_dir": str(base_a / "sensitivity" / "s01" / "hz_workspace"),
        "artifact_paths": {"head": str(base_a / "summary" / "head" / "head_L1.tif")},
        "raw_response_paths": [str(base_a / "data_sources" / "usgs" / "delineate.json")],
        "n": 3, "label": "Slight", "nested": [{"p": str(base_a / "inputs" / "dem.tif")}],
    }
    tok = bundle.tokenize_paths(obj, base_a)
    flat = json.dumps(tok)
    assert str(base_a) not in flat                       # nothing absolute survived
    assert flat.count(bundle.WS_TOKEN) == 5

    back = bundle.detokenize_paths(tok, base_b)
    assert back["dir"] == str(base_b / "sensitivity" / "s01")
    assert back["hz_dir"] == str(base_b / "sensitivity" / "s01" / "hz_workspace")
    assert back["artifact_paths"]["head"] == str(base_b / "summary" / "head" / "head_L1.tif")
    assert back["raw_response_paths"] == [str(base_b / "data_sources" / "usgs" / "delineate.json")]
    assert back["nested"][0]["p"] == str(base_b / "inputs" / "dem.tif")
    assert back["n"] == 3 and back["label"] == "Slight"


def test_tokenize_leaves_foreign_paths_alone(tmp_path):
    base = tmp_path / "ws"
    foreign = str(Path(tmp_path.anchor) / "somewhere" / "else.tif")
    obj = {"inside": str(base / "inputs" / "dem.tif"), "outside": foreign, "rel": "inputs/x"}
    tok = bundle.tokenize_paths(obj, base)
    assert tok["inside"].startswith(bundle.WS_TOKEN + "/")
    assert tok["outside"] == foreign
    assert tok["rel"] == "inputs/x"
    back = bundle.detokenize_paths(tok, base)
    assert back == obj


# ---------------------------------------------------------------- self-zip regression

def test_main_hype_never_enters_its_own_zip(tmp_path):
    """The main .hype sits at the project-folder root; zip_workspace sweeps only named
    subtrees, so a Complete export from a project folder must not swallow the main file
    (or any other root-level stray)."""
    folder = tmp_path / "SiteA"
    folder.mkdir()
    _make_workspace(folder)
    (folder / "SiteA.hype").write_bytes(b"PK\x05\x06" + b"\x00" * 18)   # empty-zip stub
    (folder / "notes.txt").write_text("user's own file")

    out = bundle.zip_workspace(folder, vectors={"reach": _FEATURE},
                               state={"format_version": 2}, include_computed=True)
    try:
        names = zipfile.ZipFile(out).namelist()
    finally:
        os.unlink(out)
    assert not any("SiteA.hype" in n for n in names)
    assert not any("notes.txt" in n for n in names)
    assert any(n.endswith("sim.nam") for n in names)      # the real content did go in


# ---------------------------------------------------------------- folder_clash

def test_folder_clash_empty_and_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert bundle.folder_clash(empty, empty / "New.hype") == ([], False)
    # the subfolder-recursion target: neither folder nor main file exists yet
    missing = tmp_path / "nope"
    assert bundle.folder_clash(missing, missing / "New.hype") == ([], False)


def test_folder_clash_detects_content_dirs(tmp_path):
    folder = tmp_path / "SiteA"
    folder.mkdir()
    _make_workspace(folder)
    names, foreign = bundle.folder_clash(folder, folder / "SiteA.hype")
    assert names == ["inputs", "model", "ras", "data_sources"]   # PROJECT_DIRS order
    assert foreign is False


def test_folder_clash_foreign_hype_excludes_target(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "SiteA.hype").write_bytes(b"zip")
    (folder / "Other.hype").write_bytes(b"zip")
    # existing target excluded, sibling reported
    assert bundle.folder_clash(folder, folder / "SiteA.hype") == (["Other.hype"], True)
    # nonexistent target: both on-disk projects are foreign, sorted
    assert bundle.folder_clash(folder, folder / "New.hype") == \
        (["Other.hype", "SiteA.hype"], True)


def test_folder_clash_suffix_case_insensitive(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "UPPER.HYPE").write_bytes(b"zip")
    assert bundle.folder_clash(folder, folder / "New.hype") == (["UPPER.HYPE"], True)


@pytest.mark.skipif(os.name != "nt", reason="case-insensitive path identity is NTFS behavior")
def test_folder_clash_target_excluded_across_casing(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "SiteA.hype").write_bytes(b"zip")
    # differently-cased reference to the same file must not count as a foreign project
    assert bundle.folder_clash(folder, folder / "sitea.HYPE") == ([], False)


def test_folder_clash_ignores_hype_named_directory(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "Not.hype").mkdir()
    assert bundle.folder_clash(folder, folder / "New.hype") == ([], False)


def test_folder_clash_orders_dirs_then_sorted_hypes(tmp_path):
    folder = tmp_path / "shared"
    folder.mkdir()
    (folder / "inputs").mkdir()
    (folder / "b.hype").write_bytes(b"zip")
    (folder / "a.hype").write_bytes(b"zip")
    assert bundle.folder_clash(folder, folder / "New.hype") == \
        (["inputs", "a.hype", "b.hype"], True)


def test_project_dirs_match_restore_layout():
    """PROJECT_DIRS is the folder-layout contract: it must stay the union of the restore
    targets' first path components, or the clash check goes blind to a new dir."""
    derived = {v.split("/")[0] for v in bundle._RESTORE_FILES.values()}
    derived |= {dest.split("/")[0] for _, dest in bundle._RESTORE_TREES}
    assert set(bundle.PROJECT_DIRS) == derived
    assert len(set(bundle.PROJECT_DIRS)) == len(bundle.PROJECT_DIRS)


def test_clash_subfolder_derivation(tmp_path):
    assert bundle.clash_subfolder(tmp_path / "Mink.hype") == \
        tmp_path / "Mink" / "Mink.hype"
    # Win32 strips trailing dots/spaces on dir create — the folder component pre-strips
    assert bundle.clash_subfolder(tmp_path / "A..hype") == tmp_path / "A" / "A..hype"
    assert bundle.clash_subfolder(tmp_path / "Site .hype") == \
        tmp_path / "Site" / "Site .hype"
    assert bundle.clash_subfolder(tmp_path / "....hype") == \
        tmp_path / "Project" / "....hype"


# ---------------------------------------------------------------- copy_project_tree (Save As)

def test_copy_project_tree_copies_only_contract_dirs(tmp_path):
    src = tmp_path / "SiteA"
    src.mkdir()
    _make_workspace(src)
    (src / "SiteA.hype").write_bytes(b"zip")
    (src / "notes.txt").write_text("user's own file")
    (src / "scene").mkdir()
    (src / "scene" / "drape.png").write_bytes(b"PNG")

    dst = tmp_path / "SiteB"
    dst.mkdir()
    copied = bundle.copy_project_tree(src, dst)

    assert copied == ["inputs", "model", "ras", "data_sources"]   # PROJECT_DIRS order
    assert (dst / "inputs" / "dem.tif").read_bytes() == b"FAKE-DEM"
    assert (dst / "model" / "gwf_workspace" / "sim.nam").is_file()
    # the old main file, transient scene/, and user strays never travel
    assert not (dst / "SiteA.hype").exists()
    assert not (dst / "notes.txt").exists()
    assert not (dst / "scene").exists()


def test_copy_project_tree_merges_into_existing_dirs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_workspace(src)
    dst = tmp_path / "dst"
    (dst / "inputs").mkdir(parents=True)
    (dst / "inputs" / "keepme.txt").write_text("old")

    bundle.copy_project_tree(src, dst)

    assert (dst / "inputs" / "keepme.txt").read_text() == "old"
    assert (dst / "inputs" / "dem.tif").read_bytes() == b"FAKE-DEM"


def test_copy_project_tree_rolls_back_fresh_dirs_only(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _make_workspace(src)                       # inputs, model, ras, data_sources
    dst = tmp_path / "dst"
    (dst / "inputs").mkdir(parents=True)       # pre-existing: must survive a failure
    (dst / "inputs" / "keepme.txt").write_text("old")

    real = bundle.shutil.copytree

    def flaky(s, d, *a, **kw):                 # *a: copytree recurses through this name
        if Path(s).name == "ras":              # inputs, model succeed; ras blows up
            raise OSError("disk full")
        return real(s, d, *a, **kw)

    monkeypatch.setattr(bundle.shutil, "copytree", flaky)
    with pytest.raises(OSError):
        bundle.copy_project_tree(src, dst)

    # fresh dirs rolled back; the pre-existing dir keeps both its old file and whatever
    # merged in before the failure (partial by design — the clash dialog warned)
    assert not (dst / "model").exists()
    assert not (dst / "ras").exists()
    assert (dst / "inputs" / "keepme.txt").read_text() == "old"


def test_save_as_shape_end_to_end(tmp_path):
    """Bundle-level rehearsal of desktop Save As: copy the tree, write a fresh main file
    under the new name, and prove the source project is byte-for-byte untouched."""
    src = tmp_path / "SiteA"
    src.mkdir()
    _make_workspace(src)
    src_main = src / "SiteA.hype"
    bundle.save_bundle_to(src, src_main, vectors={"reach": _FEATURE},
                          state={"format_version": 2, "desktop_project": True,
                                 "project_name": "SiteA", "project_units": "metric",
                                 "project_created": "2026-07-23T08:00:00"})
    before = _snapshot(src)

    dst = tmp_path / "SiteB"
    dst.mkdir()
    bundle.copy_project_tree(src, dst)
    dst_main = dst / "SiteB.hype"
    bundle.save_bundle_to(dst, dst_main, vectors={"reach": _FEATURE},
                          state={"format_version": 2, "desktop_project": True,
                                 "project_name": "SiteB", "project_units": "metric",
                                 "project_created": "2026-07-23T08:00:00"})

    assert bundle.classify_bundle(dst_main) == "project"
    out = bundle.restore_in_place(dst_main)
    assert out["state"]["project_name"] == "SiteB"
    assert not (dst / "SiteA.hype").exists()
    assert (dst / "inputs" / "dem.tif").read_bytes() == b"FAKE-DEM"
    assert _snapshot(src) == before
