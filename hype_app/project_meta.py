"""Project identity metadata (name, units, created stamp) — pure helpers, NO Shiny.

The three keys ("project_name", "project_units", "project_created") ride config/state.json
as first-class peers of sel_node/current_step — additive, so pre-metadata bundles simply
lack them and every reader here tolerates that. Units are recorded but LOCKED to metric
for now: the token exists so a future unit system can key off saved projects (mirroring
Stream Corridor's locked us_ft placeholder), not because anything converts today.
"""
from __future__ import annotations

import re
from datetime import datetime

UNITS_METRIC = "metric"
# Display labels per unit-system token. A future system (e.g. US customary) adds a token
# here; unknown tokens from newer saves degrade to metric rather than erroring.
UNIT_LABELS: dict[str, str] = {UNITS_METRIC: "Metric (meters, days)"}

# Everything Win32 forbids in a file name, plus control characters.
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def meta_from_state(state: dict | None, *, fallback_name: str | None = None) -> dict:
    """Project metadata from a restored state.json, tolerant of pre-metadata bundles.

    name: stored project_name wins; else fallback_name (a filename stem); else None.
    units: the stored token when known, else metric.
    created: the stored ISO stamp or None — never backfilled ("Not recorded" beats a lie).
    """
    st = state or {}
    name = str(st.get("project_name") or "").strip() or None
    if name is None:
        name = str(fallback_name or "").strip() or None
    units = st.get("project_units")
    if units not in UNIT_LABELS:
        units = UNITS_METRIC
    created = st.get("project_created")
    return {"name": name, "units": units, "created": str(created) if created else None}


def filename_stem(name: str | None, fallback: str = "") -> str:
    """Win32-safe file/download stem from a project name.

    Replaces the forbidden characters, collapses whitespace runs, trims the trailing
    dots/spaces Win32 silently drops, and clamps the length. An empty or fully-forbidden
    name yields `fallback`.
    """
    s = _ILLEGAL.sub(" ", str(name or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80].rstrip(" .") or fallback


def created_display(iso: str | None) -> str:
    """Human date for the Project pane: ISO stamp -> "Jul 23, 2026", else "Not recorded"."""
    if not iso:
        return "Not recorded"
    try:
        return datetime.fromisoformat(str(iso)).strftime("%b %d, %Y")
    except ValueError:
        return "Not recorded"
