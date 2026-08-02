"""Site Summary Report generation (revision spec §11).

Every output — the in-app modal, self-contained HTML, native PDF, and the two CSVs — reads ONLY
the canonical `AssessmentResultsV2` model (§11.2), and all of them render from ONE flat
`metric_rows()` list, so the numbers agree across formats by construction (§13.6). User-entered
text is HTML-escaped. Report generation is a pure function of the results model, so it can be
retried without rerunning the model (§11.5).
"""
from __future__ import annotations

import csv
import html
import io
import json
import math
from pathlib import Path

from jinja2 import Environment, select_autoescape

from . import dims
from .contracts import AssessmentResultsV2
from .fmt import fmt, fmt_range, fmt_sig

REPORT_METHOD_VERSION = "site-report/2.0"
RUN_SUMMARY_SCHEMA_VERSION = "hype-run-summary/1.0"
RTD_DISTRIBUTION_SCHEMA_VERSION = "hype-rtd-distribution/1.0"

# Display names of the three hydraulic dimensions (report §5/§6/§7): the single source for the
# scorecards, detailed metric tables, PDF, and the site_metrics.csv section values. Machine keys
# (run-summary/results schemas, CSV headers) are independent of these and must not change.
#
# They live in `dims` now rather than here, because `signature.py` and `functions/registry.py` both
# name the dimensions and neither can import this module (report imports signature, not the other
# way round). These three aliases stay so every existing `report.DIM_FREQUENCY` reader is
# unaffected, and the STRINGS are unchanged, so site_metrics.csv is byte-identical.
DIM_FREQUENCY = dims.DIM_LABEL[dims.FREQUENCY]
DIM_DURATION = dims.DIM_LABEL[dims.DURATION]
DIM_EXTENT = dims.DIM_LABEL[dims.EXTENT]


def should_autoopen(prev_hash, cur_hash) -> bool:
    """Fire the report modal once per completed run: true only when there is a current run whose
    input hash differs from the last one shown, so later site-metadata edits do not reopen it."""
    return bool(cur_hash) and cur_hash != prev_hash


#: `fmt`, `fmt_sig` and `fmt_range` are re-exported from `.fmt` above. They moved so `signature.py`
#: could format without importing this module; every `report.fmt(...)` reader still works.

def _hours(days):
    """Residence times are stored in days; the report presents hours."""
    return None if days is None else days * 24.0


def _times(cms):
    """m3/s -> L/s for compact report values."""
    return None if cms is None else cms * 1000.0


