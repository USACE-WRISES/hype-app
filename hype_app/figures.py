"""Best-effort report figures (report §10, §17.4).

Every function returns PNG bytes or None on any failure, uses the headless Agg backend, and imports
matplotlib lazily so importing this module stays cheap. Spatial figures take already-loaded GeoJSON
dicts / GeoDataFrames so this module never reads the workspace itself.
"""
from __future__ import annotations

import io


def _agg_pyplot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    return plt


def _png(fig) -> bytes:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, bbox_inches="tight")
    return buf.getvalue()


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
        finite = times[np.isfinite(times) & (times > 0)]
        vmin = float(finite.min()) if finite.size else 1e-3
        vmax = float(finite.max()) if finite.size else 1.0
        norm = mpl.colors.LogNorm(vmin=max(1e-3, vmin), vmax=max(vmin * 10, vmax))
        fig, ax = plt.subplots(figsize=(6.4, 3.0))
        lc = LineCollection(segs, cmap=mpl.cm.viridis, norm=norm, linewidths=0.8, alpha=0.85)
        lc.set_array(times)
        ax.add_collection(lc)
        ax.autoscale()
        ax.set_xlabel("Distance along reach (m)")
        ax.set_ylabel("Elevation (m)")
        cb = fig.colorbar(lc, ax=ax)
        cb.set_label("Residence time (days)")
        ax.grid(True, ls=":", lw=0.3, alpha=0.5)
        fig.tight_layout()
        out = _png(fig)
        plt.close(fig)
        return out
    except Exception:  # noqa: BLE001 — figures are best-effort
        return None


__all__ = ["render_threshold_bar", "render_planview_figure", "render_section_figure"]
