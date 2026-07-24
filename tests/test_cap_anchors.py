"""Cap-line corner anchoring (2026-07-15, head-anchor/1.1): the station-0/1 corners of each
floodplain boundary reference the WSE along their OWN boundary cap — the valid sample nearest
the corner on the cap line — instead of the globally nearest wetted edge (which for a corner can
be a bank partway down the reach). Intermediate points keep the nearest-edge snap.

These tests pin the two mirrored samplers (preview `wse_index.valid_samples_along_line` vs
engine `my_utils.nearest_valid_wse_along_line`: same half-pixel density, same validity mask),
the engine's f0/f1 anchor override in `compute_boundary_heads_from_profile` (with a no-anchor
regression pin), and the method-version-aware scenario hash that keeps restored sensitivity
results from mixing anchor semantics with fresh runs."""
import numpy as np
import pytest

from hype_app.contracts import GradientBoundaryConfigV2, GradientControl, Side

# Raster convention (mirrors test_gradient_points): EPSG:32618 (metric), 2 m pixels, origin
# (100, 100) top-left — pixel (row, col) center = (101 + 2*col, 99 - 2*row).
RES = 2.0


def _write_tif(path, arr, *, nodata=-9999.0, res=RES):
    import rasterio
    from rasterio.transform import from_origin
    arr = np.asarray(arr, dtype="float32")
    with rasterio.open(
            str(path), "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
            count=1, dtype="float32", crs="EPSG:32618",
            transform=from_origin(100.0, 100.0, res, res), nodata=nodata) as dst:
        dst.write(arr, 1)
    return str(path)


def _strip_raster(tmp_path, name="wse.tif", *, cols=range(8, 12), value=210.0, nodata=-9999.0):
    """20x20 nodata field with a full-height wet strip on `cols` (x = 116..124 for 8..11)."""
    arr = np.full((20, 20), -9999.0)
    for c in cols:
        arr[:, c] = value
    return _write_tif(tmp_path / name, arr, nodata=nodata)


def _cap_feature(x0, x1, y):
    """A 4326 LineString Feature for a horizontal cap at raster-CRS y, spanning x0..x1."""
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)
    lons, lats = tr.transform([x0, x1], [y, y])
    return {"type": "Feature", "properties": {},
            "geometry": {"type": "LineString",
                         "coordinates": [[lons[0], lats[0]], [lons[1], lats[1]]]}}


# --- preview sampler -------------------------------------------------------------------------

def test_valid_samples_along_line_masks_and_values(tmp_path):
    """Only the wet-strip crossing survives, values exact; a dry cap returns None; -9999 with
    nodata UNDECLARED and a -3000 junk pixel are both masked (the upload hardening)."""
    from hype_app import wse_index
    arr = np.full((20, 20), -9999.0)
    arr[:, 8:12] = 210.0
    arr[2, 9] = -3000.0                                   # junk on the cap row
    p = _write_tif(tmp_path / "wse.tif", arr, nodata=None)  # sentinels undeclared
    cap = _cap_feature(101.0, 139.0, 95.0)                # row 2 centers
    raw = wse_index.valid_samples_along_line(p, cap)
    assert raw is not None and raw["value"].size > 0
    assert np.allclose(raw["value"], 210.0)               # -9999 AND -3000 both masked
    assert raw["x"].min() >= 116.0 - 1e-6 and raw["x"].max() <= 124.0 + 1e-6
    # pixel col 9 spans x (118, 120): the -3000 pixel contributes nothing
    in_junk = (raw["x"] > 118.0 + 1e-9) & (raw["x"] < 120.0 - 1e-9)
    assert not in_junk.any()
    # a cap that never touches the strip
    dry = wse_index.valid_samples_along_line(p, _cap_feature(101.0, 113.0, 95.0))
    assert dry is None


def test_valid_samples_short_line_min_floor(tmp_path):
    """A sub-pixel cap still samples (min_n floor), entirely inside the strip."""
    from hype_app import wse_index
    p = _strip_raster(tmp_path)
    raw = wse_index.valid_samples_along_line(p, _cap_feature(119.0, 120.0, 95.0))
    assert raw is not None and raw["value"].size >= 64
    assert np.allclose(raw["value"], 210.0)


# --- engine sampler --------------------------------------------------------------------------

def test_engine_nearest_valid_wse_along_line(tmp_path):
    """The corner picks its own bank: left ref point anchors at the strip's left boundary
    (x = 116) with the exact distance; with TWO strips, each corner anchors to its own side;
    a dry cap returns None."""
    from shapely.geometry import LineString, Point

    from hypetool.functions.my_utils import nearest_valid_wse_along_line
    p = _strip_raster(tmp_path)
    cap = LineString([(101.0, 95.0), (139.0, 95.0)])
    hit = nearest_valid_wse_along_line(p, cap, Point(101.0, 95.0))
    assert hit is not None
    d, w, (sx, sy) = hit
    assert w == pytest.approx(210.0)
    assert sy == pytest.approx(95.0)
    # nearest valid sample sits at the strip's left boundary, within one sample spacing
    spacing = cap.length / 63                              # n floors at 64 for a 38 m cap
    assert sx == pytest.approx(116.0, abs=spacing)
    assert d == pytest.approx(15.0, abs=spacing)

    arr = np.full((20, 20), -9999.0)
    arr[:, 3:5] = 201.0                                    # left strip  x (106, 110)
    arr[:, 15:17] = 202.0                                  # right strip x (130, 134)
    p2 = _write_tif(tmp_path / "two.tif", arr)
    hit_l = nearest_valid_wse_along_line(p2, cap, Point(101.0, 95.0))
    hit_r = nearest_valid_wse_along_line(p2, cap, Point(139.0, 95.0))
    assert hit_l[1] == pytest.approx(201.0)
    assert hit_r[1] == pytest.approx(202.0)

    assert nearest_valid_wse_along_line(
        p, LineString([(101.0, 95.0), (113.0, 95.0)]), Point(101.0, 95.0)) is None


