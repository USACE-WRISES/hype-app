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


def flux_metrics(hz_stats: dict, hz_dir, *, transit_rows: bool = True) -> dict:
    """Flux-weighted §8.3 interface-pass metrics as a dict bundle: exchange (m3/s),
    transit_times, transit_weights, path_depths (returning subset; None when the depth pass did
    not run), censored, transit_rows. The model budget is m3/day; the canonical results +
    streamflow are m3/s, hence the /86400.

    Lives here rather than in `app.py` because it captures nothing from the session: it is a
    pure read of one run directory, and the scenario-envelope build calls it from the report
    WORKER THREAD, where a `server()` closure has no business being.

    `transit_rows=False` skips the per-particle row list. Only the Basecase needs it (the
    transit CSV and the RTD figure); building it for each alternative in a sweep would allocate
    a dict per particle per scenario for data nothing reads.

    THE WEIGHTS STAY RAW m3/day. `functions.screen` requires that basis (its Sigma-w is Q_HEF);
    only `ExchangeAccounting` and the per-row `flow_weight` below are converted."""
    from .metrics import ExchangeAccounting
    DAY = 86400.0
    out = {"exchange": None, "transit_times": None, "transit_weights": None,
           "path_depths": None, "path_lengths": None, "censored": None, "transit_rows": [],
           "downwelling_cells": None, "iface_ppc": None}
    acct = ((hz_stats or {}).get("flux") or {}).get("accounting") \
        if isinstance((hz_stats or {}).get("flux"), dict) else None
    if acct:
        # Provenance of the returning-path count, straight off the saved accounting so an
        # older project reports whatever density IT was run at.
        out["downwelling_cells"] = acct.get("n_stream_cells_downwelling")
        out["iface_ppc"] = acct.get("particles_per_cell")
        out["exchange"] = ExchangeAccounting(
            total_downwelling=acct["total_downwelling"] / DAY,
            returning_hyporheic=acct["returning"] / DAY,
            losing_to_sides=acct["losing"] / DAY,
            unresolved=acct["unresolved"] / DAY)
        if acct.get("total_downwelling"):
            out["censored"] = acct["unresolved"] / acct["total_downwelling"]
    fx = flux_arrays(hz_dir) if hz_dir else None
    if fx is not None:
        ret = fx["cls"] == 1
        has_depth = "max_depth_m" in fx
        # Path LENGTH, for the particulate module. Written by the same optional pathline pass
        # as the depth and absent from artifacts saved before it existed, so the microplastic
        # capture check degrades to "re-run the calculations" instead of breaking.
        has_length = "path_length_m" in fx
        if ret.any():
            out["transit_times"] = fx["time_days"][ret]
            out["transit_weights"] = fx["weight"][ret]
            if has_depth:
                out["path_depths"] = fx["max_depth_m"][ret]
            if has_length:
                out["path_lengths"] = fx["path_length_m"][ret]
        cls_names = {0: "unresolved", 1: "returning", 2: "losing",
                     3: "gaining", 4: "throughflow"}
        if transit_rows:
            out["transit_rows"] = [
                {"particle_id": int(i), "source_cell": int(fx["source_node"][i]),
                 "flow_weight": float(fx["weight"][i] / DAY),
                 "endpoint_class": cls_names.get(int(fx["cls"][i]), "unresolved"),
                 "transit_time_days": float(fx["time_days"][i]),
                 "max_depth_m": (float(fx["max_depth_m"][i]) if has_depth
                                 and fx["max_depth_m"][i] == fx["max_depth_m"][i] else None),
                 "termination": int(fx["status"][i])}
                for i in range(len(fx["cls"]))]
    return out


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
