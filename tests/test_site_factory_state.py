"""Unit tests for tools/site_factory/appstate.py (converters + bundle-state merge)
and results_capture.build_calibration.

The merge is the factory's contract with the app: every app-authored state key
must pass through untouched, and re-running the bundle stage must never
duplicate wells or wipe in-app edits.
"""
from __future__ import annotations

import pytest

from tools.site_factory import appstate
from tools.site_factory import results_capture

SHEET_ROWS = [
    {"obs_name": "hed1", "name": "BR1", "include": "Yes",
     "lat": 30.64876666, "lon": -99.25177786, "obs_head_ft": 1311.6,
     "screen_elev_ft": None},
    {"obs_name": "hed2", "name": "BR2", "include": "Yes",
     "lat": 30.64917672, "lon": -99.25133526, "obs_head_ft": 1311.2,
     "screen_elev_ft": 1305.0},
]


# ---------------------------------------------------------------- app_well_records

def test_app_well_records_shape_and_units():
    recs = appstate.app_well_records("LL01096", SHEET_ROWS)
    assert len(recs) == 2
    r = recs[0]
    assert set(r) == {"id", "name", "lat", "lon", "screen_elev", "obs_head"}
    assert len(r["id"]) == 8
    # ftUS -> m with the US survey foot, not the international foot
    assert r["obs_head"] == pytest.approx(1311.6 * appstate.FT_US, abs=1e-3)
    assert r["screen_elev"] is None
    assert recs[1]["screen_elev"] == pytest.approx(1305.0 * appstate.FT_US, abs=1e-3)


def test_app_well_records_normalize_stable():
    from hype_app import wells as wells_mod

    recs = appstate.app_well_records("LL01096", SHEET_ROWS)
    assert wells_mod.normalize_wells(recs) == recs


def test_app_well_records_filters():
    rows = [
        dict(SHEET_ROWS[0], include="No"),                  # excluded by gate
        dict(SHEET_ROWS[0], obs_name="a", lat=None),        # no coordinates
        dict(SHEET_ROWS[0], obs_name="b", include=True),    # bool include ok
        dict(SHEET_ROWS[0], obs_name="c", include="yes", name=""),  # name falls back
        dict(SHEET_ROWS[0], obs_name=""),                   # no key
    ]
    recs = appstate.app_well_records("S", rows)
    assert [r["name"] for r in recs] == ["BR1", "c"]


def test_well_ids_deterministic_and_distinct():
    a1 = appstate.well_id("LL01096", "hed1")
    assert a1 == appstate.well_id("LL01096", "hed1")
    assert a1 != appstate.well_id("LL01096", "hed2")
    assert a1 != appstate.well_id("CH00156", "hed1")


# ---------------------------------------------------------------- aerial records

def test_aerial_layer_records_shape_and_normalize():
    from hype_app import map_layers as ml_mod

    recs = appstate.aerial_layer_records("LL01096", ["NAIP_2022.tif", "NAIP_2020.tif"])
    assert [r["path"] for r in recs] == ["$WORKSPACE$/aerials/NAIP_2022.tif",
                                         "$WORKSPACE$/aerials/NAIP_2020.tif"]
    assert all(r["kind"] == "raster" and r["visible"] is False for r in recs)
    # imagery registers fully opaque (band-4 NIR is no longer mistaken for alpha)
    assert all(r["opacity"] == 1.0 for r in recs)
    norm = ml_mod.normalize_map_layers(recs)
    assert [r["id"] for r in norm] == [r["id"] for r in recs]
    assert all(r["visible"] is False for r in norm)
    # deterministic ids across re-runs
    again = appstate.aerial_layer_records("LL01096", ["NAIP_2022.tif"])
    assert again[0]["id"] == recs[0]["id"]


# ---------------------------------------------------------------- merge_state

def _factory_wells():
    return appstate.app_well_records("S", [
        {"obs_name": "hed1", "name": "BR1", "include": "Yes",
         "lat": 30.1, "lon": -99.1, "obs_head_ft": 1311.6, "screen_elev_ft": None},
        {"obs_name": "hed2", "name": "BR2", "include": "Yes",
         "lat": 30.2, "lon": -99.2, "obs_head_ft": None, "screen_elev_ft": None},
    ])


def test_merge_state_fresh():
    st = appstate.merge_state(None, site_id="S", factory_wells=_factory_wells(),
                              aerial_layers=appstate.aerial_layer_records("S", ["a.tif"]),
                              format_version=2)
    assert st["format_version"] == 2
    assert st["desktop_project"] is True
    assert st["project_name"] == "S"
    assert len(st["obs_wells"]) == 2
    assert st["well_pairs"] == []
    assert len(st["map_layers"]) == 1


