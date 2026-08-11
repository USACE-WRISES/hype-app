"""Server-rendered flow-path animation video.

Renders the 2D map view (basemap + styled flow path lines + moving particles)
into an MP4, mirroring what www/path_anim.js draws in the browser. The client
cannot record its own map (tile images are cross-origin without a crossorigin
attribute, which taints any composite canvas), and a full matplotlib redraw is
~0.45 s per frame, so this module draws the scene once, caches the background
with Agg's copy_from_bbox, and per frame restores the region and redraws only
the particle artists (~7 ms per frame at 960x720).

Pure functions of a plain payload: no pyplot, no reactive reads, no globals,
so it runs on a worker thread with no need for the report's matplotlib lock.

Motion and rendering deliberately replicate www/path_anim.js:
  * position along a path is an ARC-LENGTH fraction (constant speed per path),
  * the median-residence-time path loops in 36/speed seconds, 800 ms floor,
  * per-particle phase is the golden-ratio hash of particleid,
  * comet mode is three stacked fading strokes plus a head dot, dots mode is a
    glow dot with a contrast core.
"""
from __future__ import annotations

import math
import os
import shutil
from pathlib import Path

GOLDEN = 0.6180339887
COMET_TAIL_SECONDS = 1.0     # tail spans the arc covered in the last second
COMET_TAIL_MAX_FRAC = 0.25   # capped at a quarter of the path
MEDIAN_LOOP_S = 36.0         # median path loops in 36/speed seconds (path_anim.js)
MIN_LOOP_MS = 800.0

# path_anim.js comet strokes: (alpha, width scale) innermost first, then the head.
COMET_STROKES = ((0.12, 1.0), (0.20, 1.4), (0.30, 1.9))


def resolve_ffmpeg(log=print) -> str | None:
    """The encoder, if any: imageio-ffmpeg's bundled binary, HYPE_FFMPEG, PATH."""
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).exists():
            return exe
    except Exception as e:  # noqa: BLE001
        log(f"[video] imageio-ffmpeg unavailable: {e}")
    env = os.environ.get("HYPE_FFMPEG")
    if env and Path(env).exists():
        return env
    return shutil.which("ffmpeg")


def _prep_paths(paths_gdf, *, visible_classes, class_colors, speed: float):
    """GDF -> per-particle dicts with projected coords, cumulative arc length,
    duration in ms and phase, exactly as path_anim.js scan() + retime() build them."""
    import numpy as np

    out = []
    tds = []
    for _, row in paths_gdf.iterrows():
        cls = row.get("hz_class")
        if cls not in visible_classes:
            continue
        td = float(row.get("total_time_d") or 0.0)
        geom = row.geometry
        if td <= 0 or geom is None or geom.geom_type != "LineString":
            continue
        xy = np.asarray(geom.coords, dtype=float)[:, :2]
        if len(xy) < 2:
            continue
        seg = np.hypot(*np.diff(xy, axis=0).T)
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        total = float(cum[-1])
        if total <= 0:
            continue
        pid = int(row.get("particleid") or 0)
        out.append({"xy": xy, "cum": cum, "total": total, "td": td,
                    "phase": (pid * GOLDEN) % 1.0,
                    "color": class_colors.get(cls, "#ff2bd6")})
        tds.append(td)
    if not out:
        return out
    med = float(np.median(tds)) or 1.0
    ms_per_day = (MEDIAN_LOOP_S * 1000.0 / max(speed, 0.1)) / med
    for p in out:
        p["dur"] = max(p["td"] * ms_per_day, MIN_LOOP_MS)
    return out


def _points_at(p, fracs):
    """(len(fracs), 2) positions at arc-length fractions of path `p`.

    One np.interp pair per call: identical piecewise-linear interpolation to
    the old per-sample segment walk, an order of magnitude fewer Python ops.
    """
    import numpy as np

    d = np.asarray(fracs, dtype=float) * p["total"]
    return np.column_stack([np.interp(d, p["cum"], p["xy"][:, 0]),
                            np.interp(d, p["cum"], p["xy"][:, 1])])


def _point_at(p, frac):
    """Interpolated (x, y) at arc-length fraction `frac` of path `p`."""
    return _points_at(p, [frac])[0]


