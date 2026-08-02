"""Build MODFLOW-grid geometry for the browser 3D mesh viewer — runs the REAL engine grid, NO vtk.

The Mesh tab's "Compute mesh" button calls :func:`build_grid_geometry` to turn the domain polygon
+ terrain DEM + (cell_size, model depth, layer thickness) into a **decimated** set of active
hexahedral cells (VTK_HEXAHEDRON) that ``www/mesh3d.js`` renders with vtk.js.

To match what the model actually builds, this reuses the engine's own discretization
(``hypetool.functions.my_utils.build_model_domain``): a **flat bottom** at ``bed = min(DEM)`` with
uniform layers of thickness ``z`` stacked down to a flat base, and only the **top** layer's top
stretched to the terrain (``tops[0] = DEM`` per cell, ``botm[0] = bed`` flat, ``botm[k] = bed − k·z``;
``nlay = int(depth / z)``). Each cell gets a **single, flat top and bottom elevation** — the mesh is
blocky/stair-stepped, never interpolated within a cell — so the preview is the real grid, not a
smooth terrain-following slab.

Points are emitted in **local metres** (SW corner = origin, z above the flat model base) so WebGL's
float32 coordinates stay precise; the client applies vertical exaggeration + clipping.
"""
from __future__ import annotations

import math
import os
import shutil
import tempfile
from types import SimpleNamespace

import numpy as np


def _reproject_dem_like_run(dem_path, crs, cell_size, out_tif):
    """Reproject the DEM to `crs` EXACTLY as the run does (``Settings.setup_terrain``:
    ``calculate_default_transform`` over the full DEM, nearest resampling), writing `out_tif`. The
    engine (`build_model_domain`) then derives a grid identical to the run's — same extent, alignment
    and cell centres — instead of the old domain-bbox+buffer approximation. Returns
    ``(xmin, ymax, ncol, nrow)`` of the grid the engine will build (ncol/nrow at `cell_size`) so the
    caller can enforce the cell cap before per-layer arrays are allocated."""
    import rasterio
    from rasterio.crs import CRS as RioCRS
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    dst_crs = RioCRS.from_user_input(crs)
    with rasterio.open(dem_path) as src:
        dst_transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds)
        meta = src.meta.copy()
        meta.update(crs=dst_crs, transform=dst_transform, width=width, height=height)
        with rasterio.open(out_tif, "w", **meta) as dst:
            reproject(source=rasterio.band(src, 1), destination=rasterio.band(dst, 1),
                      src_transform=src.transform, src_crs=src.crs,
                      dst_transform=dst_transform, dst_crs=dst_crs,
                      resampling=Resampling.nearest)
    xmin = float(dst_transform.c)
    ymax = float(dst_transform.f)
    xmax = xmin + width * float(dst_transform.a)
    ymin = ymax + height * float(dst_transform.e)          # dst_transform.e < 0 (north-up)
    ncol = max(1, int((xmax - xmin) / float(cell_size)))   # matches build_model_domain's int() grid
    nrow = max(1, int((ymax - ymin) / float(cell_size)))
    return xmin, ymax, ncol, nrow


BOUNDARY_STYLE = {                     # matches the app's 2-D map colors (app.py *_STYLE)
    "up": ("Upstream", "#f08c00"),
    "left": ("Left FPL", "#1f6feb"),
    "right": ("Right FPL", "#d83933"),
    "down": ("Downstream", "#9b59b6"),
}


def preview_cell_cap() -> int:
    """FULL-grid cell cap for the 3-D preview build. The engine discretization allocates
    per-layer float64 arrays for the whole (buffered) bbox, so an over-fine cell size can
    OOM the app process — refuse anything the run itself would refuse (same red band)."""
    from . import estimate
    return int(os.environ.get("HYPE_MESH_PREVIEW_MAX_CELLS", estimate.AMBER_MAX))


