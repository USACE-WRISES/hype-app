"""Turn run_hyporheic artifacts into map-ready GeoJSON (EPSG:4326) + summary text."""
from __future__ import annotations

import json
from pathlib import Path

import geopandas as gpd


def _read_gdf_4326(path, max_features: int = 4000):
    if not path:
        return None
    p = Path(path)
    if not p.exists():
        return None
    gdf = gpd.read_file(p)
    if gdf.empty:
        return None
    gdf = gdf.to_crs(4326)
    if len(gdf) > max_features:                      # down-sample dense particle sets
        gdf = gdf.sample(max_features, random_state=0).sort_index()
    return gdf


def _to_geojson_4326(path, max_features: int = 4000):
    gdf = _read_gdf_4326(path, max_features=max_features)
    return None if gdf is None else json.loads(gdf.to_json())


def pathlines_geojson(result: dict):
    return _to_geojson_4326(result.get("pathlines_fc"))


def points_geojson(result: dict):
    return _to_geojson_4326(result.get("points_fc"), max_features=6000)


def pathlines_gdf_4326(result: dict):
    """The flow-path lines as a 4326 GeoDataFrame of (particleid, geometry) — the app renders the
    map layer from this same (possibly down-sampled) frame, so the displayed set is exactly the
    selectable set. Total feature count is capped like the old GeoJSON path (seeded sample)."""
    gdf = _read_gdf_4326(result.get("pathlines_fc"))
    if gdf is None:
        return None
    if "particleid" not in gdf.columns:              # defensive — the engine always writes it
        gdf = gdf.assign(particleid=range(len(gdf)))
    gdf["particleid"] = gdf["particleid"].astype(int)
    return gdf[["particleid", "geometry"]]


def gdf_geojson(gdf):
    """GeoJSON dict for an already-4326 GeoDataFrame (the app draws the flow-path layer from the
    same frame it keeps for selection, so displayed == selectable)."""
    return None if gdf is None or len(gdf) == 0 else json.loads(gdf.to_json())


def flowpath_downsampled(result: dict, gdf) -> bool:
    """True when `gdf` is a down-sampled subset of the pathlines shapefile (>4000 features)."""
    import pyogrio
    p = result.get("pathlines_fc")
    if gdf is None or not p or not Path(p).exists():
        return False
    try:
        n = pyogrio.read_info(str(p)).get("features")
        return n is not None and int(n) > len(gdf)
    except Exception:  # noqa: BLE001
        return False


# NOTE on units: hypetool hardcodes "_ft" into its output column names, but it never converts —
# the values are simply in whatever units the inputs used. This app's pipeline is metric end to
# end (3DEP meters, UTM CRS, cell sizes in m), so lengths are m, velocities m/day, heads m. The
# stat columns here are therefore unit-neutral; the pane labels say m / days / m/day.
_FP_STAT_COLS = ["length", "horiz", "depth", "rtime_d", "vel",
                 "head_start", "head_end", "hyd_grad"]


def _fp_summary_file(out_dir, name: str):
    """summary/Forward_<name>, tolerating a different tracking-direction prefix."""
    sdir = Path(out_dir) / "summary"
    p = sdir / f"Forward_{name}"
    if p.exists():
        return p
    hits = sorted(sdir.glob(f"*_{name}"))
    return hits[0] if hits else None


def _fp_geom_stats(result: dict):
    """Fallback per-particle stats from the 3D pathline geometry when the summary CSVs are
    missing: horizontal length, 3D length and depth below start, all in the model's own units
    (no conversion — geometry CRS and model units agree in this pipeline). Returns a DataFrame
    indexed by particleid, or None."""
    import numpy as np
    import pandas as pd
    p = result.get("pathlines_fc_3d") or result.get("pathlines_fc")
    if not p or not Path(p).exists():
        return None
    gdf = gpd.read_file(p)
    if gdf.empty or "particleid" not in gdf.columns:
        return None
    rows = {}
    for pid, geom in zip(gdf["particleid"].astype(int), gdf.geometry):
        if geom is None or geom.is_empty or geom.geom_type != "LineString":
            continue
        c = np.asarray(geom.coords, dtype=float)
        if len(c) < 2:
            continue
        dxy = np.hypot(np.diff(c[:, 0]), np.diff(c[:, 1]))
        horiz = float(dxy.sum())
        if c.shape[1] >= 3:
            z = c[:, 2]
            depth = float(z[0] - z.min())
            length = float(np.sqrt(dxy ** 2 + np.diff(z) ** 2).sum())
        else:
            depth = length = np.nan
        rows[int(pid)] = (length, horiz, depth)
    if not rows:
        return None
    df = pd.DataFrame.from_dict(rows, orient="index", columns=["length", "horiz", "depth"])
    df.index.name = "particleid"
    return df


