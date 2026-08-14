"""Acquire the working DEM (`inputs/dem.tif`): USGS 3DEP download or local-GeoTIFF import.

3DEP DEMs are hydro-flattened, so by default this same DEM serves as BOTH the terrain
and the water-surface elevation in run_hyporheic (the caller may override WSE later).
A local import (`import_local_dem`) writes the SAME working file over the SAME reach-buffer
AOI, so every downstream consumer (carve, delineation, RAS terrain, GW grid, hillshade) is
source-agnostic; the project keeps only a pointer to the source file.
"""
from __future__ import annotations

from pathlib import Path

import py3dep
from shapely.geometry import box

# Keep the fetch in step with hype_app.estimate.estimate_cells (same buffer).
BUFFER_FRAC = 0.12
_RES_PRIORITY = (1, 3, 5, 10, 30)     # finest-first; 3DEP metres (1/3/5 m are lidar-only)
_MAX_PIXELS = 7_000_000               # stay under py3dep's ~8 M dynamic-service cap

DEM_SUFFIXES = frozenset({".tif", ".tiff"})   # local DEM import is GeoTIFF-only


class DemImportError(RuntimeError):
    """A local-DEM import failure whose message is written for the user (shown verbatim)."""


def _candidate_resolutions(aoi_bounds, requested) -> list:
    """Ordered 3DEP resolutions (m) to try. ``requested='auto'`` → finest available first (via
    ``check_3dep_availability``); an explicit value → just that. 10 m then 30 m are always appended
    as nationwide last-resorts, so a fetch always has something to fall back to."""
    order = []
    if requested in (None, "auto", "Auto"):
        try:
            avail = py3dep.check_3dep_availability(tuple(float(b) for b in aoi_bounds))
        except Exception:  # noqa: BLE001 — service hiccup → just try finest-first
            avail = {}
        order = [r for r in _RES_PRIORITY if avail.get(f"{r}m") is True]
        if not order:                 # availability unknown/failed → try everything, finest-first
            order = list(_RES_PRIORITY)
    else:
        order = [int(requested)]
    for r in (10, 30):                # nationwide safety net
        if r not in order:
            order.append(r)
    return order


_IMAGESERVER = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
                "3DEPElevation/ImageServer/exportImage")
_IMAGESERVER_MAX_SIDE = 4000        # the service caps exports around 4100 px per side


def _fetch_imageserver(aoi_bounds4326, res_m, out_path, w_m, h_m):
    """Direct 3DEP fetch via the ArcGIS ImageServer with plain urllib.

    py3dep's fine-resolution (1/3/5 m) path goes through aiohttp, whose async DNS
    resolver can fail on hosts the system resolver handles fine — observed as
    'Cannot connect to host elevation.nationalmap.gov' while urllib requests to the
    SAME host succeed. This fallback keeps 1 m lidar reachable in that situation
    (losing it silently degrades every downstream surface — RAS property tables,
    result rasters, wetted extent). Raises on failure; validates that the returned
    tile actually contains data (the server answers all-nodata where a collection
    has no coverage)."""
    import shutil
    import urllib.parse
    import urllib.request

    import numpy as np
    import rasterio

    w_px, h_px = round(w_m / res_m), round(h_m / res_m)
    if max(w_px, h_px) > _IMAGESERVER_MAX_SIDE:
        raise RuntimeError(f"AOI too large for a single ImageServer export at {res_m} m")
    minx, miny, maxx, maxy = aoi_bounds4326
    params = urllib.parse.urlencode({
        "bbox": f"{minx},{miny},{maxx},{maxy}", "bboxSR": 4326, "imageSR": 5070,
        "size": f"{w_px},{h_px}", "format": "tiff", "pixelType": "F32",
        "noData": -9999, "interpolation": "RSP_BilinearInterpolation", "f": "image"})
    with urllib.request.urlopen(f"{_IMAGESERVER}?{params}", timeout=180) as r, \
            open(out_path, "wb") as f:
        shutil.copyfileobj(r, f)
    with rasterio.open(out_path) as ds:
        a = ds.read(1, masked=True)
        valid = int((~a.mask).sum()) if np.ma.isMaskedArray(a) else a.size
    if valid < 0.01 * w_px * h_px:
        raise RuntimeError(f"ImageServer returned no data at {res_m} m (collection gap)")
    return str(out_path)