def build_grid_geometry(domain_feat, dem_path, crs, cell_size, depth, z, *,
                        origin: float | None = None,
                        sides: dict | None = None, want_basemap: bool = True,
                        max_cells: int = 40_000, max_layers: int = 30,
                        buffer_frac: float = 0.12, scene_z0: float | None = None,
                        log=print) -> dict:
    """Domain Feature (4326) + DEM + (cell_size, depth, z) → JSON-safe geometry for vtk.js:
    ``{points, cells, cellLayer, cellElev, elevRange, nHex, nPoints, dims, previewDims, decimation,
    layerStride, nActiveFull, bounds, boundaries, basemap}``. ``cellElev`` colours the top layer by
    real terrain elevation (deeper layers get a below-``elevRange`` sentinel → gray). ``cells`` is a
    flat list of 8 point-indices per hexahedron (the client
    adds the VTK cell-size/type framing). Runs the real engine grid (``build_model_domain``); each
    hexahedron has a flat per-cell top/bottom (blocky). Decimated so ``nHex ≤ max_cells``.

    ``sides`` (optional) = the four oriented boundary LineString Features (EPSG:4326,
    keys up/left/right/down) → per-side marker polylines along the top of the boundary's
    cells, for on-mesh orientation labels. ``want_basemap`` fetches a USGS aerial image
    over the preview extent for the client to drape on the top surface.
    """
    from hypetool.functions.my_utils import build_model_domain, make_idomain

    from .geometry import single_feature_gdf

    dom = single_feature_gdf(domain_feat).to_crs(crs)

    tmpdir = tempfile.mkdtemp(prefix="hype_mesh_")
    try:
        # Reproject the DEM EXACTLY as the run does so the engine derives a byte-identical grid.
        tmp_tif = os.path.join(tmpdir, "terrain_projcrs.tif")
        _gxmin, _gymax, gncol, gnrow = _reproject_dem_like_run(dem_path, crs, float(cell_size), tmp_tif)

        # hard safeguard BEFORE the engine allocates per-layer arrays (a too-fine cell size can OOM
        # the process; no point burning a core on a grid the run itself would refuse)
        nlay_est = max(1, math.ceil(float(depth) / float(z)))
        cap = preview_cell_cap()
        if gncol * gnrow * nlay_est > cap:
            need = float(cell_size) * math.sqrt(gncol * gnrow * nlay_est / cap)
            raise ValueError(
                f"Grid would be {gncol}×{gnrow}×{nlay_est} = {gncol * gnrow * nlay_est:,} cells — "
                f"over the {cap:,}-cell preview limit. Try a cell size of ~{need:.0f} m, a shallower "
                f"model, or thicker layers.")

        # --- run the REAL engine discretization (same code + origin as the run) ---
        cfg = SimpleNamespace(terrain_output_raster=tmp_tif,
                              cell_size_x=float(cell_size), cell_size_y=float(cell_size),
                              gw_mod_depth=float(depth), z=float(z),
                              model_origin_elev=(float(origin) if origin is not None else None))
        dm = build_model_domain(cfg)
        # --- active cells: the SAME idomain the run builds (domain intersect + above-ground) ---
        idomain, _grid_gdf = make_idomain(cfg, dom)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    tops, botm = dm["tops"], dm["botm"]                    # each a list of nlay (nrow, ncol) arrays
    idomain = np.asarray(idomain)
    inside = (idomain != 0).any(axis=0)
    if not inside.any():
        raise ValueError("No grid cells fall inside the domain.")
    log(f"[mesh] grid datum {float(dm['datum']):.2f} m"
        + (f" (origin {float(origin):.2f} m)" if origin is not None else " (min-DEM)"))

    return _emit_geometry(tops, botm, inside, idomain, float(cell_size),
                          float(dm["xmin"]), float(dm["ymin"]), crs,
                          sides=sides, want_basemap=want_basemap, max_cells=max_cells,
                          max_layers=max_layers, scene_z0=scene_z0, log=log)


