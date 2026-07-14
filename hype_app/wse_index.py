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


__all__ = ["build_edge_samples", "nearest_edge"]
