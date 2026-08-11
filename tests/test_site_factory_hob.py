"""Unit tests for tools/site_factory/hob.py, the GMS observation-well harvest.

All fixtures are literal text under tmp_path - no dependence on the 22 GB
source tree. The end-to-end harvest test builds a miniature site folder with a
real EPSG:2277 prj so the reprojection and the 3 km centroid gate run for real.
"""
from __future__ import annotations

import math

import pytest

from tools.site_factory import hob

# Real LL01096 values: EPSG:2277 (NAD83 / Texas Central ftUS) XY that lands
# ~66 m from the site centroid at (30.649364, -99.251808).
X1, Y1 = 2636667.5153323, 10201340.744087
X2, Y2 = 2636805.2308429, 10201491.224256
CENTROID = (30.649364, -99.251808)

HOB_TEXT = f"""# GMS HOB package
#           CoverageGUID ObjectType ID X Y Time OBNAME
#GMSCOMMENT 1edd5771-0000 POINT 1, {X1}, {Y1} ts_0 hed1
#GMSCOMMENT 1edd5771-0001 POINT 2, {X2}, {Y2} ts_0 hed2
2 0 500 0 0
hed1 4 48 36 1 0.0 -0.077648 0.170294 1311.6 0.51021 1 1
hed2 4 48 37 1 0.0 -0.055000 0.120000 1310.9 0.51021 1 1
"""


def _epsg2277_esri_wkt() -> str:
    import pyproj

    return pyproj.CRS.from_epsg(2277).to_wkt(version="WKT1_ESRI")


def _write_site(tmp_path, hob_text=HOB_TEXT, with_prj=True):
    site = tmp_path / "LL01096"
    mf = site / "GMS" / "LL01096_MODFLOW"
    mf.mkdir(parents=True)
    (mf / "LL01096.hob").write_text(hob_text, encoding="utf-8")
    # the MODFLOW dir's own .prj is a GMS package file, NOT WKT - must be skipped
    (mf / "LL01096.prj").write_text("MODFLOW project file, not a CRS", encoding="utf-8")
    gis = site / "GMS" / "GIS"
    gis.mkdir(parents=True)
    if with_prj:
        (gis / "GWDomain.prj").write_text(_epsg2277_esri_wkt(), encoding="utf-8")
    return site


# ---------------------------------------------------------------- parse_hob

def test_parse_hob_reads_comment_xy_and_heads(tmp_path):
    p = tmp_path / "a.hob"
    p.write_text(HOB_TEXT, encoding="utf-8")
    wells = hob.parse_hob(p)
    assert [w["obname"] for w in wells] == ["hed1", "hed2"]
    assert wells[0]["x"] == pytest.approx(X1)
    assert wells[0]["y"] == pytest.approx(Y1)
    assert wells[0]["head_ft"] == pytest.approx(1311.6)
    assert wells[1]["head_ft"] == pytest.approx(1310.9)


def test_parse_hob_tolerates_missing_data_line(tmp_path):
    text = HOB_TEXT.replace("hed2 4 48 37 1 0.0 -0.055000 0.120000 1310.9 0.51021 1 1\n", "")
    p = tmp_path / "a.hob"
    p.write_text(text, encoding="utf-8")
    wells = hob.parse_hob(p)
    assert wells[1]["obname"] == "hed2"
    assert wells[1]["head_ft"] is None


def test_parse_hob_missing_file_is_empty(tmp_path):
    assert hob.parse_hob(tmp_path / "nope.hob") == []


# ---------------------------------------------------------------- find_hobs

def test_find_hobs_skips_uuid_run_copies(tmp_path):
    site = tmp_path / "S"
    good = site / "GMS" / "S_MODFLOW"
    good.mkdir(parents=True)
    (good / "S.hob").write_text(HOB_TEXT, encoding="utf-8")
    copy = site / "GMS" / "12345678-abcd-ef01-2345-6789abcdef01" / "S_MODFLOW"
    copy.mkdir(parents=True)
    (copy / "S.hob").write_text(HOB_TEXT, encoding="utf-8")
    found = hob.find_hobs(site)
    assert len(found) == 1
    assert "12345678" not in str(found[0])


# ---------------------------------------------------------------- gms_crs

def test_gms_crs_prefers_gwdomain_and_skips_non_wkt(tmp_path):
    site = _write_site(tmp_path)
    hob_path = site / "GMS" / "LL01096_MODFLOW" / "LL01096.hob"
    crs = hob.gms_crs(hob_path)
    assert crs is not None and crs.to_epsg() == 2277


def test_gms_crs_skips_geogcs_spikes_prj(tmp_path):
    import pyproj

    site = _write_site(tmp_path, with_prj=False)
    gis = site / "GMS" / "GIS"
    (gis / "Spikes").mkdir()
    (gis / "Spikes" / "pts.prj").write_text(
        pyproj.CRS.from_epsg(4326).to_wkt(version="WKT1_ESRI"), encoding="utf-8")
    assert hob.gms_crs(site / "GMS" / "LL01096_MODFLOW" / "LL01096.hob") is None