def build_grid_geometry_from_run(gwf_ws, crs, *, sides: dict | None = None,
                                 want_basemap: bool = True, max_cells: int = 40_000,
                                 max_layers: int = 30, scene_z0: float | None = None,
                                 log=print) -> dict:
    """The SAME payload as :func:`build_grid_geometry`, but read from a COMPLETED run's binary
    grid file instead of a pre-run estimate. The preview build derives its flat bed from
    ``min(DEM)`` over its own buffered bbox, which can sit metres above the run's actual bed
    (different raster crop / knobs) — so once a run exists, the 3-D "Model grid" must show the
    run's real DIS or zone volumes (built from the run) hang below it."""
    import warnings
    from pathlib import Path

    from flopy.mf6.utils import MfGrdFile

    grb_path = next(Path(gwf_ws).glob("*.dis.grb"), None)
    if grb_path is None:
        raise ValueError(f"No .dis.grb binary grid file in {gwf_ws}")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")                    # flopy shape-set deprecation noise
        mg = MfGrdFile(str(grb_path), verbose=False).modelgrid
        top2d = np.asarray(mg.top, dtype=float)
        botm3d = np.asarray(mg.botm, dtype=float)
        idom = np.asarray(mg.idomain).reshape(botm3d.shape)
        delr = np.asarray(mg.delr, dtype=float)
        x_anchor, y_anchor = float(mg.xoffset), float(mg.yoffset)
    nlay = botm3d.shape[0]
    # engine arrays are SOUTH-first (row 0 = southernmost) — exactly what _emit_geometry's
    # local frame expects (cell [R, C] at (C·delr, R·delr) from the SW anchor)
    tops = [top2d] + [botm3d[k] for k in range(nlay - 1)]
    botm = [botm3d[k] for k in range(nlay)]
    inside = (idom != 0).any(axis=0)
    if not inside.any():
        raise ValueError("The run's grid has no active cells.")
    log(f"[mesh] rebuilding the 3-D grid from the run: {grb_path.name}")
    return _emit_geometry(tops, botm, inside, idom, float(delr[0]), x_anchor, y_anchor, crs,
                          sides=sides, want_basemap=want_basemap, max_cells=max_cells,
                          max_layers=max_layers, scene_z0=scene_z0, log=log)


