"""Child-process runner for the Hydraulic Alternatives sweep.

Runs the scenario list SEQUENTIALLY in one spawned process (never two memory-heavy
MODFLOW/HZ solves at once). Each scenario is a full groundwater run + hyporheic-zone
analysis at the MAIN run's particle settings into `<work_dir>/alternatives/<id>/`, followed
by verify-then-prune: the retained artifacts (head rasters + summary/hz) are checked
reopenable, then the model workspace is deleted. A failed scenario removes its whole
directory and HALTS the loop; the app offers Retry / Continue / Stop and relaunches this
child with a scenario sublist, so no bidirectional process control is needed.

Queue protocol: ('log', str) ..., ('scenario', {...}) per finished scenario, then
('result', {"scenarios": [...], "halted_on": id?}) or ('error', traceback).
Log markers are ASCII only (Windows cp1252 child stdout): "ALT i/n [label] starting".
"""
from __future__ import annotations

import gc
import json
import shutil
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

from hype_app.run import _prepare_linux_bin, execute, modflow_bin_dir

LOG_TAIL_KEEP = 40      # compact per-scenario solver log retained in the index


class _VerifyError(RuntimeError):
    """Raised when a completed scenario's retained artifacts fail the reopen check."""


def _diagnostics(wd: Path) -> str:
    """Tails of the MODFLOW/MODPATH listing files so a failed scenario explains itself."""
    parts = []
    try:
        for pattern in ("model/gwf_workspace/*.lst", "model/hz_workspace/*.mplst"):
            for f in sorted(Path(wd).glob(pattern)):
                txt = f.read_text(errors="ignore")
                parts.append(f"----- {f.name} (tail) -----\n"
                             + "\n".join(txt.splitlines()[-25:]))
    except Exception:  # noqa: BLE001 - diagnostics must never mask the original error
        pass
    return "\n\n".join(parts).strip()


def _verify_alt_artifacts(wd: Path, stats: dict, sample_per_class: int) -> None:
    """Reopen every retained result before the workspace is deleted (retention spec).

    Anything the panes or ranges need must load here; a failure downgrades the scenario to
    failed rather than shipping a dir that renders blank after the prune."""
    import numpy as np

    hz_dir = wd / "summary" / "hz"
    with open(hz_dir / "hz_stats.json", "r", encoding="utf-8") as f:
        json.load(f)
    if stats.get("flux") is None:
        raise _VerifyError("flux-weighted interface pass produced no accounting")
    with np.load(hz_dir / "hz_flux.npz") as npz:
        if "time_days" not in npz.files and "weight" not in npz.files:
            raise _VerifyError("hz_flux.npz is missing its arrays")

    tifs = sorted((wd / "summary" / "head" / "per_layer_tif").glob("head_L*.tif"))
    if not tifs:
        raise _VerifyError("no head layer rasters were written")
    import rasterio
    with rasterio.open(tifs[0]) as src:
        src.read(1, out_shape=(1, 1))

    if sample_per_class > 0 and not (hz_dir / "hz_paths_3d.gpkg").is_file():
        raise _VerifyError("display pathlines were requested but hz_paths_3d.gpkg is absent")


def _prune_alt_dir(wd: Path) -> None:
    """Results-only retention: drop the model workspace and everything under summary/head
    except the per-layer rasters. The Basecase is the only run keeping a full workspace."""
    def _rm(path: Path) -> None:
        for attempt in (0, 1):
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                return
            gc.collect()            # Windows handle lag: one retry after collection
            time.sleep(0.2 * (attempt + 1))

    _rm(wd / "model")
    _rm(wd / "inputs")
    head = wd / "summary" / "head"
    if head.is_dir():
        for item in head.iterdir():
            if item.name != "per_layer_tif":
                _rm(item) if item.is_dir() else item.unlink(missing_ok=True)


