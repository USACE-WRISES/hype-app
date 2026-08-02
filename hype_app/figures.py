"""Best-effort report figures (report §10, §17.4).

Every function returns PNG bytes or None on any failure, uses the headless Agg backend, and imports
matplotlib lazily so importing this module stays cheap. Spatial figures take already-loaded GeoJSON
dicts / GeoDataFrames, or explicit raster/workspace paths handed over by the caller — the module
never touches app state itself.
"""
from __future__ import annotations

import io


def _agg_pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _png(fig, dpi: int = 130) -> bytes:
    """PNG bytes, cropped to the ink. Every figure here is a plot with slack around it."""
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=dpi, bbox_inches="tight")
    return buf.getvalue()


def _autocrop(png: bytes, pad: int = 10) -> bytes:
    """Trim uniform white margins from a rendered PNG. mplot3d wastes canvas around the
    projected axes box and bbox_inches='tight' cannot see inside the 3-D panel."""
    try:
        import numpy as np
        from PIL import Image

        im = Image.open(io.BytesIO(png)).convert("RGB")
        arr = np.asarray(im)
        nonwhite = (arr < 250).any(axis=2)
        rows = np.nonzero(nonwhite.any(axis=1))[0]
        cols = np.nonzero(nonwhite.any(axis=0))[0]
        if rows.size == 0 or cols.size == 0:
            return png
        r0 = max(0, int(rows.min()) - pad)
        r1 = min(arr.shape[0], int(rows.max()) + 1 + pad)
        c0 = max(0, int(cols.min()) - pad)
        c1 = min(arr.shape[1], int(cols.max()) + 1 + pad)
        out = io.BytesIO()
        im.crop((c0, r0, c1, r1)).save(out, format="PNG")
        return out.getvalue()
    except Exception:  # noqa: BLE001 — cropping is cosmetic
        return png


#: Navy and its supporting ink/rule tones, used across the report figures.
_NAVY = "#2f4b7c"
_INK = "#1f2a3a"
_RULE = "#c7d2e0"


def render_threshold_bar(thresholds) -> bytes | None:
    """Percent of gross hyporheic exchange exceeding each residence-time scenario (report §10.4)."""
    rows = [t for t in (thresholds or [])
            if getattr(t, "flow_exceedance_fraction", None) is not None]
    if not rows:
        return None
    try:
        plt = _agg_pyplot()
        labels = [f"{int(t.threshold_value_h)} hr" for t in rows]
        vals = [100.0 * t.flow_exceedance_fraction for t in rows]
        fig, ax = plt.subplots(figsize=(4.6, 2.6))
        ax.bar(labels, vals, color="#2f4b7c")
        ax.set_ylabel("Exchange over threshold (%)")
        ax.set_ylim(0, 100)
        ax.grid(True, axis="y", ls=":", lw=0.4, alpha=0.6)
        for i, v in enumerate(vals):
            ax.text(i, min(v + 2.0, 98), f"{v:.0f}", ha="center", fontsize=8)
        fig.tight_layout()
        out = _png(fig)
        plt.close(fig)
        return out
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


def render_opportunity_curve(nutrient) -> bytes | None:
    """Flux-weighted removal opportunity against the assumed reaction timescale (framework §13).

    The rate-free view of the screen: R(tau) = Σ wᵢ(1 - exp(-tᵢ/tau)) / Σ wᵢ, swept across decades.
    Marks the residence-time threshold used for the mass chain so the reader can see where this
    site's assumption sits on the curve. Log time axis, since the sweep spans four decades."""
    pts = [p for p in (getattr(nutrient, "opportunity_curve", None) or [])
           if getattr(p, "opportunity", None) is not None]
    if len(pts) < 2:
        return None
    try:
        plt = _agg_pyplot()
        xs = [p.tau_hours for p in pts]
        ys = [100.0 * p.opportunity for p in pts]
        # Sized and typed for the report, like the quadrant figure above it: at 7 inches the
        # displayed text is about 2.2 px per point, so 11pt labels land near 24px. The 4.6-inch
        # version with 7pt annotations was unreadable at report scale.
        fig, ax = plt.subplots(figsize=(7.0, 3.6))
        ax.plot(xs, ys, color=_NAVY, lw=2.4)
        ax.set_xscale("log")
        ax.set_xlabel("Assumed reaction timescale (hours)", fontsize=11)
        ax.set_ylabel("Exchange with sufficient time (%)", fontsize=11)
        ax.set_ylim(0, 100)
        ax.tick_params(labelsize=10)
        ax.grid(True, ls=":", lw=0.5, alpha=0.6)
        thr = getattr(nutrient, "threshold_hours", None)
        if thr:
            ax.axvline(float(thr), color="#b3541e", ls="--", lw=1.4)
            ax.text(float(thr), 96, f" {float(thr):g} hr threshold", color="#b3541e",
                    fontsize=9.5, va="top")
        fig.tight_layout()
        out = _png(fig)
        plt.close(fig)
        return out
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


