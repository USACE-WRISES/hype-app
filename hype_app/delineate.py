"""Auto-delineate the GW domain, floodplain boundary lines, and wetted-extent polygon
from a reach line + DEM, sized by Bieger bankfull geometry.

The floodplain is measured at only TWO cross-sections — one at the upstream end (s=0) and
one at the downstream end (s=L). On each, the DEM is sampled across a line perpendicular to
the reach: find the thalweg (min elevation, anchored to a channel window so it can't snap to
a far tributary), then walk outward each side to the first point where the DEM rises to a
target elevation (thalweg + X * bankfull_depth for the domain; + 1 * bankfull_depth for the
wetted channel). The resulting left/right offsets are then linearly INTERPOLATED along the
reach, with each intermediate cross-section kept perpendicular to the reach — so the ribbon
follows the channel curve with a smoothly varying width (no per-transect jumping/self-cross).
Connecting the interpolated edge points yields the domain ribbon, the left/right boundary
lines, and the wetted-extent polygon.
"""
from __future__ import annotations

from typing import Optional

CRS_ALBERS = 5070  # work in metres (USGS CONUS Albers)


def _normal(line, s, ds=5.0):
    """Unit vector perpendicular to `line` at station `s` (metres), via a centred difference so it's
    stable at the endpoints. Returns (point, (nx, ny)) with the normal pointing to the RIGHT of the
    downstream direction of travel, so `_edge_offsets`' `ro >= 0` side is the true right bank and
    `lo <= 0` the true left bank (projected CRS is x=east, y=north). Rotating the tangent 90° CW is
    `(dy, -dx)`; the earlier `(-dy, dx)` was 90° CCW = left, which swapped Left/Right FPL."""
    L = line.length
    s0 = max(0.0, min(float(s), L))
    a = line.interpolate(max(0.0, s0 - ds))
    b = line.interpolate(min(L, s0 + ds))
    dx, dy = b.x - a.x, b.y - a.y
    n = (dx * dx + dy * dy) ** 0.5 or 1.0
    return line.interpolate(s0), (dy / n, -dx / n)


def _tangent(line, s, ds=5.0):
    """Downstream-pointing unit tangent at station `s` (same centred difference as `_normal`).
    Returns (point, (tx, ty))."""
    L = line.length
    s0 = max(0.0, min(float(s), L))
    a = line.interpolate(max(0.0, s0 - ds))
    b = line.interpolate(min(L, s0 + ds))
    dx, dy = b.x - a.x, b.y - a.y
    n = (dx * dx + dy * dy) ** 0.5 or 1.0
    return line.interpolate(s0), (dx / n, dy / n)


def _clamp_to_end_planes(pts, line, L):
    """Pin edge points into the band between the upstream (s=0) and downstream (s=L) end
    cross-sections. On a bend near a reach end, the interpolated perpendiculars fan past the end
    transect and the domain balloons upstream/downstream of it; any point with an along-reach
    component beyond an end plane is projected back onto that plane (its lateral offset is kept).
    Points ON the end transects have a zero along-tangent component, so the caps themselves are
    unchanged."""
    p0, (tx0, ty0) = _tangent(line, 0.0)
    pL, (txL, tyL) = _tangent(line, L)
    out = []
    for (x, y) in pts:
        d0 = (x - p0.x) * tx0 + (y - p0.y) * ty0
        if d0 < 0.0:                       # upstream of the upstream transect
            x, y = x - d0 * tx0, y - d0 * ty0
        dL = (x - pL.x) * txL + (y - pL.y) * tyL
        if dL > 0.0:                       # downstream of the downstream transect
            x, y = x - dL * txL, y - dL * tyL
            d0 = (x - p0.x) * tx0 + (y - p0.y) * ty0   # re-check plane 1 (crossing planes)
            if d0 < 0.0:
                x, y = x - d0 * tx0, y - d0 * ty0
        out.append((x, y))
    return out