def test_merge_state_preserves_app_keys_and_edits():
    fw = _factory_wells()
    existing = {
        "sel_node": "gw.res",                       # arbitrary app-authored keys
        "check_state": {"basemap": False},
        "project_name": "renamed by hand",
        "obs_wells": [
            # the factory well, tuned in-app: screen_elev set, name irrelevant
            {"id": fw[0]["id"], "name": "old", "lat": 0.0, "lon": 0.0,
             "screen_elev": 399.0, "obs_head": None},
            # an app-added well with a foreign id
            {"id": "deadbeef", "name": "OW-9", "lat": 30.5, "lon": -99.5,
             "screen_elev": None, "obs_head": None},
        ],
        "well_pairs": [{"id": "p1", "a": fw[0]["id"], "b": "deadbeef"}],
        "map_layers": [
            {"id": "m1", "path": "$WORKSPACE$\\AERIALS\\A.TIF", "name": "a",
             "kind": "raster", "opacity": 0.5, "color": "#112233", "visible": True}],
    }
    st = appstate.merge_state(existing, site_id="S", factory_wells=fw,
                              aerial_layers=appstate.aerial_layer_records(
                                  "S", ["a.tif", "b.tif"]),
                              format_version=2)
    assert st["sel_node"] == "gw.res"
    assert st["check_state"] == {"basemap": False}
    assert st["project_name"] == "S"                 # factory-owned, overlaid
    wells = {w["id"]: w for w in st["obs_wells"]}
    w0 = wells[fw[0]["id"]]
    assert w0["name"] == "BR1"                       # factory wins name/coords
    assert w0["lat"] == pytest.approx(30.1)
    assert w0["screen_elev"] == pytest.approx(399.0)  # in-app edit kept
    assert w0["obs_head"] == pytest.approx(1311.6 * appstate.FT_US, abs=1e-3)
    assert "deadbeef" in wells                       # app-added well preserved
    assert wells[fw[1]["id"]]["name"] == "BR2"       # new factory well appended
    assert st["well_pairs"] == existing["well_pairs"]
    # dedupe matched the casefolded backslash path: only b.tif appended
    paths = [r["path"] for r in st["map_layers"]]
    assert paths[0] == "$WORKSPACE$\\AERIALS\\A.TIF"
    assert paths[1:] == ["$WORKSPACE$/aerials/b.tif"]


def test_merge_state_keeps_existing_obs_head_when_factory_lacks_one():
    fw = _factory_wells()
    existing = {"obs_wells": [
        {"id": fw[1]["id"], "name": "x", "lat": 0.0, "lon": 0.0,
         "screen_elev": None, "obs_head": 398.5}]}
    st = appstate.merge_state(existing, site_id="S", factory_wells=fw,
                              aerial_layers=[], format_version=2)
    merged = {w["id"]: w for w in st["obs_wells"]}
    assert merged[fw[1]["id"]]["obs_head"] == pytest.approx(398.5)


def test_merge_state_idempotent():
    fw = _factory_wells()
    aer = appstate.aerial_layer_records("S", ["a.tif"])
    once = appstate.merge_state({"foo": 1}, site_id="S", factory_wells=fw,
                                aerial_layers=aer, format_version=2)
    twice = appstate.merge_state(once, site_id="S", factory_wells=fw,
                                 aerial_layers=aer, format_version=2)
    assert twice == once


# ---------------------------------------------------------------- build_calibration

def _srow(**kw):
    base = {"id": "w1", "name": "BR1", "lat": 30.1, "lon": -99.1,
            "screen_elev": None, "obs_head": 399.8, "layer": None,
            "computed": None, "residual": None, "reason": "enter screen elevation"}
    base.update(kw)
    return base


def test_build_calibration_empty_is_none():
    assert results_capture.build_calibration([], []) is None


def test_build_calibration_screenless_rows_have_no_stats():
    cal = results_capture.build_calibration([_srow()], [])
    assert len(cal.wells) == 1
    assert cal.wells[0].note == "enter screen elevation"
    assert cal.wells[0].computed_head_m is None
    assert cal.stats is None
    assert cal.pairs == []


def test_build_calibration_sampled_rows_carry_stats():
    rows = [_srow(id="w1", screen_elev=398.0, layer=3, computed=399.9,
                  residual=0.1, reason=None),
            _srow(id="w2", name="BR2", obs_head=399.0, screen_elev=398.0,
                  layer=3, computed=399.4, residual=0.4, reason=None)]
    cal = results_capture.build_calibration(rows, [])
    assert cal.stats is not None
    assert cal.stats.n_observed == 2
    assert cal.stats.mean_error_m == pytest.approx(0.25)