def _emit_geometry(tops, botm, inside, idomain3d, cell_size: float, x_anchor: float, y_anchor: float,
                   crs, *, sides, want_basemap, max_cells, max_layers, scene_z0, log) -> dict:
    """tops/botm (lists of nlay (nrow, ncol) arrays, SOUTH-first rows) + active mask →
    the decimated hexahedra payload for www/mesh3d.js (see build_grid_geometry docstring).

    ``idomain3d`` (nlay, nrow, ncol) is the run's activity mask: cells switched off (above-ground
    or outside the domain) are skipped per layer, so the preview draws the SAME active set the run
    solves — including the terrain-clipped downstream columns."""
    nlay = len(botm)
    nrow, ncol = inside.shape
    n_active2d = int(inside.sum())

    # --- decimate to the budget: layer stride lf, then row/col stride f ---
    lf = max(1, math.ceil(nlay / max_layers))
    nlay_d = max(1, math.ceil(nlay / lf))
    f = 1
    while f < max(nrow, ncol) and int(inside[::f, ::f].sum()) * nlay_d > max_cells:
        f += 1
    inside_d = inside[::f, ::f]
    # per-preview-layer activity at the merge's TOP real layer (kt = s*lf), decimated to (nlay_d, …)
    idom_d = np.asarray(idomain3d)[::lf, ::f, ::f] if idomain3d is not None else None
    nrow_d, ncol_d = inside_d.shape
    delr_d = float(cell_size) * f

    # --- emit blocky hexahedra: one flat top (tops[s]) + flat bottom (botm[s]) per cell, no interp ---
    # z datum: the shared scene z0 when the caller provides one (all 3D layers — terrain,
    # drapes, flow paths — must use ONE datum or vertical exaggeration breaks alignment);
    # else the flat model base, as before.
    if scene_z0 is not None:
        z_ref = float(scene_z0)
    else:
        z_ref = float(np.nanmin(botm[nlay - 1]))           # flat model base (the deepest, uniform bottom)
    tvals = np.asarray(tops[0])[inside]                    # real terrain elevations over active cells
    elev_lo, elev_hi = float(np.nanmin(tvals)), float(np.nanmax(tvals))
    if elev_hi - elev_lo < 1e-6:                           # flat terrain → give the legend a usable span
        elev_hi = elev_lo + 1.0
    sentinel = elev_lo - 1000.0                            # deeper layers → below-range → gray body in the viewer
    points: list = []
    cells: list = []
    cell_layer: list = []
    cell_top: list = []                                    # 1 = this column's shallowest DRAWN cell
    cell_elev: list = []                                   # per-hex colour scalar: top cell = terrain elev, else sentinel
    zt_max = 0.0
    top0_d = np.asarray(tops[0])[::f, ::f]                 # true terrain (ground) per decimated column
    topped = np.zeros((nrow_d, ncol_d), dtype=bool)        # column already has its top cell emitted
    for s in range(nlay_d):
        kt = s * lf                                        # merged preview layer s spans real layers kt..kb
        kb = min((s + 1) * lf, nlay) - 1
        top_a = np.asarray(tops[kt])[::f, ::f]
        bot_a = np.asarray(botm[kb])[::f, ::f]
        for R in range(nrow_d):
            y0, y1 = R * delr_d, (R + 1) * delr_d
            for C in range(ncol_d):
                if not inside_d[R, C]:
                    continue
                if idom_d is not None and idom_d[s, R, C] == 0:   # above-ground / inactive → skip
                    continue
                zt = float(top_a[R, C]) - z_ref
                zb = float(bot_a[R, C]) - z_ref
                if zt - zb <= 1e-6:                        # skip zero-thickness cells (e.g. top layer at min-DEM)
                    continue
                x0, x1 = C * delr_d, (C + 1) * delr_d
                b = len(points) // 3
                points.extend((x0, y0, zb, x1, y0, zb, x1, y1, zb, x0, y1, zb,     # bottom face 0..3
                               x0, y0, zt, x1, y0, zt, x1, y1, zt, x0, y1, zt))    # top face 4..7
                cells.extend((b, b + 1, b + 2, b + 3, b + 4, b + 5, b + 6, b + 7))
                # The visible TOP surface is each column's shallowest DRAWN cell — layer 0 upstream,
                # but a deeper layer downstream where above-ground layer-0 cells are switched off.
                # It carries the terrain colour and the aerial drape; deeper cells stay gray body.
                is_top = not topped[R, C]
                topped[R, C] = True
                cell_layer.append(s)
                cell_top.append(1 if is_top else 0)
                cell_elev.append(float(top0_d[R, C]) if is_top else sentinel)
                if zt > zt_max:
                    zt_max = zt

    n_hex = len(cell_layer)
    log(f"[mesh] engine grid {ncol}x{nrow}x{nlay}; preview x{f} (layers /{lf}) -> "
        f"{n_hex} hexes, {len(points) // 3} points")

    # Local-coordinate anchor: cell [R, C] sits at local (C·delr, R·delr) with row 0 = SOUTH
    # (the engine's rows run ymin→ymax), i.e. local = (x − x_anchor, y − y_anchor). top0_d above.
    boundaries = _boundary_markers(sides, crs, x_anchor, y_anchor, delr_d, inside_d,
                                   top0_d, z_ref) if sides else []
    basemap = basemap_topo = None
    if want_basemap:
        basemap = _fetch_basemap(crs, x_anchor, y_anchor,
                                 float(ncol_d * delr_d), float(nrow_d * delr_d), log=log)
        basemap_topo = _fetch_basemap(crs, x_anchor, y_anchor,
                                      float(ncol_d * delr_d), float(nrow_d * delr_d),
                                      service="USGSTopo", log=log)

    return {
        "points": points, "cells": cells, "cellLayer": cell_layer,
        "cellTop": cell_top, "cellElev": cell_elev, "elevRange": [elev_lo, elev_hi],
        "nHex": n_hex, "nPoints": len(points) // 3,
        "dims": {"nlay": nlay, "nrow": nrow, "ncol": ncol},
        "previewDims": {"nlay": nlay_d, "nrow": nrow_d, "ncol": ncol_d},
        "decimation": f, "layerStride": lf, "nActiveFull": n_active2d * nlay,
        "bounds": [0.0, float(ncol_d * delr_d), 0.0, float(nrow_d * delr_d), 0.0, float(zt_max)],
        "boundaries": boundaries, "basemap": basemap, "basemapTopo": basemap_topo,
        "origin": [x_anchor, y_anchor],    # local-frame anchor in the model CRS (scene align)
        "z0": z_ref,                       # the z datum geometry is relative to
    }


