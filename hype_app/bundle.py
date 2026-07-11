"""Zip a HYPE session's workspace for download — and restore one from such a zip.

The session's work lives in an ephemeral temp dir (deleted on session end), so the user
downloads it before leaving. `zip_workspace` gathers the entire session — the drawn reach and
boundaries (serialized from the app's in-memory reactives), the terrain, the HEC-RAS 2025
surface model, and the MODFLOW 6 / MODPATH 7 groundwater model + results — into one archive
organized by pipeline stage. `restore_workspace` is its inverse: it maps the stage-organized
arcnames back onto the raw workspace layout (inputs/, ras/, model/, summary/) and returns the
session state saved in config/state.json, so "Open project" can pick up where a save left off.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = "hype_workspace"

FORMAT_VERSION = 2        # bump when the archive layout or state.json schema changes
#   v1 -> v2 (HYPE revision): adds config/assessment_input.json (frozen run snapshot) +
#   config/scoring_profile.json, and the data_sources/{usgs,nrcs}/, sensitivity/, and
#   6_Site_Report/ trees. v1 archives still open (their new pieces are simply absent).


class ProjectError(ValueError):
    """A project archive can't be opened; str(err) is a user-facing message."""

# reach + boundary reactives -> arcname under ROOT (all EPSG:4326; in-memory only until now).
# `k_zones` is a LIST of features; the rest are single Feature dicts. _write_vectors handles both.
_VECTOR_ARCS = {
    "reach":      "1_Reach_Centerline/reach_centerline.geojson",
    "upstream":   "3_Boundaries/upstream.geojson",
    "left":       "3_Boundaries/left.geojson",
    "right":      "3_Boundaries/right.geojson",
    "downstream": "3_Boundaries/downstream.geojson",
    "domain":     "3_Boundaries/domain.geojson",
    "wse_extent": "3_Boundaries/wse_extent.geojson",
    "k_zones":    "3_Boundaries/k_zones.geojson",
}


def _arc(*parts: str) -> str:
    """Build a zip arcname under ROOT using '/' separators — NEVER os.sep. Handing zipfile a
    Path on Windows would embed backslashes and produce a spec-violating archive."""
    out = [ROOT]
    for p in parts:
        if p:
            out.append(p.strip("/"))
    return "/".join(out)


def _add_file(zf: zipfile.ZipFile, src, arcname: str, seen: set) -> None:
    src = Path(src)
    if not src.is_file() or arcname in seen:   # missing = stage not run yet -> skip
        return
    seen.add(arcname)
    zf.write(src, arcname)


def _add_tree(zf: zipfile.ZipFile, src_dir, arc_prefix: str, seen: set) -> None:
    """Recursively add every file under src_dir, preserving nesting (Geometries/, arrays/, ...)."""
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        return
    for p in sorted(src_dir.rglob("*")):
        if p.is_file():
            rel = p.relative_to(src_dir).as_posix()   # as_posix() => forward slashes on Windows
            _add_file(zf, p, f"{arc_prefix}/{rel}", seen)


def _add_glob(zf: zipfile.ZipFile, src_dir, pattern: str, arc_prefix: str, seen: set) -> None:
    src_dir = Path(src_dir)
    if not src_dir.is_dir():
        return
    for p in sorted(src_dir.glob(pattern)):
        if p.is_file():
            _add_file(zf, p, f"{arc_prefix}/{p.name}", seen)


def _fc(features: list) -> dict:
    """Wrap Feature(s) as a FeatureCollection with a lon/lat CRS member (QGIS/geopandas read it
    cleanly). One shape serves both the singleton lines and the k-zone list."""
    return {
        "type": "FeatureCollection",
        "crs": {"type": "name", "properties": {"name": "urn:ogc:def:crs:OGC:1.3:CRS84"}},
        "features": features,
    }


def _write_vectors(zf: zipfile.ZipFile, vectors: dict, seen: set) -> None:
    for key, sub in _VECTOR_ARCS.items():
        obj = vectors.get(key)
        if not obj:                       # None or [] -> stage not drawn -> skip
            continue
        feats = obj if isinstance(obj, list) else [obj]
        arc = f"{ROOT}/{sub}"
        zf.writestr(arc, json.dumps(_fc(feats), indent=2))
        seen.add(arc)


