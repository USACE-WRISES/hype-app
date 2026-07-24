"""Child-process wrapper for the gradient-sensitivity scenario runs (revision spec §10.3).

Runs the scenario list SEQUENTIALLY in one spawned process (never two memory-heavy
MODFLOW/HZ solves at once): for each scenario, a full groundwater run into its own isolated
workspace `<work_dir>/sensitivity/<id>/`, followed by a metrics-only hyporheic-zone analysis
(sample_per_class=0, no 3-D shells — §10.4 compact alternatives). The preferred scenario runs
FIRST; if it fails the whole set stops; an alternative's failure is recorded and the loop
continues. Scenarios whose id is in `skip_ids` (already completed, unchanged hash) are skipped —
that is the resume path.

Queue protocol: ('log', str) …, ('scenario', {...}) per finished scenario, then
('result', {"scenarios": [...]}) or ('error', traceback).
"""
from __future__ import annotations

import traceback
from pathlib import Path

from hype_app.run import _prepare_linux_bin, execute, modflow_bin_dir


def child_run(payload: dict, q) -> None:
    try:
        _prepare_linux_bin(modflow_bin_dir())
        from hype_app import geometry
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

        skip = set(payload.get("skip_ids") or [])
        out: list[dict] = []
        n = len(payload["scenarios"])
        for i, scen in enumerate(payload["scenarios"], start=1):
            sid = scen["id"]
            if sid in skip:
                q.put(("log", f"SCENARIO {i}/{n} [{scen['label']}] — already complete, skipped"))
                continue
            q.put(("log", f"SCENARIO {i}/{n} [{scen['label']}] starting"))
            wd = Path(payload["work_dir"]) / "sensitivity" / sid
            params = dict(payload["params"])
            params["boundary_condition_mode"] = "Spatially Varying Gradient"
            params["left_boundary_gradient_profile"] = scen["left_profile"]
            params["right_boundary_gradient_profile"] = scen["right_profile"]
            rec = {"id": sid, "label": scen["label"],
                   "is_preferred": bool(scen.get("is_preferred")), "dir": str(wd)}
            try:
                execute(domain_gdf=dom, left_gdf=left, right_gdf=right, crs=crs,
                        dem_path=payload["dem"], wse_path=payload["wse_path"],
                        wse_mode=payload["wse_mode"],
                        wse_relief_thresh=payload["wse_relief_thresh"],
                        kh_polygon_gdf=khgdf, cell_k_builder=builder,
                        params=params, work_dir=str(wd),
                        log=lambda m, s=sid: q.put(("log", f"[{s}] {m}")))
                hz = run_hz_analysis(
                    wd, crs=crs, left_line=lines["left"], right_line=lines["right"],
                    up_line=lines["up"], down_line=lines["down"],
                    particles_per_cell=1, sample_per_class=0, classes_for_volume=(),
                    porosity=float(params.get("porosity", 0.3)),
                    modflow_bin_dir=modflow_bin_dir(),
                    log=lambda m, s=sid: q.put(("log", f"[{s}] {m}")))
                rec.update(ok=True, stats=hz["stats"], hz_dir=hz["hz_dir"])
            except Exception:  # noqa: BLE001
                rec.update(ok=False, error=traceback.format_exc(limit=8))
                q.put(("log", f"SCENARIO {i}/{n} [{scen['label']}] FAILED"))
                out.append(rec)
                q.put(("scenario", rec))
                if rec["is_preferred"]:
                    q.put(("log", "Preferred scenario failed — stopping the set (§10.3)."))
                    break
                continue
            out.append(rec)
            q.put(("scenario", rec))
            q.put(("log", f"SCENARIO {i}/{n} [{scen['label']}] complete"))
        q.put(("result", {"scenarios": out}))
    except Exception:
        q.put(("error", traceback.format_exc()))