def metric_rows(results: AssessmentResultsV2) -> list[dict]:
    """The single canonical (section, name, value, unit) list every format renders from.

    Grouped by the three hydraulic dimensions (report §5-7): Frequency of Hyporheic Exchange,
    Duration in Hyporheic Zone, Extent of Hyporheic Zone."""
    c, r, z = results.connectivity, results.residence_time, results.zone
    raw: list[tuple[str, str, object, str]] = [
        # Frequency of Hyporheic Exchange (report §5)
        (DIM_FREQUENCY, "Streamflow-equivalent turnovers", c.turnovers_per_km, "turnovers/km"),
        (DIM_FREQUENCY, "River turnover length", c.turnover_length_km, "km"),
        (DIM_FREQUENCY, "Gross hyporheic exchange", _times(c.returning_hyporheic_cms), "L/s"),
        (DIM_FREQUENCY, "Exchange intensity", c.exchange_flux_mm_day, "mm/day"),
        (DIM_FREQUENCY, "Gross exchange ratio (reach)", c.gross_exchange_ratio_reach, ""),
        (DIM_FREQUENCY, "Stream discharge", c.streamflow_cms, "m³/s"),
        (DIM_FREQUENCY, "Streambed area", c.streambed_area_m2, "m²"),
        (DIM_FREQUENCY, "Active streambed fraction", c.active_streambed_fraction, "fraction"),
        # The discharge side and the union. `active_*` above is entry only (framework §4.7) and
        # stays the published cross-site basis; these say how much bed is engaged either way.
        (DIM_FREQUENCY, "Return streambed area", c.return_streambed_area_m2, "m²"),
        (DIM_FREQUENCY, "Connected streambed fraction", c.connected_streambed_fraction, "fraction"),
        (DIM_FREQUENCY, "Net groundwater exchange", c.net_stream_exchange_cms, "m³/s"),
        (DIM_FREQUENCY, "Excursions per mile", c.excursions_per_mile, "1/mi"),
        (DIM_FREQUENCY, "Mass-balance error", c.mass_balance_error, "fraction"),
        (DIM_FREQUENCY, "Returning flow fraction", c.returning_flow_fraction, "fraction"),
        # Duration in Hyporheic Zone (report §6)
        (DIM_DURATION, "Median residence time", _hours(r.weighted_median_days), "hr"),
        (DIM_DURATION, "Residence time P10", _hours(r.p10_days), "hr"),
        (DIM_DURATION, "Residence time P90", _hours(r.p90_days), "hr"),
        (DIM_DURATION, "Flow-weighted mean", _hours(r.weighted_mean_days), "hr"),
        (DIM_DURATION, "Fraction over 1 day", r.frac_above_1d, "fraction"),
        (DIM_DURATION, "Censored fraction", r.censored_fraction, "fraction"),
        (DIM_DURATION, "Effective particle count", r.effective_particle_count, ""),
        # Extent of Hyporheic Zone (report §7)
        (DIM_EXTENT, "Equivalent active depth", z.equivalent_active_depth_m, "m"),
        (DIM_EXTENT, "Active hyporheic volume", z.bulk_saturated_volume_m3, "m³"),
        (DIM_EXTENT, "Mobile pore-water storage", z.mobile_pore_storage_m3, "m³"),
        (DIM_EXTENT, "Volume basis", z.active_volume_basis, ""),
        (DIM_EXTENT, "P90 max path depth", z.path_depth_p90_m, "m"),
        (DIM_EXTENT, "P50 max path depth", z.path_depth_p50_m, "m"),
        (DIM_EXTENT, "Max path depth", z.path_depth_max_m, "m"),
        (DIM_EXTENT, "Binary footprint", z.footprint_binary_m2, "m²"),
        (DIM_EXTENT, "Mean / max thickness",
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
    return [
        {"dimension": DIM_FREQUENCY,
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
        {"dimension": DIM_DURATION,
         "primary_name": "Median residence time",
         "primary_value": fmt(med), "primary_unit": "hr",
         "primary_range": None,
         "definition": "The flux-weighted time exchanged streamwater remains in the subsurface, "
                       "reported as the median.",
         "relevance": "Residence time sets the opportunity for thermal exchange, oxygen "
                      "consumption, and nutrient or contaminant transformation. It does not "
                      "establish that a reaction occurred.",
         "supporting": [("Fraction over 1 day", pct(r.frac_above_1d), "%"),
                        ("Residence time P10", fmt(lo), "hr"),
                        ("Residence time P90", fmt(hi), "hr")]},
        {"dimension": DIM_EXTENT,
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


def alternative_range_rows(results: AssessmentResultsV2) -> list[dict]:
    """Hydraulic Alternatives ranges keyed by the metric_rows vocabulary: min to max across
    the Basecase (this document's own numbers) plus every completed alternative. One row per
    (section, name) that ranged over at least two runs. Because every scenario's sections run
    through the SAME metric_rows, units and labels line up with the detailed table by
    construction — no second mapping to drift."""
    manifest = results.alternatives
    if manifest is None or not manifest.completed():
        return []
    from .alternatives import metric_ranges
    base_rows = metric_rows(results)
    ranges = metric_ranges(manifest, base_rows)
    rows: list[dict] = []
    for r in base_rows:                                  # canonical order
        rr = ranges.get((r["section"], r["name"]))
        if rr is None or rr["n"] < 2:
            continue
        rows.append({"section": r["section"], "name": r["name"], "unit": r["unit"],
                     "lo": rr["lo"], "hi": rr["hi"],
                     "range": fmt_range(rr["lo"], rr["hi"]) or "n/a"})
    return rows


def scenario_range_map(results: AssessmentResultsV2) -> dict:
    """{(section, name): range string} for the detailed-metric tables and the CSV."""
    return {(r["section"], r["name"]): r["range"] for r in alternative_range_rows(results)}


def alternative_scenario_rows(results: AssessmentResultsV2) -> list[dict]:
    """One row per alternative scenario for the Hydraulic Alternatives section. Duration is
    presented in hours, the report's residence-time unit."""
    manifest = results.alternatives
    if manifest is None:
        return []
    from .contracts import ALT_STATUS_LABEL
    rows: list[dict] = []
    for s in manifest.scenarios:
        m = s.metrics or {}
        dur = m.get("rtd_median_days")
        rows.append({"label": s.label,
                     "factors": f"K x{s.k_factor:g}, gradient x{s.g_factor:g}",
                     "status": ALT_STATUS_LABEL.get(s.status, str(s.status)),
                     "freq": fmt(m.get("turnovers_per_km")),
                     "dur": fmt(None if dur is None else dur * 24.0),
                     "ext": fmt(m.get("equivalent_active_depth_m"))})
    return rows


def alternatives_note(results: AssessmentResultsV2) -> str | None:
    """The completion line; it doubles as the partial-range marker."""
    manifest = results.alternatives
    if manifest is None:
        return None
    return (f"{len(manifest.completed())} of {len(manifest.scenarios)} alternative runs "
            "completed. Ranges cover the Basecase plus completed alternatives only.")


#: Which metric answers which question (functions plan §10). Static text; the values slot in.
_DECISION_FRAMEWORK = [
    ("Prioritize rivers to preserve", "total",
     "You are protecting existing function, so the sites doing the most actual work win."),
    ("Prioritize rivers for restoration", "areal",
     "You are hunting for headroom. Large area with a weak areal flux has the most room to gain. "
     "Total mass alone misleads here, because a healthy big river looks attractive but has little "
     "room to improve."),
    ("Compare restoration alternatives at one site", "total",
     "Same site, so area and efficiency are both in play. The change in total mass is the benefit "
     "metric for a cost comparison."),
    ("Regulatory decision, for example a TMDL", "total",
     "Load allocations are written in mass per time, so this is the number that plugs into the "
     "accounting."),
]


def _pct(x, digits: int = 1) -> str | None:
    """Percent to one decimal. Three would imply a precision these screening methods do not have,
    and the values sit next to ranges spanning a factor of three."""
    return None if x is None else fmt(x * 100.0, digits)


def _conc(x) -> str | None:
    """A concentration a reader can act on. Saturated removal drives the outlet concentration to
    something like 7e-14 mg/L, and printing that reads as instrument noise rather than what it
    means, which is that effectively none of the entering load came back out."""
    if x is None:
        return None
    return "under 0.01" if 0 < x < 0.01 else fmt_sig(x)


def _pct_sig(x, sig: int = 3) -> str | None:
    """A percentage in SIGNIFICANT FIGURES, for shares that span orders of magnitude.

    `_pct`'s single decimal suits a share of exchanged flow. It destroys a share of STREAMFLOW: on
    a large river the hyporheic return is a few thousandths of a percent, which rounds to a flat
    "0" and reads as "none" rather than "small"."""
    return None if x is None else f"{fmt_sig(x * 100.0, sig)}%"


def _num(x, digits: int = 3) -> str | None:
    """`fmt`, but a missing value stays None so `_rows` DROPS the row.

    `fmt` itself renders None as "n/a", which is right for a table whose shape is fixed and wrong
    for one built from whatever resolved: a section that computed no mass would otherwise print a
    derivation chain of "n/a" and hang a decision framework off it."""
    return None if x is None else fmt(x, digits)


def _sig(x, sig: int = 3) -> str | None:
    """`fmt_sig` under the same rule as `_num`."""
    return None if x is None else fmt_sig(x, sig)


def _rows(*pairs) -> list[dict]:
    """(name, value) pairs, dropping any whose value is missing."""
    return [{"name": n, "value": v} for n, v in pairs if v is not None]


def _references(section) -> list[str]:
    """One formatted reference line per source, so the report prints a list rather than a
    paragraph. Falls back to the flat `citation` string for a section that names no sources
    (the contaminant calculator, which genuinely has nothing to cite) or an older payload."""
    from .functions.helptext import SOURCES

    keys = list(getattr(section, "source_keys", None) or [])
    if not keys:
        return [section.citation] if section.citation else []
    return [SOURCES[k].reference() for k in keys if k in SOURCES]


def function_sections(results: AssessmentResultsV2) -> list[dict]:
    """Every screening section for the report (functions plan Part II). [] when none ran.

    One entry per CALCULATOR RUN, not per function: Pollutant Attenuation contributes one section
    per ticked dissolved endpoint plus microplastic retention, and a section whose toggle was off
    never reaches here at all. Each entry is a title plus a flat row list, so the template loops
    rather than carrying a bespoke block apiece. The mass-bearing entries additionally carry the
    four-metric chain and the decision framework, because those are a derivation whose order is
    the point."""
    fn = results.functions
    if fn is None:
        return []
    out: list[dict] = []

    n = fn.nutrient
    if n is not None:
        rows = _rows(
            ("Returning flow paths", (None if n.n_paths is None else fmt(n.n_paths))),
            # The gate is a user choice now, so the document has to say which way it was set. Only
            # printed when the section actually answered: `fmt` turns None into "yes"/"no"'s
            # cousin "n/a", which would read as a third state that does not exist.
            ("Oxygen limitation applied",
             (None if n.oxygen_gate is None else fmt(n.oxygen_gate))),
            # `_num`, NOT `fmt`. `fmt` renders None as the string "n/a", which keeps the row and
            # makes it read as missing data. With the gate switched off there IS no onset and no
            # dissolved oxygen in play, so the rows have to leave entirely -- the line above
            # already says why, and "Time to anoxia: n/a" underneath it would suggest the run
            # tried to derive one and failed.
            ("Time to anoxia (h)", _num(n.time_to_anoxia_hours, 2)),
            ("Stream dissolved oxygen (mg/L)", _num(n.dissolved_oxygen_mg_l)),
            ("Exchange reaching anoxia (%)", _pct(n.fraction_above_threshold)),
            ("Exchange staying oxic (%)", _pct(n.fraction_below_threshold)),
            ("Stream nitrate", (None if n.inlet_concentration_mg_l is None else
                                f"{fmt(n.inlet_concentration_mg_l)} {n.nitrate_basis_label or ''}")),
            ("Implied zero-order rate (mg N L⁻¹ day⁻¹)",
             fmt(n.implied_zero_order_rate_mg_l_day, 2)),
            ("Monod half-saturation (mg/L as N)", fmt(n.monod_half_saturation_mg_l, 2)),
            # The ratio itself, not just its two ingredients. The validity warning is a cliff at
            # 1.0 and that is correct, but a reader at 0.9 could not previously see it coming.
            ("Nitrate vs Monod half-saturation", fmt(n.saturation_ratio, 2)),
        )
        chain = _rows(
            ("Removal efficiency (%)", _pct(n.removal_efficiency)),
            ("Outlet concentration (mg/L)", _conc(n.outlet_concentration_mg_l)),
            ("Areal removal rate (g N m⁻² day⁻¹)", fmt_sig(n.areal_removal_rate_g_m2_day)),
            ("Hyporheic streambed area (m²)", fmt(n.reference_area_m2)),
            ("Removal per stream km (kg N day⁻¹ km⁻¹)", fmt_sig(n.removal_per_km_kg_day)),
            ("Total removed (kg N day⁻¹)", fmt_sig(n.total_removed_kg_day)),
            ("Total removed (lb N day⁻¹)", fmt_sig(n.total_removed_lb_day)),
        )
        values = {"total": (fmt_sig(n.total_removed_kg_day), "kg N day⁻¹"),
                  "areal": (fmt_sig(n.areal_removal_rate_g_m2_day), "g N m⁻² day⁻¹")}
        _rng = fmt_range(n.total_removed_low_kg_day, n.total_removed_high_kg_day)
        out.append({
            "key": "nutrient", "title": n.process_label or "Nutrient Cycling",
            # THE LEDE FOLLOWS THE GATE. It used to assert the onset unconditionally, which is
            # simply false on a run screened with the oxygen limitation switched off -- and a
            # document that describes a mechanism the numbers below it did not use is worse than
            # one that says nothing. The note comes from `screen.py`, so pane and report word it
            # identically.
            "lede": ("Nitrate removed by denitrification. " + (
                n.oxygen_gate_note or
                "Oxygen is consumed first, so removal begins on each flow path only once "
                "dissolved oxygen falls below the anoxic threshold. Time to anoxia is derived, "
                "not entered.")),
            "rows": rows, "chain": chain,
            "range": (None if _rng is None else f"{_rng} kg N per day"),
            "range_note": ("Sensitivity bounds spanning both the denitrification rate and the "
                           "oxygen consumption rate, which together set when removal begins. Not "
                           "a confidence interval. A single value means the sweep did not move "
                           "the estimate, usually because removal has already run to completion "
                           "on nearly every flow path."),
            "decisions": [{"context": ctx,
                           "metric": ("Total mass removed" if k == "total"
                                      else "Areal removal rate"),
                           "value": f"{values[k][0]} {values[k][1]}", "reasoning": why}
                          for ctx, k, why in _DECISION_FRAMEWORK] if chain else [],
            "validity_note": n.first_order_validity_note,
            "unavailable_reason": n.unavailable_reason,
            "citation": n.citation, "references": _references(n),
            "transferability_note": n.transferability_note,
        })

    # ONE SECTION PER TICKED ENDPOINT. The section used to be singular because the pane could only
    # carry one contaminant; now a report can compare zinc against acesulfame, and each element
    # brings its own vocabulary, units and citation. `dissolved_endpoints` also resolves a payload
    # written before the list existed.
    for p in fn.dissolved_endpoints():
        # Same split as the nutrient section above: what was supplied and what the hydraulics give
        # for free, then the derivation, whose ORDER is the content. It was one flat table, which
        # made a section answering the same question of a different solute look like a different
        # kind of result.
        # The §7 vocabulary in effect, so the report says the same words the pane does. A metal
        # never reads "removal" here either: `pollutants.TERMS` supplies the noun and is validated
        # against the reference's banned-word table.
        act = (p.headline_label or "Concentration reduction")
        rows = _rows(
            ("Endpoint", p.preset_label or p.contaminant_name),
            ("Rate provenance", None if p.rate_derived is None else
             ("derived by unit conversion" if p.rate_derived else "reported by the authors")),
            ("Returning flow paths", _num(p.n_paths)),
            (f"Stream concentration ({p.concentration_unit or 'mg/L'})",
             _num(p.inlet_concentration_mg_l)),
            ("Attenuation rate (1/day)", _num(p.rate_value)),
            ("Reactive exposure (m³)", _sig(p.reactive_exposure_m3)),
            # Reference §4.4, rule 14. Whether the rate matters at all belongs beside the inputs,
            # because it decides how much weight the derivation below can carry.
            ("Median residence time (days)", _sig(p.t50_days)),
            ("Damkohler number", _sig(p.damkohler, 2)),
            ("Exchange limitation", p.damkohler_regime),
        )
        chain = _rows(
            (f"{act} (%)", _pct(p.removal_efficiency)),
            # Rule 5: name whose water this is. The stream figure sits two rows below, so the two
            # cannot be read as the same quantity.
            ("Returning water concentration (mg/L)", _conc(p.outlet_concentration_mg_l)),
            (f"{p.areal_label or 'Per streambed area'} ({p.areal_rate_unit or 'g/m²/day'})",
             _sig(p.areal_removal_rate_g_m2_day)),
            ("Hyporheic streambed area (m²)", _num(p.reference_area_m2)),
            (f"{p.per_km_label or 'Per stream km'} ({p.per_km_unit or 'kg/day/km'})",
             _sig(p.removal_per_km_kg_day)),
            (f"{p.mass_label or 'Total'} (kg day⁻¹)", _sig(p.total_removed_kg_day)),
            (f"{p.mass_label or 'Total'} (lb day⁻¹)", _sig(p.total_removed_lb_day)),
            ("Hyporheic return as a share of streamflow", _pct_sig(p.exchange_ratio)),
            ("Stream concentration change (mg/L)", _sig(p.stream_concentration_change_mg_l)),
            ("Reach-scale reduction", _pct_sig(p.reach_removal_fraction)),
            ("Processing length (m)", _sig(p.processing_length_m)),
        )
        values = {"total": (fmt_sig(p.total_removed_kg_day), "kg day⁻¹"),
                  "areal": (fmt_sig(p.areal_removal_rate_g_m2_day), "g m⁻² day⁻¹")}
        _span = fmt_range(p.total_removed_low_kg_day, p.total_removed_high_kg_day)
        _rng_pol = None if _span is None else f"{_span} kg per day"
        endpoint = p.preset_label or p.contaminant_name
        out.append({
            # ONE ID PER ENDPOINT, since there are now as many sections as ticked endpoints. A
            # payload from before the multi-select carries no preset key and keeps the old id.
            "key": f"pollutant.{p.preset_key}" if p.preset_key else "pollutant",
            "title": endpoint or p.process_label or "Dissolved Pollutants",
            # There are FOUR functions and several calculators. Pollutant Attenuation hosts the
            # dissolved endpoints and microplastic retention; `function` groups them and
            # `mechanism` names this one, so the report renders one h3 with an h4 per endpoint
            # rather than a row of peer sections.
            "function": "pollutant", "mechanism": endpoint or "Dissolved phase",
            "parent": "pollutant",
            "lede": ("First-order attenuation of one endpoint along returning flow paths, with "
                     "no redox gate, so attenuation begins as soon as water enters the bed. "
                     "Nothing here is destroyed: the metals endpoints are reversible sorption and "
                     "the organics are transformation."),
            # Every condition the reference will not let a result ship without, in one place.
            "conditions": list(p.eligibility_conditions or []),
            "guard_notes": [n for n in (p.calibration_note, p.depth_note, p.preset_note) if n],
            "rows": rows, "chain": chain,
            # A RANGE ONLY FOR A CITED ENDPOINT. `_sensitivity_bounds` falls back to factor-of-two
            # around whatever rate is in effect, so for a user-supplied number the corners are the
            # app's own invention and calling them a reported range would claim a provenance that
            # does not exist. A preset brings a real spread (+/- 1 SD of the measured rate
            # constants for the metals, the measured 0.30 to 2.52 for acesulfame). The pane applies
            # the same rule, so the two surfaces cannot disagree.
            "range": (None if p.preset_key is None else _rng_pol),
            "range_note": ("Sensitivity bounds from the published spread of the rate constant, "
                           "not a confidence interval. A single value means the sweep did not "
                           "move the estimate, which above a Damkohler number of about 100 is "
                           "expected: there the exchange flux sets the answer and the rate "
                           "carries no information."),
            "decisions": [{"context": ctx,
                           "metric": (p.mass_label or "Total")
                           if k == "total" else (p.areal_label or "Per streambed area"),
                           "value": f"{values[k][0]} {values[k][1]}", "reasoning": why}
                          for ctx, k, why in _DECISION_FRAMEWORK] if chain else [],
            "unavailable_reason": p.unavailable_reason,
            "citation": p.citation, "references": _references(p),
            "transferability_note": p.transferability_note,
        })

    mp = getattr(fn, "microplastic", None)
    if mp is not None:
        # DORMANT since 2026-08-01: microplastics is unregistered (see `registry.PROCESSES`), so
        # `assess` never packs this field and the guard above never opens. Kept rather than deleted
        # for the same reason `assess` keeps its three particulate lines -- re-registering the
        # calculator then needs no edit here, and this block is guarded, so it cannot misfire.
        #
        # A DIFFERENT CALCULATOR, and the section says so rather than looking like the others with
        # different numbers in it. Tier A is the reported retention; Tier B is a capability check
        # and the two are never summed (reference rule 11).
        rows = _rows(
            ("Reach length (m)", _num(mp.reach_length_m)),
            ("Retention coefficient (1/km)", _sig(mp.alpha_mp_per_km, 3)),
            ("Particle size (µm)", _num(mp.particle_size_um)),
            ("Median grain size (mm)", _num(mp.median_grain_size_mm)),
            ("Particle to grain size ratio", _sig(mp.size_ratio, 3)),
            ("Size gate", mp.size_gate),
        )
        chain = _rows(
            ("Reach-scale retention (%)", _pct(mp.retained_fraction)),
            ("Capture along a flow path (%)", _pct(mp.path_capture_fraction)),
            ("Median flow path length (m)", _sig(mp.path_length_p50_m)),
            ("Filter coefficient (1/cm)", _sig(mp.lambda_f_per_cm, 2)),
            ("Capture cap (%)", _pct(mp.capture_cap)),
        )
        _rng_mp = fmt_range(mp.retained_fraction_low, mp.retained_fraction_high)
        out.append({
            "key": "microplastic", "title": mp.process_label or "Microplastic Retention",
            # Nested under Pollutant Attenuation: retention is a MECHANISM, physical rather than
            # chemical, not a fifth hyporheic function. The calculator stays entirely separate
            # (distance-driven, never time-driven); only the presentation groups them.
            "function": "pollutant", "mechanism": "Microplastics", "parent": "pollutant",
            "lede": ("Physical retention of microplastic in the bed by hyporheic exchange. This "
                     "is filtration over distance, not decay over time: retention profiles do not "
                     "change with how long water flows. Particles are stored, not degraded, and "
                     "bed turnover can return them to the stream."),
            "guard_notes": [n for n in (mp.size_gate_note, mp.tier_b_reason) if n],
            "rows": rows, "chain": chain,
            "chain_title": "Two independent readings, never added",
            "chain_header": "Reading",
            "range": (None if _rng_mp is None else
                      f"{fmt(100 * (mp.retained_fraction_low or 0), 1)} to "
                      f"{fmt(100 * (mp.retained_fraction_high or 0), 1)} percent"),
            "range_note": ("Across the 3 to 8 percent per kilometre spread Drummond et al. report "
                           "between stream classes. Not a confidence interval."),
            "decisions": [],
            "unavailable_reason": mp.unavailable_reason,
            "citation": mp.citation, "references": _references(mp),
            "transferability_note": mp.transferability_note,
        })

    h = fn.habitat
    if h is not None:
        out.append({
            "key": "habitat", "title": h.process_label or "Habitat Creation",
            "lede": ("Hydraulically connected subsurface space, reported as potential habitat "
                     "space standing in for habitat creation. Pore-water volume is the headline "
                     "because it is the water-filled space an organism could occupy. This is "
                     "potential habitat volume, never habitat quality or occupancy. Depths "
                     "normalized over the streambed are volume divided by area, not a uniform "
                     "layer of that thickness."),
            # Ordered and labelled to match the Screening pane: the pore-basis set first, then the
            # bulk-basis pair with its basis in the label. Framework §4.6 -- the two bases ship
            # together and neither is ever left for the reader to infer.
            "rows": _rows(
                ("Connected streambed coverage (%)", _pct(h.connected_streambed_fraction)),
                ("Equivalent pore-water depth (m)", fmt(h.pore_equivalent_depth_m)),
                ("Potential connected pore-water habitat volume (m³)",
                 fmt(h.habitable_pore_volume_m3)),
                ("Depth where exchange occurs (m)", fmt(h.pore_depth_active_m)),
                ("Median maximum path depth (m)", fmt(h.path_depth_p50_m)),
                ("P90 maximum path depth (m)", fmt(h.path_depth_p90_m)),
                ("Bulk sediment volume (m³)", fmt(h.bulk_volume_m3)),
                ("Equivalent depth, bulk basis (m)", fmt(h.equivalent_active_depth_m)),
                # Both sides of the coverage headline, split, with the framework's entry-only
                # fraction named as such: the Extent scorecard above still publishes that one.
                ("Water entry streambed (m²)", fmt(h.active_streambed_area_m2)),
                ("Water return streambed (m²)", fmt(h.return_streambed_area_m2)),
                ("Water entry coverage (%)", _pct(h.active_streambed_fraction)),
                ("Connected streambed area (m²)", fmt(h.connected_streambed_area_m2)),
                ("Streambed area (m²)", fmt(h.streambed_area_m2))),
            "chain": [], "range": None, "decisions": [],
            "unavailable_reason": h.unavailable_reason,
            "citation": h.citation, "references": _references(h),
            "transferability_note": h.transferability_note,
        })

    tm = fn.thermal
    if tm is not None:
        bands = [{"name": b.get("label"), "value": _pct(b.get("flow_fraction"))}
                 for b in (tm.response_bands or []) if b.get("flow_fraction") is not None]
        out.append({
            "key": "thermal", "title": tm.process_label or "Temperature Regulation",
            "lede": ("How much of the daily temperature swing returning exchange has shed, how "
                     "much water comes back that way, and how much of it stays under past a full "
                     "day. This reports buffering opportunity only: it is not degrees of cooling "
                     "and not a reach temperature change, because stream temperature is set "
                     "mainly by the surface energy budget."),
            # WHY THE DAMPED SHARE ALONE IS NOT ENOUGH, said where a reader meets the number: past
            # about three response times it pins at its ceiling and stops telling reaches apart.
            "guard_notes": [n for n in (tm.damkohler_note,) if n],
            # The first three follow the Screening pane's headline order. `remaining_anomaly` and
            # the flow both left the pane, one to a card and one as a restatement of the line above
            # it, so this is where they still reach the reader.
            "rows": _rows(
                ("Thermal response time (h)", fmt(tm.response_time_hours, 0)),
                ("Daily temperature swing damped (%)", _pct(tm.buffering_opportunity)),
                ("Buffered flow returned to the stream (L/s)",
                 fmt(tm.attenuation_weighted_flow_l_s)),
                ("Exchange held past a full day (%)", _pct(tm.fraction_above_diel)),
                ("Remaining anomaly (%)", _pct(tm.remaining_anomaly_fraction)),
                ("Attenuation-weighted connectivity (per km)",
                 fmt(tm.attenuation_weighted_connectivity_per_km)),
                ("Exchange past one response time (%)", _pct(tm.fraction_above_1tau)),
                ("Exchange past two (%)", _pct(tm.fraction_above_2tau)),
                ("Exchange past three (%)", _pct(tm.fraction_above_3tau)),
                ("Median thermal Damkohler", fmt(tm.thermal_damkohler_median, 2))),
            "chain": bands, "chain_title": "Response bands", "chain_header": "Band",
            "range": (None if tm.buffering_opportunity_low is None else
                      f"{_pct(tm.buffering_opportunity_low)}% to "
                      f"{_pct(tm.buffering_opportunity_high)}%"),
            "range_note": ("Across the 4 to 16 hour thermal response-time cases. Sensitivity "
                           "bounds, not a confidence interval."),
            "decisions": [],
            "unavailable_reason": tm.unavailable_reason,
            "citation": tm.citation, "references": _references(tm),
            "transferability_note": tm.transferability_note,
        })
    _attach_group_headings(out)
    return out


def _attach_group_headings(sections: list[dict]) -> None:
    """Head the first section of a multi-section function with the function's own name.

    Pollutant Attenuation is the only one, and it spans one section per ticked endpoint. Each of
    those renders one level down, so without this the document would open that function with a
    chemical's name and never say what the endpoints have in common. Whichever section happens to
    come first carries the heading, so unticking one does not lose it."""
    from .functions import FUNCTIONS, get_function

    seen = set()
    for s in sections:
        fn_key = s.get("function")
        if not s.get("parent") or fn_key in seen or fn_key not in FUNCTIONS:
            continue
        seen.add(fn_key)
        s["group_title"] = get_function(fn_key).display_label


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


def _log_time_ticks(ax, tmin: float, tmax: float) -> None:
    """Readable x ticks for a log time axis. matplotlib labels minor log ticks
    (2x10^n, 3x10^n, ...) whenever the span is under ~1.5 decades, which overlaps
    badly at this subplot width. Plain numbers, majors only, span-aware density."""
    from matplotlib.ticker import FuncFormatter, LogLocator, MaxNLocator, NullFormatter
    span = math.log10(tmax / tmin)
    if span >= 2.0:
        ax.xaxis.set_major_locator(LogLocator(base=10.0, subs=(1.0,)))
    elif span >= 0.5:
        ax.xaxis.set_major_locator(LogLocator(base=10.0, subs=(1.0, 2.0, 5.0)))
    else:
        ax.xaxis.set_major_locator(MaxNLocator(nbins=4, steps=[1, 2, 2.5, 5, 10]))
    ax.xaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x:g}"))
    ax.xaxis.set_minor_formatter(NullFormatter())
    ax.tick_params(axis="x", labelsize=8.5)


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
    _log_time_ticks(ax1, ts.min(), ts.max())
    ax1.set_xlabel("Residence time (days, log)")
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
    _log_time_ticks(ax2, ts.min(), ts.max())
    ax2.set_xlabel("Residence time (days, log)")
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
    # The scenario_range column appears ONLY when alternatives exist, so projects without a
    # sweep keep byte-identical CSV output (versioned-schema discipline).
    rmap = scenario_range_map(results)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["section", "metric", "value", "unit"]
                   + (["scenario_range"] if rmap else []))
        for r in rows:
            w.writerow([r["section"], r["name"], r["value"], r["unit"]]
                       + ([rmap.get((r["section"], r["name"]), "")] if rmap else []))
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
        "return_streambed_area_m2": c.return_streambed_area_m2,
        "connected_streambed_area_m2": c.connected_streambed_area_m2,
        "connected_streambed_fraction": c.connected_streambed_fraction,
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

    # Function screening, flat so the cross-site table can consume it directly. One prefix per
    # section, so a new function lands beside these without renaming anything.
    fn = results.functions
    n = getattr(fn, "nutrient", None) if fn else None
    if n is not None:
        out.update({
            # First, because it decides whether the three that follow entered the estimate at all.
            # A cross-site table that mixes gated and ungated reaches without this column is
            # comparing two different questions.
            "denit_oxygen_gate": n.oxygen_gate,
            "denit_dissolved_oxygen_mg_l": n.dissolved_oxygen_mg_l,
            "denit_oxygen_consumption_mg_l_day": n.oxygen_consumption_mg_l_day,
            "denit_time_to_anoxia_hr": n.time_to_anoxia_hours,
            "denit_fraction_above_threshold": n.fraction_above_threshold,
            "denit_fraction_below_threshold": n.fraction_below_threshold,
            "denit_reactive_exposure_m3": n.reactive_exposure_m3,
            "denit_removal_efficiency": n.removal_efficiency,
            "denit_areal_removal_rate_g_m2_day": n.areal_removal_rate_g_m2_day,
            "denit_reference_area_m2": n.reference_area_m2,
            "denit_reference_area_basis": n.reference_area_basis,
            "denit_total_removed_kg_day": n.total_removed_kg_day,
            "denit_total_removed_lb_day": n.total_removed_lb_day,
            "denit_total_removed_low_kg_day": n.total_removed_low_kg_day,
            "denit_total_removed_high_kg_day": n.total_removed_high_kg_day,
            "denit_inlet_concentration_mg_l": n.inlet_concentration_mg_l,
            "denit_nitrate_basis": n.nitrate_basis,
            "denit_total_removed_kg_n_day": n.total_removed_kg_n_day,
            "denit_rate_per_day": n.rate_value,
            "denit_implied_zero_order_mg_l_day": n.implied_zero_order_rate_mg_l_day,
            "denit_monod_half_saturation_mg_l": n.monod_half_saturation_mg_l,
            "denit_saturation_ratio": n.saturation_ratio,
            "denit_first_order_validity_note": n.first_order_validity_note,
            "denit_n_paths": n.n_paths,
            "denit_kinetics": n.kinetics,
            "denit_method_version": n.method_version,
            "denit_opportunity_curve": [{"tau_hours": p.tau_hours, "opportunity": p.opportunity}
                                        for p in n.opportunity_curve],
        })
    p = getattr(fn, "pollutant", None) if fn else None
    if p is not None:
        out.update({
            "pollutant_name": p.contaminant_name,
            "pollutant_preset": p.preset_key,
            "pollutant_endpoint_type": p.endpoint_type,
            "pollutant_rate_derived": p.rate_derived,
            "pollutant_concentration_mg_l": p.inlet_concentration_mg_l,
            "pollutant_rate_per_day": p.rate_value,
            "pollutant_removal_efficiency": p.removal_efficiency,
            "pollutant_areal_rate_g_m2_day": p.areal_removal_rate_g_m2_day,
            "pollutant_total_removed_kg_day": p.total_removed_kg_day,
            # Exchange limitation, so a cross-site table can tell an informative result from one
            # that is the exchange flux restated (reference §4.4).
            "pollutant_t50_days": p.t50_days,
            "pollutant_damkohler": p.damkohler,
            "pollutant_damkohler_regime": p.damkohler_regime,
            "pollutant_exchange_ratio": p.exchange_ratio,
            "pollutant_stream_concentration_change_mg_l": p.stream_concentration_change_mg_l,
            "pollutant_processing_length_m": p.processing_length_m,
        })
    mp = getattr(fn, "microplastic", None) if fn else None
    if mp is not None:
        out.update({
            # Distance coefficients, with their units IN THE KEY NAME. alpha_MP and lambda_f differ
            # by six orders of magnitude and describe different geometry (reference §5.2), so a
            # column header that drops the unit is the likeliest way to confuse them downstream.
            "microplastic_retained_fraction": mp.retained_fraction,
            "microplastic_alpha_per_km": mp.alpha_mp_per_km,
            "microplastic_lambda_f_per_cm": mp.lambda_f_per_cm,
            "microplastic_path_capture_fraction": mp.path_capture_fraction,
            "microplastic_size_ratio": mp.size_ratio,
            "microplastic_size_gate": mp.size_gate,
            "microplastic_path_length_p50_m": mp.path_length_p50_m,
        })
    h = getattr(fn, "habitat", None) if fn else None
    if h is not None:
        out.update({
            "habitat_pore_volume_m3": h.habitable_pore_volume_m3,
            "habitat_bulk_volume_m3": h.bulk_volume_m3,
            "habitat_equivalent_depth_m": h.equivalent_active_depth_m,
            "habitat_active_streambed_area_m2": h.active_streambed_area_m2,
            "habitat_active_streambed_fraction": h.active_streambed_fraction,
            "habitat_return_streambed_area_m2": h.return_streambed_area_m2,
            "habitat_connected_streambed_area_m2": h.connected_streambed_area_m2,
            "habitat_connected_streambed_fraction": h.connected_streambed_fraction,
            "habitat_pore_equivalent_depth_m": h.pore_equivalent_depth_m,
            "habitat_pore_depth_active_m": h.pore_depth_active_m,
            "habitat_path_depth_p50_m": h.path_depth_p50_m,
            "habitat_path_depth_p90_m": h.path_depth_p90_m,
        })
    tm = getattr(fn, "thermal", None) if fn else None
    if tm is not None:
        out.update({
            "thermal_response_time_hr": tm.response_time_hours,
            "thermal_buffering_opportunity": tm.buffering_opportunity,
            "thermal_buffering_low": tm.buffering_opportunity_low,
            "thermal_buffering_high": tm.buffering_opportunity_high,
            "thermal_attenuation_weighted_flow_l_s": tm.attenuation_weighted_flow_l_s,
            "thermal_attenuation_weighted_connectivity_per_km":
                tm.attenuation_weighted_connectivity_per_km,
            "thermal_fraction_above_1tau": tm.fraction_above_1tau,
            "thermal_fraction_above_3tau": tm.fraction_above_3tau,
            # Fixed 24 h, so this is the one persistence column comparable across two runs that
            # used different response-time scenarios.
            "thermal_fraction_above_diel": tm.fraction_above_diel,
            "thermal_damkohler_median": tm.thermal_damkohler_median,
            "thermal_damkohler_regime": tm.damkohler_regime,
            "thermal_response_bands": tm.response_bands,
        })
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
<title>{{ title_name }}: Site Summary Report</title>
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
 .eyebrow{font-size:10.5px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
  color:var(--muted);margin:0 0 2px}
 h1{font-size:1.45rem;margin:0;color:var(--navy-d);letter-spacing:.2px}
 h2{font-size:1.02rem;color:var(--navy-d);border-bottom:2px solid var(--navy);padding-bottom:.25rem;
  margin:1.7rem 0 .7rem;letter-spacing:.2px}
 h3{font-size:.92rem;color:var(--navy-d);margin:1rem 0 .4rem}
 h4{font-size:.86rem;color:var(--navy-d);margin:.9rem 0 .3rem}
 /* THE STRUCTURAL BREAK between what the model computed and what was inferred from it
    (revision spec §9.3, §19.1). Part A is the hyporheic hydraulic signature and always renders;
    Part B is the optional screening layer. Heavier than an h2 on purpose: a reader must not be
    able to quote a screening estimate without having crossed this line. */
 .part{margin:2.6rem 0 0;padding-top:1.1rem;border-top:3px solid var(--navy)}
 .part:first-of-type{margin-top:1.9rem}
 .parteyebrow{font-size:10.5px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
  color:var(--navy);margin:0 0 3px}
 .parthead{font-size:1.22rem;color:var(--navy-d);margin:0;letter-spacing:.2px;border:0;padding:0}
 .partsub{color:var(--muted);font-size:.88rem;margin:.3rem 0 0;max-width:46rem}
 .part h2:first-of-type{margin-top:1.2rem}
 .muted{color:var(--muted);font-size:.85rem}
 /* the governing equation, printed so the headline is checkable by hand against it */
 .eq{font-family:"Cascadia Mono",Consolas,"SF Mono",monospace;font-size:.9rem;
  background:var(--soft);border:1px solid var(--card);border-radius:6px;padding:.5rem .7rem;
  margin:.5rem 0;display:inline-block}
 .foot{margin-top:2.4rem;padding-top:.7rem;border-top:1px solid var(--rule);
  color:var(--muted);font-size:11.5px}
 /* reference list: one entry per line with a hanging indent, never a run-on paragraph */
 p.ref{display:block;padding-left:1.1em;text-indent:-1.1em;margin:.2em 0 0;
   overflow-wrap:anywhere}
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
 img.fig{display:block;width:auto;max-width:100%;height:auto;max-height:460px;border:1px solid var(--rule);
  border-radius:8px;margin:.4rem auto;background:#fff;cursor:zoom-in}
 .maps{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:.4rem 0;align-items:start}
 .maps figure,figure.wide{margin:0}
 .maps img.fig{max-height:330px;margin:0 auto}
 figure.wide img.fig{max-height:400px;margin:0 auto}
 figcaption{font-size:11.5px;color:var(--muted);margin-top:3px;line-height:1.35;text-align:center}
 .lightbox{position:fixed;inset:0;z-index:60;display:none;flex-direction:column;align-items:center;
  justify-content:center;gap:10px;background:rgba(16,26,42,.88);cursor:zoom-out;padding:22px}
 .lightbox.open{display:flex}
 .lightbox img{max-width:96vw;max-height:90vh;width:auto;height:auto;background:#fff;border-radius:8px}
 .lightbox .cap{color:#dce5f2;font-size:12.5px;max-width:92vw;text-align:center;line-height:1.4}
 @media (max-width:640px){.maps{grid-template-columns:1fr}}
 @media print{body{padding:0} h2{page-break-after:avoid} table,img,figure{page-break-inside:avoid}
  details.sec{border:0;background:none} .lightbox{display:none !important}}
</style></head><body>
<div class="wrap">
<div class="head">
<div class="eyebrow">Hyporheic Exchange Assessment</div>
<h1>{{ title_name }}: Site Summary Report</h1>
<div class="facts">
 {% if site.site_name and site.site_name != title_name %}<span class="fact"><b>Site:</b> {{ site.site_name }}</span>{% endif %}
 {% if location %}<span class="fact"><b>Location:</b> {{ location }}</span>{% endif %}
 {% if project_name and project_name != title_name %}<span class="fact"><b>Project:</b> {{ project_name }}</span>{% endif %}
 {% if site.analyst %}<span class="fact"><b>Analyst:</b> {{ site.analyst }}{% if site.organization %} ({{ site.organization }}){% endif %}</span>{% endif %}
 {% if site.assessment_date %}<span class="fact"><b>Date:</b> {{ site.assessment_date }}</span>{% endif %}
 <span class="fact"><b>Reach:</b> {{ fmt(site.reach_length_m) }} m</span>
 <span class="fact"><b>Discharge:</b> {{ fmt(results.connectivity.streamflow_cms) }} m&sup3;/s</span>
 <span class="fact"><b>Dimensionality:</b> 3D</span>
 <span class="fact"><b>Volume basis:</b> {{ results.zone.active_volume_basis or 'bulk sediment' }}</span>
</div>
{% if site.notes %}<p class="muted" style="margin:.5rem 0 0">{{ site.notes }}</p>{% endif %}
</div>

{% if hydraulics %}
{% if map_topo_b64 or map_imagery_b64 or map_wse_b64 or map_head_b64 or map_3d_b64 %}
<h2>Site Maps</h2>
{% if map_topo_b64 or map_imagery_b64 %}
<div class="maps">
{% if map_topo_b64 %}<figure><img class="fig" src="data:image/png;base64,{{ map_topo_b64 }}" alt="Site overview on the USGS topographic basemap"/>
<figcaption>Site overview: reach centerline and boundary condition lines (USGS topographic basemap).</figcaption></figure>{% endif %}
{% if map_imagery_b64 %}<figure><img class="fig" src="data:image/png;base64,{{ map_imagery_b64 }}" alt="Site overview on USGS aerial imagery"/>
<figcaption>Aerial overview: reach centerline and boundary condition lines (USGS imagery basemap).</figcaption></figure>{% endif %}
</div>
{% endif %}
{% if map_wse_b64 or map_head_b64 %}
<div class="maps">
{% if map_wse_b64 %}<figure><img class="fig" src="data:image/png;base64,{{ map_wse_b64 }}" alt="Water surface elevation raster"/>
<figcaption>Water surface elevation over the model reach (meters). Modeled discharge: {{ fmt(results.connectivity.streamflow_cms) }} m&sup3;/s.</figcaption></figure>{% endif %}
{% if map_head_b64 %}<figure><img class="fig" src="data:image/png;base64,{{ map_head_b64 }}" alt="Simulated hydraulic head contours"/>
<figcaption>Simulated hydraulic head contours, {% if head_layer %}model layer {{ head_layer }}{% else %}top model layer{% endif %} (meters).</figcaption></figure>{% endif %}
</div>
{% endif %}
{% if map_3d_b64 %}<figure class="wide"><img class="fig" src="data:image/png;base64,{{ map_3d_b64 }}" alt="Static 3D view of the model terrain and water surface"/>
<figcaption>Model terrain and grid with the simulated water surface (isometric view, USGS imagery drape).</figcaption></figure>{% endif %}
{% endif %}

<section class="part">
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
  <details><summary>What this means</summary><p>{{ c.definition }}</p><p class="muted">{{ c.relevance }}</p><p class="muted">{{ c.caution }}</p></details>
 </div>
{% endfor %}
</div>

{% if rtd_png_b64 or threshold_b64 or planview_b64 or section_b64 or map_paths_b64 %}
<h2>Figures</h2>
{% if rtd_png_b64 %}<h3>Flux-weighted residence-time distribution</h3>
<img class="fig" src="data:image/png;base64,{{ rtd_png_b64 }}" alt="Flux-weighted residence-time distribution"/>{% endif %}
{% if planview_b64 or map_paths_b64 %}
<div class="maps">
{% if planview_b64 %}<figure><img class="fig" src="data:image/png;base64,{{ planview_b64 }}" alt="Plan-view hyporheic exchange"/>
<figcaption>Plan-view hyporheic extent.</figcaption></figure>{% endif %}
{% if map_paths_b64 %}<figure><img class="fig" src="data:image/png;base64,{{ map_paths_b64 }}" alt="Plan view of hyporheic flow paths colored by residence time"/>
<figcaption>Hyporheic flow paths (plan view), colored by residence time.</figcaption></figure>{% endif %}
</div>
{% endif %}
{% if section_b64 or threshold_b64 %}
<div class="maps">
{% if section_b64 %}<figure><img class="fig" src="data:image/png;base64,{{ section_b64 }}" alt="Longitudinal section of returning flow paths"/>
<figcaption>Returning flow paths (longitudinal section).</figcaption></figure>{% endif %}
{% if threshold_b64 %}<figure><img class="fig" src="data:image/png;base64,{{ threshold_b64 }}" alt="Threshold exceedance"/>
<figcaption>Threshold exceedance.</figcaption></figure>{% endif %}
</div>
{% endif %}
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

{% if results.warnings %}
<ul>{% for w in results.warnings %}<li class="warn">{{ w.message }}</li>{% endfor %}</ul>
{% endif %}

<details class="sec"><summary>Detailed hydraulic metrics</summary>
{% for section, items in grouped %}
<h3>{{ section }}</h3>
<table><tr><th>Metric</th><th class="num">Value</th><th>Unit</th>{% if alt_scenarios %}<th class="num">Scenario range</th>{% endif %}</tr>
{% for r in items %}<tr><td>{{ r.name }}</td><td class="num">{{ r.value }}</td><td>{{ r.unit }}</td>{% if alt_scenarios %}<td class="num">{{ alt_ranges.get((section, r.name), "") }}</td>{% endif %}</tr>{% endfor %}
</table>
{% endfor %}
</details>
</section>
{% endif %}

{% if functions %}
<section class="part">
<p class="parteyebrow">Part B</p>
<h1 class="parthead">Functional Screening Estimates</h1>
{% if hydraulics %}
<p class="partsub">Everything above is direct model output. Everything below is inferred from it.
Published reaction rates applied to the modeled flow paths, under assumptions you set. Good for
comparing sites and alternatives. Not a prediction of what this reach will do.</p>
{% else %}
<p class="partsub">Published reaction rates applied to the modeled flow paths, under assumptions
you set. Good for comparing sites and alternatives. Not a prediction of what this reach will do.
The hydraulics behind them are in the Hydraulics Report.</p>
{% endif %}

{# NO CONCEPTUAL FIGURE HERE. The framing diagram is its own report product now (Site Reports ->
   Conceptual Model, `concept_html`), because it is static: it describes the framework and says
   nothing about this run, so reprinting it at the top of every screening document made the
   document's first screen the one part of it that never changes. #}

{# Standalone screening report only: the three headline hydraulic values the estimates below are
   computed from. Without them this document would assert transformation rates with no visible
   basis, which is exactly the §9.3 distinction the panes make on screen. #}
{% if not hydraulics and cards %}
<h2>The Signature These Estimates Rest On</h2>
<div class="cards">
{% for c in cards %}
 <div class="card">
  <div class="dim">{{ c.dimension }}</div>
  <div class="pname">{{ c.primary_name }}</div>
  <div class="pval">{{ c.primary_value }} <small>{{ c.primary_unit }}</small></div>
  {% if c.primary_range %}<div class="prange">{{ c.primary_range }}</div>{% endif %}
 </div>
{% endfor %}
</div>
<p class="muted">{{ signature_sentence }} Full derivation, figures and diagnostics are in the
hydraulics report.</p>
{% endif %}

{% for s in functions %}
{# A function that spans several mechanisms heads them once, then each renders one level down. #}
{% if s.group_title %}<h3>{{ s.group_title }}</h3>{% endif %}
{% if s.parent %}<h4>{{ s.mechanism or s.title }}</h4>{% else %}<h3>{{ s.title }}</h3>{% endif %}
<p class="muted">{{ s.lede }}</p>
{% if s.conditions %}
<p class="muted"><b>Applies only where all of these hold.</b></p>
<ul class="muted">{% for c in s.conditions %}<li>{{ c }}</li>{% endfor %}</ul>
{% endif %}
{% for n in s.guard_notes or [] %}<p class="muted">{{ n }}</p>{% endfor %}
{% if s.rows %}
<table>
 <tr><th>Metric</th><th class="num">Value</th></tr>
 {% for r in s.rows %}<tr><td>{{ r.name }}</td><td class="num">{{ r.value }}</td></tr>{% endfor %}
</table>
{% endif %}
{% if s.chain %}
<h4>{{ s.chain_title or "From intensity to total load" }}</h4>
<table>
 <tr><th>{{ s.chain_header or "Step" }}</th><th class="num">Value</th></tr>
 {% for r in s.chain %}<tr><td>{{ r.name }}</td><td class="num">{{ r.value }}</td></tr>{% endfor %}
</table>
{% if s.key == "nutrient" %}
<p class="muted">Total mass removed = areal removal rate &times; streambed area. Areal removal rate =
exchange flux &times; inlet concentration &times; removal efficiency.</p>
{% endif %}
{% endif %}
{% if s.range %}
<p><strong>Reported range: {{ s.range }}.</strong> {{ s.range_note }}</p>
{% endif %}
{% if s.key == "nutrient" and function_b64 %}<figure><img id="figfunc" class="fig" src="data:image/png;base64,{{ function_b64 }}" alt="Removal opportunity against assumed reaction timescale"><figcaption>Flux-weighted removal opportunity as a function of the assumed reaction timescale. This needs no rate constant.</figcaption></figure>{% endif %}
{% if s.decisions %}
<h4>Which number to use</h4>
<table>
 <tr><th>Decision</th><th>Use</th><th class="num">Value</th><th>Why</th></tr>
 {% for d in s.decisions %}
 <tr><td>{{ d.context }}</td><td>{{ d.metric }}</td><td class="num">{{ d.value }}</td><td class="muted">{{ d.reasoning }}</td></tr>
 {% endfor %}
</table>
{% endif %}
{% if s.validity_note %}<div class="card warn">{{ s.validity_note }}</div>{% endif %}
{% if s.unavailable_reason %}<p class="muted">{{ s.unavailable_reason }}</p>{% endif %}
<p class="muted"><b>Transferability.</b> {{ s.transferability_note }}</p>
{% if s.references %}<details class="sec"><summary>Sources</summary>
{% for r in s.references %}<p class="muted ref">{{ r }}</p>{% endfor %}</details>{% endif %}
{% endfor %}

<div class="card warn">
<strong>Assumptions behind every section above</strong>
<ul>
 <li>Rate constants are scenario inputs. Nothing here has been calibrated against field data.</li>
 <li>Carbon is assumed non-limiting for denitrification, which overstates removal where it is scarce.</li>
 <li>Reported ranges are sensitivity bounds, not confidence intervals.</li>
 <li>Only returning flow paths are counted. Water leaving the domain without returning is excluded.</li>
 <li>Temperature regulation reports buffering opportunity, never degrees of cooling.</li>
 <li>Habitat extent is connected subsurface space, never habitat quality or occupancy.</li>
</ul>
</div>
</section>
{% endif %}

{% if input_rows %}
<details class="sec"><summary>Model inputs and assumptions</summary>
<table><tr><th>Group</th><th>Input</th><th class="num">Value</th><th>Unit</th></tr>
{% for r in input_rows %}<tr><td>{{ r.section }}</td><td>{{ r.name }}</td><td class="num">{{ r.value }}</td><td>{{ r.unit }}</td></tr>{% endfor %}
</table>
</details>
{% endif %}

{# `data_sources` is computed and deliberately NOT rendered. Revision spec §19.1 item 10 asks for
   an input-provenance section, but a "Data sources and provenance" block was removed from this
   report at a user's explicit request (see tests/test_report.py, which asserts its absence).
   The user's request wins over the framework document; flagged rather than silently reinstated. #}
{% if alt_scenarios %}
<details class="sec"><summary>Hydraulic Alternatives</summary>
<p class="muted">Order of magnitude variations of hydraulic conductivity and head gradient, each run
 through the full groundwater and hyporheic analysis. Ranges show sensitivity to these two factors.
 They are not confidence intervals.</p>
<table><tr><th>Run</th><th>Factors</th><th>Status</th><th class="num">Frequency (turnovers/km)</th><th class="num">Duration (hr)</th><th class="num">Extent (m)</th></tr>
{% for s in alt_scenarios %}<tr><td>{{ s.label }}</td><td>{{ s.factors }}</td><td>{{ s.status }}</td><td class="num">{{ s.freq }}</td><td class="num">{{ s.dur }}</td><td class="num">{{ s.ext }}</td></tr>{% endfor %}
</table>
{% if alt_note %}<p class="muted">{{ alt_note }}</p>{% endif %}
</details>
{% endif %}

{% if references %}
<details class="sec"><summary>References</summary>
{% for r in references %}<p class="muted ref">{{ r.authors }}{% if r.year %} ({{ r.year }}){% endif %}. {{ r.title }}.{% if r.url %} {{ r.url }}{% endif %}</p>{% endfor %}
</details>
{% endif %}

<p class="foot">Generated {{ generated_at }} by HYPE {{ app_version }} using {{ model_version }}.
Report method {{ method_version }}.</p>

</div>
<div class="lightbox" id="figzoom" role="dialog" aria-label="Enlarged figure"><img alt=""/><div class="cap"></div></div>
<script>
(function(){
 var lb=document.getElementById("figzoom");
 if(!lb)return;
 var big=lb.querySelector("img"),cap=lb.querySelector(".cap");
 function shut(){lb.classList.remove("open");big.removeAttribute("src");}
 document.addEventListener("click",function(ev){
  var t=ev.target;
  if(lb.classList.contains("open")){shut();return;}
  if(!t||t.tagName!=="IMG"||!t.classList.contains("fig"))return;
  big.src=t.src;big.alt=t.alt||"";
  var fc=t.parentElement&&t.parentElement.querySelector("figcaption");
  cap.textContent=(fc&&fc.textContent)||t.alt||"";
  lb.classList.add("open");
 });
 document.addEventListener("keydown",function(ev){if(ev.key==="Escape")shut();});
})();
</script>
</body></html>"""


def _site_location(site) -> str | None:
    """Mid-reach lat/lon chip text from the snapshot's endpoint points (fallback outlet)."""
    try:
        pts = [p for p in (getattr(site, "upstream_point", None),
                           getattr(site, "downstream_point", None)) if p is not None]
        if not pts and getattr(site, "outlet", None) is not None:
            pts = [site.outlet]
        if not pts:
            return None
        lat = sum(float(p.lat) for p in pts) / len(pts)
        lon = sum(float(p.lon) for p in pts) / len(pts)
        return (f"{abs(lat):.5f}°{'N' if lat >= 0 else 'S'}, "
                f"{abs(lon):.5f}°{'E' if lon >= 0 else 'W'}")
    except Exception:  # noqa: BLE001 — the chip is optional
        return None


def render_html(results: AssessmentResultsV2, *, app_version=None, model_version=None,
                figures: dict | None = None, rtd_dist: dict | None = None,
                project_name: str | None = None, head_layer: int | None = None,
                include_functions: bool = True, include_hydraulics: bool = True) -> str:
    """Render the self-contained report HTML (inline CSS/JS only; figures are data URIs
    shown as height-capped previews with a click-to-enlarge lightbox, so the file works
    offline). `figures` is a dict of PNG bytes keyed rtd/planview/section/threshold plus
    the site-map suite; `rtd_dist` is accepted for compatibility but no longer embedded.
    `project_name` feeds the report title (site name wins when both are set);
    `head_layer` is the 1-based model layer shown in the head-contour figure caption."""
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

    title_name = (getattr(site, "site_name", None) or project_name or "HYPE")
    return template.render(
        results=results, site=site, cards=headline_cards(results), grouped=grouped,
        title_name=title_name, project_name=project_name,
        location=_site_location(site), head_layer=head_layer,
        thresholds=threshold_rows(results), fmt=fmt,
        # EITHER PART DROPS WHOLE, taking its header with it, which is what makes two documents
        # out of one template. Part B off gives the hydraulics report (§9.4: a complete hydraulic
        # signature must never depend on chemistry). Part A off gives the screening report, which
        # then opens with a three-card recap of what it rests on rather than asserting rates
        # against nothing.
        functions=(function_sections(results) if include_functions else []),
        hydraulics=include_hydraulics,
        input_rows=input_rows(results), data_sources=data_source_rows(results),
        alt_scenarios=alternative_scenario_rows(results),
        alt_ranges=scenario_range_map(results),
        alt_note=alternatives_note(results), references=report_references(results),
        rtd_png_b64=_b64("rtd"), threshold_b64=_b64("threshold"), function_b64=_b64("function"),
        planview_b64=_b64("planview"), section_b64=_b64("section"),
        map_topo_b64=_b64("map_topo"), map_imagery_b64=_b64("map_imagery"),
        map_wse_b64=_b64("map_wse"), map_head_b64=_b64("map_head"),
        map_3d_b64=_b64("map_3d"), map_paths_b64=_b64("map_paths"),
        rtd_blob=(json.dumps(rtd_dist) if rtd_dist else None),
        generated_at=(results.created_at.isoformat() if results.created_at else "n/a"),
        method_version=REPORT_METHOD_VERSION,
        app_version=app_version or "n/a", model_version=model_version or "n/a")


def render_pdf(results: AssessmentResultsV2, path, *, app_version=None,
               model_version=None, figures: dict | None = None,
               project_name: str | None = None, include_functions: bool = True,
               include_hydraulics: bool = True) -> str:
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

    snap = results.input_snapshot
    site = snap.site if snap else None
    title_name = ((getattr(site, "site_name", None) if site else None)
                  or project_name or "HYPE")
    story = [Paragraph(f"{title_name}: Site Summary Report", styles["Title"]),
             Paragraph("Hyporheic Exchange Assessment", small)]

    # Section 1: site identity (was PDF-missing)
    if site is not None:
        story.append(Spacer(1, 0.12 * inch))
        ident = [["Site", site.site_name or "n/a", "Analyst",
                  (site.analyst or "n/a") + (f" ({site.organization})" if site.organization else "")],
                 ["Date", (site.assessment_date.isoformat() if site.assessment_date else "n/a"),
                  "Reach length", f"{fmt(site.reach_length_m)} m"],
                 ["Project", project_name or "n/a",
                  "Location", _site_location(site) or "n/a"]]
        it = Table(ident, colWidths=[0.9 * inch, 2.55 * inch, 0.9 * inch, 2.15 * inch])
        it.setStyle(TableStyle([("FONTSIZE", (0, 0), (-1, -1), 8),
                                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f0f4f8")),
                                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f0f4f8"))]))
        story.append(it)
        if site.notes:
            story.append(Paragraph(f"<i>{site.notes}</i>", small))
    story.append(Spacer(1, 0.16 * inch))

    # ---- PART A: site maps and the hyporheic hydraulic signature -------------
    # Dropped WHOLE for the screening-only document, exactly as the HTML template drops it.
    # The two renderers are separate code paths and have drifted apart before, so the split
    # has to be made the same way in both or one format silently keeps a part the other lost.
    if include_hydraulics:
        # Site maps (report §10): 2x2 map grid + the full-width static 3-D view
        map_grid = (("map_topo", "map_imagery"), ("map_wse", "map_head"))
        if any(figures.get(k) for pair in map_grid for k in pair) or figures.get("map_3d"):
            story.append(Paragraph("Site Maps", styles["Heading2"]))
            for pair in map_grid:
                cells = []
                for k in pair:
                    b = figures.get(k)
                    if b:
                        img = Image(io.BytesIO(b))
                        img._restrictSize(3.35 * inch, 3.1 * inch)
                        cells.append(img)
                    else:
                        cells.append("")
                if any(c != "" for c in cells):
                    t = Table([cells], colWidths=[3.45 * inch, 3.45 * inch])
                    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                           ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                           ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
                    story.append(t)
            if figures.get("map_3d"):
                img = Image(io.BytesIO(figures["map_3d"]))
                img._restrictSize(6.9 * inch, 4.2 * inch)
                story.append(img)
            story.append(Spacer(1, 0.16 * inch))

        # ---- The hyporheic hydraulic signature (always renders) ------------------
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
        _rmap = scenario_range_map(results)
        if _rmap:
            story.append(_table(["Section", "Metric", "Value", "Unit", "Scenario range"],
                                [[r["section"], r["name"], r["value"], r["unit"],
                                  _rmap.get((r["section"], r["name"]), "")]
                                 for r in metric_rows(results)],
                                [1.2 * inch, 1.9 * inch, 1.2 * inch, 0.7 * inch, 1.3 * inch]))
        else:
            story.append(_table(["Section", "Metric", "Value", "Unit"],
                                [[r["section"], r["name"], r["value"], r["unit"]]
                                 for r in metric_rows(results)],
                                [1.4 * inch, 2.4 * inch, 1.6 * inch, 0.9 * inch]))

        irows = input_rows(results)
        if irows:
            story.append(Spacer(1, 0.16 * inch))
            story.append(Paragraph("Model inputs", styles["Heading2"]))
            story.append(_table(["Group", "Input", "Value", "Unit"],
                                [[r["section"], r["name"], r["value"], r["unit"]] for r in irows],
                                [1.2 * inch, 2.4 * inch, 1.8 * inch, 0.9 * inch]))

        # Figures: RTD full-width, then the four map-like figures paired 2-up (mirrors the
        # HTML's .maps rows and reuses the site-map grid Table pattern above).
        fig_pairs = [(("planview", "Plan-view hyporheic extent"),
                      ("map_paths", "Hyporheic flow paths (plan view)")),
                     (("section", "Returning flow paths (longitudinal section)"),
                      ("threshold", "Threshold exceedance"))]
        paired_keys = [k for pair in fig_pairs for k, _ in pair]
        if figures.get("rtd") or any(figures.get(k) for k in paired_keys):
            story.append(Spacer(1, 0.16 * inch))
            story.append(Paragraph("Figures", styles["Heading2"]))
            if figures.get("rtd"):
                story.append(Paragraph("Residence-time distribution", styles["Heading3"]))
                img = Image(io.BytesIO(figures["rtd"]))
                img._restrictSize(6.9 * inch, 3.4 * inch)
                story.append(img)
            for pair in fig_pairs:
                cells = []
                for key, title in pair:
                    b = figures.get(key)
                    if b:
                        img = Image(io.BytesIO(b))
                        img._restrictSize(3.35 * inch, 3.1 * inch)
                        cells.append([Paragraph(title, small), img])
                    else:
                        cells.append("")
                if any(c != "" for c in cells):
                    t = Table([cells], colWidths=[3.45 * inch, 3.45 * inch])
                    t.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                           ("LEFTPADDING", (0, 0), (-1, -1), 0),
                                           ("RIGHTPADDING", (0, 0), (-1, -1), 2)]))
                    story.append(t)

        trows = threshold_rows(results)
        if trows:
            story.append(Spacer(1, 0.16 * inch))
            story.append(Paragraph("Residence Time Exceedance", styles["Heading2"]))
            story.append(_table(
                ["Scenario", "Threshold", "Over threshold", "Functional flow", "Functional /km"],
                [[r["label"], f"{int(r['threshold_h'])} hr", f"{r['exceedance_pct']}%",
                  f"{r['functional_l_s']} L/s", r["functional_per_km"]] for r in trows],
                [1.7 * inch, 0.8 * inch, 1.2 * inch, 1.3 * inch, 1.2 * inch]))

    # Function screening. Mirrors the HTML block above; sections live in both renderers, so a
    # change to one without the other is the classic way this report drifts between formats.
    fsecs = function_sections(results) if include_functions else []
    if fsecs:
        story.append(Spacer(1, 0.22 * inch))
        story.append(Paragraph("Part B. Functional Screening Estimates", styles["Heading1"]))
        story.append(Paragraph(
            ("Everything above is direct model output. Everything below is inferred from it. "
             if include_hydraulics else "") +
            "Published reaction rates applied to the modeled flow paths, under assumptions you "
            "set. Good for comparing sites and alternatives. Not a prediction of what this reach "
            "will do." +
            ("" if include_hydraulics
             else " The hydraulics behind them are in the Hydraulics Report."), small))
        # NO CONCEPTUAL FIGURE HERE: it is its own report product now (`concept_pdf_bytes`).
        # Standalone screening document only: the three headline values these estimates are
        # computed from, so the document never asserts a rate with no visible basis (§9.3).
        if not include_hydraulics:
            cards = headline_cards(results)
            if cards:
                story.append(Spacer(1, 0.16 * inch))
                story.append(Paragraph("The Signature These Estimates Rest On",
                                       styles["Heading2"]))
                story.append(_table(
                    ["Dimension", "Metric", "Value", "Unit"],
                    [[c["dimension"], c["primary_name"], c["primary_value"], c["primary_unit"]]
                     for c in cards],
                    [1.5 * inch, 2.6 * inch, 1.2 * inch, 1.0 * inch]))
                story.append(Paragraph(
                    dims.SIGNATURE_SENTENCE + " Full derivation, figures and diagnostics are in "
                    "the hydraulics report.", small))
        for s in fsecs:
            story.append(Spacer(1, 0.12 * inch))
            # A mechanism renders one level down, beneath the function that hosts it, so the
            # dissolved endpoints and microplastic retention read as parts of Pollutant
            # Attenuation rather than as peer functions. The function's own name is printed once,
            # by whichever of its mechanisms comes first.
            if s.get("group_title"):
                story.append(Paragraph(s["group_title"], styles["Heading3"]))
            story.append(Paragraph(
                s.get("mechanism") or s["title"] if s.get("parent") else s["title"],
                styles["Heading4" if s.get("parent") else "Heading3"]))
            story.append(Paragraph(s["lede"], small))
            # The conditions and guards travel with the PDF too. A printed result that omits the
            # eligibility gate is the one most likely to be quoted out of context.
            if s.get("conditions"):
                story.append(Paragraph("<b>Applies only where all of these hold.</b>", small))
                for c in s["conditions"]:
                    story.append(Paragraph(f"• {c}", small))
            for n in s.get("guard_notes") or []:
                story.append(Paragraph(n, small))
            if s["rows"]:
                story.append(_table(["Metric", "Value"],
                                    [[r["name"], r["value"]] for r in s["rows"]],
                                    [4.2 * inch, 2.0 * inch]))
            if s["chain"]:
                story.append(Spacer(1, 0.08 * inch))
                story.append(_table([s.get("chain_title") or "Step", "Value"],
                                    [[r["name"], r["value"]] for r in s["chain"]],
                                    [4.2 * inch, 2.0 * inch]))
            if s.get("range"):
                story.append(Paragraph(
                    f"<b>Reported range: {s['range']}.</b> {s.get('range_note', '')}", small))
            if s.get("validity_note"):
                story.append(Paragraph(f"<b>Caution.</b> {s['validity_note']}", small))
            if s["decisions"]:
                story.append(Spacer(1, 0.08 * inch))
                story.append(_table(
                    ["Decision", "Use", "Value"],
                    [[d["context"], d["metric"], d["value"]] for d in s["decisions"]],
                    [2.6 * inch, 1.8 * inch, 1.8 * inch]))
            if s.get("unavailable_reason"):
                story.append(Paragraph(s["unavailable_reason"], small))
            story.append(Paragraph(f"<b>Transferability.</b> {s['transferability_note']}", small))
            if s.get("references"):
                # One reference per paragraph, as the HTML does. A single concatenated Source
                # line is exactly the wall this split exists to remove.
                story.append(Paragraph("<b>Sources.</b>", small))
                for ref in s["references"]:
                    story.append(Paragraph(html.escape(ref), small))
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph("<b>Assumptions behind every section above</b>", small))
        for line in [
                "Rate constants are scenario inputs. Nothing here has been calibrated against "
                "field data.",
                "Carbon is assumed non-limiting for denitrification, which overstates removal "
                "where it is scarce.",
                "Reported ranges are sensitivity bounds, not confidence intervals.",
                "Only returning flow paths are counted. Water leaving the domain without "
                "returning is excluded.",
                "Temperature regulation reports buffering opportunity, never degrees of cooling.",
                "Habitat extent is connected subsurface space, never habitat quality or "
                "occupancy."]:
            story.append(Paragraph("• " + line, small))

    arows = alternative_scenario_rows(results)
    if arows:
        story.append(Spacer(1, 0.16 * inch))
        story.append(Paragraph("Hydraulic Alternatives", styles["Heading2"]))
        story.append(Paragraph("Order of magnitude variations of hydraulic conductivity and "
                               "head gradient, each run through the full groundwater and "
                               "hyporheic analysis. Ranges show sensitivity to these two "
                               "factors. They are not confidence intervals.", small))
        story.append(_table(["Run", "Factors", "Status", "Freq (turnovers/km)", "Dur (hr)",
                             "Ext (m)"],
                            [[r["label"], r["factors"], r["status"], r["freq"], r["dur"],
                              r["ext"]] for r in arows],
                            [1.6 * inch, 1.4 * inch, 0.8 * inch, 1.3 * inch,
                             0.8 * inch, 0.8 * inch]))
        note = alternatives_note(results)
        if note:
            story.append(Paragraph(note, small))

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
                    app_version=None, model_version=None, spatial=None,
                    project_name: str | None = None, include_functions: bool = True) -> dict:
    """Write every format into out_dir; return {format: path}. Retryable without a model run.

    `spatial` (optional) supplies already-loaded map data for the plan-view + section figures:
    {"planview": {down_fc, up_fc, footprint_fc, reach_lonlat, domain_lonlat},
     "paths_gdf": <returning paths GeoDataFrame>, "reach_line": <shapely LineString, metric CRS>}
    plus optional site-map inputs (report §10): "crs_wkt" (model CRS), "wse_tif" and
    "head_tif" (GeoTIFF paths), "head_layer" (1-based layer of head_tif), "gwf_ws" (run
    workspace with *.dis.grb), "dem_path" for the static 3-D view, and "sides_lonlat"
    ({up|left|right|down: [[lon,lat],...]}) for the colored boundary-condition lines.
    Missing keys just drop the corresponding figures. `project_name` feeds the title."""
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
    if results.functions is not None and results.functions.nutrient is not None:
        figures["function"] = fig_mod.render_opportunity_curve(results.functions.nutrient)
    if spatial:
        figures["planview"] = fig_mod.render_planview_figure(**(spatial.get("planview") or {}))
        figures["section"] = fig_mod.render_section_figure(spatial.get("paths_gdf"),
                                                           spatial.get("reach_line"))
        try:
            figures.update(fig_mod.render_map_suite(spatial))
        except Exception:  # noqa: BLE001 — the site maps are best-effort
            pass
    figures = {k: v for k, v in figures.items() if v}
    for key, fname in (("rtd", "rtd_distribution.png"), ("threshold", "threshold_exceedance.png"),
                       ("planview", "planview.png"), ("section", "section.png"),
                       ("map_topo", "site_map_topo.png"), ("map_imagery", "site_map_imagery.png"),
                       ("map_wse", "site_map_wse.png"), ("map_head", "site_map_head.png"),
                       ("map_3d", "site_map_3d.png"), ("map_paths", "flowpaths_planview.png")):
        if figures.get(key):
            (out / fname).write_bytes(figures[key])

    # Returning-subset RTD summary; passed to render_html for compatibility only (the
    # HTML no longer embeds it since the custom-threshold widget was removed).
    rtd_dist = None
    if ret:
        rtd_dist = {
            "t_hours": [float(r["transit_time_days"]) * 24.0 for r in ret],
            "w": [float(r["flow_weight"]) for r in ret],
            "q_hef_l_s": (results.connectivity.returning_hyporheic_cms or 0.0) * 1000.0,
            "c_per_km": results.connectivity.turnovers_per_km or 0.0}

    paths = {
        "json": str(out / "assessment_results.json"),
        "csv_metrics": write_site_metrics_csv(results, out / "site_metrics.csv"),
        "csv_transit": write_transit_times_csv(rows, out / "hyporheic_transit_times.csv"),
        "run_summary": write_run_summary_json(results, out / "run_summary.json",
                                              app_version=app_version, model_version=model_version),
        "rtd_json": write_rtd_distribution_json(rows, out / "rtd_distribution.json"),
    }
    head_layer = (spatial or {}).get("head_layer")
    Path(paths["json"]).write_text(results_to_json(results), encoding="utf-8")

    # TWO DOCUMENTS FROM ONE FIGURE SET, and they do not overlap. The hydraulics report is the
    # signature alone; the screening report is the inferred layer alone. That separation is the
    # point: §9.4 requires a complete hydraulic signature that never depends on chemistry, and a
    # reader who does not accept the rate assumptions can be handed the first document without
    # the second. The figures are the expensive part and both draw from the same dict, so the
    # split costs a second template render rather than a second build.
    #
    # `html`/`pdf` still name the hydraulics document. It is the one that always has content, and
    # every existing caller and download handler already reads those keys.
    docs = [("html", "pdf", "site_report", True, False)]
    if include_functions:
        # Not a subset of the old combined report: standalone, it gains a three-card recap of the
        # signature it rests on, which Part B never needed while Part A sat directly above it.
        docs.append(("screening_html", "screening_pdf", "screening_report", False, True))
    for html_key, pdf_key, stem, want_hyd, want_fn in docs:
        paths[html_key] = str(out / f"{stem}.html")
        Path(paths[html_key]).write_text(
            render_html(results, app_version=app_version, model_version=model_version,
                        figures=figures, rtd_dist=rtd_dist, project_name=project_name,
                        head_layer=head_layer, include_functions=want_fn,
                        include_hydraulics=want_hyd),
            encoding="utf-8")
        try:
            paths[pdf_key] = render_pdf(results, out / f"{stem}.pdf",
                                        app_version=app_version, model_version=model_version,
                                        figures=figures, project_name=project_name,
                                        include_functions=want_fn, include_hydraulics=want_hyd)
        except Exception as e:  # noqa: BLE001 — PDF is best-effort; other formats still land
            paths[f"{pdf_key}_error"] = str(e)
    return paths


# ----------------------------------------------------------------------------------------------
# The Conceptual Model document (Site Reports -> Conceptual Model).
# ----------------------------------------------------------------------------------------------
#: The framing figure, hand-authored rather than drawn. See
#: `notes/functional_screening_conceptual_figure/README.md` for the build and the guards.
CONCEPT_ASSETS = Path(__file__).resolve().parent / "data" / "figure"
CONCEPT_SVG = CONCEPT_ASSETS / "conceptual_model.svg"
#: The same figure as raster, for the PDF. COMMITTED, not rendered on demand: nothing in this
#: stack can put an SVG into a PDF (`cairosvg` and `svglib` are absent and `renderPM` has no SVG
#: parser), so the only rasterizer available is a local browser, and a report build must not
#: depend on one being installed.
CONCEPT_PNG = CONCEPT_ASSETS / "conceptual_model.png"

# LOUD, AT IMPORT, on purpose. The desktop payload is built with `git archive HEAD`, so an asset
# that is present locally but never committed ships as nothing at all, and the failure would
# otherwise surface as a broken report in front of a user rather than at build time. This is the
# guard `validate_signature` used to run over the plate's illustrations.
_missing_concept = [p.name for p in (CONCEPT_SVG, CONCEPT_PNG) if not p.is_file()]
if _missing_concept:
    raise ImportError(
        f"missing conceptual figure artwork under {CONCEPT_ASSETS}: {_missing_concept}. "
        f"Rebuild with notes/functional_screening_conceptual_figure/make_concept_assets.py, "
        f"and make sure hype_app/data/figure is committed.")

CONCEPT_TITLE = "Conceptual Model"
#: THE ONLY PROSE IN THE DOCUMENT. There used to be a lede above the figure as well, saying what
#: the framework is; the figure's own title and subtitle say it, so it was read twice and cut.
CONCEPT_CAPTION = ("The hydraulic signature is measured. The screening estimates are inferred "
                   "from it under assumptions, and they are opportunities for functions and "
                   "outcomes.")

#: Deliberately NOT `_HTML_TEMPLATE`, which is built around an `AssessmentResultsV2` and would
#: drag a site header onto a document that says nothing about a site. What it does share is
#: `figure > img.fig` and the lightbox, because the lightbox handler filters on exactly that
#: tag and class: an inline `<svg>` would render identically and silently lose click-to-enlarge.
_CONCEPT_TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>{{ title }}</title>
<style>
 :root{--navy:#2f4b7c;--navy-d:#243a61;--ink:#1f2d3d;--muted:#5a6b7b;--rule:#e6e9ef}
 *{box-sizing:border-box}
 html,body{margin:0}
 body{font-family:"Space Grotesk","Segoe UI",system-ui,-apple-system,Arial,sans-serif;
  color:var(--ink);background:#fff;padding:1.1rem 1.3rem;font-size:13.5px;line-height:1.5}
 .wrap{max-width:70rem;margin:0 auto}
 .head{border-bottom:1px solid var(--rule);padding-bottom:.85rem}
 .eyebrow{font-size:10.5px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;
  color:var(--navy);margin-bottom:.2rem}
 h1{font-size:1.45rem;margin:0;color:var(--navy-d);letter-spacing:.2px}
 figure{margin:1rem 0 0}
 /* A DIAGRAM IS TEXT, NOT A PHOTOGRAPH, so it is never height-capped: full container width,
    with the lightbox for detail. */
 img.fig{display:block;width:100%;height:auto;border:1px solid var(--rule);border-radius:8px;
  margin:0 auto;background:#fff;cursor:zoom-in}
 figcaption{font-size:11.5px;color:var(--muted);margin-top:6px;line-height:1.35;text-align:center}
 .lightbox{position:fixed;inset:0;z-index:60;display:none;flex-direction:column;align-items:center;
  justify-content:center;gap:10px;background:rgba(16,26,42,.88);cursor:zoom-out;padding:22px}
 .lightbox.open{display:flex}
 .lightbox img{max-width:96vw;max-height:90vh;width:auto;height:auto;background:#fff;border-radius:8px}
 .lightbox .cap{color:#dce5f2;font-size:12.5px;max-width:92vw;text-align:center;line-height:1.4}
 @media print{body{padding:0} figure{page-break-inside:avoid} .lightbox{display:none !important}}
</style></head><body>
<div class="wrap">
<div class="head">
<div class="eyebrow">Hyporheic Exchange Assessment</div>
<h1>{{ title }}{% if project_name %}: {{ project_name }}{% endif %}</h1>
</div>
<figure><img class="fig" src="data:image/svg+xml;base64,{{ svg_b64 }}"
 alt="The hyporheic hydraulic signature and the four functional screening families"/>
<figcaption>{{ caption }}</figcaption></figure>
</div>
<div class="lightbox" id="figzoom" role="dialog" aria-label="Enlarged figure"><img alt=""/><div class="cap"></div></div>
<script>
(function(){
 var lb=document.getElementById("figzoom");
 if(!lb)return;
 var big=lb.querySelector("img"),cap=lb.querySelector(".cap");
 function shut(){lb.classList.remove("open");big.removeAttribute("src");}
 document.addEventListener("click",function(ev){
  var t=ev.target;
  if(lb.classList.contains("open")){shut();return;}
  if(!t||t.tagName!=="IMG"||!t.classList.contains("fig"))return;
  big.src=t.src;big.alt=t.alt||"";
  var fc=t.parentElement&&t.parentElement.querySelector("figcaption");
  cap.textContent=(fc&&fc.textContent)||t.alt||"";
  lb.classList.add("open");
 });
 document.addEventListener("keydown",function(ev){if(ev.key==="Escape")shut();});
})();
</script>
</body></html>"""


def concept_html(project_name: str | None = None) -> str:
    """The Conceptual Model document, self-contained.

    TAKES NO RESULTS, which is the whole point: it is openable before a run, and it is what the
    reader can be handed to explain what the other two documents are measuring."""
    import base64

    env = Environment(autoescape=select_autoescape(["html", "xml"]))
    return env.from_string(_CONCEPT_TEMPLATE).render(
        title=CONCEPT_TITLE, project_name=project_name, caption=CONCEPT_CAPTION,
        svg_b64=base64.b64encode(CONCEPT_SVG.read_bytes()).decode())


def concept_pdf_bytes(project_name: str | None = None) -> bytes:
    """The same document as a one-page PDF.

    THE SIZE NUMBERS ARE THE FRAME'S, NOT THE PAGE'S. `SimpleDocTemplate`'s frame is 456 x 636 pt
    (6.333 x 8.833 in), because the default `Frame` adds 6 pt of padding inside the 1 inch margins.
    An oversize flowable raises `LayoutError` rather than shrinking, so a generous-looking 6.5 x 9.0
    would produce no PDF at all. 8.24 in leaves the heading and the caption their lines, which is
    what keeps the caption on the same page as the figure it captions. Which of the two binds is
    the canvas's aspect: at 1210 x 1336 the figure is 6.33 x 6.99 in, so the width does."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer

    styles = getSampleStyleSheet()
    small = styles["BodyText"].clone("small", fontSize=8, leading=10)
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=letter, title=CONCEPT_TITLE,
                            leftMargin=inch, rightMargin=inch,
                            topMargin=inch, bottomMargin=inch)
    img = Image(str(CONCEPT_PNG))
    img._restrictSize(6.33 * inch, 8.24 * inch)
    doc.build([
        Paragraph(CONCEPT_TITLE + (f": {html.escape(project_name)}" if project_name else ""),
                  styles["Title"]),
        Spacer(1, 0.10 * inch),
        img,
        Paragraph(CONCEPT_CAPTION, small),
    ])
    return buf.getvalue()


__all__ = [
    "fmt", "fmt_sig", "fmt_range",
    "metric_rows", "headline_cards", "threshold_rows", "input_rows", "data_source_rows",
    "alternative_range_rows", "alternative_scenario_rows", "alternatives_note",
    "scenario_range_map", "report_references", "render_rtd_figure", "results_to_json",
    "write_site_metrics_csv", "write_transit_times_csv", "run_summary_dict",
    "write_run_summary_json", "write_rtd_distribution_json", "render_html", "render_pdf",
    "generate_report", "REPORT_METHOD_VERSION", "RUN_SUMMARY_SCHEMA_VERSION",
    "RTD_DISTRIBUTION_SCHEMA_VERSION",
    "concept_html", "concept_pdf_bytes", "CONCEPT_SVG", "CONCEPT_PNG", "CONCEPT_TITLE",
    "CONCEPT_CAPTION",
]