def _readme(run_config: dict | None, seen: set) -> str:
    present = sorted({a.split("/", 2)[1] for a in seen if "/" in a})   # top-level stage folders
    rc = run_config or {}
    crs = rc.get("working_crs") or {}
    ts = rc.get("generated_at") or datetime.now().isoformat(timespec="seconds")
    lines = [
        "HYPE - Download Workspace",
        f"Generated: {ts}",
        f"Working CRS: EPSG:{crs.get('epsg')} ({crs.get('name')})    Vectors: EPSG:4326",
        "",
        "Folder guide:",
        "  1_Reach_Centerline/  Traced reach centerline (GeoJSON, EPSG:4326).",
        "  2_Terrain/           3DEP DEM, carved DEM + carve difference, reprojected terrain.",
        "  3_Boundaries/        Upstream/Left/Right/Downstream lines, derived domain,",
        "                       wetted extent, K-zones (GeoJSON, EPSG:4326).",
        "  4_Surface_Water/     Full HEC-RAS 2025 project (HEC-RAS/), last-timestep depth/WSE,",
        "                       and the water-surface input rasters.",
        "  5_Groundwater/       model/gwf_workspace (MODFLOW 6) + model/mp7_workspace (MODPATH 7),",
        "                       GW inputs, and Results/ (head, pathlines, hyporheic_zone).",
        "  6_Site_Report/       Generated site summary report (HTML/PDF/CSV/JSON), when produced.",
        "  data_sources/        Recorded USGS StreamStats/NSS and NRCS SDA responses, when fetched.",
        "  sensitivity/         Gradient-sensitivity scenario outputs, when run.",
        "  config/              params.json (engine inputs) + run_config.json (CRS/origin/modes)",
        "                       + state.json (session state - lets HYPE reopen this archive)",
        "                       + assessment_input.json (frozen run snapshot) + scoring_profile.json.",
        "",
        "Reopen this archive any time with Open in the HYPE header to pick up where you left off.",
        "",
        "Note: the two model/ subfolders keep their original names (gwf_workspace, mp7_workspace)",
        "so MODPATH 7's relative links to the MODFLOW head/budget stay valid (re-runnable in place).",
        "",
        "Included in this archive: " + (", ".join(present) or "config only (nothing run yet)"),
        "",
    ]
    return "\n".join(lines)