def fetch_dem(domain_gdf_4326, out_path, resolution="auto", buffer_frac: float = BUFFER_FRAC):
    """Download the finest available USGS 3DEP DEM over the domain bbox (+buffer) and write it to
    `out_path` as a GeoTIFF. `resolution` is ``"auto"`` (finest 3DEP available — 1 m where lidar
    exists) or an explicit metre value (1/3/5/10/30). Resolutions whose request would exceed the
    3DEP pixel cap are skipped; each fine resolution is tried via py3dep first and then via the
    ImageServer directly (see _fetch_imageserver) before falling back to coarser data. Returns
    ``{"path", "resolution_m", "source"}``."""
    import geopandas as gpd

    minx, miny, maxx, maxy = (float(v) for v in domain_gdf_4326.total_bounds)
    dx, dy = (maxx - minx) * buffer_frac, (maxy - miny) * buffer_frac
    aoi = box(minx - dx, miny - dy, maxx + dx, maxy + dy)
    mb = gpd.GeoSeries([aoi], crs=4326).to_crs(5070).iloc[0].bounds       # metric size for the budget
    w_m, h_m = (mb[2] - mb[0]), (mb[3] - mb[1])

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    for res in _candidate_resolutions(aoi.bounds, resolution):
        if (w_m / res) * (h_m / res) > _MAX_PIXELS:
            errors.append((res, "exceeds the 3DEP pixel budget")); continue
        try:
            dem = py3dep.get_dem(aoi, res)
            if dem.rio.nodata is None:
                dem = dem.rio.write_nodata(-9999.0)
            dem.rio.to_raster(out_path)
            return {"path": str(out_path), "resolution_m": int(res), "source": "USGS 3DEP"}
        except Exception as e:  # noqa: BLE001
            errors.append((res, str(e)[:140]))
        if res <= 5:                       # dynamic-service resolutions: try urllib directly
            try:
                _fetch_imageserver(aoi.bounds, res, out_path, w_m, h_m)
                return {"path": str(out_path), "resolution_m": int(res),
                        "source": "USGS 3DEP"}
            except Exception as e:  # noqa: BLE001
                errors.append((res, "imageserver: " + str(e)[:120]))
        continue
    raise RuntimeError(f"3DEP DEM fetch failed at all candidate resolutions: {errors}")


def normalize_dem_source(raw) -> dict:
    """Restore hygiene for the persisted terrain-source record. Unknown modes fall back to
    the 3DEP download; path/mtime coerce to str/float or None. A missing source file is NOT
    dropped here: missing is a display state (warn card + relink), never a reason to lose
    the pointer."""
    rec = raw if isinstance(raw, dict) else {}
    mode = "local" if rec.get("mode") == "local" else "3dep"
    path = rec.get("path")
    path = str(path) if path else None
    try:
        mtime = float(rec["src_mtime"]) if rec.get("src_mtime") is not None else None
    except (TypeError, ValueError):
        mtime = None
    return {"mode": mode, "path": path, "src_mtime": mtime}


def _pixel_res_m(transform, crs, lat0: float) -> float:
    """Mean pixel size of (transform, crs) in metres. Geographic CRSes convert via the
    AOI-centre latitude; projected CRSes in non-metre units convert via the CRS factor."""
    import math

    dx, dy = abs(float(transform.a)), abs(float(transform.e))
    if getattr(crs, "is_geographic", False):
        mx = dx * 111320.0 * max(math.cos(math.radians(lat0)), 1e-6)
        my = dy * 110540.0
        return (mx + my) / 2.0
    try:
        factor = float(crs.linear_units_factor[1])
    except Exception:  # noqa: BLE001 — unknown linear unit: assume metres
        factor = 1.0
    return (dx + dy) / 2.0 * factor


