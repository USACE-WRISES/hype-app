"""Child-process wrapper for the NRCS soils fetch.

The SDA spatial acquisition reprojects, clips and dedupes polygons with shapely/geopandas —
the numpy2.5 + shapely2.1 native-GC pairing that can crash the interpreter. Running it in a
spawned process (mirroring run.py / hz_run.py) isolates that risk from the Shiny event loop.
Queue protocol: ('log', str) then ('result', dict) or ('error', traceback).
"""
from __future__ import annotations

import traceback


def child_run(payload: dict, q) -> None:
    """Fetch + normalize an NRCS SoilDataSnapshot in this (spawned) process.

    Payload: {"domain_geojson" (EPSG:4326 geometry mapping), "working_crs_epsg",
    "anisotropy_ratio", "cache_dir"}.
    """
    try:
        from shapely.geometry import shape

        from hype_app.services.nrcs import NRCSClient

        domain = shape(payload["domain_geojson"])
        client = NRCSClient(cache_dir=payload.get("cache_dir"))
        try:
            q.put(("log", "Querying NRCS Soil Data Access…"))
            snap = client.fetch_soil_snapshot(
                domain, working_crs_epsg=payload.get("working_crs_epsg"),
                anisotropy_ratio=payload.get("anisotropy_ratio"))
        finally:
            client.close()
        q.put(("result", snap.model_dump(mode="json")))
    except Exception:
        q.put(("error", traceback.format_exc()))