def flowpath_stats(result: dict, out_dir):
    """Per-particle flow-path metrics for the Results properties pane, indexed by particleid:
    length (3D along-path), horiz (Σ√(Δx²+Δy²)), depth (start z − deepest z), rtime_d, vel,
    head_start, head_end, hyd_grad. Missing sources leave NaN columns ("n/a" in the pane)
    rather than raising. Values are in the model's units (metric in this app: m / days / m/day;
    hypetool's "_ft" column names are labels only — see _FP_STAT_COLS note)."""
    import numpy as np
    import pandas as pd

    out = None
    summary_p = _fp_summary_file(out_dir, "particle_summary_table.csv")
    if summary_p is not None:
        try:
            s = pd.read_csv(summary_p)
            s = s.set_index(pd.to_numeric(s["particleid"], errors="coerce").astype(int))
            out = pd.DataFrame(index=s.index)
            for src, dst in (("total_length_ft", "length"),
                             ("total_residence_time_days", "rtime_d"),
                             ("particle_velocity_ft_per_day", "vel"),
                             ("starting_hydraulic_head", "head_start"),
                             ("ending_hydraulic_head", "head_end"),
                             ("hydraulic_gradient", "hyd_grad")):
                out[dst] = (pd.to_numeric(s[src], errors="coerce")
                            if src in s.columns else np.nan)
        except Exception:  # noqa: BLE001
            out = None

    verts_p = _fp_summary_file(out_dir, "pathlines_filtered.csv")
    if verts_p is not None:
        try:
            # The CSV carries raw vertices (particleid, time, x, y, z, …) — the Δx/Δy/Δz columns
            # the engine uses internally are NOT written, so derive the deltas here.
            v = pd.read_csv(verts_p, usecols=["particleid", "time", "x", "y", "z"])
            v["particleid"] = pd.to_numeric(v["particleid"], errors="coerce").astype(int)
            v = v.sort_values(["particleid", "time"])
            gb0 = v.groupby("particleid")
            v["_h"] = np.hypot(gb0["x"].diff(), gb0["y"].diff())  # NaN first rows → skipped by sum
            gb = v.groupby("particleid")
            vd = pd.DataFrame({"horiz": gb["_h"].sum(),
                               "depth": gb["z"].first() - gb["z"].min()})
            out = vd if out is None else out.join(vd, how="outer")
        except Exception:  # noqa: BLE001
            pass

    if out is None or out.reindex(columns=["length", "horiz", "depth"]).isna().all(axis=None):
        geo = _fp_geom_stats(result)                 # CSVs missing → best-effort from geometry
        if geo is not None:
            out = geo if out is None else out.combine_first(geo)
    if out is None:
        return None
    out = out.reindex(columns=_FP_STAT_COLS)
    out.index.name = "particleid"
    return out


def flowpath_nodes_geojson(gdf):
    """(start_gj, end_gj) point FeatureCollections from the 4326 pathlines gdf. Pathline vertices
    are time-ordered by construction, so coords[0]/coords[-1] are each particle's start/end —
    and the nodes land exactly on the drawn (possibly down-sampled) paths."""
    if gdf is None or len(gdf) == 0:
        return None, None

    def _pt(pid, c):
        return {"type": "Feature", "properties": {"particleid": int(pid)},
                "geometry": {"type": "Point", "coordinates": [float(c[0]), float(c[1])]}}

    starts, ends = [], []
    for pid, geom in zip(gdf["particleid"], gdf.geometry):
        if geom is None or geom.is_empty:
            continue
        parts = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
        try:
            c0 = parts[0].coords[0]
            c1 = parts[-1].coords[-1]
        except Exception:  # noqa: BLE001
            continue
        starts.append(_pt(pid, c0))
        ends.append(_pt(pid, c1))
    fc = lambda feats: ({"type": "FeatureCollection", "features": feats} if feats else None)  # noqa: E731
    return fc(starts), fc(ends)


