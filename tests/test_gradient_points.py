"""The groundwater BC redesign (2026-07): qualitative mode gets editable slight/strong
multipliers + a centerline-method reference slope, and the structured mode becomes map-placed
gradient POINTS (corner numerics = the mandatory station-0/1 controls, click-added interior
points, head = nearest-WSE + gradient × distance previewed app-side by hype_app.wse_index).

These tests pin the pure pieces: the multiplier scale, the points→engine profile parity, the
preview edge sampler vs the engine's validity mask, the -9999-sentinel hardening, the
click→station math, and the kept-input restore migration."""
import numpy as np
import pytest

from hype_app.contracts import (
    QUALITATIVE_MULTIPLIER,
    GradientBoundaryConfigV2,
    GradientControl,
    GradientQualitative,
    ReferenceSlope,
    Side,
)
from hype_app.gradients import (
    anchor_head,
    downstream_wse_warnings,
    migrate_kept_gradients,
    signed_multiplier,
)

Q = GradientQualitative


def _rs(v=0.004):
    return ReferenceSlope(value=v, source="wse_raster", method="test")


# --- multiplier scale ----------------------------------------------------------------------

def test_signed_multiplier_scale():
    for cat in Q:
        assert signed_multiplier(cat) == QUALITATIVE_MULTIPLIER[cat]      # defaults reproduce
    assert signed_multiplier(Q.strongly_gaining, slight=0.3, strong=2.0) == +2.0
    assert signed_multiplier(Q.slightly_gaining, slight=0.3, strong=2.0) == +0.3
    assert signed_multiplier(Q.neutral, slight=0.3, strong=2.0) == 0.0
    assert signed_multiplier(Q.slightly_losing, slight=0.3, strong=2.0) == -0.3
    assert signed_multiplier(Q.strongly_losing, slight=0.3, strong=2.0) == -2.0


def test_from_qualitative_custom_multipliers():
    cfg = GradientBoundaryConfigV2.from_qualitative(
        left=Q.slightly_gaining, right=Q.strongly_losing, reference_slope=_rs(0.004),
        slight=0.3, strong=2.0)
    assert cfg.left_controls[0].preferred == pytest.approx(+0.3 * 0.004)
    assert cfg.right_controls[0].preferred == pytest.approx(-2.0 * 0.004)
    # defaults reproduce the original locked scale
    d = GradientBoundaryConfigV2.from_qualitative(
        left=Q.slightly_gaining, right=Q.strongly_losing, reference_slope=_rs(0.004))
    assert d.left_controls[0].preferred == pytest.approx(+0.5 * 0.004)
    assert d.right_controls[0].preferred == pytest.approx(-1.0 * 0.004)


# --- points -> engine profile parity ---------------------------------------------------------

def test_points_profile_engine_roundtrip():
    """Corner numerics + one interior map point serialize to the exact anchors the engine's
    profile parser reads back — the app preview and the run share the same stations/gradients."""
    from hype_app.gradients import serialize_profile
    from hypetool.functions.my_utils import parse_fraction_gradient_profile
    ctls = [GradientControl(id="left-0", side=Side.left, station=0.0, preferred=0.004),
            GradientControl(id="left-abc", side=Side.left, station=0.42, preferred=-0.002),
            GradientControl(id="left-1", side=Side.left, station=1.0, preferred=0.006)]
    assert parse_fraction_gradient_profile(serialize_profile(ctls)) == [
        (0.0, 0.004), (0.42, -0.002), (1.0, 0.006)]


# --- wse_index: preview edge sampler ---------------------------------------------------------

def _write_tif(path, arr, *, nodata=-9999.0, res=2.0):
    import rasterio
    from rasterio.transform import from_origin
    arr = np.asarray(arr, dtype="float32")
    with rasterio.open(
            str(path), "w", driver="GTiff", height=arr.shape[0], width=arr.shape[1],
            count=1, dtype="float32", crs="EPSG:32618",
            transform=from_origin(100.0, 100.0, res, res), nodata=nodata) as dst:
        dst.write(arr, 1)
    return str(path)