def import_local_dem(src_path, domain_gdf_4326, out_path, *, reach_feat_4326=None,
                     buffer_frac: float = BUFFER_FRAC,
                     max_pixels: int | None = _MAX_PIXELS) -> dict:
    """Import a user GeoTIFF as the working DEM: clip it to the same reach-buffer AOI the
    3DEP fetch uses and write `out_path` as float32 / nodata -9999 in the SOURCE grid + CRS
    (verbatim pixel copy, no resampling; decimated only past `max_pixels`, and never when
    max_pixels is None — the app's desktop import path). Clipping here is
    what keeps the GW grid sane: the engine sizes the MODFLOW grid from the whole raster's
    bounds, so an unclipped county-wide DEM would explode the cell count. Validation failures
    raise DemImportError with a user-facing message; `reach_feat_4326` (a GeoJSON Feature)
    additionally requires elevation data along the centerline, which otherwise surfaces as
    NaN offsets deep inside boundary delineation. Returns ``{"path", "resolution_m",
    "source": "Local raster", "src_name", "note"}`` (`note` is a non-fatal coverage or
    decimation warning, else None)."""
    import math

    import numpy as np
    import rasterio
    from rasterio import windows
    from rasterio.warp import transform_bounds

    src_path = Path(src_path)
    if not src_path.is_file():
        raise DemImportError("The raster file was not found.")

    minx, miny, maxx, maxy = (float(v) for v in domain_gdf_4326.total_bounds)
    dx, dy = (maxx - minx) * buffer_frac, (maxy - miny) * buffer_frac
    aoi4326 = (minx - dx, miny - dy, maxx + dx, maxy + dy)
    lat0 = (miny + maxy) / 2.0

    try:
        src_ds = rasterio.open(src_path)
    except Exception as e:  # noqa: BLE001
        raise DemImportError(f"Couldn't read the raster: {e}") from e
    with src_ds as src:
        if src.crs is None:
            raise DemImportError("The raster has no projection information. Assign a "
                                 "coordinate system and try again.")
        if src.count != 1:
            raise DemImportError(f"The raster has {src.count} bands. The DEM must be a "
                                 "single band elevation raster.")
        try:
            aoi_src = transform_bounds("EPSG:4326", src.crs, *aoi4326, densify_pts=21)
        except Exception as e:  # noqa: BLE001
            raise DemImportError("Couldn't relate the raster's coordinate system to the "
                                 f"reach location: {e}") from e

        win = windows.from_bounds(*aoi_src, transform=src.transform)
        # floor the offsets + ceil the far edge = smallest whole-pixel window COVERING the
        # AOI (rasterio 1.5 dropped round_lengths(op="ceil"), which rounds to nearest now).
        col0, row0 = math.floor(win.col_off), math.floor(win.row_off)
        win = windows.Window(col0, row0,
                             math.ceil(win.col_off + win.width) - col0,
                             math.ceil(win.row_off + win.height) - row0)
        try:
            win = windows.intersection(win, windows.Window(0, 0, src.width, src.height))
        except Exception as e:  # rasterio WindowError: disjoint
            raise DemImportError("The raster does not cover the reach area.") from e
        w, h = int(win.width), int(win.height)
        if w < 1 or h < 1:
            raise DemImportError("The raster does not cover the reach area.")

        # The centerline must sit on real data, or delineation dies far less legibly later.
        if reach_feat_4326 is not None:
            from pyproj import Transformer
            from shapely.geometry import shape as _shape
            line = _shape(reach_feat_4326["geometry"])
            pts = [line.interpolate(t, normalized=True) for t in np.linspace(0.0, 1.0, 50)]
            tr = Transformer.from_crs("EPSG:4326", src.crs.to_wkt(), always_xy=True)
            xs, ys = tr.transform([p.x for p in pts], [p.y for p in pts])
            inb = []
            for x, y in zip(xs, ys):
                r, c = src.index(x, y)
                if 0 <= r < src.height and 0 <= c < src.width:
                    inb.append((x, y))
            nd = src.nodata
            good = sum(1 for v in (src.sample(inb) if inb else ())
                       if np.isfinite(float(v[0])) and (nd is None or float(v[0]) != nd))
            if good < 0.9 * len(pts):
                raise DemImportError("The raster has no elevation data along the reach "
                                     "centerline.")

        # Verbatim-grid read; integer-stride decimation only past the pixel budget.
        out_w, out_h, decimated = w, h, False
        if max_pixels and w * h > max_pixels:
            stride = math.ceil(math.sqrt((w * h) / max_pixels))
            out_w, out_h = math.ceil(w / stride), math.ceil(h / stride)
            decimated = True
        arr = src.read(1, window=win, out_shape=(out_h, out_w), masked=True)
        eff = src.window_transform(win) * rasterio.Affine.scale(w / out_w, h / out_h)
        data = np.ma.filled(arr.astype("float32"), np.float32(-9999.0))
        data = np.where(np.isfinite(data), data, np.float32(-9999.0)).astype("float32")
        if not (data != -9999.0).any():
            raise DemImportError("The raster has no valid elevation pixels over the "
                                 "reach area.")

        # Partial AOI coverage is legal (their DEM may simply be smaller) but worth a note.
        # windows.bounds can hand back swapped y edges on a south-up raster: normalize both
        # rectangles to (min, max) pairs before comparing.
        wb = windows.bounds(win, src.transform)
        tol = 2.0 * max(abs(src.transform.a), abs(src.transform.e))
        partial = (min(wb[0], wb[2]) > min(aoi_src[0], aoi_src[2]) + tol
                   or min(wb[1], wb[3]) > min(aoi_src[1], aoi_src[3]) + tol
                   or max(wb[0], wb[2]) < max(aoi_src[0], aoi_src[2]) - tol
                   or max(wb[1], wb[3]) < max(aoi_src[1], aoi_src[3]) - tol)
        res_m = round(_pixel_res_m(eff, src.crs, lat0), 2)
        out_crs = src.crs

    notes = []
    if partial:
        notes.append("The raster covers only part of the recommended terrain extent. "
                     "Boundary generation may be limited near the edges.")
    if decimated:
        notes.append(f"The raster was reduced to about {res_m:g} m for the working copy "
                     "to stay within the processing budget.")

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", driver="GTiff", height=out_h, width=out_w, count=1,
                       dtype="float32", crs=out_crs, transform=eff, nodata=-9999.0,
                       compress="deflate") as dst:
        dst.write(data, 1)
    return {"path": str(out_path), "resolution_m": res_m, "source": "Local raster",
            "src_name": src_path.name, "note": " ".join(notes) or None}


