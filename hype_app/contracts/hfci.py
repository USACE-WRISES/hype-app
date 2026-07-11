"""Hyporheic Functional Capacity Index scoring-profile contract (revision spec §4.2, §9).

The application LOADS a versioned scoring profile — equations and thresholds are data, not code
scattered through the UI (§9.1). This contract is that data shape; the frozen literature-derived
curve values ship as `hype_app/scoring_profiles/hfci_v1.json` (built in Phase 5). Scores are whole
0–15; classes are Low/Moderate/High with LOCKED default colors (§9.6).
"""
from __future__ import annotations

from pydantic import Field, model_validator

from ..provenance import Citation, HypeModel

HFCI_PROFILE_SCHEMA_VERSION = "hfci-scoring-profile/1.0"
HFCI_VALIDATION_LABEL = "Literature-derived HFCI v1 - validation ongoing"


class ScoreCurve(HypeModel):
    """A monotonic, piecewise-linear raw-driver -> 0..15 score curve (§9.2–9.4).

    Evaluated by linear interpolation over (knots_x, knots_y); values beyond the
    literature-supported range are flagged as extrapolation by the scorer.
    """

    driver: str                                   # what raw quantity feeds this component
    raw_unit: str
    knots_x: list[float]                          # raw driver values, ascending
    knots_y: list[float]                          # score at each knot, 0..15
    supported_range: list[float] | None = None    # [lo, hi] of the literature-supported driver range
    citations: list[Citation] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_knots(self) -> "ScoreCurve":
        if len(self.knots_x) < 2 or len(self.knots_x) != len(self.knots_y):
            raise ValueError(f"{self.driver}: need >=2 matching knots_x/knots_y")
        if any(b <= a for a, b in zip(self.knots_x, self.knots_x[1:])):
            raise ValueError(f"{self.driver}: knots_x must be strictly ascending")
        if any(not (0.0 <= y <= 15.0) for y in self.knots_y):
            raise ValueError(f"{self.driver}: knots_y must lie in [0, 15]")
        return self


class CapacityClass(HypeModel):
    name: str                                     # "Low" | "Moderate" | "High"
    min_score: int                                # inclusive, whole 0..15
    max_score: int                                # inclusive
    color: str                                    # hex token


# LOCKED default class bands + colors (§3.6, §9.6).
DEFAULT_CLASSES: list[CapacityClass] = [
    CapacityClass(name="Low", min_score=0, max_score=5, color="#d73027"),
    CapacityClass(name="Moderate", min_score=6, max_score=10, color="#fdbf11"),
    CapacityClass(name="High", min_score=11, max_score=15, color="#2c7bb6"),
]


class HFCIScoringProfileV1(HypeModel):
    """A versioned, literature-derived HFCI scoring profile."""

    schema_version: str = HFCI_PROFILE_SCHEMA_VERSION
    profile_id: str = "hfci-v1"
    version: str = "1.0.0"
    validation_label: str = HFCI_VALIDATION_LABEL
    applicable_domain: str | None = None
    exchange: ScoreCurve
    storage: ScoreCurve
    processing: ScoreCurve
    rounding_rule: str = "half_up"                # round-half-up to a whole number (§9.5)
    classes: list[CapacityClass] = Field(default_factory=lambda: list(DEFAULT_CLASSES))
    storage_denominators: list[str] = Field(default_factory=list)   # supported reference-area methods (§9.3)
    citations: list[Citation] = Field(default_factory=list)
    evidence_notes: str | None = None
    change_log: list[str] = Field(default_factory=list)

    def class_for(self, score: int) -> CapacityClass | None:
        for c in self.classes:
            if c.min_score <= score <= c.max_score:
                return c
        return None


__all__ = [
    "ScoreCurve", "CapacityClass", "DEFAULT_CLASSES", "HFCIScoringProfileV1",
    "HFCI_PROFILE_SCHEMA_VERSION", "HFCI_VALIDATION_LABEL",
]
