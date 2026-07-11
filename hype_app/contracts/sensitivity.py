"""Gradient-sensitivity scenario contracts (revision spec §4.2, §10).

`ScenarioSpec` is one gradient scenario (its config, canonical hash, status/timing, and — for
alternatives — the complete numeric metrics + compact artifacts). `SensitivityScenarioManifest`
is the ordered set with the preferred scenario, generator type, and cancel/resume metadata.
Scenarios are deduplicated by canonical hash (§10.2) and run sequentially, preferred first (§10.3).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from ..provenance import HypeModel, HypeWarning
from .gradients import GradientBoundaryConfigV2

SENSITIVITY_MANIFEST_SCHEMA_VERSION = "sensitivity-scenario-manifest/1.0"
DEFAULT_MAX_SCENARIOS = 25            # configurable by deployment (§10.3)


class GeneratorType(str, Enum):
    linked = "linked"                 # default lower/preferred/upper at all controls
    one_at_a_time = "one_at_a_time"
    crossed = "crossed"               # left/right low·pref·high, 9 combinations
    custom = "custom"


class ScenarioStatus(str, Enum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class ScenarioSpec(HypeModel):
    """One gradient scenario in a sensitivity run."""

    id: str
    label: str
    is_preferred: bool = False
    gradients: GradientBoundaryConfigV2
    canonical_hash: str

    status: ScenarioStatus = ScenarioStatus.pending
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_s: float | None = None
    error: str | None = None
    warnings: list[HypeWarning] = Field(default_factory=list)

    # Alternatives retain complete metric/HFCI outputs + compact artifacts (§10.4);
    # the preferred scenario additionally retains full workspaces/figures on disk.
    metrics: dict = Field(default_factory=dict)
    artifact_paths: dict = Field(default_factory=dict)


class SensitivityScenarioManifest(HypeModel):
    """Ordered scenario set with preferred-first execution + cancel/resume metadata (§4.2)."""

    schema_version: str = SENSITIVITY_MANIFEST_SCHEMA_VERSION
    generator: GeneratorType
    preferred_id: str
    scenarios: list[ScenarioSpec] = Field(default_factory=list)
    max_scenarios: int = DEFAULT_MAX_SCENARIOS

    cancelled: bool = False
    resume_token: str | None = None
    warnings: list[HypeWarning] = Field(default_factory=list)

    def preferred(self) -> ScenarioSpec | None:
        return next((s for s in self.scenarios if s.id == self.preferred_id), None)

    def successful(self) -> list[ScenarioSpec]:
        return [s for s in self.scenarios if s.status == ScenarioStatus.completed]

    def failed(self) -> list[ScenarioSpec]:
        return [s for s in self.scenarios if s.status == ScenarioStatus.failed]


__all__ = [
    "GeneratorType", "ScenarioStatus", "ScenarioSpec", "SensitivityScenarioManifest",
    "SENSITIVITY_MANIFEST_SCHEMA_VERSION", "DEFAULT_MAX_SCENARIOS",
]