def _edge_offsets(dem, line, s, *, half, n_samp, rel_height, depth_bf, chan_half):
    """At station `s`: sample the DEM across the perpendicular transect and return the signed
    offsets ``(lo, ro)`` along the unit normal (``lo <= 0`` left, ``ro >= 0`` right) where the
    surface first rises ``rel_height * depth_bf`` above the thalweg on each side. The thalweg
    search is anchored to the channel window ``|t| <= chan_half`` so it can't snap to a far-off
    tributary; if the rise isn't reached on a side, fall back to that side's local elevation-max
    (the valley shoulder) rather than the far ±half edge. Returns ``(lo, ro)`` or ``None``."""
    import numpy as np
    import xarray as xr

    p, (nx, ny) = _normal(line, s)
    ts = np.linspace(-half, half, n_samp)
    zx = p.x + nx * ts
    zy = p.y + ny * ts
    z = np.asarray(dem.interp(x=xr.DataArray(zx, dims="t"),
                              y=xr.DataArray(zy, dims="t")).values, dtype=float)
    ok = np.isfinite(z)
    if ok.sum() < 5:
        return None
    vi = np.where(ok)[0]
    centre = ok & (np.abs(ts) <= chan_half)
    pool = np.where(centre)[0] if centre.any() else vi
    k = int(pool[int(np.argmin(z[pool]))])             # thalweg, anchored to the channel window
    thresh = z[k] + float(rel_height) * float(depth_bf)
    li = next((i for i in range(k, -1, -1) if ok[i] and z[i] >= thresh), None)
    ri = next((i for i in range(k, len(ts)) if ok[i] and z[i] >= thresh), None)
    if li is None:                                     # shoulder fallback: highest ground left of k
        seg = [i for i in range(0, k + 1) if ok[i]]
        li = int(seg[int(np.argmax(z[seg]))]) if seg else int(vi[0])
    if ri is None:                                     # highest ground right of k
        seg = [i for i in range(k, len(ts)) if ok[i]]
        ri = int(seg[int(np.argmax(z[seg]))]) if seg else int(vi[-1])
    if li == ri:                                       # degenerate: nudge a couple samples
        li = max(int(vi[0]), k - 2); ri = min(int(vi[-1]), k + 2)
        if li == ri:
            return None
    return float(ts[li]), float(ts[ri])


def _interp_sides(dem, line, L, stations, *, rel_height, depth_bf, half, n_samp, chan_half):
    """Measure the floodplain offsets only at the upstream (s=0) and downstream (s=L) cross-
    sections, then build per-station edge points by linearly interpolating the left/right
    offsets along the reach and placing them perpendicular to it — so the ribbon follows the
    channel curve with a smoothly varying width (no per-transect jumping). Returns
    ``(dleft, dright)`` coordinate lists, or ``None`` if neither end could be sampled."""
    def _end(s):
        e = _edge_offsets(dem, line, s, half=half, n_samp=n_samp, rel_height=rel_height,
                          depth_bf=depth_bf, chan_half=chan_half)
        if e is None and L > 0:                        # retry ~2% in from the reach end
            s2 = min(max(s + (0.02 * L if s < L / 2.0 else -0.02 * L), 0.0), L)
            e = _edge_offsets(dem, line, s2, half=half, n_samp=n_samp, rel_height=rel_height,
                              depth_bf=depth_bf, chan_half=chan_half)
        return e

    up = _end(0.0)
    dn = _end(L)
    if up is None and dn is None:
        return None
    up = up or dn                                      # if one end failed, reuse the other
    dn = dn or up
    lo_u, ro_u = up
    lo_d, ro_d = dn
    dleft, dright = [], []
    for s in stations:
        f = (s / L) if L > 0 else 0.0
        lo = lo_u + f * (lo_d - lo_u)
        ro = ro_u + f * (ro_d - ro_u)
        p, (nx, ny) = _normal(line, s)
        dleft.append((p.x + nx * lo, p.y + ny * lo))
        dright.append((p.x + nx * ro, p.y + ny * ro))
    return dleft, dright


def _ribbon(dleft, dright):
    """A single valid simple Polygon from left+right side coordinates. Uses the direct ring when
    the sides don't cross (the common case); otherwise unions per-panel quads — which provably
    contain both side-lines, so the boundary lines can never fall outside the domain — and keeps
    the largest piece. Returns the Polygon or ``None``."""
    from shapely.geometry import Polygon
    from shapely.ops import unary_union
    try:
        from shapely import make_valid
    except Exception:  # noqa: BLE001
        make_valid = None

    n = min(len(dleft), len(dright))
    if n < 2:
        return None
    ring = Polygon(dleft + dright[::-1])
    if ring.is_valid and ring.is_simple and ring.area > 0:
        return ring
    quads = []
    for i in range(n - 1):
        q = Polygon([dleft[i], dleft[i + 1], dright[i + 1], dright[i]])
        q = make_valid(q) if make_valid is not None else q.buffer(0)
        if (not q.is_empty) and q.area > 0:
            quads.append(q.buffer(0))
    if not quads:
        return None
    merged = unary_union(quads)
    if merged.geom_type == "MultiPolygon":
        merged = max(merged.geoms, key=lambda g: g.area)
    merged = merged.buffer(0)
    return merged if (not merged.is_empty and merged.area > 0) else None


