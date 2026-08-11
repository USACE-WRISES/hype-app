"""Geometry stage: build the model domain the same way the app would end up with it.

`ras_import` mode (all 34 sites): the solved RAS project's 2D flow-area
perimeter and its US/DS boundary-condition lines ARE the calibrated model
footprint, so reuse them. The ring is split into left/right/up/down sides with
the app's own `delineate._sides_from_ring`, then reassembled and oriented by
`geometry.assemble_domain_from_sides`, which is the exact orientation contract
the engine's gradient interpolation expects. This also sidesteps NHD
drainage-area resolution entirely (the LL01096 confluence trap).

The reach centerline is metadata (DEM AOI, report, orientation), never a run
input: tiered from the RAS Features shapefiles, else a straight US-DS line.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

FT_US = 0.3048006096012192


def _feature(geom) -> dict:
    from shapely.geometry import mapping

    return {"type": "Feature", "properties": {}, "geometry": mapping(geom)}


def read_ras_geometry(prj_path: Path) -> dict:
    """Perimeter ring, BC lines, and CRS out of the project's current-plan geometry HDF."""
    import h5py
    from pyproj import CRS

    prj_path = Path(prj_path)
    text = prj_path.read_text(errors="replace")
    import re

    geoms = re.findall(r"Geom File=(\w+)", text)
    base = prj_path.with_suffix("")
    ghdf = None
    for g in geoms:  # first geometry with an HDF (all 34 use g01)
        cand = base.parent / f"{base.name}.{g}.hdf"
        if cand.exists():
            ghdf = cand
            break
    if ghdf is None:
        raise FileNotFoundError(f"no geometry HDF next to {prj_path}")

    with h5py.File(ghdf, "r") as f:
        crs_wkt = f.attrs.get("Projection")
        if isinstance(crs_wkt, bytes):
            crs_wkt = crs_wkt.decode("utf-8", "replace")
        geo = f["Geometry"]
        fa = geo["2D Flow Areas"]
        area_names = [k for k in fa.keys() if isinstance(fa[k], h5py.Group) and "Perimeter" in fa[k]]
        if not area_names:
            raise ValueError(f"{ghdf.name}: no 2D flow area with a Perimeter dataset")
        ring = fa[area_names[0]]["Perimeter"][:]
        bc = geo["Boundary Condition Lines"]
        bc_names = [(r[0].decode() if isinstance(r[0], bytes) else str(r[0]))
                    for r in bc["Attributes"][:]]
        pts = bc["Polyline Points"][:]
        # Polyline Points concatenates all BC lines; Polyline Info gives (start, count) rows.
        if "Polyline Info" in bc:
            info = bc["Polyline Info"][:]
            spans = [(int(r[0]), int(r[1])) for r in info]
        else:  # two 2-point lines laid out consecutively
            n = len(pts) // max(len(bc_names), 1)
            spans = [(i * n, n) for i in range(len(bc_names))]
        lines = {}
        for name, (start, count) in zip(bc_names, spans):
            lines[name.strip().upper()] = pts[start:start + count]

    crs = CRS.from_wkt(crs_wkt) if crs_wkt else CRS.from_epsg(2277)
    return {"ghdf": str(ghdf), "crs": crs, "ring": ring, "bc_lines": lines}


