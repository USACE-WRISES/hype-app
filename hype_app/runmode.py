"""Cloud vs desktop run mode.

Desktop = launched by the HYPE Desktop shell (or anything that sets HYPE_DESKTOP=1, e.g.
the hype-app-desktop launch config). Cloud/default = the Posit Connect Cloud deployment:
the pre-run size gates in app.py block over-budget runs there. In desktop mode those same
gates become advisory only — the run proceeds and the machine's memory is the limit.
"""
from __future__ import annotations

import os

IS_DESKTOP = os.environ.get("HYPE_DESKTOP") == "1"


def hz_particle_cap() -> int:
    """Particle-count hard cap for the zone delineation (app.py aliases this as
    HZ_MAX_PARTICLES; hz_run passes it to the engine as hard_cap_particles)."""
    return int(os.environ.get("HYPE_HZ_MAX_PARTICLES", "2000000"))


def picker_mode() -> str:
    """No-shell desktop picker flavor: "auto" (default) spawns the tkinter child
    dialog; "modal" keeps the typed-path fallback (E2E drives this via HYPE_PICKER).
    Never "auto" outside desktop mode: cloud must not spawn dialogs on a server."""
    if not IS_DESKTOP:
        return "modal"
    env = os.environ.get("HYPE_PICKER", "auto").strip().lower()
    return "modal" if env == "modal" else "auto"


def cloud_limits() -> list[tuple[str, str]]:
    """(name, value) rows for the stage-bar Cloud Run hover card. Reads the live
    env-overridable caps at render time so the hover never goes stale."""
    from . import estimate, ras
    _green, sw_cap = ras.cell_budget()
    return [
        ("Surface-water mesh", f"up to {sw_cap:,} cells"),
        ("Groundwater grid", f"up to {estimate.AMBER_MAX:,} cells"),
        ("Particle tracking", f"up to {hz_particle_cap():,} particles"),
        ("Zone display points", "300 per class (max 1,000)"),
    ]