def render_planview_figure(*, down_fc=None, up_fc=None, footprint_fc=None,
                           reach_lonlat=None, domain_lonlat=None) -> bytes | None:
    """Plan-view of hyporheic exchange (report §17.4): downwelling/upwelling stream cells, the
    active hyporheic footprint, the reach centerline, and the model domain. Inputs are GeoJSON
    FeatureCollections (dicts) in lon/lat plus coordinate lists."""
    try:
        plt = _agg_pyplot()
        from matplotlib.collections import PatchCollection
        from matplotlib.patches import Polygon as MplPoly

        fig, ax = plt.subplots(figsize=(5.2, 4.2))
        drew = False

        def _polys(fc, facecolor, alpha, label):
            nonlocal drew
            if not fc:
                return
            patches = []
            for feat in fc.get("features", []):
                geom = feat.get("geometry") or {}
                if geom.get("type") == "Polygon" and geom.get("coordinates"):
                    ring = geom["coordinates"][0]
                    patches.append(MplPoly([(c[0], c[1]) for c in ring], closed=True))
            if patches:
                ax.add_collection(PatchCollection(
                    patches, facecolor=facecolor, edgecolor=facecolor, alpha=alpha, linewidths=0.4))
                ax.plot([], [], color=facecolor, lw=6, alpha=max(alpha, 0.4), label=label)
                drew = True

        if domain_lonlat:
            xs = [p[0] for p in domain_lonlat]
            ys = [p[1] for p in domain_lonlat]
            ax.plot(xs + [xs[0]], ys + [ys[0]], color="#888", lw=0.8, ls="--",
                    label="Model domain")
            drew = True
        _polys(footprint_fc, "#0d9488", 0.25, "Active hyporheic footprint")
        _polys(down_fc, "#2563eb", 0.7, "Downwelling")
        _polys(up_fc, "#dc2626", 0.7, "Upwelling")
        if reach_lonlat:
            ax.plot([p[0] for p in reach_lonlat], [p[1] for p in reach_lonlat],
                    color="#111", lw=1.6, label="Reach")
            drew = True
        if not drew:
            plt.close(fig)
            return None
        ax.set_aspect("equal", adjustable="datalim")
        ax.set_xlabel("Longitude")
        ax.set_ylabel("Latitude")
        ax.legend(fontsize=7, loc="best", framealpha=0.9)
        ax.grid(True, ls=":", lw=0.3, alpha=0.5)
        fig.tight_layout()
        out = _png(fig)
        plt.close(fig)
        return out
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


def render_section_figure(paths_gdf, reach_line) -> bytes | None:
    """Longitudinal section of returning flow paths (report §17.4): distance along the reach vs
    elevation, each path colored by residence time. paths_gdf: LineString Z in a metric CRS with a
    'total_time_d' column; reach_line: a shapely LineString in the same CRS."""
    try:
        if paths_gdf is None or len(paths_gdf) == 0 or reach_line is None:
            return None
        import matplotlib as mpl
        import numpy as np
        from matplotlib.collections import LineCollection
        from shapely.geometry import Point

        plt = _agg_pyplot()
        segs, times = [], []
        for _, row in paths_gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            coords = list(geom.coords)
            if len(coords) < 2:
                continue
            sta = [reach_line.project(Point(c[0], c[1])) for c in coords]
            z = [c[2] if len(c) > 2 else 0.0 for c in coords]
            segs.append(np.column_stack([sta, z]))
            times.append(float(row.get("total_time_d", np.nan)))
        if not segs:
            return None
        times = np.asarray(times, float)
        norm = _logtime_norm(times)
        fig, ax = plt.subplots(figsize=(6.4, 3.0))
        lc = LineCollection(segs, cmap=mpl.cm.viridis, norm=norm, linewidths=0.8, alpha=0.85)
        lc.set_array(times)
        ax.add_collection(lc)
        ax.autoscale()
        ax.set_xlabel("Distance along reach (m)")
        ax.set_ylabel("Elevation (m)")
        _time_colorbar(fig, lc, ax)
        ax.grid(True, ls=":", lw=0.3, alpha=0.5)
        fig.tight_layout()
        out = _png(fig)
        plt.close(fig)
        return out
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


# --------------------------------------------------------------------------------------
# Site-map suite (report §10): shared cartographic helpers + the map/3-D producers.
# All 2-D maps share one bbox so the frames align; basemaps come pre-fetched from
# mesh.fetch_basemap_image via render_map_suite so the suite makes exactly two requests.
# --------------------------------------------------------------------------------------

_NAVY = "#2f4b7c"          # matches the report CSS --navy accent
_ATTRIB = "Basemap: USGS The National Map"


def _logtime_norm(times):
    """The shared log-scale color normalization for residence-time figures (section +
    plan-view paths), with guards for empty/degenerate/zero time arrays."""
    import matplotlib as mpl
    import numpy as np

    t = np.asarray(times, float)
    finite = t[np.isfinite(t) & (t > 0)]
    vmin = float(finite.min()) if finite.size else 1e-3
    vmax = float(finite.max()) if finite.size else 1.0
    return mpl.colors.LogNorm(vmin=max(1e-3, vmin), vmax=max(vmin * 10, vmax))


def _halo(lw: float = 2.2):
    """White stroke path effect so linework and labels stay legible over any basemap."""
    from matplotlib import patheffects
    return [patheffects.withStroke(linewidth=lw, foreground="white")]


def _time_colorbar(fig, mappable, ax):
    """Residence-time colorbar with plain-number ticks (no scientific notation) on the
    log scale; shared by the section figure and the flow-path plan view."""
    from matplotlib import ticker

    cb = fig.colorbar(mappable, ax=ax, fraction=0.046, pad=0.03)
    cb.set_label("Residence time (days)", fontsize=7.5)
    cb.ax.yaxis.set_major_locator(ticker.LogLocator(subs=(1.0, 2.0, 5.0)))
    cb.ax.yaxis.set_major_formatter(ticker.FuncFormatter(lambda v, _p: f"{v:g}"))
    cb.ax.yaxis.set_minor_formatter(ticker.NullFormatter())
    cb.ax.tick_params(labelsize=6.5)
    return cb


def _decode_img(fetched):
    """mesh.fetch_basemap_image result -> (RGB ndarray with row 0 = north, extent) or None."""
    if not fetched or not fetched.get("data"):
        return None
    try:
        import numpy as np
        from PIL import Image

        img = np.asarray(Image.open(io.BytesIO(fetched["data"])).convert("RGB"))
        x0, x1, y0, y1 = fetched["extent"]
        return img, (float(x0), float(x1), float(y0), float(y1))
    except Exception:  # noqa: BLE001 — basemaps are a nice-to-have
        return None


