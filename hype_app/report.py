"""Site Summary Report generation (revision spec §11).

Every output — the in-app modal, self-contained HTML, native PDF, and the two CSVs — reads ONLY
the canonical `AssessmentResultsV2` model (§11.2), and all of them render from ONE flat
`metric_rows()` list, so the numbers agree across formats by construction (§13.6). User-entered
text is HTML-escaped. Report generation is a pure function of the results model, so it can be
retried without rerunning the model (§11.5).
"""
from __future__ import annotations

import csv
import io
import json
import math
from pathlib import Path

from jinja2 import Environment, select_autoescape

from .contracts import AssessmentResultsV2

REPORT_METHOD_VERSION = "site-report/2.0"
RUN_SUMMARY_SCHEMA_VERSION = "hype-run-summary/1.0"
RTD_DISTRIBUTION_SCHEMA_VERSION = "hype-rtd-distribution/1.0"


def should_autoopen(prev_hash, cur_hash) -> bool:
    """Fire the report modal once per completed run: true only when there is a current run whose
    input hash differs from the last one shown, so later site-metadata edits do not reopen it."""
    return bool(cur_hash) and cur_hash != prev_hash


def fmt(value, digits: int = 3) -> str:
    """Uniform numeric formatting shared by every output format (§11.4). Missing values render as
    'n/a' (never an em dash, which reads as machine-generated in user-facing copy)."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value != value:                 # NaN
            return "n/a"
        if value == 0:
            return "0"
        a = abs(value)
        if a < 0.001:
            return f"{value:.3g}"           # scientific only for tiny magnitudes
        if a >= 10000:
            return f"{value:.0f}"           # 16093, 8200000 — plain integer, CSV-safe (no commas)
        return f"{round(value, digits):g}"  # 0.736, 2460, 1.4
    return str(value)


def _hours(days):
    """Residence times are stored in days; the report presents hours."""
    return None if days is None else days * 24.0


def _times(cms):
    """m3/s -> L/s for compact report values."""
    return None if cms is None else cms * 1000.0


def metric_rows(results: AssessmentResultsV2) -> list[dict]:
    """The single canonical (section, name, value, unit) list every format renders from.

    Grouped by the three hydraulic dimensions (report §5-7): Exchange frequency, Exposure duration,
    Active hyporheic capacity."""
    c, r, z = results.connectivity, results.residence_time, results.zone
    raw: list[tuple[str, str, object, str]] = [
        # Exchange frequency (report §5)
        ("Exchange frequency", "Streamflow-equivalent turnovers", c.turnovers_per_km, "turnovers/km"),
        ("Exchange frequency", "River turnover length", c.turnover_length_km, "km"),
        ("Exchange frequency", "Gross hyporheic exchange", _times(c.returning_hyporheic_cms), "L/s"),
        ("Exchange frequency", "Exchange intensity", c.exchange_flux_mm_day, "mm/day"),
        ("Exchange frequency", "Gross exchange ratio (reach)", c.gross_exchange_ratio_reach, ""),
        ("Exchange frequency", "Stream discharge", c.streamflow_cms, "m³/s"),
        ("Exchange frequency", "Streambed area", c.streambed_area_m2, "m²"),
        ("Exchange frequency", "Active streambed fraction", c.active_streambed_fraction, "fraction"),
        ("Exchange frequency", "Net groundwater exchange", c.net_stream_exchange_cms, "m³/s"),
        ("Exchange frequency", "Excursions per mile", c.excursions_per_mile, "1/mi"),
        ("Exchange frequency", "Mass-balance error", c.mass_balance_error, "fraction"),
        # Exposure duration (report §6)
        ("Exposure duration", "Median residence time", _hours(r.weighted_median_days), "hr"),
        ("Exposure duration", "Residence time P10", _hours(r.p10_days), "hr"),
        ("Exposure duration", "Residence time P90", _hours(r.p90_days), "hr"),
        ("Exposure duration", "Flow-weighted mean", _hours(r.weighted_mean_days), "hr"),
        ("Exposure duration", "Fraction over 1 day", r.frac_above_1d, "fraction"),
        ("Exposure duration", "Censored fraction", r.censored_fraction, "fraction"),
        # Active hyporheic capacity (report §7)
        ("Active hyporheic capacity", "Equivalent active depth", z.equivalent_active_depth_m, "m"),
        ("Active hyporheic capacity", "Active hyporheic volume", z.bulk_saturated_volume_m3, "m³"),
        ("Active hyporheic capacity", "Mobile pore-water storage", z.mobile_pore_storage_m3, "m³"),
        ("Active hyporheic capacity", "Volume basis", z.active_volume_basis, ""),
        ("Active hyporheic capacity", "P90 max path depth", z.path_depth_p90_m, "m"),
        ("Active hyporheic capacity", "P50 max path depth", z.path_depth_p50_m, "m"),
        ("Active hyporheic capacity", "Max path depth", z.path_depth_max_m, "m"),
        ("Active hyporheic capacity", "Binary footprint", z.footprint_binary_m2, "m²"),
        ("Active hyporheic capacity", "Mean / max thickness",
         None if z.thickness_mean_m is None else f"{fmt(z.thickness_mean_m)} / {fmt(z.thickness_max_m)}", "m"),
    ]
    return [{"section": s, "name": n, "value_raw": v,
             "value": v if isinstance(v, str) else fmt(v), "unit": u}
            for s, n, v, u in raw]


def headline_cards(results: AssessmentResultsV2) -> list[dict]:
    """The three headline scorecards (report §17.2), read only from the results model so they agree
    with the metric table and machine summary by construction. Each: dimension, primary value/unit,
    optional range, plain-language definition, ecological relevance, and supporting values."""
    c, r, z = results.connectivity, results.residence_time, results.zone

    def pct(frac):
        return None if frac is None else fmt(frac * 100.0)

    med, lo, hi = _hours(r.weighted_median_days), _hours(r.p10_days), _hours(r.p90_days)
    duration_range = (f"P10 to P90: {fmt(lo)} to {fmt(hi)} hr"
                      if (lo is not None and hi is not None) else None)
    return [
        {"dimension": "Exchange frequency",
         "primary_name": "Streamflow-equivalent turnovers",
         "primary_value": fmt(c.turnovers_per_km), "primary_unit": "turnovers/km",
         "primary_range": None,
         "definition": "How frequently streamwater is exchanged with returning hyporheic flow "
                       "paths, over one kilometer of channel.",
         "relevance": "Higher connectivity means more frequent delivery of oxygen, nutrients, "
                      "carbon, and heat to the subsurface. It does not by itself indicate longer "
                      "residence or greater processing.",
         "supporting": [("Gross hyporheic exchange", fmt(_times(c.returning_hyporheic_cms)), "L/s"),
                        ("Exchange intensity", fmt(c.exchange_flux_mm_day), "mm/day"),
                        ("River turnover length", fmt(c.turnover_length_km), "km")]},
        {"dimension": "Exposure duration",
         "primary_name": "Median residence time",
         "primary_value": fmt(med), "primary_unit": "hr",
         "primary_range": duration_range,
         "definition": "The flux-weighted time exchanged streamwater remains in the subsurface, "
                       "reported as the median with the P10 to P90 range.",
         "relevance": "Residence time sets the opportunity for thermal exchange, oxygen "
                      "consumption, and nutrient or contaminant transformation. It does not "
                      "establish that a reaction occurred.",
         "supporting": [("Fraction over 1 day", pct(r.frac_above_1d), "%"),
                        ("Censored flow", pct(r.censored_fraction), "%")]},
        {"dimension": "Active hyporheic capacity",
         "primary_name": "Equivalent active depth",
         "primary_value": fmt(z.equivalent_active_depth_m), "primary_unit": "m",
         "primary_range": None,
         "definition": "Active hyporheic volume normalized by streambed area. It is a "
                       "volume-normalized equivalent depth, not a uniform layer of that thickness.",
         "relevance": "Represents the hydraulically connected subsurface space available for "
                      "exchange, reaction, thermal storage, and potential habitat. It is not a "
                      "measure of habitat quality.",
         "supporting": [("Active hyporheic volume", fmt(z.bulk_saturated_volume_m3), "m³"),
                        ("Active streambed", pct(c.active_streambed_fraction), "%"),
                        ("P90 max path depth", fmt(z.path_depth_p90_m), "m")]},
    ]


def threshold_rows(results: AssessmentResultsV2) -> list[dict]:
    """Functional-opportunity rows (report §10.2, §30), one per residence-time scenario."""
    rows = []
    for t in results.thresholds:
        rows.append({
            "threshold_h": t.threshold_value_h,
            "label": t.threshold_label or "",
            "exceedance_pct": fmt(None if t.flow_exceedance_fraction is None
                                  else t.flow_exceedance_fraction * 100.0),
            "functional_l_s": fmt(_times(t.functional_exchange_m3_s)),
            "functional_per_km": fmt(t.functional_connectivity_per_km),
        })
    return rows


def input_rows(results: AssessmentResultsV2) -> list[dict]:
    """Section 8 (§11.3): the flow / soil-K / gradient / grid / model inputs the run consumed."""
    snap = results.input_snapshot
    if snap is None:
        return []
    k, g, grid, sf, terr = snap.k, snap.gradients, snap.grid, snap.streamflow, snap.terrain
    rows: list[tuple[str, str, object, str]] = [
        ("Flow", "Streamflow", sf.value_cfs, "cfs"),
        ("Flow", "Streamflow", sf.value_cms, "m³/s"),
        ("Soil / K", "Horizontal K (KH)", k.kh_m_day, "m/day"),
        ("Soil / K", "Vertical K (KV)", k.kv_m_day, "m/day"),
        ("Soil / K", "Anisotropy KH:KV", k.anisotropy_ratio, "ratio"),
        ("Soil / K", "Porosity", k.porosity, ""),
        ("Soil / K", "NRCS aggregation", (k.aggregation_policy.value
                                          if k.aggregation_policy else None), ""),
        ("Soil / K", "Manual K-zones", (k.kzone_count if k.use_kzones else 0), "zones"),
        ("Gradient", "Method", g.mode, ""),
        ("Gradient", "Left / right controls", f"{len(g.left_controls)} / {len(g.right_controls)}", ""),
        ("Gradient", "Reference slope", (g.reference_slope.value if g.reference_slope else None),
         "m/m"),
        ("Grid", "Cell size", f"{fmt(grid.cell_size_x)} × {fmt(grid.cell_size_y)}", "m"),
        ("Grid", "Model depth", grid.gw_mod_depth, "m"),
        ("Grid", "Layer thickness", grid.layer_thickness, "m"),
        ("Grid", "Layers", grid.nlay, ""),
        ("Model", "Model origin (streambed)", terr.model_origin_elev, "m"),
        ("Model", "Working CRS", (f"EPSG:{terr.crs_epsg}" if terr.crs_epsg else None), ""),
    ]
    return [{"section": s, "name": n, "value": v if isinstance(v, str) else fmt(v), "unit": u}
            for s, n, v, u in rows if v is not None and v != ""]


def data_source_rows(results: AssessmentResultsV2) -> list[dict]:
    """Section 9 (§11.3): data sources, retrieval dates, and overrides — the provenance record."""
    snap = results.input_snapshot
    if snap is None:
        return []
    out: list[dict] = []

    def _prov(item, prov):
        out.append({"item": item, "source": prov.source or "n/a",
                    "retrieved": (prov.retrieved_at.date().isoformat()
                                  if prov.retrieved_at else "n/a"),
                    "detail": (("edited; " if prov.user_modified else "")
                               + ", ".join(prov.fallbacks) if prov.fallbacks
                               else ("edited by analyst" if prov.user_modified else "n/a"))})

    _prov("Streamflow", snap.streamflow.provenance)
    if snap.k.soil_snapshot_id:
        out.append({"item": "Soil conductivity", "source": "NRCS SDA (SSURGO)",
                    "retrieved": "n/a",
                    "detail": (f"{snap.k.aggregation_policy.value} aggregation"
                               if snap.k.aggregation_policy else "derived")})
    if snap.terrain.dem_source:
        out.append({"item": "Terrain (DEM)", "source": snap.terrain.dem_source,
                    "retrieved": "n/a",
                    "detail": (f"{fmt(snap.terrain.dem_resolution_m)} m"
                               if snap.terrain.dem_resolution_m else "n/a")})
    if snap.gradients.reference_slope:
        rs = snap.gradients.reference_slope
        out.append({"item": "Gradient reference slope", "source": rs.source or "n/a",
                    "retrieved": "n/a", "detail": rs.method or "n/a"})
    return out


def sensitivity_rows(results: AssessmentResultsV2) -> list[dict]:
    """Section 10 (§11.3): per-metric preferred/min/max/range across the sensitivity scenarios."""
    manifest = results.sensitivity
    if manifest is None or not manifest.scenarios:
        return []
    from .sensitivity import aggregate_metric
    labels = [("turnovers_per_km", "Turnovers per km"),
              ("rtd_median_days", "Median residence time (days)"),
              ("equivalent_active_depth_m", "Equivalent active depth (m)"),
              ("volume_m3", "Active hyporheic volume (m³)")]
    rows: list[dict] = []
    for key, label in labels:
        agg = aggregate_metric(manifest.scenarios, key, manifest.preferred_id)
        if not agg:
            continue
        rows.append({"metric": label, "preferred": fmt(agg.get("preferred")),
                     "min": fmt(agg["min"]), "max": fmt(agg["max"]), "range": fmt(agg["range"])})
    return rows


def report_references(results: AssessmentResultsV2) -> list[dict]:
    """Section 13 (§11.3): scientific + service references, deduped by title."""
    refs: list[dict] = [
        {"title": "How hydrologic connectivity regulates water quality in river corridors",
         "authors": "Harvey et al.", "year": 2019,
         "url": "https://pubs.usgs.gov/publication/70205454"},
        {"title": "Hyporheic hydraulic geometry", "authors": "Poole et al.", "year": 2022,
         "url": "https://doi.org/10.1371/journal.pone.0262080"},
    ]
    snap = results.input_snapshot
    if snap is not None:
        if (snap.streamflow.provenance.source or "").startswith("USGS"):
            refs.append({"title": "USGS StreamStats / National Streamflow Statistics",
                         "authors": "U.S. Geological Survey", "year": None,
                         "url": "https://streamstats.usgs.gov"})
        if snap.k.soil_snapshot_id:
            refs.append({"title": "NRCS Soil Data Access (SSURGO)",
                         "authors": "USDA-NRCS", "year": None,
                         "url": "https://sdmdataaccess.nrcs.usda.gov"})
        for c in snap.citations:
            refs.append({"title": c.title, "authors": c.authors, "year": c.year, "url": c.url})
    seen, deduped = set(), []
    for r in refs:
        if r["title"] not in seen:
            seen.add(r["title"])
            deduped.append(r)
    return deduped


def render_rtd_figure(transit_days, weights) -> bytes | None:
    """Weighted residence-time distribution figure (§8.5): flux-weighted empirical CDF + a
    log-time histogram, as PNG bytes. Returns None when there's nothing to plot. Uses the
    headless Agg backend so it is safe in the pip-only Connect-Cloud environment."""
    import numpy as np
    t = np.asarray(transit_days, dtype=float)
    w = (np.ones_like(t) if weights is None else np.asarray(weights, dtype=float))
    ok = np.isfinite(t) & (t > 0) & np.isfinite(w) & (w > 0)
    t, w = t[ok], w[ok]
    if t.size < 2:
        return None
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:  # noqa: BLE001 — figures are best-effort (§11.5)
        return None

    order = np.argsort(t)
    ts, ws = t[order], w[order]
    cdf = np.cumsum(ws) / ws.sum()
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7.4, 2.9))
    ax1.step(ts, cdf, where="post", color="#2c7bb6", lw=1.6)
    ax1.set_xscale("log")
    ax1.set_xlabel("Transit time (days, log)")
    ax1.set_ylabel("Flux-weighted CDF")
    ax1.set_ylim(0, 1)
    ax1.grid(True, which="both", ls=":", lw=0.4, alpha=0.6)
    for hrs, lab in ((1 / 24, "1 h"), (1.0, "1 d")):
        if ts.min() <= hrs <= ts.max():
            ax1.axvline(hrs, color="#d73027", lw=0.8, ls="--")
            ax1.text(hrs, 0.02, lab, fontsize=6, color="#d73027", rotation=90, va="bottom")
    bins = np.logspace(np.log10(ts.min()), np.log10(ts.max()), 24)
    ax2.hist(ts, bins=bins, weights=ws, color="#2c7bb6", alpha=0.85)
    ax2.set_xscale("log")
    ax2.set_xlabel("Transit time (days, log)")
    ax2.set_ylabel("Flux weight")
    ax2.grid(True, which="both", ls=":", lw=0.4, alpha=0.6)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130)
    plt.close(fig)
    return buf.getvalue()


def results_to_json(results: AssessmentResultsV2) -> str:
    return results.model_dump_json(indent=2)


def write_site_metrics_csv(results: AssessmentResultsV2, path) -> str:
    rows = metric_rows(results)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "metric", "value", "unit"])
        for r in rows:
            w.writerow([r["section"], r["name"], r["value"], r["unit"]])
    return str(path)


def write_transit_times_csv(rtd_rows: list[dict], path) -> str:
    """Per-release-particle RTD rows (§8.5): source cell, flow weight, class, transit time, status."""
    fields = ["particle_id", "source_cell", "flow_weight", "endpoint_class",
              "transit_time_days", "termination"]
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for row in rtd_rows or []:
            w.writerow(row)
    return str(path)


def _json_safe(o):
    """Recursively replace non-finite floats (NaN/inf) with None so the export is strict JSON."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: _json_safe(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_json_safe(v) for v in o]
    return o


