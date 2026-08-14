"""Manual-reach direction: hydro.reach_flow_direction (NHD-first) + the hardened terrain
fallback (delineate.orient_reach_downstream) + the app wiring pins.

The bug this guards: a manually drawn centerline on a low-gradient meander (the Luling
horseshoe) came out reversed — the old terrain-only check compared MEAN end-window
elevations against a 5 cm threshold, which lidar water-surface noise and bank pixels under
a hand-drawn line decide, so the flip was a coin toss exactly where the channel doubles
back. The NHD flow direction now decides first (the same authority Auto mode is built on);
terrain decides only for unmapped streams, and only on a decisive slope.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from hype_app import delineate, hydro

ROOT = Path(__file__).resolve().parent.parent


# ------------------------------------------------------------- reach_flow_direction

def _gdf(feats):
    """[(geom, da), ...] → EPSG:4326 GeoDataFrame shaped like flowlines_bbox output."""
    import geopandas as gpd
    return gpd.GeoDataFrame({"totdasqkm": [f[1] for f in feats]},
                            geometry=[f[0] for f in feats], crs=4326)


def _p(lon, lat):
    return {"lat": lat, "lon": lon}


def test_straight_same_flowline_ok_and_reversed():
    from shapely.geometry import LineString
    main = LineString([(-97.710, 29.670), (-97.690, 29.670)])   # digitized W→E = flow W→E
    g = _gdf([(main, 1956.0)])
    up, dn = _p(-97.708, 29.6701), _p(-97.692, 29.6701)
    assert hydro.reach_flow_direction(up, dn, g) == "ok"
    assert hydro.reach_flow_direction(dn, up, g) == "reversed"


def test_horseshoe_projection_decides():
    # A U-bend: the drawn ends sit ~400 m apart in space but ~2.9 km apart along the
    # line — exactly where end-to-end DEM slopes are too small to trust. Projection onto
    # the digitized line is unambiguous.
    from shapely.geometry import LineString
    u = LineString([(-97.7020, 29.6600), (-97.7020, 29.6700),
                    (-97.6980, 29.6700), (-97.6980, 29.6600)])
    g = _gdf([(u, 1956.0)])
    start, end = _p(-97.7021, 29.6605), _p(-97.6979, 29.6605)
    assert hydro.reach_flow_direction(start, end, g) == "ok"
    assert hydro.reach_flow_direction(end, start, g) == "reversed"


def test_trib_mouth_loses_per_endpoint():
    # A tiny tributary passes CLOSER to one endpoint than the mainstem does. Nearest-wins
    # snapping would judge direction off the trib; the per-endpoint largest-DA rule keeps
    # the mainstem on both ends (the snap_reach_da lesson, applied per endpoint).
    from shapely.geometry import LineString
    main = LineString([(-97.710, 29.670), (-97.690, 29.670)])
    trib = LineString([(-97.692, 29.6720), (-97.692, 29.67025)])
    g = _gdf([(main, 1956.0), (trib, 0.9)])
    up, dn = _p(-97.708, 29.6702), _p(-97.692, 29.6702)
    assert hydro.reach_flow_direction(up, dn, g) == "ok"
    assert hydro.reach_flow_direction(dn, up, g) == "reversed"


def test_split_mainstem_da_orders():
    # The mainstem splits into segments at a confluence, so no single feature is near both
    # ends: the larger total drainage area is downstream (Auto's ordering rule).
    from shapely.geometry import LineString
    seg_up = LineString([(-97.710, 29.670), (-97.700, 29.670)])
    seg_dn = LineString([(-97.700, 29.670), (-97.690, 29.670)])
    g = _gdf([(seg_up, 1900.0), (seg_dn, 1956.0)])
    up, dn = _p(-97.708, 29.6701), _p(-97.692, 29.6701)
    assert hydro.reach_flow_direction(up, dn, g) == "ok"
    assert hydro.reach_flow_direction(dn, up, g) == "reversed"


def test_split_mainstem_equal_da_undecidable():
    from shapely.geometry import LineString
    seg_up = LineString([(-97.710, 29.670), (-97.700, 29.670)])
    seg_dn = LineString([(-97.700, 29.670), (-97.690, 29.670)])
    g = _gdf([(seg_up, 1956.0), (seg_dn, 1956.0)])
    assert hydro.reach_flow_direction(_p(-97.708, 29.6701), _p(-97.692, 29.6701), g) is None


def test_no_flowline_within_cutoff_is_none():
    from shapely.geometry import LineString
    far = LineString([(-97.710, 29.680), (-97.690, 29.680)])    # ~1.1 km away
    g = _gdf([(far, 1956.0)])
    assert hydro.reach_flow_direction(_p(-97.708, 29.6701), _p(-97.692, 29.6701), g) is None


def test_missing_da_is_none():
    import geopandas as gpd
    from shapely.geometry import LineString
    main = LineString([(-97.710, 29.670), (-97.690, 29.670)])
    g = gpd.GeoDataFrame(geometry=[main], crs=4326)             # no totdasqkm column
    assert hydro.reach_flow_direction(_p(-97.708, 29.6701), _p(-97.692, 29.6701), g) is None


def test_multipart_geometry_is_none():
    # A MultiLineString's part order is not flow order — undecidable, never a guess.
    from shapely.geometry import MultiLineString
    m = MultiLineString([[(-97.710, 29.670), (-97.700, 29.670)],
                         [(-97.700, 29.670), (-97.690, 29.670)]])
    g = _gdf([(m, 1956.0)])
    assert hydro.reach_flow_direction(_p(-97.708, 29.6701), _p(-97.692, 29.6701), g) is None


def test_degenerate_projection_is_none():
    from shapely.geometry import LineString
    main = LineString([(-97.710, 29.670), (-97.690, 29.670)])
    g = _gdf([(main, 1956.0)])
    assert hydro.reach_flow_direction(_p(-97.7000, 29.6701), _p(-97.7001, 29.6701), g) is None


# ------------------------------------------------------------- terrain fallback

def _reach_and_dem(tmp_path, fill):
    """A 1.8 km E-W reach Feature (EPSG:4326) over a synthetic UTM 17N GeoTIFF whose
    values come from fill(x_utm) (constant down each column); returns (feature, path).
    No nodata is declared so the undeclared-sentinel handling is exercised as shipped."""
    import rasterio
    from pyproj import Transformer
    from rasterio.transform import from_origin

    h = w = 400                                     # 5 m cells: x 500000..502000
    xs = 500000.0 + (np.arange(w) + 0.5) * 5.0
    data = np.tile(fill(xs).astype("float32"), (h, 1))
    p = tmp_path / "dem.tif"
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       crs="EPSG:32617",
                       transform=from_origin(500000.0, 4200000.0, 5.0, 5.0)) as dst:
        dst.write(data, 1)
    tr = Transformer.from_crs("EPSG:32617", "EPSG:4326", always_xy=True)
    lon0, lat0 = tr.transform(500100.0, 4199000.0)
    lon1, lat1 = tr.transform(501900.0, 4199000.0)
    feat = {"type": "Feature", "properties": {},
            "geometry": {"type": "LineString", "coordinates": [[lon0, lat0], [lon1, lat1]]}}
    return feat, str(p)


def test_steep_backwards_draw_still_flips(tmp_path):
    feat, dem = _reach_and_dem(tmp_path, lambda xs: 100.0 + (xs - 500000.0) * 0.01)
    fixed, flipped = delineate.orient_reach_downstream(feat, dem)
    assert flipped is True
    assert fixed["geometry"]["coordinates"][0] == feat["geometry"]["coordinates"][-1]


def test_bank_noise_no_longer_flips(tmp_path):
    # Flat reach; alternating 5 m columns in the TAIL window read 5 m high (bank pixels
    # under a hand-drawn line). The old mean + 5 cm rule flipped on this; the 25th
    # percentile ignores the contaminated upper tail.
    def fill(xs):
        v = np.full(xs.shape, 100.0)
        v[(xs >= 501700.0) & ((xs.astype(int) // 5) % 2 == 0)] = 105.0
        return v
    feat, dem = _reach_and_dem(tmp_path, fill)
    fixed, flipped = delineate.orient_reach_downstream(feat, dem)
    assert flipped is False and fixed is feat


def test_sub_margin_uphill_keeps_as_drawn(tmp_path):
    # 0.2 m total rise start→end is within lidar water-surface noise: keep as drawn
    # (the old 5 cm threshold flipped this).
    feat, dem = _reach_and_dem(tmp_path, lambda xs: 100.0 + (xs - 500000.0) * (0.2 / 1800.0))
    fixed, flipped = delineate.orient_reach_downstream(feat, dem)
    assert flipped is False and fixed is feat


def test_decisive_uphill_flips(tmp_path):
    feat, dem = _reach_and_dem(tmp_path, lambda xs: 100.0 + (xs - 500000.0) * (2.0 / 1800.0))
    _fixed, flipped = delineate.orient_reach_downstream(feat, dem)
    assert flipped is True


def test_undeclared_sentinel_masked(tmp_path):
    # -9999 cells with no declared nodata must be masked out, not read as a "lower" head
    # (a low percentile is even more sensitive to sentinels than the old mean was).
    def fill(xs):
        v = np.full(xs.shape, 100.0)
        v[(xs >= 500100.0) & (xs <= 500200.0)] = -9999.0
        return v
    feat, dem = _reach_and_dem(tmp_path, fill)
    _fixed, flipped = delineate.orient_reach_downstream(feat, dem)
    assert flipped is False


def test_reversed_feature_roundtrip():
    feat = {"type": "Feature", "properties": {"a": 1},
            "geometry": {"type": "LineString",
                         "coordinates": [[0.0, 0.0], [1.0, 0.5], [2.0, 0.0]]}}
    r = delineate.reversed_feature(feat)
    assert r["geometry"]["coordinates"] == [[2.0, 0.0], [1.0, 0.5], [0.0, 0.0]]
    assert r["properties"] == {"a": 1}
    assert delineate.reversed_feature({"geometry": {"coordinates": [[0, 0]]}}) is None
    assert delineate.reversed_feature({}) is None


# ------------------------------------------------------------- wiring pins

APP = (ROOT / "app.py").read_text(encoding="utf-8")


def _slice(src, start, end):
    i = src.index(start)
    return src[i:src.index(end, i)]


def test_app_wires_nhd_direction_check():
    assert "def _manual_dir_check():" in APP
    assert "dir_task(*_reach_endpoints(rf))" in APP
    assert ('hydro.reach_flow_direction(p1, p2, _flow.get("gdf"), '
            "max_ft=_DA_SNAP_MAX_FT)") in APP
    assert "delineate.reversed_feature(rf)" in APP


def test_dem_done_defers_to_the_verdict():
    assert '_reach_dir["sig"] == _dir_sig(reach_feat())' in APP
    assert '_dem_orient_reach(res["path"])' in APP


def test_deferred_flip_recommits_to_the_chain():
    body = _slice(APP, "def _dir_done():", "async def delineate_task")
    assert "reach_gen.set(reach_gen() + 1)" in body
    assert "_dem_orient_reach(dem_p, bump_gen=True)" in body


def test_toast_copy_no_em_dash():
    assert "(from the NHD flow direction)." in APP
    assert "(from the terrain)." in APP
    i = 0
    while True:
        i = APP.find("Centerline direction corrected to upstream", i)
        if i < 0:
            break
        assert "—" not in APP[i:i + 160]
        i += 1


def test_restore_preseeds_direction_as_settled():
    body = _slice(APP, 'reach = vec.get("reach")', "reach_feat.set(reach)")
    assert '_reach_dir["sig"] = _dir_sig(reach)' in body
    assert '_reach_dir["verdict"] = "ok"' in body


def test_da_sig_is_direction_insensitive():
    assert 'sig = tuple(sorted((round(e["lat"], 7), round(e["lon"], 7)) for e in eps))' in APP


def test_clear_cancels_dir_task_and_resets():
    body = _slice(APP, "async def _clear_reach_all():", "async def _clear_points():")
    assert "dir_task" in body
    assert '_reach_dir["sig"] = None' in body


def test_hydro_and_delineate_pins():
    hy = (ROOT / "hype_app" / "hydro.py").read_text(encoding="utf-8")
    de = (ROOT / "hype_app" / "delineate.py").read_text(encoding="utf-8")
    assert "def reach_flow_direction(" in hy
    assert "prep = _reach_candidates(p_up, p_dn, flowlines_gdf)" in hy  # shared prep in use
    assert "margin: float = 0.30" in de
    assert "np.percentile(vals, 25)" in de
    assert "def reversed_feature(" in de


def test_changelog_mentions_the_fix():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "direction from the NHD flow direction" in text
