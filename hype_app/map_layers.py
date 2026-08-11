"""User reference map layers: path-pointer records + display payload builders.

A map layer is a LINK to a file on the user's machine (raster: GeoTIFF/VRT, vector:
Shapefile/GeoJSON) — the project stores only the path, never a copy, so a missing
file is a normal state the UI must surface (warning row), not an error that drops
the record. Everything here is pure (no Shiny, no widget imports) so record hygiene
and the load paths stay unit-testable.

Display contract: reference layers draw in the dedicated "hype-ref" Leaflet pane
(declared once in app._build_map; zIndex between the terrain pane and the default
overlay pane), so they always sit above the basemap and terrain hillshade but below
every app-generated overlay, no matter what order the heal machinery re-adds layers.
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

#: Leaflet pane names — declared in app._build_map's Map(panes=...) CONSTRUCTOR call
#: (mutating Map.panes later makes the client re-render the whole map).
PANE_TERRAIN = "hype-terrain"
PANE_REF = "hype-ref"

RASTER_SUFFIXES = {".tif", ".tiff", ".vrt"}
VECTOR_SUFFIXES = {".shp", ".geojson", ".json"}

DEFAULT_OPACITY = 0.8
DEFAULT_COLOR = "#e11d48"

_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}")


def classify_path(path) -> str | None:
    """"raster" | "vector" from the file suffix, None for anything unsupported."""
    sfx = Path(str(path)).suffix.lower()
    if sfx in RASTER_SUFFIXES:
        return "raster"
    if sfx in VECTOR_SUFFIXES:
        return "vector"
    return None


def default_name(path) -> str:
    return Path(str(path)).stem or "layer"


def new_layer_record(path) -> dict:
    """Fresh record for a just-picked file. Callers filter unsupported suffixes first."""
    p = str(path)
    return {"id": uuid.uuid4().hex[:8], "path": p, "name": default_name(p),
            "kind": classify_path(p) or "vector",
            "opacity": DEFAULT_OPACITY, "color": DEFAULT_COLOR, "visible": True}


def _num(v, lo: float, hi: float, fallback: float) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return fallback
    if f != f:                                          # NaN
        return fallback
    return min(hi, max(lo, f))


def normalize_map_layers(raw) -> list[dict]:
    """Restore hygiene for saved layer records (wells.normalize_wells idiom): mint
    missing ids, drop pathless or unsupported-suffix rows, re-derive kind from the
    suffix, clamp opacity, validate the color, coerce visible. A record whose file is
    MISSING on disk is kept — missing is a display state, never grounds for dropping
    the user's link."""
    out, seen = [], set()
    for r in raw or []:
        if not isinstance(r, dict):
            continue
        path = str(r.get("path") or "").strip()
        kind = classify_path(path)
        if not path or kind is None:
            continue
        uid = str(r.get("id") or "").strip() or uuid.uuid4().hex[:8]
        if uid in seen:
            continue
        seen.add(uid)
        color = str(r.get("color") or "").strip()
        out.append({"id": uid, "path": path,
                    "name": str(r.get("name") or "").strip() or default_name(path),
                    "kind": kind,
                    "opacity": _num(r.get("opacity"), 0.0, 1.0, DEFAULT_OPACITY),
                    "color": color if _HEX_RE.fullmatch(color) else DEFAULT_COLOR,
                    "visible": bool(r.get("visible", True))})
    return out


def vector_style(color: str, opacity: float) -> dict:
    """Uniform layer-level GeoJSON style: linework in the chosen color plus a faint
    same-color fill (~25% of the layer opacity). Layer-level style= is correct here
    because the styling is uniform (per-feature properties.style is only for layers
    whose features differ)."""
    op = min(1.0, max(0.0, float(opacity)))
    return {"color": color, "weight": 2, "opacity": op,
            "fillColor": color, "fillOpacity": round(op * 0.25, 3)}


def vector_point_style() -> dict:
    """CircleMarker options for point features. point_style is CONSTRUCTION-ONLY on
    the client (the pointToLayer closure captures it), so only the radius and the
    pane live here; color/opacity ride the live-mutable style trait instead."""
    return {"radius": 5, "pane": PANE_REF}


def _vertex_count(geoms) -> int:
    from shapely import get_coordinates
    return int(get_coordinates(geoms).shape[0])


def load_vector_fc(path, *, max_features: int = 8000, max_vertices: int = 250_000):
    """Read a vector file into an EPSG:4326 FeatureCollection dict for an ipyleaflet
    GeoJSON layer. Returns (fc, bounds, err, simplified): bounds is [[s, w], [n, e]];
    err is a short user-facing reason (fc/bounds None) when the file can't be shown;
    simplified is True when the geometry was thinned to fit the display budget.

    Attributes are deliberately dropped: reference layers are display-only, and
    properties can hold non-JSON types (timestamps) that would break the widget
    payload. The whole FeatureCollection ships over the websocket as widget state,
    hence the feature/vertex budgets."""
    import geopandas as gpd

    p = Path(str(path))
    try:
        gdf = gpd.read_file(p)
    except Exception as e:  # noqa: BLE001 — any read failure is a per-layer reason
        return None, None, f"could not read the file ({e})", False
    if gdf.crs is None:
        # Bare GeoJSON is EPSG:4326 by spec; a shapefile without its .prj is unknowable.
        if p.suffix.lower() in (".geojson", ".json"):
            gdf = gdf.set_crs(4326)
        else:
            return None, None, "no projection info (.prj is missing)", False
    try:
        gdf = gdf.to_crs(4326)
    except Exception as e:  # noqa: BLE001
        return None, None, f"could not reproject ({e})", False
    gdf = gdf[~(gdf.geometry.isna() | gdf.geometry.is_empty)]
    if len(gdf) == 0:
        return None, None, "no drawable features", False
    if len(gdf) > max_features:
        return None, None, f"too many features to display ({len(gdf):,}; limit {max_features:,})", False

    simplified = False
    nv = _vertex_count(gdf.geometry.values)
    tol = 1e-5                                          # ~1 m in degrees; escalate x4
    while nv > max_vertices and tol <= 0.02:
        gdf = gdf.assign(geometry=gdf.geometry.simplify(tol, preserve_topology=True))
        gdf = gdf[~(gdf.geometry.isna() | gdf.geometry.is_empty)]
        nv = _vertex_count(gdf.geometry.values)
        simplified = True
        tol *= 4
    if nv > max_vertices:
        return None, None, "too detailed to display, even after simplifying", False

    feats = [{"type": "Feature", "properties": {}, "geometry": g.__geo_interface__}
             for g in gdf.geometry]
    fc = {"type": "FeatureCollection", "features": feats}
    w, s, e, n = (float(v) for v in gdf.total_bounds)
    return fc, [[s, w], [n, e]], None, simplified


