"""Channel carving — burn a uniform trapezoidal cross-section into the DEM along the reach
centerline.

The carve works ON THE ORIGINAL RASTER GRID (windowed; no reprojection/resampling of the DEM
itself), so the output is drop-in identical to the input for every downstream consumer (HEC-RAS
createterrain, the groundwater terrain setup, hillshade display). Only the affected window is
recomputed:

    station s, offset d   = each pixel located against the centerline (metric CRS)
    thalweg(s)            = moving-average(DEM sampled along the line) − depth
    target(pixel)         = thalweg(s) + max(0, d − bottom_width/2) / side_slope_H
    carved                = min(dem, target)          # carving only lowers, never fills

Side slope is H:1V (horizontal metres per vertical metre), so larger = gentler banks.
"""
from __future__ import annotations

import numpy as np


def carve_channel(dem_path: str, centerline_feat: dict, out_path: str, *,
                  bottom_width_m: float = 4.0, depth_m: float = 1.5,
                  side_slope_h_per_v: float = 2.0, smooth_window_m: float = 60.0,
                  diff_path: str | None = None, log=print) -> dict:
    import geopandas as gpd
    import rasterio
    import shapely
    from pyproj import Transformer
    from rasterio.features import geometry_mask
    from rasterio.windows import Window, from_bounds, intersection
    from shapely.geometry import shape

    g_src = gpd.GeoSeries([shape(centerline_feat["geometry"])], crs=4326)
    crs_m = g_src.estimate_utm_crs()
    line_m = g_src.to_crs(crs_m).iloc[0]
    H = max(float(side_slope_h_per_v), 0.05)
    bw2 = max(float(bottom_width_m), 0.0) / 2.0
    depth = max(float(depth_m), 0.01)
    half_w = bw2 + depth * H

    with rasterio.open(dem_path) as src:
        # carve footprint (buffered) in the RASTER's CRS
        buf = gpd.GeoSeries([line_m.buffer(half_w + 8.0)], crs=crs_m).to_crs(src.crs).iloc[0]
        win = from_bounds(*buf.bounds, transform=src.transform)
        win = win.round_offsets(op="floor").round_lengths(op="ceil")
        win = intersection(win, Window(0, 0, src.width, src.height))
        if win.width <= 0 or win.height <= 0:
            raise ValueError("The centerline does not overlap the DEM.")
        arr = src.read(1, window=win).astype("float64")
        wt = src.window_transform(win)
        nodata = src.nodata
        mask = geometry_mask([buf], out_shape=arr.shape, transform=wt, invert=True)
        rows, cols = np.where(mask)
        if not len(rows):
            raise ValueError("No DEM cells fall inside the carve footprint.")

        xs, ys = rasterio.transform.xy(wt, rows, cols)
        to_m = Transformer.from_crs(src.crs, crs_m, always_xy=True)
        xm, ym = to_m.transform(np.asarray(xs), np.asarray(ys))
        pts = shapely.points(xm, ym)
        s = shapely.line_locate_point(line_m, pts)      # station along the line (m)
        d = shapely.distance(line_m, pts)               # lateral offset (m)

        # thalweg profile: existing ground along the line, smoothed, minus the carve depth
        L = float(line_m.length)
        step = float(np.clip(L / 400.0, 1.0, 5.0))
        ss = np.arange(0.0, L + step, step)
        lp = shapely.line_interpolate_point(line_m, ss)
        to_src = Transformer.from_crs(crs_m, src.crs, always_xy=True)
        lx, ly = to_src.transform(shapely.get_x(lp), shapely.get_y(lp))
        zs = np.array([v[0] for v in src.sample(np.column_stack([lx, ly]))], dtype="float64")
        if nodata is not None:
            zs = np.where(zs == nodata, np.nan, zs)
        wpts = max(1, int(round(float(smooth_window_m) / step)))
        if wpts > 1:
            k = np.ones(wpts) / wpts
            fin = np.isfinite(zs)
            num = np.convolve(np.where(fin, zs, 0.0), k, mode="same")
            den = np.convolve(fin.astype(float), k, mode="same")
            zs = np.where(den > 0, num / den, np.nan)
        if not np.isfinite(zs).any():
            raise ValueError("Could not sample terrain along the centerline.")
        zs = np.where(np.isfinite(zs), zs, np.nanmean(zs))
        thal = np.interp(s, ss, zs) - depth

        target = thal + np.maximum(0.0, d - bw2) / H
        cur = arr[rows, cols]
        valid = np.isfinite(target) & np.isfinite(cur)
        if nodata is not None:
            valid &= cur != nodata
        cut = valid & (target < cur)
        arr[rows, cols] = np.where(cut, target, cur)
        n_cut = int(cut.sum())
        max_cut = float(np.max(cur[cut] - target[cut])) if n_cut else 0.0

        out = src.read(1)
        r0, c0 = int(win.row_off), int(win.col_off)
        out[r0:r0 + arr.shape[0], c0:c0 + arr.shape[1]] = arr.astype(out.dtype)
        meta = src.meta.copy()
        orig_full = None
        if diff_path:
            orig_full = src.read(1).astype("float64")

    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(out, 1)

    if diff_path and orig_full is not None:
        diff = orig_full - out.astype("float64")     # >0 where lowered
        bad = ~np.isfinite(diff) | (diff < 0.005)
        if nodata is not None:
            bad |= orig_full == nodata
        dd = np.where(bad, -9999.0, diff).astype("float32")
        m2 = meta.copy()
        m2.update(dtype="float32", nodata=-9999.0)
        with rasterio.open(diff_path, "w", **m2) as dst:
            dst.write(dd, 1)

    log(f"[carve] {n_cut} cells lowered, max cut {max_cut:.2f} m "
        f"(bw={bottom_width_m} m, depth={depth} m, slope {H}:1)")
    return {"path": str(out_path), "diff_path": str(diff_path) if diff_path else None,
            "cells_cut": n_cut, "max_cut_m": round(max_cut, 3)}


def child_carve(payload: dict, q) -> None:
    """Run the carve in a spawned child process (crash isolation): puts ('log', line)... then
    ('result', dict) or ('error', message) on `q`. Top-level and picklable for the 'spawn'
    start method. Isolation matters: shapely's vectorized creation inside carve_channel can
    hard-crash the interpreter (Windows 0x80000003 while "Garbage-collecting", numpy 2.5.0 +
    shapely 2.1.2, observed 2026-07-07) — in a child the server survives and reports the
    failure instead of dying."""
    try:
        res = carve_channel(payload["dem"], payload["feat"], payload["out"],
                            bottom_width_m=float(payload["bw"]), depth_m=float(payload["depth"]),
                            side_slope_h_per_v=float(payload["slope"]),
                            diff_path=payload.get("diff"),
                            log=lambda m: q.put(("log", str(m))))
        q.put(("result", res))
    except Exception as e:  # noqa: BLE001
        q.put(("error", str(e)))
