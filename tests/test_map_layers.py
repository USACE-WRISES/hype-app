"""Map layers (hype_app/map_layers.py + the app wiring): record hygiene, loaders, and the
source pins that hold the feature's two structural invariants — reference layers are PATH
POINTERS (missing files stay in the list), and their z-slot is a Leaflet PANE declared once
at Map construction (append-order and the heal machinery can never scramble it)."""
from __future__ import annotations

import json
import re
from pathlib import Path

import numpy as np
import pytest

rasterio = pytest.importorskip("rasterio")
gpd = pytest.importorskip("geopandas")

from hype_app import map_layers as ml  # noqa: E402
from hype_app import ui_tree  # noqa: E402


# --------------------------------------------------------------------------- record hygiene

def test_classify_and_default_name():
    assert ml.classify_path(r"D:\GIS\ortho.TIF") == "raster"
    assert ml.classify_path("a/b/depth.vrt") == "raster"
    assert ml.classify_path("parcels.shp") == "vector"
    assert ml.classify_path("sites.GeoJSON") == "vector"
    assert ml.classify_path("notes.txt") is None
    assert ml.classify_path("") is None
    assert ml.default_name(r"D:\GIS\parcels.shp") == "parcels"


def test_new_layer_record_defaults():
    rec = ml.new_layer_record(r"D:\GIS\ortho.tif")
    assert re.fullmatch(r"[0-9a-f]{8}", rec["id"])
    assert rec["kind"] == "raster" and rec["name"] == "ortho"
    assert rec["opacity"] == ml.DEFAULT_OPACITY and rec["color"] == ml.DEFAULT_COLOR
    assert rec["visible"] is True


def test_normalize_tolerates_garbage_and_rederives_kind():
    raw = [
        None, "junk", {"name": "no path"},
        {"path": "notes.txt"},                              # unsupported suffix: dropped
        {"id": "aa11bb22", "path": r"D:\GIS\p.shp", "kind": "raster",   # lies about kind
         "opacity": "7", "color": "purple", "visible": 0},
        {"id": "aa11bb22", "path": r"D:\GIS\dupe.shp"},     # dup id: first wins
        {"path": r"D:\GIS\o.tif", "opacity": -3, "color": "#12AB34"},   # id minted
    ]
    out = ml.normalize_map_layers(raw)
    assert len(out) == 2
    a, b = out
    assert a["kind"] == "vector"                            # suffix wins over the stored kind
    assert a["opacity"] == 1.0                              # clamped
    assert a["color"] == ml.DEFAULT_COLOR                   # bad hex -> default
    assert a["visible"] is False
    assert b["kind"] == "raster" and b["opacity"] == 0.0 and b["color"] == "#12AB34"
    assert re.fullmatch(r"[0-9a-f]{8}", b["id"]) and b["name"] == "o"
    assert ml.normalize_map_layers(None) == []


def test_normalize_keeps_missing_files():
    # THE pointer rule: a record whose file is gone stays in the list — missing is a
    # display state (warn row + relink), never grounds for dropping the user's link.
    out = ml.normalize_map_layers([{"id": "deadbeef",
                                    "path": r"D:\definitely\not\there.tif"}])
    assert [r["id"] for r in out] == ["deadbeef"]


def test_vector_style_math_and_point_pane():
    st = ml.vector_style("#336699", 0.8)
    assert st["color"] == st["fillColor"] == "#336699"
    assert st["opacity"] == 0.8 and st["fillOpacity"] == 0.2      # faint fill at 25%
    assert ml.vector_style("#000000", 7.0)["opacity"] == 1.0      # clamped
    # point_style is construction-only on the client: the pane MUST ride in it or point
    # features land in the default overlay pane above app layers.
    assert ml.vector_point_style()["pane"] == ml.PANE_REF


# --------------------------------------------------------------------------- vector loading

def _write_geojson(tmp_path, name="sites.geojson"):
    fc = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "properties": {"label": "A", "when": "2026-08-03"},
         "geometry": {"type": "Point", "coordinates": [-72.51, 43.52]}},
        {"type": "Feature", "properties": {"label": "B"},
         "geometry": {"type": "LineString",
                      "coordinates": [[-72.53, 43.50], [-72.50, 43.53]]}},
    ]}
    p = tmp_path / name
    p.write_text(json.dumps(fc), encoding="utf-8")
    return p


