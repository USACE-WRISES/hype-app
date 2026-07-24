"""Drawn-geometry handling: ipyleaflet DrawControl GeoJSON -> projected GeoDataFrames.

The map hands us EPSG:4326 features. The MODFLOW grid math works in the projected CRS's
linear units, so we reproject to a UTM zone (metres) chosen from the domain centroid; the
model therefore runs in metres (length_units='meters').
"""
from __future__ import annotations

from math import cos, hypot, radians
from typing import Iterable, Optional

import geopandas as gpd
from shapely.geometry import LineString, Point, Polygon, mapping, shape


def features_to_gdf(features: Iterable[dict], crs="EPSG:4326") -> gpd.GeoDataFrame:
    """A list of GeoJSON Feature dicts -> GeoDataFrame in EPSG:4326."""
    geoms = [shape(f["geometry"]) for f in features if f and f.get("geometry")]
    return gpd.GeoDataFrame(geometry=geoms, crs=crs)


def _zone_num(props: dict, key: str, default: float) -> float:
    try:
        v = float((props or {}).get(key))
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def normalize_kzone_features(features: Iterable[dict], *, default_kh: float = 50.0,
                             default_kv: float = 5.0) -> list[dict]:
    """Ensure every K-zone Feature carries its own {uid, KH, KV, LABEL, src} properties.

    Zones loaded from old saves or freshly drawn arrive as bare geometry; they get a uid and
    the defaults. Zones that already carry properties pass through untouched (the DrawControl
    round-trips feature properties, so a shape edit keeps its K). The edit-only
    ``properties.style`` is stripped. Returns new Feature dicts; never mutates the input."""
    import uuid

    out = []
    for i, f in enumerate(features):
        if not f or not f.get("geometry"):
            continue
        p = dict(f.get("properties") or {})
        p.pop("style", None)
        if not p.get("uid"):
            p["uid"] = uuid.uuid4().hex[:8]
        p["KH"] = _zone_num(p, "KH", default_kh)
        p["KV"] = _zone_num(p, "KV", default_kv)
        p.setdefault("LABEL", f"Zone {i + 1}")
        p.setdefault("src", "drawn")
        out.append({"type": "Feature", "properties": p, "geometry": f["geometry"]})
    return out


def kzones_to_gdf(features: Iterable[dict], *, fallback_kh: float, fallback_kv: float,
                  crs="EPSG:4326") -> gpd.GeoDataFrame:
    """K-zone Features -> GeoDataFrame with per-row KH/KV/ZONE_ID/LABEL columns (the engine's
    ``_kh_arrays_from_polygon`` contract). Values come from each Feature's own properties;
    features without them (legacy saves) use the payload-wide fallback pair."""
    feats = [f for f in features if f and f.get("geometry")]
    props = [f.get("properties") or {} for f in feats]
    return gpd.GeoDataFrame(
        {
            "ZONE_ID": [str(p.get("uid") or i + 1) for i, p in enumerate(props)],
            "LABEL": [str(p.get("LABEL") or f"Zone {i + 1}") for i, p in enumerate(props)],
            "KH": [_zone_num(p, "KH", fallback_kh) for p in props],
            "KV": [_zone_num(p, "KV", fallback_kv) for p in props],
        },
        geometry=[shape(f["geometry"]) for f in feats], crs=crs)


def single_feature_gdf(feature: dict, crs="EPSG:4326") -> gpd.GeoDataFrame:
    """A single GeoJSON Feature dict -> 1-row GeoDataFrame in EPSG:4326."""
    return gpd.GeoDataFrame(geometry=[shape(feature["geometry"])], crs=crs)


def pick_projected_crs(domain_gdf_4326: gpd.GeoDataFrame):
    """UTM (metre) CRS appropriate for the domain centroid. The model works in metres."""
    return domain_gdf_4326.estimate_utm_crs()


def _feat(geom) -> dict:
    """Wrap a shapely geometry as a GeoJSON Feature dict (matches delineate._feat)."""
    return {"type": "Feature", "properties": {}, "geometry": mapping(geom)}


def _coords_of(feature) -> list:
    """First LineString's (x, y) coords from a Feature / geometry / FeatureCollection."""
    g = feature
    if isinstance(g, dict) and g.get("type") == "FeatureCollection":
        g = (g.get("features") or [{}])[0].get("geometry") or {}
    elif isinstance(g, dict) and g.get("type") == "Feature":
        g = g.get("geometry") or {}
    coords = list((g or {}).get("coordinates") or [])
    if coords and isinstance(coords[0][0], (list, tuple)):   # MultiLineString → first part
        coords = list(coords[0])
    return [tuple(c[:2]) for c in coords]


