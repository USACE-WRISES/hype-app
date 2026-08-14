"""Residence-time coloring for the static flow-path LINES + the shared legends.

Round 13 of the flow-path display: the pane's Line "Color by" gains "total" (one fixed
turbo color per path) and "elapsed" (a gradient along each path toward its total-time
color at exit; the live 2D map approximates with the total color, the 3D view and the
captures grade for real). One log-scale turbo mapping is shared by lines, particles,
the in-pane legend, the on-map canvas legend, the 3D overlay, and the exports. These
tests pin the shared color helpers, the export rendering (including legend-without-
animation), and the wiring on all surfaces.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from hype_app import video

ROOT = Path(__file__).resolve().parents[1]
APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")
PANIM_SRC = (ROOT / "www" / "path_anim.js").read_text(encoding="utf-8")
MESH3D_SRC = (ROOT / "www" / "mesh3d.js").read_text(encoding="utf-8")
STYLES_SRC = (ROOT / "www" / "styles.css").read_text(encoding="utf-8")


# ------------------------------------------------------------- shared color math

def test_time_hex_colors_span_turbo():
    from matplotlib import colormaps
    from matplotlib.colors import to_hex

    rng = (1.0, 100.0)
    cols = video.time_hex_colors([1.0, 10.0, 100.0], rng)
    assert cols[0] == to_hex(tuple(colormaps["turbo"](0.0)[:3]))
    assert cols[2] == to_hex(tuple(colormaps["turbo"](1.0)[:3]))
    assert cols[1] == to_hex(tuple(colormaps["turbo"](0.5)[:3]))   # log midpoint
    assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in cols)


def test_time_range_days_degenerate_and_stops():
    lo, hi = video.time_range_days([7.0, 7.0])
    assert lo == pytest.approx(7.0 / 10 ** 0.5)
    assert hi == pytest.approx(7.0 * 10 ** 0.5)
    stops = video.turbo_css_stops()
    assert len(stops) == 13
    assert stops[0] != stops[-1]
    assert all(re.fullmatch(r"#[0-9a-f]{6}", c) for c in stops)


def test_legend_label_rule():
    assert video.legend_label("total", None) == "Total residence time (days)"
    assert video.legend_label(None, "elapsed") == "Elapsed residence time (days)"
    assert video.legend_label("total", "total") == "Total residence time (days)"
    assert video.legend_label("elapsed", "total") == "Residence time (days)"
    assert video.legend_label("solid", "class") == "Residence time (days)"  # degenerate


# ---------------------------------------------------------------- gradient drawing

def _one_long_path_gdf():
    import geopandas as gpd
    from shapely.geometry import LineString

    xs = np.linspace(-11_000_000, -10_999_000, 40)
    return gpd.GeoDataFrame(
        [{"particleid": 1, "hz_class": "hyporheic", "total_time_d": 200.0,
          "geometry": LineString([(x, -1_000_000.0) for x in xs])}], crs=3857)


def test_legacy_elapsed_gradient_many_colors():
    from matplotlib.figure import Figure

    gdf = _one_long_path_gdf()
    ax = Figure().add_subplot()
    rng = (0.5, 500.0)
    video._legacy_rainbow_lines(ax, gdf, {"hyporheic"}, "elapsed", rng, 2.0, 0.9)
    (lc,) = ax.collections
    cols = np.asarray(lc.get_colors())
    assert len(cols) == 39                                  # one per segment
    assert len({tuple(np.round(c, 3)) for c in cols}) > 8   # a real gradient
    # entry is cooler (blue-dominant) than exit (red-dominant) on turbo
    assert cols[0][2] > cols[0][0]
    assert cols[-1][0] > cols[-1][2]


def test_legacy_total_one_color_per_path():
    from matplotlib.figure import Figure

    gdf = _one_long_path_gdf()
    ax = Figure().add_subplot()
    video._legacy_rainbow_lines(ax, gdf, {"hyporheic"}, "total", (0.5, 500.0),
                                2.0, 0.9)
    (lc,) = ax.collections
    assert len(np.asarray(lc.get_colors())) == 1            # whole path, one color


def test_draw_elapsed_lines_from_scene_features():
    from matplotlib.figure import Figure

    lon = np.linspace(-98.0, -97.99, 30)
    feat = {"type": "Feature",
            "properties": {"total_time_d": 42.0, "style": {"color": "#123456"}},
            "geometry": {"type": "LineString",
                         "coordinates": [[x, 30.0] for x in lon]}}
    ax = Figure().add_subplot()
    video._draw_elapsed_lines(ax, lambda a: a, [feat],
                              {"weight": 2.0, "opacity": 0.9}, 2.0, (1.0, 100.0))
    (lc,) = ax.collections
    assert len(np.asarray(lc.get_colors())) == 29


# ------------------------------------------------------------------ still exports

@pytest.fixture()
def hz_dir(tmp_path):
    import geopandas as gpd
    from shapely.geometry import LineString

    rows = []
    for i in range(6):
        y = -1_000_000 - i * 30.0
        rows.append({
            "particleid": i + 1,
            "hz_class": "hyporheic",
            "total_time_d": [0.5, 2.0, 8.0, 30.0, 120.0, 500.0][i],
            "length_m": 100.0,
            "geometry": LineString([(-11_000_000 + 0, y, 0.0),
                                    (-11_000_000 + 60, y + 15, -1.0),
                                    (-11_000_000 + 120, y, 0.0)]),
        })
    out = tmp_path / "hz"
    out.mkdir()
    gpd.GeoDataFrame(rows, crs=3857).to_file(out / "hz_paths_3d.gpkg",
                                             layer="hz_paths_3d", driver="GPKG")
    return out


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("hype_app.mesh.fetch_basemap_image", lambda *a, **k: None)


def _bounds():
    from pyproj import Transformer

    tr = Transformer.from_crs(3857, 4326, always_xy=True)
    w, s = tr.transform(-11_000_100, -1_000_300)
    e, n = tr.transform(-10_999_800, -999_900)
    return {"west": w, "south": s, "east": e, "north": n}


def _scene_with_baked_lines(hz_dir, rng):
    """A minimal _gather_map_scene twin: the hz paths layer data in 4326 with the
    per-feature total-time colors baked, and a color-less layer style (the rainbow
    contract)."""
    import geopandas as gpd

    g = gpd.read_file(hz_dir / "hz_paths_3d.gpkg", layer="hz_paths_3d").to_crs(4326)
    cols = video.time_hex_colors([float(t) for t in g["total_time_d"]], rng)
    feats = []
    for (_, row), c in zip(g.iterrows(), cols):
        feats.append({"type": "Feature",
                      "properties": {"total_time_d": float(row["total_time_d"]),
                                     "style": {"color": c}},
                      "geometry": row.geometry.__geo_interface__})
    return {"items": [{"kind": "vector", "key": "hz_paths_hyporheic",
                       "data": {"type": "FeatureCollection", "features": feats},
                       "style": {"weight": 2.0, "opacity": 0.9},
                       "point_style": {}}],
            "labels": []}


def _px(path):
    from PIL import Image

    return np.asarray(Image.open(path).convert("RGB")).reshape(-1, 3).astype(int)


def test_still_line_total_no_anim_draws_lines_and_legend(hz_dir, tmp_path):
    rng = video.time_range_days([0.5, 2.0, 8.0, 30.0, 120.0, 500.0])
    payload = {
        "hz_dir": str(hz_dir), "bounds4326": _bounds(), "basemap": "none",
        "visible_classes": ["hyporheic"], "class_colors": {"hyporheic": "#0d9488"},
        "anim": {"on": False},
        "line": {"show": True, "weight": 2.0, "opacity": 0.9, "mode": "total",
                 "color": "#0d9488"},
        "scene": _scene_with_baked_lines(hz_dir, rng),
        "width_px": 480,
    }
    res = video.build_flowpath_still(payload, tmp_path / "still", log=lambda *_: None)
    flat = _px(res["path"])
    # legend + warm/cool line ends: the full turbo span must land in the frame even
    # though the animation is OFF (the old code drew a legend only for particles)
    assert ((flat[:, 0] > 140) & (flat[:, 1] < 90) & (flat[:, 2] < 90)).any(), "red"
    assert ((flat[:, 2] > 120) & (flat[:, 0] < 90)).any(), "blue"


def test_scene_layer_honors_baked_feature_colors():
    # The rainbow contract end to end: a color-less layer style + per-feature
    # properties.style colors -> one segment batch per baked color.
    from matplotlib.figure import Figure

    feats = [{"type": "Feature",
              "properties": {"style": {"color": c}},
              "geometry": {"type": "LineString",
                           "coordinates": [[-98.0, 30.0 + i * 0.01],
                                           [-97.9, 30.0 + i * 0.01]]}}
             for i, c in enumerate(("#112233", "#aabbcc"))]
    ax = Figure().add_subplot()
    video._draw_vector_layer(ax, lambda a: a,
                             {"data": {"features": feats},
                              "style": {"weight": 2.0, "opacity": 0.9}}, 2.0)
    cols = {tuple(np.round(np.asarray(lc.get_colors())[0], 3)) for lc in ax.collections}
    assert len(cols) == 2                       # the layer style did NOT flatten them


def test_still_line_elapsed_gradient_smoke(hz_dir, tmp_path):
    rng = video.time_range_days([0.5, 2.0, 8.0, 30.0, 120.0, 500.0])
    payload = {
        "hz_dir": str(hz_dir), "bounds4326": _bounds(), "basemap": "none",
        "visible_classes": ["hyporheic"], "class_colors": {"hyporheic": "#0d9488"},
        "anim": {"on": False},
        "line": {"show": True, "weight": 2.0, "opacity": 0.9, "mode": "elapsed",
                 "color": "#0d9488"},
        "scene": _scene_with_baked_lines(hz_dir, rng),
        "width_px": 480,
    }
    res = video.build_flowpath_still(payload, tmp_path / "still", log=lambda *_: None)
    flat = _px(res["path"])
    assert ((flat[:, 0] > 140) & (flat[:, 1] < 90) & (flat[:, 2] < 90)).any()


# ------------------------------------------------------------------- wiring pins

def test_app_line_mode_wiring():
    assert 'FP_LINE_MODES = ("class", "total", "elapsed")' in APP_SRC
    # restore maps the retired vocabulary (v1.0.0 "single", short-lived "solid")
    # onto "class"; custom line colors are gone entirely
    assert 'if _flm in ("solid", "single"):' in APP_SRC
    assert "fp_line_color_v" not in APP_SRC
    m = re.search(r"@reactive\.event\(input\.fp_line_mode_evt[^)]*\)", APP_SRC)
    assert m and "ignore_init" not in m.group(0)
    assert "def _bake_fp_line_colors" in APP_SRC
    assert "def _fp_time_range" in APP_SRC
    assert "def fp_time_legend" in APP_SRC
    assert '"lmode": lmode' in APP_SRC          # hype_fp_anim + hype3d_style carry it
    assert '"trng"' in APP_SRC
    # the three line Color-by entries (labels distinct from the anim row's)
    for entry in ('("class", "Class"',
                  '("total", "Total time"', '("elapsed", "Elapsed"'):
        assert entry in APP_SRC
    # rainbow layer styles must NOT carry a color (per-feature bake shows through)
    assert 'st.pop("color", None)' in APP_SRC


def test_app_elapsed_canvas_wiring():
    # the anim message carries the line stroke style for the 2-D gradient canvas...
    assert '"lw": lw, "lop": lop' in APP_SRC
    # ...and weight/opacity/show tweaks re-send it while elapsed is active
    assert APP_SRC.count('if fp_line_mode_v() == "elapsed":') >= 3


def test_video_class_color_pick():
    src = (ROOT / "hype_app" / "video.py").read_text(encoding="utf-8")
    # non-rainbow lines always wear the class identity colors; no custom color path
    assert 'color = class_colors.get(cls, "#0d9488")' in src
    assert src.count('line.get("mode") or "class"') == 2


def test_client_2d_line_wiring():
    assert 'lmode: "class"' in PANIM_SRC
    assert "function lineRainbow" in PANIM_SRC
    assert "function lineElapsed" in PANIM_SRC
    # the true elapsed gradient on the 2-D map: offscreen cache + pan-offset blit,
    # rebuilt on zoom/data/style changes, tracked mid-drag via the "move" binding
    assert "function buildLineCache" in PANIM_SRC
    assert "function blitLines" in PANIM_SRC
    assert "msg.lw" in PANIM_SRC and "msg.lop" in PANIM_SRC
    assert 'm.on("move", onMove)' in PANIM_SRC
    assert "function staticDraw" in PANIM_SRC
    assert "function legendLabel" in PANIM_SRC
    assert "msg.lmode" in PANIM_SRC


def test_client_3d_line_wiring():
    assert "function lineRainbowScalars" in MESH3D_SRC
    assert "S.lineMode3" in MESH3D_SRC
    assert "msg.trng" in MESH3D_SRC
    assert "function legendLabel3" in MESH3D_SRC
    assert "p0:" in MESH3D_SRC                  # per-path slice into the merged points


def test_pane_legend_css_and_copy():
    assert ".hype-pane-legend" in STYLES_SRC
    # no em dashes in the new user-facing copy (project rule); scan only this
    # renderer, not whatever function follows it
    i = APP_SRC.index("def fp_time_legend")
    seg = APP_SRC[i:APP_SRC.index("def ", i + 10)]
    assert "—" not in seg
    for title in ("Total residence time (days)", "Elapsed residence time (days)"):
        assert title in (ROOT / "hype_app" / "video.py").read_text(encoding="utf-8")