def _resample_line(line, spacing):
    """Re-distribute the centerline's vertices at even `spacing` (m) so the perpendiculars don't
    wobble on jagged input — same idea as RAS's fixed along-channel resampling."""
    import numpy as np
    from shapely.geometry import LineString
    L = float(line.length)
    if L <= 0:
        return line
    n = max(2, int(L / max(spacing, 1.0)) + 1)
    pts = [line.interpolate(float(s)) for s in np.linspace(0.0, L, n)]
    return LineString([(p.x, p.y) for p in pts])


def _sides_from_ring(domain, up_left, up_right, dn_left, dn_right):
    """Derive the left/right boundary lines + flat upstream/downstream caps as slices of the clean
    domain boundary ring — the shapely equivalent of RAS's SnapToRing + SubRing, so the four sides
    are simple, lie exactly on the domain, and meet at the caps (instead of using the raw offset
    points, which can self-cross). Returns ``(left, right, up_cap, dn_cap)`` LineStrings (left/right
    oriented upstream→downstream), or ``None`` if the four corners aren't in a clean cyclic order
    (the caller then falls back to the raw offset lines)."""
    from shapely.geometry import LineString, Point
    from shapely.ops import substring
    try:
        ring = LineString(domain.exterior.coords)
        total = float(ring.length)
        if total <= 0:
            return None
        corners = {"ul": tuple(up_left), "ur": tuple(up_right),
                   "dl": tuple(dn_left), "dr": tuple(dn_right)}
        pos = {k: float(ring.project(Point(v))) for k, v in corners.items()}
        order = sorted(corners, key=lambda k: pos[k])
        arc_by_pair = {}
        for i in range(4):
            a, b = order[i], order[(i + 1) % 4]
            if i < 3:
                seg = substring(ring, pos[a], pos[b])
            else:                                          # last arc wraps past the closure point
                s1 = substring(ring, pos[a], total)
                s2 = substring(ring, 0.0, pos[b])
                seg = LineString(list(s1.coords) + list(s2.coords)[1:])
            arc_by_pair[frozenset((a, b))] = seg
        need = [("ul", "dl"), ("dl", "dr"), ("dr", "ur"), ("ur", "ul")]
        if any(frozenset(p) not in arc_by_pair for p in need):
            return None                                    # corners not in a clean cyclic order

        def _oriented(start_key, end_key):
            seg = arc_by_pair[frozenset((start_key, end_key))]
            sp = ring.interpolate(pos[start_key])
            if seg is None or seg.is_empty or len(seg.coords) < 2:
                return LineString([sp, ring.interpolate(pos[end_key])])
            cs = list(seg.coords)
            if Point(cs[0]).distance(sp) > Point(cs[-1]).distance(sp):
                cs = cs[::-1]                              # orient start → end
            return LineString(cs)

        sides = (_oriented("ul", "dl"), _oriented("ur", "dr"),
                 _oriented("ul", "ur"), _oriented("dl", "dr"))
        for g in sides:
            if not (g.is_simple and g.length > 0):
                return None
            if max(Point(c).distance(ring) for c in g.coords) > 5.0:
                return None             # a side degenerated to an off-boundary chord (corner swallowed
            #                             inside the union on a very tight curve) — let the caller fall back
        return sides
    except Exception:  # noqa: BLE001
        return None


# A vertex is pruned only if its removal grows the floodplain by MORE than this (m²) — a floor above
# float64 shoelace noise on Albers-metre coordinates (|x·y| ~ 1e13, so per-area error ~0.1-0.2 m²), so
# genuinely near-collinear vertices aren't churned in/out by rounding (and the result no longer depends
# on shoelace-vs-GEOS rounding). A real inward dent gains orders of magnitude more than this.
_FPL_AREA_EPS_M2 = 1.0


def _floodplain_area(side_coords, reach_coords) -> float:
    """Area (m², projected CRS) of the floodplain polygon bounded by one side line and the
    centerline: down the side line (upstream→downstream), back up the reversed centerline, then
    close. The two implicit closing edges (side-DS→reach-DS and reach-US→side-US) are the left/right
    halves of the downstream/upstream boundary caps. Returns 0.0 for a degenerate ring — including a
    self-crossing one, whose shoelace area is smaller, so such chords are never chosen below.

    Deliberately a pure-Python shoelace, NOT shapely: the greedy prune calls this O(n²) times per
    side, and keeping it off the GEOS/shapely path both speeds it up and avoids piling native
    allocations onto the numpy2.5+shapely2.1 GC hazard ([[hype-app-shiny-map-gotchas]])."""
    ring = list(side_coords) + list(reversed(list(reach_coords)))
    n = len(ring)
    if n < 3:
        return 0.0
    total = 0.0
    x1, y1 = ring[-1]
    for x2, y2 in ring:
        total += x1 * y2 - x2 * y1
        x1, y1 = x2, y2
    return abs(total) * 0.5