def split_and_orient(raw: dict) -> dict:
    """Ring + BC lines -> oriented 4326 Features via the app's own helpers."""
    import geopandas as gpd
    import numpy as np
    from shapely.geometry import LineString, Polygon, mapping

    from hype_app import delineate as dln
    from hype_app import geometry as geo

    ring = np.asarray(raw["ring"], dtype=float)
    us, ds = raw["bc_lines"].get("US"), raw["bc_lines"].get("DS")
    if us is None or ds is None:
        raise ValueError(f"BC lines missing US/DS: have {list(raw['bc_lines'])}")

    mid_us = np.mean(np.asarray(us, dtype=float), axis=0)
    mid_ds = np.mean(np.asarray(ds, dtype=float), axis=0)
    d = mid_ds - mid_us                      # downstream travel direction
    left_normal = np.array([-d[1], d[0]])    # left of travel

    def left_of(pt, mid):
        return float(np.dot(np.asarray(pt, dtype=float) - mid, left_normal))

    us_sorted = sorted((tuple(p) for p in us), key=lambda p: -left_of(p, mid_us))
    ds_sorted = sorted((tuple(p) for p in ds), key=lambda p: -left_of(p, mid_ds))
    up_left, up_right = us_sorted[0], us_sorted[-1]
    dn_left, dn_right = ds_sorted[0], ds_sorted[-1]

    poly = Polygon(ring)
    if not poly.is_valid:
        poly = poly.buffer(0)
    split = dln._sides_from_ring(poly, up_left, up_right, dn_left, dn_right)
    if split is None:
        raise ValueError("corners not in clean cyclic order on the RAS perimeter ring")
    left, right, up_cap, dn_cap = split

    def to4326(shp):
        return gpd.GeoSeries([shp], crs=raw["crs"]).to_crs(4326).iloc[0]

    build = geo.assemble_domain_from_sides(
        _feature(to4326(up_cap)), _feature(to4326(left)),
        _feature(to4326(right)), _feature(to4326(dn_cap)))
    if build is None:
        raise ValueError("assemble_domain_from_sides rejected the RAS-derived sides")
    build["mid_us_4326"] = tuple(to4326(LineString([mid_us, mid_us + d * 1e-6]).interpolate(0)).coords)[0] \
        if False else tuple(gpd.GeoSeries(
            [LineString([mid_us, mid_ds])], crs=raw["crs"]).to_crs(4326).iloc[0].coords)[0]
    build["mid_ds_4326"] = tuple(gpd.GeoSeries(
        [LineString([mid_us, mid_ds])], crs=raw["crs"]).to_crs(4326).iloc[0].coords)[-1]
    return build


def _spans_domain(line, build) -> bool:
    """A usable reach must run cap to cap, not be a local transect stub.

    Both endpoints must land within 20 percent of the US-DS separation of their
    respective caps. This is what rejected LL01096's Profile Lines candidate,
    which is a well-transect profile covering a third of the domain.
    """
    from shapely.geometry import Point, shape

    up = shape(build["up"]["geometry"])
    down = shape(build["down"]["geometry"])
    scale = Point(build["mid_us_4326"]).distance(Point(build["mid_ds_4326"]))
    if scale <= 0:
        return False
    a, b = Point(line.coords[0]), Point(line.coords[-1])
    d_up = min(a.distance(up), b.distance(up))
    d_dn = min(a.distance(down), b.distance(down))
    return d_up < 0.20 * scale and d_dn < 0.20 * scale


def _midline_of_sides(build):
    """Corridor midline: midpoints of left/right sampled at equal fractions.

    Both sides are oriented upstream to downstream by assemble_domain_from_sides,
    so equal-fraction midpoints span the domain cap to cap by construction.
    """
    from shapely.geometry import LineString, shape

    left = shape(build["left"]["geometry"])
    right = shape(build["right"]["geometry"])
    n = 40
    pts = []
    for i in range(n + 1):
        f = i / n
        a = left.interpolate(f, normalized=True)
        b = right.interpolate(f, normalized=True)
        pts.append(((a.x + b.x) / 2.0, (a.y + b.y) / 2.0))
    return LineString(pts)


