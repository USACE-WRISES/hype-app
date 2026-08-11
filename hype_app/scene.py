"""3-D scene payload builders — terrain surface, flow-path polylines, raster drapes.

Every payload shares ONE scene frame: xy in metres relative to a caller-supplied `origin`
(absolute model-CRS coordinates), z in metres above a caller-supplied `z0` datum. The SAME
z0 is passed to the mesh preview build (hype_app/mesh.py `scene_z0`), which is what keeps
vertical exaggeration from tearing the layers apart — exaggeration scales z about 0, so all
layers must measure z from the same zero.

Consumed client-side by www/mesh3d.js via the `hype3d_layer` custom message
({key, kind: terrain|lines3d|drape|volume, data}).
"""
from __future__ import annotations

import numpy as np


def terrain_payload(dem_path: str, crs, origin, z0: float, *, max_dim: int = 200) -> dict:
    """Decimated DEM surface on a regular grid in `crs`. Row 0 = SOUTH (the client builds
    quads bottom-up); nodata cells are None and the client skips their quads."""
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import calculate_default_transform, reproject

    with rasterio.open(dem_path) as src:
        t, w, h = calculate_default_transform(src.crs, crs, src.width, src.height, *src.bounds)
        scale = max(w / max_dim, h / max_dim, 1.0)
        w2, h2 = max(2, int(w / scale)), max(2, int(h / scale))
        t2 = rasterio.Affine(t.a * (w / w2), t.b, t.c, t.d, t.e * (h / h2), t.f)
        dst = np.full((h2, w2), np.nan, dtype="float32")
        reproject(rasterio.band(src, 1), dst, dst_transform=t2, dst_crs=crs,
                  dst_nodata=np.nan, src_nodata=src.nodata, resampling=Resampling.bilinear)
    dx = float(t2.a)
    dy = float(-t2.e)                        # t2.e < 0 (north-up raster)
    x0 = float(t2.c) + dx / 2.0              # west column center
    y0 = float(t2.f) + float(t2.e) * (h2 - 0.5)   # SOUTH row center
    grid = np.flipud(dst)                    # row 0 = south, matching y0 + j*dy
    zlo, zhi = float(np.nanmin(grid)), float(np.nanmax(grid))
    rel = grid.astype("float64") - float(z0)
    z = [None if not np.isfinite(v) else round(float(v), 3) for v in rel.ravel()]
    return {"key": "terrain", "kind": "terrain",
            "data": {"nx": int(w2), "ny": int(h2),
                     "x0": x0 - float(origin[0]), "y0": y0 - float(origin[1]),
                     "dx": dx, "dy": dy, "z": z,
                     "zRange": [zlo - float(z0), zhi - float(z0)],
                     "origin": [float(origin[0]), float(origin[1])]}}


def flowpaths_payload(gdf_4326, crs, origin, z0: float, *, max_paths: int = 800,
                      color: str = "#0a3d91", key: str = "paths",
                      width: float = 2, opacity: float = 1.0) -> dict | None:
    """3-D polylines from a pathlines GeoDataFrame (any CRS set on it; reprojected to
    `crs`). Returns None when the geometries carry no z — flat lines pinned to the
    datum would only mislead."""
    if gdf_4326 is None or not len(gdf_4326):
        return None
    g = gdf_4326.to_crs(crs)
    if len(g) > max_paths:
        g = g.iloc[:max_paths]
    ox, oy = float(origin[0]), float(origin[1])
    # Per-path residence time + particle id ride ALIGNED with polylines so the
    # client can animate particles along the 3-D lines with the same relative
    # speeds as the 2-D animator. Optional: absent columns just omit the keys.
    tds = g["total_time_d"].tolist() if "total_time_d" in g.columns else None
    pids = g["particleid"].tolist() if "particleid" in g.columns else None
    polylines = []
    times: list[float] = []
    part_ids: list[int] = []
    have_z = False
    for i, geom in enumerate(g.geometry):
        if geom is None or geom.is_empty or geom.geom_type != "LineString":
            continue
        flat = []
        for c in geom.coords:
            zv = float(c[2]) if len(c) > 2 else float(z0)
            if len(c) > 2:
                have_z = True
            flat.extend((round(float(c[0]) - ox, 3), round(float(c[1]) - oy, 3),
                         round(zv - float(z0), 3)))
        if len(flat) >= 6:
            polylines.append(flat)
            if tds is not None:
                try:
                    times.append(round(float(tds[i]), 6))
                except (TypeError, ValueError):
                    times.append(0.0)
            if pids is not None:
                try:
                    part_ids.append(int(pids[i]))
                except (TypeError, ValueError):
                    part_ids.append(i + 1)
    if not polylines or not have_z:
        return None
    data = {"polylines": polylines, "color": color,
            "width": max(1, int(round(float(width)))),
            "opacity": max(0.0, min(float(opacity), 1.0)),
            "origin": [ox, oy]}
    if tds is not None:
        data["times"] = times
    if pids is not None:
        data["pids"] = part_ids
    return {"key": key, "kind": "lines3d", "data": data}


