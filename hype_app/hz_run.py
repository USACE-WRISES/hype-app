"""Child-process wrapper for the hyporheic-zone delineation analysis.

Mirror of hype_app/run.py's queue protocol: the app spawns this in a separate
process (start method 'spawn', hard-killable on Cancel) and reads ('log', line)
messages followed by ('result', dict) or ('error', traceback) off the queue.
"""
from __future__ import annotations

import traceback
from pathlib import Path

from hype_app.run import _prepare_linux_bin, modflow_bin_dir


def _hz_diagnostics(work_dir) -> str:
    """Tail of the MODPATH listing files from the HZ workspace, so a failed
    analysis explains itself (same spirit as run.py's _modflow_diagnostics)."""
    try:
        ws = Path(work_dir) / "model" / "hz_workspace"
        parts = []
        for f in sorted(ws.glob("*.mplst")):
            txt = f.read_text(errors="ignore")
            parts.append(f"----- {f.name} (tail) -----\n" + "\n".join(txt.splitlines()[-30:]))
        return "\n\n".join(parts).strip()
    except Exception:  # noqa: BLE001 — diagnostics must never mask the original error
        return ""


def child_run(payload: dict, q) -> None:
    """Run the HZ analysis in this (spawned) process; stream logs + result over `q`.

    Payload: {"work_dir", "crs", "left"/"right"/"up"/"down" (GeoJSON features of the
    boundary lines), "params": kwargs for run_hz_analysis (particles_per_cell,
    sample_per_class, porosity, modflow_bin_dir, ...)}.
    """
    try:
        _prepare_linux_bin(modflow_bin_dir())
        from hype_app import geometry
        from hypetool.functions.hz_analysis import run_hz_analysis

        crs = payload["crs"]
        lines = {k: geometry.single_feature_gdf(payload[k]).to_crs(crs)
                 for k in ("left", "right", "up", "down")}
        res = run_hz_analysis(
            payload["work_dir"], crs=crs,
            left_line=lines["left"], right_line=lines["right"],
            up_line=lines["up"], down_line=lines["down"],
            log=lambda m: q.put(("log", str(m))),
            **payload.get("params", {}),
        )
        q.put(("result", res))
    except Exception:
        diag = _hz_diagnostics(payload.get("work_dir"))
        q.put(("error", (diag + "\n\n" if diag else "") + traceback.format_exc()))