def test_edge_samples_and_nearest(tmp_path):
    """A wet 2x2 blob inside a nodata ring: every wet pixel borders nodata, so all 4 are edge
    samples; the nearest lookup returns the true center distance + value, and anchor_head is
    the engine formula on those numbers."""
    from hype_app import wse_index
    arr = np.full((4, 4), -9999.0)
    arr[1, 1], arr[1, 2], arr[2, 1], arr[2, 2] = 213.0, 213.1, 213.2, 213.3
    p = _write_tif(tmp_path / "wse.tif", arr)
    idx = wse_index.build_edge_samples(p)
    assert idx is not None and len(idx["x"]) == 4          # the whole blob is its own edge
    # pixel (1,1) center = (103, 97) at 2 m res from origin (100, 100)
    d, w, ex, ey, i = wse_index.nearest_edge(idx, 103.0, 105.0)
    assert (ex, ey) == (103.0, 97.0) and w == pytest.approx(213.0)
    assert d == pytest.approx(8.0)
    assert anchor_head(w, 0.005, d) == pytest.approx(213.0 + 0.005 * 8.0)


def test_edge_samples_mask_matches_engine(tmp_path):
    """The preview's edge-pixel set must match the engine's (build_wse_valid_edge_index):
    same validity mask, same erosion — else arrows point at cells the run won't use."""
    from hype_app import wse_index
    from hypetool.functions.my_utils import build_wse_valid_edge_index
    rng = np.random.default_rng(7)
    arr = np.full((12, 12), -9999.0)
    arr[3:9, 2:10] = 210.0 + rng.random((6, 8))            # a wet slab with a real interior
    p = _write_tif(tmp_path / "wse2.tif", arr)
    idx = wse_index.build_edge_samples(p)
    eng = build_wse_valid_edge_index(p)
    ours = set(zip(idx["x"].tolist(), idx["y"].tolist()))
    theirs = set(zip(np.asarray(eng["edge_x"]).tolist(), np.asarray(eng["edge_y"]).tolist()))
    assert ours == theirs


def test_edge_samples_undeclared_nodata(tmp_path):
    """An uploaded WSE raster with UNDECLARED -9999 (nodata=None) must still mask the
    sentinels — else the 'nearest WSE' can be -9999 and heads go absurd."""
    from hype_app import wse_index
    arr = np.full((4, 4), -9999.0)
    arr[1, 1] = 213.0
    p = _write_tif(tmp_path / "wse3.tif", arr, nodata=None)
    idx = wse_index.build_edge_samples(p)
    assert idx is not None and len(idx["x"]) == 1
    assert idx["value"][0] == pytest.approx(213.0)


def test_min_elevation_ignores_undeclared_sentinel(tmp_path):
    """min_elevation_along_line now samples WSE rasters for the reference slope; an undeclared
    -9999 sentinel must never win the min (it used to only mask DECLARED nodata)."""
    from hype_app.delineate import min_elevation_along_line
    arr = np.full((4, 4), -9999.0)
    arr[1, 1], arr[2, 2] = 213.0, 212.5
    p = _write_tif(tmp_path / "wse4.tif", arr, nodata=None)
    # the raster sits at UTM 18N (100..108, 92..100); build a 4326 line crossing it
    from pyproj import Transformer
    tr = Transformer.from_crs("EPSG:32618", "EPSG:4326", always_xy=True)
    lon0, lat0 = tr.transform(101.0, 99.0)
    lon1, lat1 = tr.transform(107.0, 93.0)
    feat = {"type": "Feature", "properties": {},
            "geometry": {"type": "LineString", "coordinates": [[lon0, lat0], [lon1, lat1]]}}
    assert min_elevation_along_line(feat, p) == pytest.approx(212.5)


# --- click -> station math -------------------------------------------------------------------

def test_click_station_math():
    """The map-click handler's math (pure re-implementation): project the click onto the side
    line, normalize by length, clamp to [0.02, 0.98] (the corners own stations 0/1), and
    reject clicks beyond the pixel tolerance."""
    from shapely.geometry import LineString, Point
    line = LineString([(0.0, 0.0), (500.0, 0.0)])
    tol = 14 * 2.0                                          # 14 px at 2 m/px

    def station_of(pt):
        if pt.distance(line) > tol:
            return None
        return min(max(float(line.project(pt) / line.length), 0.02), 0.98)

    assert station_of(Point(210.0, 5.0)) == pytest.approx(0.42)
    assert station_of(Point(3.0, 0.0)) == 0.02              # near-corner clamps inward
    assert station_of(Point(499.0, -10.0)) == 0.98
    assert station_of(Point(250.0, 40.0)) is None           # beyond tolerance -> no point


