"""Shared provenance primitives + the base model for every HYPE versioned contract.

Per the revision spec (§3.1), *every* result must report its data source, retrieval date,
user modifications, applied defaults, missing/fallback data, scientific-method version, and
warnings/limitations. Rather than bolt those onto each contract ad hoc, they live here as small
reusable models embedded wherever provenance is required.

These are deliberately Shiny-independent and JSON-round-trippable so the same objects serialize
into `config/*.json` in the project archive and into the canonical report model.
"""
from __future__ import annotations

from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HypeModel(BaseModel):
    """Base for all HYPE contracts: strict (unknown keys rejected), assignment-validated.

    `model_dump(mode="json")` emits computed fields (e.g. `insertable`, `input_hash`) so they
    appear in persisted JSON and reports; the before-validator below drops those same keys on the
    way back in, so `model_validate(model.model_dump())` round-trips cleanly under `extra="forbid"`
    while genuine typos in real fields still raise.
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        ser_json_timedelta="iso8601",
    )

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_fields(cls, data):
        if isinstance(data, dict) and cls.model_computed_fields:
            drop = set(cls.model_computed_fields)
            if drop.intersection(data):
                return {k: v for k, v in data.items() if k not in drop}
        return data


class Severity(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"


class HypeWarning(HypeModel):
    """A single actionable warning/limitation attached to a result or data source.

    Named `HypeWarning` (not `Warning`) to avoid shadowing the builtin.
    """

    code: str                       # stable machine code, e.g. "snap_distance_large"
    message: str                    # human-facing text
    severity: Severity = Severity.warning
    context: dict = Field(default_factory=dict)


class Citation(HypeModel):
    """A literature or service citation, rendered verbatim in reports."""

    title: str
    url: str | None = None
    authors: str | None = None
    year: int | None = None
    accessed: date | None = None


class Provenance(HypeModel):
    """Where a value came from and how it was derived — the §3.1 provenance record."""

    source: str                              # "USGS StreamStats", "NRCS SDA", "manual", ...
    retrieved_at: datetime | None = None     # service retrieval / computation time
    method_version: str | None = None        # scientific-method or client version
    endpoint: str | None = None              # service URL when applicable
    user_modified: bool = False              # value edited by the analyst after retrieval
    applied_defaults: list[str] = Field(default_factory=list)
    fallbacks: list[str] = Field(default_factory=list)   # missing/fallback data used
    warnings: list[HypeWarning] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    notes: str | None = None

    def with_warning(self, code: str, message: str,
                     severity: Severity = Severity.warning, **context) -> "Provenance":
        """Return a copy with an added warning (contracts are treated as immutable snapshots)."""
        w = HypeWarning(code=code, message=message, severity=severity, context=context)
        return self.model_copy(update={"warnings": [*self.warnings, w]})


__all__ = ["HypeModel", "Severity", "HypeWarning", "Citation", "Provenance"]