def centerline(site_dir: Path, build: dict, raw: dict) -> dict:
    """Reach centerline Feature (4326), oriented US -> DS, spanning cap to cap.

    Tiers: RAS Features shapefile line (must span), NHD network trace between
    the BC midpoints (must span), corridor midline (spans by construction).
    """
    import geopandas as gpd
    from shapely.geometry import LineString, Point, shape

    dom = shape(build["domain"]["geometry"])
    mid_us = Point(build["mid_us_4326"])
    cand, src = None, None

    for name in ("Profile Lines.shp", "Polyline Layer.shp"):
        if cand is not None:
            break
        for shp in Path(site_dir).rglob(name):
            try:
                gdf = gpd.read_file(shp).to_crs(4326)
            except Exception:  # noqa: BLE001
                continue
            lines = [g for g in gdf.geometry if g is not None and g.geom_type == "LineString"]
            lines = [ln for ln in lines if ln.intersects(dom)]
            for ln in sorted(lines, key=lambda l: -l.intersection(dom).length):
                clipped = ln.intersection(dom)
                if clipped.geom_type == "MultiLineString":
                    clipped = max(clipped.geoms, key=lambda g: g.length)
                if clipped.geom_type == "LineString" and len(clipped.coords) >= 2 \
                        and _spans_domain(clipped, build):
                    cand, src = clipped, "ras_features"
                    break
            if cand is not None:
                break

    if cand is None:
        try:
            from hype_app import hydro

            lon_u, lat_u = build["mid_us_4326"]
            lon_d, lat_d = build["mid_ds_4326"]
            rb = hydro.reach_between({"lat": lat_u, "lon": lon_u},
                                     {"lat": lat_d, "lon": lon_d})
            ln = shape(rb["reach"]["geometry"])
            clipped = ln.intersection(dom)
            if clipped.geom_type == "MultiLineString":
                clipped = max(clipped.geoms, key=lambda g: g.length)
            if clipped.geom_type == "LineString" and len(clipped.coords) >= 2 \
                    and _spans_domain(clipped, build):
                cand, src = clipped, "nhd_trace"
        except Exception as e:  # noqa: BLE001 — network service, never a blocker
            print(f"[centerline] NHD trace unavailable: {e}")

    if cand is None:
        cand, src = _midline_of_sides(build), "corridor_midline"

    if Point(cand.coords[0]).distance(mid_us) > Point(cand.coords[-1]).distance(mid_us):
        cand = LineString(list(cand.coords)[::-1])
    f = _feature(cand)
    f["properties"]["source"] = src
    return f


def stage_geometry(site_dir: Path, work_dir: Path, prj_path: Path) -> dict:
    """Run the geometry stage, persist inputs/geometry.json, return the build dict."""
    raw = read_ras_geometry(prj_path)
    build = split_and_orient(raw)
    reach = centerline(site_dir, build, raw)
    out = {
        "reach": reach,
        "domain": build["domain"],
        "up": build["up"], "left": build["left"],
        "right": build["right"], "down": build["down"],
        "source": {"ghdf": raw["ghdf"], "mode": "ras_import",
                   "crs": str(raw["crs"].to_epsg() or raw["crs"].name)},
    }
    inputs = Path(work_dir) / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / "geometry.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
    return out