def _tail_points(p, frac_head, tail_frac, n=12):
    """Polyline of the comet tail ending at the head.

    CLAMPED at the path start, exactly like the client (path_anim.js: each loop
    begins with a release pulse). Never wrap through 0: a modulo here sampled
    the path END while the head sat at the START, drawing a straight chord
    across the whole path once per loop (the reported flashing-line artifact).
    Returns None when the span has collapsed (head at the release point).
    """
    import numpy as np

    fr0 = max(frac_head - tail_frac, 0.0)
    if frac_head - fr0 <= 1e-6:
        return None
    return _points_at(p, np.linspace(fr0, frac_head, n))


def _warp_overlay(png_bytes, bounds):
    """An ImageOverlay PNG (north-up EPSG:4326 grid, cell-center bounds
    [[south, west], [north, east]]) -> (RGBA array with rows remapped to Web
    Mercator, extent (x0, x1, y0, y1) in 3857). x is linear in lon, so only the
    rows need resampling; done once at scene setup, zero per-frame cost."""
    import io

    import numpy as np
    from PIL import Image
    from pyproj import Transformer

    (s, w), (n, e) = bounds
    img = np.asarray(Image.open(io.BytesIO(png_bytes)).convert("RGBA"))
    h = img.shape[0]
    tr = Transformer.from_crs(4326, 3857, always_xy=True)
    x0, y_s = tr.transform(w, s)
    x1, y_n = tr.transform(e, n)
    if h > 1:
        # source rows are uniform in LATITUDE (row 0 = north); output rows are
        # uniform in mercator y. Sample source row index at each output row's lat.
        merc_y = np.linspace(y_n, y_s, h)
        lat = np.degrees(2.0 * np.arctan(np.exp(merc_y / 6378137.0)) - np.pi / 2.0)
        src_rows = (n - lat) / max(n - s, 1e-12) * (h - 1)
        idx = np.clip(np.round(src_rows).astype(int), 0, h - 1)
        img = img[idx]
    return img, (x0, x1, y_s, y_n)


def _feature_style(feat, layer_style):
    """Effective leaflet style: per-feature properties.style, with a layer-level
    style merged OVER it when present (ipyleaflet's documented merge order)."""
    st = dict((feat.get("properties") or {}).get("style") or {})
    st.update(layer_style or {})
    return st


def _draw_vector_layer(ax, to3857, lyr, zorder):
    """One GeoJSON layer -> matplotlib artists, honoring color/weight/opacity/
    fill and point_style radius the way Leaflet renders them."""
    import numpy as np
    from matplotlib.collections import LineCollection

    data = lyr.get("data") or {}
    feats = data.get("features") or []
    lstyle = lyr.get("style") or {}
    pstyle = lyr.get("point_style") or {}
    seg_batches: dict = {}     # (color, weight, opacity) -> [xy, ...]
    for feat in feats:
        geom = feat.get("geometry") or {}
        gt = geom.get("type")
        st = _feature_style(feat, lstyle)
        color = st.get("color", "#3388ff")
        weight = float(st.get("weight", 3.0))
        opacity = float(st.get("opacity", 1.0))
        if gt in ("LineString", "MultiLineString"):
            lines = [geom["coordinates"]] if gt == "LineString" else geom["coordinates"]
            for c in lines:
                xy = to3857(np.asarray(c, dtype=float)[:, :2])
                seg_batches.setdefault((color, weight, opacity), []).append(xy)
        elif gt in ("Polygon", "MultiPolygon"):
            polys = [geom["coordinates"]] if gt == "Polygon" else geom["coordinates"]
            for rings in polys:
                if not rings:
                    continue
                xy = to3857(np.asarray(rings[0], dtype=float)[:, :2])
                # Leaflet's fill flag wins over its 0.2 default: DOMAIN_STYLE and
                # friends set fill=False, and ignoring it painted the whole model
                # domain with a 20 percent wash of the stroke color (the reported
                # yellow fill, video-only).
                fill_op = 0.0 if st.get("fill") is False \
                    else float(st.get("fillOpacity", 0.2))
                if fill_op > 0:
                    ax.fill(xy[:, 0], xy[:, 1],
                            color=st.get("fillColor", color), alpha=fill_op,
                            zorder=zorder, linewidth=0)
                if opacity > 0 and weight > 0:
                    ax.plot(np.append(xy[:, 0], xy[0, 0]), np.append(xy[:, 1], xy[0, 1]),
                            color=color, linewidth=weight * 0.72, alpha=opacity,
                            zorder=zorder)
        elif gt in ("Point", "MultiPoint"):
            pts = [geom["coordinates"]] if gt == "Point" else geom["coordinates"]
            eff = dict(pstyle)
            eff.update({k: v for k, v in st.items() if k not in ("weight",)})
            radius = float(eff.get("radius", 5.0))
            for c in pts:
                xy = to3857(np.asarray([c[:2]], dtype=float))
                ax.plot(xy[:, 0], xy[:, 1], marker="o", linestyle="none",
                        markersize=max(radius * 1.44, 1.0),
                        markerfacecolor=eff.get("fillColor", "#3388ff"),
                        markeredgecolor=eff.get("color", "#000000"),
                        markeredgewidth=float(eff.get("weight", 1.0)) * 0.5,
                        alpha=float(eff.get("fillOpacity", eff.get("opacity", 0.9))),
                        zorder=zorder)
    for (c, w, o), segs in seg_batches.items():
        ax.add_collection(LineCollection(segs, colors=c, linewidths=w * 0.72,
                                         alpha=o, zorder=zorder))


