"""USGS StreamStats / NSS flow-lookup contracts (revision spec §4.2, §5).

`FlowCandidate` is one normalized discharge statistic returned by the service; `FlowLookupSnapshot`
is the immutable record of a whole lookup (request point, snapped point, watershed, candidates,
raw-response paths, provenance). A candidate is *insertable* into the canonical flow input only
when it is a finite, positive discharge with a recognized unit conversion and is not excluded (§5.4).
National/extrapolated/stale/user-edited candidates remain insertable but carry prominent warnings.
"""
from __future__ import annotations

import math
from datetime import datetime

from pydantic import Field, computed_field

from ..provenance import Citation, HypeModel, HypeWarning

FLOW_SNAPSHOT_SCHEMA_VERSION = "flow-lookup-snapshot/1.0"


class LatLon(HypeModel):
    lat: float
    lon: float


class FlowCandidate(HypeModel):
    """A single normalized discharge statistic (§5.3)."""

    id: str                                        # stable candidate id
    statistic_group: str | None = None
    statistic_code: str | None = None
    result_code: str | None = None
    description: str | None = None

    original_value: float
    original_unit: str                             # e.g. "ft^3/s"
    value_cfs: float | None = None                 # normalized cubic feet / s
    value_cms: float | None = None                 # normalized cubic metres / s

    # Recurrence / duration / exceedance metadata — only when the service states it unambiguously.
    recurrence_years: float | None = None
    duration: str | None = None
    annual_exceedance_prob: float | None = None
    nonexceedance_prob: float | None = None

    regression_region: str | None = None
    region_weight: float | None = None

    equation: str | None = None
    parameters: dict = Field(default_factory=dict)
    parameter_ranges: dict = Field(default_factory=dict)
    in_range: bool | None = None

    approval_status: str | None = None             # e.g. "Approved" / "Provisional"
    applicability: str | None = None
    is_national: bool = False
    is_extrapolated: bool = False

    # Uncertainty (when returned).
    standard_error: float | None = None
    interval_low: float | None = None
    interval_high: float | None = None
    equivalent_years: float | None = None

    warnings: list[HypeWarning] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    excluded: bool = False                         # explicitly excluded/disabled by service

    @computed_field  # type: ignore[prop-decorator]
    @property
    def insertable(self) -> bool:
        """§5.4: insertable only if a finite, positive discharge (recognized cfs normalization)
        that is not excluded. Non-discharge results have no cfs value and are never insertable."""
        v = self.value_cfs
        return (not self.excluded and v is not None
                and math.isfinite(v) and v > 0.0)


class FlowLookupSnapshot(HypeModel):
    """Immutable record of one StreamStats/NSS lookup (§4.2 FlowLookupSnapshot)."""

    schema_version: str = FLOW_SNAPSHOT_SCHEMA_VERSION
    requested_point: LatLon
    snapped_point: LatLon | None = None
    snap_distance_m: float | None = None
    selected_region: str | None = None

    watershed_geojson: dict | None = None          # delineated basin (GeoJSON geometry/feature)
    regression_regions: list[str] = Field(default_factory=list)
    region_weights: dict = Field(default_factory=dict)
    basin_characteristics: dict = Field(default_factory=dict)

    candidates: list[FlowCandidate] = Field(default_factory=list)
    selected_candidate_id: str | None = None

    raw_response_paths: list[str] = Field(default_factory=list)
    service_endpoints: dict = Field(default_factory=dict)
    retrieved_at: datetime | None = None

    methods: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    approval_status: str | None = None
    warnings: list[HypeWarning] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)

    def selected(self) -> FlowCandidate | None:
        if not self.selected_candidate_id:
            return None
        return next((c for c in self.candidates if c.id == self.selected_candidate_id), None)


def watershed_display_features(
        watershed_geojson: dict | None) -> tuple[dict | None, tuple[float, float] | None]:
    """(watershed FeatureCollection | None, pour-point (lat, lon) | None) for the review map.

    `FlowLookupSnapshot.watershed_geojson` stores the raw ss-delineate `featurecollection`
    list, whose items sometimes arrive nested one list deep — flatten one level, then match
    the documented member names ("globalwatershed" / "globalwatershedpoint"). Never raises:
    a malformed payload just yields (None, None).
    """
    items: list = []
    for it in ((watershed_geojson or {}).get("featurecollection") or []):
        items.extend(it if isinstance(it, list) else [it])
    ws = pour = None
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("feature"), dict):
            continue
        if it.get("name") == "globalwatershed":
            ws = it["feature"]
        elif it.get("name") == "globalwatershedpoint":
            try:
                lon, lat = it["feature"]["features"][0]["geometry"]["coordinates"][:2]
                pour = (float(lat), float(lon))
            except Exception:  # noqa: BLE001
                pour = None
    return ws, pour


__all__ = ["LatLon", "FlowCandidate", "FlowLookupSnapshot", "FLOW_SNAPSHOT_SCHEMA_VERSION",
           "watershed_display_features"]