def hist_datauri(values, *, label: str, unit: str):
    """A small histogram PNG (base64 data URI) for the multi-select flow-path pane, with a mean
    line. Returns None when there are no finite values."""
    import base64
    import io

    import matplotlib.pyplot as plt
    import numpy as np

    a = np.asarray(list(values), dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return None
    fig, ax = plt.subplots(figsize=(3.2, 1.6))
    bins = int(min(24, max(6, round(a.size ** 0.5) * 2)))
    ax.hist(a, bins=bins, color="#2b7bff", edgecolor="white", linewidth=0.4)
    ax.axvline(float(a.mean()), color="#e02020", linewidth=1.0)
    ax.set_xlabel(f"{label} ({unit})", fontsize=8)
    ax.set_ylabel("count", fontsize=8)
    ax.tick_params(labelsize=7)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", transparent=True)
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def bounds_latlon(result: dict):
    """[[south, west], [north, east]] over the result vectors, for map.fit_bounds."""
    for key in ("pathlines_fc", "points_fc", "pathlines_fc_3d"):
        p = result.get(key)
        if p and Path(p).exists():
            gdf = gpd.read_file(p).to_crs(4326)
            if not gdf.empty:
                minx, miny, maxx, maxy = (float(v) for v in gdf.total_bounds)
                return [[miny, minx], [maxy, maxx]]
    return None


def summary_text(result: dict, out_dir) -> str:
    """Best-effort: the engine's publication-ready stats .txt, else a short fallback."""
    stats = Path(out_dir) / "summary" / "Forward_pathline_stats.txt"
    if stats.exists():
        try:
            return stats.read_text(encoding="utf-8-sig")
        except Exception:  # noqa: BLE001
            pass
    grid = result.get("grid") or {}
    return (f"Grid: {grid.get('ncol')}×{grid.get('nrow')}×{grid.get('nlay')} "
            f"({grid.get('n_cells_total'):,} cells)\n"
            f"Pathlines: {result.get('pathlines_fc')}\n"
            f"Points: {result.get('points_fc')}")


# ---- hydraulic-head + grid visualization (per-layer GeoTIFFs the engine already exports) ----

def head_rasters(work_dir, result: dict | None = None) -> list[str]:
    """Sorted per-layer head GeoTIFFs (index 0 = head_L01 = top layer)."""
    import glob
    d = Path(work_dir) / "summary" / "head" / "per_layer_tif"
    tifs = sorted(glob.glob(str(d / "head_L*.tif")))
    if tifs:
        return tifs
    head = (result or {}).get("head") or {}            # fallback to engine-reported paths
    for key in ("geotiffs", "tifs", "per_layer_tif"):
        v = head.get(key) if isinstance(head, dict) else None
        if isinstance(v, (list, tuple)) and v:
            return sorted(str(p) for p in v)
    return []


def _valid_mask(a, nodata):
    import numpy as np
    m = np.isfinite(a)
    if nodata is not None:
        m &= (a != nodata)
    return m & (a > -9000.0)                            # guard -9999 sentinel / HDRY


def head_value_range(paths) -> tuple[float, float]:
    """Global (vmin, vmax) of head across all layers, ignoring nodata — keeps colors comparable."""
    import numpy as np
    import rasterio
    lo, hi = np.inf, -np.inf
    for f in paths:
        with rasterio.open(f) as s:
            a = s.read(1).astype("float64"); nod = s.nodata
        m = _valid_mask(a, nod)
        if m.any():
            lo = min(lo, float(a[m].min())); hi = max(hi, float(a[m].max()))
    if not (np.isfinite(lo) and np.isfinite(hi)) or hi <= lo:
        return (0.0, 1.0)
    return (lo, hi)


def raster_overlay(path, *, vmin, vmax, cmap="viridis", max_dim: int = 1024,
                   smooth_to: int = 700) -> dict:
    """Colorize a single-band raster (e.g. a head layer) → ipyleaflet ImageOverlay payload
    {"url","bounds"} in EPSG:4326, transparent at nodata. NaN-aware upsampling renders the
    otherwise blocky ~180 px per-cell field as a smooth, crisp overlay."""
    import matplotlib
    import numpy as np
    from matplotlib.colors import Normalize

    from .dem import load_raster_4326, rgba_to_overlay
    z, xs, ys, _dx, _dy = load_raster_4326(path, max_dim=max_dim)
    valid = np.isfinite(z)
    f = int(smooth_to // max(z.shape)) if max(z.shape) else 1
    if f >= 2 and valid.any():
        try:                                            # smooth head within valid area, crisp edges
            from scipy.ndimage import zoom
            z = zoom(np.where(valid, z, float(np.nanmean(z[valid]))), f, order=1)
            valid = zoom(valid.astype(np.float32), f, order=0) > 0.5
        except Exception:  # noqa: BLE001 — scipy missing / zoom failure: keep native resolution
            pass
    cmap_obj = (matplotlib.colormaps[cmap] if hasattr(matplotlib, "colormaps")
                else matplotlib.cm.get_cmap(cmap))
    rgba = cmap_obj(Normalize(vmin=vmin, vmax=vmax)(np.where(valid, z, vmin)))
    rgba[..., 3] = valid.astype(float)
    return rgba_to_overlay(rgba, xs, ys)


def head_contours_geojson(path, *, levels):
    """Hydraulic-head contour lines (EPSG:4326 GeoJSON LineStrings, each with a `level`)."""
    import numpy as np
    import rasterio
    from contourpy import contour_generator
    from pyproj import Transformer

    with rasterio.open(path) as s:
        a = s.read(1).astype("float64"); nod = s.nodata; transform = s.transform; crs = s.crs
    z = np.where(_valid_mask(a, nod), a, np.nan)        # NaN → contourpy masks it (no edge artifacts)
    if not np.isfinite(z).any():
        return None
    cg = contour_generator(z=z)
    tr = Transformer.from_crs(crs, 4326, always_xy=True)
    feats = []
    for lv in levels:
        for seg in cg.lines(float(lv)):                 # seg: (N,2) array of (col, row) index coords
            if len(seg) < 2:
                continue
            xs_, ys_ = rasterio.transform.xy(transform, seg[:, 1], seg[:, 0], offset="center")
            lon, lat = tr.transform(np.asarray(xs_), np.asarray(ys_))
            coords = [[float(x), float(y)] for x, y in zip(np.atleast_1d(lon), np.atleast_1d(lat))]
            feats.append({"type": "Feature", "properties": {"level": round(float(lv), 3)},
                          "geometry": {"type": "LineString", "coordinates": coords}})
    return {"type": "FeatureCollection", "features": feats} if feats else None


def head_contour_labels(gj, *, max_labels: int = 40):
    """One (lat, lon, "NNN.N") label per contour line (at its midpoint), decimated to max_labels."""
    if not gj:
        return []
    out = []
    for f in gj.get("features", []):
        coords = f.get("geometry", {}).get("coordinates") or []
        if len(coords) < 2:
            continue
        lon, lat = coords[len(coords) // 2]
        out.append((float(lat), float(lon), f"{float(f['properties']['level']):.1f}"))
    if len(out) > max_labels:
        s = len(out) / max_labels
        out = [out[int(i * s)] for i in range(max_labels)]
    return out


def grid_overlay(paths, *, max_dim: int = 1400):
    """Render ONLY the actively-modeled cells as a grid-line overlay (EPSG:4326), aligned with
    the head overlay. Active mask ≈ idomain = union of (head ≠ nodata) across all layers (the
    deep layers fill the whole domain). Returns {"url","bounds"} or None."""
    import numpy as np
    import rasterio
    import rioxarray  # noqa: F401 — .rio accessor
    from rasterio.enums import Resampling

    from .dem import rgba_to_overlay
    mask = None
    for f in paths:
        with rasterio.open(f) as s:
            a = s.read(1).astype("float64"); nod = s.nodata
        m = _valid_mask(a, nod)
        mask = m if mask is None else (mask | m)
    if mask is None or not mask.any():
        return None
    da = rioxarray.open_rasterio(paths[0], masked=True).squeeze()
    da_m = da.copy(data=mask.astype("float32")).rio.write_nodata(0.0)
    da_m = da_m.rio.reproject("EPSG:4326", resampling=Resampling.nearest)
    M = np.asarray(da_m.values, dtype=float) > 0.5
    xs = np.asarray(da_m["x"].values, dtype=float); ys = np.asarray(da_m["y"].values, dtype=float)
    if ys[0] < ys[-1]:
        M = M[::-1, :]; ys = ys[::-1]
    nr, nc = M.shape
    u = max(1, min(6, int(max_dim // max(nr, nc))))
    rgba = np.zeros((nr * u, nc * u, 4), dtype=float)
    up = np.repeat(np.repeat(M, u, axis=0), u, axis=1)
    if u >= 2:                                          # paint each active cell's edges as a line
        rr = np.arange(nr * u) % u; cc = np.arange(nc * u) % u
        edge = (rr[:, None] == 0) | (rr[:, None] == u - 1) | (cc[None, :] == 0) | (cc[None, :] == u - 1)
        rgba[up & edge] = [0.16, 0.16, 0.16, 0.9]
    else:                                               # very large grid: faint fill instead of lines
        rgba[up] = [0.30, 0.30, 0.30, 0.25]
    return rgba_to_overlay(rgba, xs, ys)


def colorbar_datauri(vmin, vmax, *, cmap="viridis", label="Hydraulic head") -> str:
    """A small horizontal colorbar PNG (base64 data URI) for the Results legend."""
    import base64
    import io

    import matplotlib
    import matplotlib.pyplot as plt
    from matplotlib.colors import Normalize

    cmap_obj = (matplotlib.colormaps[cmap] if hasattr(matplotlib, "colormaps")
                else matplotlib.cm.get_cmap(cmap))
    fig, ax = plt.subplots(figsize=(3.2, 0.55))
    cb = matplotlib.colorbar.ColorbarBase(ax, cmap=cmap_obj, norm=Normalize(vmin, vmax),
                                          orientation="horizontal")
    cb.set_label(label, fontsize=8)
    ax.tick_params(labelsize=7)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120, bbox_inches="tight", transparent=True)
    plt.close(fig)
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