def _draw_scene(ax, to3857, scene):
    """The frozen map scene (already ordered by the gather: pane buckets, then
    fills, lines, paths, points) under a rising zorder. Labels ride on top."""
    import matplotlib.patheffects as pe

    z = 1.0
    for item in scene.get("items") or []:
        z += 0.01
        kind = item.get("kind")
        if kind == "raster":
            try:
                img, extent = _warp_overlay(item["png"], item["bounds"])
                ax.imshow(img, extent=extent, origin="upper",
                          interpolation="bilinear",
                          alpha=float(item.get("opacity", 1.0)), zorder=z)
            except Exception:  # noqa: BLE001 — one bad overlay must not kill the build
                continue
        elif kind == "vector":
            _draw_vector_layer(ax, to3857, item, z)
    for lab in scene.get("labels") or []:
        z += 0.01
        try:
            import numpy as np

            xy = to3857(np.asarray([[lab["lon"], lab["lat"]]], dtype=float))
            ax.annotate(lab.get("text") or "", (xy[0, 0], xy[0, 1]),
                        fontsize=7.5, color=lab.get("color") or "#1f3864",
                        fontweight="bold", ha="center", va="center", zorder=z,
                        path_effects=[pe.withStroke(linewidth=2.2,
                                                    foreground="white")])
        except Exception:  # noqa: BLE001
            continue


