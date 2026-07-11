"""Canonical assessment-results contract (revision spec §4.2, §8, §9, §11).

`AssessmentResultsV2` is the immutable model generated right after a completed analysis. The report
modal and EVERY exported format (HTML/PDF/CSV/JSON) read only this model (§11.2), so numbers agree
across formats by construction. It references the frozen input snapshot and carries connectivity,
residence-time, zone, and HFCI metrics with full provenance, warnings, and artifact paths.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from ..provenance import HypeModel, HypeWarning
from .hfci import HFCI_VALIDATION_LABEL
from .inputs import AssessmentInputSnapshot
from .sensitivity import SensitivityScenarioManifest

RESULTS_SCHEMA_VERSION = "assessment-results/2.0"


class ConnectivityMetrics(HypeModel):
    """Flux accounting + excursions-per-mile (§8.4). None when unavailable (with a reason)."""

    streamflow_cms: float | None = None
    total_downwelling_cms: float | None = None
    returning_hyporheic_cms: float | None = None
    losing_cms: float | None = None
    unresolved_cms: float | None = None
    turnover_length_m: float | None = None
    excursions_per_mile: float | None = None
    mass_balance_error: float | None = None
    unavailable_reason: str | None = None


class ResidenceTimeMetrics(HypeModel):
    """Flux-weighted returning-particle residence-time distribution (§8.5)."""

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
    """Zone size (§8.2). Bulk vs mobile pore storage kept distinct + correctly labeled."""

    bulk_saturated_volume_m3: float | None = None
    mobile_pore_storage_m3: float | None = None
    footprint_binary_m2: float | None = None            # grid/particle-resolution dependent
    footprint_weighted_m2: float | None = None
    thickness_mean_m: float | None = None
    thickness_max_m: float | None = None


class ComponentScore(HypeModel):
    """One HFCI component (Exchange / Storage / Processing), §9.5."""

    raw_value: float | None = None
    raw_unit: str | None = None
    score: int | None = None                            # whole 0..15
    class_name: str | None = None                       # Low / Moderate / High
    color: str | None = None
    extrapolated: bool = False


class HFCIResult(HypeModel):
    """The composite index + its components (§9)."""

    exchange: ComponentScore = Field(default_factory=ComponentScore)
    storage: ComponentScore = Field(default_factory=ComponentScore)
    processing: ComponentScore = Field(default_factory=ComponentScore)
    hfci: float | None = None                           # 0.00..1.00, or None if not computable
    hfci_class: str | None = None
    hfci_color: str | None = None
    not_computable_reason: str | None = None
    profile_id: str | None = None
    profile_version: str | None = None
    validation_label: str = HFCI_VALIDATION_LABEL


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
    hfci: HFCIResult = Field(default_factory=HFCIResult)

    sensitivity: SensitivityScenarioManifest | None = None

    warnings: list[HypeWarning] = Field(default_factory=list)
    untested_uncertainty: list[str] = Field(default_factory=list)   # §10.6
    quality_diagnostics: dict = Field(default_factory=dict)
    artifact_paths: dict = Field(default_factory=dict)              # figures + tables
    report_status: str | None = None                                # "generated" | "failed" | None
    created_at: datetime | None = None


__all__ = [
    "ConnectivityMetrics", "ResidenceTimeMetrics", "ZoneMetrics", "ComponentScore",
    "HFCIResult", "AssessmentResultsV2", "RESULTS_SCHEMA_VERSION",
]