def _report_bbox(*, xy_lists=(), gdfs=(), margin_frac: float = 0.08):
    """One shared map bbox (x0, x1, y0, y1) in the model CRS: the union of the given
    coordinate lists and GeoDataFrame bounds, padded by `margin_frac` of the larger span."""
    xs, ys = [], []
    for pts in xy_lists:
        if pts:
            xs += [float(p[0]) for p in pts]
            ys += [float(p[1]) for p in pts]
    for g in gdfs:
        try:
            if g is not None and len(g):
                b = g.total_bounds
                xs += [float(b[0]), float(b[2])]
                ys += [float(b[1]), float(b[3])]
        except Exception:  # noqa: BLE001
            continue
    if not xs or not ys:
        return None
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    span = max(x1 - x0, y1 - y0)
    if span <= 0:                              # a single point: give it a 50 m stage
        span = 50.0
        x0, x1, y0, y1 = x0 - 25.0, x1 + 25.0, y0 - 25.0, y1 + 25.0
    m = margin_frac * span
    return (x0 - m, x1 + m, y0 - m, y1 + m)


def _map_axes(plt, *, bbox, basemap=None):
    """A clean cartographic frame: optional basemap, equal aspect, no tick clutter."""
    x0, x1, y0, y1 = bbox
    dx, dy = x1 - x0, y1 - y0
    h = min(5.6, max(3.4, 4.8 * (dy / dx if dx > 0 else 1.0)))
    fig, ax = plt.subplots(figsize=(4.8, h))
    if basemap is not None:
        img, extent = basemap
        ax.imshow(img, extent=extent, origin="upper", interpolation="bilinear", zorder=0)
    else:
        ax.set_facecolor("#eef1f5")
    ax.set_xlim(x0, x1)
    ax.set_ylim(y0, y1)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xticks([])
    ax.set_yticks([])
    for s in ax.spines.values():
        s.set_linewidth(0.6)
        s.set_color("#444")
    return fig, ax


def _vectors(ax, *, reach_xy=None, domain_xy=None) -> bool:
    """Model boundary + reach centerline on a map axes; returns True if anything drew."""
    drew = False
    if domain_xy:
        xs = [p[0] for p in domain_xy]
        ys = [p[1] for p in domain_xy]
        ax.plot(xs + [xs[0]], ys + [ys[0]], color="#888", lw=0.9, ls="--",
                label="Model boundary", path_effects=_halo(2.0), zorder=4)
        drew = True
    if reach_xy:
        ax.plot([p[0] for p in reach_xy], [p[1] for p in reach_xy], color="#111", lw=1.7,
                label="Reach centerline", path_effects=_halo(2.8), zorder=5)
        drew = True
    return drew


def _side_lines(ax, sides_xy, *, labels: bool = True) -> bool:
    """The four boundary-condition lines, colored and labeled like the app's 2-D and 3-D
    views (colors/names from mesh.BOUNDARY_STYLE). Returns True if anything drew."""
    if not sides_xy:
        return False
    from . import mesh as mesh_mod

    import numpy as np

    drew = False
    for key, (name, color) in mesh_mod.BOUNDARY_STYLE.items():
        pts = sides_xy.get(key)
        if not pts or len(pts) < 2:
            continue
        xs = np.asarray([float(p[0]) for p in pts])
        ys = np.asarray([float(p[1]) for p in pts])
        ax.plot(xs, ys, color=color, lw=2.0, solid_capstyle="round",
                path_effects=_halo(2.8), zorder=5)
        if labels:
            # true arc-length midpoint (the middle VERTEX of a 2-point line is an endpoint)
            cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])
            half = float(cum[-1]) / 2.0
            j = int(np.clip(np.searchsorted(cum, half), 1, len(xs) - 1))
            t = 0.0 if cum[j] == cum[j - 1] else (half - cum[j - 1]) / (cum[j] - cum[j - 1])
            lx = float(xs[j - 1] + t * (xs[j] - xs[j - 1]))
            ly = float(ys[j - 1] + t * (ys[j] - ys[j - 1]))
            ax.annotate(name, (lx, ly), fontsize=6, color=color,
                        ha="center", va="bottom", xytext=(0, 3),
                        textcoords="offset points", path_effects=_halo(1.8), zorder=7)
        drew = True
    return drew


def _scalebar(ax, bbox):
    """A two-segment scale bar (navy/white) anchored lower left, nice 1/2/5 length."""
    import numpy as np
    from matplotlib.patches import Rectangle

    x0, x1, y0, y1 = bbox
    span = x1 - x0
    if span <= 0:
        return
    target = span / 4.0
    k = 10.0 ** np.floor(np.log10(target))
    length = min((1 * k, 2 * k, 5 * k, 10 * k), key=lambda v: abs(v - target))
    label = f"{length / 1000:g} km" if length >= 1000 else f"{length:g} m"
    bx = x0 + 0.045 * span
    by = y0 + 0.045 * (y1 - y0)
    bh = 0.013 * (y1 - y0)
    ax.add_patch(Rectangle((bx, by), length / 2, bh, facecolor=_NAVY,
                           edgecolor=_NAVY, lw=0.5, zorder=6))
    ax.add_patch(Rectangle((bx + length / 2, by), length / 2, bh, facecolor="white",
                           edgecolor=_NAVY, lw=0.5, zorder=6))
    ax.text(bx + length / 2, by + bh * 1.7, label, ha="center", va="bottom", fontsize=6.5,
            color="#1f2d3d", path_effects=_halo(1.8), zorder=6)


def _north_arrow(ax):
    ax.annotate("N", xy=(0.965, 0.955), xytext=(0.965, 0.885),
                xycoords="axes fraction", textcoords="axes fraction",
                arrowprops=dict(arrowstyle="-|>", color=_NAVY, lw=1.4),
                ha="center", va="center", fontsize=9, fontweight="bold",
                color=_NAVY, path_effects=_halo(2.0), zorder=7)


def _attribution(ax, text: str = _ATTRIB):
    ax.text(0.99, 0.012, text, transform=ax.transAxes, ha="right", va="bottom",
            fontsize=5.5, color="#333", path_effects=_halo(1.6), zorder=7)


def _finish_map(fig, ax, plt, *, bbox, drew_vectors: bool, basemap) -> bytes:
    """Legend + furniture + PNG for a finished 2-D map figure."""
    if drew_vectors:
        ax.legend(fontsize=7, loc="upper left", framealpha=0.9)
    _scalebar(ax, bbox)
    _north_arrow(ax)
    if basemap is not None:
        _attribution(ax)
    out = _png(fig)
    plt.close(fig)
    return out


