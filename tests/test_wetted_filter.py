"""split_wetted_by_connectivity + mask_out_polygons — the isolated-pool filter.

Synthetic 100x100 1 m depth raster in a metric CRS: a wet ribbon spanning the full
north-south extent (touches both caps), an isolated island, and a strip touching only
the upstream cap. The caps are the top/bottom raster edges expressed as EPSG:4326
LineString Features, exactly like the app's normalized boundary caps.
"""
from __future__ import annotations

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")

from pyproj import Transformer  # noqa: E402
from rasterio.transform import from_origin  # noqa: E402

from hype_app import ras_results  # noqa: E402

CRS = "EPSG:32618"
X0, Y_TOP = 500_000.0, 4_800_000.0     # top-left corner; 1 m pixels, 100x100
N = 100

RIBBON_COLS = slice(45, 56)            # touches row 0 (up cap) and row 99 (down cap)
ISLAND = (slice(30, 36), slice(70, 81))            # 6 x 11 px, touches nothing
ONE_CAP = (slice(0, 11), slice(10, 13))            # touches only the up cap


def _write_tif(path, arr, nodata=None):
    meta = {"driver": "GTiff", "dtype": "float32", "count": 1, "width": N, "height": N,
            "crs": CRS, "transform": from_origin(X0, Y_TOP, 1.0, 1.0)}
    if nodata is not None:
        meta["nodata"] = nodata
    with rasterio.open(path, "w", **meta) as dst:
        dst.write(arr.astype("float32"), 1)
    return str(path)


def _depth(tmp_path, *, ribbon=True, island=True, one_cap=True):
    a = np.zeros((N, N), dtype="float32")
    if ribbon:
        a[:, RIBBON_COLS] = 0.5
    if island:
        a[ISLAND] = 0.3
    if one_cap:
        a[ONE_CAP] = 0.2
    return _write_tif(tmp_path / "depth.tif", a)


def _cap(y_m):
    """The full raster width at metric y as a 4326 LineString Feature (like the app caps)."""
    tr = Transformer.from_crs(CRS, "EPSG:4326", always_xy=True)
    lons, lats = tr.transform([X0, X0 + N], [y_m, y_m])
    return {"type": "Feature", "properties": {},
            "geometry": {"type": "LineString",
                         "coordinates": [[lons[0], lats[0]], [lons[1], lats[1]]]}}


@pytest.fixture()
def caps():
    return _cap(Y_TOP), _cap(Y_TOP - N)    # up = top edge, down = bottom edge


def test_split_keeps_ribbon_removes_pools(tmp_path, caps):
    up, down = caps
    res = ras_results.split_wetted_by_connectivity(_depth(tmp_path), up, down)
    assert res is not None and res["n_kept"] == 1
    assert res["n_removed"] == 2           # the island AND the one-cap strip
    island_m2 = 6 * 11
    one_cap_m2 = 11 * 3
    assert res["removed_m2"] == pytest.approx(island_m2 + one_cap_m2, rel=0.01)
    # kept feature is 4326 and covers the ribbon, not the island
    geom = res["kept_feat"]["geometry"]
    assert geom["type"] in ("Polygon", "MultiPolygon")
    lons = np.array([c[0] for ring in geom["coordinates"]
                     for c in (ring if geom["type"] == "Polygon" else ring[0])])
    assert np.all((-180 < lons) & (lons < 0))
    assert res["removed_feat"]["properties"]["n_parts"] == 2


def test_split_none_when_nothing_spans_both_caps(tmp_path, caps):
    up, down = caps
    # island + one-cap strip only: nothing touches BOTH caps -> filter cannot apply
    res = ras_results.split_wetted_by_connectivity(
        _depth(tmp_path, ribbon=False), up, down)
    assert res is None
    # all dry -> None too
    dry = _write_tif(tmp_path / "dry.tif", np.zeros((N, N), dtype="float32"))
    assert ras_results.split_wetted_by_connectivity(dry, up, down) is None


def test_mask_out_polygons_nulls_only_removed(tmp_path, caps):
    up, down = caps
    res = ras_results.split_wetted_by_connectivity(_depth(tmp_path), up, down)
    wse = np.full((N, N), 100.0, dtype="float32")
    src = _write_tif(tmp_path / "wse.tif", wse, nodata=-9999.0)
    out = ras_results.mask_out_polygons(src, res["removed_feat"],
                                        tmp_path / "wse_gw.tif")
    with rasterio.open(out) as ds:
        a = ds.read(1)
        assert ds.nodata == -9999.0
    assert np.all(a[ISLAND] == -9999.0)
    assert np.all(a[ONE_CAP] == -9999.0)
    assert np.all(a[:, RIBBON_COLS] == 100.0)          # the kept ribbon is untouched
    # all_touched may take a 1-px halo around removed parts, never more
    n_removed_px = 6 * 11 + 11 * 3
    n_nulled = int((a == -9999.0).sum())
    assert n_removed_px <= n_nulled <= n_removed_px + 2 * (2 * (6 + 11) + 2 * (11 + 3) + 8)