def build_flowpath_video(payload: dict, out_path, log=print, progress=None,
                         cancel=None) -> dict:
    """Render and encode the animation.

    payload:
      hz_dir           str, the hyporheic results dir (summary/hz)
      bounds4326       {west, south, east, north} of the current map view
      basemap          "imagery" | "topo"
      visible_classes  list of class names to draw
      line             {show, weight, opacity, mode, color} composed line style
      class_colors     {cls: "#hex"} the identity palette
      anim             {speed, style ("comet"|"dots"), color "#hex"}
      duration_s, fps  clip length and rate
      width_px         target frame width (height follows the view aspect)

    payload may also carry `scene` ({items, labels}, the frozen visible-map
    snapshot) which replaces the legacy line drawing, and callers may pass
    `progress(stage, i, n)` plus a threading.Event `cancel` checked between
    stages and every frame (cancel -> partial file removed, {"cancelled": True}).

    Returns {"path", "format", "frames", "fps", "width", "height", "encoder"}.
    """
    import geopandas as gpd
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.collections import LineCollection
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from pyproj import Transformer

    from . import hz_results
    from .mesh import fetch_basemap_image

    def _tick(stage, i, n):
        if progress:
            try:
                progress(stage, i, n)
            except Exception:  # noqa: BLE001 — progress must never kill the build
                pass

    def _cancelled():
        return cancel is not None and cancel.is_set()

    b = payload["bounds4326"]
    tr = Transformer.from_crs(4326, 3857, always_xy=True)
    x0, y0 = tr.transform(b["west"], b["south"])
    x1, y1 = tr.transform(b["east"], b["north"])
    if x1 <= x0 or y1 <= y0:
        raise ValueError("empty map view")

    fps = int(payload.get("fps") or 30)
    duration_s = float(payload.get("duration_s") or 8.0)
    n_frames = max(int(round(duration_s * fps)), 1)

    width_px = int(payload.get("width_px") or 1280)
    width_px = max(320, min(width_px, 1920))
    aspect = (y1 - y0) / (x1 - x0)
    height_px = int(round(width_px * aspect))
    width_px -= width_px % 2       # yuv420p requires even dimensions
    height_px -= height_px % 2
    # Degenerate-height guard only: a rubber-band rect can legitimately be very
    # short, and a taller floor would stretch its aspect.
    height_px = max(height_px, 64)

    _tick("basemap", 0, 1)
    basemap_choice = payload.get("basemap")
    fetched = None
    if basemap_choice in ("imagery", "topo"):
        service = "USGSImageryOnly" if basemap_choice == "imagery" else "USGSTopo"
        fmt = "jpg" if service == "USGSImageryOnly" else "png"
        fetched = fetch_basemap_image(3857, x0, y0, x1, y1, service=service, fmt=fmt,
                                      max_px=max(width_px, height_px), timeout_s=30.0,
                                      log=log)
    if _cancelled():
        return {"cancelled": True}

    paths_gdf = hz_results.class_paths_gdf(payload["hz_dir"])
    if paths_gdf is None or not len(paths_gdf):
        raise ValueError("no flow paths to animate")
    paths_gdf = paths_gdf.to_crs(3857)

    line = payload.get("line") or {}
    anim = payload.get("anim") or {}
    class_colors = dict(payload.get("class_colors") or {})
    visible = set(payload.get("visible_classes") or [])
    particles = _prep_paths(paths_gdf, visible_classes=visible,
                            class_colors=class_colors,
                            speed=float(anim.get("speed") or 3.0))
    if not particles:
        raise ValueError("no visible flow paths in the selected classes")

    dpi = 100.0
    fig = Figure(figsize=(width_px / dpi, height_px / dpi), dpi=dpi)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_axis_off()
    if fetched:
        from .figures import _decode_img

        dec = _decode_img(fetched)
        if dec:
            img, extent = dec
            ax.imshow(img, extent=extent, origin="upper",
                      interpolation="bilinear", zorder=0)
    else:
        ax.set_facecolor("#eef1f5")

    _tick("scene", 0, 1)
    scene = payload.get("scene")
    if scene:
        tr4326 = Transformer.from_crs(4326, 3857, always_xy=True)

        def to3857(arr):
            xs, ys = tr4326.transform(arr[:, 0], arr[:, 1])
            return np.column_stack([xs, ys])

        _draw_scene(ax, to3857, scene)
    elif line.get("show", True):
        # legacy fallback (no scene snapshot supplied): flow path lines only
        lw = float(line.get("weight") or 2.0)
        lop = float(line.get("opacity") or 0.9)
        mode = line.get("mode") or "class"
        for cls in visible:
            sub = paths_gdf[paths_gdf["hz_class"] == cls]
            if not len(sub):
                continue
            segs = [np.asarray(g.coords)[:, :2] for g in sub.geometry
                    if g is not None and g.geom_type == "LineString"]
            color = (line.get("color") or "#0d9488") if mode == "single" \
                else class_colors.get(cls, "#0d9488")
            ax.add_collection(LineCollection(segs, colors=color, linewidths=lw,
                                             alpha=lop, zorder=2))
    ax.text(0.995, 0.005, "Basemap: USGS The National Map", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=6, color="#333",
            bbox={"facecolor": "white", "alpha": 0.6, "pad": 1, "edgecolor": "none"})

    canvas.draw()
    background = canvas.copy_from_bbox(fig.bbox)

    style = anim.get("style") or "comet"
    pcolor = anim.get("color") or "#ff2bd6"
    core = "#000000" if pcolor.lower() == "#ffffff" else "#ffffff"

    # Batched dynamic artists: a handful of collection/marker draws per frame
    # instead of ~5 draw_artist calls PER PARTICLE (2,500 at 500 particles,
    # ~1 s/frame measured live; the per-artist overhead dominated, not the ink).
    from matplotlib.collections import LineCollection as _LC

    if style == "comet":
        tier_cols = [
            _LC([], colors=pcolor, alpha=a, linewidths=w * 1.4,
                capstyle="round", zorder=5 + 0.1 * k)
            for k, (a, w) in enumerate(COMET_STROKES)]
        for c in tier_cols:
            ax.add_collection(c)
        heads_art = Line2D([], [], marker="o", markersize=3.4, color=pcolor,
                           markeredgecolor="none", linestyle="none", zorder=6)
        cores_art = Line2D([], [], marker="o", markersize=1.4, color=core,
                           markeredgecolor="none", linestyle="none", zorder=7)
        ax.add_line(heads_art)
        ax.add_line(cores_art)
        dyn = (*tier_cols, heads_art, cores_art)
    else:
        glow_art = Line2D([], [], marker="o", markersize=6.0, color=pcolor,
                          alpha=0.35, markeredgecolor="none", linestyle="none",
                          zorder=5)
        dot_art = Line2D([], [], marker="o", markersize=3.2, color=pcolor,
                         markeredgecolor=core, markeredgewidth=0.5,
                         linestyle="none", zorder=6)
        ax.add_line(glow_art)
        ax.add_line(dot_art)
        dyn = (glow_art, dot_art)

    def render_frame(t_ms: float):
        canvas.restore_region(background)
        heads = np.empty((len(particles), 2))
        tiers = ([], [], []) if style == "comet" else None
        for j, p in enumerate(particles):
            fr = ((t_ms / p["dur"]) + p["phase"]) % 1.0
            heads[j] = _points_at(p, [fr])[0]
            if style == "comet":
                tail_frac = min((COMET_TAIL_SECONDS * 1000.0) / p["dur"],
                                COMET_TAIL_MAX_FRAC)
                span = fr - max(fr - tail_frac, 0.0)
                if span <= 1e-6:
                    continue        # release pulse: head only this frame
                for k in range(3):  # staggered thirds, the client's taper
                    pts = _tail_points(p, fr, span * (1.0 - k / 3.0))
                    if pts is not None:
                        tiers[k].append(pts)
        if style == "comet":
            for k in range(3):
                tier_cols[k].set_segments(tiers[k])
            heads_art.set_data(heads[:, 0], heads[:, 1])
            cores_art.set_data(heads[:, 0], heads[:, 1])
        else:
            glow_art.set_data(heads[:, 0], heads[:, 1])
            dot_art.set_data(heads[:, 0], heads[:, 1])
        for a in dyn:
            ax.draw_artist(a)
        canvas.blit(fig.bbox)
        buf = np.asarray(canvas.buffer_rgba())
        return buf[:, :, :3].copy()

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg = resolve_ffmpeg(log)
    result = {"frames": n_frames, "fps": fps, "width": width_px, "height": height_px}

    if ffmpeg:
        import imageio

        os.environ.setdefault("IMAGEIO_FFMPEG_EXE", ffmpeg)
        target = out_path.with_suffix(".mp4")
        was_cancelled = False
        # Cancel BREAKS rather than raises: an exception through the writer's
        # __exit__ leaves imageio's ffmpeg child wedged on Windows (stdin never
        # closes cleanly), which pins the partial file forever. A clean break
        # lets close() finalize, ffmpeg exits, and the unlink is ordinary.
        import time as _time

        _t_frames = _time.perf_counter()
        with imageio.get_writer(str(target), fps=fps, codec="libx264",
                                pixelformat="yuv420p", quality=8,
                                macro_block_size=1) as w:
            for i in range(n_frames):
                if _cancelled():
                    was_cancelled = True
                    break
                w.append_data(render_frame(i * 1000.0 / fps))
                _tick("frames", i + 1, n_frames)
                if i % (fps * 2) == 0:
                    ms = 1000.0 * (_time.perf_counter() - _t_frames) / (i + 1)
                    log(f"[video] frame {i + 1}/{n_frames} ({ms:.0f} ms/frame)")
        if was_cancelled:
            import time as _time

            for _ in range(30):
                try:
                    target.unlink(missing_ok=True)
                except PermissionError:
                    pass
                if not target.exists():
                    break
                _time.sleep(0.1)
            log("[video] build canceled, partial file removed")
            return {"cancelled": True}
        result |= {"path": str(target), "format": "mp4", "encoder": ffmpeg}
    else:
        from PIL import Image

        target = out_path.with_suffix(".webp")
        frames = []
        for i in range(n_frames):
            if _cancelled():
                log("[video] build canceled")
                return {"cancelled": True}
            frames.append(Image.fromarray(render_frame(i * 1000.0 / fps)))
            _tick("frames", i + 1, n_frames)
        frames[0].save(target, save_all=True, append_images=frames[1:],
                       duration=int(round(1000.0 / fps)), loop=0, quality=80)
        log("[video] no MP4 encoder found, wrote animated WebP instead")
        result |= {"path": str(target), "format": "webp", "encoder": None}
    log(f"[video] wrote {result['path']} ({n_frames} frames at {fps} fps)")
    return result