def _read_raster(path, *, crs_wkt=None, max_dim: int = 1200):
    """Band 1 of a GeoTIFF as (masked_array, extent) decimated to `max_dim`, warped to
    `crs_wkt` when the source CRS differs. Extent is (x0, x1, y0, y1) for
    imshow(origin='upper'); nodata/-9999/non-finite cells are masked."""
    if not path:
        return None
    import numpy as np
    import rasterio
    from rasterio import Affine
    from rasterio.enums import Resampling

    with rasterio.open(str(path)) as src0:
        vrt = None
        src = src0
        try:
            if crs_wkt and src0.crs is not None:
                from rasterio.crs import CRS as RioCRS
                target = RioCRS.from_user_input(crs_wkt)
                if src0.crs != target:
                    from rasterio.vrt import WarpedVRT
                    vrt = WarpedVRT(src0, crs=target)
                    src = vrt
            scale = max(src.width, src.height) / float(max_dim)
            if scale > 1.0:
                out_w = max(2, int(round(src.width / scale)))
                out_h = max(2, int(round(src.height / scale)))
            else:
                out_w, out_h = src.width, src.height
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)  # rasterio + numpy 2.5
                data = src.read(1, out_shape=(out_h, out_w),
                                resampling=Resampling.bilinear).astype(float)
            t = src.transform * Affine.scale(src.width / out_w, src.height / out_h)
            mask = ~np.isfinite(data) | (data <= -9000)
            if src.nodata is not None and np.isfinite(src.nodata):
                mask |= np.isclose(data, float(src.nodata))
            if mask.all():
                return None
            arr = np.ma.masked_array(data, mask)
            extent = (float(t.c), float(t.c + t.a * out_w),
                      float(t.f + t.e * out_h), float(t.f))
            return arr, extent
        finally:
            if vrt is not None:
                vrt.close()


def _raster_centers(arr, extent):
    """Cell-center coordinate vectors for a raster read by _read_raster, with the rows
    flipped so y ascends (what contour/surface plotting needs)."""
    import numpy as np

    h, w = arr.shape
    x0, x1, y0, y1 = extent                     # y1 = top edge of row 0
    xs = x0 + (np.arange(w) + 0.5) * (x1 - x0) / w
    ys = y1 + (np.arange(h) + 0.5) * (y0 - y1) / h
    z = arr.filled(np.nan) if np.ma.isMaskedArray(arr) else np.asarray(arr, float)
    if ys[0] > ys[-1]:
        ys = ys[::-1]
        z = z[::-1]
    return z, xs, ys


def render_vector_map(*, basemap=None, bbox=None, reach_xy=None, domain_xy=None,
                      sides_xy=None) -> bytes | None:
    """Site overview map: reach centerline + boundary lines over a USGS basemap (report §10).
    When the four boundary-condition lines are available they are drawn colored + labeled
    (app map parity) in place of the plain dashed domain outline."""
    try:
        if not bbox:
            return None
        plt = _agg_pyplot()
        fig, ax = _map_axes(plt, bbox=bbox, basemap=basemap)
        drew_sides = _side_lines(ax, sides_xy)
        drew = _vectors(ax, reach_xy=reach_xy,
                        domain_xy=(None if drew_sides else domain_xy))
        if not (drew or drew_sides) and basemap is None:
            plt.close(fig)
            return None
        return _finish_map(fig, ax, plt, bbox=bbox, drew_vectors=drew, basemap=basemap)
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


def render_wse_map(*, wse_tif=None, crs_wkt=None, basemap=None, bbox=None,
                   reach_xy=None, domain_xy=None) -> bytes | None:
    """Water surface elevation raster over the topo basemap, with a WSE colorbar (viridis,
    matching the in-app WSE/head ramps). Uses nearest-neighbor drawing so the wetted extent
    stays honest (valid pixels only)."""
    try:
        if not bbox:
            return None
        rr = _read_raster(wse_tif, crs_wkt=crs_wkt)
        if rr is None:
            return None
        arr, extent = rr
        import numpy as np

        vals = arr.compressed()
        if vals.size == 0:
            return None
        vmin = float(np.percentile(vals, 2.0))
        vmax = float(np.percentile(vals, 98.0))
        if not vmax > vmin:
            vmin, vmax = float(vals.min()), float(vals.min()) + 1e-6
        plt = _agg_pyplot()
        fig, ax = _map_axes(plt, bbox=bbox, basemap=basemap)
        im = ax.imshow(arr, extent=extent, origin="upper", cmap="viridis", alpha=0.85,
                       vmin=vmin, vmax=vmax, interpolation="nearest", zorder=2)
        drew = _vectors(ax, reach_xy=reach_xy, domain_xy=domain_xy)
        cb = fig.colorbar(im, ax=ax, fraction=0.046, pad=0.03)
        cb.set_label("Water surface elevation (m)", fontsize=7.5)
        cb.ax.tick_params(labelsize=6.5)
        return _finish_map(fig, ax, plt, bbox=bbox, drew_vectors=drew, basemap=basemap)
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