def review_card(work_dir: Path, site_row: dict, geom: dict, dem_path=None, wells_shp=None,
                wells_lonlat=None):
    """Gate 1 artifact: one PNG a human can approve before any physics runs.

    wells_lonlat ((lon, lat, name) triples from the WELLS sheet) wins over the
    wells_shp fallback, which only one of the 34 sites even has.
    """
    import geopandas as gpd
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from shapely.geometry import shape

    fig, (ax, ax2) = plt.subplots(
        1, 2, figsize=(13, 7.2), gridspec_kw={"width_ratios": [2.1, 1.0]})
    utm = gpd.GeoSeries([shape(geom["domain"]["geometry"])], crs=4326).estimate_utm_crs()

    def gs(f):
        return gpd.GeoSeries([shape(f["geometry"])], crs=4326).to_crs(utm)

    if dem_path and Path(dem_path).exists():
        try:
            import numpy as np
            import rasterio
            from matplotlib.colors import LightSource

            with rasterio.open(dem_path) as src:
                z = src.read(1, masked=True)
                extent = [src.bounds.left, src.bounds.right, src.bounds.bottom, src.bounds.top]
                hs = LightSource(azdeg=315, altdeg=45).hillshade(
                    np.where(z.mask, np.nan, z.filled(np.nan)), vert_exag=2)
                dem_crs = src.crs
            hs_gdf_extent = gpd.GeoSeries(
                gpd.points_from_xy([extent[0], extent[1]], [extent[2], extent[3]]),
                crs=dem_crs).to_crs(utm)
            ax.imshow(hs, cmap="gray", extent=[hs_gdf_extent.iloc[0].x, hs_gdf_extent.iloc[1].x,
                                               hs_gdf_extent.iloc[0].y, hs_gdf_extent.iloc[1].y],
                      alpha=0.7, zorder=0)
        except Exception as e:  # noqa: BLE001
            ax.set_title(f"(hillshade unavailable: {e})", fontsize=7)
    gs(geom["domain"]).plot(ax=ax, facecolor="#9ecae1", edgecolor="none", alpha=0.30, zorder=1)
    for key, color, lw in (("left", "#1f77b4", 2.2), ("right", "#d62728", 2.2),
                           ("up", "#ff7f0e", 3.0), ("down", "#9467bd", 3.0)):
        gs(geom[key]).plot(ax=ax, color=color, linewidth=lw, zorder=3, label=key)
    gs(geom["reach"]).plot(ax=ax, color="black", linewidth=1.8, linestyle="--",
                           zorder=4, label="reach")
    if wells_lonlat:
        try:
            pts = gpd.GeoSeries(
                gpd.points_from_xy([w[0] for w in wells_lonlat],
                                   [w[1] for w in wells_lonlat]), crs=4326).to_crs(utm)
            ax.scatter([p.x for p in pts], [p.y for p in pts],
                       color="lime", edgecolor="black", s=45, zorder=5)
            for p, w in zip(pts, wells_lonlat):
                ax.annotate(str(w[2] or ""), (p.x, p.y),
                            fontsize=8, xytext=(4, 4), textcoords="offset points")
        except Exception:  # noqa: BLE001
            pass
    elif wells_shp and Path(wells_shp).exists():
        try:
            w = gpd.read_file(wells_shp).to_crs(utm)
            w.plot(ax=ax, color="lime", edgecolor="black", markersize=45, zorder=5)
            name_col = next((c for c in w.columns if c.lower() in ("name", "well", "id")), None)
            for _, row in w.iterrows():
                ax.annotate(str(row[name_col]) if name_col else "", (row.geometry.x, row.geometry.y),
                            fontsize=8, xytext=(4, 4), textcoords="offset points")
        except Exception:  # noqa: BLE001
            pass
    ax.legend(loc="lower right", fontsize=8)
    ax.set_title(f"{site_row['site_id']}: RAS-imported domain (Gate 1 review)", fontsize=12)
    ax.set_aspect("equal")

    ax2.axis("off")
    lines = [f"{site_row['site_id']}  ({site_row.get('river') or '?'})", ""]
    for k in ("geometry_source", "dem_source", "dem_vertical_units", "flow_use_cfs",
              "friction_slope", "manning_n", "kh_m_day", "kv_m_day", "porosity",
              "gradient_left", "gradient_right", "ras_cell_m", "gw_cell_m",
              "gw_mod_depth_m", "layer_thickness_m"):
        v = site_row.get(k)
        if isinstance(v, float):
            v = round(v, 4)
        if isinstance(v, str) and len(v) > 42:
            v = "..." + v[-39:]
        lines.append(f"{k:>20}: {v}")
    lines.append("")
    lines.append(f"{'reach source':>20}: {geom['reach']['properties'].get('source')}")
    ax2.text(0.02, 0.98, "\n".join(lines), family="monospace", fontsize=9,
             va="top", transform=ax2.transAxes)
    out = Path(work_dir) / "review_card.png"
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)
    return out