def assemble_domain_from_sides(up, left, right, down) -> Optional[dict]:
    """Stitch four boundary LineString Features — Upstream, Left FPL, Right FPL, Downstream — into a
    closed domain Polygon, snapping the four shared corners.

    Returns ``{"domain", "left", "right", "up", "down"}`` of GeoJSON Features with **left/right
    oriented upstream→downstream and up/down oriented left→right** (the orientation the engine's
    gradient interpolation expects), or ``None`` if any side is missing or the ring can't be built.
    Works in lon/lat (EPSG:4326); corners are matched by nearest endpoints. This is the inverse of
    ``delineate._sides_from_ring``.
    """
    coords = {}
    for k, f in (("up", up), ("left", left), ("right", right), ("down", down)):
        c = _coords_of(f) if f else []
        if len(c) < 2:
            return None
        coords[k] = c

    def _ends(k):
        return (coords[k][0], coords[k][-1])

    def _corner(a_ends, b_ends):
        """Mean of the closest endpoint pair between two sides (the snapped shared corner)."""
        best = None
        for pa in a_ends:
            for pb in b_ends:
                d = hypot(pa[0] - pb[0], pa[1] - pb[1])
                if best is None or d < best[0]:
                    best = (d, pa, pb)
        _, pa, pb = best
        return ((pa[0] + pb[0]) / 2.0, (pa[1] + pb[1]) / 2.0)

    ul = _corner(_ends("up"), _ends("left"))      # upstream-left
    ur = _corner(_ends("up"), _ends("right"))     # upstream-right
    dl = _corner(_ends("down"), _ends("left"))    # downstream-left
    dr = _corner(_ends("down"), _ends("right"))   # downstream-right

    def _oriented(k, start, end):
        """Side k as coords running start→end (flip if stored backwards), with both endpoints
        replaced by the snapped corners."""
        cs = list(coords[k])
        d0 = hypot(cs[0][0] - start[0], cs[0][1] - start[1])
        d1 = hypot(cs[-1][0] - start[0], cs[-1][1] - start[1])
        if d0 > d1:
            cs = cs[::-1]
        cs[0], cs[-1] = start, end
        return cs

    # Walk the ring: up(UL→UR) → right(UR→DR) → down(DR→DL) → left(DL→UL), dropping each repeated corner.
    ring = list(_oriented("up", ul, ur))
    for seg in (_oriented("right", ur, dr), _oriented("down", dr, dl), _oriented("left", dl, ul)):
        ring.extend(seg[1:])
    try:
        poly = Polygon(ring)
        if not poly.is_valid:
            poly = poly.buffer(0)
        if poly.is_empty or poly.geom_type != "Polygon" or poly.area <= 0:
            return None
    except Exception:  # noqa: BLE001
        return None

    return {
        "domain": _feat(poly),
        "left": _feat(LineString(_oriented("left", ul, dl))),   # upstream→downstream
        "right": _feat(LineString(_oriented("right", ur, dr))),  # upstream→downstream
        "up": _feat(LineString(_oriented("up", ul, ur))),        # left→right
        "down": _feat(LineString(_oriented("down", dl, dr))),    # left→right
    }


def corner_gaps_m(up, left, right, down) -> Optional[float]:
    """Approximate MAX shared-corner endpoint gap in metres across the four boundary sides, or None
    if a side is missing. Corner-matching mirrors ``assemble_domain_from_sides`` (closest endpoint
    pair between two adjacent sides). Uses a local equirectangular scale (good enough to decide
    whether the user's lines actually meet); a large value means the domain doesn't cleanly close."""
    coords = {}
    for k, f in (("up", up), ("left", left), ("right", right), ("down", down)):
        c = _coords_of(f) if f else []
        if len(c) < 2:
            return None
        coords[k] = c
    lat0 = coords["up"][0][1]
    kx = 111320.0 * cos(radians(lat0))       # metres per degree lon at this latitude
    ky = 110540.0                            # metres per degree lat
    def _ends(k):
        return (coords[k][0], coords[k][-1])
    def _gap(a_ends, b_ends):
        return min(hypot((pa[0] - pb[0]) * kx, (pa[1] - pb[1]) * ky)
                   for pa in a_ends for pb in b_ends)
    return max(_gap(_ends("up"), _ends("left")), _gap(_ends("up"), _ends("right")),
               _gap(_ends("down"), _ends("left")), _gap(_ends("down"), _ends("right")))