def test_load_vector_fc_geojson(tmp_path):
    fc, bounds, err, simplified = ml.load_vector_fc(_write_geojson(tmp_path))
    assert err is None and simplified is False
    assert fc["type"] == "FeatureCollection" and len(fc["features"]) == 2
    # attributes are deliberately dropped (display-only; timestamps break the payload)
    assert all(f["properties"] == {} for f in fc["features"])
    (s, w), (n, e) = bounds
    assert s == pytest.approx(43.50) and n == pytest.approx(43.53)
    assert w == pytest.approx(-72.53) and e == pytest.approx(-72.50)


def test_load_vector_fc_shapefile_reprojects_and_needs_prj(tmp_path):
    from shapely.geometry import LineString
    gdf = gpd.GeoDataFrame({"name": ["r"]},
                           geometry=[LineString([(500000.0, 4200000.0),
                                                 (500100.0, 4200050.0)])],
                           crs="EPSG:32617")
    shp = tmp_path / "reach.shp"
    gdf.to_file(shp)
    fc, bounds, err, _ = ml.load_vector_fc(shp)
    assert err is None
    lon, lat = fc["features"][0]["geometry"]["coordinates"][0]
    assert -85.0 < lon < -80.0 and 35.0 < lat < 40.0        # really reprojected to 4326
    # same shapefile with its .prj deleted: unknowable CRS is a per-layer reason, not a crash
    (tmp_path / "reach.prj").unlink()
    fc2, bounds2, err2, _ = ml.load_vector_fc(shp)
    assert fc2 is None and bounds2 is None and ".prj" in err2


def test_load_vector_fc_budgets(tmp_path):
    from shapely.geometry import LineString, Point
    many = gpd.GeoDataFrame(geometry=[Point(-72.5, 43.5), Point(-72.6, 43.6)],
                            crs="EPSG:4326")
    p = tmp_path / "many.geojson"
    many.to_file(p, driver="GeoJSON")
    fc, _b, err, _ = ml.load_vector_fc(p, max_features=1)
    assert fc is None and "too many features" in err
    # vertex budget: a densely-sampled straight line simplifies under the cap
    xs = np.linspace(-72.6, -72.5, 400)
    dense = gpd.GeoDataFrame(geometry=[LineString([(x, 43.5) for x in xs])],
                             crs="EPSG:4326")
    p2 = tmp_path / "dense.geojson"
    dense.to_file(p2, driver="GeoJSON")
    fc2, _b2, err2, simplified = ml.load_vector_fc(p2, max_vertices=50)
    assert err2 is None and simplified is True
    n_pts = len(fc2["features"][0]["geometry"]["coordinates"])
    assert n_pts <= 50


# --------------------------------------------------------------------------- raster loading

def _write_tif(tmp_path, name, *, bands=1, dtype="float32", crs="EPSG:32617"):
    from rasterio.transform import from_origin
    h, w = 18, 24
    kw = dict(driver="GTiff", height=h, width=w, count=bands, dtype=dtype,
              transform=from_origin(500000.0, 4200000.0, 5.0, 5.0))
    if crs:
        kw["crs"] = crs
    p = tmp_path / name
    with rasterio.open(p, "w", **kw) as dst:
        for b in range(1, bands + 1):
            if dtype == "uint8":
                data = np.tile(np.linspace(10, 240, w, dtype="uint8"), (h, 1))
            else:
                data = np.linspace(95.0, 100.0, h * w).reshape(h, w).astype("float32")
            dst.write(data, b)
    return p


def test_load_raster_overlay_single_band(tmp_path):
    ov, err = ml.load_raster_overlay(_write_tif(tmp_path, "dem.tif"))
    assert err is None
    assert ov["url"].startswith("data:image/png;base64,")
    (s, w), (n, e) = ov["bounds"]
    assert -85.0 < w < e < -80.0 and 35.0 < s < n < 40.0    # EPSG:4326 bounds


def test_load_raster_overlay_rgb(tmp_path):
    ov, err = ml.load_raster_overlay(_write_tif(tmp_path, "ortho.tif", bands=3,
                                                dtype="uint8"))
    assert err is None and ov["url"].startswith("data:image/png;base64,")


def test_load_raster_overlay_errors(tmp_path):
    ov, err = ml.load_raster_overlay(tmp_path / "gone.tif")
    assert ov is None and err
    ov2, err2 = ml.load_raster_overlay(_write_tif(tmp_path, "nocrs.tif", crs=None))
    assert ov2 is None and "projection" in err2