def child_run(payload: dict, q) -> None:
    try:
        _prepare_linux_bin(modflow_bin_dir())
        from hype_app import geometry
        from hype_app.alternatives import scale_profile
        from hypetool.functions.hz_analysis import run_hz_analysis

        crs = payload["crs"]
        dom = geometry.single_feature_gdf(payload["domain"]).to_crs(crs)
        left = geometry.single_feature_gdf(payload["left"]).to_crs(crs)
        right = geometry.single_feature_gdf(payload["right"]).to_crs(crs)
        lines = {k: geometry.single_feature_gdf(payload[k]).to_crs(crs)
                 for k in ("left", "right", "up", "down")}
        khgdf = None
        if payload.get("kzones"):
            khgdf = geometry.kzones_to_gdf(
                payload["kzones"], fallback_kh=float(payload["kzone_kh"]),
                fallback_kv=float(payload["kzone_kv"])).to_crs(crs)
        builder = None
        if payload.get("soil_k"):
            from hype_app.soil_k import make_cell_k_builder
            builder = make_cell_k_builder(payload["soil_k"])

        hz_kw = dict(payload["hz"])
        hz_kw["classes_for_volume"] = tuple(hz_kw.get("classes_for_volume") or ())
        sample_per_class = int(hz_kw.get("sample_per_class", 0))
        alt_root = Path(payload["alt_root"])
        base_left = payload["left_profile"]
        base_right = payload["right_profile"]

        out: list[dict] = []
        n = len(payload["scenarios"])
        for i, scen in enumerate(payload["scenarios"], start=1):
            sid, label = scen["id"], scen["label"]
            q.put(("log", f"ALT {i}/{n} [{label}] starting"))
            wd = alt_root / sid
            shutil.rmtree(wd, ignore_errors=True)

            scen_log: list[str] = []

            def _log(m, s=sid, buf=scen_log):
                line = f"[{s}] {m}"
                buf.append(str(m))
                q.put(("log", line))

            params = dict(payload["params"])
            params["boundary_condition_mode"] = "Spatially Varying Gradient"
            params["left_boundary_gradient_profile"] = scale_profile(
                base_left, scen["g_factor"])
            params["right_boundary_gradient_profile"] = scale_profile(
                base_right, scen["g_factor"])
            params["k_scale"] = float(scen["k_factor"])

            rec = {"id": sid, "label": label, "dir": str(wd),
                   "k_factor": scen["k_factor"], "g_factor": scen["g_factor"],
                   "started_at": datetime.now(timezone.utc).isoformat()}
            t0 = time.monotonic()
            try:
                execute(domain_gdf=dom, left_gdf=left, right_gdf=right, crs=crs,
                        dem_path=payload["dem"], wse_path=payload["wse_path"],
                        wse_mode=payload["wse_mode"],
                        wse_relief_thresh=payload["wse_relief_thresh"],
                        kh_polygon_gdf=khgdf, cell_k_builder=builder,
                        params=params, work_dir=str(wd), log=_log)
                hz = run_hz_analysis(
                    wd, crs=crs, left_line=lines["left"], right_line=lines["right"],
                    up_line=lines["up"], down_line=lines["down"],
                    modflow_bin_dir=modflow_bin_dir(), log=_log, **hz_kw)
                _verify_alt_artifacts(wd, hz["stats"], sample_per_class)
                _prune_alt_dir(wd)
                rec.update(ok=True, stats=hz["stats"], hz_dir=hz["hz_dir"],
                           duration_s=round(time.monotonic() - t0, 1),
                           finished_at=datetime.now(timezone.utc).isoformat(),
                           log_tail=scen_log[-LOG_TAIL_KEEP:])
            except Exception:  # noqa: BLE001 - halt the sweep, never skip silently
                diag = _diagnostics(wd)
                err = traceback.format_exc(limit=8)
                shutil.rmtree(wd, ignore_errors=True)   # failed runs keep nothing on disk
                rec.update(ok=False,
                           error=(diag + "\n\n" if diag else "") + err,
                           duration_s=round(time.monotonic() - t0, 1),
                           finished_at=datetime.now(timezone.utc).isoformat(),
                           log_tail=scen_log[-LOG_TAIL_KEEP:])
                out.append(rec)
                q.put(("scenario", rec))
                q.put(("log", f"ALT {i}/{n} [{label}] FAILED"))
                q.put(("result", {"scenarios": out, "halted_on": sid}))
                return
            out.append(rec)
            q.put(("scenario", rec))
            q.put(("log", f"ALT {i}/{n} [{label}] complete"))
        q.put(("result", {"scenarios": out}))
    except Exception:
        q.put(("error", traceback.format_exc()))