def clip_dem_to_polygon(dem_path, polygon_gdf, out_path, nodata: float = -9999.0):
    """Write a WSE raster = DEM elevations INSIDE the polygon, `nodata` everywhere else.

    The "draw the wetted extent" workflow: the user draws the water-surface extent and we hand
    the engine a WSE raster (DEM values inside the polygon) exactly as if it had been uploaded.
    Cells outside the polygon — and any non-finite DEM pixels inside it — become `nodata`
    (-9999, matching the engine's WSE `!= -9999` / `>= 0` filters) so they never become CHD
    cells (and never re-introduce a NaN). Returns the output path.
    """
    import numpy as np
    import rasterio
    from rasterio.features import geometry_mask

    with rasterio.open(dem_path) as src:
        arr = src.read(1).astype("float32")
        poly = polygon_gdf.to_crs(src.crs)
        inside = geometry_mask(
            [geom.__geo_interface__ for geom in poly.geometry],
            out_shape=arr.shape, transform=src.transform, invert=True,
        )
        out = np.where(inside & np.isfinite(arr), arr, np.float32(nodata)).astype("float32")
        meta = src.meta.copy()
    meta.update(driver="GTiff", dtype="float32", count=1, nodata=float(nodata))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **meta) as dst:
        dst.write(out, 1)
    return str(out_path)


def load_raster_4326(path, *, max_dim: int = 1024):
    """Reproject a single-band raster to EPSG:4326 and return (z, xs, ys, dx_m, dy_m): a 2-D
    float array (NaN at nodata), 1-D lon/lat cell centres (north-up), and the cell spacing in
    metres. Shared by the DEM hillshade and the result-raster overlays."""
    import math

    import numpy as np
    import rioxarray  # noqa: F401 — registers the .rio accessor

    da = rioxarray.open_rasterio(path, masked=True).squeeze().rio.reproject("EPSG:4326")
    z = np.asarray(da.values, dtype=float)
    xs = np.asarray(da["x"].values, dtype=float)
    ys = np.asarray(da["y"].values, dtype=float)
    if z.ndim != 2 or xs.size < 2 or ys.size < 2:
        raise ValueError("Unexpected raster shape for overlay.")
    if ys[0] < ys[-1]:                      # north-up: row 0 = northernmost (overlay's top edge)
        z = z[::-1, :]; ys = ys[::-1]
    step = max(1, math.ceil(max(z.shape) / max_dim))   # keep the data-URI small
    if step > 1:
        z = z[::step, ::step]; xs = xs[::step]; ys = ys[::step]
    lat0 = float((ys.min() + ys.max()) / 2)
    dx = abs(float(xs[1] - xs[0])) * 111320.0 * max(math.cos(math.radians(lat0)), 1e-6)
    dy = abs(float(ys[1] - ys[0])) * 110540.0
    return z, xs, ys, dx, dy


