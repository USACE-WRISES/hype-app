"""Observation wells (hype_app/wells.py): layer math, sampling, pair gradients, hygiene.

The engine's head tifs are deliberately SOUTH-UP (+dy transform, origin at the south
edge) so tif (row, col) equals the engine's south-first grid indices — the fixtures here
write that exact georeferencing and the locate tests pin that no flip ever sneaks in.
Wells are observation data: nothing here touches the input snapshot or input_hash.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")

from hype_app import wells  # noqa: E402

# Small engine-shaped grid: 4 layers, 3 rows, 4 cols, in UTM metres.
NLAY, NROW, NCOL = 4, 3, 4
DX = DY = 10.0
X0, Y0 = 720_000.0, 4_840_000.0          # SW corner (south-up origin)
CRS_UTM = "EPSG:32618"
NODATA = -9999.0


def _grid(top=100.0):
    """Flat-interface stack: terrain `top`, botm = top-2, top-4, top-6, top-8."""
    topa = np.full((NROW, NCOL), float(top))
    botm = np.stack([np.full((NROW, NCOL), float(top) - 2.0 * (k + 1))
                     for k in range(NLAY)])
    idom = np.ones((NLAY, NROW, NCOL), dtype=int)
    return topa, botm, idom


def _south_up_tifs(tmp_path, values_by_layer):
    """Write head_L*.tifs the way the engine does: row 0 = SOUTH, +dy transform."""
    from rasterio.transform import Affine

    tr = Affine(DX, 0.0, X0, 0.0, DY, Y0)          # positive e — south-up on purpose
    paths = []
    for k, arr in enumerate(values_by_layer):
        p = tmp_path / f"head_L{k + 1:02d}.tif"
        with rasterio.open(p, "w", driver="GTiff", height=NROW, width=NCOL, count=1,
                           dtype="float32", crs=CRS_UTM, nodata=NODATA,
                           transform=tr) as dst:
            dst.write(np.asarray(arr, dtype="float32"), 1)
        paths.append(str(p))
    return paths


def _lonlat_of_cell(row, col):
    """WGS84 coords of the CENTER of engine cell (row, col) under the south-up frame."""
    from pyproj import Transformer

    x = X0 + (col + 0.5) * DX
    y = Y0 + (row + 0.5) * DY
    lon, lat = Transformer.from_crs(CRS_UTM, "EPSG:4326", always_xy=True).transform(x, y)
    return lon, lat


# --------------------------------------------------------------------------- names + hygiene

def test_default_name_first_free_integer():
    assert wells.default_name([]) == "OW-1"
    assert wells.default_name(["OW-1", "ow-2", "junk"]) == "OW-3"
    assert wells.default_name(["OW-2"]) == "OW-1"        # first FREE, not max+1


def test_normalize_wells_coerces_fills_and_dedupes():
    raw = [
        {"id": "a1", "name": "North", "lat": "43.5", "lon": "-72.5",
         "screen_elev": "191.5", "obs_head": None},
        {"id": "a1", "name": "dupe", "lat": 1.0, "lon": 2.0},          # dup id: first wins
        {"id": "b2", "lat": 43.6, "lon": -72.6},                       # missing keys -> None
        {"id": "", "lat": 43.7, "lon": -72.7},                         # no id: dropped
        {"id": "c3", "lat": None, "lon": -72.8},                       # no location: dropped
        "junk",                                                        # not a dict: dropped
    ]
    out = wells.normalize_wells(raw)
    assert [w["id"] for w in out] == ["a1", "b2"]
    assert out[0]["screen_elev"] == pytest.approx(191.5)
    assert out[0]["lat"] == pytest.approx(43.5)
    assert out[1]["screen_elev"] is None and out[1]["obs_head"] is None
    assert out[1]["name"]                                  # missing name gets a default
    assert wells.normalize_wells(None) == []


def test_normalize_pairs_drops_dangling_self_and_duplicates():
    raw = [{"id": "p1", "a": "a1", "b": "b2"},
           {"id": "p2", "a": "b2", "b": "a1"},             # unordered duplicate of p1
           {"id": "p3", "a": "a1", "b": "a1"},             # self-pair
           {"id": "p4", "a": "a1", "b": "zz"},             # dangling well id
           {"id": "", "a": "a1", "b": "b2"}]               # no id
    out = wells.normalize_pairs(raw, {"a1", "b2"})
    assert [p["id"] for p in out] == ["p1"]
    assert wells.normalize_pairs(None, {"a1"}) == []


# --------------------------------------------------------------------------- layer math

def test_layer_for_elevation_topmost_first_and_interfaces():
    top, botm, idom = _grid(top=100.0)                    # intervals: [98,100] [96,98] ...
    assert wells.layer_for_elevation(top, botm, idom, 1, 1, 99.0) == (0, None)
    assert wells.layer_for_elevation(top, botm, idom, 1, 1, 97.5) == (1, None)
    assert wells.layer_for_elevation(top, botm, idom, 1, 1, 92.5) == (3, None)
    # An interface elevation belongs to the UPPER layer (topmost-first scan).
    assert wells.layer_for_elevation(top, botm, idom, 1, 1, 98.0) == (0, None)
    # Tolerance: a hair above terrain still lands in layer 0; more than tol does not.
    assert wells.layer_for_elevation(top, botm, idom, 1, 1, 100.0000005) == (0, None)


def test_layer_for_elevation_reasons():
    top, botm, idom = _grid(top=100.0)
    k, why = wells.layer_for_elevation(top, botm, idom, 0, 0, 101.0)
    assert k is None and why == "above terrain at this location"
    k, why = wells.layer_for_elevation(top, botm, idom, 0, 0, 91.0)
    assert k is None and why == "below model bottom"
    idom2 = idom.copy(); idom2[0, 0, 0] = 0               # above-ground deactivation
    k, why = wells.layer_for_elevation(top, botm, idom2, 0, 0, 99.0)
    assert k is None and why == "inactive cell at screen elevation"
    idom3 = np.zeros_like(idom)
    k, why = wells.layer_for_elevation(top, botm, idom3, 0, 0, 99.0)
    assert k is None and why == "outside active model area"


def test_layer_for_elevation_low_terrain_column():
    """Downstream cell whose terrain sits below the upper flat interfaces: the above-terrain
    guard reads the TERRAIN, not the stack, so air between terrain and an upper interface is
    'above terrain', and elevations below terrain land in the deeper containing layer."""
    top, botm, idom = _grid(top=100.0)
    top[2, 2] = 95.0                                      # this column's terrain is lower
    k, why = wells.layer_for_elevation(top, botm, idom, 2, 2, 95.5)
    assert k is None and why == "above terrain at this location"
    assert wells.layer_for_elevation(top, botm, idom, 2, 2, 94.5) == (2, None)


# --------------------------------------------------------------------------- raster sampling

def test_locate_cell_honors_south_up_transform(tmp_path):
    """The tif's (row, col) must equal the ENGINE's south-first indices — src.index reads
    the +dy affine, no flip anywhere."""
    vals = np.arange(NROW * NCOL, dtype=float).reshape(NROW, NCOL)    # row 0 = south
    tifs = _south_up_tifs(tmp_path, [vals])
    lon, lat = _lonlat_of_cell(2, 3)                      # NORTHERNMOST row, engine row 2
    assert wells.locate_cell(tifs[0], lon, lat) == (2, 3)
    assert wells.sample_head_tif(tifs, 0, 2, 3) == pytest.approx(vals[2, 3])
    lon, lat = _lonlat_of_cell(0, 0)                      # southernmost row
    assert wells.locate_cell(tifs[0], lon, lat) == (0, 0)


def test_locate_cell_outside_grid_is_none(tmp_path):
    tifs = _south_up_tifs(tmp_path, [np.zeros((NROW, NCOL))])
    from pyproj import Transformer
    lon, lat = Transformer.from_crs(CRS_UTM, "EPSG:4326", always_xy=True).transform(
        X0 - 500.0, Y0 - 500.0)
    assert wells.locate_cell(tifs[0], lon, lat) is None


def test_sample_head_tif_invalid_flavors(tmp_path):
    a = np.full((NROW, NCOL), 192.5)
    a[1, 1] = NODATA                                      # declared nodata
    a[1, 2] = -9999.0                                     # sentinel
    tifs = _south_up_tifs(tmp_path, [a])
    assert wells.sample_head_tif(tifs, 0, 0, 0) == pytest.approx(192.5)
    assert wells.sample_head_tif(tifs, 0, 1, 1) is None
    assert wells.sample_head_tif(tifs, 0, 1, 2) is None
    assert wells.sample_head_tif(tifs, 5, 0, 0) is None   # layer raster missing
    assert wells.sample_head_tif([], 0, 0, 0) is None


# --------------------------------------------------------------------------- sample_wells

def _well(uid, row, col, screen=99.0, obs=None):
    lon, lat = _lonlat_of_cell(row, col)
    return {"id": uid, "name": uid.upper(), "lat": lat, "lon": lon,
            "screen_elev": screen, "obs_head": obs}


def test_sample_wells_reason_priority_and_residual(tmp_path):
    top, botm, idom = _grid(top=100.0)
    idom[:, 0, 3] = 0                                     # an all-inactive column
    heads = [np.full((NROW, NCOL), 99.2), np.full((NROW, NCOL), 99.1),
             np.full((NROW, NCOL), 99.0), np.full((NROW, NCOL), 98.9)]
    heads[0][1, 1] = NODATA                               # dry cell in layer 0
    tifs = _south_up_tifs(tmp_path, heads)
    grid = {"top": top, "botm": botm, "idomain": idom, "nlay": NLAY}

    from pyproj import Transformer
    out_lon, out_lat = Transformer.from_crs(CRS_UTM, "EPSG:4326", always_xy=True).transform(
        X0 - 900.0, Y0 - 900.0)
    wls = [
        _well("w1", 2, 1, screen=99.0, obs=99.5),         # good, layer 0, residual -0.3
        _well("w2", 1, 1, screen=99.0),                   # dry cell
        _well("w3", 2, 2, screen=None),                   # no screen elevation yet
        {"id": "w4", "name": "W4", "lat": out_lat, "lon": out_lon,
         "screen_elev": 99.0, "obs_head": None},          # outside the raster
        _well("w5", 0, 3, screen=99.0),                   # inactive column
        _well("w6", 2, 3, screen=97.0),                   # layer 1
    ]
    rows = wells.sample_wells(wls, crs=CRS_UTM, tifs=tifs, grid=grid)
    by = {r["id"]: r for r in rows}
    assert by["w1"]["layer"] == 1 and by["w1"]["computed"] == pytest.approx(99.2)
    # float32 tif storage: match to raster precision, not float64 exactness
    assert by["w1"]["residual"] == pytest.approx(-0.3, abs=1e-4)   # computed minus observed
    assert by["w2"]["computed"] is None and by["w2"]["reason"] == "dry cell"
    assert by["w3"]["reason"] == "enter screen elevation"
    assert by["w4"]["reason"] == "outside model grid"
    assert by["w5"]["reason"] == "outside active model area"
    assert by["w6"]["layer"] == 2 and by["w6"]["computed"] == pytest.approx(99.1)
    assert all(r["x"] is not None and r["y"] is not None for r in rows)


def test_sample_wells_no_run_and_missing_files():
    wls = [_well("w1", 0, 0)]
    rows = wells.sample_wells(wls, crs=CRS_UTM, no_run=True)
    assert rows[0]["reason"] == "no groundwater run" and rows[0]["computed"] is None
    assert rows[0]["x"] is not None                       # distances work without a run
    rows = wells.sample_wells(wls, crs=CRS_UTM, tifs=[], grid=None)
    assert rows[0]["reason"] == "model output files not found"
    rows = wells.sample_wells(wls, crs=None, no_run=True)
    assert rows[0]["x"] is None                           # no CRS yet -> no distances


# --------------------------------------------------------------------------- pairs + stats

def _row(uid, x, y, computed=None, obs=None, name=None):
    return {"id": uid, "name": name or uid.upper(), "lat": 0.0, "lon": 0.0, "x": x, "y": y,
            "screen_elev": None, "obs_head": obs, "layer": None, "computed": computed,
            "residual": None, "reason": None}


def test_pair_rows_distance_and_gradient_sign():
    rows = {"a": _row("a", 0.0, 0.0, computed=100.5, obs=100.8),
            "b": _row("b", 30.0, 40.0, computed=100.0, obs=100.2)}   # 3-4-5: 50 m apart
    out = wells.pair_rows([{"id": "p1", "a": "a", "b": "b"}], rows)
    p = out[0]
    assert p["distance"] == pytest.approx(50.0)
    assert p["computed_gradient"] == pytest.approx((100.5 - 100.0) / 50.0)   # (A-B)/d
    assert p["observed_gradient"] == pytest.approx((100.8 - 100.2) / 50.0)


def test_pair_rows_edge_cases():
    rows = {"a": _row("a", 0.0, 0.0, computed=100.5),
            "b": _row("b", 30.0, 40.0),                              # no heads at b
            "c": _row("c", 0.0, 0.005),                              # < 1 cm from a
            "n": _row("n", None, None, computed=101.0)}              # no CRS -> no xy
    out = wells.pair_rows([
        {"id": "p1", "a": "a", "b": "zz"},                           # dangling
        {"id": "p2", "a": "a", "b": "c"},                            # coincident
        {"id": "p3", "a": "a", "b": "b"},                            # one-sided heads
        {"id": "p4", "a": "a", "b": "n"},                            # missing xy
    ], rows)
    by = {p["id"]: p for p in out}
    assert by["p1"]["reason"] == "well removed" and by["p1"]["distance"] is None
    assert by["p2"]["reason"] == "wells coincide" and by["p2"]["computed_gradient"] is None
    assert by["p3"]["distance"] == pytest.approx(50.0)
    assert by["p3"]["computed_gradient"] is None and by["p3"]["observed_gradient"] is None
    assert by["p4"]["distance"] is None and by["p4"]["reason"] is None


def test_residual_stats_hand_computed():
    rows = [dict(residual=1.0), dict(residual=-2.0), dict(residual=3.0), dict(residual=None)]
    st = wells.residual_stats(rows)
    assert st["n"] == 3
    assert st["mean_error"] == pytest.approx(2.0 / 3.0)
    assert st["mean_abs_error"] == pytest.approx(2.0)
    assert st["rmse"] == pytest.approx(math.sqrt(14.0 / 3.0))
    assert wells.residual_stats([dict(residual=None)]) is None
    assert wells.residual_stats([]) is None


# --------------------------------------------------------------------------- app-source pins

def _app_src():
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def _fn_body(src: str, name: str) -> str:
    """The source slice from `def name` to the next def at the same indent."""
    import re
    m = re.search(rf"\n(    (?:async )?def {name}\b.*?)(?=\n    (?:async )?def |\n    @|\nclass )",
                  src, flags=re.S)
    assert m, f"{name} not found in app.py"
    return m.group(1)


def _code_only(body: str) -> str:
    """Body with the docstring and # comments stripped, so prose can't satisfy (or trip)
    a code-shaped assertion."""
    import re
    body = re.sub(r'""".*?"""', "", body, count=1, flags=re.S)
    return "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())


def test_well_samples_stays_bound_to_the_basecase():
    """The pane, report, and canonical results must all sample the BASECASE run: the samples
    calc reads work_dir artifacts directly and never the displayed-run seam (the head-tifs
    reactive follows the displayed alternative; the alt-view reactive is the seam itself)."""
    body = _code_only(_fn_body(_app_src(), "well_samples"))
    assert "results.head_rasters(work_dir)" in body
    assert "head_tifs()" not in body
    assert "alt_view()" not in body


def test_wells_reset_on_project_switch_and_reach_clear():
    """Unlike grad_pts (a known quirk), the wells reactives reset in _reset_memory_state so a
    New/Open project can never inherit another project's wells; a full reach clear drops them
    too (they anchor to the cleared domain)."""
    src = _app_src()
    for fn in ("_reset_memory_state", "_clear_reach_all"):
        body = _fn_body(src, fn)
        assert "obs_wells.set([])" in body, fn
        assert "well_pairs.set([])" in body, fn


def test_wells_never_enter_the_input_snapshot():
    """Wells are observation data: nothing in the inputs contract may know about them (a
    snapshot field would re-stamp input_hash for every project — the extra_hashes scar).
    Matches identifier spellings, not the bare word (prose says "as well as")."""
    from pathlib import Path
    inputs_src = (Path(__file__).resolve().parents[1] / "hype_app" / "contracts"
                  / "inputs.py").read_text(encoding="utf-8").lower()
    for needle in ("obs_well", "well_pairs", "observation", "calibration"):
        assert needle not in inputs_src, needle
