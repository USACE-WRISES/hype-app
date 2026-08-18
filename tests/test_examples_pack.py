"""tools/examples/pack.py — a desktop project folder becomes a publishable example bundle.

Pins the token-space scrub (desktop_project gone, aerial map-layer rows gone, format
version current), the complete-bundle scope (results travel, aerials never), the sidecar the
catalog is built from, and --verify's hydration checks. Synthetic project only.
"""
from __future__ import annotations

import hashlib
import json

import pytest

from hype_app import bundle, examples
from tests.test_bundle import _FEATURE, _make_workspace
from tools.examples import pack


def _project(tmp_path):
    """A desktop project folder: settings-only main file + sibling content dirs + aerials."""
    folder = tmp_path / "SITE1"
    folder.mkdir()
    _make_workspace(folder)
    (folder / "summary" / "hz").mkdir(parents=True)
    (folder / "summary" / "hz" / "hz_stats.json").write_text("{}")
    (folder / "aerials").mkdir()
    (folder / "aerials" / "NAIP.tif").write_bytes(b"IMG")
    (folder / "GMS").mkdir()
    (folder / "GMS" / "site.gpr").write_bytes(b"GPR")
    state = {"format_version": 1, "desktop_project": True, "project_name": "SITE1",
             "map_layers": [{"id": "a", "path": "$WORKSPACE$/aerials/NAIP.tif", "kind": "raster"},
                            {"id": "b", "path": "$WORKSPACE$/inputs/other.tif", "kind": "raster"}],
             "dem_source": {"kind": "file", "path": "D:/somewhere/dem.tif"},
             "hz_result": {"hz_dir": "$WORKSPACE$/summary/hz"}}
    main = folder / "SITE1.hype"
    bundle.save_bundle_to(folder, main, vectors={"reach": _FEATURE}, state=state,
                          params={"k": 1})
    return folder, main


def test_scrub_state_is_pure_and_targeted():
    st = {"desktop_project": True, "format_version": 1, "keep": 1,
          "map_layers": [{"path": "$WORKSPACE$/aerials/x.tif"}, {"path": "$WORKSPACE$/inputs/y.tif"}],
          "dem_source": {"kind": "3dep"}}
    out = pack.scrub_state(st)
    assert "desktop_project" not in out and st["desktop_project"] is True   # input untouched
    assert out["format_version"] == bundle.FORMAT_VERSION
    assert out["map_layers"] == [{"path": "$WORKSPACE$/inputs/y.tif"}]
    assert out["dem_source"] == {"kind": "3dep"}                # a service pointer stays
    assert out["keep"] == 1
    assert "dem_source" not in pack.scrub_state({"dem_source": {"kind": "file", "path": "x"}})


def test_pack_verify_and_sidecar(tmp_path):
    folder, main = _project(tmp_path)
    out = tmp_path / "out"
    rc = pack.main([str(folder), "--out", str(out), "--id", "SITE1", "--title", "Site One, SITE1",
                    "--description", "d", "--tags", "a, b", "--credit", "c", "--verify"])
    assert rc == 0
    dest = out / "SITE1.hype"
    assert dest.is_file()
    row = json.loads((out / "SITE1.json").read_text(encoding="utf-8"))
    assert row["id"] == "SITE1" and row["tags"] == ["a", "b"]
    assert row["size_bytes"] == dest.stat().st_size
    assert row["sha256"] == hashlib.sha256(dest.read_bytes()).hexdigest()
    assert row["url"] == examples.EXAMPLES_URL_PREFIX + "examples-1/SITE1.hype"
    assert row["thumbnail"] == "examples/SITE1.jpg"
    assert row["format_version"] == bundle.FORMAT_VERSION
    # the row (minus app_version) is a valid catalog entry once the thumbnail exists
    www = tmp_path / "www"
    (www / "examples").mkdir(parents=True)
    (www / "examples" / "SITE1.jpg").write_bytes(b"x")
    row.pop("app_version")
    ex = examples.parse_catalog(json.dumps({"schema": 1, "examples": [row]}), www=www)[0]
    assert ex.title == "Site One, SITE1"

    # what travelled: results yes, aerials + GMS never, state scrubbed
    v = pack.verify(dest)
    assert v["ok"], v
    scratch = tmp_path / "scratch"
    payload = bundle.restore_workspace(dest, scratch)
    assert "desktop_project" not in payload["state"]
    assert payload["state"]["format_version"] == bundle.FORMAT_VERSION
    assert [r["path"] for r in payload["state"]["map_layers"]] == ["$WORKSPACE$/inputs/other.tif"]
    assert "dem_source" not in payload["state"]
    assert "model/gwf_workspace/sim.nam" in payload["restored"]
    assert "summary/hz/hz_stats.json" in payload["restored"]
    assert "report/report.html" in payload["restored"]
    assert not any(p.startswith("aerials/") or p.startswith("GMS/") for p in payload["restored"])
    assert bundle.classify_bundle(dest) == "standalone"      # never opens the cache dir in place


def test_find_main_file_requires_exactly_one(tmp_path):
    folder, main = _project(tmp_path)
    assert pack.find_main_file(folder) == main
    assert pack.find_main_file(main) == main
    (folder / "Other.hype").write_bytes(b"PK\x05\x06" + b"\0" * 18)
    with pytest.raises(SystemExit):
        pack.find_main_file(folder)
