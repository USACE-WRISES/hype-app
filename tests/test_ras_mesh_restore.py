"""The 2D mesh overlay survives project reopen.

The "RAS mesh" overlay used to exist only in the session that ran the mesher: the preview
dict was deliberately never saved, restore never rebuilt it, and the tree's "2D mesh" box
restored checked over nothing (and even a built preview hid off the Surface step). The fix
rasterizes the mesh already inside the saved workspace's ras/Geometries/Geometry.h5 at
restore time (mesh_preview_from_h5, no mesher rerun) and makes the overlay checkbox-driven.
These tests pin the helper's math on a synthetic geometry HDF, the from_h5 dispatch, and
the app wiring.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from hype_app import ras

ROOT = Path(__file__).resolve().parents[1]
APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")
RAS_SRC = (ROOT / "hype_app" / "ras.py").read_text(encoding="utf-8")


@pytest.fixture()
def geometry_h5(tmp_path):
    """Minimal RAS geometry HDF with the datasets mesh_preview_from_h5 reads. Coordinates
    are EPSG:5070 metres near the projection's central meridian, so the 4326 back-transform
    lands at plausible CONUS lon/lat."""
    import h5py

    path = tmp_path / "Geometry.h5"
    nodes = np.array([[0.0, 1_000_000.0], [50.0, 1_000_000.0], [100.0, 1_000_000.0],
                      [0.0, 1_000_060.0], [50.0, 1_000_060.0], [100.0, 1_000_060.0]])
    faces = np.array([
        [0, 1, 0, 1],       # valid
        [1, 2, 1, 2],       # valid
        [2, 3, 3, 4],       # valid
        [3, -1, -1, 4],     # NodeA -1: perimeter sentinel, must be masked out
        [4, 5, 2, 99],      # NodeB out of range, masked out
    ], dtype=np.int64)
    att = np.zeros(1, dtype=[("Cell Count", "<i4"), ("Face Count", "<i4")])
    att["Cell Count"][0] = 4
    att["Face Count"][0] = len(faces)
    with h5py.File(path, "w") as f:
        g = f.create_group("Geometry/2D Flow Areas")
        g.create_dataset("Attributes", data=att)
        m = g.create_group("Mesh")
        m.create_dataset("Node Coordinates", data=nodes)
        m.create_dataset("Face Data", data=faces)
    return path


def test_from_h5_counts_mask_and_overlay(geometry_h5):
    res = ras.mesh_preview_from_h5(geometry_h5, 12.5, crs="EPSG:5070",
                                   log=lambda *_: None)
    assert res["cell_count"] == 4
    assert res["n_faces"] == 3            # the -1 and out-of-range rows are masked
    assert res["too_big"] is False
    assert res["cell_size_m"] == 12.5
    ov = res["overlay"]
    assert ov["url"].startswith("data:image/png;base64,")
    (s, w), (n, e) = ov["bounds"]
    assert 20 < s < n < 55                # plausible CONUS latitudes
    assert -130 < w < e < -60             # plausible CONUS longitudes


def test_from_h5_too_big_skips_overlay(geometry_h5, monkeypatch):
    monkeypatch.setattr(ras, "MESH_PREVIEW_MAX_FACES", 1)
    res = ras.mesh_preview_from_h5(geometry_h5, -1.0, crs="EPSG:5070",
                                   log=lambda *_: None)
    assert res["too_big"] is True
    assert res["overlay"] is None
    assert res["n_faces"] == 3


def test_safe_dispatch_and_error_paths(geometry_h5, tmp_path):
    # the restore payload takes the from_h5 branch and never touches the mesher
    res = ras.build_mesh_preview_safe({"from_h5": str(geometry_h5), "crs": "EPSG:5070",
                                       "cell_size_m": -1.0}, log=lambda *_: None)
    assert res.get("cell_count") == 4 and "error" not in res
    assert res["cell_size_m"] == -1.0     # the restore sentinel rides through
    # a missing file degrades to an error dict, never a raise
    res = ras.build_mesh_preview_safe({"from_h5": str(tmp_path / "nope.h5"),
                                       "crs": "EPSG:5070"}, log=lambda *_: None)
    assert "error" in res
    # an HDF without the mesh datasets takes the RasError message path
    import h5py

    bare = tmp_path / "bare.h5"
    with h5py.File(bare, "w") as f:
        f.create_group("Geometry")
    res = ras.build_mesh_preview_safe({"from_h5": str(bare), "crs": "EPSG:5070"},
                                      log=lambda *_: None)
    assert "error" in res and "mesh" in res["error"].lower()


# ------------------------------------------------------------------- wiring pins

def test_run_path_shares_the_helper():
    # build_mesh_preview's tail was extracted; both paths must render identically
    assert "return mesh_preview_from_h5(geometry_h5, cell, crs=crs, log=log)" in RAS_SRC


def test_restore_fires_the_rebuild():
    m = re.search(r"def _rehydrate.*?def ", APP_SRC, re.DOTALL)
    assert m
    block = m.group(0)
    assert '"Geometries" / "Geometry.h5"' in block
    assert '"from_h5"' in block
    assert '_mesh_auto["on"] = True' in block   # restore failures stay log-only


def test_mesh_overlay_is_not_step_gated():
    m = re.search(r"def _ras_mesh_sync.*?def ", APP_SRC, re.DOTALL)
    assert m
    assert "STEP_SURFACE" not in m.group(0)
    assert 'show = prev and not prev.get("too_big") and ov' in m.group(0)


def test_too_big_note_in_pane_copy():
    i = APP_SRC.index("faces, too many to draw as a")
    seg = APP_SRC[i - 200:i + 200]
    assert "the model itself is unaffected" in seg
    assert "—" not in seg                 # no em dashes in user-facing copy
