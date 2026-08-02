"""Shared pytest fixtures + collection hooks for the HYPE test suite.

Two environment gates keep the default `pytest` run fast and offline on any dev box:

* ``@pytest.mark.live``   — skipped unless ``HYPE_LIVE_TESTS=1`` (real USGS/NRCS calls).
* ``@pytest.mark.engine`` — skipped unless ``HYPE_MODFLOW_BIN`` points at native mf6/mp7
                            (the bundled ``bin/linux`` binaries only run on Linux).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make the repo root importable (app.py, hype_app/, hypetool/) regardless of the
# invoking cwd, so `import app` / `import hypetool...` resolve during collection.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return ROOT


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    return FIXTURES


def _write_fake_tif(tmp_path: Path, name: str) -> str:
    """A small float32 GeoTIFF in EPSG:32617 (5 m cells, smooth gradient, nodata corner)
    for the site-map figure tests."""
    import numpy as np
    import rasterio
    from rasterio.transform import from_origin

    h, w = 18, 24
    data = np.linspace(95.0, 100.0, h * w).reshape(h, w).astype("float32")
    data[:4, :4] = -9999.0
    p = tmp_path / name
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=1, dtype="float32",
                       crs="EPSG:32617", nodata=-9999.0,
                       transform=from_origin(500000.0, 4200000.0, 5.0, 5.0)) as dst:
        dst.write(data, 1)
    return str(p)


@pytest.fixture
def fake_spatial(tmp_path):
    """A synthetic `spatial` bundle (UTM 17N) matching what app._report_spatial hands to
    report.generate_report: lon/lat vectors, metric paths GDF, and raster/DEM paths."""
    import geopandas as gpd
    from pyproj import CRS, Transformer
    from shapely.geometry import LineString

    crs = CRS.from_epsg(32617)
    tr = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)

    def _ll(pts):
        return [list(tr.transform(x, y)) for x, y in pts]

    reach_utm = [(500005.0, 4199915.0), (500060.0, 4199950.0), (500115.0, 4199985.0)]
    dom_utm = [(500000.0, 4199910.0), (500120.0, 4199910.0),
               (500120.0, 4200000.0), (500000.0, 4200000.0)]
    sides_utm = {"up": [dom_utm[3], dom_utm[0]], "down": [dom_utm[1], dom_utm[2]],
                 "left": [dom_utm[0], dom_utm[1]], "right": [dom_utm[2], dom_utm[3]]}
    paths = gpd.GeoDataFrame(
        {"total_time_d": [0.1, 1.0, 10.0], "hz_class": ["hyporheic"] * 3},
        geometry=[
            LineString([(500010.0, 4199920.0, 98.0), (500035.0, 4199940.0, 97.2),
                        (500060.0, 4199955.0, 98.1)]),
            LineString([(500020.0, 4199925.0, 98.2), (500050.0, 4199945.0, 97.0)]),
            LineString([(500040.0, 4199930.0, 98.4), (500090.0, 4199970.0, 97.8)])],
        crs=crs)
    return {
        "planview": {"reach_lonlat": _ll(reach_utm), "domain_lonlat": _ll(dom_utm)},
        "paths_gdf": paths, "reach_line": LineString(reach_utm),
        "crs_wkt": crs.to_wkt(),
        "wse_tif": _write_fake_tif(tmp_path, "wse.tif"),
        "head_tif": _write_fake_tif(tmp_path, "head.tif"),
        "head_layer": 1,
        "gwf_ws": None,
        "dem_path": _write_fake_tif(tmp_path, "dem.tif"),
        "sides_lonlat": {k: _ll(v) for k, v in sides_utm.items()},
    }


def _has_engine_binaries() -> bool:
    return bool(os.getenv("HYPE_MODFLOW_BIN"))


def pytest_collection_modifyitems(config, items):
    live_on = os.getenv("HYPE_LIVE_TESTS") == "1"
    engine_on = _has_engine_binaries()
    skip_live = pytest.mark.skip(reason="live service test — set HYPE_LIVE_TESTS=1 to run")
    skip_engine = pytest.mark.skip(
        reason="engine test — set HYPE_MODFLOW_BIN to a dir with native mf6/mp7")
    for item in items:
        if "live" in item.keywords and not live_on:
            item.add_marker(skip_live)
        if "engine" in item.keywords and not engine_on:
            item.add_marker(skip_engine)
