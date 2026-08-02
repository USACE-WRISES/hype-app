"""k_scale: the Hydraulic Alternatives K multiplier.

`build_gwf_model` only CONSTRUCTS the flopy simulation (no mf6 execution), so these tests
build the same tiny model twice (k_scale 1 vs 10) and assert the resolved NPF arrays scale
exactly. One case per K source proves the single post-resolution multiply covers all three:
the uniform fallback, K-zone polygons, and a soil-derived cell_k_builder.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import numpy as np
import pytest

gpd = pytest.importorskip("geopandas")
pytest.importorskip("flopy")

from shapely.geometry import box  # noqa: E402

from hypetool.functions.my_utils import build_gwf_model  # noqa: E402

NLAY, NROW, NCOL = 2, 2, 2


def _cfg(tmp_path, *, k_scale=1.0, kh_polygon_gdf=None, cell_k_builder=None):
    ws = tmp_path / f"gwf_{k_scale:g}_{id(kh_polygon_gdf)}_{id(cell_k_builder)}"
    ws.mkdir(parents=True, exist_ok=True)
    top = np.full((NROW, NCOL), 100.0)
    botm = np.stack([np.full((NROW, NCOL), 95.0), np.full((NROW, NCOL), 90.0)])
    return SimpleNamespace(
        nlay=NLAY, nrow=NROW, ncol=NCOL,
        sim_name="t", md6_exe_path=None, gwf_ws=ws, gwf_name="gwf_t",
        time_units="days", nper=1, nstp=1, perlen=1.0, tsmult=1.0,
        cell_size_x=10.0, cell_size_y=10.0,
        tops=[top], botm=botm, xmin=0.0, ymin=0.0, raster_crs=None,
        model_origin_elev=99.0, bed_elevation=95.0,
        kh=10.0, kv=1.0, k_scale=k_scale,
        headfile="t.hds", budgetfile="t.cbb",
        kh_polygon_shapefile=None, kh_polygon_gdf=kh_polygon_gdf,
        cell_k_builder=cell_k_builder,
    )


def _npf_arrays(tmp_path, **kw):
    cfg = _cfg(tmp_path, **kw)
    idomain = np.ones((NLAY, NROW, NCOL), dtype=int)
    _sim, gwf = build_gwf_model(cfg, chd_data=[[0, 0, 0, 100.0]], idomain=idomain)
    npf = gwf.get_package("NPF")
    return np.asarray(npf.k.array, dtype=float), np.asarray(npf.k33.array, dtype=float)


def _zone_gdf():
    # One zone polygon covering ONLY column 0 of the 2x10m grid (x 0..10, y 0..20).
    # Any intersection assigns the zone, so stay strictly inside column 0's x range.
    return gpd.GeoDataFrame({"ZONE_ID": [1], "KH": [50.0], "KV": [5.0]},
                            geometry=[box(-1.0, -1.0, 9.5, 21.0)])


def test_uniform_k_scales_exactly(tmp_path):
    k1, k331 = _npf_arrays(tmp_path, k_scale=1.0)
    k10, k3310 = _npf_arrays(tmp_path, k_scale=10.0)
    assert np.allclose(k1, 10.0) and np.allclose(k331, 1.0)
    assert np.allclose(k10, k1 * 10.0)
    assert np.allclose(k3310, k331 * 10.0)


def test_zone_polygon_k_scales_exactly(tmp_path):
    k1, k331 = _npf_arrays(tmp_path, k_scale=1.0, kh_polygon_gdf=_zone_gdf())
    # Prove the polygon path engaged: column 0 carries the zone K, column 1 the fallback.
    assert np.allclose(k1[:, :, 0], 50.0) and np.allclose(k1[:, :, 1], 10.0)
    assert np.allclose(k331[:, :, 0], 5.0) and np.allclose(k331[:, :, 1], 1.0)
    k01, k3301 = _npf_arrays(tmp_path, k_scale=0.1, kh_polygon_gdf=_zone_gdf())
    assert np.allclose(k01, k1 * 0.1)
    assert np.allclose(k3301, k331 * 0.1)


def test_soil_builder_k_scales_exactly(tmp_path):
    def builder(cfg, gwf, idomain):
        shape = (cfg.nlay, cfg.nrow, cfg.ncol)
        return np.full(shape, 7.0), np.full(shape, 0.7)

    k1, k331 = _npf_arrays(tmp_path, k_scale=1.0, cell_k_builder=builder)
    assert np.allclose(k1, 7.0) and np.allclose(k331, 0.7)
    k10, k3310 = _npf_arrays(tmp_path, k_scale=10.0, cell_k_builder=builder)
    assert np.allclose(k10, 70.0)
    assert np.allclose(k3310, 7.0)


def test_run_hyporheic_accepts_k_scale():
    """The app calls execute(**params); an unknown kwarg would TypeError at run time."""
    from hypetool.core.run_headless import run_hyporheic
    assert "k_scale" in inspect.signature(run_hyporheic).parameters


def test_settings_declares_k_scale():
    from hypetool.inputs import Settings
    fields = getattr(Settings, "model_fields", None) or Settings.__fields__
    assert "k_scale" in fields
