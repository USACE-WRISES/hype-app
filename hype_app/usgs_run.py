"""Child-process wrapper for the USGS StreamStats/NSS flow lookup.

Same spawn + queue protocol as run.py/soil_run.py. Process isolation matters here for a
different reason than the GC crash: running the sync httpx chain on an in-process worker
thread wedged the Shiny session's flush pipeline in live testing (this app's stack also
hosts HyRiver's second event loop + pycares threads). A spawned child has its own clean
interpreter, streams stage logs, and is hard-killable for Cancel.

Queue protocol: ('log', str) …, then ('result', FlowLookupSnapshot dict) or ('error', tb).
"""
from __future__ import annotations

import traceback


def child_run(payload: dict, q) -> None:
    """Payload: {"region", "lat", "lon", "want_national", "cache_dir"}."""
    try:
        from hype_app.services.streamstats import StreamStatsClient, suggest_region

        region = (payload.get("region") or "").strip().upper()
        lat, lon = float(payload["lat"]), float(payload["lon"])
        if not region:
            q.put(("log", "No region given — asking the FCC area API…"))
            region = suggest_region(lat, lon) or ""
            if not region:
                q.put(("error", "Couldn't determine the state for this point — "
                                "enter the 2-letter region code."))
                return
        q.put(("log", f"USGS lookup: {region} @ ({lat:.5f}, {lon:.5f})"))
        client = StreamStatsClient(cache_dir=payload.get("cache_dir"))
        try:
            q.put(("log", "Delineating the watershed (ss-delineate)…"))
            snap = client.lookup_flow(region, lat, lon,
                                      want_national=bool(payload.get("want_national")),
                                      cancel=None)
        finally:
            client.close()
        n_ins = sum(1 for c in snap.candidates if c.insertable)
        q.put(("log", f"Lookup complete: {len(snap.candidates)} statistics, "
                      f"{n_ins} insertable"))
        q.put(("result", snap.model_dump(mode="json")))
    except Exception:
        q.put(("error", traceback.format_exc()))
