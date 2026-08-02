"""The three hydraulic dimensions, named once (revision spec §4).

The HYPORHEIC HYDRAULIC SIGNATURE is the combined characterization of exchange frequency,
duration, and extent for a modeled reach or scenario (§4.1). It is NOT a single score: the three
metrics do not share units, do not sum to a whole, and can move in opposite directions when
conductivity or gradient changes (§4.4). Anything in this package that combines them into one
number is a bug, not a feature.

This module holds only the identifiers and the display strings, with no dependencies at all, so
that `signature.py` (which owns the arithmetic and the help cards) and `functions/registry.py`
(which names the dimensions each function reads) can both import it without a cycle.

`DIM_LABEL` values are the strings `report.py` has always used. They are display names ONLY, and
they are also the `section` column of `site_metrics.csv`, so changing one changes a published
file. Machine keys -- results-schema field names, run-summary keys, CSV headers -- are independent
of these and must not follow them.
"""
from __future__ import annotations

__all__ = ["SIGNATURE_TITLE", "SIGNATURE_SUBTITLE", "SIGNATURE_SENTENCE", "SIGNATURE_ANALOGY",
           "FREQUENCY", "DURATION", "EXTENT", "SIGNATURE_DIMS", "DIM_LABEL", "DIM_CONTROLS",
           "DIM_SHORT"]

#: The name the interface uses for the three metrics taken together (§4.1, §20, §26).
SIGNATURE_TITLE = "Hyporheic Hydraulic Signature"

#: One line under the title, wherever the title appears.
SIGNATURE_SUBTITLE = ("A three-part characterization of exchange turnover, residence time, "
                      "and exchange extent.")

#: The concise explanation (§4.2), verbatim. Short enough to sit above the cards themselves.
SIGNATURE_SENTENCE = ("Frequency controls delivery, duration controls contact time, and extent "
                      "controls the size of the participating domain.")

#: The plain-language analogy (§4.3), for onboarding and help cards rather than results panes.
SIGNATURE_ANALOGY = ("The river sends water into a subsurface playground. Frequency tells us how "
                     "often water makes a trip into the playground, duration how long it stays, "
                     "and extent how large the participating playground is.")

FREQUENCY = "frequency"
DURATION = "duration"
EXTENT = "extent"

#: Display order everywhere: delivery, then contact time, then the size of the domain.
SIGNATURE_DIMS = (FREQUENCY, DURATION, EXTENT)

#: Full section names (report §5/§6/§7). Also the site_metrics.csv `section` values.
DIM_LABEL = {
    FREQUENCY: "Frequency of Hyporheic Exchange",
    DURATION: "Duration in Hyporheic Zone",
    EXTENT: "Extent of Hyporheic Zone",
}

#: One word for a pane chip or a table header, where the full label will not fit.
DIM_SHORT = {FREQUENCY: "Frequency", DURATION: "Duration", EXTENT: "Extent"}

#: What each dimension controls, in plain language (§4.2, §20). This is the phrase that makes a
#: function's reason for reading a dimension legible: nutrient cycling wants contact time, habitat
#: wants participating capacity.
DIM_CONTROLS = {
    FREQUENCY: "delivery",
    DURATION: "contact time",
    EXTENT: "participating capacity",
}