def zip_workspace(work_dir, *, vectors: dict, params: dict | None = None,
                  run_config: dict | None = None, state: dict | None = None,
                  assessment_input: dict | None = None,
                  scoring_profile: dict | None = None) -> str:
    """Build the organized workspace archive on disk; return the temp-file path.

    The archive is built to a temp file (not io.BytesIO) so peak memory stays flat even when the
    RAS Results HDF + dozens of head rasters run to hundreds of MB; the caller streams the file to
    the browser in chunks, then unlinks it.

    vectors          : name -> GeoJSON Feature dict | list[Feature] | None  (None/[] = skip stage)
    params           : the app's params() dict        -> config/params.json
    run_config       : reproducibility metadata        -> config/run_config.json
    state            : session-state manifest          -> config/state.json (restore reads this)
    assessment_input : frozen AssessmentInputSnapshot  -> config/assessment_input.json  (v2)
    scoring_profile  : HFCI scoring profile            -> config/scoring_profile.json   (v2)
    """
    root = Path(work_dir)
    seen: set = set()
    fd, tmp = tempfile.mkstemp(prefix="hype_ws_", suffix=".zip")
    os.close(fd)                          # mkstemp, not NamedTemporaryFile: Windows can't reopen an open handle
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            # 1_Reach_Centerline + 3_Boundaries — serialized from in-memory reactives
            _write_vectors(zf, vectors, seen)

            # 2_Terrain
            for n in ("dem.tif", "dem_carved.tif", "dem_carve_diff.tif"):
                _add_file(zf, root / "inputs" / n, _arc("2_Terrain", n), seen)
            _add_file(zf, root / "model" / "reprojected_terrain_raster.tif",
                      _arc("2_Terrain", "reprojected_terrain_raster.tif"), seen)

            # 4_Surface_Water — whole HEC-RAS project + convenience result tifs + WSE inputs
            _add_tree(zf, root / "ras", _arc("4_Surface_Water", "HEC-RAS"), seen)
            for n in ("depth_last.tif", "wse_last.tif"):
                _add_file(zf, root / "ras" / n, _arc("4_Surface_Water", n), seen)
            _add_glob(zf, root / "inputs", "wse_*.tif",
                      _arc("4_Surface_Water", "water_surface_inputs"), seen)

            # 5_Groundwater — MODFLOW6/MODPATH7 workspaces (original names) + GW inputs + Results
            _add_tree(zf, root / "model" / "gwf_workspace",
                      _arc("5_Groundwater", "model", "gwf_workspace"), seen)
            _add_tree(zf, root / "model" / "mp7_workspace",
                      _arc("5_Groundwater", "model", "mp7_workspace"), seen)
            for n in ("reprojected_water_surface_raster.tif",
                      "cropped_water_surface_raster.tif", "grid_points_elevation.csv"):
                _add_file(zf, root / "model" / n, _arc("5_Groundwater", "inputs", n), seen)
            _add_tree(zf, root / "summary" / "head",
                      _arc("5_Groundwater", "Results", "head"), seen)
            _add_glob(zf, root / "summary", "Forward_*",
                      _arc("5_Groundwater", "Results", "pathlines"), seen)
            _add_tree(zf, root / "summary" / "hz",
                      _arc("5_Groundwater", "Results", "hyporheic_zone"), seen)

            # v2 trees — recorded external data, sensitivity outputs, and the site report.
            # Each skips silently when its source dir doesn't exist yet (feature not run).
            _add_tree(zf, root / "data_sources", _arc("data_sources"), seen)
            _add_tree(zf, root / "sensitivity", _arc("sensitivity"), seen)
            _add_tree(zf, root / "report", _arc("6_Site_Report"), seen)

            # config/ + README (writestr => arcname is a str with '/', always portable)
            if params is not None:
                zf.writestr(f"{ROOT}/config/params.json",
                            json.dumps(params, indent=2, default=str))
            if run_config is not None:
                zf.writestr(f"{ROOT}/config/run_config.json",
                            json.dumps(run_config, indent=2, default=str))
            if state is not None:
                zf.writestr(f"{ROOT}/config/state.json",
                            json.dumps(state, indent=2, default=str))
            if assessment_input is not None:
                zf.writestr(f"{ROOT}/config/assessment_input.json",
                            json.dumps(assessment_input, indent=2, default=str))
            if scoring_profile is not None:
                zf.writestr(f"{ROOT}/config/scoring_profile.json",
                            json.dumps(scoring_profile, indent=2, default=str))
            zf.writestr(f"{ROOT}/README.txt", _readme(run_config, seen))
        return tmp
    except BaseException:
        try:
            os.unlink(tmp)                # never leak the temp file on failure
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# restore side — the inverse of the layout written above. Keep the two in sync:
# every _add_file/_add_tree/_add_glob destination in zip_workspace must have a
# rule here, or restored projects silently lose that artifact.
# ---------------------------------------------------------------------------

# exact arcname (under ROOT) -> workspace-relative target
_RESTORE_FILES = {
    "2_Terrain/dem.tif":                        "inputs/dem.tif",
    "2_Terrain/dem_carved.tif":                 "inputs/dem_carved.tif",
    "2_Terrain/dem_carve_diff.tif":             "inputs/dem_carve_diff.tif",
    "2_Terrain/reprojected_terrain_raster.tif": "model/reprojected_terrain_raster.tif",
    # top-level depth/wse are convenience duplicates of the HEC-RAS/ copies -> same target
    "4_Surface_Water/depth_last.tif":           "ras/depth_last.tif",
    "4_Surface_Water/wse_last.tif":             "ras/wse_last.tif",
    "5_Groundwater/inputs/reprojected_water_surface_raster.tif":
        "model/reprojected_water_surface_raster.tif",
    "5_Groundwater/inputs/cropped_water_surface_raster.tif":
        "model/cropped_water_surface_raster.tif",
    "5_Groundwater/inputs/grid_points_elevation.csv": "model/grid_points_elevation.csv",
}