def _maximize_floodplain(side_line, reach_line):
    """Prune STRICTLY CONCAVE vertices from a floodplain boundary line — the "drop inward dents, keep
    every outward bulge" pass applied on auto-generation. A vertex is removed only when its removal
    STRICTLY grows the floodplain area between the side line and the centerline (i.e. it is an inward
    dent, lying on the channel side of the chord between its neighbours). A convex/outward vertex
    shrinks the area, so it is always kept — the line never sacrifices a bulge to shortcut a nearby
    dent. Repeated to a fixpoint, since removing one dent can leave a neighbour concave; each pass
    that removes nothing means no interior vertex is concave. Endpoints — the domain corners shared
    with the up/down caps — are never touched, so the derived domain still closes on them. Returns a
    LineString (the input unchanged when it has < 3 vertices)."""
    from shapely.geometry import LineString
    P = [tuple(c[:2]) for c in side_line.coords]
    if len(P) < 3:
        return side_line
    reach = [tuple(c[:2]) for c in reach_line.coords]
    changed = True
    while changed and len(P) > 2:
        changed = False
        k = 1
        while k < len(P) - 1:                     # endpoints P[0]/P[-1] are pinned
            if _floodplain_area(P[:k] + P[k + 1:], reach) > _floodplain_area(P, reach) + _FPL_AREA_EPS_M2:
                del P[k]                           # strictly concave dent → drop it; re-test new P[k]
                changed = True
            else:
                k += 1                             # convex / collinear → keep it, advance
    return LineString(P)


def _line_coords(reach_geojson) -> list:
    """Pull the first LineString's coordinates from a FeatureCollection / Feature / geometry."""
    g = reach_geojson
    if isinstance(g, dict) and g.get("type") == "FeatureCollection":
        g = (g.get("features") or [{}])[0].get("geometry") or {}
    elif isinstance(g, dict) and g.get("type") == "Feature":
        g = g.get("geometry") or {}
    coords = (g or {}).get("coordinates") or []
    if coords and isinstance(coords[0][0], (list, tuple)):  # MultiLineString → first part
        coords = coords[0]
    return coords


def _feat(geom):
    from shapely.geometry import mapping
    return {"type": "Feature", "properties": {}, "geometry": mapping(geom)}


SIMPLIFY_TOL_M = 2.0  # metres; Douglas-Peucker tolerance. << the 10 m model cells, so the
#                       generated linework keeps its shape while shedding redundant vertices.


def _simplify(geom, tol=SIMPLIFY_TOL_M):
    """Drop duplicate + near-collinear vertices (Douglas-Peucker; first/last point always kept).
    preserve_topology keeps polygons valid / non-self-intersecting; buffer(0) is a safety net."""
    if geom is None or geom.is_empty:
        return geom
    s = geom.simplify(tol, preserve_topology=True)
    if s is None or s.is_empty:
        return geom                                    # never collapse to nothing
    if s.geom_type in ("Polygon", "MultiPolygon"):
        s = s.buffer(0)
        if s.geom_type == "MultiPolygon" and not s.is_empty:
            s = max(s.geoms, key=lambda g: g.area)     # keep the largest piece (single Polygon)
    return s if (s is not None and not s.is_empty) else geom


