"""Observation wells: computed-vs-observed head sampling for groundwater calibration.

Wells are OBSERVATION data. They never feed the model, never enter the input snapshot,
and never touch input_hash — they only read a finished Basecase run (per-layer head
GeoTIFFs + the binary grid file) and report back. Everything here is pure so the layer
math and gradient accounting stay unit-testable without a run.
"""
from __future__ import annotations

import re
from pathlib import Path

#: A pair tighter than this is a datum problem, not a gradient (metres).
MIN_PAIR_DISTANCE_M = 0.01

_TOL = 1e-6


def default_name(existing) -> str:
    """First free "OW-<n>" not already taken (case-insensitive)."""
    taken = set()
    for nm in existing or []:
        m = re.fullmatch(r"OW-(\d+)", str(nm).strip(), flags=re.IGNORECASE)
        if m:
            taken.add(int(m.group(1)))
    n = 1
    while n in taken:
        n += 1
    return f"OW-{n}"


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None                        # NaN -> None


def normalize_wells(raw) -> list[dict]:
    """Restore hygiene for saved well records: coerce numerics, fill missing keys,
    drop rows without an id or a location, dedupe ids (first wins)."""
    out, seen = [], set()
    for w in raw or []:
        if not isinstance(w, dict):
            continue
        uid = str(w.get("id") or "").strip()
        lat, lon = _num(w.get("lat")), _num(w.get("lon"))
        if not uid or uid in seen or lat is None or lon is None:
            continue
        seen.add(uid)
        name = str(w.get("name") or "").strip()
        out.append({"id": uid,
                    "name": name or default_name([x["name"] for x in out]),
                    "lat": lat, "lon": lon,
                    "screen_elev": _num(w.get("screen_elev")),
                    "obs_head": _num(w.get("obs_head"))})
    return out


def normalize_pairs(raw, well_ids) -> list[dict]:
    """Keep only pairs whose two DISTINCT wells still exist; dedupe unordered."""
    ids = set(well_ids or ())
    out, seen = [], set()
    for p in raw or []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip()
        a, b = str(p.get("a") or ""), str(p.get("b") or "")
        key = frozenset((a, b))
        if not pid or a == b or a not in ids or b not in ids or key in seen:
            continue
        seen.add(key)
        out.append({"id": pid, "a": a, "b": b})
    return out


def load_grid(gwf_ws) -> dict | None:
    """Layer geometry from the run's binary grid file, in ENGINE order (row 0 = south):
    {"top": (nrow, ncol), "botm": (nlay, nrow, ncol), "idomain": same, "nlay": int}.
    None when the workspace or grb is missing/unreadable."""
    import warnings

    import numpy as np

    try:
        grb_path = next(Path(gwf_ws).glob("*.dis.grb"), None)
        if grb_path is None:
            return None
        from flopy.mf6.utils import MfGrdFile
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")             # flopy shape-set deprecation noise
            mg = MfGrdFile(str(grb_path), verbose=False).modelgrid
            botm = np.asarray(mg.botm, dtype=float)
            return {"top": np.asarray(mg.top, dtype=float),
                    "botm": botm,
                    "idomain": np.asarray(mg.idomain).reshape(botm.shape),
                    "nlay": int(botm.shape[0])}
    except Exception:  # noqa: BLE001 — geometry is best-effort; callers report a reason
        return None


def locate_cell(tif_path, lon, lat):
    """(row, col) of the head-raster cell containing the WGS84 point, or None outside.
    The engine's head tifs are deliberately SOUTH-UP (+dy transform, origin at ymin) so
    their (row, col) EQUALS the engine's south-first grid indices — src.index() reads the
    affine, so no flip is ever applied here."""
    import rasterio
    from pyproj import Transformer

    with rasterio.open(tif_path) as src:
        x, y = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True).transform(lon, lat)
        row, col = src.index(x, y)
        if 0 <= row < src.height and 0 <= col < src.width:
            return int(row), int(col)
    return None


def layer_for_elevation(top, botm, idomain, row, col, elev, tol=_TOL):
    """(0-based layer, None) for the TOPMOST layer whose interval contains ``elev`` at the
    cell, else (None, reason). Strict — no engine-style clamping: a screen above terrain or
    below the model bottom is a data problem the user should see, not silently relocate.
    An interface elevation belongs to the upper layer (topmost-first scan)."""
    col_dom = idomain[:, row, col]
    if not (col_dom == 1).any():
        return None, "outside active model area"
    terrain = float(top[row, col])
    if elev > terrain + tol:
        return None, "above terrain at this location"
    if elev < float(botm[-1, row, col]) - tol:
        return None, "below model bottom"
    for k in range(botm.shape[0]):
        t_k = terrain if k == 0 else float(botm[k - 1, row, col])
        b_k = float(botm[k, row, col])
        if b_k - tol <= elev <= t_k + tol and t_k > b_k:
            if col_dom[k] != 1:
                return None, "inactive cell at screen elevation"
            return k, None
    # Every interval was degenerate or missed (above-ground stack at a low-terrain cell):
    # the elevation sits in deactivated air between terrain and the first active interface.
    return None, "inactive cell at screen elevation"


