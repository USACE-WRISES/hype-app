"""Child-process wrapper for the GMS folder refresh.

The export writes HDF5 via h5py; inside the app process (which also carries GDAL's
own HDF5 runtime via rasterio) that combination hard-crashes on Windows with no
traceback (verified live 2026-07-26: the in-thread build died at the .h5 write while
the identical standalone run succeeded). So the refresh always runs in a spawned
child, mirroring hype_app/run.py's queue protocol: ('log', line) messages, then
('result', dict) or ('error', traceback).
"""
from __future__ import annotations

import traceback


def child_run(payload: dict, q) -> None:
    """Run refresh_gms_tree in this (spawned) process; stream logs + result over `q`.

    Payload: {"work_dir", "name", "wkt", "porosity", "include_hz"}. The epoch veto
    lives app-side (_gms_done compares epochs after the fact) because this process
    cannot see the app's live state.
    """
    try:
        from hype_app.gms import refresh_gms_tree
        res = refresh_gms_tree(
            payload["work_dir"],
            name=payload["name"], crs_wkt_esri=payload["wkt"],
            porosity=payload["porosity"], include_hz=payload["include_hz"],
            log=lambda m: q.put(("log", str(m))))
        q.put(("result", res))
    except Exception:  # noqa: BLE001 — refresh_gms_tree never raises; belt and braces
        q.put(("error", traceback.format_exc()))
