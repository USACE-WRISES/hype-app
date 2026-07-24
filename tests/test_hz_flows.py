"""Four-way flux-accounting primitives (§8.3 extension) — pure functions, no executables.

classify_flux_endpoints: weighted origin→exit split into returning/losing/gaining/
throughflow; side_interior_cell: where a side CHD cell's inflow seeds; stream_exchange_gdf:
the exchange-map rectangles built from the same south-first edge arrays as the zone
footprints. The real-MODPATH counterparts live in test_engine_fixture.py (engine-gated).
"""
from __future__ import annotations

import numpy as np
import pytest

from hypetool.functions.hz_analysis import (
    FLUX_CLS,
    MEMBER,
    classify_flux_endpoints,
    path_max_depth,
    side_interior_cell,
    stream_exchange_gdf,
)


def test_path_max_depth():
    """report §7.4: penetration depth = top-of-stream-cell elevation minus the path's minimum z,
    clamped at 0; NaN for an empty path. z needs no N-S row flip (it is a vertical coordinate)."""
    import math

    assert path_max_depth(10.0, np.array([9.0, 7.5, 8.0])) == pytest.approx(2.5)
    assert path_max_depth(10.0, np.array([12.0, 11.0])) == 0.0     # never dips below the bed
    assert math.isnan(path_max_depth(10.0, np.array([])))


def test_classify_flux_endpoints_four_way():
    origin = np.array([MEMBER["top"], MEMBER["top"], MEMBER["left"],
                       MEMBER["downstream"], MEMBER["top"], MEMBER["right"]],
                      dtype=np.uint8)
    exit_c = np.array([MEMBER["top"], MEMBER["right"], MEMBER["top"],
                       MEMBER["upstream"], MEMBER["top"], MEMBER["none"]],
                      dtype=np.uint8)
    status = np.array([5, 2, 3, 6, 1, 5], dtype=np.int16)   # 1 = not a resolved terminus
    w = np.array([10.0, 20.0, 30.0, 40.0, 50.0, 60.0])
    out = classify_flux_endpoints(origin, exit_c, status, w)
    assert out["cls"].tolist() == [FLUX_CLS["returning"], FLUX_CLS["losing"],
                                   FLUX_CLS["gaining"], FLUX_CLS["throughflow"], 0, 0]
    s = out["sums"]
    assert s["returning"] == 10.0 and s["losing"] == 20.0
    assert s["gaining"] == 30.0 and s["throughflow"] == 40.0
    assert s["unresolved_stream"] == 50.0     # unresolved status, stream origin
    assert s["unresolved_side"] == 60.0       # resolved status but exit off any boundary
    assert sum(s.values()) == pytest.approx(w.sum())   # nothing dropped, nothing doubled


def _grid(nlay=2, nrow=3, ncol=3):
    idomain = np.ones((nlay, nrow, ncol), dtype=int)
    member3 = np.zeros((nlay, nrow, ncol), dtype=np.uint8)
    return idomain, member3


def test_side_interior_cell_prefers_lateral():
    idomain, member3 = _grid()
    member3[:, :, 0] = MEMBER["left"]              # whole west edge is boundary
    assert side_interior_cell(0, 1, 0, idomain, member3) == (0, 1, 1)


def test_side_interior_cell_descends_from_corners():
    idomain, member3 = _grid()
    member3[0, :, 0] = MEMBER["left"]
    member3[0, 0, 1] = MEMBER["upstream"]
    # (0,0,0): i-1 / j-1 out of bounds, i+1 and j+1 are boundary cells -> straight down
    assert side_interior_cell(0, 0, 0, idomain, member3) == (1, 0, 0)


def test_side_interior_cell_none_when_landlocked():
    idomain, member3 = _grid(nlay=1)
    member3[0, :, 0] = MEMBER["left"]
    member3[0, 0, 1] = MEMBER["upstream"]
    assert side_interior_cell(0, 0, 0, idomain, member3) is None


def test_stream_exchange_gdf_rectangles():
    pytest.importorskip("geopandas")
    x0, y0 = 500_000.0, 4_800_000.0
    nlay, nrow, ncol = 2, 3, 4
    xe = x0 + 10.0 * np.arange(ncol + 1)
    ye = y0 + 10.0 * np.arange(nrow + 1)           # south-first row edges (engine order)
    nodes = [0 * ncol + 1,                          # layer 0, (row 0, col 1): downwelling
             2 * ncol + 3]                          # layer 0, (row 2, col 3): upwelling
    g = stream_exchange_gdf(nodes, [5.0, -2.5], (nlay, nrow, ncol),
                            xe=xe, ye=ye, crs="EPSG:32618")
    assert list(g["q_m3d"]) == [5.0, -2.5]
    assert g.geometry.iloc[0].bounds == (x0 + 10, y0 + 0, x0 + 20, y0 + 10)
    assert g.geometry.iloc[1].bounds == (x0 + 30, y0 + 20, x0 + 40, y0 + 30)
    assert g.to_crs(4326).geometry.iloc[0].centroid.x < 0   # western hemisphere sanity


def test_stream_exchange_gdf_layer_folds_to_plan_cell():
    """A node in ANY layer maps to its (row, col) rectangle — the map is plan-view."""
    pytest.importorskip("geopandas")
    nlay, nrow, ncol = 3, 2, 2
    xe = np.array([0.0, 1.0, 2.0])
    ye = np.array([0.0, 1.0, 2.0])
    node_l2 = 2 * nrow * ncol + 1 * ncol + 0        # layer 2, (row 1, col 0)
    g = stream_exchange_gdf([node_l2], [1.0], (nlay, nrow, ncol),
                            xe=xe, ye=ye, crs="EPSG:32618")
    assert g.geometry.iloc[0].bounds == (0.0, 1.0, 1.0, 2.0)


def test_stream_exchange_gdf_empty():
    pytest.importorskip("geopandas")
    g = stream_exchange_gdf([], [], (1, 2, 2), xe=np.array([0.0, 1, 2]),
                            ye=np.array([0.0, 1, 2]), crs="EPSG:32618")
    assert len(g) == 0 and "q_m3d" in g.columns