# --- restore migration -----------------------------------------------------------------------

def test_migrate_kept_corner_mode():
    kept = {"bc_mode": "4 Corner Gradients",
            "g_ul": 0.01, "g_ur": 0.02, "g_dl": 0.03, "g_dr": 0.04}
    pts = migrate_kept_gradients(kept, None)
    assert kept["bc_mode"] == "Spatially Varying Gradient"
    assert pts == []                                        # corners already hold the values
    assert kept["g_ul"] == 0.01 and kept["g_dr"] == 0.04


def test_migrate_kept_structured_text():
    kept = {"bc_mode": "Spatially Varying Gradient",
            "g_left_ctl": "0, 0.01\n0.5, 0.02\n1, 0.03",
            "g_right_ctl": "0, -0.005\n1, -0.005"}
    pts = migrate_kept_gradients(kept, None)
    assert kept["g_ul"] == 0.01 and kept["g_dl"] == 0.03    # stations 0/1 -> corner numerics
    assert kept["g_ur"] == -0.005 and kept["g_dr"] == -0.005
    assert len(pts) == 1
    assert pts[0]["side"] == "left" and pts[0]["station"] == 0.5
    assert pts[0]["gradient"] == 0.02 and "lower" not in pts[0]


def test_migrate_saved_points_win_over_legacy_text():
    kept = {"bc_mode": "Spatially Varying Gradient", "g_left_ctl": "0, 0.9\n0.5, 0.9\n1, 0.9",
            "g_ul": 0.01}
    saved = [{"id": "abc12345", "side": "left", "station": 0.3, "gradient": 0.002}]
    pts = migrate_kept_gradients(kept, saved)
    assert pts == saved
    assert kept["g_ul"] == 0.01                             # legacy text NOT re-applied


def test_migrate_unparseable_text_ignored():
    kept = {"bc_mode": "Spatially Varying Gradient", "g_left_ctl": "not, numbers, at all",
            "g_ul": 0.007}
    assert migrate_kept_gradients(kept, None) == []
    assert kept["g_ul"] == 0.007


def test_migrate_preserves_explicit_bounds():
    kept = {"bc_mode": "Spatially Varying Gradient",
            "g_left_ctl": "0, 0.01\n0.5, 0.02, 0.015, 0.025\n1, 0.03"}
    pts = migrate_kept_gradients(kept, None)
    assert pts[0]["lower"] == 0.015 and pts[0]["upper"] == 0.025


# --- downstream-WSE monotonicity warnings --------------------------------------------------


def _wrow(uid, side, station, wse):
    return {"uid": uid, "side": side, "station": station, "wse": wse}


def test_downstream_wse_warnings_flags_lower_than_next():
    # The live example: left WSE declines 0% → 64% (192.54 > 190.59 — no flag on the corner),
    # then the downstream corner samples HIGHER (192.21) — only the 64% row is flagged. The
    # comparison is on the raw WSE sample, not the gradient-derived head.
    rows = [_wrow("ul", "left", 0.0, 192.54), _wrow("p1", "left", 0.64, 190.59),
            _wrow("dl", "left", 1.0, 192.21),
            _wrow("ur", "right", 0.0, 191.09), _wrow("dr", "right", 1.0, 186.42)]
    assert downstream_wse_warnings(rows) == {"p1"}


def test_downstream_wse_warnings_sides_independent():
    # A rise on the right never flags left rows (and vice versa), even when the left values
    # interleave with the right ones globally; unsorted input is sorted per side by station.
    rows = [_wrow("dr", "right", 1.0, 200.0), _wrow("ul", "left", 0.0, 195.0),
            _wrow("ur", "right", 0.0, 190.0), _wrow("dl", "left", 1.0, 194.0)]
    assert downstream_wse_warnings(rows) == {"ur"}


def test_downstream_wse_warnings_none_and_equal_values():
    rows = [_wrow("ul", "left", 0.0, None), _wrow("p1", "left", 0.5, 191.0),
            _wrow("dl", "left", 1.0, None),
            _wrow("ur", "right", 0.0, 190.0), _wrow("dr", "right", 1.0, 190.0)]
    assert downstream_wse_warnings(rows) == set()