def sample_head_tif(tifs, k, row, col):
    """Head value from layer ``k`` (0-based) at the cell, or None when invalid/dry.
    Validity matches results._valid_mask: finite, not nodata, above the -9999 sentinel."""
    import rasterio
    from rasterio.windows import Window

    if not tifs or k >= len(tifs):
        return None
    with rasterio.open(tifs[k]) as src:
        a = src.read(1, window=Window(col, row, 1, 1))
        if a.size != 1:
            return None
        v = float(a[0, 0])
        nod = src.nodata
    if v != v or (nod is not None and v == nod) or v <= -9000.0:
        return None
    return v


def sample_wells(wells, *, crs=None, tifs=None, grid=None, no_run=False) -> list[dict]:
    """Per-well sample rows for the pane, report, and pair math.

    Rows: {"id","name","lat","lon","x","y","screen_elev","obs_head","layer" (1-based|None),
    "computed","residual","reason"}. x/y (projected metres, for pair distances) are filled
    whenever ``crs`` is known, run or no run. Reason priority: no run > missing files >
    missing screen elevation > outside grid > layer reason > dry cell."""
    xs = ys = None
    if crs is not None and wells:
        try:
            from pyproj import Transformer
            tr = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
            xs, ys = tr.transform([w["lon"] for w in wells], [w["lat"] for w in wells])
        except Exception:  # noqa: BLE001 — distances degrade to n/a
            xs = ys = None

    out = []
    for i, w in enumerate(wells or []):
        row = {"id": w["id"], "name": w.get("name") or "", "lat": w["lat"], "lon": w["lon"],
               "x": float(xs[i]) if xs is not None else None,
               "y": float(ys[i]) if ys is not None else None,
               "screen_elev": w.get("screen_elev"), "obs_head": w.get("obs_head"),
               "layer": None, "computed": None, "residual": None, "reason": None}
        out.append(row)
        if no_run:
            row["reason"] = "no groundwater run"
            continue
        if not tifs or grid is None:
            row["reason"] = "model output files not found"
            continue
        if w.get("screen_elev") is None:
            row["reason"] = "enter screen elevation"
            continue
        try:
            rc = locate_cell(tifs[0], w["lon"], w["lat"])
        except Exception:  # noqa: BLE001 — unreadable raster
            row["reason"] = "model output files not found"
            continue
        if rc is None:
            row["reason"] = "outside model grid"
            continue
        k, why = layer_for_elevation(grid["top"], grid["botm"], grid["idomain"],
                                     rc[0], rc[1], float(w["screen_elev"]))
        if k is None:
            row["reason"] = why
            continue
        if k >= len(tifs):
            row["reason"] = "model output files not found"
            continue
        try:
            v = sample_head_tif(tifs, k, rc[0], rc[1])
        except Exception:  # noqa: BLE001
            row["reason"] = "model output files not found"
            continue
        row["layer"] = k + 1
        if v is None:
            row["reason"] = "dry cell"
            continue
        row["computed"] = v
        if w.get("obs_head") is not None:
            row["residual"] = v - float(w["obs_head"])
    return out


def pair_rows(pairs, rows_by_id) -> list[dict]:
    """Tracked-pair rows: distance in metres plus computed/observed gradients, each n/a
    independently when its inputs are missing. Gradient sign is (A - B) / distance."""
    import math

    out = []
    for p in pairs or []:
        ra, rb = rows_by_id.get(p.get("a")), rows_by_id.get(p.get("b"))
        row = {"id": p.get("id"), "a": p.get("a"), "b": p.get("b"),
               "name_a": (ra or {}).get("name") or "?",
               "name_b": (rb or {}).get("name") or "?",
               "distance": None, "computed_gradient": None,
               "observed_gradient": None, "reason": None}
        out.append(row)
        if ra is None or rb is None:
            row["reason"] = "well removed"
            continue
        if None in (ra["x"], ra["y"], rb["x"], rb["y"]):
            continue                                    # no CRS yet — distance stays n/a
        d = math.hypot(ra["x"] - rb["x"], ra["y"] - rb["y"])
        if d < MIN_PAIR_DISTANCE_M:
            row["reason"] = "wells coincide"
            continue
        row["distance"] = d
        if ra["computed"] is not None and rb["computed"] is not None:
            row["computed_gradient"] = (ra["computed"] - rb["computed"]) / d
        if ra["obs_head"] is not None and rb["obs_head"] is not None:
            row["observed_gradient"] = (ra["obs_head"] - rb["obs_head"]) / d
    return out


def residual_stats(rows) -> dict | None:
    """{"n","mean_error","mean_abs_error","rmse"} over wells with BOTH heads, else None."""
    res = [r["residual"] for r in rows or [] if r.get("residual") is not None]
    if not res:
        return None
    n = len(res)
    return {"n": n,
            "mean_error": sum(res) / n,
            "mean_abs_error": sum(abs(r) for r in res) / n,
            "rmse": (sum(r * r for r in res) / n) ** 0.5}
