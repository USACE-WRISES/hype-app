"""Versioned public contracts for the HYPE revision (spec §4.2).

Import site for every Pydantic model that crosses a persistence or process boundary: the frozen
input snapshot, USGS flow lookup, NRCS soils + derived conductivity, structured gradients,
hydraulic-alternatives manifest, and canonical results. All are Shiny-independent and
JSON-round-trippable.

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
    watershed_display_features,
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
from .inputs import (
    INPUT_SNAPSHOT_SCHEMA_VERSION,
    AssessmentInputSnapshot,
    GridSettings,
    KSettings,
    SiteMetadata,
    StreamflowInput,
    TerrainSource,
)
from .function_envelope import (
    BASECASE_ID,
    BASECASE_LABEL,
    FUNCTION_ENVELOPE_SCHEMA_VERSION,
    EnvelopeRow,
    FunctionEnvelope,
    SectionEnvelope,
)
from .results import (
    RESULTS_SCHEMA_VERSION,
    AssessmentResultsV2,
    CalibrationPair,
    CalibrationStats,
    CalibrationWell,
    ConnectivityMetrics,
    ContaminantScreening,
    FunctionScreening,
    GroundwaterCalibration,
    HabitatScreening,
    MicroplasticRetention,
    NutrientScreening,
    OpportunityPoint,
    ReactiveScreening,
    ResidenceTimeMetrics,
    ThermalOpportunity,
    ThresholdResult,
    ZoneMetrics,
)
from .alternatives import (
    ALT_STATUS_LABEL,
    ALTERNATIVES_MANIFEST_SCHEMA_VERSION,
    AltScenario,
    AltStatus,
    HydraulicAlternativesManifest,
)
from .comparison import (
    COMPARISON_COLLECTION_SCHEMA_VERSION,
    COMPARISON_FINDING_SCHEMA_VERSION,
    COMPARISON_MEMBER_SCHEMA_VERSION,
    COMPARISON_OBSERVATION_SCHEMA_VERSION,
    COMPARISON_SCENARIO_SCHEMA_VERSION,
    COMPARISON_SNAPSHOT_SCHEMA_VERSION,
    COMPARISON_VIEW_SCHEMA_VERSION,
    ComparisonCollectionV1,
    ComparisonFindingV1,
    ComparisonMemberV1,
    ComparisonMetricObservationV1,
    ComparisonScenarioV1,
    ComparisonSnapshotV1,
    ComparisonSourceStatus,
    ComparisonViewSettingsV1,
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
    "hydraulic-alternatives": ALTERNATIVES_MANIFEST_SCHEMA_VERSION,
    "function-envelope": FUNCTION_ENVELOPE_SCHEMA_VERSION,
    "assessment-results": RESULTS_SCHEMA_VERSION,
    "comparison-collection": COMPARISON_COLLECTION_SCHEMA_VERSION,
    "comparison-member": COMPARISON_MEMBER_SCHEMA_VERSION,
    "comparison-snapshot": COMPARISON_SNAPSHOT_SCHEMA_VERSION,
    "comparison-metric-observation": COMPARISON_OBSERVATION_SCHEMA_VERSION,
    "comparison-scenario": COMPARISON_SCENARIO_SCHEMA_VERSION,
    "comparison-finding": COMPARISON_FINDING_SCHEMA_VERSION,
    "comparison-view-settings": COMPARISON_VIEW_SCHEMA_VERSION,
}

# kind -> ordered list of (from_version, upgrade_fn). Registered by later phases as schemas evolve.
_MIGRATIONS: dict[str, list[tuple[str, Callable[[dict], dict]]]] = {}


def register_migration(kind: str, from_version: str,
                       fn: Callable[[dict], dict]) -> None:
    _MIGRATIONS.setdefault(kind, []).append((from_version, fn))


def migrate(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    """Apply any registered upgrade functions for `kind` until data is at the current version.

    The seam exists so version bumps are explicit. Registered upgrades run in order.
    """
    out = dict(data)
    for from_version, fn in _MIGRATIONS.get(kind, []):
        if str(out.get("schema_version", "")).endswith(from_version):
            out = fn(out)
    return out


def _drop_hfci_2_0(data: dict[str, Any]) -> dict[str, Any]:
    """assessment-results 2.0 -> 2.1: the HFCI composite index was removed. Drop the field so an
    older results payload validates under the current model (the report leads with the three
    hydraulic dimensions; there is no combined score).

    Hands off to the 2.1 step rather than stamping the current version, so the chain keeps
    running as further migrations are registered."""
    out = dict(data)
    out.pop("hfci", None)
    out["schema_version"] = "assessment-results/2.1"
    return out


def _add_functions_2_1(data: dict[str, Any]) -> dict[str, Any]:
    """assessment-results 2.1 -> 2.2: the hyporheic function screening container was added.

    Nothing to move: `functions` is optional and defaults to None, so a 2.1 payload is already
    valid under the 2.2 model and this only stamps the version. The step exists so the bump is
    explicit and so a later migration has a documented predecessor to chain from.

    Stamps the LITERAL 2.2, not `RESULTS_SCHEMA_VERSION`, for the same reason `_drop_hfci_2_0`
    does. `migrate()` makes a single pass and matches on the version it finds, so stamping the
    current version here would carry a 2.1 payload straight past every step registered after this
    one, silently skipping them."""
    out = dict(data)
    out["schema_version"] = "assessment-results/2.2"
    return out


def _swap_sensitivity_2_2(data: dict[str, Any]) -> dict[str, Any]:
    """assessment-results 2.2 -> 2.3: the gradient-bounds sensitivity manifest was replaced by
    the Hydraulic Alternatives sweep. Old payloads carry `"sensitivity": null` (or, rarely, a
    stale manifest that the new model cannot validate); the new `alternatives` field starts
    None either way, so dropping the key is the whole migration.

    Stamps the LITERAL 2.3, not `RESULTS_SCHEMA_VERSION`, per the chain rule documented on
    `_add_functions_2_1`."""
    out = dict(data)
    out.pop("sensitivity", None)
    out["schema_version"] = "assessment-results/2.3"
    return out


def _add_function_envelope_2_3(data: dict[str, Any]) -> dict[str, Any]:
    """assessment-results 2.3 -> 2.4: the hydraulic scenario envelope for the screening modules
    was added.

    Nothing to move: `function_envelope` is optional and defaults to None, so a 2.3 payload is
    already valid under the 2.4 model. The step exists so the bump is explicit and so the
    version-chain tests keep reaching the current version from 2.0.

    Stamps the LITERAL 2.4, not `RESULTS_SCHEMA_VERSION`, per the chain rule documented on
    `_add_functions_2_1`."""
    out = dict(data)
    out["schema_version"] = "assessment-results/2.4"
    return out


def _add_calibration_2_4(data: dict[str, Any]) -> dict[str, Any]:
    """assessment-results 2.4 -> 2.5: the groundwater observation-well calibration table was
    added.

    Nothing to move: `calibration` is optional and defaults to None, so a 2.4 payload is
    already valid under the 2.5 model. The step exists so the bump is explicit and so the
    version-chain tests keep reaching the current version from 2.0.

    Stamps the LITERAL 2.5, not `RESULTS_SCHEMA_VERSION`, per the chain rule documented on
    `_add_functions_2_1`."""
    out = dict(data)
    out["schema_version"] = "assessment-results/2.5"
    return out


register_migration("assessment-results", "2.0", _drop_hfci_2_0)
register_migration("assessment-results", "2.1", _add_functions_2_1)
register_migration("assessment-results", "2.2", _swap_sensitivity_2_2)
register_migration("assessment-results", "2.3", _add_function_envelope_2_3)
register_migration("assessment-results", "2.4", _add_calibration_2_4)


__all__ = [
    # flow
    "LatLon", "FlowCandidate", "FlowLookupSnapshot", "FLOW_SNAPSHOT_SCHEMA_VERSION",
    "watershed_display_features",
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
    "ConnectivityMetrics", "ResidenceTimeMetrics", "ZoneMetrics", "ThresholdResult",
    "OpportunityPoint", "ReactiveScreening", "NutrientScreening", "ContaminantScreening",
    "HabitatScreening", "MicroplasticRetention", "ThermalOpportunity",
    "FunctionScreening",
    "CalibrationWell", "CalibrationPair", "CalibrationStats", "GroundwaterCalibration",
    "AssessmentResultsV2", "RESULTS_SCHEMA_VERSION",
    # hydraulic scenario envelope
    "EnvelopeRow", "SectionEnvelope", "FunctionEnvelope",
    "FUNCTION_ENVELOPE_SCHEMA_VERSION", "BASECASE_LABEL", "BASECASE_ID",
    # alternatives
    "AltStatus", "ALT_STATUS_LABEL", "AltScenario", "HydraulicAlternativesManifest",
    "ALTERNATIVES_MANIFEST_SCHEMA_VERSION",
    # cross-project hydraulic comparisons
    "ComparisonSourceStatus", "ComparisonFindingV1", "ComparisonScenarioV1",
    "ComparisonMetricObservationV1", "ComparisonSnapshotV1", "ComparisonMemberV1",
    "ComparisonViewSettingsV1", "ComparisonCollectionV1",
    "COMPARISON_COLLECTION_SCHEMA_VERSION", "COMPARISON_MEMBER_SCHEMA_VERSION",
    "COMPARISON_SNAPSHOT_SCHEMA_VERSION", "COMPARISON_OBSERVATION_SCHEMA_VERSION",
    "COMPARISON_SCENARIO_SCHEMA_VERSION", "COMPARISON_FINDING_SCHEMA_VERSION",
    "COMPARISON_VIEW_SCHEMA_VERSION",
    # registry
    "SCHEMA_VERSIONS", "register_migration", "migrate",
]