def test_preview_engine_cap_anchor_parity(tmp_path):
    """Same raster + cap + corner: the preview sampler (+ metric argmin, as cap_wse_anchors
    does) and the engine sampler agree on (wse, dist) — the two mirrors share density and
    validity rules by construction, this pins it."""
    from shapely.geometry import LineString, Point

    from hype_app import wse_index
    from hypetool.functions.my_utils import nearest_valid_wse_along_line
    arr = np.full((20, 20), -9999.0)
    arr[:, 8:12] = 210.0 + np.arange(20)[:, None] * 0.1    # row-varying values
    p = _write_tif(tmp_path / "wse.tif", arr)
    corner = (101.0, 95.0)
    raw = wse_index.valid_samples_along_line(p, _cap_feature(101.0, 139.0, 95.0))
    assert raw is not None
    dx, dy = raw["x"] - corner[0], raw["y"] - corner[1]    # raster CRS IS the metric CRS here
    i = int(np.argmin(dx * dx + dy * dy))
    pv_wse, pv_dist = float(raw["value"][i]), float(np.hypot(dx[i], dy[i]))

    d, w, _xy = nearest_valid_wse_along_line(
        p, LineString([(101.0, 95.0), (139.0, 95.0)]), Point(*corner))
    assert w == pytest.approx(pv_wse)
    assert d == pytest.approx(pv_dist, abs=1e-3)           # 4326 round-trip fp only


# --- engine anchor override ------------------------------------------------------------------

def test_compute_boundary_heads_with_anchors(tmp_path):
    """f0/f1 anchors override the corner lookups (head = wse + g*dist exactly), the mid-station
    anchor still uses the nearest-edge path (identical across runs), a single-ended anchor
    leaves the other corner at baseline, and the no-anchor call reproduces itself (pin)."""
    from shapely.geometry import LineString

    from hypetool.functions.my_utils import (
        build_wse_valid_edge_index,
        compute_boundary_heads_from_profile,
    )
    p = _strip_raster(tmp_path)
    idx = build_wse_valid_edge_index(p)
    line = LineString([(105.0, 97.0), (105.0, 63.0)])      # a "left boundary", stations 0..1
    cells = [(0, 0, 0), (0, 1, 0), (0, 2, 0)]              # stations 0.0 / 0.5 / 1.0
    gx = np.array([[105.0], [105.0], [105.0]])
    gy = np.array([[97.0], [80.0], [63.0]])
    prof = "0,0.01 0.5,0.02 1,0.01"
    quiet = lambda *_: None  # noqa: E731

    base, b_f0, b_f1 = compute_boundary_heads_from_profile(
        cells, gx, gy, line, prof, idx, log=quiet)
    again = compute_boundary_heads_from_profile(cells, gx, gy, line, prof, idx, log=quiet)
    assert again[0] == base and (again[1], again[2]) == (b_f0, b_f1)   # regression pin

    heads, f0, f1 = compute_boundary_heads_from_profile(
        cells, gx, gy, line, prof, idx, log=quiet,
        f0_anchor=(7.0, 215.0), f1_anchor=(3.0, 216.0))
    assert f0 == pytest.approx(215.0 + 0.01 * 7.0)
    assert f1 == pytest.approx(216.0 + 0.01 * 3.0)
    assert heads[0] == pytest.approx(f0) and heads[2] == pytest.approx(f1)
    assert heads[1] == pytest.approx(base[1])              # mid anchor untouched by the corners

    _, only_f0, still_b1 = compute_boundary_heads_from_profile(
        cells, gx, gy, line, prof, idx, log=quiet, f0_anchor=(7.0, 215.0))
    assert only_f0 == pytest.approx(215.07) and still_b1 == pytest.approx(b_f1)


# --- scenario hash ---------------------------------------------------------------------------

def test_scenario_hash_includes_method_version():
    """Same gradients under a different head-anchor rule = a different scenario: restored
    sensitivity results must not be mixed with fresh runs after the anchor revision."""
    from hype_app.sensitivity import canonical_scenario_hash

    def _side(side):
        return [GradientControl(id=f"{side.value}-0", side=side, station=0.0, preferred=0.004),
                GradientControl(id=f"{side.value}-1", side=side, station=1.0, preferred=0.006)]
    cfg = GradientBoundaryConfigV2(mode="quantitative",
                                   left_controls=_side(Side.left),
                                   right_controls=_side(Side.right))
    assert canonical_scenario_hash(cfg) == canonical_scenario_hash(cfg.model_copy())
    old = cfg.model_copy(update={"method_version": "head-anchor/1.0"})
    assert canonical_scenario_hash(cfg) != canonical_scenario_hash(old)