def reach_boundary_issues(reach, up, left, right, down, *, touch_tol_m=10.0) -> list:
    """Soft validation of the reach centerline against the boundary caps: flow enters and leaves
    through the upstream/downstream boundaries, so the centerline must meet both of them. Returns
    human-readable issue strings — empty when everything passes OR when any input is missing
    (absence is reported elsewhere). Centerline-overlap errors are the separate, blocking
    ``centerline_conflicts``.

    Same local equirectangular-metres approximation as ``corner_gaps_m`` (affine scaling preserves
    the intersection predicates). Generation places each cap *through* a reach endpoint with up to
    ~2 m of simplify/ring-projection slop, so "meets" is intersects OR a reach endpoint within
    ``touch_tol_m`` — an exact predicate would be knife-edge on the touch. Min over both reach
    endpoints, so the check doesn't depend on the stored reach orientation."""
    rc = _coords_of(reach) if reach else []
    if len(rc) < 2:
        return []
    coords = {}
    for k, f in (("up", up), ("left", left), ("right", right), ("down", down)):
        c = _coords_of(f) if f else []
        if len(c) < 2:
            return []
        coords[k] = c
    lat0 = rc[0][1]
    kx = 111320.0 * cos(radians(lat0))       # metres per degree lon at this latitude
    ky = 110540.0                            # metres per degree lat

    def _m(pts):
        return [(x * kx, y * ky) for x, y in pts]

    reach_m = LineString(_m(rc))
    lines = {k: LineString(_m(v)) for k, v in coords.items()}
    ends = (Point(reach_m.coords[0]), Point(reach_m.coords[-1]))
    issues = []
    for k, label in (("up", "Upstream"), ("down", "Downstream")):
        if reach_m.intersects(lines[k]):
            continue                                       # crossing or touch — both count
        gap = min(p.distance(lines[k]) for p in ends)
        if gap > touch_tol_m:
            issues.append(f"The reach centerline doesn't reach the {label} boundary "
                          f"(gap ≈ {gap:.0f} m) — extend the centerline or move the boundary, "
                          f"then regenerate.")
    return issues


def centerline_conflicts(reach, up, left, right, down, *, cap_tol_m=25.0) -> list:
    """Blocking validation: boundary lines must not lie across the reach centerline. Returns
    ``[{"slot", "label", "msg"}, ...]`` — one entry per offending side. Unlike
    ``reach_boundary_issues`` each side is checked independently, so a half-drawn boundary set
    still flags the one line that overlaps; only a missing/short reach silences everything.

    Left/right floodplain lines conflict on ANY intersection (the domain edge would run through
    the channel). The up/down caps legitimately pass *through* a reach endpoint (generation
    builds them on the end transects; manual cap edits straighten to chords with snapped
    endpoints), so a cap conflicts only where its intersection with the centerline lies more
    than ``cap_tol_m`` from BOTH reach endpoints — a cap dragged across the stream interior, or
    lying along it. Same equirectangular-metres scaling as ``reach_boundary_issues``."""
    rc = _coords_of(reach) if reach else []
    if len(rc) < 2:
        return []
    lat0 = rc[0][1]
    kx = 111320.0 * cos(radians(lat0))       # metres per degree lon at this latitude
    ky = 110540.0                            # metres per degree lat

    def _m(pts):
        return [(x * kx, y * ky) for x, y in pts]

    reach_m = LineString(_m(rc))
    end_zone = (Point(reach_m.coords[0]).buffer(cap_tol_m)
                .union(Point(reach_m.coords[-1]).buffer(cap_tol_m)))
    conflicts = []
    for slot, label, f in (("up", "Upstream", up), ("left", "Left floodplain", left),
                           ("right", "Right floodplain", right), ("down", "Downstream", down)):
        c = _coords_of(f) if f else []
        if len(c) < 2:
            continue
        line = LineString(_m(c))
        if not reach_m.intersects(line):
            continue
        if slot in ("left", "right"):
            conflicts.append({"slot": slot, "label": label,
                              "msg": f"The {label} boundary crosses the stream centerline. "
                                     "Move the boundary line or redraw the reach centerline "
                                     "so they don't overlap."})
        elif not reach_m.intersection(line).difference(end_zone).is_empty:
            conflicts.append({"slot": slot, "label": label,
                              "msg": f"The {label} boundary crosses the stream centerline away "
                                     "from the reach end. Move the boundary line or redraw the "
                                     "reach centerline."})
    return conflicts
