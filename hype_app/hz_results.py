"""Readers for the hyporheic-zone analysis artifacts (work_dir/summary/hz/).

Kept separate from results.py (which carries unrelated in-flight edits). All
functions take the hz artifact directory (the "hz_dir" in the analysis result
dict) and return None/empty when an artifact is absent — the analysis writes
per-class files only for non-empty classes.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HZ_CLASSES = ("hyporheic", "losing", "gaining", "throughflow")


def hz_dir_for(work_dir) -> Path:
    return Path(work_dir) / "summary" / "hz"


def stats(hz_dir) -> dict | None:
    """The hz_stats.json contract (per-class stats, counts, knobs, artifact names)."""
    p = Path(hz_dir) / "hz_stats.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def stats_text(hz_dir) -> str | None:
    p = Path(hz_dir) / "hz_stats.txt"
    return p.read_text() if p.exists() else None


def class_paths_geojson(hz_dir, cls: str) -> dict | None:
    """2-D display pathlines for one class as a GeoJSON dict (EPSG:4326, with
    particleid / hz_class / times / length_m feature properties)."""
    p = Path(hz_dir) / f"hz_paths_{cls}_2d.geojson"
    if not p.exists():
        return None
    try:
        gj = json.loads(p.read_text())
        return gj if gj.get("features") else None
    except Exception:  # noqa: BLE001
        return None


def class_paths_gdf(hz_dir):
    """All classes' 3-D display pathlines (model CRS, LineString Z, attrs incl.
    hz_class) — feeds the 3-D line payloads and the box-select intersection."""
    p = Path(hz_dir) / "hz_paths_3d.gpkg"
    if not p.exists():
        return None
    try:
        import geopandas as gpd
        g = gpd.read_file(p)
        return g if len(g) else None
    except Exception:  # noqa: BLE001
        return None


def footprint_geojson(hz_dir, cls: str) -> dict | None:
    """Plan-view zone footprint for one class (EPSG:4326 polygons)."""
    p = Path(hz_dir) / f"hz_foot_{cls}.geojson"
    if not p.exists():
        return None
    try:
        gj = json.loads(p.read_text())
        return gj if gj.get("features") else None
    except Exception:  # noqa: BLE001
        return None


def flow_exchange_geojson(hz_dir, direction: str) -> dict | None:
    """Streambed exchange cells for one direction ("down" = stream water entering the
    aquifer, "up" = aquifer discharging to the stream) — EPSG:4326 cell rectangles with
    a q_m3d property. None before the four-way interface pass existed."""
    p = Path(hz_dir) / f"hz_flow_{direction}.geojson"
    if not p.exists():
        return None
    try:
        gj = json.loads(p.read_text())
        return gj if gj.get("features") else None
    except Exception:  # noqa: BLE001
        return None


def flux_arrays(hz_dir) -> dict | None:
    """Per-release-particle arrays from the flux-weighted boundary-interface pass (§8.3):
    {source_node, weight (m3/day), cls (0 unresolved / 1 returning / 2 losing /
    3 gaining / 4 throughflow), time_days, status, exit_code, origin_code (absent in
    pre-four-way artifacts)}. None when the pass didn't run (no boundary inflow)."""
    p = Path(hz_dir) / "hz_flux.npz"
    if not p.exists():
        return None
    try:
        with np.load(p) as z:
            return {k: np.asarray(z[k]) for k in z.files}
    except Exception:  # noqa: BLE001
        return None


def volume_arrays(hz_dir, cls: str) -> tuple[np.ndarray, np.ndarray] | None:
    """(points (P,3) absolute model coords, quads (Q,4)) of the zone's exterior
    shell, for scene.volume_payload."""
    p = Path(hz_dir) / f"hz_vol_{cls}.npz"
    if not p.exists():
        return None
    try:
        with np.load(p) as z:
            pts, qds = np.asarray(z["points"]), np.asarray(z["quads"])
        return (pts, qds) if pts.size and qds.size else None
    except Exception:  # noqa: BLE001
        return None