def build_flowpath_still(payload: dict, out_path, log=print) -> dict:
    """One PNG of the current 2-D map view: basemap + the frozen scene snapshot,
    plus the particles at their t=0 positions when the payload's anim is on.

    The browser fallback for Save image (tile CORS taints any client composite),
    so unlike the video it must work on ANY project state: no flow paths, no
    visible classes, no scene are all fine, they just draw nothing.
    """
    import numpy as np
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from pyproj import Transformer

    from . import hz_results
    from .mesh import fetch_basemap_image

    b = payload["bounds4326"]
    tr = Transformer.from_crs(4326, 3857, always_xy=True)
    x0, y0 = tr.transform(b["west"], b["south"])
    x1, y1 = tr.transform(b["east"], b["north"])
    if x1 <= x0 or y1 <= y0:
        raise ValueError("empty map view")

    width_px = max(320, min(int(payload.get("width_px") or 1280), 1920))
    # 64 floor is a degenerate-height guard only: a rubber-band rect can be very
    # short, and a taller floor would stretch its aspect.
    height_px = max(64, int(round(width_px * (y1 - y0) / (x1 - x0))))
    # Supersample: same figure geometry at k times the dpi, so every artist
    # (lines, labels, markers) scales together and the PNG is k times as wide.
    scale = max(1, min(int(payload.get("scale") or 1), 3))

    fetched = None
    if payload.get("basemap") in ("imagery", "topo"):
        service = ("USGSImageryOnly" if payload["basemap"] == "imagery" else "USGSTopo")
        fmt = "jpg" if service == "USGSImageryOnly" else "png"
        fetched = fetch_basemap_image(
            3857, x0, y0, x1, y1, service=service, fmt=fmt,
            max_px=max(width_px, height_px) * scale, timeout_s=30.0, log=log)
        if fetched is None and scale > 1:
            # The service may refuse oversize exports: retry unscaled and let
            # imshow's bilinear upscale absorb the difference.
            fetched = fetch_basemap_image(
                3857, x0, y0, x1, y1, service=service, fmt=fmt,
                max_px=max(width_px, height_px), timeout_s=30.0, log=log)

    dpi = 100.0 * scale
    fig = Figure(figsize=(width_px / 100.0, height_px / 100.0), dpi=dpi)
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_axes([0, 0, 1, 1])
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_axis_off()
    if fetched:
        from .figures import _decode_img

        dec = _decode_img(fetched)
        if dec:
            img, extent = dec
            ax.imshow(img, extent=extent, origin="upper",
                      interpolation="bilinear", zorder=0)
    else:
        ax.set_facecolor("#eef1f5")

    tr4326 = Transformer.from_crs(4326, 3857, always_xy=True)

    def to3857(arr):
        xs, ys = tr4326.transform(arr[:, 0], arr[:, 1])
        return np.column_stack([xs, ys])

    scene = payload.get("scene")
    if scene:
        _draw_scene(ax, to3857, scene)

    anim = payload.get("anim") or {}
    if anim.get("on"):
        try:
            paths_gdf = hz_results.class_paths_gdf(payload["hz_dir"]).to_crs(3857)
            particles = _prep_paths(
                paths_gdf, visible_classes=set(payload.get("visible_classes") or []),
                class_colors=dict(payload.get("class_colors") or {}),
                speed=float(anim.get("speed") or 3.0))
        except Exception:  # noqa: BLE001 — a still never fails for lack of paths
            particles = []
        if particles:
            pcolor = anim.get("color") or "#ff2bd6"
            core = "#000000" if pcolor.lower() == "#ffffff" else "#ffffff"
            style = anim.get("style") or "comet"
            heads = []
            for p in particles:
                fr = p["phase"]          # t=0 of the loop, the client's frame zero
                heads.append(_points_at(p, [fr])[0])
                if style == "comet":
                    tail_frac = min((COMET_TAIL_SECONDS * 1000.0) / p["dur"],
                                    COMET_TAIL_MAX_FRAC)
                    span = fr - max(fr - tail_frac, 0.0)
                    for k, (a, w) in enumerate(COMET_STROKES):
                        pts = _tail_points(p, fr, span * (1.0 - k / 3.0)) \
                            if span > 1e-6 else None
                        if pts is not None:
                            ax.plot(pts[:, 0], pts[:, 1], color=pcolor, alpha=a,
                                    linewidth=w * 1.4, solid_capstyle="round",
                                    zorder=5)
            hx = [h[0] for h in heads]
            hy = [h[1] for h in heads]
            if style != "comet":
                ax.plot(hx, hy, marker="o", markersize=6.0, color=pcolor, alpha=0.35,
                        markeredgecolor="none", linestyle="none", zorder=5)
            ax.plot(hx, hy, marker="o", markersize=3.4, color=pcolor,
                    markeredgecolor="none", linestyle="none", zorder=6)
            ax.plot(hx, hy, marker="o", markersize=1.4, color=core,
                    markeredgecolor="none", linestyle="none", zorder=7)

    ax.text(0.995, 0.005, "Basemap: USGS The National Map", transform=ax.transAxes,
            ha="right", va="bottom", fontsize=6, color="#333",
            bbox={"facecolor": "white", "alpha": 0.6, "pad": 1, "edgecolor": "none"})
    out_path = Path(out_path).with_suffix(".png")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(str(out_path), dpi=dpi)
    log(f"[still] wrote {out_path} ({width_px * scale}x{height_px * scale})")
    return {"path": str(out_path), "format": "png", "kind": payload.get("kind"),
            "width": width_px * scale, "height": height_px * scale}


