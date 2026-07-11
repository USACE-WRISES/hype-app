"""NRCS soils + derived-conductivity contracts (revision spec §4.2, §6).

`SoilDataSnapshot` is the immutable record of one NRCS acquisition (clipped polygons, map units,
components, horizons, textures, restrictions, overrides, chosen derivation policy, raw paths).
`DerivedConductivityProfile` is one component's depth-resolved KV/KH derivation. `GridConductivityAssignment`
is one model cell/layer's effective KH/KV with its origin (direct / derived / override / fallback).
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import Field

from ..provenance import HypeModel, HypeWarning, Provenance

SOIL_SNAPSHOT_SCHEMA_VERSION = "soil-data-snapshot/1.0"


class AggregationPolicy(str, Enum):
    dominant = "dominant"                 # highest-% major component, largest-overlap polygon (§6.6)
    weighted = "weighted"                 # component-% and overlap-area weighted (arith KH / harmonic KV)
    user_component = "user_component"     # analyst picks the representative component per map unit


class KOrigin(str, Enum):
    direct = "direct"                     # single representative Ksat used as-is
    derived = "derived"                   # component/polygon aggregation
    override = "override"                 # explicit NRCS override
    fallback = "fallback"                 # global manual KH/KV (below/ outside profile)


class Horizon(HypeModel):
    name: str | None = None
    top_cm: float | None = None           # depth below local ground surface
    bottom_cm: float | None = None
    ksat_um_s: float | None = None        # representative Ksat (micrometres/second)
    texture: str | None = None
    textures: list[str] = Field(default_factory=list)   # alternatives when multiple exist


class Restriction(HypeModel):
    kind: str | None = None               # e.g. "Lithic bedrock", "Densic material"
    top_cm: float | None = None
    is_bedrock: bool = False              # explicit lithic/paralithic/densic only (§6.9)


class Component(HypeModel):
    cokey: str | None = None
    name: str | None = None
    comppct_r: float | None = None        # representative component percentage
    major: bool = False
    horizons: list[Horizon] = Field(default_factory=list)
    restrictions: list[Restriction] = Field(default_factory=list)


class MapUnit(HypeModel):
    mukey: str
    musym: str | None = None
    name: str | None = None
    survey_area: str | None = None
    survey_version: str | None = None
    components: list[Component] = Field(default_factory=list)


class SoilPolygon(HypeModel):
    """One clipped MapunitPoly feature. Distinct polygons are preserved even when they share a mukey."""

    mupolygonkey: str
    mukey: str
    geometry: dict                        # GeoJSON geometry in the working CRS-serialized form
    area_m2: float | None = None


class SoilOverride(HypeModel):
    """An analyst override of a soil attribute (§6.4). Every override is fully audited."""

    target: str                           # "comppct" | "horizon_top" | "horizon_bottom" | "ksat" | "texture" | "restriction"
    source_key: str                       # mukey/cokey/chkey the override applies to
    original_value: object | None = None
    effective_value: object
    reason: str | None = None
    timestamp: datetime | None = None


class DerivedConductivityProfile(HypeModel):
    """One component's depth-resolved KV/KH derivation (§6.5–6.7)."""

    mukey: str | None = None
    cokey: str | None = None
    chkey: str | None = None              # horizon key
    mupolygonkey: str | None = None
    top_cm: float | None = None           # depth interval below local ground
    bottom_cm: float | None = None
    ksat_um_s: float | None = None
    ksat_unit: str = "um/s"
    kv_m_day: float | None = None         # converted vertical K
    anisotropy_ratio: float | None = None
    kh_m_day: float | None = None         # derived horizontal K
    aggregation: AggregationPolicy | None = None
    origin: KOrigin = KOrigin.derived
    provenance: Provenance | None = None


class GridConductivityAssignment(HypeModel):
    """One model cell/layer's effective KH/KV (§4.2 GridConductivityAssignment)."""

    layer: int
    row: int
    col: int
    kh_m_day: float
    kv_m_day: float
    origin: KOrigin
    source_keys: dict = Field(default_factory=dict)     # mukey/cokey/chkey/mupolygonkey used
    polygon_overlap_frac: float | None = None
    horizon_depth_frac: float | None = None
    warnings: list[HypeWarning] = Field(default_factory=list)


class CoverageReport(HypeModel):
    """Domain/volume coverage accounting (§6.10)."""

    domain_area_covered_pct: float | None = None
    volume_direct_pct: float | None = None
    volume_aggregated_pct: float | None = None
    volume_override_pct: float | None = None
    volume_fallback_pct: float | None = None
    volume_missing_pct: float | None = None


class SoilDataSnapshot(HypeModel):
    """Immutable record of one NRCS soils acquisition (§4.2 SoilDataSnapshot)."""

    schema_version: str = SOIL_SNAPSHOT_SCHEMA_VERSION
    spatial_retrieved_at: datetime | None = None
    tabular_retrieved_at: datetime | None = None
    service_endpoints: dict = Field(default_factory=dict)
    survey_versions: dict = Field(default_factory=dict)

    polygons: list[SoilPolygon] = Field(default_factory=list)
    map_units: list[MapUnit] = Field(default_factory=list)

    overrides: list[SoilOverride] = Field(default_factory=list)
    aggregation_policy: AggregationPolicy | None = None
    anisotropy_ratio: float | None = None          # prefilled from manual KH/KV (§6.5)

    missing_diagnostics: list[HypeWarning] = Field(default_factory=list)
    coverage: CoverageReport | None = None
    raw_response_paths: list[str] = Field(default_factory=list)
    source_columns_used: dict = Field(default_factory=dict)   # SDA schema-adapter record (§6.2)


__all__ = [
    "AggregationPolicy", "KOrigin", "Horizon", "Restriction", "Component", "MapUnit",
    "SoilPolygon", "SoilOverride", "DerivedConductivityProfile", "GridConductivityAssignment",
    "CoverageReport", "SoilDataSnapshot", "SOIL_SNAPSHOT_SCHEMA_VERSION",
]
