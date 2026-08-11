"""Versioned persistence contracts for cross-project hydraulic comparisons.

The comparison file is deliberately a small, self-contained snapshot.  Source projects are
references only: opening a comparison never adopts a project workspace and never needs the large
model artifacts stored beside or inside a ``.hype`` archive.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, field_validator, model_validator

from ..provenance import HypeModel, Severity
from .alternatives import AltStatus


COMPARISON_COLLECTION_SCHEMA_VERSION = "comparison-collection/1.0"
COMPARISON_MEMBER_SCHEMA_VERSION = "comparison-member/1.0"
COMPARISON_SNAPSHOT_SCHEMA_VERSION = "comparison-snapshot/1.0"
COMPARISON_OBSERVATION_SCHEMA_VERSION = "comparison-metric-observation/1.0"
COMPARISON_SCENARIO_SCHEMA_VERSION = "comparison-scenario/1.0"
COMPARISON_FINDING_SCHEMA_VERSION = "comparison-finding/1.0"
COMPARISON_VIEW_SCHEMA_VERSION = "comparison-view-settings/1.0"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _finite_or_none(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not math.isfinite(float(value)):
        raise ValueError("comparison metric values must be finite numbers or null")
    return float(value)


class ComparisonSourceStatus(str, Enum):
    """Relationship between a frozen member and its external source."""

    ready = "ready"
    changed = "changed"
    missing = "missing"
    moved = "moved"
    invalid = "invalid"


class ComparisonFindingV1(HypeModel):
    schema_version: Literal[COMPARISON_FINDING_SCHEMA_VERSION] = COMPARISON_FINDING_SCHEMA_VERSION
    code: str
    message: str
    severity: Severity = Severity.warning
    context: dict[str, Any] = Field(default_factory=dict)


class ComparisonScenarioV1(HypeModel):
    """One captured alternative and its presentation-unit hydraulic values."""

    schema_version: Literal[COMPARISON_SCENARIO_SCHEMA_VERSION] = COMPARISON_SCENARIO_SCHEMA_VERSION
    scenario_id: str
    label: str
    status: AltStatus
    k_factor: float | None = None
    gradient_factor: float | None = None
    metrics: dict[str, float] = Field(default_factory=dict)
    error: str | None = None

    @field_validator("k_factor", "gradient_factor", mode="before")
    @classmethod
    def _factors_are_finite(cls, value):
        return _finite_or_none(value)

    @field_validator("metrics", mode="before")
    @classmethod
    def _metrics_are_finite(cls, values):
        return {str(key): _finite_or_none(value) for key, value in values.items()}


class ComparisonMetricObservationV1(HypeModel):
    """Baseline and min/max sensitivity range for one stable hydraulic metric ID."""

    schema_version: Literal[COMPARISON_OBSERVATION_SCHEMA_VERSION] = \
        COMPARISON_OBSERVATION_SCHEMA_VERSION
    metric_id: str
    unit: str
    baseline: float | None = None
    low: float | None = None
    high: float | None = None
    aggregation_method: Literal["min_max_completed_alternatives"] = \
        "min_max_completed_alternatives"
    finite_case_count: int = Field(default=0, ge=0, strict=True)
    completed_scenario_count: int = Field(default=0, ge=0, strict=True)
    configured_scenario_count: int = Field(default=0, ge=0, strict=True)
    completeness: Literal["unavailable", "baseline_only", "complete", "partial"] = "unavailable"
    has_range: bool = Field(default=False, strict=True)
    low_scenarios: list[str] = Field(default_factory=list)
    high_scenarios: list[str] = Field(default_factory=list)
    incomplete_scenarios: list[str] = Field(default_factory=list)

    @field_validator("baseline", "low", "high", mode="before")
    @classmethod
    def _values_are_finite(cls, value):
        return _finite_or_none(value)

    @model_validator(mode="after")
    def _range_is_coherent(self):
        if self.completed_scenario_count > self.configured_scenario_count:
            raise ValueError("completed scenario count cannot exceed configured count")
        if (self.low is None) != (self.high is None):
            raise ValueError("low and high must both be present or both be null")
        if self.low is None and self.finite_case_count:
            raise ValueError("finite cases require low and high")
        if self.low is not None and self.finite_case_count < 1:
            raise ValueError("low and high require at least one finite case")
        if self.low is not None and self.low > self.high:
            raise ValueError("low cannot exceed high")
        if (self.baseline is not None and self.low is not None
                and not self.low <= self.baseline <= self.high):
            raise ValueError("the sensitivity range must include the baseline")
        if self.has_range != (self.baseline is not None and self.finite_case_count >= 2):
            raise ValueError("has_range requires a baseline and two or more finite cases")
        if self.completeness == "unavailable" and self.finite_case_count:
            raise ValueError("an unavailable observation cannot contain finite cases")
        if self.completeness == "baseline_only" and self.configured_scenario_count:
            raise ValueError("baseline_only requires no configured alternatives")
        if self.completeness == "complete" and (
                not self.configured_scenario_count
                or self.completed_scenario_count != self.configured_scenario_count
                or self.incomplete_scenarios):
            raise ValueError("complete requires every configured alternative to be available")
        if self.completeness == "partial" and not self.configured_scenario_count:
            raise ValueError("partial requires configured alternatives")
        return self


class ComparisonSnapshotV1(HypeModel):
    """A frozen, plot-ready hydraulic summary captured from one project."""

    schema_version: Literal[COMPARISON_SNAPSHOT_SCHEMA_VERSION] = COMPARISON_SNAPSHOT_SCHEMA_VERSION
    captured_at: datetime = Field(default_factory=_now)
    source_revision: str
    results_schema_version: str
    alternatives_schema_version: str | None = None
    assessment_id: str
    input_hash: str
    project_id: str | None = None
    site_id: str | None = None
    site_name: str | None = None
    project_name: str | None = None
    run_date: datetime | None = None
    valid: bool = True
    readiness: Literal["ready", "warning", "invalid"] = "ready"
    baseline_metrics: dict[str, float] = Field(default_factory=dict)
    scenarios: list[ComparisonScenarioV1] = Field(default_factory=list)
    observations: dict[str, ComparisonMetricObservationV1] = Field(default_factory=dict)
    findings: list[ComparisonFindingV1] = Field(default_factory=list)
    quality_diagnostics: dict[str, Any] = Field(default_factory=dict)
    provenance: dict[str, Any] = Field(default_factory=dict)
    compatibility: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source_revision")
    @classmethod
    def _revision_is_sha256(cls, value):
        value = str(value).lower()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("source_revision must be a SHA-256 hexadecimal digest")
        return value

    @field_validator("baseline_metrics", mode="before")
    @classmethod
    def _baseline_is_finite(cls, values):
        return {str(key): _finite_or_none(value) for key, value in values.items()}

    @model_validator(mode="after")
    def _snapshot_is_coherent(self):
        for key, observation in self.observations.items():
            if key != observation.metric_id:
                raise ValueError("observation map keys must equal observation.metric_id")
            if key in self.baseline_metrics and observation.baseline != self.baseline_metrics[key]:
                raise ValueError("observation baseline differs from baseline_metrics")
        if self.valid != (self.readiness != "invalid"):
            raise ValueError("valid and readiness disagree")
        return self


class ComparisonMemberV1(HypeModel):
    schema_version: Literal[COMPARISON_MEMBER_SCHEMA_VERSION] = COMPARISON_MEMBER_SCHEMA_VERSION
    member_id: UUID = Field(default_factory=uuid4)
    project_id: str | None = None
    site_id: str | None = None
    source_relative: str | None = None
    source_absolute: str
    label: str
    alias: str | None = None
    included: bool = Field(default=True, strict=True)
    source_revision: str | None = None
    source_status: ComparisonSourceStatus = ComparisonSourceStatus.ready
    source_findings: list[ComparisonFindingV1] = Field(default_factory=list)
    snapshot: ComparisonSnapshotV1 | None = None

    @field_validator("source_revision")
    @classmethod
    def _member_revision_is_sha256(cls, value):
        if value is None:
            return None
        value = str(value).lower()
        if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("source_revision must be a SHA-256 hexadecimal digest")
        return value

    @model_validator(mode="after")
    def _member_matches_snapshot(self):
        if self.snapshot is not None:
            if self.source_revision != self.snapshot.source_revision:
                raise ValueError("member and snapshot source revisions must match")
            if self.project_id != self.snapshot.project_id or self.site_id != self.snapshot.site_id:
                raise ValueError("member and snapshot project/site identities must match")
        return self


class ComparisonViewSettingsV1(HypeModel):
    schema_version: Literal[COMPARISON_VIEW_SCHEMA_VERSION] = COMPARISON_VIEW_SCHEMA_VERSION
    view: Literal["overview", "metric", "relationships"] = "overview"
    #: Metric-tab panels, in display order. Multiple aligned panels are the point of the
    #: tab; the single default keeps a fresh workspace focused.
    metric_ids: list[str] = Field(
        default_factory=lambda: ["connectivity.turnovers_per_km"])
    order: Literal["added", "ascending", "descending"] = "added"
    scale: Literal["auto", "linear", "log"] = "auto"

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_metric_id(cls, data):
        # Early prototype collections stored a single `metric_id`; fold it into the list so
        # those files still open. Schema version is unchanged: 1.0 never shipped.
        if isinstance(data, dict) and "metric_id" in data:
            data = dict(data)
            legacy = str(data.pop("metric_id") or "").strip()
            if not data.get("metric_ids") and legacy:
                data["metric_ids"] = [legacy]
        return data

    @field_validator("metric_ids", mode="before")
    @classmethod
    def _panel_ids_are_unique(cls, values):
        out: list[str] = []
        for value in (values or []):
            text = str(value).strip()
            if text and text not in out:
                out.append(text)
        return out


class ComparisonCollectionV1(HypeModel):
    schema_version: Literal[COMPARISON_COLLECTION_SCHEMA_VERSION] = \
        COMPARISON_COLLECTION_SCHEMA_VERSION
    collection_id: UUID = Field(default_factory=uuid4)
    name: str = "Untitled hydraulic comparison"
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    view_settings: ComparisonViewSettingsV1 = Field(default_factory=ComparisonViewSettingsV1)
    members: list[ComparisonMemberV1] = Field(default_factory=list)

    @model_validator(mode="after")
    def _member_ids_are_unique(self):
        ids = [member.member_id for member in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("comparison member IDs must be unique")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


__all__ = [
    "COMPARISON_COLLECTION_SCHEMA_VERSION", "COMPARISON_MEMBER_SCHEMA_VERSION",
    "COMPARISON_SNAPSHOT_SCHEMA_VERSION", "COMPARISON_OBSERVATION_SCHEMA_VERSION",
    "COMPARISON_SCENARIO_SCHEMA_VERSION", "COMPARISON_FINDING_SCHEMA_VERSION",
    "COMPARISON_VIEW_SCHEMA_VERSION", "ComparisonSourceStatus", "ComparisonFindingV1",
    "ComparisonScenarioV1", "ComparisonMetricObservationV1", "ComparisonSnapshotV1",
    "ComparisonMemberV1", "ComparisonViewSettingsV1", "ComparisonCollectionV1",
]