# arcname prefix (under ROOT) -> workspace-relative dir; nesting under the prefix is preserved
_RESTORE_TREES = (
    ("4_Surface_Water/HEC-RAS/",              "ras/"),
    ("4_Surface_Water/water_surface_inputs/", "inputs/"),
    ("5_Groundwater/model/gwf_workspace/",    "model/gwf_workspace/"),
    ("5_Groundwater/model/mp7_workspace/",    "model/mp7_workspace/"),
    ("5_Groundwater/Results/head/",           "summary/head/"),
    ("5_Groundwater/Results/pathlines/",      "summary/"),
    ("5_Groundwater/Results/hyporheic_zone/", "summary/hz/"),
    # v2 trees
    ("data_sources/",                         "data_sources/"),
    ("sensitivity/",                          "sensitivity/"),
    ("6_Site_Report/",                        "report/"),
)


def _target_for(rel: str) -> str | None:
    """Workspace-relative target for one arcname (already stripped of ROOT), or None to skip."""
    hit = _RESTORE_FILES.get(rel)
    if hit:
        return hit
    for prefix, dest in _RESTORE_TREES:
        if rel.startswith(prefix):
            return dest + rel[len(prefix):]
    return None                       # config/, README, vectors, or a future arc we don't know


def restore_workspace(zip_path, work_dir) -> dict:
    """Extract a HYPE project archive back into a session workspace.

    Returns {"state": dict, "vectors": {name: Feature | [Feature]}, "params": dict|None,
    "run_config": dict|None, "assessment_input": dict|None, "scoring_profile": dict|None,
    "extracted": int}. `assessment_input`/`scoring_profile` are None for v1 archives (the legacy
    adapter — their pieces simply weren't saved). Raises ProjectError with a user-facing message
    when the file isn't a reopenable HYPE project.
    """
    root = Path(work_dir).resolve()
    try:
        zf = zipfile.ZipFile(zip_path)
    except (zipfile.BadZipFile, OSError) as e:
        raise ProjectError("That file isn't a readable HYPE project archive.") from e
    with zf:
        names = set(zf.namelist())
        if not any(n.startswith(f"{ROOT}/") for n in names):
            raise ProjectError("That zip wasn't made by HYPE (no hype_workspace folder inside).")
        if f"{ROOT}/config/state.json" not in names:
            raise ProjectError("This archive predates Save — it has the files but not the "
                               "session state. Re-create it with Save from a current session.")

        def _json(arc: str):
            with zf.open(arc) as f:
                return json.load(f)

        state = _json(f"{ROOT}/config/state.json")
        fmt = state.get("format_version")
        if isinstance(fmt, int) and fmt > FORMAT_VERSION:
            raise ProjectError("This project was saved by a newer version of HYPE — "
                               "update the app to open it.")

        vectors: dict = {}
        for key, sub in _VECTOR_ARCS.items():
            arc = f"{ROOT}/{sub}"
            if arc not in names:
                continue
            feats = (_json(arc) or {}).get("features") or []
            if feats:
                vectors[key] = feats if key == "k_zones" else feats[0]

        extracted = 0
        done: set = set()             # dedupes the depth/wse convenience copies
        for info in zf.infolist():
            if info.is_dir() or not info.filename.startswith(f"{ROOT}/"):
                continue
            target_rel = _target_for(info.filename[len(ROOT) + 1:])
            if target_rel is None or target_rel in done:
                continue
            target = (root / target_rel).resolve()
            if not target.is_relative_to(root):     # zip-slip guard
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, 1024 * 1024)
            done.add(target_rel)
            extracted += 1

        def _opt(arc: str):
            return _json(arc) if arc in names else None

        params = _opt(f"{ROOT}/config/params.json")
        run_config = _opt(f"{ROOT}/config/run_config.json")
        assessment_input = _opt(f"{ROOT}/config/assessment_input.json")   # None on v1 (legacy adapter)
        scoring_profile = _opt(f"{ROOT}/config/scoring_profile.json")

    return {"state": state, "vectors": vectors, "params": params,
            "run_config": run_config, "assessment_input": assessment_input,
            "scoring_profile": scoring_profile, "extracted": extracted}