def condition_boundary_sides(left, right, up_cap, down_cap, tol=SIMPLIFY_TOL_M):
    """Straighten the upstream/downstream caps to true 2-point chords and migrate cap-collinear
    lead/tail vertices of the left/right floodplain lines into them, so the caps are straight
    BC lines and the floodplain lines are purely the sides. Pure geometry in projected metres.

    Assumes the ``_sides_from_ring`` / fallback corner convention (left ul→dl, right ur→dr,
    up ul→ur, down dl→dr, shared corner coords exact) — the cap chords are taken from the SIDE
    endpoints, which keeps the shared corners bit-exact. A side vertex counts as collinear when
    its perpendicular distance to the cap's ORIGINAL chord line is ≤ ``tol``; every candidate is
    tested against that fixed line (never a drifting one), so outward runs (end-plane clamp
    remnants beyond the corner) extend the cap and inward runs (overlap spikes doubling back
    inside the cap span) square it, with the same test. A side always keeps ≥ 2 vertices and
    never gives up its opposite endpoint. Returns ``(left, right, up_cap, down_cap)``."""
    from math import hypot

    from shapely.geometry import LineString

    L = [tuple(c[:2]) for c in left.coords]
    R = [tuple(c[:2]) for c in right.coords]
    if len(L) < 2 or len(R) < 2:
        return left, right, up_cap, down_cap               # degenerate input: hands off

    UL, UR, DL, DR = L[0], R[0], L[-1], R[-1]

    def _dist_fn(A, B):
        """Perpendicular distance to the infinite line A→B (None for a ~zero-length chord)."""
        ux, uy = B[0] - A[0], B[1] - A[1]
        n = hypot(ux, uy)
        if n < 1e-9:
            return None
        return lambda p: abs((p[0] - A[0]) * uy - (p[1] - A[1]) * ux) / n

    d_up, d_dn = _dist_fn(UL, UR), _dist_fn(DL, DR)

    def _run(P, dist):
        """Length of the contiguous collinear run walking in from P[0] (the corner itself);
        stops before the opposite endpoint so a side can never be consumed whole."""
        if dist is None:
            return 0
        j = 0
        while j + 1 <= len(P) - 2 and dist(P[j + 1]) <= tol:
            j += 1
        return j

    def _trim(P):
        a = _run(P, d_up)                     # lead run → absorbed into the up cap
        b = _run(P[::-1], d_dn)               # tail run → absorbed into the down cap
        while a + b > len(P) - 2:             # keep ≥ 2 vertices when both ends migrate
            if a >= b:
                a -= 1
            else:
                b -= 1
        return P[a:len(P) - b]

    L2, R2 = _trim(L), _trim(R)
    up2, dn2 = LineString([L2[0], R2[0]]), LineString([L2[-1], R2[-1]])
    if up2.length <= 0 or dn2.length <= 0:    # migrations collapsed a cap → straighten only
        return left, right, LineString([UL, UR]), LineString([DL, DR])
    return LineString(L2), LineString(R2), up2, dn2


def _nverts(geom):
    """Vertex count of a Polygon ring / LineString (for the delineation readout)."""
    if geom is None or getattr(geom, "is_empty", True):
        return 0
    if geom.geom_type == "Polygon":
        return len(geom.exterior.coords)
    if geom.geom_type == "LineString":
        return len(geom.coords)
    return 0


def _prep(reach_geojson, dem_path, *, da_sqkm, lat=None, lon=None):
    """Shared setup for the delineators: projected+resampled reach line, its length, the Albers
    DEM, and the Bieger-sized transect parameters."""
    import geopandas as gpd
    import rioxarray  # noqa: F401 — .rio accessor
    from shapely.geometry import LineString

    from . import bieger

    coords = _line_coords(reach_geojson)
    if len(coords) < 2:
        raise ValueError("Reach line has too few vertices to delineate.")
    line = gpd.GeoSeries([LineString(coords)], crs=4326).to_crs(CRS_ALBERS).iloc[0]
    L = float(line.length)
    if L < 5.0:
        raise ValueError("Reach is too short to delineate.")
    line = _resample_line(line, max(5.0, L / 40.0))     # even vertices → stable perpendiculars

    bf = bieger.bankfull_geometry(da_sqkm, lat, lon)
    depth_bf = max(float(bf["depth_m"]), 0.05)
    w_bf = max(float(bf["width_m"]), 1.0)
    half = min(max(8.0 * w_bf, 250.0), 800.0)          # search half-width (m); matches DEM buffer
    n_samp = int(2 * half / 5.0) + 1                    # ~5 m spacing across the transect
    chan_half = min(half, max(4.0 * w_bf, 100.0))       # channel window for the thalweg anchor

    dem = rioxarray.open_rasterio(dem_path, masked=True).squeeze().rio.reproject(CRS_ALBERS)
    return line, L, dem, bf, depth_bf, w_bf, half, n_samp, chan_half


