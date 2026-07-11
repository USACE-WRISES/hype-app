"""Soil-derived per-cell conductivity for the groundwater run (revision spec §6.6–6.10).

Bridges the NRCS soil snapshot into the engine: `prepare_soil_k_payload` (app side, picklable)
reduces the snapshot + aggregation policy to per-mukey component profiles; `make_cell_k_builder`
(child side) returns the callable the engine invokes after the grid exists to produce per-cell,
per-layer KH/KV arrays via the depth-aware intersection in `soil_profile`.

Precedence (§6.8) is realized in the engine hook: these arrays form the BASE (NRCS-derived over
global fallback), and the manual K-zone polygons overlay on top of them.

Ground surface: the engine's DIS `top` array IS the local terrain elevation per cell (layer 0 is
the DEM-following layer), so horizon depths below local ground map to elevations as
`top[r, c] - depth_cm / 100`.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np

from .contracts import (
    AggregationPolicy,
    Component,
    Horizon,
    KOrigin,
    SoilDataSnapshot,
)
from .soil_profile import (
    CoverageAccumulator,
    layer_k_for_component,
    select_components,
)


def prepare_soil_k_payload(snapshot: dict | SoilDataSnapshot, *, policy: str,
                           anisotropy_ratio: float, fallback_kh: float,
                           fallback_kv: float) -> dict | None:
    """Reduce a SoilDataSnapshot to the picklable payload the run child consumes.

    Applies component selection per the aggregation policy HERE (per map unit), so the child
    only does spatial mapping + depth intersection. Returns None when the snapshot is empty.
    """
    snap = (snapshot if isinstance(snapshot, SoilDataSnapshot)
            else SoilDataSnapshot.model_validate(snapshot))
    if not snap.polygons or not snap.map_units:
        return None
    pol = AggregationPolicy(policy)

    profiles: dict[str, list[dict]] = {}
    for mu in snap.map_units:
        selected = select_components(mu, pol)
        comps = []
        for comp, weight in selected:
            horizons = [{"top_cm": h.top_cm, "bottom_cm": h.bottom_cm,
                         "ksat_um_s": h.ksat_um_s} for h in comp.horizons]
            comps.append({"weight": float(weight), "cokey": comp.cokey, "horizons": horizons})
        if comps:
            profiles[mu.mukey] = comps

    polygons = [{"mupolygonkey": p.mupolygonkey, "mukey": p.mukey, "geometry": p.geometry}
                for p in snap.polygons if p.mukey in profiles]
    if not polygons:
        return None
    return {"polygons": polygons, "profiles": profiles, "policy": pol.value,
            "anisotropy_ratio": float(anisotropy_ratio),
            "fallback_kh": float(fallback_kh), "fallback_kv": float(fallback_kv)}


def make_cell_k_builder(soil_payload: dict):
    """Return the engine hook: (cfg, gwf, idomain) -> (k, k33) 3-D arrays (or (None, None)).

    Runs inside the spawned run child. Writes a coverage report (§6.10) to
    <work_dir>/summary/soil_k_coverage.json as a side artifact.
    """
    def _builder(cfg, gwf, idomain):
        import geopandas as gpd
        from shapely.geometry import Polygon, shape

        aniso = float(soil_payload["anisotropy_ratio"])
        fb_kh = float(soil_payload["fallback_kh"])
        fb_kv = float(soil_payload["fallback_kv"])
        profiles = soil_payload["profiles"]

        mg = gwf.modelgrid
        dis = gwf.get_package("DIS")
        nlay, nrow, ncol = np.asarray(idomain).shape
        top2d = np.asarray(dis.top.array, dtype=float)
        bot3d = np.asarray(dis.botm.array, dtype=float)

        # soil polygons -> model CRS
        crs = getattr(cfg, "hec_ras_crs", None) or getattr(mg, "crs", None)
        gdf = gpd.GeoDataFrame(
            {"mukey": [p["mukey"] for p in soil_payload["polygons"]]},
            geometry=[shape(p["geometry"]) for p in soil_payload["polygons"]],
            crs="EPSG:4326")
        if crs is not None:
            gdf = gdf.to_crs(crs)
        else:                       # CRS-less test grids: treat coords as already model-space
            gdf = gdf.set_crs(None, allow_override=True)

        # model cells -> polygons (mirrors my_utils._kh_arrays_from_polygon)
        Xv, Yv = mg.xvertices, mg.yvertices
        rows, cols, geoms = [], [], []
        for r in range(nrow):
            for c in range(ncol):
                geoms.append(Polygon([(Xv[r, c], Yv[r, c]), (Xv[r, c + 1], Yv[r, c + 1]),
                                      (Xv[r + 1, c + 1], Yv[r + 1, c + 1]),
                                      (Xv[r + 1, c], Yv[r + 1, c])]))
                rows.append(r)
                cols.append(c)
        g_cells = gpd.GeoDataFrame({"row": rows, "col": cols}, geometry=geoms, crs=crs)

        inter = gpd.overlay(g_cells, gdf, how="intersection")
        if inter is None or inter.empty:
            print("[soil-K] no soil polygons intersect the model grid — uniform K kept")
            return None, None
        inter["_area"] = inter.geometry.area
        dominant = (inter.sort_values(["row", "col", "_area"], ascending=[True, True, False])
                    .drop_duplicates(subset=["row", "col"], keep="first"))

        k = np.full((nlay, nrow, ncol), fb_kh, dtype=float)
        k33 = np.full((nlay, nrow, ncol), fb_kv, dtype=float)
        cov = CoverageAccumulator()
        area = None
        try:
            delr = np.asarray(dis.delr.array, float)
            delc = np.asarray(dis.delc.array, float)
            area = np.multiply.outer(delc, delr)
        except Exception:  # noqa: BLE001
            pass

        active_cols = (np.asarray(idomain) == 1).any(axis=0)     # (nrow, ncol) active footprint
        covered_cells = 0
        for _, rec in dominant.iterrows():
            r, c = int(rec["row"]), int(rec["col"])
            comps = profiles.get(str(rec["mukey"]))
            if not comps:
                continue
            if active_cols[r, c]:
                covered_cells += 1
            ground = float(top2d[r, c])
            for lay in range(nlay):
                lay_top = ground if lay == 0 else float(bot3d[lay - 1, r, c])
                lay_bot = float(bot3d[lay, r, c])
                if idomain[lay, r, c] != 1 or lay_top <= lay_bot:
                    continue
                # per-component depth intersection, then §6.6 cross-component combination:
                # arithmetic KH / harmonic KV weighted by component percentage
                khs, kvs, fbs, ws = [], [], [], []
                for comp in comps:
                    comp_obj = Component(cokey=comp.get("cokey"), horizons=[
                        Horizon(**h) for h in comp["horizons"]])
                    ckh, ckv, _origin, fb_frac = layer_k_for_component(
                        comp_obj, layer_top_elev=lay_top, layer_bottom_elev=lay_bot,
                        ground_elev=ground, anisotropy_ratio=aniso,
                        fallback_kh=fb_kh, fallback_kv=fb_kv)
                    khs.append(ckh)
                    kvs.append(ckv)
                    fbs.append(fb_frac)
                    ws.append(float(comp["weight"]))
                wtot = sum(ws) or 1.0
                kh_val = sum(w * v for w, v in zip(ws, khs)) / wtot
                inv = sum(w / v for w, v in zip(ws, kvs) if v > 0)
                kv_val = (wtot / inv) if inv > 0 else fb_kv
                k[lay, r, c] = kh_val
                k33[lay, r, c] = kv_val
                vol = float(area[r, c] * (lay_top - lay_bot)) if area is not None else 1.0
                fb_frac_c = sum(w * f for w, f in zip(ws, fbs)) / wtot
                cov.add(KOrigin.fallback, vol * fb_frac_c)
                cov.add(KOrigin.derived, vol * (1.0 - fb_frac_c))

        # uncovered cells simply keep the fallback-filled arrays
        n_active = int(active_cols.sum()) or 1
        report = {
            "policy": soil_payload["policy"],
            "anisotropy_ratio": aniso,
            "cells_covered": int(covered_cells),
            "cells_active": n_active,                 # active-domain footprint, not the grid box
            "domain_area_covered_pct": round(100.0 * covered_cells / n_active, 2),
            "volume_pct_by_origin": cov.as_percentages(),
        }
        try:
            out = Path(cfg.output_directory) / "summary"
            out.mkdir(parents=True, exist_ok=True)
            (out / "soil_k_coverage.json").write_text(json.dumps(report, indent=2))
        except Exception:  # noqa: BLE001
            pass
        print(f"[soil-K] derived K for {covered_cells}/{nrow * ncol} cells "
              f"({report['volume_pct_by_origin']})")
        return k, k33

    return _builder


__all__ = ["prepare_soil_k_payload", "make_cell_k_builder"]
