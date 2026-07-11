"""NRCS SDA client tests — offline against recorded fixtures (spec §6, §13.2)."""
import json
import threading
from pathlib import Path

import httpx
import pytest

from hype_app.services.http import RetryPolicy, ServiceClient
from hype_app.services.nrcs import (
    NRCSClient,
    parse_table,
    resolve_columns,
)

FIX = Path(__file__).resolve().parent / "fixtures" / "nrcs"


def _load(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- unit helpers
def test_parse_table():
    rows = parse_table({"Table": [["mukey", "muname"], ["49456", "Sarkar"], ["281221", "X"]]})
    assert rows == [{"mukey": "49456", "muname": "Sarkar"}, {"mukey": "281221", "muname": "X"}]


def test_parse_table_missing_key_raises():
    from hype_app.services.http import PayloadError
    with pytest.raises(PayloadError):
        parse_table({"nope": []})


def test_resolve_columns_schema_adapter():
    # current suffix-based names resolve to logical keys
    got = resolve_columns(["mukey", "ksat_r", "hzdept_r", "hzdepb_r", "comppct_r"],
                          ["ksat", "hzdept", "hzdepb", "comppct"])
    assert got == {"ksat": "ksat_r", "hzdept": "hzdept_r",
                   "hzdepb": "hzdepb_r", "comppct": "comppct_r"}
    # future logical column names also resolve
    assert resolve_columns(["horizon_top_depth"], ["hzdept"]) == {"hzdept": "horizon_top_depth"}
    # missing logical omitted
    assert resolve_columns(["mukey"], ["ksat"]) == {}


# --------------------------------------------------------------------------- full workflow
@pytest.fixture
def sda_handler():
    mupolygon, horizons = _load("mupolygon.json"), _load("horizons.json")
    restrictions, survey = _load("restrictions.json"), _load("survey.json")

    def handler(request):
        sql = json.loads(request.content).get("query", "")
        if "FROM mupolygon" in sql:
            return httpx.Response(200, json=mupolygon)
        if "corestrictions" in sql:
            return httpx.Response(200, json=restrictions)
        if "sacatalog" in sql:
            return httpx.Response(200, json=survey)
        if "chorizon" in sql or "component" in sql:
            return httpx.Response(200, json=horizons)
        return httpx.Response(404, text="unmocked SQL")
    return handler


def _client(handler):
    sc = ServiceClient(transport=httpx.MockTransport(handler), sleep=lambda d: None,
                       retry=RetryPolicy(max_attempts=2, backoff_base=0.001),
                       semaphore=threading.BoundedSemaphore(4))
    return NRCSClient(sc)


def _domain_box():
    from shapely.geometry import box
    return box(-72.245, 43.682, -72.230, 43.692)   # same AOI the fixtures were recorded over


def test_fetch_soil_snapshot(sda_handler):
    snap = _client(sda_handler).fetch_soil_snapshot(
        _domain_box(), working_crs_epsg=26919, anisotropy_ratio=10.0)

    assert snap.polygons, "clipped soil polygons expected"
    assert all(p.area_m2 and p.area_m2 > 0 for p in snap.polygons)
    assert snap.map_units, "map units expected"
    assert snap.anisotropy_ratio == 10.0

    # schema adapter recorded the physical columns it used
    assert snap.source_columns_used.get("ksat") == "ksat_r"
    assert snap.source_columns_used.get("hzdept") == "hzdept_r"

    # at least one component carries horizons with a representative Ksat (um/s, kept verbatim)
    comps = [c for mu in snap.map_units for c in mu.components]
    assert comps
    assert any(h.ksat_um_s is not None for c in comps for h in c.horizons)

    # components sorted by descending representative percentage
    for mu in snap.map_units:
        pcts = [c.comppct_r or 0 for c in mu.components]
        assert pcts == sorted(pcts, reverse=True)

    # survey versions captured
    assert snap.survey_versions


def test_digit_validation_filters_bad_mukeys(sda_handler):
    client = _client(sda_handler)
    out = client.fetch_tabular(["281221", "bad-key", "'; DROP TABLE"])
    # only the valid digit mukey survives to the query; the call still returns structured data
    assert "horizons" in out


def test_bedrock_restriction_flagged():
    from hype_app.services.nrcs import NRCSClient as C
    tab = {"horizons": [{"mukey": "1", "muname": "M", "musym": "A", "cokey": "10",
                         "compname": "C", "comppct_r": "80", "majcompflag": "Yes",
                         "chkey": "100", "hzname": "H", "hzdept_r": "0", "hzdepb_r": "20",
                         "ksat_r": "9.0", "texture": "sand"}],
           "restrictions": [{"mukey": "1", "cokey": "10", "reskind": "Lithic bedrock",
                             "resdept_r": "50"}],
           "survey": []}
    mus = C._build_map_units(tab, {"1"})
    comp = mus[0].components[0]
    assert comp.restrictions[0].is_bedrock is True
    assert comp.horizons[0].ksat_um_s == 9.0
