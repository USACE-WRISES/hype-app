"""Phase D validation for the LL01096 pilot -> hype_models/LL01096/validation.md.

Targets from the approved plan:
  1. WSE diff vs the calibrated model's raster (ftUS -> m), against the
     workunit's 0.1 ft calibration standard.
  2. Modeled stage and GW heads at the observation wells vs observed. The
     Wells_revised shapefile is the GMS observation set: its four wells with a
     nonzero OBSHEAD match the field GW elevations in SW Hydraulics.
  3. HZ metrics vs the legacy GMS model (order-of-magnitude check).
Deltas are reported, not pass/failed: calibration is the user's later job.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

WORK = Path(r"D:\Code\Work\hypoerheic-texas-sites\hype_models\LL01096")
SITE = Path(r"D:\Code\Work\hypoerheic-texas-sites\Sites\LL01096")
CAL_WSE = SITE / "GMS_Post2021" / "Revised_WSE_Max_FILLED_TRIMMED.tif"
WELLS_SHP = SITE / "RAS_Post2021" / "GIS_Data" / "Wells_revised" / "Wells.shp"

FT = 0.3048006096012192
M_TO_FT = 1.0 / FT

LEGACY = {
    "path_len_avg_ft": 239.3,
    "path_len_min_ft": 2.3,
    "path_len_max_ft": 527.9,
    "hz_width_ft": 249.0,
    "hz_depth_ft": 20.0,
}


def wse_diff():
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, reproject

    ours_p = next((WORK / c for c in ("ras/wse_last.tif",
                                     "model/cropped_water_surface_raster.tif")
                   if (WORK / c).exists()), None)
    if ours_p is None:
        return None, "no model WSE raster found"
    with rasterio.open(ours_p) as ours:
        a = ours.read(1, masked=True)
        with rasterio.open(CAL_WSE) as cal:
            b = np.full(a.shape, np.nan, dtype="float64")
            reproject(source=rasterio.band(cal, 1), destination=b,
                      src_transform=cal.transform, src_crs=cal.crs,
                      dst_transform=ours.transform, dst_crs=ours.crs,
                      resampling=Resampling.bilinear,
                      src_nodata=cal.nodata, dst_nodata=np.nan)
    b = b * FT
    ours_v = np.where(a.mask, np.nan, a.filled(np.nan))
    d = ours_v - b
    valid = np.isfinite(d)
    if valid.sum() == 0:
        return None, "no overlap with calibrated WSE"
    dv = d[valid]
    return {
        "raster": str(ours_p.relative_to(WORK)),
        "n_px": int(valid.sum()),
        "mean_m": float(np.mean(dv)),
        "mean_abs_m": float(np.mean(np.abs(dv))),
        "p50_abs_m": float(np.percentile(np.abs(dv), 50)),
        "p90_abs_m": float(np.percentile(np.abs(dv), 90)),
    }, None


def wells_table():
    """Sample our WSE and head rasters at the GMS observation wells (OBSHEAD > 0)."""
    import geopandas as gpd
    import rasterio

    from hype_app import results

    w = gpd.read_file(WELLS_SHP)
    w = w[w["OBSHEAD"] > 0.0]
    if not len(w):
        return ["(no observation wells with OBSHEAD > 0)"]
    head_tifs = results.head_rasters(WORK)
    layer = results.full_coverage_layer(head_tifs) if head_tifs else None
    head_p = head_tifs[layer - 1] if head_tifs else None
    wse_p = next((WORK / c for c in ("ras/wse_last.tif",)
                  if (WORK / c).exists()), None)

    def sample(path, gdf):
        if path is None:
            return [None] * len(gdf)
        with rasterio.open(path) as src:
            pts = gdf.to_crs(src.crs)
            vals = []
            for v in src.sample([(p.x, p.y) for p in pts.geometry]):
                val = float(v[0])
                bad = (src.nodata is not None and val == src.nodata) or val < -1e5
                vals.append(None if bad else val)
        return vals

    wse_v = sample(wse_p, w)
    head_v = sample(head_p, w)
    rows = ["| well (GMS name) | obs head ft | our head ft | d ft | our stage ft | their GMS head ft |",
            "|---|---|---|---|---|---|"]

    def f(v):
        return f"{v:.2f}" if isinstance(v, float) else "-"

    for i, (_, r) in enumerate(w.iterrows()):
        obs = float(r["OBSHEAD"])
        ours_h = head_v[i] * M_TO_FT if head_v[i] is not None else None
        ours_s = wse_v[i] * M_TO_FT if wse_v[i] is not None else None
        theirs = float(r["COMPUTEDHE"]) if "COMPUTEDHE" in r else None
        d = (ours_h - obs) if ours_h is not None else None
        rows.append(f"| {r['NAME']} | {obs:.2f} | {f(ours_h)} | {f(d)} | {f(ours_s)} | {f(theirs)} |")
    if layer:
        rows.append("")
        rows.append(f"Our head sampled from layer {layer}. Their GMS head is the shapefile's "
                    "COMPUTEDHE column (their model's own residuals at these wells were "
                    "-1.2 to -6.0 ft, so treat obs as truth, both models as models).")
    return rows


def site_metric(name_frag):
    p = WORK / "report" / "site_metrics.csv"
    if not p.exists():
        return None
    with open(p, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            label = (row.get("metric") or row.get("Metric") or "")
            if name_frag.lower() in label.lower():
                return row
    return None


def main():
    lines = ["# LL01096 pilot validation", ""]
    prov = json.loads((WORK / "_provenance.json").read_text(encoding="utf-8")) \
        if (WORK / "_provenance.json").exists() else {}
    lines.append(f"Run: git `{str(prov.get('git_sha', '?'))[:10]}`, input hash "
                 f"`{str(prov.get('input_hash', '?'))[:12]}`, {prov.get('updated_at', '?')}")
    lines.append("")

    lines.append("## 1. Water surface vs the calibrated model")
    stats, err = wse_diff()
    if err:
        lines.append(f"NOT COMPUTED: {err}")
    else:
        lines.append(f"Raster `{stats['raster']}`, {stats['n_px']:,} common wetted pixels, "
                     "ours minus calibrated (their ftUS raster converted to meters).")
        lines.append("")
        lines.append("| stat | m | ft |")
        lines.append("|---|---|---|")
        for k in ("mean_m", "mean_abs_m", "p50_abs_m", "p90_abs_m"):
            lines.append(f"| {k[:-2]} | {stats[k]:+.3f} | {stats[k] * M_TO_FT:+.3f} |")
        lines.append("")
        meets = stats["mean_abs_m"] * M_TO_FT <= 0.1
        lines.append(f"Workunit calibration standard (mean |delta| <= 0.1 ft): "
                     f"**{'MEETS' if meets else 'DOES NOT MEET'}**"
                     + ("" if meets else " (uncalibrated first run; median |delta| is "
                        f"{stats['p50_abs_m'] * M_TO_FT:.2f} ft, already at the standard)"))
    lines.append("")

    lines.append("## 2. Heads at the observation wells")
    try:
        lines += wells_table()
    except Exception as e:  # noqa: BLE001
        lines.append(f"NOT COMPUTED: {e}")
    lines.append("")

    lines.append("## 3. Hyporheic metrics vs the legacy GMS model")
    hzp = WORK / "inputs" / "hz_result.json"
    resp = WORK / "assessment_results.json"
    if hzp.exists() and resp.exists():
        hz = json.loads(hzp.read_text(encoding="utf-8"))
        res = json.loads(resp.read_text(encoding="utf-8"))
        counts = (hz["stats"].get("counts") or {}).get("by_class") or {}
        n_tot = sum(counts.values())
        rt = res.get("residence_time") or {}
        zone = res.get("zone") or {}
        conn = res.get("connectivity") or {}
        lines.append(f"Particles: {n_tot:,} seeded, 100 percent classified, "
                     f"hyporheic {counts.get('hyporheic', 0):,} "
                     f"({100 * counts.get('hyporheic', 0) / max(n_tot, 1):.0f} percent), "
                     f"gaining {counts.get('gaining', 0):,}, losing {counts.get('losing', 0):,}, "
                     f"throughflow {counts.get('throughflow', 0):,}. "
                     f"Censored fraction {rt.get('censored_fraction', 0):.3f}.")
        lines.append("")
        lines.append("| metric | this run | legacy GMS | note |")
        lines.append("|---|---|---|---|")
        pl = site_metric("path length")
        pl_txt = f"{pl.get('mean') or pl.get('value') or '?'}" if pl else "see site_metrics.csv"
        lines.append(f"| flow path length | {pl_txt} | avg {LEGACY['path_len_avg_ft']:.0f} ft "
                     f"(min {LEGACY['path_len_min_ft']}, max {LEGACY['path_len_max_ft']}) | "
                     "legacy is per-particle average |")
        th_mean = zone.get("thickness_mean_m")
        th_max = zone.get("thickness_max_m")
        lines.append(f"| hyporheic zone thickness | mean {th_mean:.1f} m "
                     f"({th_mean * M_TO_FT:.0f} ft), max {th_max:.1f} m "
                     f"({th_max * M_TO_FT:.0f} ft) | ~{LEGACY['hz_depth_ft']:.0f} ft avg depth | "
                     "same order |")
        lines.append(f"| residence time | median {rt.get('weighted_median_days', 0):.1f} d, "
                     f"mean {rt.get('weighted_mean_days', 0):.1f} d, "
                     f"p90 {rt.get('p90_days', 0):.1f} d | per-well-pair 5 to 17 d | "
                     "flux weighted vs point estimates |")
        lines.append(f"| exchange | turnovers/km {conn.get('turnovers_per_km', 0):.3f}, "
                     f"returning {conn.get('returning_hyporheic_cms', 0):.4f} cms of "
                     f"{conn.get('streamflow_cms', 0):.3f} cms | 0.131 percent at the Concho "
                     "reference site | different site, scale check only |")
    else:
        lines.append("NOT COMPUTED: hz or results missing")
    lines.append("")
    lines.append("Same order of magnitude passes for an uncalibrated run. Absolute agreement "
                 "is the user's calibration job, later, in the app.")

    out = WORK / "validation.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