def flowpaths_payload_from_dir(work_dir, crs, origin, z0: float, **kw) -> dict | None:
    """Load the engine's 3-D pathlines shapefile (absolute z) from the run outputs — the
    display GeoDataFrame is 2-D, so the z must come from here. Prefers the filtered
    hyporheic set (what the 2-D map shows); falls back to the full set."""
    import glob
    import os

    import geopandas as gpd

    for pat in ("Forward_hyporheic_pathlines_3D*.shp", "Forward_full_pathlines_3D*.shp"):
        hits = (glob.glob(os.path.join(str(work_dir), "summary", pat))
                or glob.glob(os.path.join(str(work_dir), "**", pat), recursive=True))
        if hits:
            try:
                g = gpd.read_file(hits[0])
            except Exception:  # noqa: BLE001
                continue
            p = flowpaths_payload(g, crs, origin, z0, **kw)
            if p is not None:
                return p
    return None


def volume_payload(key: str, points, quads, origin, z0: float, *,
                   color: str = "#0d9488", opacity: float = 0.35) -> dict | None:
    """Closed exterior shell of a marked cell set as a translucent 3-D volume.

    `points` is (N,3) float with ABSOLUTE model-CRS x/y and ABSOLUTE z (metres);
    `quads` is (M,4) int corner indices. Rebases xy to `origin` and z to `z0`, and
    emits vtk count-prefixed quad connectivity ([4,a,b,c,d, ...] — the same convention
    the terrain grid uses client-side).
    """
    pts = np.asarray(points, dtype=np.float64)
    qds = np.asarray(quads, dtype=np.int64)
    if pts.size == 0 or qds.size == 0:
        return None
    flat = np.empty(pts.shape[0] * 3, dtype=np.float64)
    flat[0::3] = np.round(pts[:, 0] - float(origin[0]), 3)
    flat[1::3] = np.round(pts[:, 1] - float(origin[1]), 3)
    flat[2::3] = np.round(pts[:, 2] - float(z0), 3)
    polys = np.empty((qds.shape[0], 5), dtype=np.int64)
    polys[:, 0] = 4
    polys[:, 1:] = qds
    return {"key": key, "kind": "volume",
            "data": {"points": flat.tolist(), "polys": polys.ravel().tolist(),
                     "color": color, "opacity": float(opacity),
                     "origin": [float(origin[0]), float(origin[1])]}}


def drape_payload(key: str, overlay: dict | None, crs, origin, *, lift: float = 0.35,
                  opacity: float = 0.85) -> dict | None:
    """A 2-D map-overlay payload ({url, bounds [[s,w],[n,e]]}, EPSG:4326 PNG) as a texture
    draped on the 3-D terrain. The PNG is 4326-axis-aligned; at reach scale the skew versus
    the metric frame is visually negligible (corners map exactly)."""
    if not overlay or not overlay.get("url"):
        return None
    from pyproj import Transformer

    (s, w), (n, e) = overlay["bounds"]
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    x0, y0 = tr.transform(float(w), float(s))
    x1, y1 = tr.transform(float(e), float(n))
    ox, oy = float(origin[0]), float(origin[1])
    return {"key": key, "kind": "drape",
            "data": {"url": overlay["url"], "x0": x0 - ox, "y0": y0 - oy,
                     "x1": x1 - ox, "y1": y1 - oy, "lift": float(lift),
                     "opacity": float(opacity),
                     "origin": [ox, oy]}}