def render_head_map(*, head_tif=None, crs_wkt=None, basemap=None, bbox=None,
                    reach_xy=None, domain_xy=None) -> bytes | None:
    """Simulated hydraulic-head contours (top model layer) over the topo basemap, with
    inline level labels and a head colorbar. Levels/colormap mirror the in-app head layer."""
    try:
        if not bbox:
            return None
        rr = _read_raster(head_tif, crs_wkt=crs_wkt, max_dim=800)
        if rr is None:
            return None
        arr, extent = rr
        import numpy as np

        vals = arr.compressed()
        if vals.size < 4:
            return None
        lo, hi = float(vals.min()), float(vals.max())
        if not hi > lo:
            return None
        import matplotlib as mpl
        from matplotlib.ticker import MaxNLocator

        # round contour values (192.8, 192.9, ...) so the inline labels are exact
        levels = [float(v) for v in
                  MaxNLocator(nbins=8, steps=[1, 2, 2.5, 5, 10]).tick_values(lo, hi)
                  if lo < v < hi]
        if len(levels) < 2:
            levels = list(np.linspace(lo, hi, 9)[1:-1])
        step = levels[1] - levels[0]
        fmt = "%.2f" if step < 0.095 else ("%.1f" if step < 0.95 else "%.0f")
        z, xs, ys = _raster_centers(arr, extent)
        plt = _agg_pyplot()
        fig, ax = _map_axes(plt, bbox=bbox, basemap=basemap)
        cs = ax.contour(xs, ys, z, levels=levels, cmap="viridis",
                        vmin=lo, vmax=hi, linewidths=1.1, zorder=3)
        for txt in ax.clabel(cs, inline=True, fontsize=6, fmt=fmt):
            txt.set_path_effects(_halo(1.8))
        drew = _vectors(ax, reach_xy=reach_xy, domain_xy=domain_xy)
        sm = mpl.cm.ScalarMappable(norm=mpl.colors.Normalize(lo, hi), cmap="viridis")
        sm.set_array([])
        cb = fig.colorbar(sm, ax=ax, fraction=0.046, pad=0.03)
        cb.set_label("Hydraulic head (m)", fontsize=7.5)
        cb.set_ticks(levels)
        cb.set_ticklabels([fmt % v for v in levels])
        cb.ax.tick_params(labelsize=6.5)
        return _finish_map(fig, ax, plt, bbox=bbox, drew_vectors=drew, basemap=basemap)
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


def render_paths_map(*, paths_gdf=None, basemap=None, bbox=None,
                     reach_xy=None, domain_xy=None) -> bytes | None:
    """Plan view of the hyporheic flow paths colored by residence time, over the topo
    basemap. Shares the colormap/normalization with the longitudinal section figure."""
    try:
        if not bbox or paths_gdf is None or len(paths_gdf) == 0:
            return None
        import matplotlib as mpl
        import numpy as np
        from matplotlib.collections import LineCollection

        segs, times = [], []
        for _, row in paths_gdf.iterrows():
            geom = row.geometry
            if geom is None or geom.is_empty:
                continue
            coords = list(geom.coords)
            if len(coords) < 2:
                continue
            segs.append(np.asarray([(c[0], c[1]) for c in coords]))
            times.append(float(row.get("total_time_d", np.nan)))
        if not segs:
            return None
        times = np.asarray(times, float)
        plt = _agg_pyplot()
        fig, ax = _map_axes(plt, bbox=bbox, basemap=basemap)
        lc = LineCollection(segs, cmap=mpl.cm.viridis, norm=_logtime_norm(times),
                            linewidths=0.8, alpha=0.9, zorder=3)
        lc.set_array(times)
        ax.add_collection(lc)
        drew = _vectors(ax, reach_xy=reach_xy, domain_xy=domain_xy)
        _time_colorbar(fig, lc, ax)
        return _finish_map(fig, ax, plt, bbox=bbox, drew_vectors=drew, basemap=basemap)
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


def _grid_top(gwf_ws):
    """(top2d, active2d, x_centers, y_centers, bottom2d, interfaces) from a completed run's
    binary grid file. Engine rows are SOUTH-first (row 0 = southernmost), so y ascends with
    the row index directly (see mesh.build_grid_geometry_from_run). `bottom2d` is the bottom
    of the deepest ACTIVE layer per cell (NaN where never active); `interfaces` is a short
    list of layer-interface elevation arrays used to draw layer lines on the skirt walls."""
    import warnings
    from pathlib import Path

    import numpy as np
    from flopy.mf6.utils import MfGrdFile

    grb = next(Path(gwf_ws).glob("*.dis.grb"), None)
    if grb is None:
        return None
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        mg = MfGrdFile(str(grb), verbose=False).modelgrid
        top = np.asarray(mg.top, dtype=float)
        botm = np.asarray(mg.botm, dtype=float)
        idom = np.asarray(mg.idomain).reshape(botm.shape)
        delr = float(np.asarray(mg.delr, dtype=float).ravel()[0])
        x_anchor, y_anchor = float(mg.xoffset), float(mg.yoffset)
    nlay, nrow, ncol = botm.shape
    act3 = idom != 0
    active = act3.any(axis=0)
    kk = np.arange(nlay)[:, None, None]
    last = np.where(act3, kk, -1).max(axis=0)
    bottom = np.take_along_axis(botm, np.clip(last, 0, nlay - 1)[None], axis=0)[0]
    bottom = np.where(last >= 0, bottom, np.nan)
    step = max(1, int(np.ceil((nlay - 1) / 8.0))) if nlay > 1 else 1
    interfaces = [botm[k] for k in range(0, nlay - 1, step)]
    xc = x_anchor + (np.arange(ncol) + 0.5) * delr
    yc = y_anchor + (np.arange(nrow) + 0.5) * delr
    return top, active, xc, yc, bottom, interfaces


def _dem_top(dem_path, crs_wkt):
    """Terrain fallback for the 3-D view when no run grid exists: the DEM decimated in the
    model CRS as (z, active, x_centers, y_centers, None, None) with y ascending."""
    rr = _read_raster(dem_path, crs_wkt=crs_wkt, max_dim=160)
    if rr is None:
        return None
    import numpy as np

    arr, extent = rr
    z, xs, ys = _raster_centers(arr, extent)
    return z, np.isfinite(z), xs, ys, None, None


