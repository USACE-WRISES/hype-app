"""Assemble the canonical AssessmentResultsV2 from a completed analysis (revision spec §11.2).

The bridge between the run/HZ outputs and the report: it drives the metrics engine and freezes the
result model that every report format reads. Kept Shiny-independent and testable: the app passes
plain dicts from the HZ workspace; this computes the three hydraulic dimensions (exchange frequency,
exposure duration, active hyporheic capacity), the threshold functional-opportunity results, and the
quality-control diagnostics, then stamps provenance + group hashes for staleness.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from . import metrics as m
from . import validate as validate_mod
from .contracts import (
    AssessmentInputSnapshot,
    AssessmentResultsV2,
    ConnectivityMetrics,
    ResidenceTimeMetrics,
    ThresholdResult,
    ZoneMetrics,
)
from .provenance import HypeWarning, Severity

DEFAULT_THRESHOLD_HOURS = (1.0, 6.0, 12.0, 24.0)
_THRESHOLD_LABELS = {1.0: "Rapid-exposure scenario", 6.0: "Intermediate-exposure scenario",
                     12.0: "Longer-exposure scenario", 24.0: "Extended-exposure scenario"}
_THRESHOLD_NOTE = ("Hydraulic opportunity only: this is the exchanged flow that stays in the "
                   "subsurface at least this long. It does not establish that any reaction "
                   "occurred.")


def _finite_or_none(x):
    """Keep finite floats (and infinity, which is meaningful for turnover length); drop NaN."""
    if x is None:
        return None
    try:
        return None if math.isnan(x) else x
    except TypeError:
        return x


def build_results(
    snapshot: AssessmentInputSnapshot,
    *,
    hz_stats: dict,
    streamflow_cms: float | None,
    reach_length_m: float | None,
    exchange: m.ExchangeAccounting | None,
    transit_times_days=None,
    transit_weights=None,
    mobile_pore_storage_m3: float | None = None,
    reference_area_m2: float | None = None,
    footprint_weighted_m2: float | None = None,
    streambed_area_m2: float | None = None,
    active_streambed_area_m2: float | None = None,
    net_stream_exchange_cms: float | None = None,
    path_depths=None,
    domain_volume_m3: float | None = None,
    threshold_hours: tuple = DEFAULT_THRESHOLD_HOURS,
    custom_thresholds: list | None = None,
    porosity: float | None = None,
    censored_fraction: float | None = None,
    max_tracking_time_days: float | None = None,
    hz_accounting: dict | None = None,
    group_hashes: dict | None = None,
    app_version: str | None = None,
    model_version: str | None = None,
) -> AssessmentResultsV2:
    """Compute every metric and return the immutable results model.

    hz_stats: the hyporheic-class stats from hz_analysis.class_stats (volume_m3, footprint_m2,
    thickness_mean_m/…). exchange: the flux-weighted ExchangeAccounting (§8.3) or None (in m3/s).
    streambed_area_m2 (A_bed) normalizes q_HEF and D_HZ; active_streambed_area_m2 (A_active) gives
    the connected-bed fraction; path_depths (aligned with transit_weights) drive the depth stats.
    """
    warnings: list[HypeWarning] = []
    hyp = (hz_stats or {}).get("hyporheic", {})

    # ---- exchange frequency (report §5) --------------------------------------
    conn = ConnectivityMetrics()
    conn_obj = None
    if exchange is not None:
        conn.total_downwelling_cms = exchange.total_downwelling
        conn.returning_hyporheic_cms = exchange.returning_hyporheic
        conn.losing_cms = exchange.losing_to_sides
        conn.unresolved_cms = exchange.unresolved
        conn.mass_balance_error = exchange.mass_balance_error
        conn_obj = m.connectivity(
            streamflow=streamflow_cms, returning_hyporheic=exchange.returning_hyporheic,
            total_downwelling=exchange.total_downwelling, losing=exchange.losing_to_sides,
            unresolved=exchange.unresolved, reach_length_m=reach_length_m)
    conn.streamflow_cms = streamflow_cms
    conn.net_stream_exchange_cms = net_stream_exchange_cms
    if conn_obj is not None:
        conn.excursions_per_mile = conn_obj.excursions_per_mile
        conn.turnover_length_m = conn_obj.turnover_length_m
        conn.turnovers_per_km = _finite_or_none(conn_obj.turnovers_per_km)
        conn.turnover_length_km = conn_obj.turnover_length_km
        conn.gross_exchange_ratio_reach = _finite_or_none(conn_obj.gross_exchange_ratio_reach)
    else:
        conn.unavailable_reason = ("Streamflow, reach length, or flux-weighted classification "
                                   "unavailable; connectivity not computed.")
        warnings.append(HypeWarning(code="connectivity_unavailable",
                                    message=conn.unavailable_reason, severity=Severity.warning))
    if exchange is not None:
        flux = m.exchange_flux(exchange.returning_hyporheic, streambed_area_m2)
        conn.exchange_flux_m_day = _finite_or_none(flux["m_per_day"])
        conn.exchange_flux_mm_day = _finite_or_none(flux["mm_per_day"])
        if exchange.total_downwelling and exchange.total_downwelling > 0:
            conn.returning_flow_fraction = exchange.returning_hyporheic / exchange.total_downwelling
            conn.censored_flow_fraction = exchange.unresolved / exchange.total_downwelling
    conn.streambed_area_m2 = streambed_area_m2
    conn.active_streambed_area_m2 = active_streambed_area_m2
    if streambed_area_m2 and active_streambed_area_m2 is not None:
        conn.active_streambed_fraction = active_streambed_area_m2 / streambed_area_m2

    # ---- exposure duration (report §6) ---------------------------------------
    rtd = ResidenceTimeMetrics(porosity=porosity)
    have_rtd = (transit_times_days is not None and transit_weights is not None
                and len(transit_times_days))
    if have_rtd:
        stats = m.residence_time_metrics(
            transit_times_days, transit_weights, porosity=porosity,
            censored_fraction=censored_fraction, max_tracking_time_days=max_tracking_time_days)
        rtd = ResidenceTimeMetrics(**{k: v for k, v in stats.items()
                                      if k in ResidenceTimeMetrics.model_fields})
        if exchange is not None:
            rtd.returning_flux_represented_cms = exchange.returning_hyporheic

    # ---- active hyporheic capacity (report §7) -------------------------------
    zone = ZoneMetrics(
        bulk_saturated_volume_m3=hyp.get("volume_m3"),
        mobile_pore_storage_m3=mobile_pore_storage_m3,
        footprint_binary_m2=hyp.get("footprint_m2"),
        footprint_weighted_m2=footprint_weighted_m2,
        thickness_mean_m=hyp.get("thickness_mean_m"),
        thickness_max_m=hyp.get("thickness_max_m"))
    if hyp.get("volume_m3") is not None and streambed_area_m2:
        zone.equivalent_active_depth_m = float(hyp["volume_m3"]) / float(streambed_area_m2)
    if path_depths is not None and transit_weights is not None:
        dstats = m.path_depth_metrics(path_depths, transit_weights)
        zone.path_depth_p50_m = dstats.get("p50_m")
        zone.path_depth_p90_m = dstats.get("p90_m")
        zone.path_depth_max_m = dstats.get("max_m")

    # ---- threshold functional opportunity (report §10) -----------------------
    thresholds: list[ThresholdResult] = []
    q_hef_cms = exchange.returning_hyporheic if exchange is not None else None
    specs = [(float(h), _THRESHOLD_LABELS.get(float(h)), "default scenario")
             for h in (threshold_hours or ())]
    for c in (custom_thresholds or []):
        specs.append((float(c["value_h"]), c.get("label"), c.get("source") or "user scenario"))
    for t_h, label, source in specs:
        p = (m.exceedance_fraction(transit_times_days, transit_weights, t_h / 24.0)
             if have_rtd else None)
        p = _finite_or_none(p)
        q_func = (q_hef_cms * p) if (q_hef_cms is not None and p is not None) else None
        c_func = (conn.turnovers_per_km * p
                  if (conn.turnovers_per_km is not None and p is not None) else None)
        thresholds.append(ThresholdResult(
            threshold_value_h=t_h, threshold_label=label, threshold_source=source,
            flow_exceedance_fraction=p, functional_exchange_m3_s=q_func,
            functional_connectivity_per_km=c_func, interpretation_note=_THRESHOLD_NOTE))

    results = AssessmentResultsV2(
        assessment_id=snapshot.assessment_id, input_hash=snapshot.input_hash,
        input_snapshot=snapshot, group_hashes=group_hashes or snapshot.group_hashes(),
        connectivity=conn, residence_time=rtd, zone=zone, thresholds=thresholds,
        warnings=warnings,
        untested_uncertainty=["K and soil configuration", "Streamflow", "Geometry",
                              "Grid resolution", "Porosity",
                              "Thermal, chemical, and biological conditions"],
        created_at=datetime.now(timezone.utc), report_status=None)

    # ---- quality control (report §27) ----------------------------------------
    acct = dict(hz_accounting or {})
    if exchange is not None:
        acct.setdefault("mass_balance_error", exchange.mass_balance_error)
    qc_warnings, diag = validate_mod.validate_results(
        results, hz_accounting=acct, domain_volume_m3=domain_volume_m3)
    results.warnings.extend(qc_warnings)
    results.quality_diagnostics = diag
    return results


__all__ = ["build_results", "DEFAULT_THRESHOLD_HOURS"]
