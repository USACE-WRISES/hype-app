"""Hydraulic scenario envelope contracts: functional screening across the alternatives sweep.

One `SectionEnvelope` per screening section that the report already renders, carrying the min
and max of that section's results over the Basecase PLUS every completed alternative, with the
functional inputs held constant. The Basecase stays the primary estimate; these are subordinate
context.

THE ENVELOPE IS NEVER DERIVED FROM THE HYDRAULIC HEADLINES. A screening mass is not a monotone
function of turnovers per km, median residence time or active depth: the alternative with the
greatest exchange frequency may transform the least nitrogen if its residence times are much
shorter. Every value here comes from re-running the screening modules against that scenario's
own path distribution and zone results.

NOTHING AGGREGATES ACROSS SECTIONS. There is no total, no composite and no score, and the
section key carries the pollutant endpoint, so two chemicals' masses have no expression in this
model in which they could meet.
"""
from __future__ import annotations

from pydantic import Field

from ..provenance import HypeModel

FUNCTION_ENVELOPE_SCHEMA_VERSION = "function-envelope/1.0"

#: Label for the Basecase inside `lo_case` / `hi_case`. It is a hydraulic realization like any
#: other and folds into its own envelope, which is what makes `lo <= base <= hi` hold.
BASECASE_LABEL = "Basecase"
BASECASE_ID = "base"


class EnvelopeRow(HypeModel):
    """One metric's range across every hydraulic case."""

    #: The CONTRACT field name, never a display twin. The registry headlines some pollutant rows
    #: with `total_mass_display` and friends, which `_build_functions` filters away when it
    #: validates into `ContaminantScreening`; `screen.CANONICAL_FOR_DISPLAY` resolves those back
    #: before they reach this model, or every endpoint would fold to None in silence.
    key: str
    name: str
    unit: str = ""
    #: Formatting carried off the registry `PaneKpi` / `PaneRow` so the range renders the way the
    #: section above it renders the same quantity. Without these a fraction prints
    #: "0.632 to 0.871" directly beneath a table saying "63.2%".
    kind: str = "num"
    digits: int = 3

    base: float | None = None      # the Basecase value, which is the headline the range sits under
    lo: float
    hi: float
    lo_case: str = BASECASE_LABEL
    hi_case: str = BASECASE_LABEL
    #: Stable scenario slugs beside the labels, because labels are user-facing text that can be
    #: renamed or repeated while `AltScenario.id` cannot.
    lo_case_id: str = BASECASE_ID
    hi_case_id: str = BASECASE_ID
    #: Cases that contributed a finite value. A primary row always has `n == case_count`; a
    #: supporting row may be short, and the report then discloses "n of N cases" rather than
    #: presenting a partial fold as if it covered everything.
    n: int = 0


class SectionEnvelope(HypeModel):
    """The envelope for ONE screening section, joined to the report by `key`."""

    #: Matches `report.function_section_key()`, so `"pollutant.zinc"` and `"pollutant.cobalt"`
    #: are separate records by construction.
    key: str
    title: str = ""
    #: None when the section could not be enveloped. `withheld_reason` then says why, and the
    #: report prints that instead of quietly omitting the block.
    primary: EnvelopeRow | None = None
    supporting: list[EnvelopeRow] = Field(default_factory=list)
    withheld_reason: str | None = None


class FunctionEnvelope(HypeModel):
    """The whole envelope: every enveloped section plus the cases it was folded over."""

    schema_version: str = FUNCTION_ENVELOPE_SCHEMA_VERSION
    method_version: str = ""

    #: Completed alternatives, EXCLUDING the Basecase. `case_count` includes it. Two explicit
    #: fields rather than one ambiguous count, because "9 runs" and "9 alternatives" differ by
    #: exactly the run the reader is looking at.
    alternative_count: int = 0
    case_count: int = 0
    case_ids: list[str] = Field(default_factory=list)
    case_labels: list[str] = Field(default_factory=list)

    sections: list[SectionEnvelope] = Field(default_factory=list)

    def by_key(self) -> dict[str, SectionEnvelope]:
        return {s.key: s for s in self.sections}


__all__ = [
    "FUNCTION_ENVELOPE_SCHEMA_VERSION", "BASECASE_LABEL", "BASECASE_ID",
    "EnvelopeRow", "SectionEnvelope", "FunctionEnvelope",
]