def _texture_for_grid(img, img_extent, grid_extent, out_shape):
    """Crop + resize a north-up RGB basemap image to per-quad facecolors for a SOUTH-first
    surface grid: returns an (nrows, ncols, 4) float array in 0..1."""
    import numpy as np
    from PIL import Image

    ix0, ix1, iy0, iy1 = img_extent
    gx0, gx1, gy0, gy1 = grid_extent
    h, w = img.shape[0], img.shape[1]
    if ix1 <= ix0 or iy1 <= iy0:
        return None
    c0 = int(np.clip(np.floor((gx0 - ix0) / (ix1 - ix0) * w), 0, w - 1))
    c1 = int(np.clip(np.ceil((gx1 - ix0) / (ix1 - ix0) * w), c0 + 1, w))
    r0 = int(np.clip(np.floor((iy1 - gy1) / (iy1 - iy0) * h), 0, h - 1))
    r1 = int(np.clip(np.ceil((iy1 - gy0) / (iy1 - iy0) * h), r0 + 1, h))
    rows, cols = out_shape
    crop = Image.fromarray(img[r0:r1, c0:c1])
    tex = np.asarray(crop.resize((max(1, cols), max(1, rows)), Image.BILINEAR), float) / 255.0
    tex = tex[::-1]                            # image rows are north-first; the grid is south-first
    if tex.ndim == 2:
        tex = np.stack([tex, tex, tex], axis=-1)
    alpha = np.ones(tex.shape[:2] + (1,), float)
    return np.concatenate([tex[..., :3], alpha], axis=2)


def _quads(X, Y, Z, keep):
    """Vectorized quad corner array (n, 4, 3) for Poly3DCollection from grid arrays; `keep`
    is the per-quad boolean mask (shape (nrow-1, ncol-1))."""
    import numpy as np

    r, c = np.nonzero(keep)
    if r.size == 0:
        return None, (r, c)
    quads = np.stack([
        np.stack([X[r, c], Y[r, c], Z[r, c]], axis=1),
        np.stack([X[r, c + 1], Y[r, c + 1], Z[r, c + 1]], axis=1),
        np.stack([X[r + 1, c + 1], Y[r + 1, c + 1], Z[r + 1, c + 1]], axis=1),
        np.stack([X[r + 1, c], Y[r + 1, c], Z[r + 1, c]], axis=1),
    ], axis=1)
    return quads, (r, c)


