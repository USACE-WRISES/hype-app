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
    with pytest.raises(PayloadError):
        parse_table("not a dict")


def test_parse_table_zero_row_shapes():
    # SDA answers a zero-row query with a bare {} (verified live 2026-08-02); tolerate the
    # explicit-null and empty-list spellings too. None of these are malformed payloads.
    assert parse_table({}) == []
    assert parse_table({"Table": None}) == []
    assert parse_table({"Table": []}) == []


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


def test_fetch_survives_zero_row_restrictions(sda_handler):
    # THE user-reported failure (2026-08-02): map units with no corestrictions records make
    # SDA answer that query with a bare {} and the whole fetch used to die on PayloadError.
    def handler(request):
        if "corestrictions" in json.loads(request.content).get("query", ""):
            return httpx.Response(200, json={})
        return sda_handler(request)

    snap = _client(handler).fetch_soil_snapshot(_domain_box(), working_crs_epsg=26919)
    assert snap.polygons and snap.map_units
    assert all(not c.restrictions for mu in snap.map_units for c in mu.components)


def test_fetch_no_coverage_returns_empty_snapshot(sda_handler):
    # A domain outside SSURGO coverage: the mupolygon query itself returns zero rows ({}).
    def handler(request):
        if "FROM mupolygon" in json.loads(request.content).get("query", ""):
            return httpx.Response(200, json={})
        return sda_handler(request)

    snap = _client(handler).fetch_soil_snapshot(_domain_box(), working_crs_epsg=26919)
    assert snap.polygons == [] and snap.map_units == []
    assert any(w.code == "no_soil_coverage" for w in snap.missing_diagnostics)


def test_bad_geometry_rows_warned_not_silent(sda_handler):
    # One unparseable WKT row must surface as a geometry_parse warning, not vanish.
    mupolygon = _load("mupolygon.json")
    cols = [str(c) for c in mupolygon["Table"][0]]
    gi = cols.index("geom")
    rows = [list(r) for r in mupolygon["Table"][1:]]
    rows[0][gi] = "POLYGON((not wkt"
    doctored = {"Table": [cols, *rows]}

    def handler(request):
        if "FROM mupolygon" in json.loads(request.content).get("query", ""):
            return httpx.Response(200, json=doctored)
        return sda_handler(request)

    snap = _client(handler).fetch_soil_snapshot(_domain_box(), working_crs_epsg=26919)
    assert snap.polygons, "remaining rows still parse"
    warn = next(w for w in snap.missing_diagnostics if w.code == "geometry_parse")
    assert "1 soil polygon" in warn.message


# --------------------------------------------------------------------------- child runner
class _ListQueue:
    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def test_child_run_reports_friendly_error(monkeypatch):
    from hype_app import soil_run
    from hype_app.services import nrcs as nrcs_mod
    from hype_app.services.http import ServiceTimeout

    class Boom:
        def __init__(self, *a, **k):
            pass

        def fetch_soil_snapshot(self, *a, **k):
            raise ServiceTimeout("ReadTimeout contacting https://sda.example")

        def close(self):
            pass

    monkeypatch.setattr(nrcs_mod, "NRCSClient", Boom)
    q = _ListQueue()
    soil_run.child_run({"domain_geojson": {
        "type": "Polygon", "coordinates": [[[0, 0], [0, 1], [1, 1], [0, 0]]]}}, q)
    kind, err = q.items[-1]
    assert kind == "error"
    assert isinstance(err, dict)
    assert "did not respond" in err["message"] and "internet connection" in err["message"]
    assert "ServiceTimeout" in err["trace"]


def test_child_run_friendly_lines():
    from hype_app.services.http import PayloadError, RateLimited
    from hype_app.soil_run import _friendly

    assert _friendly(PayloadError("SDA response missing 'Table'.")).startswith(
        "NRCS Soil Data Access returned an unexpected response.")
    assert _friendly(RateLimited("Rate limited by x (HTTP 429).")) == \
        "Rate limited by x (HTTP 429)."
    assert _friendly(ValueError("nope")) == "ValueError: nope"


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