def run_summary_dict(results: AssessmentResultsV2, *, app_version=None,
                     model_version=None) -> dict:
    """Flat machine-readable run summary (report §25) derived from the results model, so it can
    never drift from the cards/CSV. Units are in the field names; reserved for combining 5-10 sites
    (report §14, §16.2). The four default thresholds also appear as flat columns; every threshold
    (incl. custom) rides the nested `threshold_results` array."""
    c, r, z = results.connectivity, results.residence_time, results.zone
    snap = results.input_snapshot
    site = snap.site if snap else None

    def d2h(d):
        return None if d is None else d * 24.0

    def ls(x):
        return None if x is None else x * 1000.0

    out = {
        "schema_version": RUN_SUMMARY_SCHEMA_VERSION,
        "site_id": (site.site_name if site else None),
        "site_name": (site.site_name if site else None),
        "run_id": results.assessment_id,
        "run_date": (results.created_at.isoformat() if results.created_at else None),
        "app_version": app_version,
        "model_version": model_version,
        "scenario_name": "baseline",
        "model_dimension": "3D",
        "reach_length_m": (site.reach_length_m if site else None),
        "streambed_area_m2": c.streambed_area_m2,
        "stream_discharge_m3s": c.streamflow_cms,
        "gross_hyporheic_exchange_m3s": c.returning_hyporheic_cms,
        "gross_hyporheic_exchange_l_s": ls(c.returning_hyporheic_cms),
        "exchange_intensity_m_per_day": c.exchange_flux_m_day,
        "exchange_intensity_mm_per_day": c.exchange_flux_mm_day,
        "connectivity_turnovers_per_km": c.turnovers_per_km,
        "turnover_length_km": c.turnover_length_km,
        "gross_exchange_ratio_reach": c.gross_exchange_ratio_reach,
        "excursions_per_mile": c.excursions_per_mile,
        "net_groundwater_exchange_m3s": c.net_stream_exchange_cms,
        "active_streambed_fraction": c.active_streambed_fraction,
        "active_streambed_percent": (None if c.active_streambed_fraction is None
                                     else c.active_streambed_fraction * 100.0),
        "returning_flow_fraction": c.returning_flow_fraction,
        "censored_flow_fraction": c.censored_flow_fraction,
        "residence_time_p10_hr": d2h(r.p10_days),
        "residence_time_p25_hr": d2h(r.p25_days),
        "residence_time_p50_hr": d2h(r.weighted_median_days),
        "residence_time_p75_hr": d2h(r.p75_days),
        "residence_time_p90_hr": d2h(r.p90_days),
        "residence_time_mean_hr": d2h(r.weighted_mean_days),
        "active_hyporheic_volume_m3": z.bulk_saturated_volume_m3,
        "mobile_pore_storage_m3": z.mobile_pore_storage_m3,
        "active_volume_basis": z.active_volume_basis,
        "equivalent_active_depth_m": z.equivalent_active_depth_m,
        "flow_path_depth_p50_m": z.path_depth_p50_m,
        "flow_path_depth_p90_m": z.path_depth_p90_m,
        "model_converged": None,
        "model_warning_count": len(results.warnings),
        "quality_diagnostics": results.quality_diagnostics,
        "threshold_results": [
            {"threshold_value_h": t.threshold_value_h,
             "threshold_label": t.threshold_label,
             "flow_exceedance_fraction": t.flow_exceedance_fraction,
             "functional_exchange_m3_s": t.functional_exchange_m3_s,
             "functional_exchange_l_s": ls(t.functional_exchange_m3_s),
             "functional_connectivity_per_km": t.functional_connectivity_per_km}
            for t in results.thresholds],
    }
    for t in results.thresholds:
        if float(t.threshold_value_h) in (1.0, 6.0, 12.0, 24.0):
            key = f"threshold_{int(t.threshold_value_h)}hr"
            out[f"{key}_fraction"] = t.flow_exceedance_fraction
            out[f"{key}_flow_m3s"] = t.functional_exchange_m3_s
            out[f"{key}_connectivity"] = t.functional_connectivity_per_km
    return out