def _wse_ribbon(dem, line, L, *, depth_bf, half, n_samp, chan_half):
    """The wetted-extent polygon: the two-XS interpolation at the bankfull-channel threshold,
    clamped to the end transects. Returns a Polygon (Albers) or None."""
    import numpy as np
    n_wse = max(20, min(80, int(L / 15.0)))
    wsides = _interp_sides(dem, line, L, list(np.linspace(0.0, L, n_wse)), rel_height=1.0,
                           depth_bf=depth_bf, half=half, n_samp=n_samp, chan_half=chan_half)
    if wsides is None or len(wsides[0]) < 3:
        return None
    wse = _simplify(_ribbon(_clamp_to_end_planes(wsides[0], line, L),
                            _clamp_to_end_planes(wsides[1], line, L)))
    if wse is None or wse.is_empty or wse.area <= 0:
        return None
    return wse


def auto_delineate(reach_geojson, dem_path, *, da_sqkm, lat=None, lon=None,
                   x_mult=2.0, n_domain=10, want_wse=True, log=print) -> dict:
    """Build {domain, left, right, wse_extent} GeoJSON Features (EPSG:4326) + meta.
    ``want_wse=False`` skips the wetted-extent derivation entirely (the app only consumes it when
    the water-surface mode is "Wetted extent"; the modeled/uploaded modes bring their own)."""
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import LineString

    line, L, dem, bf, depth_bf, w_bf, half, n_samp, chan_half = _prep(
        reach_geojson, dem_path, da_sqkm=da_sqkm, lat=lat, lon=lon)

    # --- domain + boundaries: measure floodplain offsets at the two end cross-sections only,
    #     then interpolate the left/right widths along the reach (perpendicular at each station). ---
    n_dom = max(12, min(60, int(L / 20.0)))
    dom_stations = list(np.linspace(0.0, L, n_dom))
    sides = _interp_sides(dem, line, L, dom_stations, rel_height=float(x_mult),
                          depth_bf=depth_bf, half=half, n_samp=n_samp, chan_half=chan_half)
    if sides is None or len(sides[0]) < 3:
        raise ValueError("Could not sample valid cross-sections at the reach ends (DEM gaps?).")
    # Clamp the fan-out at bends: no station's edge points may land upstream of the upstream
    # transect or downstream of the downstream one (the domain used to balloon past the caps).
    dleft = _clamp_to_end_planes(sides[0], line, L)
    dright = _clamp_to_end_planes(sides[1], line, L)
    domain = _simplify(_ribbon(dleft, dright))
    if domain is None or domain.is_empty or domain.area <= 0:
        raise ValueError("Delineated domain is degenerate; try a different reach or X.")
    # Derive the left/right boundaries + flat upstream/downstream caps as slices of the clean
    # domain ring (RAS ChannelTopologyBuilder approach) — simple, on the domain, and meeting at
    # the caps — instead of the raw offset points, which self-cross when the floodplain is wide.
    split = _sides_from_ring(domain, dleft[0], dright[0], dleft[-1], dright[-1])
    if split is not None:
        left_line, right_line, up_cap, down_cap = split
    else:                                               # fallback: raw offset lines + straight caps
        left_line = _simplify(LineString(dleft))
        right_line = _simplify(LineString(dright))
        up_cap = LineString([dleft[0], dright[0]])
        down_cap = LineString([dleft[-1], dright[-1]])

    # Prune inward "dents" from each floodplain line: greedily drop any vertex whose removal grows
    # the floodplain area between that line and the centerline (keeping genuine outward bulges). The
    # domain, derived from the four sides downstream (app: geometry.assemble_domain_from_sides),
    # follows automatically; the shared corner endpoints are pinned, so it still closes on them.
    n_l0, n_r0 = _nverts(left_line), _nverts(right_line)
    left_line = _maximize_floodplain(left_line, line)
    right_line = _maximize_floodplain(right_line, line)
    fpl_dropped = (n_l0 - _nverts(left_line)) + (n_r0 - _nverts(right_line))

    # Straight caps + square corners: the caps become true 2-point chords (they are the RAS BC
    # lines), and lead/tail side vertices collinear with a cap (end-plane clamp remnants,
    # ring-slice jogs, overlap spikes) migrate into that cap so the floodplain lines are purely
    # the sides. The returned "domain" stays the raw ribbon — no consumer reads it; the app
    # always reassembles its domain from these four sides (geometry.assemble_domain_from_sides).
    n_u0, n_d0 = _nverts(up_cap), _nverts(down_cap)
    n_l1, n_r1 = _nverts(left_line), _nverts(right_line)
    left_line, right_line, up_cap, down_cap = condition_boundary_sides(
        left_line, right_line, up_cap, down_cap)
    cap_dropped = (n_u0 - _nverts(up_cap)) + (n_d0 - _nverts(down_cap))
    cap_migrated = (n_l1 - _nverts(left_line)) + (n_r1 - _nverts(right_line))

    # --- wetted extent (only when the app will actually use it) ---
    wse = (_wse_ribbon(dem, line, L, depth_bf=depth_bf, half=half, n_samp=n_samp,
                       chan_half=chan_half) if want_wse else None)

    def to4326(geom):
        return gpd.GeoSeries([geom], crs=CRS_ALBERS).to_crs(4326).iloc[0]

    def to4326_poly(geom):
        """Reproject + guarantee a single valid Polygon (reprojection can introduce a self-touch
        on a thin ribbon, and very short reaches with huge floodplains can still fold). Keeps the
        largest polygonal piece of whatever make_valid returns (Polygon/Multi/GeometryCollection)."""
        g = to4326(geom)
        if not g.is_valid:
            try:
                from shapely import make_valid
                g = make_valid(g)
            except Exception:  # noqa: BLE001
                g = g.buffer(0)
        polys = []
        for part in (g.geoms if hasattr(g, "geoms") else [g]):
            if part.geom_type == "Polygon":
                polys.append(part)
            elif part.geom_type == "MultiPolygon":
                polys.extend(part.geoms)
        if polys:
            g = max(polys, key=lambda p: p.area)
        return g

    out = {
        "domain": _feat(to4326_poly(domain)),
        "left": _feat(to4326(left_line)),
        "right": _feat(to4326(right_line)),
        "up_cap": _feat(to4326(up_cap)),
        "down_cap": _feat(to4326(down_cap)),
        "wse_extent": _feat(to4326_poly(wse)) if wse is not None else None,
        "meta": {
            "da_sqkm": round(float(da_sqkm or 0.0), 3),
            "bankfull_depth_m": depth_bf, "bankfull_width_m": w_bf,
            "division": bf["division_name"], "x_mult": float(x_mult),
            "reach_len_m": round(L, 1), "n_domain_xs": len(dleft),
            "wse_vertices": _nverts(wse), "boundary_vertices": _nverts(left_line),
        },
    }
    log(f"Delineated: {len(dleft)} domain XS, reach {L:.0f} m, "
        f"bankfull depth {depth_bf:.2f} m ({bf['division_name']}), X={x_mult}; "
        f"floodplain simplification dropped {fpl_dropped} inward vertex(es); "
        f"caps straightened ({cap_dropped} bend vertex(es) removed, "
        f"{cap_migrated} collinear side vertex(es) migrated into the caps).")
    return out


