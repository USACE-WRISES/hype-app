"""Child-process wrapper for the NRCS soils fetch.

The SDA spatial acquisition reprojects, clips and dedupes polygons with shapely/geopandas —
the numpy2.5 + shapely2.1 native-GC pairing that can crash the interpreter. Running it in a
spawned process (mirroring run.py / hz_run.py) isolates that risk from the Shiny event loop.
Queue protocol: ('log', str) then ('result', dict) or
('error', {"message": user-facing line, "trace": full traceback}).
"""
from __future__ import annotations

import traceback


def _friendly(e: Exception) -> str:
    """One user-facing sentence for a fetch failure, shown inside the soils modal.

    str(ServiceError) is documented user-safe (services/http.py), so surface it directly
    and only add a hint for the cases where one helps."""
    try:
        from hype_app.services.http import (PayloadError, ServiceError, ServiceHTTPError,
                                            ServiceTimeout)
        if isinstance(e, ServiceTimeout):
            return (f"{e} The service did not respond. Check your internet connection "
                    "and try again.")
        if isinstance(e, (ServiceHTTPError, PayloadError)):
            return f"NRCS Soil Data Access returned an unexpected response. {e}"
        if isinstance(e, ServiceError):
            return str(e)
    except Exception:  # noqa: BLE001 — message building must never mask the real error
        pass
    return f"{type(e).__name__}: {e}"


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
    except Exception as e:  # noqa: BLE001 — everything crosses the queue, typed by _friendly
        q.put(("error", {"message": _friendly(e), "trace": traceback.format_exc()}))