def transcode_webm_to_mp4(webm_path, out_path, log=print, fps: int | None = None) -> dict:
    """3D recording fallback: browser-recorded webm -> MP4 with the same encoder.

    MediaRecorder writes variable wall-clock timestamps; without a constant-rate
    snap those uneven timestamps ride into the MP4 and play back jumpy. fps, when
    given, resamples onto a constant grid (the fps filter works on every ffmpeg
    this app can meet, unlike the newer -fps_mode flag).
    """
    import subprocess

    ffmpeg = resolve_ffmpeg(log)
    webm_path, out_path = Path(webm_path), Path(out_path)
    if not ffmpeg:
        return {"path": str(webm_path), "format": "webm", "encoder": None}
    target = out_path.with_suffix(".mp4")
    cmd = [ffmpeg, "-y", "-i", str(webm_path)]
    if fps:
        cmd += ["-vf", f"fps={int(fps)}"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-an", str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1000:
        log(f"[video] transcode failed ({proc.returncode}): {proc.stderr[-400:]}")
        return {"path": str(webm_path), "format": "webm", "encoder": None}
    return {"path": str(target), "format": "mp4", "encoder": ffmpeg,
            "fps": fps, "frames": None}


def assemble_mjpeg_to_mp4(mjpeg_path, out_path, fps: int, frames: int | None = None,
                          log=print) -> dict:
    """Deterministic 3D recording: concatenated JPEG frames -> constant-rate MP4.

    The client steps the particle animation on an ideal clock and captures one
    JPEG per frame; a byte-concatenation of JPEGs IS a valid MJPEG stream (the
    demuxer splits on the SOI/EOI markers), so one upload carries the whole clip
    and -framerate assigns each frame its exact timestamp. Same failure contract
    as transcode_webm_to_mp4: on any failure the input path comes back untouched.
    """
    import subprocess

    ffmpeg = resolve_ffmpeg(log)
    mjpeg_path, out_path = Path(mjpeg_path), Path(out_path)
    if not ffmpeg:
        return {"path": str(mjpeg_path), "format": "mjpeg", "encoder": None}
    target = out_path.with_suffix(".mp4")
    cmd = [ffmpeg, "-y", "-f", "mjpeg", "-framerate", str(int(fps)),
           "-i", str(mjpeg_path), "-c:v", "libx264", "-crf", "18",
           "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
           "-r", str(int(fps)), str(target)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0 or not target.exists() or target.stat().st_size < 1000:
        log(f"[video] mjpeg assembly failed ({proc.returncode}): {proc.stderr[-400:]}")
        return {"path": str(mjpeg_path), "format": "mjpeg", "encoder": None}
    return {"path": str(target), "format": "mp4", "encoder": ffmpeg,
            "fps": int(fps), "frames": frames}
