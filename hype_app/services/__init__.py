"""External-service clients for the HYPE revision: USGS StreamStats/NSS + NRCS Soil Data Access.

All network access goes through `http.ServiceClient` (bounded concurrency, split connect/read
timeouts, targeted retries with backoff + Retry-After, payload-shape validation, cancellation,
and an immutable on-disk snapshot cache). The `streamstats` and `nrcs` clients build on it and
emit the versioned contracts in `hype_app.contracts`.
"""
