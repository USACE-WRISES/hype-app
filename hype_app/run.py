"""Assemble + execute a run_hyporheic call (invoked inside a worker thread)."""
from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

from hypetool.core.run_headless import run_hyporheic

_APP_ROOT = Path(__file__).resolve().parent.parent


def modflow_bin_dir() -> str:
    """Where to find mf6/mp7. The env override HYPE_MODFLOW_BIN wins (the desktop
    payload's tools\\ dir, or a local dev override); a stale/missing override falls
    through — mirroring ras_cmd — to the platform's bundled dir: bin/win (mf6.exe +
    mp7.exe) on Windows, bin/linux on Connect Cloud."""
    override = os.environ.get("HYPE_MODFLOW_BIN")
    if override and Path(override).is_dir():
        return override
    sub = "win" if sys.platform.startswith("win") else "linux"
    return str(_APP_ROOT / "bin" / sub)


def modflow_available() -> bool:
    """Are mf6 + mp7 plausibly runnable here? Pre-run gate (mirrors ras.ras_available)
    so a missing solver surfaces as a friendly notification, never a flopy traceback."""
    d = Path(modflow_bin_dir())
    ext = ".exe" if sys.platform.startswith("win") else ""
    return (d / f"mf6{ext}").is_file() and (d / f"mp7{ext}").is_file()


def _prepare_linux_bin(bin_dir: str) -> None:
    """On Linux (Connect Cloud), make the bundled mf6/mp7 executable — the +x bit is lost when the
    binaries are committed from Windows (git stores mode 100644), so FloPy's subprocess would hit
    'Permission denied' — and prepend the bin dir to LD_LIBRARY_PATH so any gfortran runtime .so's
    bundled alongside the binaries are found. No-op on Windows or for a missing dir."""
    import stat
    import sys
    if sys.platform.startswith("win"):
        return
    d = Path(bin_dir)
    if not d.is_dir():
        return
    for name in ("mf6", "mp7"):
        f = d / name
        if f.exists():
            try:
                f.chmod(f.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            except Exception:  # noqa: BLE001
                pass
    cur = os.environ.get("LD_LIBRARY_PATH", "")
    if str(d) not in cur.split(os.pathsep):
        os.environ["LD_LIBRARY_PATH"] = os.pathsep.join([str(d), cur]) if cur else str(d)


def execute(*, domain_gdf, left_gdf, right_gdf, crs, dem_path, wse_path, wse_mode,
            wse_relief_thresh, kh_polygon_gdf, params, work_dir, log,
            cell_k_builder=None):
    """Thin wrapper so the worker thread has one obvious call. Returns the artifact dict."""
    return run_hyporheic(
        domain_gdf=domain_gdf,
        left_line_gdf=left_gdf,
        right_line_gdf=right_gdf,
        crs=crs,
        dem_path=dem_path,
        wse_path=wse_path,
        wse_mode=wse_mode,
        wse_relief_thresh=wse_relief_thresh,
        kh_polygon_gdf=kh_polygon_gdf,
        cell_k_builder=cell_k_builder,
        work_dir=str(work_dir),
        modflow_bin_dir=modflow_bin_dir(),
        log=log,
        make_figures=False,
        run_particles=False,   # the app delineates post-run from ALL cells (hz_analysis);
        #                        the CLI's per-run stream-seeded MP7 pass is skipped
        **params,
    )


def _modflow_diagnostics(work_dir) -> str:
    """Best-effort: gather the tail of MODFLOW's listing files so a failed run explains
    itself. On a hard crash MODFLOW writes nothing to the queue and the listing stops mid-setup,
    so we read it off disk and flag when it never reached 'Normal termination'."""
    try:
        wd = Path(work_dir)
        files = sorted(wd.glob("**/mfsim.lst")) + sorted(wd.glob("**/gwf_model.lst"))
        finished = False
        parts = []
        for f in files:
            txt = f.read_text(errors="ignore")
            finished = finished or ("Normal termination" in txt)
            parts.append(f"----- {f.name} (tail) -----\n" + "\n".join(txt.splitlines()[-40:]))
        note = ""
        if files and not finished:
            note = ("MODFLOW exited before completing — no solver output was written. This usually "
                    "means it ran out of memory or hit a setup error for a grid this large. Try a "
                    "coarser cell size, shallower depth, or thicker layers.\n\n")
        return (note + "\n\n".join(parts)).strip()
    except Exception:  # noqa: BLE001 — diagnostics must never mask the original error
        return ""


def child_run(payload: dict, q) -> None:
    """Run a job in a separate (spawned) process; stream logs + result over the queue.

    Top-level + picklable so it works under the 'spawn' start method. Rebuilds the
    GeoDataFrames from the payload's GeoJSON, runs the engine, and puts ('log', line)
    messages followed by ('result', dict) or ('error', traceback) onto `q`.
    """
    try:
        _prepare_linux_bin(modflow_bin_dir())   # ensure the Linux mf6/mp7 are executable + linkable
        from hype_app import geometry
        crs = payload["crs"]
        dom = geometry.single_feature_gdf(payload["domain"]).to_crs(crs)
        left = geometry.single_feature_gdf(payload["left"]).to_crs(crs)
        right = geometry.single_feature_gdf(payload["right"]).to_crs(crs)
        khgdf = None
        if payload.get("kzones"):
            # Per-zone KH/KV from each Feature's properties (the engine assigns by dominant
            # polygon per cell); the payload pair is only the legacy-zone fallback.
            khgdf = geometry.kzones_to_gdf(
                payload["kzones"], fallback_kh=float(payload["kzone_kh"]),
                fallback_kv=float(payload["kzone_kv"])).to_crs(crs)
        builder = None
        if payload.get("soil_k"):
            from hype_app.soil_k import make_cell_k_builder
            builder = make_cell_k_builder(payload["soil_k"])
        result = execute(
            domain_gdf=dom, left_gdf=left, right_gdf=right, crs=crs,
            dem_path=payload["dem"], wse_path=payload["wse_path"],
            wse_mode=payload["wse_mode"], wse_relief_thresh=payload["wse_relief_thresh"],
            kh_polygon_gdf=khgdf, cell_k_builder=builder,
            params=payload["params"], work_dir=payload["work_dir"],
            log=lambda m: q.put(("log", str(m))),
        )
        q.put(("result", result))
    except Exception:
        diag = _modflow_diagnostics(payload.get("work_dir"))
        q.put(("error", (diag + "\n\n" if diag else "") + traceback.format_exc()))
