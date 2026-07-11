"""Soil-derived per-cell K integration tests (spec §6.6–6.10, §14.4–14.6).

The builder needs only an in-memory flopy grid (no MODFLOW run), so these are NOT engine-gated.
"""
import numpy as np
import pytest

from hype_app.contracts import (
    Component,
    Horizon,
    MapUnit,
    SoilDataSnapshot,
    SoilPolygon,
)
from hype_app.soil_k import make_cell_k_builder, prepare_soil_k_payload


def _snapshot():
    """Two map units; mukey 1 has a two-horizon profile, mukey 2 has no Ksat at all."""
    mu1 = MapUnit(mukey="1", components=[Component(
        cokey="c1", name="Loam", comppct_r=80, major=True, horizons=[
            Horizon(name="A", top_cm=0, bottom_cm=100, ksat_um_s=10.0),
            Horizon(name="B", top_cm=100, bottom_cm=200, ksat_um_s=20.0)])])
    mu2 = MapUnit(mukey="2", components=[Component(cokey="c2", name="Rock", comppct_r=100,
                                                   major=True, horizons=[])])
    # polygon covering x in [0, 6) of an 11-col grid at y in [0, 1); coordinates are MODEL
    # coords (the test grid has no CRS, so the builder skips reprojection)
    poly1 = SoilPolygon(mupolygonkey="p1", mukey="1", geometry={
        "type": "Polygon", "coordinates": [[[0, 0], [6, 0], [6, 1], [0, 1], [0, 0]]]})
    return SoilDataSnapshot(polygons=[poly1], map_units=[mu1, mu2])


def test_prepare_payload_dominant():
    p = prepare_soil_k_payload(_snapshot(), policy="dominant", anisotropy_ratio=10.0,
                               fallback_kh=10.0, fallback_kv=1.0)
    assert p is not None
    assert set(p["profiles"]) == {"1", "2"}
    assert p["profiles"]["1"][0]["weight"] == 1.0
    assert len(p["profiles"]["1"][0]["horizons"]) == 2
    assert len(p["polygons"]) == 1                 # only polygons whose mukey has a profile


def test_prepare_payload_empty_snapshot_none():
    empty = SoilDataSnapshot()
    assert prepare_soil_k_payload(empty, policy="dominant", anisotropy_ratio=10.0,
                                  fallback_kh=1.0, fallback_kv=0.1) is None


@pytest.fixture
def fake_gwf(tmp_path):
    """In-memory 2-layer 1×11 grid (top=10, botm 5/0), 1 m cells — no MODFLOW run needed."""
    import flopy
    sim = flopy.mf6.MFSimulation(sim_name="k", sim_ws=str(tmp_path))
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
    gwf = flopy.mf6.ModflowGwf(sim, modelname="gwf_model")
    flopy.mf6.ModflowGwfdis(gwf, nlay=2, nrow=1, ncol=11, delr=1.0, delc=1.0,
                            top=10.0, botm=[5.0, 0.0])
    return gwf


class _Cfg:
    hec_ras_crs = None
    output_directory = None

    def __init__(self, out):
        self.output_directory = out


def test_builder_depth_varying_arrays(fake_gwf, tmp_path):
    """§14.4: synthetic NRCS fixture produces the expected depth-varying KH/KV arrays;
    §14.5: below-profile volume uses the documented global fallback and is reported."""
    payload = prepare_soil_k_payload(_snapshot(), policy="dominant", anisotropy_ratio=10.0,
                                     fallback_kh=10.0, fallback_kv=1.0)
    builder = make_cell_k_builder(payload)
    idomain = np.ones((2, 1, 11), dtype=int)
    k, k33 = builder(_Cfg(tmp_path), fake_gwf, idomain)

    assert k is not None and k.shape == (2, 1, 11)
    # covered cells (cols 0..5), layer 0 spans elev 10..5; horizons cover 10..9 (KH 8.64)
    # and 9..8 (KH 17.28); the 3 m below the profile uses fallback KH 10:
    exp_kh0 = (8.64 * 1 + 17.28 * 1 + 10.0 * 3) / 5.0
    exp_kv0 = 5.0 / (1.0 / 0.864 + 1.0 / 1.728 + 3.0 / 1.0)
    assert k[0, 0, 0] == pytest.approx(exp_kh0, rel=1e-6)
    assert k33[0, 0, 0] == pytest.approx(exp_kv0, rel=1e-6)
    # layer 1 (5..0) is entirely below the known profile -> global fallback
    assert k[1, 0, 0] == pytest.approx(10.0)
    assert k33[1, 0, 0] == pytest.approx(1.0)
    # uncovered cells (cols 6..10) stay at the fallback everywhere
    assert np.allclose(k[:, 0, 6:], 10.0)
    assert np.allclose(k33[:, 0, 6:], 1.0)
    # §6.10 coverage report written
    rep = tmp_path / "summary" / "soil_k_coverage.json"
    assert rep.is_file()
    import json
    data = json.loads(rep.read_text())
    assert data["cells_covered"] == 6
    assert data["volume_pct_by_origin"]["derived"] > 0
    assert data["volume_pct_by_origin"]["fallback"] > 0


def test_manual_zone_overrides_soil_base(fake_gwf, tmp_path):
    """§14.6: manual K zones override NRCS assignments (engine overlay semantics)."""
    import geopandas as gpd
    from shapely.geometry import box

    from hypetool.functions.my_utils import _kh_arrays_from_polygon

    payload = prepare_soil_k_payload(_snapshot(), policy="dominant", anisotropy_ratio=10.0,
                                     fallback_kh=10.0, fallback_kv=1.0)
    base_k, base_k33 = make_cell_k_builder(payload)(_Cfg(tmp_path), fake_gwf,
                                                    np.ones((2, 1, 11), dtype=int))

    class _KCfg:
        kh = 10.0
        kv = 1.0
        kh_polygon_shapefile = None
        kh_polygon_gdf = gpd.GeoDataFrame({"KH": [99.0], "KV": [9.9]},
                                          geometry=[box(0, 0, 2, 1)])   # covers cols 0..1

    k, k33 = _kh_arrays_from_polygon(_KCfg(), fake_gwf, np.ones((2, 1, 11), dtype=int),
                                     base=(base_k, base_k33))
    assert np.allclose(k[:, 0, 0:2], 99.0)         # manual zone wins where it covers
    assert np.allclose(k33[:, 0, 0:2], 9.9)
    assert k[0, 0, 3] == pytest.approx(base_k[0, 0, 3])   # soil base kept elsewhere
    assert k[1, 0, 8] == pytest.approx(10.0)              # fallback kept beyond both