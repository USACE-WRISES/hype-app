"""Local-DEM import (hype_app/dem.py import_local_dem + the DEM-source app wiring): the
working-copy contract (clip to the reach-buffer AOI, float32/-9999, verbatim source grid)
and the source pins holding the feature's structural invariants — the source file is a
PATH POINTER, inputs/dem.tif stays THE working DEM for both sources, and the pick handler
(never the auto-chain) is the import trigger for a freshly linked raster."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
gpd = pytest.importorskip("geopandas")
pyproj = pytest.importorskip("pyproj")

from rasterio.transform import from_origin  # noqa: E402
from shapely.geometry import LineString, box, mapping  # noqa: E402

from hype_app import dem  # noqa: E402

UTM = "EPSG:32617"


def _site(tmp_path, *, nodata_corner=True):
    """A 400x400 10 m UTM source raster near (-81, 35) + a mid-raster domain box (4326)
    and a diagonal reach Feature inside it. Returns (src_tif, domain_gdf, reach, ctx)."""
    tr = pyproj.Transformer.from_crs("EPSG:4326", UTM, always_xy=True)
    back = pyproj.Transformer.from_crs(UTM, "EPSG:4326", always_xy=True)
    cx, cy = tr.transform(-81.0, 35.0)
    x0, y0 = cx - 2000, cy + 2000

    def ll(x, y):
        return back.transform(x, y)

    src_tif = tmp_path / "src.tif"
    data = np.add.outer(np.linspace(100, 150, 400), np.linspace(0, 50, 400)).astype("f4")
    if nodata_corner:
        data[:40, :40] = -9999.0
    with rasterio.open(src_tif, "w", driver="GTiff", height=400, width=400, count=1,
                       dtype="float32", crs=UTM, nodata=-9999.0,
                       transform=from_origin(x0, y0, 10, 10)) as dst:
        dst.write(data, 1)
    w, s = ll(cx - 600, cy - 400)
    e, n = ll(cx + 600, cy + 400)
    dom = gpd.GeoDataFrame(geometry=[box(w, s, e, n)], crs=4326)
    reach = {"type": "Feature", "properties": {}, "geometry": mapping(
        LineString([ll(cx - 500, cy - 300), ll(cx + 500, cy + 300)]))}
    return src_tif, dom, reach, {"x0": x0, "y0": y0, "cx": cx, "cy": cy, "ll": ll}


# ------------------------------------------------------------------ import_local_dem

def test_import_clips_on_the_source_grid(tmp_path):
    src, dom, reach, ctx = _site(tmp_path)
    out = tmp_path / "dem.tif"
    res = dem.import_local_dem(src, dom, out, reach_feat_4326=reach)
    assert res["path"] == str(out)
    assert res["source"] == "Local raster" and res["src_name"] == "src.tif"
    assert res["note"] is None
    assert res["resolution_m"] == pytest.approx(10.0, abs=0.01)
    with rasterio.open(out) as ds:
        assert ds.dtypes[0] == "float32" and ds.nodata == -9999.0
        assert ds.crs.to_epsg() == 32617                # source CRS kept, never reprojected
        assert ds.transform.a == pytest.approx(10) and ds.transform.e == pytest.approx(-10)
        # verbatim pixel copy: the clip window sits on the SOURCE grid (integral offsets)
        assert ((ds.transform.c - ctx["x0"]) / 10) % 1 == pytest.approx(0)
        assert ((ctx["y0"] - ds.transform.f) / 10) % 1 == pytest.approx(0)
        a = ds.read(1, masked=True)
        assert float(a.min()) > 0                       # nodata masked, real elevations only


def test_import_error_messages(tmp_path):
    src, dom, reach, ctx = _site(tmp_path)
    out = tmp_path / "dem.tif"

    with pytest.raises(dem.DemImportError, match="was not found"):
        dem.import_local_dem(tmp_path / "gone.tif", dom, out)

    nocrs = tmp_path / "nocrs.tif"
    with rasterio.open(nocrs, "w", driver="GTiff", height=10, width=10, count=1,
                       dtype="float32", transform=from_origin(0, 10, 1, 1)) as dst:
        dst.write(np.ones((10, 10), "f4"), 1)
    with pytest.raises(dem.DemImportError, match="no projection information"):
        dem.import_local_dem(nocrs, dom, out)

    multi = tmp_path / "multi.tif"
    with rasterio.open(multi, "w", driver="GTiff", height=10, width=10, count=3,
                       dtype="uint8", crs=UTM,
                       transform=from_origin(ctx["x0"], ctx["y0"], 10, 10)) as dst:
        dst.write(np.zeros((3, 10, 10), "u1"))
    with pytest.raises(dem.DemImportError, match="single band"):
        dem.import_local_dem(multi, dom, out)

    far = gpd.GeoDataFrame(geometry=[box(-120.01, 44.99, -119.99, 45.01)], crs=4326)
    with pytest.raises(dem.DemImportError, match="does not cover the reach area"):
        dem.import_local_dem(src, far, out)

    # a reach routed through the raster's nodata corner: data exists in the AOI, but not
    # under the centerline — the failure that otherwise surfaces as NaN offsets deep
    # inside boundary delineation
    ll = ctx["ll"]
    bad = {"type": "Feature", "properties": {}, "geometry": mapping(LineString(
        [ll(ctx["x0"] + 50, ctx["y0"] - 50), ll(ctx["x0"] + 350, ctx["y0"] - 350)]))}
    with pytest.raises(dem.DemImportError, match="along the reach centerline"):
        dem.import_local_dem(src, dom, out, reach_feat_4326=bad)


def test_import_all_nodata_errors(tmp_path):
    src, dom, reach, ctx = _site(tmp_path)
    blank = tmp_path / "blank.tif"
    with rasterio.open(blank, "w", driver="GTiff", height=400, width=400, count=1,
                       dtype="float32", crs=UTM, nodata=-9999.0,
                       transform=from_origin(ctx["x0"], ctx["y0"], 10, 10)) as dst:
        dst.write(np.full((400, 400), -9999.0, "f4"), 1)
    with pytest.raises(dem.DemImportError, match="no valid elevation pixels"):
        dem.import_local_dem(blank, dom, tmp_path / "dem.tif")


def test_partial_coverage_is_a_note_not_an_error(tmp_path):
    src, dom, reach, ctx = _site(tmp_path)
    ll = ctx["ll"]
    w, s = ll(ctx["x0"] - 3000, ctx["cy"] - 400)       # hangs 3 km off the west edge
    e, n = ll(ctx["cx"] - 1000, ctx["cy"] + 400)
    part = gpd.GeoDataFrame(geometry=[box(w, s, e, n)], crs=4326)
    res = dem.import_local_dem(src, part, tmp_path / "dem.tif")
    assert "part of the recommended terrain extent" in res["note"]
    assert "\u2014" not in res["note"]


def test_budget_decimation_reports_the_coarsened_resolution(tmp_path):
    src, dom, reach, ctx = _site(tmp_path)
    out = tmp_path / "dem.tif"
    res = dem.import_local_dem(src, dom, out, max_pixels=1000)
    assert res["resolution_m"] > 10 and "reduced" in res["note"]
    with rasterio.open(out) as ds:
        assert ds.width * ds.height <= 1000
        assert abs(ds.transform.a) == pytest.approx(res["resolution_m"], rel=0.01)


def test_no_budget_when_max_pixels_none(tmp_path):
    # The app's desktop import path passes max_pixels=None: no pixel budget, verbatim
    # source resolution (the old code compared w*h > None and would TypeError).
    src, dom, reach, ctx = _site(tmp_path)
    res = dem.import_local_dem(src, dom, tmp_path / "dem.tif", max_pixels=None)
    assert res["note"] is None
    assert res["resolution_m"] == pytest.approx(10.0, abs=0.01)


def test_geographic_source_reports_metres(tmp_path):
    src, dom, reach, ctx = _site(tmp_path)
    geo = tmp_path / "geo.tif"
    with rasterio.open(geo, "w", driver="GTiff", height=500, width=500, count=1,
                       dtype="float32", crs="EPSG:4326", nodata=-9999.0,
                       transform=from_origin(-81.02, 35.02, 0.0001, 0.0001)) as dst:
        dst.write(np.full((500, 500), 42.0, "f4"), 1)
    res = dem.import_local_dem(geo, dom, tmp_path / "dem.tif", reach_feat_4326=reach)
    assert 8.0 < res["resolution_m"] < 12.0            # ~0.0001 deg at 35 N


def test_nan_without_declared_nodata_becomes_nodata(tmp_path):
    src, dom, reach, ctx = _site(tmp_path)
    nan_tif = tmp_path / "nan.tif"
    data = np.full((400, 400), 50.0, "f4")
    data[200:210, 200:210] = np.nan
    with rasterio.open(nan_tif, "w", driver="GTiff", height=400, width=400, count=1,
                       dtype="float32", crs=UTM,
                       transform=from_origin(ctx["x0"], ctx["y0"], 10, 10)) as dst:
        dst.write(data, 1)
    out = tmp_path / "dem.tif"
    dem.import_local_dem(nan_tif, dom, out)
    with rasterio.open(out) as ds:
        assert ds.nodata == -9999.0
        arr = ds.read(1)
        assert np.isfinite(arr).all()                  # every NaN became the nodata value
        assert (arr == -9999.0).any()


# ------------------------------------------------------------------ record hygiene

def test_normalize_dem_source():
    assert dem.normalize_dem_source(None) == {"mode": "3dep", "path": None,
                                              "src_mtime": None}
    assert dem.normalize_dem_source("junk")["mode"] == "3dep"
    rec = dem.normalize_dem_source({"mode": "local", "path": Path(r"D:\g\dem.tif"),
                                    "src_mtime": "12.5"})
    assert rec == {"mode": "local", "path": r"D:\g\dem.tif", "src_mtime": 12.5}
    # unknown mode / bad mtime coerce; a missing file is NOT grounds for dropping the path
    rec = dem.normalize_dem_source({"mode": "usgs", "path": r"D:\gone.tif",
                                    "src_mtime": "soon"})
    assert rec == {"mode": "3dep", "path": r"D:\gone.tif", "src_mtime": None}


def test_dem_suffixes_frozen():
    assert dem.DEM_SUFFIXES == frozenset({".tif", ".tiff"})


# ------------------------------------------------------------------ app source pins

def _app_src():
    return (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def _fn_body(src: str, name: str) -> str:
    """The source slice from `def name` to the next def at the same indent."""
    m = re.search(rf"\n(    (?:async )?def {name}\b.*?)(?=\n    (?:async )?def |\n    @|\nclass )",
                  src, flags=re.S)
    assert m, f"{name} not found in app.py"
    return m.group(1)


def _code_only(body: str) -> str:
    """Body with the docstring and # comments stripped, so prose can't satisfy (or trip)
    a code-shaped assertion."""
    body = re.sub(r'""".*?"""', "", body, count=1, flags=re.S)
    return "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())


def test_state_key_saves_and_rehydrate_normalizes():
    src = _app_src()
    assert '"dem_source": _tokenize_paths(dict(dem_src() or {}))' in \
        _code_only(_fn_body(src, "_project_state"))
    assert "dem.normalize_dem_source(st.get(\"dem_source\"))" in \
        _code_only(_fn_body(src, "_rehydrate"))


def test_dispatch_routes_dem_pick_before_the_project_fallthrough():
    # _on_project_path silently eats purposes it doesn't recognize: the dem branch MUST
    # come first or a picked raster opens as a project.
    body = _code_only(_fn_body(_app_src(), "_dispatch_picked_result"))
    assert body.index('"dem_src_pick"') < body.index("_on_project_path")


def test_chain_never_imports_a_missing_source():
    body = _code_only(_fn_body(_app_src(), "_chain_dem"))
    assert "_dem_local_active()" in body and "is_file()" in body


def test_local_mode_is_desktop_gated():
    assert "IS_DESKTOP" in _code_only(_fn_body(_app_src(), "_dem_local_active"))


def test_dem_task_branches_on_the_payload_mode():
    body = _code_only(_fn_body(_app_src(), "dem_task"))
    assert "import_local_dem" in body and "fetch_dem" in body


def test_dem_done_marks_stale_and_surfaces_import_errors():
    body = _code_only(_fn_body(_app_src(), "_dem_done"))
    assert "_mark_stale_from_results()" in body
    assert "DemImportError" in body


def test_pane_gates_the_selector_to_desktop():
    body = _fn_body(_app_src(), "_pane_dem")
    assert "IS_DESKTOP" in _code_only(body)
    assert "panel_conditional" in body and "dem_src_mode" in body


def test_autosave_subscribes_the_source_record():
    assert "dem_src()" in _code_only(_fn_body(_app_src(), "_autosave_on_results"))


def test_reach_clear_keeps_the_source_choice():
    src = _app_src()
    # the pointer belongs to the PROJECT: cleared only by the full memory reset, never by
    # a reach redraw (the chain re-imports the linked raster for the new reach)
    assert "dem_src.set" not in _code_only(_fn_body(src, "_clear_reach_all"))
    assert "dem_src.set(dem.normalize_dem_source(None))" in \
        _code_only(_fn_body(src, "_reset_memory_state"))


def test_no_em_dash_in_the_new_dem_copy():
    # user-facing strings only: comments and docstrings are stripped before the scan
    src = _app_src()
    for fn in ("_pane_dem", "dem_local_src", "dem_status", "_dem_src_picked",
               "_dem_import_click", "_launch_dem_fetch", "_dem_done",
               "_mark_stale_from_results", "_pick_dem_raster",
               "_show_dem_typed_pick_modal", "_mirror_dem_src_mode"):
        assert "\u2014" not in _code_only(_fn_body(src, fn)), f"em dash in {fn}"
