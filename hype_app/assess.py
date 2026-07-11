"""Assemble the canonical AssessmentResultsV2 from a completed analysis (revision spec §11.2).

The bridge between the run/HZ outputs and the report: it drives the metrics + HFCI engines and
freezes the result model that every report format reads. Kept Shiny-independent and testable — the
app passes plain dicts from the HZ workspace; this computes connectivity, residence-time, zone, and
HFCI, and stamps provenance + group hashes for staleness.
"""
from __future__ import annotations

from datetime import datetime, timezone

from . import hfci as hfci_mod
from . import metrics as m
from .contracts import (
    AssessmentInputSnapshot,
    AssessmentResultsV2,
    ConnectivityMetrics,
    HFCIResult,
    ResidenceTimeMetrics,
    ZoneMetrics,
)
from .provenance import HypeWarning, Severity


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
    porosity: float | None = None,
    censored_fraction: float | None = None,
    max_tracking_time_days: float | None = None,
    group_hashes: dict | None = None,
    app_version: str | None = None,
    model_version: str | None = None,
) -> AssessmentResultsV2:
    """Compute every metric + HFCI and return the immutable results model.

    hz_stats: the hyporheic-class stats from hz_analysis.class_stats (volume_m3, footprint_m2,
    thickness_mean_m/…). exchange: the flux-weighted ExchangeAccounting (§8.3) or None.
    """
    warnings: list[HypeWarning] = []
    hyp = (hz_stats or {}).get("hyporheic", {})

    # ---- connectivity (§8.4) --------------------------------------------------
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
    if conn_obj is not None:
        conn.excursions_per_mile = conn_obj.excursions_per_mile
        conn.turnover_length_m = conn_obj.turnover_length_m
    else:
        conn.unavailable_reason = ("Streamflow, reach length, or flux-weighted classification "
                                   "unavailable — connectivity not computed.")
        warnings.append(HypeWarning(code="connectivity_unavailable",
                                    message=conn.unavailable_reason, severity=Severity.warning))

    # ---- residence-time distribution (§8.5) ----------------------------------
    rtd = ResidenceTimeMetrics(porosity=porosity)
    if transit_times_days is not None and transit_weights is not None and len(transit_times_days):
        stats = m.residence_time_metrics(
            transit_times_days, transit_weights, porosity=porosity,
            censored_fraction=censored_fraction, max_tracking_time_days=max_tracking_time_days)
        rtd = ResidenceTimeMetrics(**{k: v for k, v in stats.items()
                                      if k in ResidenceTimeMetrics.model_fields})

    # ---- zone (§8.2) ----------------------------------------------------------
    zone = ZoneMetrics(
        bulk_saturated_volume_m3=hyp.get("volume_m3"),
        mobile_pore_storage_m3=mobile_pore_storage_m3,
        footprint_binary_m2=hyp.get("footprint_m2"),
        footprint_weighted_m2=footprint_weighted_m2,
        thickness_mean_m=hyp.get("thickness_mean_m"),
        thickness_max_m=hyp.get("thickness_max_m"))

    # ---- HFCI (§9) ------------------------------------------------------------
    exchange_raw = conn.excursions_per_mile
    storage_raw = None
    if mobile_pore_storage_m3 is not None and reference_area_m2:
        storage_raw = mobile_pore_storage_m3 / reference_area_m2      # equivalent storage depth (m)
    processing_raw = None
    if transit_times_days is not None and transit_weights is not None and len(transit_times_days):
        processing_raw = hfci_mod.processing_driver(transit_times_days, transit_weights)
    hfci_res: HFCIResult = hfci_mod.compute_hfci(
        exchange_raw=exchange_raw, storage_raw=storage_raw, processing_raw=processing_raw)

    return AssessmentResultsV2(
        assessment_id=snapshot.assessment_id, input_hash=snapshot.input_hash,
        input_snapshot=snapshot, group_hashes=group_hashes or snapshot.group_hashes(),
        connectivity=conn, residence_time=rtd, zone=zone, hfci=hfci_res,
        warnings=warnings,
        untested_uncertainty=["K and soil configuration", "Streamflow", "Geometry",
                              "Grid resolution", "Porosity",
                              "Thermal, chemical, and biological conditions"],
        created_at=datetime.now(timezone.utc), report_status=None)


__all__ = ["build_results"]
