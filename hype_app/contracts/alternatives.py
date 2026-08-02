"""Hydraulic Alternatives contracts: the order-of-magnitude K / gradient sweep.

`AltScenario` is one alternative run (its factors, status, full metric sections, QA, and a
compact solver log). `HydraulicAlternativesManifest` is the ordered set plus the identity of
the Basecase it was computed against (input hash, assessment id, method versions) so a rerun
or edit of the primary model invalidates the batch. The Basecase itself is referenced by hash,
never copied into the manifest or the alternatives folder.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from ..provenance import HypeModel, HypeWarning

ALTERNATIVES_MANIFEST_SCHEMA_VERSION = "hydraulic-alternatives/1.0"


class AltStatus(str, Enum):
    pending = "pending"          # queued, not started
    running = "running"
    completed = "completed"
    failed = "failed"            # solver or verification failure; dir removed
    cancelled = "cancelled"      # killed mid-run by Stop; dir removed
    not_run = "not_run"          # never reached (after a stop or a halted sweep)


#: User-facing status words (the Basecase row displays "Current" instead).
ALT_STATUS_LABEL: dict[AltStatus, str] = {
    AltStatus.pending: "Queued",
    AltStatus.running: "Running",
    AltStatus.completed: "Complete",
    AltStatus.failed: "Failed",
    AltStatus.cancelled: "Canceled",
    AltStatus.not_run: "Not run",
}


class AltScenario(HypeModel):
    """One alternative run in the sweep."""

    id: str                      # folder slug, e.g. "k10_gradient01"
    label: str                   # e.g. "Higher K + lower gradient"
    k_factor: float              # 1.0 | 10.0 | 0.1 multiplier on every K source
    g_factor: float              # 1.0 | 10.0 | 0.1 multiplier on every boundary gradient

    status: AltStatus = AltStatus.pending
    error: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None

    # The three signature primaries (turnovers_per_km, rtd_median_days,
    # equivalent_active_depth_m) for the runs table.
    metrics: dict = Field(default_factory=dict)
    # model_dump of the ConnectivityMetrics / ResidenceTimeMetrics / ZoneMetrics contract
    # models, keyed "connectivity" / "residence_time" / "zone". Ranges are recomputed from
    # these at render time through the CURRENT report.metric_rows, so metric label renames
    # never orphan saved ranges.
    results_sections: dict = Field(default_factory=dict)
    quality: dict = Field(default_factory=dict)          # quality_diagnostics of the run
    warnings: list[HypeWarning] = Field(default_factory=list)
    log_tail: list[str] = Field(default_factory=list)    # compact solver log (capped)
    rel_dir: str = ""                                    # "alternatives/<id>", never absolute


class HydraulicAlternativesManifest(HypeModel):
    """The sweep: scenario set + the Basecase identity it is only valid against."""

    schema_version: str = ALTERNATIVES_MANIFEST_SCHEMA_VERSION
    #: The user's variant selection: {k_lower/k_upper/g_lower/g_higher: multiplier|None (off),
    #: "combos": bool}. Multipliers are user-editable with validated bounds
    #: (alternatives.validate_selection).
    selection: dict = Field(default_factory=dict)

    base_input_hash: str | None = None       # input_snapshot()["input_hash"] of the Basecase
    base_assessment_id: str | None = None
    app_version: str | None = None
    method_versions: dict = Field(default_factory=dict)  # results/gradient/report versions
    hz_knobs: dict = Field(default_factory=dict)         # frozen delineation knobs used

    scenarios: list[AltScenario] = Field(default_factory=list)
    cancelled: bool = False
    created_at: datetime | None = None
    warnings: list[HypeWarning] = Field(default_factory=list)

    def completed(self) -> list[AltScenario]:
        return [s for s in self.scenarios if s.status == AltStatus.completed]

    def failed(self) -> list[AltScenario]:
        return [s for s in self.scenarios if s.status == AltStatus.failed]

    def is_partial(self) -> bool:
        """True when any planned scenario did not complete (ranges must be labeled partial)."""
        return any(s.status != AltStatus.completed for s in self.scenarios)


__all__ = [
    "AltStatus", "ALT_STATUS_LABEL", "AltScenario", "HydraulicAlternativesManifest",
    "ALTERNATIVES_MANIFEST_SCHEMA_VERSION",
]