def render_iso3d(*, gwf_ws=None, dem_path=None, crs_wkt=None, wse_tif=None,
                 imagery=None, imagery_extent=None, sides_xy=None) -> bytes | None:
    """Static isometric 3-D view for the report: the model grid block (run DIS when
    available, DEM fallback) with the USGS aerial imagery draped on top, skirt walls with
    layer lines so the model reads as a volume, the boundary-condition lines draped and
    labeled (app 3-D parity), the water surface, and a relief-based vertical exaggeration
    of 3x or 5x. The PNG is auto-cropped to trim white margins."""
    try:
        import matplotlib as mpl
        import numpy as np

        top = active = xc = yc = bottom = None
        interfaces = None
        grid_edges = True
        if gwf_ws:
            try:
                g = _grid_top(gwf_ws)
                if g is not None:
                    top, active, xc, yc, bottom, interfaces = g
            except Exception:  # noqa: BLE001
                top = None
        if top is None and dem_path:
            d = _dem_top(dem_path, crs_wkt)
            if d is not None:
                top, active, xc, yc, bottom, interfaces = d
                grid_edges = False
        if top is None:
            return None
        active = active & np.isfinite(top)
        if not active.any():
            return None

        # crop to the active bounding box, then stride-decimate to <= 160 cells per side
        rows = np.nonzero(active.any(axis=1))[0]
        cols = np.nonzero(active.any(axis=0))[0]
        rsl = slice(rows.min(), rows.max() + 1)
        csl = slice(cols.min(), cols.max() + 1)
        top, active = top[rsl, csl], active[rsl, csl]
        xc, yc = xc[csl], yc[rsl]
        if bottom is not None:
            bottom = bottom[rsl, csl]
            interfaces = [w[rsl, csl] for w in (interfaces or [])]
        stride = max(1, int(np.ceil(max(top.shape) / 160)))
        top, active = top[::stride, ::stride], active[::stride, ::stride]
        xc, yc = xc[::stride], yc[::stride]
        if bottom is not None:
            bottom = bottom[::stride, ::stride]
            interfaces = [w[::stride, ::stride] for w in (interfaces or [])]
        if top.shape[0] < 2 or top.shape[1] < 2:
            return None

        # fill inactive cells from their nearest active neighbor (keeps the rim from
        # sagging when supersampled), then supersample so the imagery drape is finer
        # than the model cells; the model grid itself is drawn as explicit cell lines
        from scipy.ndimage import distance_transform_edt, zoom
        if (~active).any():
            idx = distance_transform_edt(~active, return_distances=False,
                                         return_indices=True)
            topf = top[tuple(idx)]
        else:
            topf = np.asarray(top, float)
        k = int(np.clip(np.ceil(150.0 / max(topf.shape)), 1, 4)) if grid_edges else 1
        if k > 1:
            topf = zoom(topf, k, order=1, mode="nearest", grid_mode=True)
            actf = zoom(active.astype(float), k, order=0, mode="nearest",
                        grid_mode=True) > 0.5
        else:
            actf = active
        nrow, ncol = topf.shape
        xf = np.linspace(float(xc[0]), float(xc[-1]), ncol)
        yf = np.linspace(float(yc[0]), float(yc[-1]), nrow)
        X, Y = np.meshgrid(xf, yf)

        keep = actf[:-1, :-1] & actf[1:, :-1] & actf[:-1, 1:] & actf[1:, 1:]
        quads, (qr, qc) = _quads(X, Y, topf, keep)
        if quads is None:
            return None

        # per-quad colors: draped imagery when available, terrain colormap otherwise
        tex = None
        if imagery is not None and imagery_extent is not None:
            tex = _texture_for_grid(imagery, imagery_extent,
                                    (float(xf[0]), float(xf[-1]), float(yf[0]), float(yf[-1])),
                                    (nrow - 1, ncol - 1))
        if tex is not None:
            fcolors = tex[qr, qc]
        else:
            normz = mpl.colors.Normalize(float(np.nanmin(topf)), float(np.nanmax(topf) + 1e-9))
            fcolors = mpl.colormaps["gist_earth"](normz(topf[qr, qc]))

        plt = _agg_pyplot()
        from mpl_toolkits.mplot3d.art3d import Line3DCollection, Poly3DCollection

        fig = plt.figure(figsize=(7.4, 4.4))
        ax = fig.add_subplot(projection="3d")
        ax.computed_zorder = False   # draw order: walls, terrain, lines, sides, water
        zlo, zhi = float(np.min(quads[..., 2])), float(np.max(quads[..., 2]))

        # skirt walls with layer lines around the ORIGINAL (coarse) active perimeter, from
        # the terrain top down to the deepest active layer bottom: the model block volume
        if bottom is not None and np.isfinite(bottom).any():
            nr0, nc0 = top.shape
            dx0 = float(xc[1] - xc[0]) if nc0 > 1 else 1.0
            dy0 = float(yc[1] - yc[0]) if nr0 > 1 else 1.0
            xe = np.concatenate(([xc[0] - dx0 / 2],
                                 (np.asarray(xc[:-1]) + np.asarray(xc[1:])) / 2,
                                 [xc[-1] + dx0 / 2]))
            ye = np.concatenate(([yc[0] - dy0 / 2],
                                 (np.asarray(yc[:-1]) + np.asarray(yc[1:])) / 2,
                                 [yc[-1] + dy0 / 2]))
            zfloor = float(np.nanmin(bottom))
            walls, wall_lines = [], []
            for r, c in zip(*np.nonzero(active)):
                zt = float(top[r, c])
                zb = float(bottom[r, c]) if np.isfinite(bottom[r, c]) else zfloor
                if not zb < zt:
                    continue
                for dr, dc, (xa, ya, xb, yb) in (
                        (-1, 0, (xe[c], ye[r], xe[c + 1], ye[r])),
                        (1, 0, (xe[c], ye[r + 1], xe[c + 1], ye[r + 1])),
                        (0, -1, (xe[c], ye[r], xe[c], ye[r + 1])),
                        (0, 1, (xe[c + 1], ye[r], xe[c + 1], ye[r + 1]))):
                    r2, c2 = r + dr, c + dc
                    if 0 <= r2 < nr0 and 0 <= c2 < nc0 and active[r2, c2]:
                        continue
                    walls.append([(xa, ya, zt), (xb, yb, zt),
                                  (xb, yb, zb), (xa, ya, zb)])
                    for w in interfaces or []:
                        zi = float(w[r, c])
                        if zb < zi < zt:
                            wall_lines.append([(xa, ya, zi), (xb, yb, zi)])
            if walls:
                ax.add_collection3d(Poly3DCollection(
                    walls, facecolors=(0.79, 0.75, 0.68, 0.95),
                    edgecolors=(0.35, 0.32, 0.28, 0.45), linewidths=0.2))
                if wall_lines:
                    ax.add_collection3d(Line3DCollection(
                        wall_lines, colors=(0.25, 0.22, 0.20, 0.45), linewidths=0.3))
                zlo = min(zlo, zfloor)

        ax.add_collection3d(Poly3DCollection(quads, facecolors=fcolors, linewidths=0.0))

        if grid_edges:                          # model cell lines riding on the surface
            lift = 0.012 * max(zhi - zlo, 1.0)

            def _runs(pts, ok):
                out, cur = [], []
                for p, o in zip(pts, ok):
                    if o:
                        cur.append(p)
                    else:
                        if len(cur) >= 2:
                            out.append(cur)
                        cur = []
                if len(cur) >= 2:
                    out.append(cur)
                return out

            segs = []
            for r in range(0, nrow, k):
                segs += _runs(np.column_stack([X[r], Y[r], topf[r] + lift]), actf[r])
            for c in range(0, ncol, k):
                segs += _runs(np.column_stack([X[:, c], Y[:, c], topf[:, c] + lift]),
                              actf[:, c])
            if segs:
                ax.add_collection3d(Line3DCollection(segs, colors=(0, 0, 0, 0.3),
                                                     linewidths=0.35))

        # boundary-condition lines draped on the terrain, colored + labeled (3-D app parity)
        if sides_xy:
            from . import mesh as mesh_mod

            lift3 = max(0.3, 0.02 * max(zhi - zlo, 1.0))
            gx0, gx1 = float(xf[0]), float(xf[-1])
            gy0, gy1 = float(yf[0]), float(yf[-1])
            spacing = max((gx1 - gx0) / max(ncol - 1, 1), 1e-6)

            def _z_at(px, py):
                ci = int(round((px - gx0) / max(gx1 - gx0, 1e-9) * (ncol - 1)))
                ri = int(round((py - gy0) / max(gy1 - gy0, 1e-9) * (nrow - 1)))
                if not (0 <= ci < ncol and 0 <= ri < nrow) or not actf[ri, ci]:
                    return None
                return float(topf[ri, ci])

            for skey, (sname, scolor) in mesh_mod.BOUNDARY_STYLE.items():
                pts = (sides_xy or {}).get(skey)
                if not pts or len(pts) < 2:
                    continue
                dens = []
                for (ax0, ay0), (ax1, ay1) in zip(
                        [(float(p[0]), float(p[1])) for p in pts[:-1]],
                        [(float(p[0]), float(p[1])) for p in pts[1:]]):
                    n = max(1, int(np.hypot(ax1 - ax0, ay1 - ay0) / spacing))
                    for t in np.linspace(0.0, 1.0, n, endpoint=False):
                        dens.append((ax0 + t * (ax1 - ax0), ay0 + t * (ay1 - ay0)))
                dens.append((float(pts[-1][0]), float(pts[-1][1])))
                xs3, ys3, zs3 = [], [], []
                for px, py in dens:
                    z = _z_at(px, py)
                    if z is None:
                        continue
                    xs3.append(px)
                    ys3.append(py)
                    zs3.append(z + lift3)
                if len(xs3) < 2:
                    continue
                ax.plot(xs3, ys3, zs3, color=scolor, lw=1.8, solid_capstyle="round")
                mid = len(xs3) // 2
                txt = ax.text(xs3[mid], ys3[mid], zs3[mid] + 3 * lift3, sname,
                              color=scolor, fontsize=6.5, ha="center", va="bottom")
                txt.set_path_effects(_halo(1.8))

        wrr = _read_raster(wse_tif, crs_wkt=crs_wkt, max_dim=160) if wse_tif else None
        if wrr is not None:
            warr, wext = wrr
            wz, wxs, wys = _raster_centers(warr, wext)
            wvalid = np.isfinite(wz)
            if wvalid.sum() >= 4 and wz.shape[0] >= 2 and wz.shape[1] >= 2:
                WX, WY = np.meshgrid(wxs, wys)
                wkeep = wvalid[:-1, :-1] & wvalid[1:, :-1] & wvalid[:-1, 1:] & wvalid[1:, 1:]
                wq, _ = _quads(WX, WY, np.where(wvalid, wz, 0.0) + 0.15, wkeep)
                if wq is not None:
                    # keep only water over the active terrain footprint: the WSE raster can
                    # extend past the model domain, which would leave floating blue slabs
                    cx = wq[:, :, 0].mean(axis=1)
                    cy = wq[:, :, 1].mean(axis=1)
                    ci = np.clip(np.round((cx - xf[0]) / max(xf[-1] - xf[0], 1e-9)
                                          * (ncol - 1)).astype(int), 0, ncol - 1)
                    ri = np.clip(np.round((cy - yf[0]) / max(yf[-1] - yf[0], 1e-9)
                                          * (nrow - 1)).astype(int), 0, nrow - 1)
                    inside = ((cx >= xf[0]) & (cx <= xf[-1]) & (cy >= yf[0]) & (cy <= yf[-1])
                              & actf[ri, ci])
                    wq = wq[inside]
                if wq is not None and len(wq):
                    ax.add_collection3d(Poly3DCollection(
                        wq, facecolors=(0.24, 0.58, 0.82, 0.62), linewidths=0.0))
                    zhi = max(zhi, float(np.max(wq[..., 2])))
                    zlo = min(zlo, float(np.min(wq[..., 2])))

        dx = float(xf[-1] - xf[0])
        dy = float(yf[-1] - yf[0])
        relief = max(zhi - zlo, 0.1)
        ve = 3 if relief / max(dx, dy, 1.0) >= 0.015 else 5
        ax.set_xlim(float(xf[0]), float(xf[-1]))
        ax.set_ylim(float(yf[0]), float(yf[-1]))
        ax.set_zlim(zlo, zhi)
        ax.set_box_aspect((dx, dy, relief * ve), zoom=1.5)
        ax.view_init(elev=32, azim=-60)
        ax.set_proj_type("ortho")
        ax.set_axis_off()
        note = f"Vertical exaggeration {ve}x"
        if tex is not None:
            note += "  |  Imagery: USGS The National Map"
        ax.text2D(0.01, 0.02, note, transform=ax.transAxes, fontsize=6.5, color="#333")
        fig.subplots_adjust(left=0, right=1, bottom=0, top=1)
        out = _autocrop(_png(fig))
        plt.close(fig)
        return out
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