# ---------------------------------------------------------------- match_names

def test_match_names_handles_scrambled_order():
    # the LL01096 lesson: hob order is NOT BR order (hed3=BR4 etc.)
    points = [(0.0, 0.0), (10.0, 0.0), (20.0, 0.0)]
    cands = [("BR4", 10.2, 0.0), ("BR1", 0.1, 0.0), ("BR3", 20.1, 0.0)]
    got = hob.match_names(points, cands, tol=2.0, dist_fn=hob._planar)
    assert got == {0: "BR1", 1: "BR4", 2: "BR3"}


def test_match_names_is_one_to_one_and_respects_tol():
    points = [(0.0, 0.0), (0.5, 0.0), (99.0, 0.0)]
    cands = [("A", 0.1, 0.0)]
    got = hob.match_names(points, cands, tol=2.0, dist_fn=hob._planar)
    assert got == {0: "A"}          # nearest wins, second point unmatched
    assert 2 not in got             # out of tolerance


# ---------------------------------------------------------------- harvest

def test_harvest_end_to_end_with_transobs_names(tmp_path):
    site = _write_site(tmp_path)
    # scrambled on purpose: hed1 XY -> BR2, hed2 XY -> BR1
    (site / "GMS" / "TransObservation.csv").write_text(
        f"BR2, 1, {X1}, {Y1}\n1.0, 1316.2\nBR1, 2, {X2}, {Y2}\n1.0, 1316.3\n",
        encoding="utf-8")
    wells = hob.harvest_site_wells(site, "LL01096", *CENTROID)
    assert [w["obs_name"] for w in wells] == ["hed1", "hed2"]
    assert [w["name"] for w in wells] == ["BR2", "BR1"]
    assert all(w["name_source"] == "transobs" for w in wells)
    assert all(w["include"] == "Yes" for w in wells)
    assert all(0 < w["dist_centroid_m"] < 200 for w in wells)
    assert wells[0]["obs_head_ft"] == pytest.approx(1311.6)
    assert wells[0]["source_hob"] == "GMS/LL01096_MODFLOW/LL01096.hob"
    # lat/lon actually landed at the Llano site
    assert wells[0]["lat"] == pytest.approx(30.6488, abs=2e-3)
    assert wells[0]["lon"] == pytest.approx(-99.2518, abs=2e-3)


def test_harvest_falls_back_to_hob_names(tmp_path):
    site = _write_site(tmp_path)
    wells = hob.harvest_site_wells(site, "LL01096", *CENTROID)
    assert [w["name"] for w in wells] == ["hed1", "hed2"]
    assert all(w["name_source"] == "hob" for w in wells)


def test_harvest_centroid_gate_flags_but_keeps(tmp_path):
    site = _write_site(tmp_path)
    wells = hob.harvest_site_wells(site, "LL01096", 31.5, -98.0)  # ~150 km away
    assert len(wells) == 2
    assert all(w["include"] == "No" for w in wells)
    assert all("3 km" in w["note"] for w in wells)


def test_harvest_no_crs_flags_but_keeps(tmp_path):
    site = _write_site(tmp_path, with_prj=False)
    wells = hob.harvest_site_wells(site, "LL01096", *CENTROID)
    assert len(wells) == 2
    assert all(w["lat"] is None and w["include"] == "No" for w in wells)
    assert all("CRS" in w["note"] for w in wells)


def test_harvest_variant_hob_dedupes_and_prefixes(tmp_path):
    site = _write_site(tmp_path)
    # second variant: hed1 duplicates the primary (skip), hed9 is 30 m away (add)
    x9, y9 = X1 + 100.0, Y1
    var = site / "GMS" / "LL01096_2_MODFLOW"
    var.mkdir()
    (var / "LL01096_2.hob").write_text(
        f"#GMSCOMMENT g-0 POINT 1, {X1}, {Y1} ts_0 hed1\n"
        f"#GMSCOMMENT g-1 POINT 2, {x9}, {y9} ts_0 hed9\n"
        "hed1 4 48 36 1 0.0 0.0 0.0 1311.6 0.5 1 1\n"
        "hed9 4 48 40 1 0.0 0.0 0.0 1309.0 0.5 1 1\n", encoding="utf-8")
    wells = hob.harvest_site_wells(site, "LL01096", *CENTROID)
    names = [w["obs_name"] for w in wells]
    assert names[:2] == ["hed1", "hed2"]   # primary keeps bare names
    assert names[2] == "2:hed9"            # variant prefixed, duplicate hed1 dropped
    assert len(names) == 3


def test_variant_label_shapes():
    from pathlib import Path

    assert hob._variant_label(Path("S/GMS/PR01540_2_MODFLOW/x.hob"), "PR01540") == "2"
    assert hob._variant_label(Path("S/GMS_Clone/CH00365_MODFLOW/x.hob"), "CH00365") == "GMS_Clone"


def test_planar_distance():
    assert hob._planar(0, 0, 3, 4) == pytest.approx(5.0)
    assert math.isfinite(hob._planar(1e6, 1e6, 1e6, 1e6))