def _write_tif4(tmp_path, name, *, band4, tag_alpha):
    """4-band uint8 tif whose 4th band holds `band4` everywhere. colorinterp tags the
    band alpha or leaves it undefined (the NAIP shape: band 4 = near-infrared)."""
    from rasterio.enums import ColorInterp
    from rasterio.transform import from_origin

    h, w = 18, 24
    p = tmp_path / name
    with rasterio.open(p, "w", driver="GTiff", height=h, width=w, count=4,
                       dtype="uint8", crs="EPSG:32617",
                       transform=from_origin(500000.0, 4200000.0, 5.0, 5.0)) as dst:
        for b in range(1, 4):
            dst.write(np.full((h, w), 120, dtype="uint8"), b)
        dst.write(np.full((h, w), band4, dtype="uint8"), 4)
        interp = ColorInterp.alpha if tag_alpha else ColorInterp.undefined
        dst.colorinterp = (ColorInterp.red, ColorInterp.green, ColorInterp.blue, interp)
    return p


def _overlay_alpha(ov):
    import base64
    import io

    from matplotlib import image as mpimg

    raw = base64.b64decode(ov["url"].split(",", 1)[1])
    return mpimg.imread(io.BytesIO(raw))[..., 3]


def test_naip_nir_band_is_not_alpha(tmp_path):
    """NAIP's 4th band is near-infrared (colorinterp undefined), NOT transparency.
    Multiplying it in as alpha rendered whole aerials half transparent with water
    nearly invisible (the LL01096 report). Valid pixels must stay fully opaque;
    only reprojection-edge nodata may be transparent."""
    ov, err = ml.load_raster_overlay(_write_tif4(tmp_path, "naip.tif",
                                                 band4=128, tag_alpha=False))
    assert err is None
    a = _overlay_alpha(ov)
    nz = a[a > 0.01]
    assert nz.size and float(nz.min()) > 0.99


def test_true_alpha_band_is_honored(tmp_path):
    ov, err = ml.load_raster_overlay(_write_tif4(tmp_path, "rgba.tif",
                                                 band4=128, tag_alpha=True))
    assert err is None
    a = _overlay_alpha(ov)
    nz = a[a > 0.01]
    assert nz.size and abs(float(nz.mean()) - 128.0 / 255.0) < 0.05


# --------------------------------------------------------------------------- tree model

def test_maplyr_node_sits_beneath_site_reports():
    ids = [n["id"] for n in ui_tree.NODES]
    assert ids.index("report.cmp") < ids.index("maplyr") < ids.index("base")
    node = ui_tree.NODE["maplyr"]
    assert node["group"] is True and node["check"] is True and node["layers"] == ()
    assert ui_tree.NODE_STEP["maplyr"] is None              # never step-gated
    assert "maplyr" in ui_tree.GROUP_IDS


def test_tree_payload_inserts_extras_after_their_parent():
    extra = {"id": "ml:abc12345", "label": "parcels", "parent": "maplyr", "depth": 1,
             "group": False, "status": "warn", "check": True, "disabled": False,
             "dim": False}
    payload = ui_tree.build_tree_payload(extra_rows=[extra])
    ids = [n["id"] for n in payload["nodes"]]
    assert ids[ids.index("maplyr") + 1] == "ml:abc12345"    # directly after the parent,
    assert ids[-1] == "base.hydro"                          # never appended at the end
    hidden = ui_tree.build_tree_payload(hidden=("maplyr",), extra_rows=[extra])
    assert "ml:abc12345" not in [n["id"] for n in hidden["nodes"]]


# --------------------------------------------------------------------------- app-source pins

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


def test_panes_are_declared_once_at_map_construction():
    """The two custom panes are the ONLY z-order mechanism (stacking is otherwise pure
    add-order, which the heal machinery scrambles), and Map.panes must never be mutated
    after construction — the client re-renders the whole map on a panes change."""
    src = _app_src()
    build = src[src.index("def _build_map"):src.index("_MAP = _build_map()")]
    assert "panes={ml_mod.PANE_TERRAIN" in build.replace(" ", "").replace("\n", "") or \
        "panes={" in build
    assert "ml_mod.PANE_TERRAIN" in build and "ml_mod.PANE_REF" in build
    assert '"zIndex": 320' in build and '"zIndex": 340' in build
    assert '"pointerEvents": "none"' in build
    # constructor-only: no later assignment anywhere in the app
    assert not re.search(r"\.panes\s*=", src)
    assert src.count('"zIndex": 340') == 1


def test_terrain_rasters_ride_the_terrain_pane():
    src = _app_src()
    assert "pane=ml_mod.PANE_TERRAIN" in _code_only(_fn_body(src, "_show_dem_overlay"))
    assert "pane=ml_mod.PANE_TERRAIN" in _code_only(_fn_body(src, "_show_carve_overlay"))


