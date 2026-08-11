"""Converters from WELLS-sheet rows to HYPE app state, and the bundle-state merge.

The factory's bundle stage used to write a 3-key state dict, clobbering
anything the app had saved into the same .hype (obs_wells the user tuned, map
layers, view prefs - the app's _project_state is ~52 keys). merge_state is the
repair: every app-authored key passes through untouched, the factory overlays
only the keys it owns, and wells merge per-record so a re-run never duplicates
or wipes in-app edits.

All pure. State is handled in TOKEN space ($WORKSPACE$/... paths) because
bundle._read_bundle returns state.json exactly as stored - only the app
detokenizes on open.
"""
from __future__ import annotations

import hashlib

# US survey foot: the GMS models' unit (their prj declares it), NOT the
# international foot. Heads and screen elevations convert with this.
FT_US = 0.3048006096012192


def well_id(site_id: str, obs_name: str) -> str:
    """Deterministic 8-hex id, same shape the app mints (uuid4().hex[:8]).

    Keyed by (site_id, obs_name) so re-running the bundle stage merges into the
    same records instead of appending duplicates. Never churn obs_name.
    """
    return hashlib.sha1(f"{site_id}|{obs_name}".encode()).hexdigest()[:8]


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


def _included(v) -> bool:
    if v is True:
        return True
    return str(v or "").strip().lower() in ("yes", "y", "true", "1")


def app_well_records(site_id: str, sheet_rows: list[dict] | None) -> list[dict]:
    """WELLS sheet rows -> app obs_wells records (EPSG:4326, meters).

    Only include=Yes rows with usable coordinates convert. The output is
    normalize_wells-stable: hype_app.wells.normalize_wells(records) == records.
    """
    out = []
    for r in sheet_rows or []:
        if not _included(r.get("include")):
            continue
        lat, lon = _num(r.get("lat")), _num(r.get("lon"))
        if lat is None or lon is None:
            continue
        obs_name = str(r.get("obs_name") or "").strip()
        if not obs_name:
            continue
        se = _num(r.get("screen_elev_ft"))
        oh = _num(r.get("obs_head_ft"))
        name = str(r.get("name") or "").strip() or obs_name
        out.append({
            "id": well_id(site_id, obs_name),
            "name": name,
            "lat": lat,
            "lon": lon,
            "screen_elev": None if se is None else round(se * FT_US, 3),
            "obs_head": None if oh is None else round(oh * FT_US, 3),
        })
    return out


def aerial_layer_records(site_id: str, filenames: list[str]) -> list[dict]:
    """Map-layer records for rasters sitting in the project's aerials folder.

    Token paths (the app detokenizes against the live work_dir), visible False
    so nothing draws until the user ticks the row, deterministic ids so the
    merge dedupe is stable across re-runs.
    """
    out = []
    for fn in filenames or []:
        stem = fn.rsplit(".", 1)[0] if "." in fn else fn
        out.append({
            "id": hashlib.sha1(f"{site_id}|aerial|{fn}".encode()).hexdigest()[:8],
            "path": f"$WORKSPACE$/aerials/{fn}",
            "name": stem,
            "kind": "raster",
            "opacity": 1.0,
            "color": "#e11d48",
            "visible": False,
        })
    return out


def _path_key(p) -> str:
    return str(p or "").replace("\\", "/").casefold()


def merge_state(existing: dict | None, *, site_id: str,
                factory_wells: list[dict], aerial_layers: list[dict],
                format_version) -> dict:
    """Overlay the factory-owned state keys onto an existing app state dict.

    Policy:
    - every key the factory does not own passes through untouched
    - factory owns format_version, desktop_project, project_name
    - obs_wells merge per id: factory wins name/lat/lon; factory obs_head wins
      when it has one; an in-app screen_elev (or obs_head the sheet lacks) is
      kept; app-added wells with foreign ids are preserved verbatim; new
      factory wells append in sheet order
    - well_pairs pass through (the factory never writes pairs)
    - map_layers: existing preserved, factory aerials appended unless a record
      with the same path (case-insensitive, slash-normalized) already exists

    Idempotent: merge_state(merge_state(x, ...), ...) == merge_state(x, ...).
    """
    state = dict(existing or {})
    state["format_version"] = format_version
    state["desktop_project"] = True
    state["project_name"] = site_id

    fmap = {w["id"]: w for w in factory_wells}
    merged: list[dict] = []
    seen: set[str] = set()
    for w in state.get("obs_wells") or []:
        wid = w.get("id")
        if wid in fmap:
            fw = dict(fmap[wid])
            if w.get("screen_elev") is not None:
                fw["screen_elev"] = w["screen_elev"]
            if fw.get("obs_head") is None and w.get("obs_head") is not None:
                fw["obs_head"] = w["obs_head"]
            merged.append(fw)
            seen.add(wid)
        else:
            merged.append(dict(w))
    for w in factory_wells:
        if w["id"] not in seen:
            merged.append(dict(w))
    state["obs_wells"] = merged
    state["well_pairs"] = [dict(p) for p in (state.get("well_pairs") or [])]

    layers = [dict(r) for r in (state.get("map_layers") or [])]
    have = {_path_key(r.get("path")) for r in layers}
    for rec in aerial_layers:
        if _path_key(rec.get("path")) not in have:
            layers.append(dict(rec))
            have.add(_path_key(rec.get("path")))
    state["map_layers"] = layers
    return state
