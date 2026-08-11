"""Groundwater observation-well harvest from the legacy GMS runs.

Every one of the 34 site folders carries a GMS MODFLOW project whose .hob file
records the observation wells: a #GMSCOMMENT header per well with the true XY
(in the GMS model CRS, Texas State Plane Central ftUS) and a data line with the
observed head (ftUS). The hob obs names (hed1..hedN) are NOT in BR order - the
LL01096 TransObservation.csv proves the scramble (hed3=BR4, hed4=BR5, hed5=BR3)
- so real names come from XY matching against named point sources, tiered:

  1. TransObservation.csv (same native XY as the hob, byte-identical for LL01096)
  2. GMS GIS Spikes point shapefile (all 34 sites; Name = "<SITE> BRn", WGS84)
  3. Wells*.shp with a NAME column (LL01096 only)
  4. keep the hob obs name

Pure file-reading helpers, no engines, no app imports. Heavy deps (pyproj,
geopandas) import inside functions so `import hob` stays cheap for tests.
"""
from __future__ import annotations

import math
import re
from pathlib import Path

# One well per GMSCOMMENT line: guid POINT id, x, y time obname
_GMSCOMMENT_RE = re.compile(
    r"^#GMSCOMMENT\s+\S+\s+POINT\s+\d+\s*,\s*([\d.eE+-]+)\s*,\s*([\d.eE+-]+)\s+\S+\s+(\S+)\s*$")
# UUID-named dirs are GMS run copies (CR08791, CH00365), never the source model.
_UUID_DIR_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                          r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# TransObservation.csv name rows: NAME, id, x, y (data rows between them are numeric-first)
_TRANSOBS_RE = re.compile(
    r"^\s*([A-Za-z][\w -]*?)\s*,\s*\d+\s*,\s*([\d.eE+-]+)\s*,\s*([\d.eE+-]+)")

# Match tolerances. TransObservation XY is the hob XY verbatim, so native-unit
# 2.0 is generous either way (2 ft or 2 m). Spikes/Wells matches are geodesic m.
TOL_NATIVE = 2.0
TOL_SPIKES_M = 2.0
TOL_WELLS_SHP_M = 5.0
DEDUPE_M = 0.5
CENTROID_GATE_M = 3000.0


def parse_hob(path: Path) -> list[dict]:
    """[{obname, x, y, head_ft}] from one GMS-written MODFLOW .hob, comment order.

    Wells whose data line is missing keep head_ft None; data lines are the
    12-token single-time rows every one of the 40 site hobs uses.
    """
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    order: list[str] = []
    xy: dict[str, tuple[float, float]] = {}
    for line in text.splitlines():
        m = _GMSCOMMENT_RE.match(line.strip())
        if m:
            name = m.group(3)
            if name not in xy:
                order.append(name)
                xy[name] = (float(m.group(1)), float(m.group(2)))
    heads: dict[str, float] = {}
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        toks = line.split()
        if len(toks) == 12 and toks[0] in xy and toks[0] not in heads:
            try:
                heads[toks[0]] = float(toks[8])
            except ValueError:
                continue
    return [{"obname": n, "x": xy[n][0], "y": xy[n][1], "head_ft": heads.get(n)}
            for n in order]


def _read_wkt_prj(path: Path) -> str | None:
    try:
        text = Path(path).read_text(errors="replace").strip().lstrip("﻿")
    except OSError:
        return None
    return text if text.startswith(("PROJCS", "PROJCRS", "COMPD_CS")) else None


def gms_crs(hob_path: Path):
    """Projected CRS for a hob's XY, from .prj files near it. None when unresolved.

    Tiers: GWDomain.prj, then a *102739* named prj, then any projected-WKT prj
    under the same GMS root. GEOGCS-only prjs (the WGS84 spikes) are skipped, as
    is the MODFLOW dir itself (its <site>.prj is a GMS package file, not WKT).
    """
    import pyproj

    hob_path = Path(hob_path)
    root = hob_path.parent.parent  # <gms_root>/<site>_MODFLOW/<site>.hob
    cands: list[Path] = []
    for pattern in ("GWDomain.prj", "*102739*.prj", "*.prj"):
        found = [p for p in sorted(root.rglob(pattern))
                 if p.parent != hob_path.parent]
        cands.extend(p for p in found if p not in cands)
    for p in cands:
        wkt = _read_wkt_prj(p)
        if not wkt:
            continue
        try:
            return pyproj.CRS.from_user_input(wkt)
        except Exception:  # noqa: BLE001 - malformed prj, keep looking
            continue
    return None