def rgba_to_overlay(rgba, xs, ys) -> dict:
    """Encode an RGBA float array (0..1) as a base64 PNG data URI + EPSG:4326 bounds for an
    ipyleaflet ImageOverlay: {"url": ..., "bounds": [[s, w], [n, e]]}."""
    import base64
    import io

    from matplotlib import image as mpimg

    buf = io.BytesIO()
    mpimg.imsave(buf, rgba, format="png")
    url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
    return {"url": url,
            "bounds": [[float(ys.min()), float(xs.min())], [float(ys.max()), float(xs.max())]]}


def dem_overlay(dem_path, *, max_dim: int = 1024, vert_exag: float = 2.0,
                vmin: float | None = None, vmax: float | None = None) -> dict:
    """Render the DEM as a hillshade + elevation-tint RGBA PNG (transparent at nodata) for an
    ipyleaflet ImageOverlay — lets the user toggle terrain vs. aerial while tracing the wetted
    extent. `vert_exag` scales the hillshade relief (0 disables shading → flat color tint);
    `vmin`/`vmax` pin the color stretch (e.g. to the current map view) — when omitted the
    2–98 % percentiles of the whole raster are used. Returns {"url": <base64 data URI>,
    "bounds": [[s, w], [n, e]], "vmin", "vmax"} in EPSG:4326."""
    import numpy as np
    from matplotlib import cm
    from matplotlib.colors import LightSource, Normalize

    z, xs, ys, dx, dy = load_raster_4326(dem_path, max_dim=max_dim)
    valid = np.isfinite(z)
    if not valid.any():
        raise ValueError("DEM has no valid pixels to render.")
    if vmin is None or vmax is None:
        vmin, vmax = (float(v) for v in np.nanpercentile(z[valid], [2, 98]))
    if not vmax > vmin:
        vmax = vmin + 1.0
    norm = Normalize(vmin=vmin, vmax=vmax)
    zfill = np.where(valid, z, vmin)
    if vert_exag > 0:
        ls = LightSource(azdeg=315, altdeg=45)
        rgba = ls.shade(zfill, cmap=cm.terrain, blend_mode="soft",
                        norm=norm, vert_exag=float(vert_exag), dx=dx, dy=dy)
    else:                                  # shading off: plain elevation tint
        rgba = cm.terrain(norm(np.clip(zfill, vmin, vmax)))
    rgba[..., 3] = valid.astype(float)     # transparent at nodata
    ov = rgba_to_overlay(rgba, xs, ys)
    ov.update(vmin=float(vmin), vmax=float(vmax))
    return ov


def stretch_for_bounds(dem_path, bounds_wsen) -> tuple[float, float] | None:
    """(vmin, vmax) = 2–98 % elevation percentiles INSIDE the given EPSG:4326 view bounds
    (west, south, east, north) — the "recalculate legend from the current view" stretch.
    None when the view contains no valid terrain."""
    import numpy as np

    z, xs, ys, _dx, _dy = load_raster_4326(dem_path)
    w, s, e, n = (float(v) for v in bounds_wsen)
    ix = (xs >= w) & (xs <= e)
    iy = (ys >= s) & (ys <= n)            # ys is north-up (descending); mask handles order
    if not ix.any() or not iy.any():
        return None
    sub = z[np.ix_(iy, ix)]
    sub = sub[np.isfinite(sub)]
    if sub.size < 4:
        return None
    lo, hi = (float(v) for v in np.percentile(sub, [2, 98]))
    if not hi > lo:
        hi = lo + 1.0
    return lo, hi


def dem_summary(out_path) -> dict:
    """Quick stats for the fetched DEM (for the UI)."""
    import rasterio
    import numpy as np
    with rasterio.open(out_path) as src:
        a = src.read(1, masked=True)
        return {
            "width": src.width, "height": src.height,
            "min": float(np.nanmin(a)), "max": float(np.nanmax(a)),
            "crs": str(src.crs),
        }
