"""results.probe_grid contract: the float32 hover-probe grid served to www/raster_probe.js.

The client's lat/lon -> cell math assumes row 0 = north, little-endian float32, and
bounds at the CELL-CENTER extents (the same numbers rgba_to_overlay hands the
ImageOverlay). Invalid cells (declared nodata, -9999-family sentinels, HDRY-scale
magnitudes) must arrive as NaN so the chip hides over them.
"""
from __future__ import annotations

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
pytest.importorskip("rioxarray")

from hype_app import results  # noqa: E402


W, H = 6, 5
X0, YTOP, STEP = -72.5, 43.5, 0.001          # degrees; source already EPSG:4326
NODATA = -1234.0


@pytest.fixture()
def probe_tif(tmp_path):
    from rasterio.transform import from_origin

    a = (np.arange(H * W, dtype="float32").reshape(H, W) + 100.0)  # row 0 (north) = 100..105
    a[0, 0] = -9999.0            # legacy sentinel, deliberately NOT declared as nodata
    a[1, 1] = -3.4e38            # HDRY-scale magnitude (finite in float32)
    a[3, 3] = NODATA             # declared nodata
    path = tmp_path / "probe_src.tif"
    with rasterio.open(path, "w", driver="GTiff", height=H, width=W, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=NODATA,
                       transform=from_origin(X0, YTOP, STEP, STEP)) as dst:
        dst.write(a, 1)
    return str(path)


def test_probe_grid_contract(probe_tif):
    pg = results.probe_grid(probe_tif)

    assert pg["w"] == W and pg["h"] == H
    assert len(pg["bytes"]) == W * H * 4
    z = np.frombuffer(pg["bytes"], dtype="<f4").reshape(pg["h"], pg["w"])

    # Row 0 is the NORTHERNMOST source row, in place (client assumes north-up).
    assert z[0, 5] == pytest.approx(105.0)
    assert z[4, 0] == pytest.approx(124.0)

    # All three invalid flavors arrive as NaN.
    assert np.isnan(z[3, 3])     # declared nodata
    assert np.isnan(z[0, 0])     # -9999 sentinel (undeclared)
    assert np.isnan(z[1, 1])     # HDRY-scale magnitude
    assert np.isfinite(z).sum() == W * H - 3

    # Bounds are the cell-CENTER extents [s, w, n, e] — identical numbers to the
    # ImageOverlay bounds, which is what pixel-registers the client's math.
    s, w, n, e = pg["bounds"]
    half = STEP / 2
    assert w == pytest.approx(X0 + half, abs=1e-9)
    assert e == pytest.approx(X0 + STEP * (W - 1) + half, abs=1e-9)
    assert n == pytest.approx(YTOP - half, abs=1e-9)
    assert s == pytest.approx(YTOP - STEP * (H - 1) - half, abs=1e-9)


def test_probe_grid_nearest_cell_roundtrip(probe_tif):
    # Mirror the client's valueAt() math (www/raster_probe.js) at a few cell centers and
    # make sure it lands on the exact source value.
    pg = results.probe_grid(probe_tif)
    z = np.frombuffer(pg["bytes"], dtype="<f4").reshape(pg["h"], pg["w"])
    s, w, n, e = pg["bounds"]
    for (r, c) in [(0, 5), (2, 2), (4, 0)]:
        lat = YTOP - STEP * r - STEP / 2
        lng = X0 + STEP * c + STEP / 2
        fx = (lng - w) / (e - w)
        fy = (n - lat) / (n - s)
        assert 0.0 <= fx <= 1.0 and 0.0 <= fy <= 1.0
        col = round(fx * (pg["w"] - 1))
        row = round(fy * (pg["h"] - 1))
        assert (row, col) == (r, c)
        assert z[row, col] == pytest.approx(100.0 + r * W + c)