def write_run_summary_json(results: AssessmentResultsV2, path, *, app_version=None,
                           model_version=None) -> str:
    data = _json_safe(run_summary_dict(results, app_version=app_version,
                                       model_version=model_version))
    Path(path).write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    return str(path)


def write_rtd_distribution_json(transit_rows: list[dict], path) -> str:
    """The full flux-weighted returning-path RTD (report §13.2): times + weights (+ depth when the
    engine depth pass ran), so thresholds can be recomputed later without rerunning the model."""
    ret = [r for r in (transit_rows or []) if r.get("endpoint_class") == "returning"]
    data = {
        "schema_version": RTD_DISTRIBUTION_SCHEMA_VERSION,
        "n_returning": len(ret),
        "transit_time_days": [r.get("transit_time_days") for r in ret],
        "flow_weight_m3_s": [r.get("flow_weight") for r in ret],
        "max_depth_m": [r.get("max_depth_m") for r in ret],
    }
    Path(path).write_text(json.dumps(_json_safe(data), indent=2), encoding="utf-8")
    return str(path)


_HTML_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>HYPE Site Summary: {{ site.site_name or 'Unnamed site' }}</title>
<style>
 :root{--navy:#2f4b7c;--navy-d:#243a61;--ink:#1f2d3d;--muted:#5a6b7b;--rule:#e6e9ef;
  --card:#d8e0ec;--soft:#f6f8fb}
 *{box-sizing:border-box}
 html,body{margin:0}
 body{font-family:"Space Grotesk","Segoe UI",system-ui,-apple-system,Arial,sans-serif;
  color:var(--ink);line-height:1.5;font-size:14px;background:#fff;
  padding:1.4rem clamp(1rem,4vw,2.4rem) 3rem}
 .wrap{max-width:70rem;margin:0 auto}
 .head{border-bottom:1px solid var(--rule);padding-bottom:.85rem}
 h1{font-size:1.45rem;margin:0;color:var(--navy-d);letter-spacing:.2px}
 h2{font-size:1.02rem;color:var(--navy-d);border-bottom:2px solid var(--navy);padding-bottom:.25rem;
  margin:1.7rem 0 .7rem;letter-spacing:.2px}
 h3{font-size:.92rem;color:var(--navy-d);margin:1rem 0 .4rem}
 .muted{color:var(--muted);font-size:.85rem}
 .facts{display:flex;flex-wrap:wrap;gap:.4rem;margin:.65rem 0 0}
 .fact{background:var(--soft);border:1px solid var(--card);border-radius:999px;padding:2px 11px;font-size:12px}
 .fact b{font-weight:600;color:var(--navy-d)}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:.7rem 0}
 .card{background:#fff;border:1px solid var(--card);border-left:3px solid var(--navy);border-radius:10px;
  padding:13px 16px;box-shadow:0 1px 2px rgba(20,40,80,.04)}
 .card .dim{font-size:11px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--navy)}
 .card .pname{font-size:12.5px;color:var(--muted);margin-top:3px}
 .card .pval{font-size:1.75rem;font-weight:800;color:#2b3a52;font-variant-numeric:tabular-nums;line-height:1.15;margin-top:2px}
 .card .pval small{font-size:.82rem;font-weight:600;color:var(--muted)}
 .card .prange{font-size:11.5px;color:var(--muted)}
 .card .sup{margin-top:10px;border-top:1px solid var(--rule);padding-top:7px}
 .card .sup .row{display:flex;justify-content:space-between;gap:8px;font-size:12px;padding:1.5px 0}
 .card .sup .k{color:var(--muted)} .card .sup .v{font-weight:600;font-variant-numeric:tabular-nums;text-align:right}
 .card details{margin-top:8px} .card summary{font-size:11.5px;color:var(--navy);cursor:pointer;font-weight:600}
 .card details p{font-size:12px;color:#3a4a5a;margin:.35rem 0 0;line-height:1.45}
 table{border-collapse:collapse;width:100%;margin:.4rem 0;font-size:12.5px}
 th,td{border-bottom:1px solid var(--rule);padding:5px 9px;text-align:left;vertical-align:top}
 th{background:#fafbfd;font-weight:600;color:var(--navy-d);border-bottom:2px solid #d7dce5}
 tbody tr:hover{background:#fafcff}
 td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
 details.sec{margin:.55rem 0;border:1px solid var(--rule);border-radius:8px;background:var(--soft);padding:.55rem .9rem}
 details.sec>summary{cursor:pointer;font-weight:600;color:var(--navy-d);font-size:.92rem;list-style:none}
 details.sec>summary::-webkit-details-marker{display:none}
 details.sec>summary::before{content:"";display:inline-block;width:0;height:0;
  border-left:5px solid var(--navy);border-top:4px solid transparent;border-bottom:4px solid transparent;
  margin-right:.5rem;transition:transform .15s}
 details.sec[open]>summary::before{transform:rotate(90deg)}
 details.sec[open]>summary{margin-bottom:.45rem}
 .warn{color:#8a1c1c}
 .note{background:#fff7e6;border:1px solid #f0d59a;border-radius:8px;padding:.6rem .8rem;font-size:12.5px;margin:.6rem 0}
 .fx-input{font-size:13px;padding:3px 6px;border:1px solid var(--card);border-radius:5px;width:66px}
 img.fig{max-width:100%;height:auto;border:1px solid var(--rule);border-radius:8px;margin:.4rem 0;background:#fff}
 @media print{body{padding:0} h2{page-break-after:avoid} table,img{page-break-inside:avoid}
  details.sec{border:0;background:none}}
</style></head><body>
<div class="wrap">
<div class="head">
<h1>Hyporheic Exchange: Site Summary</h1>
<div class="facts">
 <span class="fact"><b>Site:</b> {{ site.site_name or 'Unnamed' }}</span>
 {% if site.analyst %}<span class="fact"><b>Analyst:</b> {{ site.analyst }}{% if site.organization %} ({{ site.organization }}){% endif %}</span>{% endif %}
 {% if site.assessment_date %}<span class="fact"><b>Date:</b> {{ site.assessment_date }}</span>{% endif %}
 <span class="fact"><b>Reach:</b> {{ fmt(site.reach_length_m) }} m</span>
 <span class="fact"><b>Discharge:</b> {{ fmt(results.connectivity.streamflow_cms) }} m&sup3;/s</span>
 <span class="fact"><b>Dimensionality:</b> 3D</span>
 <span class="fact"><b>Volume basis:</b> {{ results.zone.active_volume_basis or 'bulk sediment' }}</span>
</div>
{% if site.notes %}<p class="muted" style="margin:.5rem 0 0">{{ site.notes }}</p>{% endif %}
</div>

<h2>Key Hyporheic Hydraulic Metrics</h2>
<div class="cards">
{% for c in cards %}
 <div class="card">
  <div class="dim">{{ c.dimension }}</div>
  <div class="pname">{{ c.primary_name }}</div>
  <div class="pval">{{ c.primary_value }} <small>{{ c.primary_unit }}</small></div>
  {% if c.primary_range %}<div class="prange">{{ c.primary_range }}</div>{% endif %}
  <div class="sup">
   {% for lab, val, unit in c.supporting %}<div class="row"><span class="k">{{ lab }}</span><span class="v">{{ val }} {{ unit }}</span></div>{% endfor %}
  </div>
  <details><summary>What this means</summary><p>{{ c.definition }}</p><p class="muted">{{ c.relevance }}</p></details>
 </div>
{% endfor %}
</div>

{% if rtd_png_b64 or threshold_b64 or planview_b64 or section_b64 %}
<h2>Figures</h2>
{% if rtd_png_b64 %}<h3>Flux-weighted residence-time distribution</h3>
<img class="fig" src="data:image/png;base64,{{ rtd_png_b64 }}" alt="Flux-weighted residence-time distribution"/>{% endif %}
{% if planview_b64 %}<h3>Plan-view hyporheic extent</h3>
<img class="fig" src="data:image/png;base64,{{ planview_b64 }}" alt="Plan-view hyporheic exchange"/>{% endif %}
{% if section_b64 %}<h3>Returning flow paths (longitudinal section)</h3>
<img class="fig" src="data:image/png;base64,{{ section_b64 }}" alt="Longitudinal section of returning flow paths"/>{% endif %}
{% if threshold_b64 %}<h3>Threshold exceedance</h3>
<img class="fig" src="data:image/png;base64,{{ threshold_b64 }}" alt="Threshold exceedance"/>{% endif %}
{% endif %}

{% if thresholds %}
<h2>Residence Time Exceedance</h2>
<table>
 <tr><th>Scenario</th><th class="num">Threshold</th><th class="num">Exchange over threshold</th><th class="num">Functional exchange</th><th class="num">Functional connectivity</th></tr>
 {% for t in thresholds %}
 <tr><td>{{ t.label }}</td><td class="num">{{ t.threshold_h|int }} hr</td><td class="num">{{ t.exceedance_pct }}%</td><td class="num">{{ t.functional_l_s }} L/s</td><td class="num">{{ t.functional_per_km }}</td></tr>
 {% endfor %}
</table>
{% endif %}

<details class="sec"><summary>Detailed hydraulic metrics</summary>
{% for section, items in grouped %}
<h3>{{ section }}</h3>
<table><tr><th>Metric</th><th class="num">Value</th><th>Unit</th></tr>
{% for r in items %}<tr><td>{{ r.name }}</td><td class="num">{{ r.value }}</td><td>{{ r.unit }}</td></tr>{% endfor %}
</table>
{% endfor %}
</details>

<details class="sec"><summary>Model quality and flow accounting</summary>
<table>
 <tr><td>Mass-balance error</td><td class="num">{{ fmt(results.connectivity.mass_balance_error) }}</td></tr>
 <tr><td>Returning flow fraction</td><td class="num">{{ fmt(results.connectivity.returning_flow_fraction) }}</td></tr>
 <tr><td>Censored flow fraction</td><td class="num">{{ fmt(results.connectivity.censored_flow_fraction) }}</td></tr>
 <tr><td>Net groundwater exchange</td><td class="num">{{ fmt(results.connectivity.net_stream_exchange_cms) }} m&sup3;/s</td></tr>
 <tr><td>Effective particle count</td><td class="num">{{ fmt(results.residence_time.effective_particle_count) }}</td></tr>
</table>
<ul>{% for w in results.warnings %}<li class="warn">{{ w.message }}</li>{% else %}<li class="muted">No quality warnings recorded.</li>{% endfor %}</ul>
</details>

{% if input_rows %}
<details class="sec"><summary>Model inputs and assumptions</summary>
<table><tr><th>Group</th><th>Input</th><th class="num">Value</th><th>Unit</th></tr>
{% for r in input_rows %}<tr><td>{{ r.section }}</td><td>{{ r.name }}</td><td class="num">{{ r.value }}</td><td>{{ r.unit }}</td></tr>{% endfor %}
</table>
</details>
{% endif %}

{% if sensitivity_rows %}
<details class="sec"><summary>Sensitivity and uncertainty</summary>
<p class="muted">Ranges reflect sensitivity to the tested gradient assumptions and are not statistical
 confidence intervals.</p>
<table><tr><th>Metric</th><th class="num">Preferred</th><th class="num">Min</th><th class="num">Max</th><th class="num">Range</th></tr>
{% for r in sensitivity_rows %}<tr><td>{{ r.metric }}</td><td class="num">{{ r.preferred }}</td><td class="num">{{ r.min }}</td><td class="num">{{ r.max }}</td><td class="num">{{ r.range }}</td></tr>{% endfor %}
</table>
</details>
{% endif %}

</div>
</body></html>"""


def render_html(results: AssessmentResultsV2, *, app_version=None, model_version=None,
                figures: dict | None = None, rtd_dist: dict | None = None) -> str:
    """Render the self-contained report HTML. `figures` is a dict of PNG bytes keyed
    rtd/planview/section/threshold; `rtd_dist` is the returning-path RTD embedded for the
    client-side custom-threshold recompute (works offline in the downloaded file)."""
    import base64

    env = Environment(autoescape=select_autoescape(["html", "xml"]))
    env.filters["fmt"] = fmt
    template = env.from_string(_HTML_TEMPLATE)
    rows = metric_rows(results)
    grouped: list[tuple[str, list]] = []
    for r in rows:
        if not grouped or grouped[-1][0] != r["section"]:
            grouped.append((r["section"], []))
        grouped[-1][1].append(r)
    site = results.input_snapshot.site if results.input_snapshot else None
    from types import SimpleNamespace
    site = site or SimpleNamespace(site_name=None, analyst=None, organization=None,
                                   assessment_date=None, reach_length_m=None, notes=None)
    figs = figures or {}

    def _b64(key):
        b = figs.get(key)
        return base64.b64encode(b).decode() if b else None

    return template.render(
        results=results, site=site, cards=headline_cards(results), grouped=grouped,
        thresholds=threshold_rows(results), fmt=fmt,
        input_rows=input_rows(results), data_sources=data_source_rows(results),
        sensitivity_rows=sensitivity_rows(results), references=report_references(results),
        rtd_png_b64=_b64("rtd"), threshold_b64=_b64("threshold"),
        planview_b64=_b64("planview"), section_b64=_b64("section"),
        rtd_blob=(json.dumps(rtd_dist) if rtd_dist else None),
        generated_at=(results.created_at.isoformat() if results.created_at else "n/a"),
        method_version=REPORT_METHOD_VERSION,
        app_version=app_version, model_version=model_version)


def render_pdf(results: AssessmentResultsV2, path, *, app_version=None,
               model_version=None, figures: dict | None = None) -> str:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (
        Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle)

    figures = figures or {}
    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    hdr = colors.HexColor("#2f4b7c")

    def _table(header, body, widths):
        data = [header] + body
        t = Table(data, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), hdr),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return t

    story = [Paragraph("Hyporheic Exchange: Site Summary", styles["Title"])]
    snap = results.input_snapshot
    site = snap.site if snap else None

    # Section 1: site identity (was PDF-missing)
    if site is not None:
        story.append(Spacer(1, 0.12 * inch))
        ident = [["Site", site.site_name or "n/a", "Analyst",
                  (site.analyst or "n/a") + (f" ({site.organization})" if site.organization else "")],
                 ["Date", (site.assessment_date.isoformat() if site.assessment_date else "n/a"),
                  "Reach length", f"{fmt(site.reach_length_m)} m"]]
        it = Table(ident, colWidths=[0.9 * inch, 2.55 * inch, 0.9 * inch, 2.15 * inch])
        it.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8),
                                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f4f8")),
                                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f0f4f8"))]))
        story.append(it)
        if site.notes:
            story.append(Paragraph(f"<i>{site.notes}</i>", small))
    story.append(Spacer(1, 0.16 * inch))

    # Three headline dimensions
    story.append(Paragraph("Key Hyporheic Hydraulic Metrics", styles["Heading2"]))
    card_body = []
    for c in headline_cards(results):
        sup = "; ".join(f"{lab} {val} {unit}".strip() for lab, val, unit in c["supporting"])
        val = f"{c['primary_value']} {c['primary_unit']}"
        if c.get("primary_range"):
            val += f" ({c['primary_range']})"
        card_body.append([Paragraph(c["dimension"], small), Paragraph(c["primary_name"], small),
                          Paragraph(val, small), Paragraph(sup, small)])
    story.append(_table(["Dimension", "Metric", "Value", "Supporting"], card_body,
                        [1.2 * inch, 1.5 * inch, 1.7 * inch, 1.9 * inch]))
    story.append(Spacer(1, 0.16 * inch))

    story.append(Paragraph("Detailed metrics", styles["Heading2"]))
    story.append(_table(["Section", "Metric", "Value", "Unit"],
                        [[r["section"], r["name"], r["value"], r["unit"]] for r in metric_rows(results)],
                        [1.4 * inch, 2.4 * inch, 1.6 * inch, 0.9 * inch]))

    irows = input_rows(results)
    if irows:
        story.append(Spacer(1, 0.16 * inch))
        story.append(Paragraph("Model inputs", styles["Heading2"]))
        story.append(_table(["Group", "Input", "Value", "Unit"],
                            [[r["section"], r["name"], r["value"], r["unit"]] for r in irows],
                            [1.2 * inch, 2.4 * inch, 1.8 * inch, 0.9 * inch]))

    fig_specs = [("rtd", "Residence-time distribution"),
                 ("planview", "Plan-view hyporheic extent"),
                 ("section", "Returning flow paths (longitudinal section)"),
                 ("threshold", "Threshold exceedance")]
    if any(figures.get(k) for k, _ in fig_specs):
        story.append(Spacer(1, 0.16 * inch))
        story.append(Paragraph("Figures", styles["Heading2"]))
        for key, title in fig_specs:
            b = figures.get(key)
            if not b:
                continue
            story.append(Paragraph(title, styles["Heading3"]))
            img = Image(io.BytesIO(b))
            img._restrictSize(6.9 * inch, 3.4 * inch)
            story.append(img)

    trows = threshold_rows(results)
    if trows:
        story.append(Spacer(1, 0.16 * inch))
        story.append(Paragraph("Residence Time Exceedance", styles["Heading2"]))
        story.append(_table(
            ["Scenario", "Threshold", "Over threshold", "Functional flow", "Functional /km"],
            [[r["label"], f"{int(r['threshold_h'])} hr", f"{r['exceedance_pct']}%",
              f"{r['functional_l_s']} L/s", r["functional_per_km"]] for r in trows],
            [1.7 * inch, 0.8 * inch, 1.2 * inch, 1.3 * inch, 1.2 * inch]))

    srows = sensitivity_rows(results)
    if srows:
        story.append(Spacer(1, 0.16 * inch))
        story.append(Paragraph("Sensitivity and uncertainty", styles["Heading2"]))
        story.append(Paragraph("Ranges reflect sensitivity to the tested gradient assumptions and "
                               "are not statistical confidence intervals.", small))
        story.append(_table(["Metric", "Preferred", "Min", "Max", "Range"],
                            [[r["metric"], r["preferred"], r["min"], r["max"], r["range"]] for r in srows],
                            [1.9 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch, 1.1 * inch]))

    story.append(Spacer(1, 0.16 * inch))
    story.append(Paragraph("Warnings &amp; limitations", styles["Heading2"]))
    if results.warnings:
        for w in results.warnings:
            story.append(Paragraph("• " + w.message, small))
    else:
        story.append(Paragraph("None recorded.", small))
    rid = results.assessment_id

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.grey)
        canvas.drawString(0.75 * inch, 0.5 * inch, f"HYPE Site Summary · report {rid}")
        canvas.drawRightString(letter[0] - 0.75 * inch, 0.5 * inch, f"Page {doc.page}")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=letter,
                            title=f"HYPE Site Summary {rid}")
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return str(path)


def generate_report(results: AssessmentResultsV2, out_dir, *, transit_rows=None,
                    app_version=None, model_version=None, spatial=None) -> dict:
    """Write every format into out_dir; return {format: path}. Retryable without a model run.

    `spatial` (optional) supplies already-loaded map data for the plan-view + section figures:
    {"planview": {down_fc, up_fc, footprint_fc, reach_lonlat, domain_lonlat},
     "paths_gdf": <returning paths GeoDataFrame>, "reach_line": <shapely LineString, metric CRS>}."""
    from . import figures as fig_mod

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rows = transit_rows or []
    ret = [r for r in rows if r.get("endpoint_class") == "returning"]

    # Figures (report §10, §17.4), all best-effort -> shared by HTML + PDF.
    figures: dict = {}
    if len(ret) >= 2:
        try:
            figures["rtd"] = render_rtd_figure([r["transit_time_days"] for r in ret],
                                               [r["flow_weight"] for r in ret])
        except Exception:  # noqa: BLE001 — figures are best-effort
            pass
    figures["threshold"] = fig_mod.render_threshold_bar(results.thresholds)
    if spatial:
        figures["planview"] = fig_mod.render_planview_figure(**(spatial.get("planview") or {}))
        figures["section"] = fig_mod.render_section_figure(spatial.get("paths_gdf"),
                                                           spatial.get("reach_line"))
    figures = {k: v for k, v in figures.items() if v}
    for key, fname in (("rtd", "rtd_distribution.png"), ("threshold", "threshold_exceedance.png"),
                       ("planview", "planview.png"), ("section", "section.png")):
        if figures.get(key):
            (out / fname).write_bytes(figures[key])

    # RTD blob for the HTML's client-side custom-threshold recompute (returning subset).
    rtd_dist = None
    if ret:
        rtd_dist = {
            "t_hours": [float(r["transit_time_days"]) * 24.0 for r in ret],
            "w": [float(r["flow_weight"]) for r in ret],
            "q_hef_l_s": (results.connectivity.returning_hyporheic_cms or 0.0) * 1000.0,
            "c_per_km": results.connectivity.turnovers_per_km or 0.0}

    paths = {
        "json": str(out / "assessment_results.json"),
        "html": str(out / "site_report.html"),
        "csv_metrics": write_site_metrics_csv(results, out / "site_metrics.csv"),
        "csv_transit": write_transit_times_csv(rows, out / "hyporheic_transit_times.csv"),
        "run_summary": write_run_summary_json(results, out / "run_summary.json",
                                              app_version=app_version, model_version=model_version),
        "rtd_json": write_rtd_distribution_json(rows, out / "rtd_distribution.json"),
    }
    Path(paths["json"]).write_text(results_to_json(results), encoding="utf-8")
    Path(paths["html"]).write_text(
        render_html(results, app_version=app_version, model_version=model_version,
                    figures=figures, rtd_dist=rtd_dist),
        encoding="utf-8")
    try:
        paths["pdf"] = render_pdf(results, out / "site_report.pdf",
                                  app_version=app_version, model_version=model_version,
                                  figures=figures)
    except Exception as e:  # noqa: BLE001 — PDF is best-effort; other formats still land
        paths["pdf_error"] = str(e)
    return paths


__all__ = [
    "fmt", "metric_rows", "headline_cards", "threshold_rows", "input_rows", "data_source_rows",
    "sensitivity_rows", "report_references", "render_rtd_figure", "results_to_json",
    "write_site_metrics_csv", "write_transit_times_csv", "run_summary_dict",
    "write_run_summary_json", "write_rtd_distribution_json", "render_html", "render_pdf",
    "generate_report", "REPORT_METHOD_VERSION", "RUN_SUMMARY_SCHEMA_VERSION",
    "RTD_DISTRIBUTION_SCHEMA_VERSION",
]
