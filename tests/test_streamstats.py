"""USGS StreamStats/NSS client tests — offline against recorded fixtures (spec §5, §13.2)."""
import json
import threading
from pathlib import Path

import httpx
import pytest

from hype_app.services.http import RetryPolicy, ServiceClient
from hype_app.services.streamstats import (
    BASE_URL,
    StreamStatsClient,
    _aep_and_recurrence,
    _normalize_discharge,
    _param_range_check,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "usgs"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


def _client(handler):
    sc = ServiceClient(base_url=BASE_URL, transport=httpx.MockTransport(handler),
                       sleep=lambda d: None, retry=RetryPolicy(max_attempts=2, backoff_base=0.001),
                       semaphore=threading.BoundedSemaphore(4))
    return StreamStatsClient(sc)


# --------------------------------------------------------------------------- unit helpers
def test_normalize_discharge_units():
    assert _normalize_discharge(26.0, "ft^3/s")[0] == 26.0
    assert _normalize_discharge(26.0, "ft^3/s")[1] == pytest.approx(0.7362, abs=1e-3)
    cfs, cms = _normalize_discharge(1.0, "m^3/s")
    assert cms == 1.0 and cfs == pytest.approx(35.3147, abs=1e-3)
    assert _normalize_discharge(3.2, "inches") == (None, None)   # non-discharge


def test_aep_parsing_only_when_unambiguous():
    assert _aep_and_recurrence("50-percent AEP flood") == (0.5, 2.0)
    assert _aep_and_recurrence("1-percent AEP flood") == (0.01, 100.0)
    assert _aep_and_recurrence("Some opaque PK50 code") == (None, None)


def test_param_range_check_flags_extrapolation():
    params = [{"code": "DRNAREA", "value": 0.56, "limits": {"min": 0.7, "max": 1290.0}}]
    in_range, values, ranges = _param_range_check(params)
    assert in_range is False
    assert values["DRNAREA"] == 0.56 and ranges["DRNAREA"] == [0.7, 1290.0]


# --------------------------------------------------------------------------- full workflow
@pytest.fixture
def fixture_handler():
    delineate, scenarios = _load("delineate.json"), _load("scenarios.json")
    basin, estimate = _load("basin_chars.json"), _load("estimate.json")

    def handler(request):
        path = request.url.path
        if "ss-delineate" in path:
            return httpx.Response(200, json=delineate)
        if "basin-characteristics" in path:
            return httpx.Response(200, json=basin)
        if path.endswith("/estimate"):
            return httpx.Response(200, json=estimate)
        if path.endswith("/scenarios"):
            return httpx.Response(200, json=scenarios)
        return httpx.Response(404, text="unmocked " + path)
    return handler


def test_lookup_flow_regional(fixture_handler):
    snap = _client(fixture_handler).lookup_flow("NH", 43.686, -72.237)
    assert snap.selected_region == "NH"
    assert snap.watershed_geojson is not None
    assert snap.basin_characteristics.get("DRNAREA") == 0.56
    assert snap.candidates, "expected discharge candidates"

    insertable = [c for c in snap.candidates if c.insertable]
    assert insertable, "at least one insertable discharge"
    assert all(c.value_cfs and c.value_cfs > 0 for c in insertable)
    assert all(c.value_cms is not None for c in insertable)

    # DRNAREA 0.56 < min 0.7 -> every peak-flow result is extrapolated with a warning
    assert all(c.is_extrapolated for c in snap.candidates)
    assert any(w.code == "extrapolated" for c in snap.candidates for w in c.warnings)

    # AEP parsed from the "N-percent AEP flood" names
    aep50 = next(c for c in snap.candidates if c.annual_exceedance_prob == 0.5)
    assert aep50.recurrence_years == 2.0


def test_no_watershed_returns_error_warning():
    def handler(request):
        if "ss-delineate" in request.url.path:
            return httpx.Response(200, json={"bcrequest": {"wsresp": {"featurecollection": []}}})
        return httpx.Response(200, json=[])
    snap = _client(handler).lookup_flow("NH", 43.6, -72.2)
    assert not snap.candidates
    assert any(w.code == "no_watershed" for w in snap.warnings)


def test_national_fallback_when_no_regional_discharge():
    """Regional estimate returns only a non-discharge result -> national fallback fires and
    supplies an insertable national discharge candidate (spec §5.2 step 9, §14.2)."""
    scen = [{"statisticGroupID": 2, "statisticGroupName": "Peak-Flow Statistics",
             "regressionRegions": [{"id": 1, "name": "RR", "code": "GC1",
                                    "parameters": [{"code": "DRNAREA", "value": -999.99,
                                                    "limits": {"min": 0.1, "max": 100.0}}]}]}]
    non_discharge_est = [{"statisticGroupID": 2, "regressionRegions": [
        {"name": "RR", "code": "GC1", "statusID": 4,
         "parameters": [{"code": "DRNAREA", "value": 5.0, "limits": {"min": 0.1, "max": 100.0}}],
         "results": [{"name": "Mean basin slope", "code": "SLOPE", "value": 3.2,
                      "unit": {"abbr": "inches"}}]}]}]
    national_est = [{"statisticGroupID": 2, "regressionRegions": [
        {"name": "National RR", "code": "USRR", "statusID": 4,
         "parameters": [{"code": "DRNAREA", "value": 5.0, "limits": {"min": 0.1, "max": 100.0}}],
         "results": [{"name": "50-percent AEP flood", "code": "PK50AEP", "value": 42.0,
                      "unit": {"abbr": "ft^3/s"}}]}]}]

    def handler(request):
        p, region_us = request.url.path, ("US" in request.url.path or
                                          request.url.params.get("regions") == "US" or
                                          request.url.params.get("region") == "US")
        if "ss-delineate" in p:
            return httpx.Response(200, json={"bcrequest": {"wsresp": {
                "workspace_id": "w", "featurecollection": [{"name": "globalwatershed"}]}}})
        if "basin-characteristics" in p:
            return httpx.Response(200, json=[{"code": "DRNAREA", "value": 5.0}])
        if p.endswith("/estimate"):
            return httpx.Response(200, json=national_est if region_us else non_discharge_est)
        if p.endswith("/scenarios"):
            return httpx.Response(200, json=scen)
        return httpx.Response(404)

    snap = _client(handler).lookup_flow("NH", 43.6, -72.2)
    national = [c for c in snap.candidates if c.is_national]
    assert national and any(c.insertable for c in national)
    assert not any(c.insertable and not c.is_national for c in snap.candidates)  # no regional discharge


def test_national_nodata_degrades_without_crash():
    """When the national basin characteristics come back as -999 NoData sentinels, the national
    estimate isn't computable at this point — the client must emit an actionable warning and keep
    the regional results, NOT crash (the live regression that broke national comparison)."""
    reg_scen = [{"statisticGroupID": 2, "statisticGroupName": "Peak-Flow Statistics",
                 "regressionRegions": [{"id": 1, "name": "RR", "code": "GC1",
                                        "parameters": [{"code": "DRNAREA", "value": 12.8,
                                                        "limits": {"min": 0.1, "max": 100.0}}]}]}]
    us_scen = [{"statisticGroupID": 2, "regressionRegions": [
        {"name": "National Urban", "code": "USRR",
         "parameters": [{"code": "IMPERV"}, {"code": "BDF"}]}]}]
    regional_est = [{"statisticGroupID": 2, "regressionRegions": [
        {"name": "RR", "code": "GC1", "statusID": 4,
         "parameters": [{"code": "DRNAREA", "value": 12.8, "limits": {"min": 0.1, "max": 100.0}}],
         "results": [{"name": "50-percent AEP flood", "code": "PK50AEP", "value": 415.0,
                      "unit": {"abbr": "ft^3/s"}}]}]}]

    def handler(request):
        p = request.url.path
        region_us = (request.url.params.get("regions") == "US")
        if "ss-delineate" in p:
            return httpx.Response(200, json={"bcrequest": {"wsresp": {
                "workspace_id": "w", "featurecollection": [{"name": "globalwatershed"}]}}})
        if "basin-characteristics" in p:
            bcs = (request.url.params.get("BCs") or "")
            if "IMPERV" in bcs or "BDF" in bcs:                 # national urban params: NoData here
                return httpx.Response(200, json=[{"code": "IMPERV", "value": -999.0},
                                                 {"code": "BDF", "value": -999.0}])
            return httpx.Response(200, json=[{"code": "DRNAREA", "value": 12.8}])
        if p.endswith("/scenarios"):
            return httpx.Response(200, json=us_scen if region_us else reg_scen)
        if p.endswith("/estimate"):
            return httpx.Response(200, json=regional_est)      # national estimate must NOT be called
        return httpx.Response(404)

    snap = _client(handler).lookup_flow("NH", 43.688, -72.243, want_national=True)
    regional = [c for c in snap.candidates if not c.is_national]
    national = [c for c in snap.candidates if c.is_national]
    assert regional and any(c.insertable for c in regional)     # regional survives
    assert not national                                          # no bogus national candidate
    assert any(w.code == "national_unavailable" and "IMPERV" in w.message for w in snap.warnings)


def test_cancellation_stops_workflow(fixture_handler):
    from hype_app.services.http import ServiceCancelled
    with pytest.raises(ServiceCancelled):
        _client(fixture_handler).lookup_flow("NH", 43.6, -72.2, cancel=lambda: True)