def transobs_points(site_dir: Path) -> list[tuple[str, float, float]]:
    """[(name, x, y)] in NATIVE model units from any TransObservation*.csv."""
    out: list[tuple[str, float, float]] = []
    for csv in sorted(Path(site_dir).rglob("TransObservation*.csv")):
        try:
            text = csv.read_text(errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            m = _TRANSOBS_RE.match(line)
            if m:
                out.append((m.group(1).strip(), float(m.group(2)), float(m.group(3))))
    return out


def shapefile_points(site_dir: Path, patterns: tuple[str, ...],
                     strip_prefix: str = "") -> list[tuple[str, float, float]]:
    """[(name, lon, lat)] from the first matching point shapefile with a name column."""
    import geopandas as gpd

    for pattern in patterns:
        for shp in sorted(Path(site_dir).rglob(pattern)):
            try:
                gdf = gpd.read_file(shp)
            except Exception:  # noqa: BLE001 - unreadable sidecar sets happen
                continue
            name_col = next((c for c in gdf.columns if c.lower() == "name"), None)
            if name_col is None or not len(gdf):
                continue
            if gdf.crs is not None and gdf.crs.to_epsg() != 4326:
                try:
                    gdf = gdf.to_crs(4326)
                except Exception:  # noqa: BLE001
                    continue
            out = []
            for _, row in gdf.iterrows():
                geom = row.geometry
                if geom is None or geom.geom_type != "Point":
                    continue
                nm = str(row[name_col] or "").strip()
                if strip_prefix and nm.lower().startswith(strip_prefix.lower()):
                    nm = nm[len(strip_prefix):].strip()
                if nm:
                    out.append((nm, float(geom.x), float(geom.y)))
            if out:
                return out
    return []


def match_names_aligned(points: list[tuple[float, float]],
                        candidates: list[tuple[str, float, float]],
                        tol: float, dist_fn) -> dict[int, str]:
    """match_names after removing a constant offset between the two clouds.

    Some sites' spike points sit 15-20 m off the hob XY as one rigid shift
    (CR07230). Centroid-align the candidate cloud onto the point cloud, then
    match at the normal tolerance. Only meaningful when the clouds pair one to
    one, so this requires equal counts (>= 3) and full coverage - a partial
    result is discarded rather than trusted.
    """
    if len(points) != len(candidates) or len(points) < 3:
        return {}
    px = sum(p[0] for p in points) / len(points)
    py = sum(p[1] for p in points) / len(points)
    cx = sum(c[1] for c in candidates) / len(candidates)
    cy = sum(c[2] for c in candidates) / len(candidates)
    shifted = [(n, x + (px - cx), y + (py - cy)) for n, x, y in candidates]
    got = match_names(points, shifted, tol, dist_fn)
    return got if len(got) == len(points) else {}


def match_names(points: list[tuple[float, float]],
                candidates: list[tuple[str, float, float]],
                tol: float, dist_fn) -> dict[int, str]:
    """Greedy one-to-one nearest matching: {point_index: name} within tol."""
    pairs = []
    for i, (px, py) in enumerate(points):
        for j, (_, cx, cy) in enumerate(candidates):
            d = dist_fn(px, py, cx, cy)
            if d <= tol:
                pairs.append((d, i, j))
    pairs.sort()
    used_p: set[int] = set()
    used_c: set[int] = set()
    out: dict[int, str] = {}
    for _, i, j in pairs:
        if i in used_p or j in used_c:
            continue
        used_p.add(i)
        used_c.add(j)
        out[i] = candidates[j][0]
    return out


def _planar(px, py, cx, cy):
    return math.hypot(px - cx, py - cy)


def _geodesic():
    import pyproj

    geod = pyproj.Geod(ellps="WGS84")

    def dist(lon1, lat1, lon2, lat2):
        return geod.inv(lon1, lat1, lon2, lat2)[2]

    return dist


def find_hobs(site_dir: Path) -> list[Path]:
    """All source .hob files, skipping GMS run-copy dirs (UUID-named components)."""
    out = []
    for p in sorted(Path(site_dir).rglob("*.hob")):
        if any(_UUID_DIR_RE.match(part) for part in p.parts):
            continue
        out.append(p)
    return out


def _variant_label(hob_path: Path, site_id: str) -> str:
    """Short stable tag for a non-primary hob (PR01540_2 -> "2", GMS_Clone -> itself)."""
    name = hob_path.parent.name
    trimmed = re.sub(r"_MODFLOW$", "", name, flags=re.IGNORECASE)
    trimmed = re.sub(rf"^{re.escape(site_id)}_?", "", trimmed, flags=re.IGNORECASE)
    return trimmed or hob_path.parent.parent.name


def harvest_site_wells(site_dir: Path, site_id: str,
                       centroid_lat=None, centroid_lon=None) -> list[dict]:
    """The per-site well list for the extracted JSON and the WELLS sheet.

    obs_name is THE stable key downstream (deterministic app well ids and the
    workbook's preserved hand edits both hang off it) - primary-hob wells keep
    the bare hob name, extra-variant wells get "<variant>:<obname>".
    """
    parsed: list[tuple[Path, list[dict]]] = []
    for h in find_hobs(site_dir):
        obs = parse_hob(h)
        if obs:
            parsed.append((h, obs))
    if not parsed:
        return []
    # Primary = most observations; ties prefer the shortest path so the
    # unsuffixed original (PR01540_MODFLOW) beats its _2/_3 variants.
    parsed.sort(key=lambda t: (-len(t[1]), len(str(t[0])), str(t[0]).lower()))

    site_dir = Path(site_dir)
    geodist = _geodesic()
    trans_cands = transobs_points(site_dir)
    spike_cands = shapefile_points(
        site_dir, ("Spikes/*point*.shp", "Spikes/*.shp"), strip_prefix=f"{site_id} ")
    shp_cands = shapefile_points(site_dir, ("Wells*.shp",))

    wells: list[dict] = []
    for rank, (hob_path, obs) in enumerate(parsed):
        crs = gms_crs(hob_path)
        lonlat: list[tuple[float, float] | None] = [None] * len(obs)
        if crs is not None:
            import pyproj

            tr = pyproj.Transformer.from_crs(crs, 4326, always_xy=True)
            for i, w in enumerate(obs):
                lon, lat = tr.transform(w["x"], w["y"])
                lonlat[i] = (float(lon), float(lat))
        native_named = match_names([(w["x"], w["y"]) for w in obs],
                                   trans_cands, TOL_NATIVE, _planar)
        label = _variant_label(hob_path, site_id)
        for i, w in enumerate(obs):
            ll = lonlat[i]
            if rank > 0:
                if ll is None:
                    continue  # a variant well we cannot place adds nothing
                dup = any(
                    wl.get("lat") is not None
                    and geodist(ll[0], ll[1], wl["lon"], wl["lat"]) <= DEDUPE_M
                    for wl in wells)
                if dup:
                    continue
            rec = {
                "obs_name": w["obname"] if rank == 0 else f"{label}:{w['obname']}",
                "name": None,
                "name_source": None,
                "lat": None if ll is None else round(ll[1], 8),
                "lon": None if ll is None else round(ll[0], 8),
                "obs_head_ft": w["head_ft"],
                "dist_centroid_m": None,
                "include": "Yes",
                "note": None,
                "source_hob": hob_path.relative_to(site_dir).as_posix(),
            }
            if i in native_named:
                rec["name"] = native_named[i]
                rec["name_source"] = "transobs"
            if ll is None:
                rec["include"] = "No"
                rec["note"] = "no CRS found for hob XY"
            wells.append(rec)

    # Remaining unnamed wells: spikes shapefile, then Wells*.shp, else hob name.
    for cands, source, tol in ((spike_cands, "spikes", TOL_SPIKES_M),
                               (shp_cands, "wells_shp", TOL_WELLS_SHP_M)):
        if not cands:
            continue
        idxs = [i for i, w in enumerate(wells)
                if w["name"] is None and w["lat"] is not None]
        pts = [(wells[i]["lon"], wells[i]["lat"]) for i in idxs]
        matched = match_names(pts, cands, tol, geodist)
        if not matched:
            aligned = match_names_aligned(pts, cands, tol, geodist)
            if aligned:
                matched = aligned
                source = f"{source}_aligned"
        for k, nm in matched.items():
            wells[idxs[k]]["name"] = nm
            wells[idxs[k]]["name_source"] = source
    for w in wells:
        if w["name"] is None:
            w["name"] = w["obs_name"]
            w["name_source"] = "hob"

    if centroid_lat is not None and centroid_lon is not None:
        for w in wells:
            if w["lat"] is None:
                continue
            d = geodist(w["lon"], w["lat"], centroid_lon, centroid_lat)
            w["dist_centroid_m"] = round(d, 1)
            if d > CENTROID_GATE_M:
                w["include"] = "No"
                w["note"] = "outside 3 km of site centroid, check CRS"
    return wells