def _scale01(band, valid):
    """Band values -> 0..1 for display: already-unit data passes through, 8-bit style
    divides by 255, anything wider gets a 2-98 percentile stretch."""
    import numpy as np

    vals = band[valid]
    if vals.size == 0:
        return np.zeros_like(band)
    mx = float(np.nanmax(vals))
    if mx <= 1.0:
        return np.clip(band, 0.0, 1.0)
    if mx <= 255.0:
        return np.clip(band / 255.0, 0.0, 1.0)
    lo, hi = (float(v) for v in np.nanpercentile(vals, [2, 98]))
    if not hi > lo:
        hi = lo + 1.0
    return np.clip((band - lo) / (hi - lo), 0.0, 1.0)


def _rgb_overlay(path, *, max_dim: int, band4_is_alpha: bool = False) -> dict:
    """3-4 band raster -> RGBA overlay payload. Mirrors dem.load_raster_4326's warp
    (reproject to 4326, north-up, stride decimate) but keeps the band axis.

    band4_is_alpha comes from the file's colorinterp tags: a 4th band is only an
    alpha channel when the file SAYS so. NAIP's 4th band is near-infrared
    (colorinterp "undefined"), and treating it as alpha rendered whole aerials
    half-transparent with water nearly invisible."""
    import math

    import numpy as np
    import rioxarray  # noqa: F401 — registers the .rio accessor

    from . import dem

    da = rioxarray.open_rasterio(path, masked=True).rio.reproject("EPSG:4326")
    arr = np.asarray(da.values, dtype=float)            # (band, y, x)
    xs = np.asarray(da["x"].values, dtype=float)
    ys = np.asarray(da["y"].values, dtype=float)
    if arr.ndim != 3 or arr.shape[0] < 3 or xs.size < 2 or ys.size < 2:
        raise ValueError("Unexpected raster shape for overlay.")
    if ys[0] < ys[-1]:                                  # north-up: row 0 = top edge
        arr = arr[:, ::-1, :]; ys = ys[::-1]
    step = max(1, math.ceil(max(arr.shape[1:]) / max_dim))
    if step > 1:
        arr = arr[:, ::step, ::step]; xs = xs[::step]; ys = ys[::step]

    rgb = arr[:3]
    valid = np.isfinite(rgb).all(axis=0)
    if not valid.any():
        raise ValueError("No valid pixels to display.")
    chans = [_scale01(np.where(valid, b, 0.0), valid) for b in rgb]
    alpha = valid.astype(float)
    if band4_is_alpha and arr.shape[0] >= 4:
        a = np.where(np.isfinite(arr[3]), arr[3], 0.0)
        alpha = alpha * _scale01(a, valid)
    return dem.rgba_to_overlay(np.dstack([*chans, alpha]), xs, ys)


def load_raster_overlay(path, *, max_dim: int = 1024):
    """Render a user raster into an ipyleaflet ImageOverlay payload {"url","bounds"}.
    Returns (overlay, err): 1-band files get a grayscale 2-98 percentile stretch,
    3-4 band files render as RGB(A). VRT rides GDAL transparently. err is a short
    user-facing reason when the file can't be shown."""
    import numpy as np
    import rasterio

    from . import dem

    try:
        with rasterio.open(path) as ds:
            count = int(ds.count)
            if ds.crs is None:
                return None, "no projection info"
            band4_is_alpha = (count >= 4 and
                              ds.colorinterp[3] == rasterio.enums.ColorInterp.alpha)
    except Exception as e:  # noqa: BLE001
        return None, f"could not read the file ({e})"
    try:
        if count >= 3:
            return _rgb_overlay(path, max_dim=max_dim,
                                band4_is_alpha=band4_is_alpha), None
        z, xs, ys, _dx, _dy = dem.load_raster_4326(path, max_dim=max_dim)
        valid = np.isfinite(z)
        if not valid.any():
            return None, "no valid pixels to display"
        lo, hi = (float(v) for v in np.nanpercentile(z[valid], [2, 98]))
        if not hi > lo:
            hi = lo + 1.0
        import matplotlib
        from matplotlib.colors import Normalize
        gray = (matplotlib.colormaps["gray"] if hasattr(matplotlib, "colormaps")
                else matplotlib.cm.get_cmap("gray"))
        rgba = gray(Normalize(vmin=lo, vmax=hi)(np.clip(np.where(valid, z, lo), lo, hi)))
        rgba[..., 3] = valid.astype(float)
        return dem.rgba_to_overlay(rgba, xs, ys), None
    except Exception as e:  # noqa: BLE001
        return None, f"could not render ({e})"
