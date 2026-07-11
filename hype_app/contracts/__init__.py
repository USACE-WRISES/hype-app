"""Versioned public contracts for the HYPE revision (spec §4.2).

Import site for every Pydantic model that crosses a persistence or process boundary: the frozen
input snapshot, USGS flow lookup, NRCS soils + derived conductivity, structured gradients,
sensitivity manifest, canonical results, and the HFCI scoring profile. All are Shiny-independent
and JSON-round-trippable.

`migrate(kind, data)` is the single explicit migration entry point — later phases register
version-upgrade functions here rather than reinterpreting old payloads implicitly (§7.7, §4.4).
"""
from __future__ import annotations

from typing import Any, Callable

from .flow import (
    FLOW_SNAPSHOT_SCHEMA_VERSION,
    FlowCandidate,
    FlowLookupSnapshot,
    LatLon,
)
from .gradients import (
    GRADIENT_METHOD_VERSION,
    GRADIENT_SCHEMA_VERSION,
    QUALITATIVE_MULTIPLIER,
    GradientBoundaryConfigV2,
    GradientControl,
    GradientQualitative,
    LegacyGradientMeta,
    ReferenceSlope,
    Side,
)
from .hfci import (
    DEFAULT_CLASSES,
    HFCI_PROFILE_SCHEMA_VERSION,
    HFCI_VALIDATION_LABEL,
    CapacityClass,
    HFCIScoringProfileV1,
    ScoreCurve,
)
from .inputs import (
    INPUT_SNAPSHOT_SCHEMA_VERSION,
    AssessmentInputSnapshot,
    GridSettings,
    KSettings,
    SiteMetadata,
    StreamflowInput,
    TerrainSource,
)
from .results import (
    RESULTS_SCHEMA_VERSION,
    AssessmentResultsV2,
    ComponentScore,
    ConnectivityMetrics,
    HFCIResult,
    ResidenceTimeMetrics,
    ZoneMetrics,
)
from .sensitivity import (
    DEFAULT_MAX_SCENARIOS,
    SENSITIVITY_MANIFEST_SCHEMA_VERSION,
    GeneratorType,
    ScenarioSpec,
    ScenarioStatus,
    SensitivityScenarioManifest,
)
from .soils import (
    SOIL_SNAPSHOT_SCHEMA_VERSION,
    AggregationPolicy,
    Component,
    CoverageReport,
    DerivedConductivityProfile,
    GridConductivityAssignment,
    Horizon,
    KOrigin,
    MapUnit,
    Restriction,
    SoilDataSnapshot,
    SoilOverride,
    SoilPolygon,
)

# Current schema version per contract kind. `kind` is the leading path of the version string.
SCHEMA_VERSIONS: dict[str, str] = {
    "assessment-input-snapshot": INPUT_SNAPSHOT_SCHEMA_VERSION,
    "flow-lookup-snapshot": FLOW_SNAPSHOT_SCHEMA_VERSION,
    "soil-data-snapshot": SOIL_SNAPSHOT_SCHEMA_VERSION,
    "gradient-boundary-config": GRADIENT_SCHEMA_VERSION,
    "sensitivity-scenario-manifest": SENSITIVITY_MANIFEST_SCHEMA_VERSION,
    "assessment-results": RESULTS_SCHEMA_VERSION,
    "hfci-scoring-profile": HFCI_PROFILE_SCHEMA_VERSION,
}

# kind -> ordered list of (from_version, upgrade_fn). Registered by later phases as schemas evolve.
_MIGRATIONS: dict[str, list[tuple[str, Callable[[dict], dict]]]] = {}


def register_migration(kind: str, from_version: str,
                       fn: Callable[[dict], dict]) -> None:
    _MIGRATIONS.setdefault(kind, []).append((from_version, fn))


def migrate(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Apply any registered upgrade functions for `kind` until data is at the current version.

    A no-op today (v1 of every contract); the seam exists so version bumps are explicit.
    """
    out = dict(data)
    for from_version, fn in _MIGRATIONS.get(kind, []):
        if str(out.get("schema_version", "")).endswith(from_version):
            out = fn(out)
    return out


__all__ = [
    # flow
    "LatLon", "FlowCandidate", "FlowLookupSnapshot", "FLOW_SNAPSHOT_SCHEMA_VERSION",
    # gradients
    "Side", "GradientQualitative", "QUALITATIVE_MULTIPLIER", "GradientControl",
    "ReferenceSlope", "LegacyGradientMeta", "GradientBoundaryConfigV2",
    "GRADIENT_SCHEMA_VERSION", "GRADIENT_METHOD_VERSION",
    # soils
    "AggregationPolicy", "KOrigin", "Horizon", "Restriction", "Component", "MapUnit",
    "SoilPolygon", "SoilOverride", "DerivedConductivityProfile", "GridConductivityAssignment",
    "CoverageReport", "SoilDataSnapshot", "SOIL_SNAPSHOT_SCHEMA_VERSION",
    # inputs
    "SiteMetadata", "TerrainSource", "StreamflowInput", "KSettings", "GridSettings",
    "AssessmentInputSnapshot", "INPUT_SNAPSHOT_SCHEMA_VERSION",
    # results
    "ConnectivityMetrics", "ResidenceTimeMetrics", "ZoneMetrics", "ComponentScore",
    "HFCIResult", "AssessmentResultsV2", "RESULTS_SCHEMA_VERSION",
    # sensitivity
    "GeneratorType", "ScenarioStatus", "ScenarioSpec", "SensitivityScenarioManifest",
    "SENSITIVITY_MANIFEST_SCHEMA_VERSION", "DEFAULT_MAX_SCENARIOS",
    # hfci
    "ScoreCurve", "CapacityClass", "DEFAULT_CLASSES", "HFCIScoringProfileV1",
    "HFCI_PROFILE_SCHEMA_VERSION", "HFCI_VALIDATION_LABEL",
    # registry
    "SCHEMA_VERSIONS", "register_migration", "migrate",
]
