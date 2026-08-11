"""hype_app.video: server-rendered flow-path animation export."""
from __future__ import annotations

import numpy as np
import pytest

from hype_app import video


@pytest.fixture()
def hz_dir(tmp_path):
    """A minimal hz_paths_3d.gpkg with two classes in EPSG:3857-friendly coords."""
    import geopandas as gpd
    from shapely.geometry import LineString

    rows = []
    for i in range(6):
        y = -1_000_000 - i * 30.0
        rows.append({
            "particleid": i + 1,
            "hz_class": "hyporheic" if i % 2 == 0 else "gaining",
            "total_time_d": 5.0 + i,
            "length_m": 100.0,
            "geometry": LineString([(-11_000_000 + 0, y, 0.0),
                                    (-11_000_000 + 60, y + 15, -1.0),
                                    (-11_000_000 + 120, y, 0.0)]),
        })
    gdf = gpd.GeoDataFrame(rows, crs=3857)
    out = tmp_path / "hz"
    out.mkdir()
    gdf.to_file(out / "hz_paths_3d.gpkg", layer="hz_paths_3d", driver="GPKG")
    return out


def payload(hz_dir, **over):
    base = {
        "hz_dir": str(hz_dir),
        "bounds4326": {"west": -98.81, "south": -8.88, "east": -98.79, "north": -8.86},
        "basemap": "topo",
        "visible_classes": ["hyporheic", "gaining"],
        "line": {"show": True, "weight": 2.0, "opacity": 0.9,
                 "mode": "class", "color": "#0d9488"},
        "class_colors": {"hyporheic": "#0d9488", "gaining": "#2563eb"},
        "anim": {"speed": 3.0, "style": "comet", "color": "#ff2bd6"},
        "duration_s": 1.0, "fps": 10, "width_px": 480,
    }
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Basemap fetch is a nice-to-have: force the offline branch in tests."""
    monkeypatch.setattr("hype_app.mesh.fetch_basemap_image",
                        lambda *a, **k: None)


def bounds_3857(hz_dir):
    """A view box that actually contains the synthetic paths."""
    from pyproj import Transformer

    tr = Transformer.from_crs(3857, 4326, always_xy=True)
    w, s = tr.transform(-11_000_100, -1_000_300)
    e, n = tr.transform(-10_999_800, -999_900)
    return {"west": w, "south": s, "east": e, "north": n}


def test_mp4_written_with_expected_frames(hz_dir, tmp_path):
    if not video.resolve_ffmpeg(lambda *_: None):
        pytest.skip("no ffmpeg available")
    p = payload(hz_dir, bounds4326=bounds_3857(hz_dir))
    res = video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None)
    assert res["format"] == "mp4"
    assert res["frames"] == 10
    assert res["width"] % 2 == 0 and res["height"] % 2 == 0
    from pathlib import Path

    f = Path(res["path"])
    assert f.exists() and f.stat().st_size > 5_000


def test_webp_fallback_when_no_encoder(hz_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(video, "resolve_ffmpeg", lambda log=print: None)
    p = payload(hz_dir, bounds4326=bounds_3857(hz_dir), fps=8, duration_s=0.5)
    res = video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None)
    assert res["format"] == "webp"
    assert res["encoder"] is None
    from pathlib import Path

    assert Path(res["path"]).suffix == ".webp"
    assert Path(res["path"]).exists()


def test_particles_move_between_frames(hz_dir):
    import geopandas as gpd

    gdf = gpd.read_file(hz_dir / "hz_paths_3d.gpkg", layer="hz_paths_3d")
    parts = video._prep_paths(gdf, visible_classes={"hyporheic", "gaining"},
                              class_colors={}, speed=3.0)
    assert len(parts) == 6
    p = parts[0]
    a = video._point_at(p, ((0.0 / p["dur"]) + p["phase"]) % 1.0)
    b = video._point_at(p, ((p["dur"] / 2.0 / p["dur"]) + p["phase"]) % 1.0)
    assert np.hypot(*(np.asarray(a) - np.asarray(b))) > 1.0


def test_hidden_class_not_prepared(hz_dir):
    import geopandas as gpd

    gdf = gpd.read_file(hz_dir / "hz_paths_3d.gpkg", layer="hz_paths_3d")
    parts = video._prep_paths(gdf, visible_classes={"hyporheic"},
                              class_colors={}, speed=3.0)
    assert len(parts) == 3


def test_no_visible_paths_raises(hz_dir, tmp_path):
    p = payload(hz_dir, bounds4326=bounds_3857(hz_dir), visible_classes=[])
    with pytest.raises(ValueError):
        video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None)


def test_duration_floor_and_median_rule(hz_dir):
    import geopandas as gpd

    gdf = gpd.read_file(hz_dir / "hz_paths_3d.gpkg", layer="hz_paths_3d")
    parts = video._prep_paths(gdf, visible_classes={"hyporheic", "gaining"},
                              class_colors={}, speed=3.0)
    med_td = float(np.median([p["td"] for p in parts]))
    med = [p for p in parts if abs(p["td"] - med_td) < 1e-9]
    if med:  # the median path loops in 36/speed seconds
        assert med[0]["dur"] == pytest.approx(36000.0 / 3.0, rel=1e-6)
    assert all(p["dur"] >= video.MIN_LOOP_MS for p in parts)


# ---- round 2: tail artifact, stagger, cancel, scene ----

def test_tail_never_wraps_into_a_chord(hz_dir):
    """Head just past the loop restart must NOT sample the path end (the
    straight-line artifact): every tail segment stays short."""
    import geopandas as gpd

    gdf = gpd.read_file(hz_dir / "hz_paths_3d.gpkg", layer="hz_paths_3d")
    parts = video._prep_paths(gdf, visible_classes={"hyporheic", "gaining"},
                              class_colors={}, speed=3.0)
    p = parts[0]
    pts = video._tail_points(p, 0.02, 0.25)
    assert pts is not None
    seg = np.hypot(*np.diff(pts, axis=0).T)
    assert seg.max() < 0.2 * p["total"]
    # release pulse: no tail at all
    assert video._tail_points(p, 0.0, 0.25) is None


def test_comet_strokes_are_staggered(hz_dir):
    import geopandas as gpd

    gdf = gpd.read_file(hz_dir / "hz_paths_3d.gpkg", layer="hz_paths_3d")
    p = video._prep_paths(gdf, visible_classes={"hyporheic", "gaining"},
                          class_colors={}, speed=3.0)[0]
    fr, tail = 0.6, 0.24
    span = fr - max(fr - tail, 0.0)
    starts = []
    for k in range(3):
        pts = video._tail_points(p, fr, span * (1.0 - k / 3.0))
        starts.append(tuple(pts[0]))
    assert len(set(starts)) == 3


def test_cancel_mid_build_removes_partial(hz_dir, tmp_path):
    import threading

    ev = threading.Event()
    seen = []

    def prog(stage, i, n):
        seen.append(stage)
        if stage == "frames" and i >= 2:
            ev.set()

    p = payload(hz_dir, bounds4326=bounds_3857(hz_dir), duration_s=2.0, fps=10)
    res = video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None,
                                     progress=prog, cancel=ev)
    assert res.get("cancelled") is True
    from pathlib import Path

    assert not list(Path(tmp_path).glob("clip.*"))
    assert "frames" in seen


def test_scene_layers_render_and_hidden_opacity_leaves_no_trace(hz_dir, tmp_path,
                                                                monkeypatch):
    """A scene vector + raster + label land in the frame; an opacity-0 layer
    leaves no trace. Uses the webp branch so no encoder is needed."""
    import io as _io

    from PIL import Image

    monkeypatch.setattr(video, "resolve_ffmpeg", lambda log=print: None)
    b = bounds_3857(hz_dir)
    buf = _io.BytesIO()
    Image.new("RGBA", (8, 8), (255, 0, 0, 255)).save(buf, format="PNG")
    scene = {
        "items": [
            {"kind": "raster", "png": buf.getvalue(),
             "bounds": [[b["south"], b["west"]], [b["north"], b["east"]]],
             "opacity": 1.0},
            {"kind": "vector", "key": "Reach", "style": {"color": "#00ff00",
             "weight": 4, "opacity": 1.0}, "point_style": {},
             "data": {"type": "FeatureCollection", "features": [{
                 "type": "Feature", "properties": {},
                 "geometry": {"type": "LineString", "coordinates": [
                     [b["west"], b["south"]], [b["east"], b["north"]]]}}]}},
            {"kind": "vector", "key": "ghost", "style": {"color": "#0000ff",
             "weight": 8, "opacity": 0.0, "fillOpacity": 0.0}, "point_style": {},
             "data": {"type": "FeatureCollection", "features": [{
                 "type": "Feature", "properties": {},
                 "geometry": {"type": "LineString", "coordinates": [
                     [b["west"], b["north"]], [b["east"], b["south"]]]}}]}},
        ],
        "labels": [{"lat": (b["south"] + b["north"]) / 2,
                    "lon": (b["west"] + b["east"]) / 2,
                    "text": "Upstream", "color": "#ff7f0e"}],
    }
    p = payload(hz_dir, bounds4326=b, duration_s=0.3, fps=5, scene=scene,
                anim={"speed": 3.0, "style": "dots", "color": "#ff2bd6"})
    res = video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None)
    frame = np.asarray(Image.open(res["path"]).convert("RGB"))
    flat = frame.reshape(-1, 3)
    # red raster visible somewhere, green reach line visible, pure blue ghost absent
    assert ((flat[:, 0] > 180) & (flat[:, 1] < 120) & (flat[:, 2] < 120)).any()
    assert ((flat[:, 1] > 150) & (flat[:, 0] < 120) & (flat[:, 2] < 120)).any()
    assert not ((flat[:, 2] > 200) & (flat[:, 0] < 60) & (flat[:, 1] < 60)).any()


def test_width_follows_payload(hz_dir, tmp_path, monkeypatch):
    monkeypatch.setattr(video, "resolve_ffmpeg", lambda log=print: None)
    p = payload(hz_dir, bounds4326=bounds_3857(hz_dir), duration_s=0.3, fps=5,
                width_px=642)
    res = video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None)
    assert res["width"] == 642


def test_frame_time_stays_batched(tmp_path, monkeypatch):
    """Per-frame cost must stay in collection-batched territory.

    History: the first renderer drew ~5 artists per particle (~2,500
    draw_artist calls per frame at 500 particles), measured at 520 ms/frame
    on this synthetic scene and ~1 s/frame live with a full map. Batching
    into 3 tier LineCollections + 2 marker lines measured 67 ms/frame here.
    The bound is generous for slow CI boxes while still failing loudly on a
    regression to per-artist drawing.
    """
    import time

    import geopandas as gpd
    from shapely.geometry import LineString

    monkeypatch.setattr(video, "resolve_ffmpeg", lambda log=print: None)
    rng = np.random.default_rng(7)
    rows = []
    for i in range(500):
        x0 = -11_000_000 + rng.uniform(0, 200)
        y0 = -1_000_000 - rng.uniform(0, 300)
        xs = x0 + np.cumsum(rng.uniform(5, 25, 8))
        ys = y0 + np.cumsum(rng.uniform(-8, 8, 8))
        rows.append({"particleid": i + 1, "hz_class": "hyporheic",
                     "total_time_d": float(rng.uniform(2, 60)), "length_m": 100.0,
                     "geometry": LineString(list(zip(xs, ys)))})
    hz = tmp_path / "hz"
    hz.mkdir()
    gpd.GeoDataFrame(rows, crs=3857).to_file(hz / "hz_paths_3d.gpkg",
                                             layer="hz_paths_3d", driver="GPKG")
    ticks = []

    def prog(stage, i, n):
        if stage == "frames":
            ticks.append(time.perf_counter())

    p = payload(hz, bounds4326=bounds_3857(hz), visible_classes=["hyporheic"],
                duration_s=2.0, fps=10, width_px=960)
    video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None,
                               progress=prog)
    deltas = np.diff(ticks)
    mean_ms = 1000.0 * float(np.mean(deltas))
    print(f"mean frame time: {mean_ms:.1f} ms")
    assert mean_ms < 250.0


# ---- round 3: fill flag, stills ----

def test_leaflet_fill_false_is_honored(hz_dir, tmp_path, monkeypatch):
    """A polygon styled fill=False (the Domain) must leave NO wash in the frame.
    Ignoring the flag painted the model domain 20 percent yellow, video-only."""
    from PIL import Image

    monkeypatch.setattr(video, "resolve_ffmpeg", lambda log=print: None)
    b = bounds_3857(hz_dir)
    poly = {"type": "Feature", "properties": {}, "geometry": {
        "type": "Polygon", "coordinates": [[
            [b["west"], b["south"]], [b["east"], b["south"]],
            [b["east"], b["north"]], [b["west"], b["north"]],
            [b["west"], b["south"]]]]}}
    scene = {"items": [
        {"kind": "vector", "key": "Domain", "point_style": {},
         "style": {"color": "#caa700", "weight": 0, "opacity": 0.0, "fill": False},
         "data": {"type": "FeatureCollection", "features": [poly]}},
    ], "labels": []}
    p = payload(hz_dir, bounds4326=b, duration_s=0.3, fps=5, scene=scene,
                anim={"speed": 3.0, "style": "dots", "color": "#ff2bd6"},
                visible_classes=["hyporheic"])
    res = video.build_flowpath_video(p, tmp_path / "clip", log=lambda *_: None)

    def yellow_wash(path):
        frame = np.asarray(Image.open(path).convert("RGB")).astype(int).reshape(-1, 3)
        # a yellow tint pulls R and G above B relative to the neutral background
        return ((frame[:, 0] > 190) & (frame[:, 0] - frame[:, 2] > 25)
                & (frame[:, 1] - frame[:, 2] > 20))

    assert not yellow_wash(res["path"]).any()

    # sanity: WITHOUT the flag the default 0.2 fill does appear
    scene["items"][0]["style"] = {"color": "#caa700", "weight": 0, "opacity": 0.0}
    res2 = video.build_flowpath_video(p, tmp_path / "clip2", log=lambda *_: None)
    assert yellow_wash(res2["path"]).any()


def test_still_works_with_and_without_particles(hz_dir, tmp_path):
    from pathlib import Path

    from PIL import Image

    b = bounds_3857(hz_dir)
    # anim off, no visible classes, no scene: still must succeed
    p = payload(hz_dir, bounds4326=b, visible_classes=[],
                anim={"on": False, "speed": 3.0, "style": "comet", "color": "#ff2bd6"})
    res = video.build_flowpath_still(p, tmp_path / "still_a", log=lambda *_: None)
    assert res["format"] == "png" and Path(res["path"]).exists()

    # anim on with particles: magenta heads land in the frame
    p2 = payload(hz_dir, bounds4326=b,
                 anim={"on": True, "speed": 3.0, "style": "comet", "color": "#ff2bd6"})
    res2 = video.build_flowpath_still(p2, tmp_path / "still_b", log=lambda *_: None)
    frame = np.asarray(Image.open(res2["path"]).convert("RGB")).reshape(-1, 3)
    magenta = (frame[:, 0] > 200) & (frame[:, 2] > 150) & (frame[:, 1] < 120)
    assert magenta.any()


def test_still_scale_supersamples(hz_dir, tmp_path):
    from pathlib import Path

    from PIL import Image

    b = bounds_3857(hz_dir)
    p1 = payload(hz_dir, bounds4326=b, visible_classes=[],
                 anim={"on": False, "speed": 3.0, "style": "comet", "color": "#ff2bd6"})
    r1 = video.build_flowpath_still(p1, tmp_path / "s1", log=lambda *_: None)
    p2 = dict(p1, scale=2, kind="view")
    r2 = video.build_flowpath_still(p2, tmp_path / "s2", log=lambda *_: None)
    w1, h1 = Image.open(r1["path"]).size
    w2, h2 = Image.open(r2["path"]).size
    assert (w2, h2) == (2 * w1, 2 * h1)
    assert (r1["width"], r1["height"]) == (w1, h1)
    assert (r2["width"], r2["height"]) == (w2, h2)
    assert r1["kind"] is None and r2["kind"] == "view"
    assert Path(r2["path"]).exists()


# ---- round 5: 3D anim payload metadata, rect-bounds renders ----

def test_flowpaths_payload_carries_times_and_pids(hz_dir):
    from hype_app import hz_results, scene

    g = hz_results.class_paths_gdf(str(hz_dir))
    p = scene.flowpaths_payload(g, g.crs, (0.0, 0.0), 0.0, key="hz3d_paths_hyporheic")
    d = p["data"]
    assert len(d["times"]) == len(d["polylines"]) == len(d["pids"])
    assert all(t > 0 for t in d["times"])
    # aligned with the kept rows in order
    assert d["times"][0] == pytest.approx(float(g["total_time_d"].iloc[0]), rel=1e-5)
    assert d["pids"][0] == int(g["particleid"].iloc[0])


def test_still_rect_bounds_override(hz_dir, tmp_path):
    from PIL import Image

    b = bounds_3857(hz_dir)
    # a narrow horizontal slice of the view: the render must follow ITS aspect
    midlat = (b["south"] + b["north"]) / 2.0
    rect = {"west": b["west"], "east": b["east"],
            "south": midlat - (b["north"] - b["south"]) * 0.08,
            "north": midlat + (b["north"] - b["south"]) * 0.08}
    p = payload(hz_dir, bounds4326=rect, width_px=640, visible_classes=[],
                anim={"on": False, "speed": 3.0, "style": "comet", "color": "#ff2bd6"})
    res = video.build_flowpath_still(p, tmp_path / "rect", log=lambda *_: None)
    w, h = Image.open(res["path"]).size
    assert w == 640
    # slice is much wider than tall (never the full-view aspect); 180*scale floor allowed
    assert h < w * 0.6


# ------------------------------------------------------------------ 3D assembly

def _jpeg_bytes(color, size=(64, 48)):
    from io import BytesIO

    from PIL import Image

    buf = BytesIO()
    Image.new("RGB", size, color).save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def test_assemble_mjpeg_to_mp4(tmp_path):
    """Concatenated JPEGs ARE a valid MJPEG stream: one blob upload becomes a
    constant-rate MP4 with exactly one output frame per captured frame."""
    if not video.resolve_ffmpeg(lambda *_: None):
        pytest.skip("no ffmpeg")
    frames = [(_i * 25 % 255, 40, 200 - _i * 15 % 200) for _i in range(10)]
    src = tmp_path / "clip.mjpeg"
    src.write_bytes(b"".join(_jpeg_bytes(c) for c in frames))
    res = video.assemble_mjpeg_to_mp4(src, tmp_path / "clip", fps=10, frames=10,
                                      log=lambda *_: None)
    assert res["format"] == "mp4" and res["fps"] == 10 and res["frames"] == 10
    import imageio.v2 as imageio

    rd = imageio.get_reader(res["path"])
    meta = rd.get_meta_data()
    got = sum(1 for _ in rd)
    rd.close()
    assert got == 10
    assert abs(float(meta.get("fps", 0)) - 10.0) < 0.01


def test_assemble_mjpeg_no_encoder_returns_input(tmp_path, monkeypatch):
    monkeypatch.setattr(video, "resolve_ffmpeg", lambda log=print: None)
    src = tmp_path / "clip.mjpeg"
    src.write_bytes(_jpeg_bytes((10, 20, 30)))
    res = video.assemble_mjpeg_to_mp4(src, tmp_path / "clip", fps=30,
                                      log=lambda *_: None)
    assert res["format"] == "mjpeg" and res["path"] == str(src)


def test_transcode_cfr_snap_flag(tmp_path, monkeypatch):
    """The webm fallback gets a constant-rate resample when fps is known; the
    legacy no-fps call keeps the old command shape."""
    monkeypatch.setattr(video, "resolve_ffmpeg", lambda log=print: "ffmpeg")
    seen = {}

    class _Proc:
        returncode = 1
        stderr = "stub"

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        return _Proc()

    import subprocess

    monkeypatch.setattr(subprocess, "run", fake_run)
    video.transcode_webm_to_mp4(tmp_path / "a.webm", tmp_path / "a", fps=30,
                                log=lambda *_: None)
    assert "-vf" in seen["cmd"] and "fps=30" in seen["cmd"]
    video.transcode_webm_to_mp4(tmp_path / "a.webm", tmp_path / "a",
                                log=lambda *_: None)
    assert "-vf" not in seen["cmd"]