def auto_wse_extent(reach_geojson, dem_path, *, da_sqkm, lat=None, lon=None, log=print):
    """Derive ONLY the wetted-extent polygon (EPSG:4326 Feature, or None) — used when the user
    switches the water-surface mode to "Wetted extent" after boundaries were generated without
    one (auto_delineate ran with want_wse=False)."""
    import geopandas as gpd

    line, L, dem, _bf, depth_bf, _w_bf, half, n_samp, chan_half = _prep(
        reach_geojson, dem_path, da_sqkm=da_sqkm, lat=lat, lon=lon)
    wse = _wse_ribbon(dem, line, L, depth_bf=depth_bf, half=half, n_samp=n_samp,
                      chan_half=chan_half)
    if wse is None:
        return None

    def to4326_poly(geom):
        g = gpd.GeoSeries([geom], crs=CRS_ALBERS).to_crs(4326).iloc[0]
        if not g.is_valid:
            try:
                from shapely import make_valid
                g = make_valid(g)
            except Exception:  # noqa: BLE001
                g = g.buffer(0)
        polys = [p for part in (g.geoms if hasattr(g, "geoms") else [g])
                 for p in (part.geoms if part.geom_type == "MultiPolygon" else [part])
                 if p.geom_type == "Polygon"]
        return max(polys, key=lambda p: p.area) if polys else g

    log(f"Derived wetted extent: {_nverts(wse)} vertices over {L:.0f} m.")
    return _feat(to4326_poly(wse))


