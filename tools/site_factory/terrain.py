"""Terrain stage: produce the metric working DEM at inputs/dem.tif.

Two rules carried over from the app, both load-bearing:
  1. CLIP. The engine sizes the MODFLOW grid from the whole raster's bounds,
     so the working DEM is `import_local_dem`'s clip of the source to the
     domain bounds + 12 percent, never the raw site raster.
  2. METERS. The model runs length_units="meters" and `import_local_dem`
     copies pixel values verbatim, so a ftUS-vertical source must have its
     values scaled by 0.3048006096 after the clip (scaling the small clip,
     not the 200 MB source).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

FT_US = 0.3048006096012192


def _scale_to_meters(path: Path, factor: float = FT_US) -> None:
    import numpy as np
    import rasterio

    with rasterio.open(path) as src:
        profile = src.profile.copy()
        z = src.read(1)
        nodata = src.nodata
    if nodata is not None:
        valid = z != nodata
    else:
        valid = np.isfinite(z)
    z = z.astype("float32")
    z[valid] = z[valid] * factor
    profile.update(dtype="float32")
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(z, 1)


def mask_shallow_wse(wse_path: Path, depth_path: Path, out_path: Path,
                     min_depth_m: float = 0.05) -> dict:
    """Drop wetted-fringe WSE pixels shallower than `min_depth_m`.

    The GW engine consumes only the WSE raster's valid pixels, so masking the
    raster IS the sanctioned way to trim extent. At the wetted edge the RAS
    water surface can sit a few cm below the model grid's aggregated bed, and
    the engine's river CHDs carry no bottom guard (sides do), so MF6 aborts
    with CHD HEAD IS LESS THAN CELL BOTTOM. Observed deficits at LL01096 were
    3.2 and 4.2 cm on two of ~4000 CHD cells; a 5 cm depth floor removes the
    entire hazard class while costing a negligible ribbon of wetted area.
    """
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    with rasterio.open(wse_path) as w:
        wse = w.read(1)
        prof = w.profile.copy()
        nod = w.nodata if w.nodata is not None else -9999.0
        with rasterio.open(depth_path) as d:
            if (d.crs == w.crs and d.transform == w.transform and d.shape == w.shape):
                depth = d.read(1)
                dnod = d.nodata
            else:
                depth = np.full(wse.shape, np.nan, dtype="float64")
                reproject(source=rasterio.band(d, 1), destination=depth,
                          src_transform=d.transform, src_crs=d.crs,
                          dst_transform=w.transform, dst_crs=w.crs,
                          resampling=Resampling.bilinear,
                          src_nodata=d.nodata, dst_nodata=np.nan)
                dnod = None
    valid = wse != nod
    shallow = ~np.isfinite(depth) | (depth < min_depth_m)
    if dnod is not None:
        shallow |= depth == dnod
    n_before = int(valid.sum())
    wse = np.where(valid & shallow, nod, wse)
    n_after = int((wse != nod).sum())
    prof.update(nodata=nod)
    with rasterio.open(out_path, "w", **prof) as dst:
        dst.write(wse.astype(prof["dtype"]), 1)
    return {"path": str(out_path), "min_depth_m": min_depth_m,
            "px_before": n_before, "px_after": n_after,
            "px_dropped": n_before - n_after}


def stage_terrain(work_dir: Path, geom: dict, dem_source: str, vertical_units: str) -> dict:
    """Write inputs/dem.tif (metric, clipped) and report resolution + model origin."""
    import geopandas as gpd
    from shapely.geometry import shape

    from hype_app import delineate as dln
    from hype_app import dem as dem_mod

    work_dir = Path(work_dir)
    out = work_dir / "inputs" / "dem.tif"
    out.parent.mkdir(parents=True, exist_ok=True)
    domain_gdf = gpd.GeoDataFrame(geometry=[shape(geom["domain"]["geometry"])], crs=4326)

    if dem_source == "3dep":
        # Batch path: the app's own 3DEP fetch, domain bbox + its default buffer, meters.
        info = dem_mod.fetch_dem(domain_gdf, out)
    else:
        info = dem_mod.import_local_dem(
            dem_source, domain_gdf, out, reach_feat_4326=geom["reach"])
        if vertical_units and vertical_units.lower().startswith("ft"):
            _scale_to_meters(out)
            info["vertical"] = "scaled ftUS -> m"

    origin = dln.min_elevation_along_line(geom["up"], str(out))
    info["model_origin_elev"] = origin
    return info