def _boundary_markers(sides, crs, x_anchor, y_anchor, delr_d, inside_d, top0_d, z_ref,
                      lift: float = 0.6) -> list:
    """Per-boundary marker polylines along the TOP of the boundary's preview cells.

    Each of the four oriented sides (EPSG:4326 LineStrings) is sampled at sub-cell spacing;
    every sample maps to its decimated preview cell (nearest active cell within 2 cells),
    and consecutive distinct cells become a polyline through the cell-top centres (z lifted
    slightly so the line never z-fights the top faces). Coordinates are preview-local.
    """
    from pyproj import Transformer
    from shapely.geometry import shape

    nrow_d, ncol_d = inside_d.shape
    tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
    out = []
    for key, (name, color) in BOUNDARY_STYLE.items():
        feat = (sides or {}).get(key)
        if not feat:
            continue
        try:
            line = shape(feat["geometry"])
            len_m = line.length * 111_000.0              # degrees → rough metres (sampling only)
            n_samp = int(np.clip(len_m / (delr_d * 0.75), 8, 400))
            pts4326 = [line.interpolate(i / (n_samp - 1), normalized=True) for i in range(n_samp)]
            xs, ys = tr.transform([p.x for p in pts4326], [p.y for p in pts4326])
        except Exception:  # noqa: BLE001 — a malformed side just loses its marker
            continue
        path, last_rc = [], None
        for x, y in zip(xs, ys):
            cd = int((x - x_anchor) // delr_d)
            rd = int((y - y_anchor) // delr_d)
            best = None
            for dr in range(-2, 3):                      # nearest ACTIVE preview cell (≤ 2 cells off)
                for dc in range(-2, 3):
                    r2, c2 = rd + dr, cd + dc
                    if 0 <= r2 < nrow_d and 0 <= c2 < ncol_d and inside_d[r2, c2]:
                        d2 = dr * dr + dc * dc
                        if best is None or d2 < best[0]:
                            best = (d2, r2, c2)
            if best is None or (best[1], best[2]) == last_rc:
                continue
            _, r2, c2 = best
            last_rc = (r2, c2)
            zt = float(top0_d[r2, c2]) - z_ref
            if not np.isfinite(zt):
                continue
            path.extend(((c2 + 0.5) * delr_d, (r2 + 0.5) * delr_d, zt + lift))
        if len(path) >= 6:                               # at least 2 points
            out.append({"key": key, "name": name, "color": color, "points": path})
    return out


def fetch_basemap_image(crs, x0, y0, x1, y1, *, service: str = "USGSImageryOnly",
                        fmt: str = "jpg", max_px: int = 1024, timeout_s: float = 30.0,
                        log=print):
    """A USGS basemap export over an absolute model-CRS bbox as raw image bytes:
    {"data": <bytes>, "extent": (x0, x1, y0, y1)} with the image's top row at the NORTH
    edge (y1). `service` picks the ArcGIS service (USGSImageryOnly for the aerial,
    USGSTopo for the topo); `fmt` picks the export format ("jpg" for photographic,
    "png" for linework). None on any failure — basemaps are a nice-to-have."""
    import urllib.parse
    import urllib.request

    from pyproj import CRS

    try:
        epsg = CRS.from_user_input(crs).to_epsg()
        if epsg is None:
            return None
        width_m, height_m = float(x1) - float(x0), float(y1) - float(y0)
        if width_m <= 0 or height_m <= 0:
            return None
        aspect = height_m / width_m
        if aspect <= 1.0:
            w_px, h_px = max_px, max(64, int(round(max_px * aspect)))
        else:
            w_px, h_px = max(64, int(round(max_px / aspect))), max_px
        params = urllib.parse.urlencode({
            "bbox": f"{x0},{y0},{x1},{y1}",
            "bboxSR": epsg, "imageSR": epsg, "size": f"{w_px},{h_px}",
            "format": fmt, "transparent": "false", "f": "image"})
        url = (f"https://basemap.nationalmap.gov/arcgis/rest/services/{service}/"
               "MapServer/export?" + params)
        with urllib.request.urlopen(url, timeout=timeout_s) as r:
            data = r.read()
        if not data or len(data) < 1000:                 # error page / empty tile
            return None
        log(f"[mesh] {service} basemap fetched ({len(data) // 1024} KB, {w_px}x{h_px} px)")
        return {"data": data, "extent": (float(x0), float(x1), float(y0), float(y1))}
    except Exception as e:  # noqa: BLE001
        log(f"[mesh] {service} basemap unavailable: {e}")
        return None


def _fetch_basemap(crs, x_anchor, y_anchor, width_m, height_m, *, max_px: int = 1024,
                   timeout_s: float = 30.0, service: str = "USGSImageryOnly", log=print):
    """The 3-D drape packaging of fetch_basemap_image: {"url", "x0", "y0", "x1", "y1"} with a
    base64 JPEG data URI and the extent in preview-LOCAL metres (y0 = south edge; the image's
    top row is the NORTH edge). None on any failure — drapes are a nice-to-have."""
    import base64

    fetched = fetch_basemap_image(crs, x_anchor, y_anchor, x_anchor + width_m,
                                  y_anchor + height_m, service=service, fmt="jpg",
                                  max_px=max_px, timeout_s=timeout_s, log=log)
    if not fetched:
        return None
    return {"url": "data:image/jpeg;base64," + base64.b64encode(fetched["data"]).decode("ascii"),
            "x0": 0.0, "y0": 0.0, "x1": float(width_m), "y1": float(height_m)}


def child_build(payload: dict, q) -> None:
    """Run the preview build in a spawned child process (crash/OOM isolation + hard cancel):
    puts ('log', line)… then ('result', geometry) or ('error', message) on `q`. Top-level and
    picklable for the 'spawn' start method."""
    try:
        z0 = payload.get("scene_z0")
        z0 = float(z0) if z0 is not None else None
        log = lambda m: q.put(("log", str(m)))  # noqa: E731
        if payload.get("run_ws"):               # completed run → the REAL grid, not the estimate
            g = build_grid_geometry_from_run(
                payload["run_ws"], payload["crs"], sides=payload.get("sides"),
                want_basemap=payload.get("want_basemap", True), scene_z0=z0, log=log)
        else:
            _origin = payload.get("origin")
            g = build_grid_geometry(
                payload["domain"], payload["dem"], payload["crs"],
                float(payload["cell_size"]), float(payload["depth"]), float(payload["z"]),
                origin=(float(_origin) if _origin is not None else None),
                sides=payload.get("sides"), want_basemap=payload.get("want_basemap", True),
                scene_z0=z0, log=log,
            )
        q.put(("result", g))
    except Exception as e:  # noqa: BLE001
        q.put(("error", str(e)))