def cross_section_lines(reach_geojson, dem_path, *, da_sqkm, lat=None, lon=None,
                        x_mult=2.0, n=10) -> Optional[dict]:
    """The domain cross-section transects as a GeoJSON FeatureCollection (for display)."""
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import LineString

    try:
        line, L, dem, _bf, depth_bf, _w_bf, half, n_samp, chan_half = _prep(
            reach_geojson, dem_path, da_sqkm=da_sqkm, lat=lat, lon=lon)
    except ValueError:
        return None
    sides = _interp_sides(dem, line, L, list(np.linspace(0.0, L, max(3, int(n)))),
                          rel_height=float(x_mult), depth_bf=depth_bf, half=half,
                          n_samp=n_samp, chan_half=chan_half)
    if sides is None:
        return None
    dleft = _clamp_to_end_planes(sides[0], line, L)     # same band as the generated domain
    dright = _clamp_to_end_planes(sides[1], line, L)
    segs = [LineString([dleft[i], dright[i]]) for i in range(len(dleft))]
    if not segs:
        return None
    gj = gpd.GeoSeries(segs, crs=CRS_ALBERS).to_crs(4326)
    return {"type": "FeatureCollection",
            "features": [_feat(g) for g in gj.geometry]}


def min_elevation_along_line(feat_4326, dem_path, *, n: int = 200) -> Optional[float]:
    """Minimum finite DEM elevation sampled along a LineString Feature (EPSG:4326).

    Used to pick the streambed (thalweg) elevation where a boundary cap crosses the channel: sample
    the (carved) terrain along the upstream/downstream boundary line and take the min. Also samples
    WSE rasters (reference-slope reporting), so values <= -1000 m are treated as undeclared nodata
    sentinels (-9999 uploads) — no terrestrial elevation goes that low. Returns None when the
    geometry is empty or nothing valid samples.
    """
    import numpy as np
    import rasterio
    from pyproj import Transformer
    from shapely.geometry import shape

    try:
        line = shape(feat_4326["geometry"])
    except Exception:  # noqa: BLE001
        return None
    if line.is_empty or line.length == 0:
        return None
    with rasterio.open(dem_path) as src:
        tr = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
        fracs = np.linspace(0.0, 1.0, max(2, int(n)))
        pts = [line.interpolate(float(f), normalized=True) for f in fracs]
        xs, ys = tr.transform([p.x for p in pts], [p.y for p in pts])
        vals = np.array([v[0] for v in src.sample(np.column_stack([xs, ys]))], dtype="float64")
        nod = src.nodata
    if nod is not None:
        vals = np.where(vals == nod, np.nan, vals)
    vals = np.where(vals <= -1000.0, np.nan, vals)   # undeclared sentinels (e.g. -9999 uploads)
    vals = vals[np.isfinite(vals)]
    if not vals.size:
        return None
    return float(np.min(vals))


def orient_reach_downstream(feat_4326, dem_path, *, frac: float = 0.1,
                            n: int = 40) -> tuple[dict, bool]:
    """Return (feature, flipped) with the reach LineString running upstream → downstream.

    The whole pipeline assumes upstream-first: `_normal` puts the RIGHT bank at `ro >= 0` of
    downstream travel, so a backwards line silently swaps the Left/Right floodplain. Auto NHD
    traces are upstream-first by construction; a MANUAL draw can run either way — so compare
    the mean DEM elevation over the first vs last `frac` of the line and reverse the
    coordinates when it was drawn uphill. On any sampling problem the feature is returned
    unchanged (never block the pipeline on this check).
    """
    import numpy as np
    import rasterio
    from pyproj import Transformer
    from shapely.geometry import shape

    try:
        line = shape(feat_4326["geometry"])
        coords = list(feat_4326["geometry"]["coordinates"])
    except Exception:  # noqa: BLE001
        return feat_4326, False
    if line.is_empty or line.length == 0 or len(coords) < 2:
        return feat_4326, False

    def _mean_elev(src, tr, fracs):
        pts = [line.interpolate(float(f), normalized=True) for f in fracs]
        xs, ys = tr.transform([p.x for p in pts], [p.y for p in pts])
        vals = np.array([v[0] for v in src.sample(np.column_stack([xs, ys]))], dtype="float64")
        if src.nodata is not None:
            vals = np.where(vals == src.nodata, np.nan, vals)
        vals = vals[np.isfinite(vals)]
        return float(np.mean(vals)) if vals.size else None

    try:
        with rasterio.open(dem_path) as src:
            tr = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
            k = max(2, int(n))
            head = _mean_elev(src, tr, np.linspace(0.0, frac, k))
            tail = _mean_elev(src, tr, np.linspace(1.0 - frac, 1.0, k))
    except Exception:  # noqa: BLE001
        return feat_4326, False
    if head is None or tail is None or head >= tail - 0.05:
        return feat_4326, False        # already downhill (or flat/ambiguous) — keep as drawn
    flipped = {"type": "Feature",
               "properties": dict(feat_4326.get("properties") or {}),
               "geometry": {"type": "LineString",
                            "coordinates": [list(c) for c in reversed(coords)]}}
    return flipped, True