def test_heal_paths_carry_the_pane():
    """Every widget-rebuild path must carry `pane` (and for GeoJSON the options list that
    delivers it to child paths) or a healed layer silently jumps to the default overlay
    pane above app layers."""
    src = _app_src()
    clone_v = _code_only(_fn_body(src, "_clone_vector"))
    assert "fresh.pane = pane" in clone_v
    assert 'getattr(lyr, "options"' in clone_v
    assert 'pane=getattr(lyr, "pane", "") or ""' in _code_only(_fn_body(src, "_clone_layer"))
    assert 'pane=getattr(lyr, "pane", "") or ""' in _code_only(
        _fn_body(src, "_reassert_layers"))


def test_owner_effect_disciplines():
    body = _code_only(_fn_body(_app_src(), "_sync_map_layers"))
    assert "anyio.to_thread.run_sync" in body               # loads never block the loop
    assert "_REPORT_MPL_LOCK" in body                       # worker-thread mpl serializes
    assert "pane=ml_mod.PANE_REF" in body                   # both widget kinds ride the pane
    assert '"pane"' in body and "options" in body           # ...GeoJSON via the options list
    assert "_hidden_keys.discard" in body                   # removal can't resurrect later
    assert "_tag_hz" in body                                # vectors opt into sweep/verify


def test_map_layers_persist_restore_reset_autosave():
    src = _app_src()
    assert '"map_layers": _tokenize_paths' in _code_only(_fn_body(src, "_project_state"))
    assert "normalize_map_layers" in _code_only(_fn_body(src, "_rehydrate"))
    reset = _code_only(_fn_body(src, "_reset_memory_state"))
    assert "map_layers.set([])" in reset and "_ml_cache.clear()" in reset
    auto = _code_only(_fn_body(src, "_autosave_on_results"))
    assert "map_layers()" in auto and "map_layers_ver()" in auto


def test_cloud_hides_the_maplyr_node():
    assert 'hidden.add("maplyr")' in _code_only(_fn_body(_app_src(), "_push_tree_state"))


def test_maplayer_purposes_branch_before_the_project_fallthrough():
    """_dispatch_picked_result ends with an unconditional _on_project_path(...) that
    silently eats unknown purposes — the maplayer branches must come first."""
    body = _code_only(_fn_body(_app_src(), "_dispatch_picked_result"))
    assert body.index('"maplayer_add"') < body.index("_on_project_path")
    assert body.index('"maplayer_relink:"') < body.index("_on_project_path")


def test_ref_pane_css_kills_pointer_events():
    """Leaflet's own CSS re-enables pointer-events on path.leaflet-interactive children,
    so the pane's inline pointerEvents:none is not enough — a filled reference polygon
    would eat the map-clear click without the descendant rule."""
    css = (Path(__file__).resolve().parents[1] / "www" / "styles.css").read_text(
        encoding="utf-8")
    m = re.search(r"\.leaflet-hype-ref-pane \*[^}]*pointer-events:\s*none\s*!important",
                  css, flags=re.S)
    assert m, "hype-ref pane descendant pointer-events rule missing"
    assert ".hype-tree-status.s-warn" in css                # missing-file tree dot


def test_maplayer_picker_never_uses_the_shell_bridge():
    """The shipped WinForms shell has no map-layer message type and silently drops
    unknown commands (the pick would hang forever) — the launcher must go straight to
    the tk child / typed modal, never send_custom_message('hype_desktop', ...)."""
    body = _code_only(_fn_body(_app_src(), "_pick_map_layers"))
    assert "hype_desktop" not in body
    assert "pick_task(payload)" in body
    assert '"kind": "maplayer"' in body


def test_ml_pane_renderers_never_ride_map_layers_ver():
    """maplyr_rows and the per-layer pane subscribe the record list + _ml_paint ONLY —
    map_layers_ver bumps on every slider drag and would remount the slider mid-drag."""
    src = _app_src()
    for fn in ("maplyr_rows", "_pane_ml_layer"):
        body = _code_only(_fn_body(src, fn))
        assert "map_layers_ver" not in body, fn
        assert "_ml_paint()" in body, fn


def test_ml_select_routes_before_the_node_guard():
    """Selecting an ml:* tree row must reach sel_node (per-layer pane) instead of being
    dropped by the static-NODE membership guard."""
    body = _code_only(_fn_body(_app_src(), "_tree_event_dispatch"))
    assert body.index('startswith("ml:")') < body.index("in ui_tree.NODE")
