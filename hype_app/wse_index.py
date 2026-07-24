"""Preview-side WSE edge index: valid-pixel centers bordering nodata + nearest-edge lookup.

Mirrors the validity mask of the engine's `my_utils.build_wse_valid_edge_index` (non-finite,
declared nodata, -9999-style sentinels, <= -1e20) without the skimage polygonization. Distances
are to edge-pixel CENTERS — within half a pixel of the engine's polygon-border distance
(`nearest_wse_edge_distance_and_value`), which is fine for the gradient-point head preview; the
run's own diagnostics stay authoritative.
"""
from __future__ import annotations


def build_edge_samples(raster_path, *, extra_nodata=(-9999.0,), decimate_above=40_000_000):
    """Edge-pixel samples of the valid (wetted) region of a WSE raster.

    Returns {"x", "y", "value" (float arrays over edge-pixel centers), "crs"} in the raster's
    CRS, or None when nothing is valid. Rasters above `decimate_above` pixels are read at
    every-other-pixel resolution (display use only).
    """
    import numpy as np
    import rasterio
    from rasterio.transform import xy as rio_xy
    from scipy.ndimage import binary_erosion

    with rasterio.open(raster_path) as src:
        step = 2 if (src.width * src.height) > int(decimate_above) else 1
        arr = src.read(1)[::step, ::step].astype("float64")
        tfm = src.transform * src.transform.scale(step, step)
        crs, nod = src.crs, src.nodata
    invalid = ~np.isfinite(arr)
    if nod is not None:
        invalid |= np.isclose(arr, float(nod))
    for v in extra_nodata:
        invalid |= np.isclose(arr, float(v))
    invalid |= arr <= -1.0e20
    valid = ~invalid
    if not valid.any():
        return None
    interior = binary_erosion(valid, structure=np.ones((3, 3), bool), border_value=0)
    rr, cc = np.nonzero(valid & ~interior)
    if rr.size == 0:                       # a fully-interior blob (no border ring) — use all
        rr, cc = np.nonzero(valid)
    xs, ys = rio_xy(tfm, rr, cc, offset="center")
    return {"x": np.asarray(xs, dtype="float64"), "y": np.asarray(ys, dtype="float64"),
            "value": arr[rr, cc], "crs": crs}


def nearest_edge(index, x, y):
    """(distance, wse, edge_x, edge_y, i) of the nearest edge pixel to (x, y).

    Units are the index's coordinate units — reproject the x/y arrays to a metric CRS first when
    the raster is geographic. `i` is the flat sample index (for parallel display arrays)."""
    import numpy as np

    dx = index["x"] - float(x)
    dy = index["y"] - float(y)
    i = int(np.argmin(dx * dx + dy * dy))
    return (float(np.hypot(dx[i], dy[i])), float(index["value"][i]),
            float(index["x"][i]), float(index["y"][i]), i)


def valid_samples_along_line(raster_path, feat_4326, *, extra_nodata=(-9999.0,),
                             min_n=64, max_n=4001):
    """Valid WSE samples along a LineString Feature (EPSG:4326) — cap-line corner anchoring.

    Samples the raster every HALF PIXEL along the line (Nyquist against the pixel grid, so a
    one-cell-wide channel crossing cannot fall between samples; the +1 puts samples exactly at
    the line's endpoints) and keeps only valid values: the build_edge_samples mask plus the
    <= -1000 undeclared-sentinel guard from delineate.min_elevation_along_line — cap sampling
    reads arbitrary interior pixels, where -9999-style uploads bite hardest. Mirrors the
    engine's `my_utils.nearest_valid_wse_along_line` (same density + validity rules; parity-
    tested). Returns {"x", "y", "value", "crs"} in the raster's CRS, or None when the geometry
    is degenerate or nothing valid samples.
    """
    import numpy as np
    import rasterio
    from pyproj import Transformer

    try:
        coords = feat_4326["geometry"]["coordinates"]
    except Exception:  # noqa: BLE001
        return None
    if not coords or len(coords) < 2:
        return None
    with rasterio.open(raster_path) as src:
        tr = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        vx, vy = tr.transform([c[0] for c in coords], [c[1] for c in coords])
        vx, vy = np.asarray(vx, dtype="float64"), np.asarray(vy, dtype="float64")
        seg = np.hypot(np.diff(vx), np.diff(vy))
        length = float(seg.sum())
        if length <= 0:
            return None
        a = src.transform
        px = float(max(np.hypot(a.a, a.d), np.hypot(a.b, a.e))) or 1.0
        n = int(np.clip(np.ceil(length / (0.5 * px)) + 1, min_n, max_n))
        # arc-length-uniform positions along the (possibly multi-vertex) polyline, pure numpy
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        s = np.linspace(0.0, length, n)
        i = np.clip(np.searchsorted(cum, s, side="right") - 1, 0, seg.size - 1)
        t = (s - cum[i]) / np.where(seg[i] > 0, seg[i], 1.0)
        sx = vx[i] + t * (vx[i + 1] - vx[i])
        sy = vy[i] + t * (vy[i + 1] - vy[i])
        vals = np.array([v[0] for v in src.sample(np.column_stack([sx, sy]))], dtype="float64")
        crs, nod = src.crs, src.nodata
    invalid = ~np.isfinite(vals)
    if nod is not None:
        invalid |= np.isclose(vals, float(nod))
    for v in extra_nodata:
        invalid |= np.isclose(vals, float(v))
    invalid |= vals <= -1.0e20
    invalid |= vals <= -1000.0
    keep = ~invalid
    if not keep.any():
        return None
    return {"x": sx[keep], "y": sy[keep], "value": vals[keep], "crs": crs}


__all__ = ["build_edge_samples", "nearest_edge", "valid_samples_along_line"]