def render_map_suite(spatial: dict) -> dict:
    """All site-map figures for the report from the spatial bundle: the topo/imagery
    overviews, the WSE and head maps, the flow-path plan view, and the static 3-D view.
    Fetches each USGS basemap service ONCE and shares it across the figures; anything
    missing (offline, no raster, no paths) degrades to whatever can still be drawn."""
    out: dict = {}
    try:
        from pyproj import CRS, Transformer

        from . import mesh as mesh_mod

        spatial = spatial or {}
        crs_wkt = spatial.get("crs_wkt")
        if not crs_wkt:
            return out
        pv = spatial.get("planview") or {}
        paths_gdf = spatial.get("paths_gdf")
        tr = Transformer.from_crs("EPSG:4326", CRS.from_user_input(crs_wkt), always_xy=True)

        def _proj(pts):
            if not pts:
                return None
            xs, ys = tr.transform([p[0] for p in pts], [p[1] for p in pts])
            return list(zip(xs, ys))

        reach_xy = _proj(pv.get("reach_lonlat"))
        domain_xy = _proj(pv.get("domain_lonlat"))
        sides_xy = {k: _proj(v) for k, v in (spatial.get("sides_lonlat") or {}).items() if v}
        bbox = _report_bbox(xy_lists=[reach_xy, domain_xy], gdfs=[paths_gdf])
        if bbox is None:
            return out

        def _fetch(service, fmt):
            try:
                return _decode_img(mesh_mod.fetch_basemap_image(
                    crs_wkt, bbox[0], bbox[2], bbox[1], bbox[3], service=service, fmt=fmt,
                    max_px=1024, timeout_s=12.0, log=lambda *_: None))
            except Exception:  # noqa: BLE001
                return None

        topo = _fetch("USGSTopo", "png")
        imagery = _fetch("USGSImageryOnly", "jpg")
        common = dict(bbox=bbox, reach_xy=reach_xy, domain_xy=domain_xy)
        out["map_topo"] = render_vector_map(basemap=topo, sides_xy=sides_xy, **common)
        out["map_imagery"] = render_vector_map(basemap=imagery, sides_xy=sides_xy, **common)
        out["map_wse"] = render_wse_map(wse_tif=spatial.get("wse_tif"), crs_wkt=crs_wkt,
                                        basemap=topo, **common)
        out["map_head"] = render_head_map(head_tif=spatial.get("head_tif"), crs_wkt=crs_wkt,
                                          basemap=topo, **common)
        out["map_paths"] = render_paths_map(paths_gdf=paths_gdf, basemap=topo, **common)
        out["map_3d"] = render_iso3d(gwf_ws=spatial.get("gwf_ws"),
                                     dem_path=spatial.get("dem_path"), crs_wkt=crs_wkt,
                                     wse_tif=spatial.get("wse_tif"),
                                     imagery=(imagery[0] if imagery else None),
                                     imagery_extent=(imagery[1] if imagery else None),
                                     sides_xy=sides_xy)
    except Exception:  # noqa: BLE001 — the map suite is best-effort
        return out
    return out


__all__ = ["render_threshold_bar", "render_opportunity_curve", "render_planview_figure",
           "render_section_figure", "render_vector_map", "render_wse_map", "render_head_map",
           "render_paths_map", "render_iso3d", "render_map_suite"]
