"""Structured groundwater-gradient configuration (revision spec §3.4, §4.2, §7).

Replaces the raw ``g_left_profile`` / ``g_right_profile`` strings with per-side control lists.
Each control carries a stable id, a normalized station (0=upstream .. 1=downstream), a preferred
gradient, and optional lower/upper gradients for sensitivity. Stations 0 and 1 are mandatory on
any configured side. Qualitative per-side categories map to LOCKED multipliers of a reference
slope (§3.4) — that mapping lives here so it is defined once, not scattered through UI code.

Sign convention (§7.3): positive gradient = higher floodplain head than the adjacent stream WSE
(gaining tendency); negative = lower floodplain head (losing tendency).
"""
from __future__ import annotations

from enum import Enum

from pydantic import Field, field_validator, model_validator

from ..provenance import HypeModel, Provenance

GRADIENT_SCHEMA_VERSION = "gradient-boundary-config/2.0"
GRADIENT_METHOD_VERSION = "head-anchor/1.0"
SIGN_CONVENTION = ("positive = higher floodplain head than stream WSE (gaining tendency); "
                   "negative = lower floodplain head (losing tendency)")


class Side(str, Enum):
    left = "left"
    right = "right"


class GradientQualitative(str, Enum):
    strongly_gaining = "strongly_gaining"
    slightly_gaining = "slightly_gaining"
    neutral = "neutral"
    slightly_losing = "slightly_losing"
    strongly_losing = "strongly_losing"


# Default scale (§3.4): category center = multiplier × reference slope. Since 2026-07 the UI
# may override the slight/strong magnitudes (from_qualitative(slight=…, strong=…)); these
# defaults reproduce the original locked scale.
QUALITATIVE_MULTIPLIER: dict[GradientQualitative, float] = {
    GradientQualitative.strongly_gaining: +1.0,
    GradientQualitative.slightly_gaining: +0.5,
    GradientQualitative.neutral: 0.0,
    GradientQualitative.slightly_losing: -0.5,
    GradientQualitative.strongly_losing: -1.0,
}

_STATION_TOL = 1e-6


class GradientControl(HypeModel):
    """One gradient control point along a boundary side."""

    id: str
    side: Side
    station: float = Field(ge=0.0, le=1.0)      # normalized arc-length position
    preferred: float                             # gradient, dimensionless m/m
    lower: float | None = None                   # sensitivity lower bound
    upper: float | None = None                   # sensitivity upper bound
    source: str = "manual"                       # "manual" | "qualitative" | "legacy_upgrade"
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _lower_le_upper(self) -> "GradientControl":
        if self.lower is not None and self.upper is not None and self.lower > self.upper:
            raise ValueError(f"control {self.id}: lower {self.lower} > upper {self.upper}")
        return self


class ReferenceSlope(HypeModel):
    """Reference slope used to scale qualitative categories (§7.4)."""

    value: float
    source: str                                  # "wse_raster" | "dem_drop" | "manual"
    method: str | None = None
    upstream_sample: float | None = None
    downstream_sample: float | None = None
    reach_distance_m: float | None = None
    policy_version: str = "reference-slope/1.0"


class LegacyGradientMeta(HypeModel):
    """Preserved legacy string profiles for a version-1 project (§7.7). Never silently reinterpreted."""

    boundary_condition_mode: str                 # "4 Corner Gradients" | "Spatially Varying Gradient"
    left_profile: str | None = None
    right_profile: str | None = None
    corner_gradients: dict[str, float] | None = None   # g_ul/g_ur/g_dl/g_dr
    method_label: str = "legacy interpolation (v1)"


class GradientBoundaryConfigV2(HypeModel):
    """The structured gradient configuration frozen into a run."""

    schema_version: str = GRADIENT_SCHEMA_VERSION
    method_version: str = GRADIENT_METHOD_VERSION
    units: str = "m/m"
    sign_convention: str = SIGN_CONVENTION
    mode: str = "quantitative"                    # "qualitative" | "quantitative"
    qualitative_left: GradientQualitative | None = None
    qualitative_right: GradientQualitative | None = None
    reference_slope: ReferenceSlope | None = None
    left_controls: list[GradientControl] = Field(default_factory=list)
    right_controls: list[GradientControl] = Field(default_factory=list)
    legacy: LegacyGradientMeta | None = None

    @field_validator("mode")
    @classmethod
    def _mode_valid(cls, v: str) -> str:
        if v not in ("qualitative", "quantitative"):
            raise ValueError("mode must be 'qualitative' or 'quantitative'")
        return v

    @staticmethod
    def _check_side(controls: list[GradientControl], side: Side) -> None:
        if not controls:
            return
        stations = sorted(c.station for c in controls)
        for a, b in zip(stations, stations[1:]):
            if abs(a - b) <= _STATION_TOL:
                raise ValueError(f"{side.value}: duplicate station {a}")
        if abs(stations[0]) > _STATION_TOL or abs(stations[-1] - 1.0) > _STATION_TOL:
            raise ValueError(f"{side.value} side must include controls at station 0 and 1")
        for c in controls:
            if c.side != side:
                raise ValueError(f"control {c.id} has side {c.side} in the {side.value} list")

    @model_validator(mode="after")
    def _validate_sides(self) -> "GradientBoundaryConfigV2":
        self._check_side(self.left_controls, Side.left)
        self._check_side(self.right_controls, Side.right)
        return self

    @classmethod
    def from_qualitative(cls, *, left: GradientQualitative, right: GradientQualitative,
                         reference_slope: ReferenceSlope,
                         provenance: Provenance | None = None,
                         slight: float = 0.5, strong: float = 1.0) -> "GradientBoundaryConfigV2":
        """Build a uniform-per-side config from qualitative categories (§3.4 multipliers).

        `slight`/`strong` set the multiplier magnitudes (symmetric for gaining/losing); the
        defaults reproduce the original locked ±0.5/±1.0 scale."""
        mult = {GradientQualitative.strongly_gaining: +float(strong),
                GradientQualitative.slightly_gaining: +float(slight),
                GradientQualitative.neutral: 0.0,
                GradientQualitative.slightly_losing: -float(slight),
                GradientQualitative.strongly_losing: -float(strong)}

        def _controls(side: Side, cat: GradientQualitative) -> list[GradientControl]:
            g = mult[cat] * reference_slope.value
            return [GradientControl(id=f"{side.value}-{s:g}", side=side, station=s,
                                    preferred=g, source="qualitative", provenance=provenance)
                    for s in (0.0, 1.0)]
        return cls(mode="qualitative", qualitative_left=left, qualitative_right=right,
                   reference_slope=reference_slope,
                   left_controls=_controls(Side.left, left),
                   right_controls=_controls(Side.right, right))


__all__ = [
    "Side", "GradientQualitative", "QUALITATIVE_MULTIPLIER", "SIGN_CONVENTION",
    "GradientControl", "ReferenceSlope", "LegacyGradientMeta", "GradientBoundaryConfigV2",
    "GRADIENT_SCHEMA_VERSION", "GRADIENT_METHOD_VERSION",
]
