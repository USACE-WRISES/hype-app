"""Canonical assessment-results contract (revision spec §4.2, §8, §11; hydraulic report §5-7, §10).

`AssessmentResultsV2` is the immutable model generated right after a completed analysis. The report
modal and EVERY exported format (HTML/PDF/CSV/JSON) read only this model (§11.2), so numbers agree
across formats by construction. It references the frozen input snapshot and carries connectivity,
residence-time, zone, and threshold functional-opportunity metrics with full provenance, warnings,
and artifact paths. The report leads with the three hydraulic dimensions (exchange frequency,
exposure duration, active hyporheic capacity); there is no combined index.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ..provenance import HypeModel, HypeWarning
from .inputs import AssessmentInputSnapshot
from .sensitivity import SensitivityScenarioManifest

RESULTS_SCHEMA_VERSION = "assessment-results/2.1"


class ConnectivityMetrics(HypeModel):
    """Flux accounting + hyporheic connectivity (report §5). The headline is streamflow-equivalent
    turnovers per km; excursions-per-mile is retained as a supporting value. None when the inputs
    make connectivity undefined (with a reason)."""

    streamflow_cms: float | None = None
    total_downwelling_cms: float | None = None
    returning_hyporheic_cms: float | None = None            # Q_HEF
    losing_cms: float | None = None
    unresolved_cms: float | None = None
    net_stream_exchange_cms: float | None = None            # gross upwelling - gross downwelling
    # exchange frequency (report §5.1-5.5)
    turnovers_per_km: float | None = None                   # C_1km, headline
    turnover_length_km: float | None = None                 # L_T
    gross_exchange_ratio_reach: float | None = None         # E_reach = Q_HEF / Q_stream
    exchange_flux_m_day: float | None = None                # q_HEF = Q_HEF / A_bed
    exchange_flux_mm_day: float | None = None
    streambed_area_m2: float | None = None                  # A_bed (modeled stream-cell area)
    active_streambed_area_m2: float | None = None           # A_active (returning downwelling cells)
    active_streambed_fraction: float | None = None          # F_active,bed
    # supporting / legacy
    turnover_length_m: float | None = None
    excursions_per_mile: float | None = None                # supporting (backward compat)
    mass_balance_error: float | None = None
    returning_flow_fraction: float | None = None            # returning / total downwelling
    censored_flow_fraction: float | None = None             # unresolved / total downwelling
    unavailable_reason: str | None = None


class ResidenceTimeMetrics(HypeModel):
    """Flux-weighted returning-particle residence-time distribution (report §6)."""

    weighted_mean_days: float | None = None
    weighted_median_days: float | None = None
    p05_days: float | None = None
    p10_days: float | None = None
    p25_days: float | None = None
    p75_days: float | None = None
    p90_days: float | None = None
    p95_days: float | None = None
    min_days: float | None = None
    max_days: float | None = None
    frac_above_1h: float | None = None
    frac_1h_to_1d: float | None = None
    frac_above_1d: float | None = None
    returning_flux_represented_cms: float | None = None
    censored_fraction: float | None = None
    effective_particle_count: float | None = None
    max_tracking_time_days: float | None = None
    porosity: float | None = None


class ZoneMetrics(HypeModel):
    """Active hyporheic capacity + spatial extent (report §7). Bulk sediment volume is the headline
    basis; mobile pore-water storage is kept distinct and correctly labeled."""

    bulk_saturated_volume_m3: float | None = None           # V_HZ headline (bulk sediment basis)
    mobile_pore_storage_m3: float | None = None             # supporting pore-water volume
    equivalent_active_depth_m: float | None = None          # D_HZ = V_HZ / A_bed
    active_volume_basis: str | None = "bulk sediment"
    footprint_binary_m2: float | None = None                # grid/particle-resolution dependent
    footprint_weighted_m2: float | None = None
    thickness_mean_m: float | None = None
    thickness_max_m: float | None = None
    path_depth_p50_m: float | None = None                   # flow-weighted median max path depth
    path_depth_p90_m: float | None = None                   # flow-weighted P90 (preferred summary)
    path_depth_max_m: float | None = None                   # supplemental single-path maximum


class ThresholdResult(HypeModel):
    """One residence-time scenario's functional-opportunity result (report §10, §24). Hydraulic
    opportunity only, never a direct ecological outcome."""

    threshold_value_h: float
    threshold_label: str | None = None
    threshold_source: str | None = None
    flow_exceedance_fraction: float | None = None           # P(T >= t*)
    functional_exchange_m3_s: float | None = None           # Q_HEF * P
    functional_connectivity_per_km: float | None = None     # C_1km * P
    interpretation_note: str | None = None


class AssessmentResultsV2(HypeModel):
    """The single source of truth read by the report modal and all exports (§11.2)."""

    schema_version: str = RESULTS_SCHEMA_VERSION
    assessment_id: str
    input_hash: str
    input_snapshot: AssessmentInputSnapshot | None = None
    group_hashes: dict = Field(default_factory=dict)    # frozen §4.3 hashes for staleness

    connectivity: ConnectivityMetrics = Field(default_factory=ConnectivityMetrics)
    residence_time: ResidenceTimeMetrics = Field(default_factory=ResidenceTimeMetrics)
    zone: ZoneMetrics = Field(default_factory=ZoneMetrics)
    thresholds: list[ThresholdResult] = Field(default_factory=list)

    sensitivity: SensitivityScenarioManifest | None = None

    warnings: list[HypeWarning] = Field(default_factory=list)
    untested_uncertainty: list[str] = Field(default_factory=list)   # §10.6
    quality_diagnostics: dict = Field(default_factory=dict)
    artifact_paths: dict = Field(default_factory=dict)              # figures + tables
    report_status: str | None = None                                # "generated" | "failed" | None
    created_at: datetime | None = None


__all__ = [
    "ConnectivityMetrics", "ResidenceTimeMetrics", "ZoneMetrics", "ThresholdResult",
    "AssessmentResultsV2", "RESULTS_SCHEMA_VERSION",
]
