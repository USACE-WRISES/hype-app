"""Rainbow residence-time color modes for the flow-path particle animation.

"Color by" on the Flow paths pane: solid (the classic swatch), total (each particle
fixed at its path's total residence-time color), elapsed (the color ages with time in
transit). Both rainbow modes share one log-scaled turbo mapping and draw a legend, in
the live client (path_anim.js / mesh3d.js) and in the server-rendered exports
(video.py). These tests pin the mapping math, the export rendering, and the wiring.
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


# ---------------------------------------------------------------- mapping math

def test_time_norm_log_endpoints_and_clamp():
    rng = (1.0, 100.0)
    assert video._time_norm(1.0, rng) == pytest.approx(0.0)
    assert video._time_norm(100.0, rng) == pytest.approx(1.0)
    assert video._time_norm(10.0, rng) == pytest.approx(0.5)   # log midpoint, not 10%
    # below the floor clamps to the coolest color; above the ceiling to the warmest
    assert video._time_norm(0.01, rng) == 0.0
    assert video._time_norm(1e6, rng) == 1.0
    # vectorized form keeps ordering
    f = video._time_norm([2.0, 5.0, 50.0], rng)
    assert f[0] < f[1] < f[2]


def test_time_range_degenerate_spreads_half_decade():
    parts = [{"td": 7.0}, {"td": 7.0}]
    lo, hi = video._time_range(parts)
    assert lo == pytest.approx(7.0 / 10 ** 0.5)
    assert hi == pytest.approx(7.0 * 10 ** 0.5)
    # the original value sits at the exact center of the scale
    assert video._time_norm(7.0, (lo, hi)) == pytest.approx(0.5)


def test_particle_colors_total_static_elapsed_ages():
    parts = [{"td": 1.0, "dur": 1000.0, "phase": 0.0},
             {"td": 100.0, "dur": 100000.0, "phase": 0.0}]
    rng = video._time_range(parts)
    tot0 = video._particle_colors(parts, "total", rng, 0.0)
    tot1 = video._particle_colors(parts, "total", rng, 500.0)
    assert np.allclose(tot0, tot1)                    # total: time-invariant
    assert not np.allclose(tot0[0], tot0[1])          # ...but path-dependent
    # the slow path (index 1) ages visibly within its loop; times chosen so the
    # fast path (index 0) is mid-loop too, not at its release point
    el0 = video._particle_colors(parts, "elapsed", rng, 10_500.0)
    el1 = video._particle_colors(parts, "elapsed", rng, 90_700.0)
    assert not np.allclose(el0[1], el1[1])
    # the quickest displayed path IS the scale floor: it stays at the coolest color
    # (young until it outlives the quickest path, by design)
    assert np.allclose(el0[0], el1[0])
    # at the end of its loop a particle wears its total-time color
    el_end = video._particle_colors(parts, "elapsed", rng, 99_999.9)
    assert np.allclose(el_end[1], tot0[1], atol=0.02)


# ------------------------------------------------------------- export rendering

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


def _bounds(hz_dir):
    from pyproj import Transformer

    tr = Transformer.from_crs(3857, 4326, always_xy=True)
    w, s = tr.transform(-11_000_100, -1_000_300)
    e, n = tr.transform(-10_999_800, -999_900)
    return {"west": w, "south": s, "east": e, "north": n}


def _payload(hz_dir, **over):
    base = {
        "hz_dir": str(hz_dir),
        "bounds4326": _bounds(hz_dir),
        "basemap": "none",
        "visible_classes": ["hyporheic"],
        "line": {"show": False, "weight": 2.0, "opacity": 0.9, "mode": "class"},
        "class_colors": {"hyporheic": "#0d9488"},
        "anim": {"on": True, "speed": 3.0, "style": "dots", "color": "#ff2bd6",
                 "mode": "total"},
        "duration_s": 0.3, "fps": 5, "width_px": 480,
    }
    base.update(over)
    return base


def _px(path):
    from PIL import Image

    return np.asarray(Image.open(path).convert("RGB")).reshape(-1, 3).astype(int)


def test_still_rainbow_draws_legend_and_no_solid_color(hz_dir, tmp_path):
    res = video.build_flowpath_still(_payload(hz_dir), tmp_path / "still",
                                     log=lambda *_: None)
    flat = _px(res["path"])
    # the legend bar spans the full turbo scale, so its red, green and blue bands
    # must land in the frame regardless of what the six particles drew
    assert ((flat[:, 0] > 140) & (flat[:, 1] < 90) & (flat[:, 2] < 90)).any(), "red"
    assert ((flat[:, 1] > 130) & (flat[:, 0] < 130) & (flat[:, 2] < 110)).any(), "green"
    assert ((flat[:, 2] > 120) & (flat[:, 0] < 90)).any(), "blue"
    # and the solid swatch color must NOT be painted in a rainbow mode
    magenta = (flat[:, 0] > 200) & (flat[:, 2] > 150) & (flat[:, 1] < 120)
    assert not magenta.any()


def test_still_solid_unchanged_has_magenta_no_legend_red(hz_dir, tmp_path):
    p = _payload(hz_dir)
    p["anim"] = dict(p["anim"], mode="solid", style="comet")
    res = video.build_flowpath_still(p, tmp_path / "still", log=lambda *_: None)
    flat = _px(res["path"])
    assert ((flat[:, 0] > 200) & (flat[:, 2] > 150) & (flat[:, 1] < 120)).any()
    # no legend in solid mode: the turbo red band is absent
    assert not ((flat[:, 0] > 140) & (flat[:, 1] < 90) & (flat[:, 2] < 90)).any()


@pytest.mark.parametrize("style,mode", [("comet", "total"), ("dots", "elapsed")])
def test_video_rainbow_smoke_both_styles(hz_dir, tmp_path, monkeypatch, style, mode):
    monkeypatch.setattr(video, "resolve_ffmpeg", lambda log=print: None)
    p = _payload(hz_dir)
    p["anim"] = dict(p["anim"], style=style, mode=mode)
    res = video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None)
    assert res["format"] == "webp"
    flat = _px(res["path"])           # PIL reads webp frame 0
    assert ((flat[:, 0] > 140) & (flat[:, 1] < 90) & (flat[:, 2] < 90)).any()


# ------------------------------------------------------------------ wiring pins

def test_app_mode_wiring():
    assert 'FP_ANIM_MODES = ("solid", "total", "elapsed")' in APP_SRC
    # the settings message and BOTH export payloads carry the mode
    assert '"mode": mode' in APP_SRC
    assert APP_SRC.count('"mode": fp_anim_mode_v()') == 2
    # nonce event input: ignore_init would eat the first click (the 2026-07-25 lesson)
    m = re.search(r"@reactive\.event\(input\.fp_anim_mode_evt[^)]*\)", APP_SRC)
    assert m and "ignore_init" not in m.group(0)
    # a swatch click returns to solid, and the memory reset restores solid
    assert 'fp_anim_mode_v.set("solid")' in APP_SRC
    # the pane offers the three color-by buttons ahead of the swatch row
    assert '"Color by"' in APP_SRC
    assert "fp_anim_mode_evt" in APP_SRC
    for entry in ('("solid", "Solid"', '("total", "Total time"',
                  '("elapsed", "Elapsed time"'):
        assert entry in APP_SRC


def test_client_2d_wiring():
    assert "var TURBO" in PANIM_SRC
    assert "function drawLegend" in PANIM_SRC
    assert 'msg.mode === "solid" || msg.mode === "total" || msg.mode === "elapsed"' \
        in PANIM_SRC
    assert "Total residence time (days)" in PANIM_SRC
    assert "Elapsed residence time (days)" in PANIM_SRC


def test_client_3d_wiring():
    assert 'mode: "solid"' in MESH3D_SRC
    assert "function turboCtf" in MESH3D_SRC
    assert "function animLegend" in MESH3D_SRC
    assert 'msg.mode === "solid" || msg.mode === "total" || msg.mode === "elapsed"' \
        in MESH3D_SRC
    assert "window.__hypeFpAnim.mode" in MESH3D_SRC


def test_no_em_dashes_in_new_copy():
    # user-facing strings introduced by the rainbow modes (project copy rule)
    i0 = APP_SRC.index('"Color by"')
    i1 = APP_SRC.index('"Particle color"', i0)
    assert "—" not in APP_SRC[i0:i1]
    for src in (PANIM_SRC, MESH3D_SRC):
        for title in ("Total residence time (days)", "Elapsed residence time (days)"):
            assert title in src
            assert "—" not in title
