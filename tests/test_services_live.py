"""Live smoke tests against the real USGS + NRCS services (spec §13.2).

Skipped by default; run with HYPE_LIVE_TESTS=1. Slow (StreamStats delineation is ~30 s). These
exist to catch upstream schema/endpoint drift that recorded fixtures can't; they are intentionally
lenient (structure + at least one usable result), not exact-value assertions.
"""
import pytest

pytestmark = [pytest.mark.live, pytest.mark.slow]

_LAT, _LON, _REGION = 43.686, -72.237, "NH"


def test_streamstats_live():
    from hype_app.services.streamstats import StreamStatsClient
    client = StreamStatsClient()
    try:
        snap = client.lookup_flow(_REGION, _LAT, _LON)
    finally:
        client.close()
    assert snap.watershed_geojson is not None
    assert snap.candidates, "expected discharge candidates from live NSS"
    assert any(c.insertable for c in snap.candidates)


def test_nrcs_live():
    from shapely.geometry import box

    from hype_app.services.nrcs import NRCSClient
    client = NRCSClient()
    try:
        snap = client.fetch_soil_snapshot(
            box(-72.245, 43.682, -72.230, 43.692), working_crs_epsg=26919)
    finally:
        client.close()
    assert snap.polygons, "expected soil polygons from live SDA"
    assert snap.map_units
    assert any(h.ksat_um_s is not None
               for mu in snap.map_units for c in mu.components for h in c.horizons)
