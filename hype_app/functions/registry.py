"""Process registry for hyporheic function screening (functions plan §5, Part I-II).

One entry per screenable function. No chemistry, no rate and no threshold is hardcoded anywhere else
in the codebase, so adding a function is a row here plus a pane-factory entry.

Two kinds of function exist and the discriminator is load-bearing:

* ``residence_time`` -- driven by how long water stays under (denitrification, contaminant
  attenuation, thermal regulation).
* ``extent``         -- driven by volume, area and depth (invertebrate habitat), with no kinetics
  and no rate at all.

UNITS: thresholds and rates are stored in HOURS or per-day because that is how the literature and
the reference project report them. The residence-time distribution is in DAYS. Conversion happens at
the calculation boundary in `screen.py`, never here.

CITATIONS: every spec names `sources`, which are keys into `helptext.SOURCES`; `citation` is a
derived property so nothing downstream has to change. `transferability_note` is required non-empty
and a rate may not ship without `rate_citation`. Framework §14.2 is explicit -- "No
process-specific threshold should be displayed without its source and transferability note" -- and
`validate_registry()` enforces it at import time.

HELP TEXT lives in `helptext.Help` objects, not prose strings, and is length-checked at import.
See that module for why.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .helptext import (MAX_SLOT_WORDS, SOURCES, Help, flat_text, format_sources, render_card,
                       source_labels, validate_help)

# Kinds
KIND_RESIDENCE_TIME = "residence_time"
KIND_EXTENT = "extent"
#: Driven by DISTANCE rather than time (microplastic retention). Not a variant of the others: the
#: independent variable, the coefficient and its units all differ, and the screening reference's
#: first implementation rule is that a per-day rate must never reach this path. Munz et al. (2024)
#: measured retention profiles independent of flow duration, so this is empirical, not a modelling
#: preference.
KIND_PARTICULATE = "particulate"
KINDS = (KIND_RESIDENCE_TIME, KIND_EXTENT, KIND_PARTICULATE)

# Kinetic forms. `relaxation` is first-order toward equilibrium with no onset and no mass, used by
# the thermal screen. `none` is the extent kind, which has no kinetics.
KINETICS_FIRST_ORDER = "first_order"
KINETICS_ZERO_ORDER = "zero_order"
KINETICS_RELAXATION = "relaxation"
KINETICS_NONE = "none"
KINETICS = (KINETICS_FIRST_ORDER, KINETICS_ZERO_ORDER, KINETICS_RELAXATION, KINETICS_NONE)

RATE_FIRST_ORDER_PER_DAY = "1/day"
RATE_ZERO_ORDER_MG_L_DAY = "mg/L/day"
RATE_TIMESCALE_H = "hours"


# --------------------------------------------------------------------------- the oxygen gate
# Denitrification does not start when water enters the bed: oxygen is the preferred electron
# acceptor and must be drawn down first. These constants convert a dissolved-oxygen concentration
# the user CAN estimate into the onset time they cannot.
#
# The consumption rate is ZERO-ORDER, which is not an approximation of convenience. With the
# half-saturation constant K_O2 = 6.25e-3 mmol/L (0.200 mg/L) from Trauth and Fleckenstein (2017),
# the Monod term C/(K+C) at a stream DO of 9 mg/L is 0.978: consumption is substrate-saturated.
# Zero order also makes the onset time linear in the DO the user enters, so the input behaves the
# way they expect.
#
# THE RANGE IS ANCHORED AT BOTH ENDS BY INDEPENDENT SOURCES, not invented:
#   low  15.3 mg/L/day = Trauth and Fleckenstein mumax,AR (4.78e-1 mmol/L/d x 32 g/mol)
#                        -> 14.0 h to anoxia at DO 9 mg/L
#   high 31.0 mg/L/day = the rate that reproduces the 6.9 h transition Zarnetske et al. (2011)
#                        OBSERVED at Drift Creek, Oregon
# A modeling parameter set and a field measurement bracket the same quantity within a factor of 2.2.
# The central value is their midpoint and is explicitly not itself a measured rate.
DO_STREAM_DEFAULT_MG_L = 9.0            # near saturation at 20 C; user-estimable
DO_ANOXIC_THRESHOLD_MG_L = 0.1          # project requirement (denitrification plan §1)
OXYGEN_CONSUMPTION_MG_L_DAY = (15.3, 23.2, 31.0)


# --------------------------------------------------------------------- first-order validity
# Nitrate decay is modeled first-order, which is what the reference project does (IREACT 1) and
# what the denitrification literature fits. First order has no ceiling, so k * C keeps growing
# with concentration while real denitrification saturates. The bound is the Monod half-saturation
# constant: below it the Monod term is close to linear and first order is a fair approximation;
# above it the fit is being extrapolated past where it was measured.
#
# This is not a caveat invented here. It is the exact justification Lotts and Hester give for
# choosing first order: their nitrate sits below the half-saturation constant of a typical
# riparian soil. So the same number that licenses the model also bounds it.
NITRATE_DEFAULT_MG_N_L = 1.0            # Hester et al. (2016) base case
MONOD_HALF_SATURATION_MG_N_L = 1.64

# Built from SOURCES so a reference is written once and rendered the same way everywhere.
SATURATION_CITATION = format_sources(("zarnetske2012", "lotts2022"))
OXYGEN_CITATION = format_sources(("trauth2017", "zarnetske2011"))
OXYGEN_TRANSFERABILITY = (
    "Oxygen consumption depends on organic carbon supply, temperature and microbial activity, none "
    "of which this model represents. The bounds span a modeling parameter set and a single field "
    "observation, so treat the onset time as a scenario and sweep it."
)


# ------------------------------------------------------------------------- pane layout
# The pane's rows are DATA, not code. Before this, `_pane_fn` carried one hardcoded row list per
# section inside a widening if/elif, so every section had its own private idea of what a result
# looks like and the app.py source was linted with string counts. Now the registry names the
# metric and the pane only formats it.
#: `pct_sig` is a percentage in SIGNIFICANT FIGURES rather than one decimal place. One decimal is
#: right for a share of exchanged flow, which lives between 1 and 100; it destroys a share of
#: STREAMFLOW, which on a large river is a few thousandths of a percent and rounds to a flat "0".
#: Same reasoning that put `fmt_sig` in the report: these span orders of magnitude across sites.
ROW_KINDS = ("num", "pct", "pct_sig", "int")


@dataclass(frozen=True)
class PaneRow:
    """One row in a pane table. `key` indexes the flat dict `screen_process` returns.

    Long units go in `label`, following the existing "Areal rate (g N/m2/day)" convention; `unit`
    is for the short suffixes the pane appends to the value itself, like " h". `help` renders an
    info tip beside the label, for the rare row whose NAME is not self-explanatory.

    `label_key` and `unit_key` read from the RESULT when set, for a row whose wording or units
    depend on which endpoint is selected. Same mechanism as `PaneKpi`; see its docstring.

    `shared` marks a row that is IDENTICAL for every endpoint a section screens -- read from the
    hydraulics, with no rate and no concentration anywhere in its derivation. It matters only for
    Pollutant Attenuation, the one section that produces several results at once: without it the
    pane repeated eight hydraulic values once per ticked chemical, so three endpoints printed the
    same path count three times. Marked rows are hoisted out of the per-endpoint disclosures and
    shown once. It is DATA rather than a rule in app.py because whether a number depends on the
    rate is a property of how it is computed, and `screen.py` is where that is known."""

    key: str
    label: str
    kind: str = "num"
    unit: str = ""
    digits: int = 3
    label_key: str = ""
    unit_key: str = ""
    shared: bool = False
    help: Help | None = None


@dataclass(frozen=True)
class PaneKpi:
    """One headline result. Echoes `report.headline_cards` so the pane and the report are
    recognisably the same shape: a name, a value with its unit, and a range.

    `context_key` supplies a second value shown beside the number, which is how the concentration
    reduction card shows what the concentration actually fell to.

    `label_key` and `unit_key` read the label and unit OUT OF THE RESULT when set, falling back to
    the static ones. That is how one section serves several vocabularies without a branch in
    app.py: the pollutant endpoints must say attenuation for a metal and transformation for an
    organic (screening reference §7), and a microgram-per-litre endpoint must read g/day where a
    milligram-per-litre one reads kg/day. The words themselves live in `pollutants.TERMS`, which is
    validated against the reference's banned-word table, so a forbidden label cannot arrive here."""

    label: str
    key: str
    unit: str = ""
    kind: str = "num"
    digits: int = 3
    low_key: str = ""
    high_key: str = ""
    context_key: str = ""
    context_fmt: str = ""          # e.g. "{c_in} to {v} mg/L"; `v` is the context value
    label_key: str = ""
    unit_key: str = ""
    help: Help | None = None


@dataclass(frozen=True)
class PaneGroup:
    """One titled table. `assumed_rate` drives the tier chip: True means every number below it
    scales with a rate constant the user accepted or entered, False means the block survives
    disowning every rate. `list_key` reads rows out of a list in the result (the thermal response
    bands) instead of `rows`."""

    title: str
    rows: tuple[PaneRow, ...] = ()
    help: Help | None = None
    assumed_rate: bool = False
    list_key: str = ""


@dataclass(frozen=True)
class PaneCurve:
    """The R(tau) sparkline, behind the More-metrics disclosure rather than on the card.

    It used to hang off a PaneGroup with no label and no tooltip at all, in 34px with no y axis,
    which is unreadable in the case that matters: R is monotone in tau and the marker sits at
    1/rate, so a run near complete removal pins the whole left half at the ceiling by algebra. It
    still earns a place -- it is the only on-pane signal of how much the answer rests on an assumed
    rate -- but it earns it with a name and an explanation, which is why `help` is enforced."""

    key: str
    label: str
    help: Help | None = None


@dataclass(frozen=True)
class ProcessSpec:
    """One screenable hyporheic function and the assumptions behind it.

    `rate` is a (low, central, high) triple, or None when no defensible central value exists yet.
    None is a deliberate state, not an oversight: the rate-free outputs still compute and the mass
    chain stays empty until the user supplies a rate they own.

    Low and high are SENSITIVITY BOUNDS, not confidence intervals; the report must say so.
    """

    key: str
    display_label: str
    kind: str
    kinetics: str
    transferability_note: str
    #: Keys into `helptext.SOURCES`. `citation` is derived from these, so a reference is written
    #: once and rendered three ways: a short label in the card, a formatted line in the pane
    #: footer, and the contract string the report already reads.
    sources: tuple[str, ...] = field(default_factory=tuple)
    #: Used in place of `sources` when there is genuinely nothing to cite, which is a real state:
    #: no defensible rate library exists for arbitrary contaminants.
    no_source_note: str = ""
    ecosystem_context: str = ""
    required_conditions: str = ""
    # residence-time kinds
    oxygen_gated: bool = False
    rate: tuple[float, float, float] | None = None
    rate_unit: str | None = None
    rate_citation: str | None = None
    rate_label: str | None = None
    # heat exchanges with the solid matrix; solutes do not. Explicit so it is never an invisible
    # assumption (thermal plan §13.5).
    retardation: float = 1.0
    concentration_label: str | None = None
    concentration_unit: str | None = None
    #: Tooltips, in named slots. `help` is the section card; the other two sit on their inputs.
    #: `rate_help` carries the published spread, which is deliberately SEPARATE from the `rate`
    #: triple: that triple is the sensitivity sweep and drives the reported range, so widening it
    #: to the full literature spread would blow the headline range out past an order of magnitude
    #: and make it useless. Guidance informs the choice; the sweep tests it.
    help: Help = field(default_factory=Help)
    rate_help: Help = field(default_factory=Help)
    concentration_help: Help = field(default_factory=Help)
    #: The chip beside the exchange-limitation sentence, when a section's regime means something
    #: different from the solute one. Thermal's Damkohler compares residence time against a heat
    #: response time rather than a reaction rate, so it needs its own card; everything else falls
    #: back to the solute card in app.py.
    regime_help: Help | None = None
    #: Named cases for a rate the reader PICKS rather than measures, as `(value, label)` pairs.
    #: The values must BE `rate`, and that is enforced: the pane renders these as the only choices,
    #: so a fourth option here would be a scenario the sensitivity sweep never brackets. Empty
    #: wherever the rate is a number the reader types.
    rate_scenarios: tuple[tuple[float, str], ...] = ()
    #: One line above the headline saying what the section does NOT cover, when its own name
    #: invites a wider reading than the model supports. "Nutrient Cycling" implies phosphorus and
    #: uptake; only nitrate is modeled. Naming the exclusion is the point -- the process name alone
    #: identifies what IS there and leaves the reader to assume the rest. Empty where the label
    #: already matches the scope.
    scope_note: str = ""
    #: Pane layout. All default empty so a spec that has not been authored yet still validates.
    kpis: tuple[PaneKpi, ...] = ()
    pane_groups: tuple[PaneGroup, ...] = ()
    detail_rows: tuple[PaneRow, ...] = ()
    detail_curve: PaneCurve | None = None
    #: Read-only settings the RUN was configured with, for a section that supplies no input of its
    #: own. These fill the Advanced inputs panel, which every section's disclosure carries: Habitat
    #: Creation has no rate and no concentration, but its volume is the zone pass times porosity,
    #: so porosity and the particle density that resolved the zone ARE its inputs. They live on
    #: other panes because changing either means re-running the model, which is exactly why they
    #: are shown rather than offered. Empty wherever a section has editable inputs instead.
    run_settings: tuple[PaneRow, ...] = ()
    #: Where those settings are set and what applying a change costs. Required with the above, and
    #: enforced: the panel is named "Advanced inputs" on every pane, and this line is what stops
    #: that being a promise a read-only table does not keep.
    run_settings_note: str = ""

    @property
    def citation(self) -> str:
        """The reference string the results contract and report carry, built from `sources`."""
        return format_sources(self.sources) if self.sources else self.no_source_note

    @property
    def has_rate(self) -> bool:
        return self.rate is not None

    @property
    def rate_central(self) -> float | None:
        return None if self.rate is None else float(self.rate[1])


# ---------------------------------------------------------------------- shared pane help cards
# Declared above the specs that carry them. Tier cards first: every pane group takes one of the
# two, so the distinction the plan cares about (which results survive disowning the rate
# constants) is stated once and reads identically in all four sections.
ASSUMED_RATE_HELP = Help(
    title="Under the assumed rate",
    definition="Everything here scales with the rate constant you accepted or entered.",
    method="Change it under Advanced inputs and every value in this block moves.",
    note="The blocks marked hydraulics do not depend on any rate constant.",
)

HYDRAULICS_HELP = Help(
    title="From the hydraulics",
    definition="Read from modeled residence times and flows, with no reaction rate applied.",
    note="These survive even if every rate constant below is disowned.",
)

#: The assumed-rate tier for a section whose rates are CITED rather than entered. Same claim as
#: `ASSUMED_RATE_HELP` about what moves, minus its pointer to an input that does not exist here:
#: Pollutant Attenuation offers no box to overwrite a published rate with, which is deliberate and
#: is the reason the panel it names is read only.
CITED_RATE_HELP = Help(
    title="Under the cited rate",
    definition="Everything here scales with the published rate constant for this endpoint.",
    method="Which paper each rate comes from is under Sources; none of them is editable.",
    note="The blocks marked hydraulics do not depend on any rate constant.",
)

#: The path count is the one number on these panes that reliably gets misread. The delineation
#: releases particles TWICE -- once to map the zone's extent (thousands, reported on the Hyporheic
#: Zone pane) and once to follow the flux (a handful, reported here) -- and the two sit next to
#: each other in the tree with no cue that they answer different questions.
RETURNING_PATHS_HELP = Help(
    title="Returning flow paths",
    definition="Particles released where stream water enters the bed and returns to it.",
    method="Four per downwelling cell, each weighted by that cell's inflow.",
    note="Not the zone's particle count, which maps extent rather than flux.",
)

DOWNWELLING_CELLS_HELP = Help(
    title="Downwelling cells",
    definition="Streambed cells where water enters the bed rather than leaving it.",
    note="A gaining reach has few, so the path count above is small by hydrology.",
)

#: Rides BOTH reactive sections, so the title has to be section-neutral. It read "Removal
#: opportunity" and Pollutant Attenuation drew it over metal endpoints, where reference §7 forbids
#: that word outright -- sorption is reversible and calling it removal is the claim the whole
#: vocabulary table exists to prevent. The curve's own label already differed per section; only the
#: card had been left behind.
OPPORTUNITY_CURVE_HELP = Help(
    title="Reaction opportunity",
    definition="Share of returning exchange reacting, against the reaction timescale you assume.",
    method="Swept from 15 minutes to 40 days. No onset and no rate constant.",
    rows_label="How to read it",
    rows=(("Marker", "the timescale this run assumed"),
          ("Steep curve", "the answer follows the assumption, not the site")),
    sources=("zarnetske2012",),
)

#: Headline cards, one per KPI that needs more than its label.
NUTRIENT_MASS_HELP = Help(
    title="Estimated nitrate-N transformed",
    definition="Nitrate nitrogen converted to nitrogen gas along returning hyporheic paths each day.",
    method="Summed over every returning path, each weighted by its own flow.",
    note="Within the modeled reach only. It is not a whole-stream load reduction.",
    sources=("zarnetske2011",),
)

NUTRIENT_REDUCTION_HELP = Help(
    title="Concentration reduction",
    definition="Share of the nitrate entering the bed that does not come back out.",
    method="Flow weighted over returning paths, so it is a load reduction, not a path average.",
    note="A share of the exchanged load, not of the whole stream load.",
    sources=("zarnetske2011",),
)

NUTRIENT_AREAL_HELP = Help(
    title="Removal per streambed area",
    definition="Nitrate removed per square metre of streambed per day.",
    method="Total mass divided by the streambed area of the modeled reach.",
    note="The form published denitrification rates are usually reported in.",
    sources=("hester2016",),
)

NUTRIENT_PER_KM_HELP = Help(
    title="Removal per stream km",
    definition="Nitrate removed per kilometre of channel per day.",
    method="Total mass divided by the modeled reach length.",
    note="Extrapolating beyond this reach assumes the next kilometre behaves the same.",
)

#: The pollutant trio mirrors the nutrient trio above, key for key, because the two sections answer
#: the same question about different solutes and a reader moving between them should not have to
#: re-learn the card. What differs is provenance: every rate here is cited rather than assumed, and
#: the two mass cards carry no sensitivity range at all -- see the KPI list for why.
#:
#: All three said the rate was the user's, which stopped being true when the cited-endpoint library
#: landed and the section stopped offering a rate box at all. Corrected 2026-08-01.
POLLUTANT_MASS_HELP = Help(
    title="Total attenuated",
    definition="Mass taken out of the water column along returning hyporheic paths each day.",
    method="Summed over every returning path, each weighted by its own flow.",
    note="Attenuation, not destruction. Sorbed and stored mass can return as conditions change.",
)

POLLUTANT_REDUCTION_HELP = Help(
    title="Concentration reduction",
    definition="Share of the contaminant entering the bed that does not come back out.",
    method="Flow weighted over returning paths, using this endpoint's published rate.",
    note="A share of the exchanged load in returning water, not of the whole stream load.",
)

POLLUTANT_AREAL_HELP = Help(
    title="Per streambed area",
    definition="Mass acted on per square metre of streambed per day.",
    method="Total mass divided by the streambed area of the modeled reach.",
    note="The range spans the spread the endpoint's own study reported, never a confidence "
         "interval.",
)

POLLUTANT_PER_KM_HELP = Help(
    title="Per stream km",
    definition="Mass acted on per kilometre of channel per day.",
    method="Total mass divided by the modeled reach length.",
    note="Extrapolating beyond this reach assumes the next kilometre behaves the same.",
)

#: The regime verdict is the one output that says whether the rate matters at all, so it rides the
#: card. Everything it implies for the reach sits behind the disclosure.
POLLUTANT_REGIME_HELP = Help(
    title="Exchange limitation",
    definition="Whether the answer is set by the reaction rate, the residence time, or neither.",
    method="Damkohler number, the assumed rate times the median residence time.",
    rows_label="Bands",
    rows=(("Under 0.01", "reaction-limited, near zero"),
          ("Around 1", "most informative"),
          ("Over 100", "transport-limited, exchange sets it")),
    sources=("harvey2013",),
)

#: Microplastics. Every word here is checked against reference §7: retention, never removal.
MICROPLASTIC_RETENTION_HELP = Help(
    title="Reach-scale retention",
    definition="Share of passing microplastic reaching long-term storage in the bed.",
    method="Empirical coefficient on stream distance, not on residence time.",
    note="Storage, not destruction. Bed turnover can remobilize it.",
    sources=("drummond2022",),
)

MICROPLASTIC_CAPTURE_HELP = Help(
    title="Capture along a flow path",
    definition="Share of entering particles filtered out, given this run's path lengths.",
    method="Deep-bed filtration on path length. Capped at 97.7 percent.",
    note="A capability check, not a second estimate. Never added to reach retention.",
    sources=("munz2024",),
)

MICROPLASTIC_GATE_HELP = Help(
    title="Size exclusion",
    definition="Whether a particle can enter the pore network at all.",
    method="Particle size divided by median grain size.",
    rows_label="Thresholds",
    rows=(("Under 0.002", "straining negligible"),
          ("Up to 0.08", "enters the bed and is filtered"),
          ("Over 0.08", "excluded, deposits at the interface")),
    sources=("munz2024", "bradford2002"),
)

#: The three habitat headlines, in reading order: where exchange connects, how deep it goes, how
#: much space that comes to. All three are on the PORE-WATER basis, which is what keeps them
#: comparable with each other; the bulk-basis figures sit in the detail rows saying so.
HABITAT_COVERAGE_HELP = Help(
    title="Connected streambed coverage",
    definition="Share of the streambed where exchanged water enters the bed or returns from it.",
    method="Both sides counted, each cell once. Only paths that return to the stream qualify.",
    rows_label="Reported separately below",
    rows=(("Water entry", "framework A_active; what the site report card carries"),
          ("Water return", "usually the wider side on a gaining reach")),
    note="Regional groundwater reaching the bed is not exchanged water and does not count.",
    sources=("framework",),
)

HABITAT_DEPTH_HELP = Help(
    title="Equivalent pore-water depth",
    definition="Connected pore water spread evenly over the whole modeled streambed.",
    method="Pore-water volume divided by streambed area. A normalization, not a measured depth.",
    rows_label="Not to be read as",
    rows=(("A uniform layer", "the zone is nowhere this tidy"),
          ("Depth where water exchanges", "that is deeper; see More metrics")),
    note="Divide by coverage to get the depth over only the bed that exchanges.",
    sources=("framework",),
)

HABITAT_VOLUME_HELP = Help(
    title="Potential connected pore-water habitat volume",
    definition="Water-filled space inside the hydraulically connected hyporheic zone.",
    method="Modeled hyporheic volume times porosity. No kinetics and no rate anywhere here.",
    note="Potential space only, never habitat quality or occupancy.",
    sources=("framework",),
)

#: Why an entry-only coverage appears below a union headline: it is the framework's own metric and
#: the one the site report card carries, so it has to stay legible and named.
HABITAT_ENTRY_HELP = Help(
    title="Water entry streambed",
    definition="Bed area where exchanged water enters, excluding where it comes back out.",
    method="This is the framework's active streambed area, the basis used across sites.",
    note="The coverage headline above counts both sides, so it is the larger number.",
    sources=("framework",),
)

#: Why two depths and two volumes appear on one pane. Sits on the detail block, which is where a
#: reader who noticed the discrepancy goes looking.
HABITAT_BASIS_HELP = Help(
    title="Bulk sediment basis",
    definition="The same zone measured as sediment plus water, before porosity is applied.",
    method="Divide by streambed area for the framework's D_HZ, which the site report headlines.",
    note="Never mix the two bases in one comparison. Framework §4.6.",
    sources=("framework",),
)

#: The volume comes from the zone pass, not the flux pass whose count the other panes report.
HABITAT_RESOLUTION_HELP = Help(
    title="Zone particle resolution",
    definition="Particles released across the domain to map the zone's extent.",
    method="A separate release from the flux paths the other sections count.",
    note="Unclassified particles leave their cell's share resting on fewer samples.",
)

#: The tier card for the Advanced inputs panel of a section that supplies no input of its own.
#: Phrased like HYDRAULICS_HELP, which is the other "where did this come from" chip on the pane.
RUN_SETTINGS_HELP = Help(
    title="What the run was configured with",
    definition="The model settings that produced the numbers above, shown as they were used.",
    note="They belong to the run, so they are reported here rather than offered for editing.",
)

#: Why the one number on this pane that looks like a knob is not one. The chain is the whole
#: argument: a since-edited value cannot be applied by multiplying, because it would have changed
#: which particles came back at all.
HABITAT_POROSITY_HELP = Help(
    title="Porosity",
    definition="The fraction of the sediment that is water, as the hyporheic run tracked it.",
    method="It set pore velocity, so travel times, so which particles returned, so the volume.",
    note="Applying a new value needs the Hyporheic Zone calculations re-run, not the volume scaled.",
)

THERMAL_BUFFERING_HELP = Help(
    title="Daily temperature swing damped",
    definition="How much of the swing returning water carried in has been shed when it comes back.",
    method="Each path damped by its own residence time, then combined by flow.",
    rows_label="Range",
    rows=(("Sweeps", "the response time in effect"),
          ("Bounds", "sensitivity corners, not a confidence interval")),
    note="Opportunity only, never degrees of cooling.",
    sources=("marzadri2013",),
)

#: WHY THIS SITS BESIDE THE PERCENTAGE. A reach can damp every path completely and still exchange
#: almost nothing, and the percentage alone reads as a strong result either way (thermal plan
#: §5.2). This is the quantity, so the two cannot be read apart.
THERMAL_FLOW_HELP = Help(
    title="Buffered flow returned to the stream",
    definition="Returning exchange flow scaled by the share of its daily swing that was damped.",
    note="Attenuation-weighted return flow, never cooled flow.",
    sources=("marzadri2013",),
)

#: The closest thing this screen has to a lag, and the reason it is not called one.
THERMAL_DIEL_HELP = Help(
    title="Exchange held past a full day",
    definition="Share of returning flow that stays under longer than one daily cycle.",
    method="Fixed at 24 hours, so it does not move when the response time changes.",
    note="Storage opportunity, not a predicted delay in hours.",
    sources=("marzadri2013",),
)

#: Thermal's Damkohler is residence time over a HEAT response time, so the solute card's language
#: about rate constants does not apply to it.
THERMAL_REGIME_HELP = Help(
    title="What sets this answer",
    definition="Median residence time divided by the response time in force.",
    method="Far above one, damping is capped and only the amount of returning flow can move it.",
    note="A reading of the number above, not a site grade.",
    sources=("marzadri2013",),
)


_DENITRIFICATION = ProcessSpec(
    key="denitrification",
    display_label="Nutrient Cycling",
    # The label is the dimension; this is the process. Nitrogen is the only nutrient modeled and
    # denitrification is the only pathway, so the pane says so where the eye lands rather than
    # leaving it to the section tooltip, which is where it used to live alone.
    scope_note="Denitrification only: nitrate removal, not phosphorus or nutrient uptake.",
    kind=KIND_RESIDENCE_TIME,
    kinetics=KINETICS_FIRST_ORDER,
    oxygen_gated=True,
    # RC1 from the reference project, verified three ways: the cell-by-cell array holds 1.220000
    # across all 343,214 active cells, the listing echoes it per layer, and MT3DMS prints the
    # reaction stability limit as 0.8197 d, which is 1/1.22.
    rate=(0.61, 1.22, 2.44),
    rate_unit=RATE_FIRST_ORDER_PER_DAY,
    rate_label="Denitrification first-order rate constant",
    rate_citation="Reference GMS project, first-order rate 1.22 /day (half-life 13.6 h).",
    concentration_label="Stream nitrate",
    concentration_unit="mg/L as NO3-N",
    sources=("zarnetske2011", "hester2016", "lotts2022"),
    help=Help(
        title="Nutrient Cycling",
        definition="Nitrate removed by denitrification along returning hyporheic flow paths.",
        method="Oxygen is consumed first; nitrate then decays first-order past the anoxic gate.",
        rows_label="How paths combine",
        rows=(("Weights", "each path's flow, m3/day"),
              ("Efficiency", "flow-weighted mean, not a particle average"),
              ("Mass removed", "sum over every returning path")),
        sources=("zarnetske2011", "hester2016"),
    ),
    rate_help=Help(
        title="Denitrification rate constant",
        definition="First-order decay constant for nitrate, applied once the water has gone anoxic.",
        rows_label="Published values",
        rows=(("Base case", "6 /day"), ("Range", "0.6 to 36 /day")),
        default="default: 1.22 /day, half-life 13.6 h",
        note="The default is the reference gravel-bed project; the published range is riparian "
             "bank soil, which is far richer in carbon.",
        sources=("gms_rct", "hester2016"),
    ),
    concentration_help=Help(
        title="Stream nitrate",
        definition="Nitrate in the stream water entering the bed, as nitrogen.",
        # The saturation reasoning lives HERE, not in the warn card. The card is one sentence in a
        # narrow pane; the reader who wants to know why an upper bound comes here.
        method="Above 1.64 mg/L denitrification saturates, so first-order kinetics read high.",
        rows=(("Mixed land use", "0.5 to 3 mg/L"),
              ("Agricultural", "3 to 10.5 mg/L"),
              ("Drinking-water limit", "10 mg/L")),
        default="default: 1.0",
        note="Enter nitrogen, not the nitrate ion. Divide an as-NO3 number by 4.43.",
        sources=("hester2016", "usgs_c1350", "schilling2000"),
    ),
    ecosystem_context=(
        "Rate from a gravel-bed reference model; onset anchored on Drift Creek, Oregon, a forested "
        "gravel-bed stream studied with an isotopically labeled nitrate tracer."
    ),
    required_conditions=(
        "Denitrification needs suboxic water, available nitrate and labile organic carbon. Carbon "
        "is assumed non-limiting here, which will overstate removal where carbon is scarce."
    ),
    transferability_note=(
        "The reference rate is uncalibrated and was applied uniformly in its source project. It "
        "should not be transferred to a warmer, more nutrient-enriched or finer-sediment system "
        "without checking temperature, carbon supply and nitrate availability."
    ),
    # Headline: the mass, then the load reduction, then the per-km normalization. Mass leads
    # because it is what the report's decision framework sends the TMDL case to and what a manager
    # carries away; the efficiency alone says nothing about whether the reach matters at scale.
    # Per-area is the form published denitrification rates come in, so it keeps a row below.
    kpis=(
        PaneKpi(label="Estimated nitrate-N transformed", key="total_removed_kg_day",
                unit="kg N/day", low_key="total_removed_low_kg_day",
                high_key="total_removed_high_kg_day", help=NUTRIENT_MASS_HELP),
        PaneKpi(label="Concentration reduced", key="removal_efficiency", kind="pct",
                context_key="outlet_concentration_mg_l", context_fmt="{c_in} to {v} mg/L",
                help=NUTRIENT_REDUCTION_HELP),
        PaneKpi(label="Nitrate-N transformed per channel km", key="removal_per_km_kg_day",
                unit="kg N/day/km", low_key="removal_per_km_low_kg_day",
                high_key="removal_per_km_high_kg_day", help=NUTRIENT_PER_KM_HELP),
    ),
    pane_groups=(
        PaneGroup(title="Exchange", help=HYDRAULICS_HELP,
                  rows=(PaneRow("n_paths", "Returning flow paths", kind="int",
                                help=RETURNING_PATHS_HELP),
                        PaneRow("downwelling_cells", "Downwelling cells", kind="int",
                                help=DOWNWELLING_CELLS_HELP),
                        PaneRow("time_to_anoxia_hours", "Time to anoxia", unit=" h"),
                        PaneRow("fraction_above_threshold", "Exchange reaching anoxia",
                                kind="pct"))),
        # The areal rate takes the slot the total mass vacated when it went to the headline, so
        # this group keeps a row and with it the assumed-rate chip: both numbers move together
        # when the rate constant does, and that is the whole point of the tier.
        PaneGroup(title="Removal", help=ASSUMED_RATE_HELP, assumed_rate=True,
                  rows=(PaneRow("areal_removal_rate_g_m2_day",
                                "Removal per streambed area (g N/m²/day)",
                                help=NUTRIENT_AREAL_HELP),)),
    ),
    detail_rows=(
        PaneRow("total_removed_lb_day", "Removed (lb N/day)"),
        PaneRow("reference_area_m2", "Streambed area (m²)"),
        PaneRow("reach_length_m", "Reach length (m)"),
        PaneRow("reactive_exposure_m3", "Reactive exposure (m³)"),
        PaneRow("censored_flow_fraction", "Censored flow", kind="pct"),
        # The ratio itself, not just its two ingredients. The validity warning is a cliff at 1.0
        # and that is right, but a reader at 0.9 could not previously see it coming. The constant
        # rides the LABEL now that its own row is gone, interpolated rather than typed so the
        # denominator on the pane and the one in the arithmetic cannot drift apart.
        PaneRow("saturation_ratio",
                f"Nitrate vs {MONOD_HALF_SATURATION_MG_N_L:g} mg/L half-saturation"),
    ),
    detail_curve=PaneCurve("opportunity_curve", "Removal opportunity",
                           help=OPPORTUNITY_CURVE_HELP),
)

#: Tooltips for the shared oxygen inputs. They belong to denitrification but sit on the pane's
#: Advanced inputs accordion rather than on the spec's own rate, so they live here.
#:
#: The gate itself is a user choice. Switching it off is a defensible screening posture -- it asks
#: what the reach could do if carbon and redox never limited it -- so it gets a control rather than
#: being wired shut, and the three inputs below only exist while it is on.
OXYGEN_GATE_HELP = Help(
    title="Limit denitrification by dissolved oxygen",
    definition="Whether oxygen must be drawn down before nitrate removal starts on a flow path.",
    method="Off means removal begins the moment water enters the bed, which is an upper bound.",
    note="On by default. Turning it off removes the three inputs below and raises the estimate.",
    sources=("zarnetske2011",),
)

OXYGEN_HELP = Help(
    title="Stream dissolved oxygen",
    definition="Oxygen in the stream water entering the bed.",
    method="Sets where removal starts: time to anoxia is derived from it, never entered.",
    rows=(("Near saturation at 20 C", "about 9 mg/L"),
          ("Well-oxygenated riffle", "8 to 11 mg/L")),
    default="default: 9.0",
    sources=("trauth2017",),
)

OXYGEN_RATE_HELP = Help(
    title="Oxygen consumption",
    definition="Zero-order rate at which oxygen is drawn down along a flow path.",
    rows_label="Bounds",
    rows=(("Modeling parameter set", "15.3 mg/L/day"),
          ("Reproduces observed onset", "31.0 mg/L/day")),
    default="default: 23.2",
    note="Zero order is correct here, not a shortcut: at stream concentrations oxygen is the "
         "saturating substrate.",
    sources=("trauth2017", "zarnetske2011"),
)

#: The label says what the number DOES; the card keeps the literature term, so a reader who came
#: looking for "anoxic threshold" still lands on it.
ANOXIC_THRESHOLD_HELP = Help(
    title="Denitrification stops above",
    definition="The anoxic threshold: the oxygen level denitrification is assumed to begin below.",
    default="default: 0.1",
    note="Lowering it delays onset on every path and so reduces removal.",
)



_CONTAMINANT = ProcessSpec(
    key="contaminant",
    # ATTENUATION, NOT REMOVAL. The metals endpoints are sorption to newly forming manganese
    # oxides, which Fuller and Bargar observed reversing as pH fell; the organics transform. With
    # nitrate in its own section, nothing here is destruction (screening reference §7), so the
    # node cannot promise a word the pane spends its whole vocabulary avoiding.
    display_label="Dissolved Pollutants",
    # The label still reads as a category, though, and the model is ONE first-order sink per
    # endpoint, each screened on its own. `required_conditions` already says the rate lumps in
    # irreversible sorption, so what a reader cannot otherwise tell is that nothing desorbs and no
    # transformation product is tracked. Same construction as the nutrient line: what it is, then
    # what it is not.
    scope_note="Each endpoint on its own: irreversible first-order loss, not reversible sorption "
               "or daughter products.",
    kind=KIND_RESIDENCE_TIME,
    kinetics=KINETICS_FIRST_ORDER,
    oxygen_gated=False,
    # No rate ships. There is no defensible first-order attenuation library covering metals,
    # pesticides and industrial organics, and validate_registry() rejects an uncited rate. The
    # section is a calculator that becomes useful the moment the user has a literature value.
    rate=None,
    rate_unit=RATE_FIRST_ORDER_PER_DAY,
    rate_label="Attenuation rate",
    concentration_label="Stream concentration",
    concentration_unit="mg/L",
    no_source_note=(
        "No rate ships with this section. First-order attenuation is the standard screening form "
        "for a decaying solute; the rate must come from the user with a source, because published "
        "values span orders of magnitude across contaminants and sediment settings."
    ),
    ecosystem_context="Whatever setting the user's rate was measured in.",
    required_conditions=(
        "First-order attenuation assumes the contaminant degrades or sorbs irreversibly at a rate "
        "proportional to its concentration, and that the process is not substrate limited."
    ),
    transferability_note=(
        "Supply a rate measured in a comparable sediment, temperature and redox setting, and record "
        "its source. Unlike denitrification there is no oxygen gate here, so attenuation is assumed "
        "to begin as soon as water enters the bed."
    ),
    help=Help(
        title="Pollutant Attenuation",
        definition="Attenuation of a contaminant you name, with no redox gate.",
        method="First-order decay from the moment water enters the bed, flux weighted.",
        rows_label="How paths combine",
        # "Mass attenuated", not removed: this card rides the FIRST headline, so it renders over
        # a metal endpoint too, and §7 bans the word there. Denitrification keeps "removed" in
        # its own card, where nitrate really is converted to N2.
        rows=(("Weights", "each path's flow, m3/day"),
              ("Efficiency", "flow-weighted mean, not a particle average"),
              ("Mass attenuated", "sum over every returning path")),
    ),
    # BOTH CARDS WERE WRITTEN FOR THE CUSTOM-RATE ERA and outlived it. They said the unit was
    # always mg/L (it is the endpoint's own now, µg/L for the organics) and that no default ships
    # (every endpoint carries a cited rate and most a shipped concentration). Corrected 2026-08-01.
    concentration_help=Help(
        title="Stream concentration",
        definition="What your monitoring data reports for this endpoint, in the unit beside the "
                   "field.",
        default="each endpoint opens on its own published value",
        note="A shipped concentration is the study's, not your site's. Replace it with a measured "
             "one before quoting a mass.",
    ),
    rate_help=Help(
        title="Attenuation rate",
        definition="First-order decay constant, taken from the paper that measured this endpoint.",
        note="Read only. Every rate here is traceable to a citation, so the section offers no box "
             "to overwrite one with an unsourced number.",
        sources=("hype_pollutant_ref",),
    ),
    # THE SAME THREE HEADLINES AS DENITRIFICATION, key for key and in the same order. The two
    # sections ask one question of different solutes, so a reader moving between them reads the
    # same card; only the substance and its units change.
    #
    # NO SENSITIVITY RANGE on the two mass cards, which is the one place the parity stops. The
    # nutrient corners come from a published rate triple. No triple exists for an arbitrary
    # contaminant, so `_sensitivity_bounds` falls back to factor-of-two around whatever the user
    # typed -- a spread the app invented, which the card would then label "sensitivity range" as
    # though it had a source. The corners are still computed and still travel in the contract for
    # an API caller; they are just not shown as though they meant what the nutrient ones mean.
    #
    # Every label and unit here is a FALLBACK for the Custom case. With a cited endpoint selected
    # the `*_key` fields win, so a metal reads "Dissolved-phase attenuation" in kg/day and an
    # organic reads "Concentration reduction" in g/day, straight out of `pollutants.TERMS`.
    # MASS LEADS, matching Nutrient Cycling key for key. It became load-bearing when each endpoint
    # went behind its own expander and the panel header started carrying the lead number: on a
    # transport-limited reach every endpoint attenuates ~100%, so a collapsed list headed by the
    # efficiency read "Zinc 100% / Cobalt 100% / Nickel 100%" and discriminated nothing. The mass
    # is what differs between chemicals and what a manager carries away.
    kpis=(
        PaneKpi(label="Total attenuated", key="total_mass_display", unit="kg/day",
                label_key="mass_label", unit_key="total_mass_unit", help=POLLUTANT_MASS_HELP),
        PaneKpi(label="Concentration reduction", key="removal_efficiency", kind="pct",
                label_key="headline_label",
                context_key="outlet_concentration_display",
                # "returning water", never "stream": reference rule 5. The stream sees this
                # diluted by the exchange ratio, and that figure is a detail row below. `{u}` is
                # the endpoint's own unit, so the pair never reads mg/L under a µg/L field.
                context_fmt="{c_in} to {v} {u} in returning water",
                help=POLLUTANT_REDUCTION_HELP),
        PaneKpi(label="Attenuation per stream km", key="per_km_display", unit="kg/day/km",
                label_key="per_km_label", unit_key="per_km_unit",
                low_key="per_km_display_low", high_key="per_km_display_high",
                help=POLLUTANT_PER_KM_HELP),
    ),
    pane_groups=(
        # No "Exchange in contact" row: with no oxygen gate the onset is zero, so the exceedance
        # fraction is 1.0 by construction and read 100% on every run that ever produced a path.
        # Reactive exposure is the real rate-free number and takes its place.
        PaneGroup(title="Exchange", help=HYDRAULICS_HELP,
                  rows=(PaneRow("n_paths", "Returning flow paths", kind="int",
                                help=RETURNING_PATHS_HELP),
                        PaneRow("downwelling_cells", "Downwelling cells", kind="int",
                                help=DOWNWELLING_CELLS_HELP),
                        PaneRow("reactive_exposure_m3", "Reactive exposure (m³)"))),
        # The areal rate took the slot the total mass vacated when it went to the headline, so this
        # group keeps a row and with it the cited-rate chip. Per-area is the form published
        # attenuation rates come in, which is why it is the one that stays reported.
        PaneGroup(title="Attenuation", help=CITED_RATE_HELP, assumed_rate=True,
                  rows=(PaneRow("areal_rate_display", "Per streambed area",
                                label_key="areal_label", unit_key="areal_rate_unit",
                                help=POLLUTANT_AREAL_HELP),)),
    ),
    # `shared=True` means IDENTICAL FOR EVERY ENDPOINT: hydraulics, with no rate and no
    # concentration anywhere in the derivation. Those five are hoisted out of the per-endpoint
    # disclosures and shown once, because three ticked chemicals used to print the same path count,
    # the same median residence time and the same streambed area three times each.
    detail_rows=(
        # Reference §4.3-§4.4, rule 14. The regime VERDICT rides the card; its arithmetic and what
        # it implies for the reach live here, where a reader who wants to check it will look.
        PaneRow("damkohler", "Damkohler number", digits=2),
        PaneRow("t50_days", "Median residence time (days)", shared=True),
        PaneRow("processing_length_m", "Processing length (m)"),
        PaneRow("processing_length_reaches", "Processing length (reach lengths)", digits=2),
        # Significant figures, not one decimal: on a large river these are thousandths of a
        # percent, and rounding them to a flat "0" hides the whole point of the two rows.
        PaneRow("exchange_ratio", "Hyporheic return as a share of streamflow", kind="pct_sig",
                shared=True),
        PaneRow("reach_removal_fraction", "Reach-scale reduction", kind="pct_sig"),
        # Rule 5: the stream figure sits BESIDE the returning-water one on the card, so neither
        # can be mistaken for the other.
        PaneRow("stream_concentration_change_mg_l", "Stream concentration change (mg/L)"),
        PaneRow("total_removed_lb_day", "Total (lb/day)"),
        PaneRow("reference_area_m2", "Streambed area (m²)", shared=True),
        PaneRow("reach_length_m", "Reach length (m)", shared=True),
        PaneRow("censored_flow_fraction", "Censored flow", kind="pct", shared=True),
    ),
    # Needed here MORE than on nutrient, not less: no rate ships with this section, so until the
    # user enters one the sparkline draws with no marker at all -- a curve with nothing on it
    # locating this run. Behind the disclosure, with a label, that is honest rather than puzzling.
    detail_curve=PaneCurve("opportunity_curve", "Attenuation opportunity",
                           help=OPPORTUNITY_CURVE_HELP),
)


_HABITAT = ProcessSpec(
    key="habitat",
    display_label="Habitat Creation",
    # "Habitat Creation" is the widest-reading label of the four sections: it names an outcome the
    # hydraulics cannot establish and implies something was made. What the model maps is connected
    # pore water. Same construction as the nutrient line -- what it is, then what it is not.
    scope_note="Potential space only: connected pore water, not habitat quality or use.",
    kind=KIND_EXTENT,
    kinetics=KINETICS_NONE,
    sources=("framework", "boulton1998"),
    ecosystem_context="Any stream where the modeled domain resolves the exchanging streambed.",
    required_conditions=(
        "Pore-space accessibility, grain size, fine-sediment clogging, dissolved oxygen, food "
        "supply and temperature all constrain whether modeled volume is usable habitat. None of "
        "them are represented here."
    ),
    transferability_note=(
        "This is hydraulically connected subsurface space, not habitat quality and not occupancy. "
        "A groundwater model cannot resolve pore-throat size, clogging or food availability, so "
        "treat the volume as an upper bound on what could be habitable."
    ),
    help=Help(
        title="Habitat Creation",
        definition="Physical extent of the hydraulically connected hyporheic zone.",
        method="Read from modeled volume, streambed area and depth. No kinetics, no rates.",
        rows_label="Volume basis",
        rows=(("Pore water", "headline; the space an organism could occupy"),
              ("Bulk sediment", "reported alongside, never mixed with it")),
        note="Potential space only, never habitat quality or occupancy.",
        sources=("framework",),
    ),
    # No assumed-rate group at all. That absence is the statement: nothing in this section rests
    # on a rate constant, and it falls out of the data rather than an `if`.
    #
    # Three headlines in reading order -- where exchange connects, how deep, how much -- which is
    # framework §7.6's active-capacity card: its D_HZ headline plus the two supporting values it
    # names. All three on the PORE-WATER basis, so nothing on the visible card needs a basis
    # qualifier to be read correctly. The bulk-basis pair moved down to `detail_rows`, labelled.
    kpis=(
        PaneKpi(label="Connected streambed coverage", key="connected_streambed_fraction",
                kind="pct", help=HABITAT_COVERAGE_HELP),
        PaneKpi(label="Equivalent pore-water depth", key="pore_equivalent_depth_m", unit="m",
                help=HABITAT_DEPTH_HELP),
        # "Potential" and "habitat" both belong where the eye lands rather than only in the
        # tooltip: this is the card a reader quotes, and the framework's §13.2 caution is that
        # hydraulics establish potential access, never habitat itself.
        PaneKpi(label="Potential connected pore-water habitat volume",
                key="habitable_pore_volume_m3", unit="m³", help=HABITAT_VOLUME_HELP),
    ),
    # Coverage's two ingredients, and nothing else: the group exists so the pane keeps its
    # `hydraulics` tier chip, which is what says none of this rests on a rate constant.
    pane_groups=(
        PaneGroup(title="Connected subsurface space", help=HYDRAULICS_HELP,
                  rows=(PaneRow("connected_streambed_area_m2", "Connected streambed (m²)"),
                        PaneRow("streambed_area_m2", "Streambed area (m²)"))),
    ),
    # SIX ROWS, down from thirteen. Five moved to `run_settings` below, where a reader asking what
    # the model was configured with will look for them, and two were cut outright: the entry-only
    # COVERAGE (both areas and the bed total are already listed here in m2, and the site report
    # publishes the percentage) and the bulk-basis equivalent depth (HABITAT_BASIS_HELP on the row
    # above says to divide by streambed area for D_HZ, and the report's Extent card headlines it).
    # Both still compute, still ship on the contract and still print in the report. Only the pane
    # stopped listing them.
    detail_rows=(
        # The connected-basis depth sits FIRST, directly under the headline that normalizes over
        # the whole bed, because it is the number a reader reaches for on seeing coverage below
        # 100%. depth x coverage == the headline depth, an identity anyone can check here.
        PaneRow("pore_depth_active_m", "Depth where exchange occurs (m)"),
        # The two sides of the coverage headline, split. Their RATIO is the informative part:
        # entry much smaller than return means focused recharge with diffuse discharge, which a
        # single union number hides.
        PaneRow("active_streambed_area_m2", "Water entry streambed (m²)",
                help=HABITAT_ENTRY_HELP),
        PaneRow("return_streambed_area_m2", "Water return streambed (m²)"),
        PaneRow("path_depth_p50_m", "Median maximum path depth (m)"),
        PaneRow("path_depth_p90_m", "P90 maximum path depth (m)"),
        PaneRow("bulk_volume_m3", "Bulk sediment volume (m³)", help=HABITAT_BASIS_HELP),
    ),
    # This section supplies nothing, but it still RAN under settings, and those settings are what
    # a reader questioning the numbers reaches for. Porosity gets a `label_key` so the result can
    # say "assumed" when no porosity was recorded for the run: two of the three headlines scale
    # linearly with it, and a row reading "as run" over a fallback value is a false provenance
    # claim rather than a rounding one.
    run_settings=(
        PaneRow("porosity", "Porosity (as run)", digits=2, label_key="porosity_label",
                help=HABITAT_POROSITY_HELP),
        PaneRow("zone_particles_per_cell", "Zone particles per cell", kind="int",
                help=HABITAT_RESOLUTION_HELP),
        PaneRow("zone_seeds", "Zone particles released", kind="int"),
        PaneRow("zone_cells_seeded", "Zone cells seeded", kind="int"),
        PaneRow("zone_classified_fraction", "Zone particles classified", kind="pct"),
    ),
    run_settings_note=("Porosity is set under Subsurface properties, particle density on the "
                       "Hyporheic Zone pane. Both are model inputs, so a change applies only "
                       "once the Hyporheic Zone calculations are re-run."),
)


_MICROPLASTIC = ProcessSpec(
    key="microplastic",
    display_label="Microplastic Retention",
    # Reference §7 is categorical: retention, storage or delayed transport, never degradation or
    # removal. Nothing here destroys anything, and bed turnover can give it back.
    # The second sentence used to live in a per-mechanism signature block that this pane no longer
    # draws, and it is the one thing on the pane a reader cannot infer: Drummond's coefficient is a
    # cross-class average over stream DISTANCE, so this site's turnover and residence time do not
    # move the reach-scale number at all. Only the capture check reads the modeled hydraulics, and
    # it reads how far a path travels, never how long it takes.
    scope_note="Retention only: particles are stored in the bed, not degraded, and can be "
               "remobilized. The reach-scale number is an average over stream distance, so this "
               "site's turnover and residence time do not change it.",
    kind=KIND_PARTICULATE,
    kinetics=KINETICS_NONE,
    sources=("drummond2022", "drummond2020", "munz2024", "hype_pollutant_ref"),
    ecosystem_context=("Modelled across stream classes from headwaters to mainstems; filter "
                       "coefficients from saturated polystyrene column experiments."),
    required_conditions=(
        "Deep-bed filtration assumes particles small enough to enter the pore network, saturated "
        "flow, and a bed that is not already clogged. Remobilization by bed turnover is not "
        "represented at all, which is the largest gap in this module."
    ),
    transferability_note=(
        "Reach-scale retention is an empirical cross-class average and is insensitive to site "
        "conditions: an armoured bed and a loose sand bed return the same number. The filter "
        "coefficients were measured on polystyrene in organic-free, narrowly graded sediment, so "
        "natural beds with biofilm, organic content and wide grading will differ."
    ),
    help=Help(
        title="Microplastic Retention",
        definition="Share of passing microplastic stored in the bed by hyporheic exchange.",
        method="Distance-based filtration, never a per-day decay. Retention profiles do not "
               "depend on elapsed time.",
        rows_label="Two independent readings",
        rows=(("Reach scale", "the reported number, on stream distance"),
              ("Flow path", "a capability check, on path length")),
        note="Never added together. The gap between them is remobilization, which is unmodeled.",
        sources=("drummond2022", "munz2024"),
    ),
    kpis=(
        PaneKpi(label="Reach-scale retention", key="retained_fraction", kind="pct",
                low_key="retained_fraction_low", high_key="retained_fraction_high",
                help=MICROPLASTIC_RETENTION_HELP),
    ),
    pane_groups=(
        PaneGroup(title="Reach", help=HYDRAULICS_HELP,
                  rows=(PaneRow("reach_length_m", "Reach length (m)"),
                        PaneRow("alpha_mp_per_km", "Retention coefficient (1/km)", digits=3))),
        # Tier B. Deliberately a SEPARATE group with its own title, so nothing invites a reader to
        # add it to the number above (reference rule 11).
        PaneGroup(title="Can this bed capture it", help=MICROPLASTIC_CAPTURE_HELP,
                  rows=(PaneRow("size_ratio", "Particle to grain size ratio", digits=3,
                                help=MICROPLASTIC_GATE_HELP),
                        PaneRow("path_capture_fraction", "Capture along a flow path", kind="pct"),
                        PaneRow("n_paths", "Flow paths measured", kind="int"))),
    ),
    detail_rows=(
        PaneRow("path_length_p50_m", "Median flow path length (m)"),
        PaneRow("lambda_f_per_cm", "Filter coefficient (1/cm)", digits=2),
        PaneRow("capture_cap", "Capture cap", kind="pct"),
        PaneRow("path_capture_low", "Capture, weakest filtering", kind="pct"),
        PaneRow("path_capture_high", "Capture, strongest filtering", kind="pct"),
        PaneRow("particle_size_um", "Particle size (µm)", digits=4),
        PaneRow("median_grain_size_mm", "Median grain size (mm)", digits=3),
    ),
)


#: The thermal section's second table. Not tied to an input, so it lives beside the spec.
#: Residence-time classes for the thermal section's second table (thermal plan §5.6).
#:
#: FIXED HOURS, not multiples of the response time. The names refer to the DIEL cycle -- a path
#: under 4 h is still coupled to the day's temperature swing however fast the bed damps -- so
#: these boundaries do not move when the response time is changed. That also means the plan's
#: Da_T column (0.5 / 1 / 2) holds only at the 8 h default, which is why it is not shown.
#: Declared ABOVE _THERMAL because that spec's band group carries THERMAL_BANDS_HELP.
THERMAL_BANDS = (("Diel-coupled", 0.0, 4.0), ("Transitional", 4.0, 8.0),
                 ("Buffered", 8.0, 16.0), ("Strong buffering", 16.0, math.inf))


def _band_range(lo_h: float, hi_h: float) -> str:
    if not math.isfinite(hi_h):
        return f"{lo_h:g} h and over"
    return f"under {hi_h:g} h" if lo_h <= 0 else f"{lo_h:g} to {hi_h:g} h"


THERMAL_BANDS_HELP = Help(
    title="Response bands",
    definition="Share of returning exchange falling in each residence-time class.",
    # Generated from THERMAL_BANDS itself, so the legend cannot drift from the calculation.
    rows_label="Class boundaries",
    rows=tuple((label, _band_range(lo, hi)) for label, lo, hi in THERMAL_BANDS),
    note="Fixed hour ranges, not multiples of the response time. Mathematical classes, never "
         "quality classes.",
)


_THERMAL = ProcessSpec(
    key="thermal_regulation",
    display_label="Temperature Regulation",
    kind=KIND_RESIDENCE_TIME,
    kinetics=KINETICS_RELAXATION,
    oxygen_gated=False,
    # Thermal response time, not a decay rate: the timescale over which a diel temperature anomaly
    # is damped as water travels through the bed. 8 h reference with factor-of-two bounds.
    rate=(4.0, 8.0, 16.0),
    rate_unit=RATE_TIMESCALE_H,
    rate_label="Thermal response time",
    # The thermal plan's §4.2 scenario table, which is the whole set of published cases. Nobody
    # measures this at a site, so the pane offers these three rather than a box, and pinning them
    # to `rate` above keeps the choices and the sensitivity corners the same three numbers.
    rate_scenarios=((4.0, "Fast"), (8.0, "Reference"), (16.0, "Slow")),
    rate_citation=("Marzadri et al. (2013) gravel-bed reference, 8 h. The 4 and 16 h cases are "
                   "factor-of-two sensitivity bounds, not a confidence interval."),
    # Heat exchanges with the solid matrix, so the thermal front lags the water by roughly
    # (rho c)_bulk / (n (rho c)_water), about 2 for saturated sand. Explicit rather than hidden.
    retardation=2.0,
    sources=("marzadri2013", "fogg2023"),
    # "Temperature Regulation" invites cooling, and cooling is the one thing thermal plan §10.1
    # rules out: without the surface energy budget there are no degrees to report. Naming the
    # exclusion where the eye lands, rather than leaving it to a tooltip.
    scope_note="Buffering only: how much of the daily swing is damped, not degrees of cooling.",
    ecosystem_context="Gravel-bed streams; the reference model was validated on Bear Valley Creek, Idaho.",
    required_conditions=(
        "Damping of a diel temperature signal requires sustained water-sediment contact. Actual "
        "stream temperature is set mainly by the surface energy budget, which this does not model."
    ),
    transferability_note=(
        "This reports buffering OPPORTUNITY, never degrees of cooling and never a reach temperature "
        "change. Fogg et al. (2023) compare shade against hyporheic exchange as competing controls; "
        "without the surface energy budget, hyporheic influence cannot be converted to a "
        "temperature."
    ),
    help=Help(
        title="Temperature Regulation",
        # Not "the share that stays under long enough": that is an exceedance, and B_Q is a
        # continuous mean of 1 - exp(-t/tau) over every path. The section leads with three numbers
        # now, and the quantity is half the answer.
        definition="How much of a daily temperature swing returning exchange has shed, and how "
                   "much water that is.",
        method="Each path scored on its own residence time, then combined by flow.",
        rows_label="How paths combine",
        rows=(("Weights", "each path's flow, m3/day"),
              ("Damped share", "flow-weighted mean, not a particle average")),
        note="Reports opportunity only, never degrees of cooling.",
        sources=("marzadri2013",),
    ),
    regime_help=THERMAL_REGIME_HELP,
    # ROWS ARE INDEXED ON RESIDENCE TIME, not on the response time, and the label has to say so.
    # Thermal plan §4.3 tabulates 1 - exp(-t/tau) at the 8 h reference, so "4 h" here means water
    # that stayed four hours. Reading them as response times inverts the relationship: a SHORTER
    # response time damps MORE, not less.
    rate_help=Help(
        title="Thermal response time",
        definition="Time a parcel takes to shed about 63% of the temperature swing it carried in.",
        method="Reference 8 h is the primary case. Fast 4 h and slow 16 h are sensitivity bounds.",
        rows_label="Damping at the 8 h reference",
        rows=(("Held 4 h", "39% damped"), ("Held 8 h", "63%"),
              ("Held 16 h", "87%"), ("Held 24 h", "95%")),
        default="default: reference, 8 h",
        note="No published bed-type mapping. A site value needs temperature loggers.",
        sources=("marzadri2013",),
    ),
    # QUALITY, THEN QUANTITY, THEN PERSISTENCE -- the first three of thermal plan §8's four result
    # groups. The damped share leads because it is what the response time actually computes, but on
    # its own it saturates: a reach whose paths all run for weeks reads 100% however little water
    # comes back, so §5.2's flow sits beside it and is what tells the two sites apart.
    kpis=(
        PaneKpi(label="Daily temperature swing damped", key="buffering_opportunity", kind="pct",
                low_key="buffering_opportunity_low", high_key="buffering_opportunity_high",
                help=THERMAL_BUFFERING_HELP),
        PaneKpi(label="Buffered flow returned to the stream", key="attenuation_weighted_flow_l_s",
                unit="L/s", help=THERMAL_FLOW_HELP),
        PaneKpi(label="Exchange held past a full day", key="fraction_above_diel", kind="pct",
                help=THERMAL_DIEL_HELP),
    ),
    pane_groups=(
        # Tagged assumed rate even though n_paths is tau-invariant: the two persistence fractions
        # are measured in response times, so the block as a whole moves with tau. Over-marking is
        # safe here, under-marking is not.
        PaneGroup(title="Exchange", help=ASSUMED_RATE_HELP, assumed_rate=True,
                  rows=(PaneRow("n_paths", "Returning flow paths", kind="int",
                                help=RETURNING_PATHS_HELP),
                        PaneRow("downwelling_cells", "Downwelling cells", kind="int",
                                help=DOWNWELLING_CELLS_HELP),
                        # Three response times is 12 h under the fast scenario and 48 h under the
                        # slow one, so the row moved with the setting while reading like a fixed
                        # fact. The full-day headline is the version that holds still.
                        PaneRow("fraction_above_1tau", "Exchange past one response time",
                                kind="pct"))),
        # The bands ARE the section's rate-free artifact: fixed hour ranges that do not move when
        # the response time changes, which is what HYDRAULICS_HELP claims and this makes visible.
        PaneGroup(title="Response bands", help=THERMAL_BANDS_HELP, list_key="response_bands"),
    ),
    # The flow is a headline now, and the remaining anomaly was exactly 100% minus the headline
    # above it. Both still reach the reader in the site report. What is left is the arithmetic
    # behind the regime line and the storage pair, which are the only rows here that say something
    # no card above already does.
    detail_rows=(
        PaneRow("thermal_damkohler_median", "Median Damkohler"),
        PaneRow("rtd_storage_m3", "Mobile storage (m³)"),
        PaneRow("attenuation_weighted_storage_m3", "Buffered storage (m³)"),
        PaneRow("censored_flow_fraction", "Censored flow", kind="pct"),
    ),
)

#: MICROPLASTICS IS UNREGISTERED, NOT DELETED (2026-08-01, user call: "remove it for now").
#:
#: `_MICROPLASTIC` above, `screen_particulate` and its constants, `MicroplasticRetention` and
#: `TestParticulateModule` all stay exactly as they were -- they are pure, tested and coupled to
#: nothing. What went is its reachability, and it went from exactly three places: this dict,
#: `SECTION_ORDER` below, and `_F_POLLUTANT.processes`/`mechanisms`. Re-add it to those three plus
#: the two `fn.scr.pol.*` tree nodes and the pane comes back with NO app.py edit, because
#: `FN_NODE_PROCESS`, `FN_GROUP_NODES`, `PANE_FOR_NODE` and `PREREQS` are all derived from here.
#:
#: There is no third state. `validate_functions` rejects a process no function claims, and
#: `pane_node` would name a tree node that does not exist, so a calculator cannot sit here computing
#: quietly with no way in -- which is the invariant that keeps the interface honest about what ran.
PROCESSES: dict[str, ProcessSpec] = {
    p.key: p for p in (_DENITRIFICATION, _CONTAMINANT, _HABITAT, _THERMAL)
}

#: Display order for the Screening sections.
SECTION_ORDER = ("denitrification", "contaminant", "habitat", "thermal_regulation")


def process_keys() -> tuple[str, ...]:
    return SECTION_ORDER


def get_process(key: str) -> ProcessSpec:
    try:
        return PROCESSES[key]
    except KeyError:
        raise KeyError(f"unknown process {key!r}; known: {', '.join(PROCESSES)}") from None


def visible_rows(spec: ProcessSpec) -> tuple[PaneRow, ...]:
    """Every row the pane paints ABOVE the disclosure, never `detail_rows`.

    One definition, used by the pane factory, by validate_registry and by the tests, so the three
    cannot disagree about what a user actually sees without clicking."""
    return tuple(r for g in spec.pane_groups for r in g.rows)


def validate_registry(processes: dict[str, ProcessSpec] | None = None) -> None:
    """Structural invariants, run at import so a malformed entry cannot ship."""
    procs = PROCESSES if processes is None else processes
    for key, p in procs.items():
        where = f"process {key!r}"
        if p.key != key:
            raise ValueError(f"{where}: key mismatch ({p.key!r})")
        if p.kind not in KINDS:
            raise ValueError(f"{where}: kind {p.kind!r} not in {KINDS}")
        if p.kinetics not in KINETICS:
            raise ValueError(f"{where}: kinetics {p.kinetics!r} not in {KINETICS}")
        unknown = [k for k in p.sources if k not in SOURCES]
        if unknown:
            raise ValueError(f"{where}: unresolved sources {sorted(unknown)}")
        if not p.citation.strip():
            raise ValueError(f"{where}: needs `sources`, or `no_source_note` when there is "
                             f"genuinely nothing to cite (framework §14.2)")
        if not p.transferability_note.strip():
            raise ValueError(f"{where}: transferability_note is required (framework §14.2)")
        if not p.help.definition.strip():
            raise ValueError(f"{where}: help.definition is required; it replaces pane prose")
        for label, h in (("help", p.help), ("rate_help", p.rate_help),
                         ("concentration_help", p.concentration_help)):
            validate_help(h, f"{where}.{label}")
        if p.regime_help is not None:
            validate_help(p.regime_help, f"{where}.regime_help")
        # A scenario picker offers the reader a closed set, and the reported range is a sweep of
        # `rate`. Let the two lists diverge and the pane hands out a case its own range never
        # brackets, which is the one thing the range exists to prevent.
        if p.rate_scenarios:
            if p.rate is None:
                raise ValueError(f"{where}: rate_scenarios without a rate triple to pin them to")
            if tuple(v for v, _ in p.rate_scenarios) != tuple(float(v) for v in p.rate):
                raise ValueError(f"{where}: rate_scenarios values {[v for v, _ in p.rate_scenarios]}"
                                 f" must be the rate triple {list(p.rate)}, or the pane offers a "
                                 f"case the sensitivity range never brackets")
            if not all(str(lbl).strip() for _, lbl in p.rate_scenarios):
                raise ValueError(f"{where}: every rate scenario needs a name")
        # A read-only table under a panel named "Advanced inputs" has to say where the settings
        # ARE changed, or the name promises an edit the panel does not offer.
        if bool(p.run_settings) != bool(p.run_settings_note.strip()):
            raise ValueError(f"{where}: run_settings and run_settings_note go together. A "
                             f"read-only table needs a line saying where those settings are set")
        # ...and a row may appear in exactly one place. Moving rows between the disclosure panels
        # is how a value ends up listed twice, and two copies of one number reads as two numbers.
        # `kpis` is in the sweep because PROMOTING a row to a headline is the version of this
        # mistake that looks like success: the card renders, and the row it was lifted from goes on
        # printing the same number two panels down.
        placed: dict[str, str] = {}
        for slot, rows in (("kpis", list(p.kpis)),
                           ("pane_groups", [r for g in p.pane_groups for r in g.rows]),
                           ("detail_rows", list(p.detail_rows)),
                           ("run_settings", list(p.run_settings))):
            for r in rows:
                if r.key in placed:
                    raise ValueError(f"{where}: row {r.key!r} is in both {placed[r.key]} and "
                                     f"{slot}; it would render twice on the pane")
                placed[r.key] = slot
        if p.kind == KIND_EXTENT:
            if p.kinetics != KINETICS_NONE:
                raise ValueError(f"{where}: an extent process cannot carry kinetics")
            if p.rate is not None or p.oxygen_gated:
                raise ValueError(f"{where}: an extent process cannot carry a rate or oxygen gate")
        # Reference rule 1, enforced structurally rather than by review. A particulate endpoint is
        # driven by DISTANCE; a per-day coefficient reaching it would be the exact category error
        # the two-module split exists to prevent, and Munz et al. showed empirically that time is
        # the wrong variable.
        if p.kind == KIND_PARTICULATE:
            if p.kinetics != KINETICS_NONE:
                raise ValueError(f"{where}: a particulate process cannot carry kinetics")
            if p.rate is not None or p.rate_unit or p.oxygen_gated:
                raise ValueError(f"{where}: a particulate process is distance-driven and must "
                                 f"carry no rate, no rate unit and no oxygen gate (reference "
                                 f"rule 1)")
            if p.detail_curve is not None:
                raise ValueError(f"{where}: the opportunity curve sweeps a reaction timescale, "
                                 f"which has no meaning for a distance-driven endpoint")
        if p.rate is not None:
            if len(p.rate) != 3:
                raise ValueError(f"{where}: rate must be (low, central, high)")
            lo, mid, hi = (float(x) for x in p.rate)
            if not (lo <= mid <= hi):
                raise ValueError(f"{where}: rate must be non-decreasing, got {(lo, mid, hi)}")
            if lo <= 0:
                raise ValueError(f"{where}: rate must be positive")
            if not p.rate_unit:
                raise ValueError(f"{where}: rate_unit is required when a rate ships")
            # A rate that ships without provenance is exactly the failure this registry prevents.
            if not (p.rate_citation or "").strip():
                raise ValueError(f"{where}: rate_citation is required when a rate ships")
            # Provenance says where the default came from; the rate card says what else is
            # defensible. A user who cannot see the published spread cannot judge the default.
            if not p.rate_help.rows:
                raise ValueError(f"{where}: rate_help needs published values when a rate ships")
        if p.retardation <= 0:
            raise ValueError(f"{where}: retardation must be positive")

        # ---- pane layout -------------------------------------------------------------
        if p.pane_groups and not p.kpis:
            raise ValueError(f"{where}: a pane with groups must name at least one headline")
        for r in (*visible_rows(p), *p.detail_rows, *p.run_settings):
            if r.kind not in ROW_KINDS:
                raise ValueError(f"{where}: row {r.key!r} kind {r.kind!r} not in {ROW_KINDS}")
            if not r.label.strip():
                raise ValueError(f"{where}: row {r.key!r} needs a label")
            if r.help is not None:
                validate_help(r.help, f"{where}.row[{r.key}]")
        for kpi in p.kpis:
            if bool(kpi.low_key) != bool(kpi.high_key):
                raise ValueError(f"{where}: headline {kpi.key!r} bounds must be set as a pair")
            if bool(kpi.context_key) != bool(kpi.context_fmt):
                raise ValueError(f"{where}: headline {kpi.key!r} context needs a key and a format")
            if kpi.kind not in ROW_KINDS:
                raise ValueError(f"{where}: headline {kpi.key!r} kind {kpi.kind!r} unknown")
            if kpi.help is not None:
                validate_help(kpi.help, f"{where}.kpi[{kpi.key}]")
        for g in p.pane_groups:
            if not g.title.strip():
                raise ValueError(f"{where}: every pane group needs a title")
            if bool(g.rows) == bool(g.list_key):
                raise ValueError(f"{where}: group {g.title!r} needs rows OR a list_key, not both")
            if g.help is not None:
                validate_help(g.help, f"{where}.group[{g.title}]")
        if sum(1 for g in p.pane_groups if g.list_key) > 1:
            raise ValueError(f"{where}: at most one list-driven group per pane")
        # A chart with no name and no explanation is what demoting this one was meant to end, and
        # nothing validated the old `curve_key` at all -- a typo just dropped the chart in silence.
        if p.detail_curve is not None:
            c = p.detail_curve
            if not c.key.strip() or not c.label.strip():
                raise ValueError(f"{where}: detail_curve needs both a key and a label")
            if c.help is None:
                raise ValueError(f"{where}: detail_curve {c.label!r} needs a help card; an "
                                 f"unlabelled chart is what the disclosure move exists to stop")
            validate_help(c.help, f"{where}.curve[{c.label}]")
        # Someone reading the pane asked whether the numbers were an average over paths. They are
        # flow weighted, so every residence-time section must show the count WHERE IT IS SEEN --
        # a count swept into the disclosure does not answer the question. Enforced here rather
        # than by counting a string in app.py, so it fails at import and names the offender.
        if p.kind == KIND_RESIDENCE_TIME and p.pane_groups:
            if not any(r.key == "n_paths" for r in visible_rows(p)):
                raise ValueError(f"{where}: a residence-time pane must show the path count above "
                                 f"the disclosure, not in detail_rows (framework §4.5)")
    if processes is None and set(SECTION_ORDER) != set(PROCESSES):
        raise ValueError("SECTION_ORDER must cover exactly the registered processes")
    if processes is None:
        validate_functions()


# =========================================================================== the function layer
# THERE ARE FOUR HYPORHEIC FUNCTIONS: nutrient cycling, pollutant attenuation, habitat creation,
# and temperature regulation. There are FIVE calculators, and the two stopped being one-to-one when
# microplastic retention arrived: retention is a MECHANISM of pollutant attenuation, physical
# rather than chemical, not a fifth function.
#
# A function is what a manager asks about. A process is what the app can calculate. Keeping the
# layers separate is what lets the panes and the report reorganise around the four without a single
# calculator moving, and `validate_functions` makes the mapping total, so a sixth calculator cannot
# ship without someone deciding which function hosts it.


@dataclass(frozen=True)
class Mechanism:
    """One way a function can act, when a function hosts more than one calculator.

    Pollutant Attenuation is the only one. Dissolved-phase loss is first order in TIME and
    microplastic retention is empirical in DISTANCE; they are one function because a reader looking
    for one is looking for the other, and two calculators because Munz et al. measured that time is
    the wrong independent variable for the second.

    Each mechanism owns a TREE NODE. They shared one node behind a radio until the tree took the
    job over: switching mechanism is a navigation, and the tree is where this app navigates."""

    key: str                        # stable id, used in saved projects and report section keys
    label: str
    process: str                    # registry process key
    node_id: str = ""               # required when the function hosts more than one process
    note: str = ""                  # one line: what makes this mechanism different
    headline_kpi: str = ""          # which ProcessSpec.kpis entry rides above the fold
    assumption: PaneRow | None = None


@dataclass(frozen=True)
class FunctionSpec:
    """One hyporheic function. There are four; see the banner above."""

    key: str
    display_label: str
    node_id: str
    processes: tuple[str, ...]
    limits: tuple[str, ...]                 # what the estimate cannot tell you
    headline_kpi: str = ""                  # "" = ProcessSpec.kpis[0]
    assumption: PaneRow | None = None       # the one parameter the estimate rests on
    mechanisms: tuple[Mechanism, ...] = ()
    help: Help = field(default_factory=Help)

    @property
    def primary_process(self) -> str:
        return self.processes[0]

    def mechanism(self, key: str | None) -> Mechanism | None:
        """The selected mechanism, or None for a single-calculator function."""
        if not self.mechanisms:
            return None
        for mech in self.mechanisms:
            if mech.key == key:
                return mech
        return self.mechanisms[0]

    def mechanism_for_process(self, process_key: str) -> Mechanism | None:
        """The mechanism that owns a calculator, or None for a single-calculator function."""
        for mech in self.mechanisms:
            if mech.process == process_key:
                return mech
        return None

    def headline(self, mech: Mechanism | None = None) -> str:
        return (mech.headline_kpi if (mech is not None and mech.headline_kpi)
                else self.headline_kpi)

    def rests_on(self, mech: Mechanism | None = None) -> PaneRow | None:
        return mech.assumption if (mech is not None and mech.assumption) else self.assumption


def pane_node(process_key: str) -> str:
    """The tree node whose pane hosts a calculator.

    A mechanism names its own node; every other calculator uses its function's. One definition, so
    app.py and the tests cannot disagree about which node draws which pane."""
    fspec = function_for_process(process_key)
    mech = fspec.mechanism_for_process(process_key)
    return mech.node_id if (mech is not None and mech.node_id) else fspec.node_id


_F_NUTRIENT = FunctionSpec(
    key="nutrient",
    display_label="Nutrient Cycling",
    node_id="fn.scr.nut",
    processes=("denitrification",),
    headline_kpi="total_removed_kg_day",
    assumption=PaneRow("rate_value", "Denitrification rate", unit=" /day"),
    limits=("Carbon supply, temperature and microbial community are not modeled.",
            "Nitrate can be produced as well as removed; only removal is estimated.",
            "The rate comes from other streams, not from measurements here."),
    help=Help(
        title="Nutrient Cycling",
        definition="Nitrate removed along returning hyporheic paths, once the water goes anoxic.",
        method="Oxygen drawdown sets the onset, then first-order removal over the remaining time.",
        note="A screening estimate under stated assumptions, not a measured rate.",
        sources=("zarnetske2011", "hester2016", "lotts2022")),
)

_F_POLLUTANT = FunctionSpec(
    key="pollutant",
    display_label="Pollutant Attenuation",
    node_id="fn.scr.pol",
    processes=("contaminant",),
    # Moved up off the dissolved Mechanism when microplastics left and the function stopped needing
    # one. `mechanisms` is now empty, which `validate_functions` requires for a single-process
    # function: `bool(mechanisms) == (len(processes) > 1)`.
    #
    # The MASS leads, the same choice Nutrient Cycling makes and for the same reason -- see the
    # KPI list. It matters more here: each endpoint's panel header carries this number, and on a
    # transport-limited reach the efficiency is 100% for every chemical in the list.
    headline_kpi="total_mass_display",
    assumption=PaneRow("rate_value", "Attenuation rate", unit=" /day"),
    limits=("Attenuation is not destruction; sorbed and stored mass can return.",
            "Sediment chemistry, pH and redox are not modeled.",
            "Rates and coefficients come from other systems, not from measurements here."),
    help=Help(
        title="Pollutant Attenuation",
        definition="Contaminant taken out of the water column by the modeled hyporheic exchange.",
        method="First-order loss over each returning path's travel time, from a cited rate.",
        note="Attenuation, not destruction. Stored mass can be remobilized.",
        sources=("hype_pollutant_ref",)),
)

_F_HABITAT = FunctionSpec(
    key="habitat",
    display_label="Habitat Creation",
    node_id="fn.scr.hab",
    processes=("habitat",),
    headline_kpi="connected_streambed_fraction",
    # No `assumption`: this section rests on no rate constant, which is why its estimate block
    # carries the hydraulics chip rather than the assumed-rate one. It falls out of the data.
    # The surrogate bullet leads because it frames the other three: they qualify a measurement,
    # and this one says the measurement is standing in for the thing the section is named after.
    limits=("This reports potential habitat space as a surrogate for habitat creation.",
            "Grain size, clogging, oxygen, food supply and temperature are not modeled.",
            "This is potential space, never habitat quality and never occupancy.",
            "No suitability index is applied. A generic curve would imply an unsupported optimum."),
    help=Help(
        title="Habitat Creation",
        definition="Connected subsurface space and pore water the modeled exchange makes available.",
        method="Extent of the hyporheic zone, restated on a pore-water basis.",
        note="Hydraulics alone cannot establish habitat quality.",
        sources=("framework", "boulton1998")),
)

_F_THERMAL = FunctionSpec(
    key="thermal",
    display_label="Temperature Regulation",
    node_id="fn.scr.tmp",
    processes=("thermal_regulation",),
    headline_kpi="buffering_opportunity",
    # Whole hours: the three scenarios are 4, 8 and 16, and "8.00 h" implied a precision the
    # reference value does not have.
    assumption=PaneRow("response_time_hours", "Thermal response time", unit=" h", digits=0),
    limits=("The response time is a literature scenario, not a site-calibrated value.",
            "Sediment thermal properties and groundwater temperature are not modeled.",
            "The result is a fraction of an anomaly damped, never a temperature.",
            "Seasonal and daily timing of exchange is not represented."),
    help=Help(
        title="Temperature Regulation",
        definition="The share of a stream temperature anomaly the returning water has shed.",
        method="First-order relaxation toward sediment temperature over each path's travel time.",
        note="Reports a fraction, never degrees.",
        sources=("marzadri2013", "fogg2023")),
)

FUNCTIONS: dict[str, FunctionSpec] = {
    f.key: f for f in (_F_NUTRIENT, _F_POLLUTANT, _F_HABITAT, _F_THERMAL)
}

#: Display order in the tree and the report. Nutrient, pollutant, habitat, temperature.
FUNCTION_ORDER = ("nutrient", "pollutant", "habitat", "thermal")


def function_keys() -> tuple[str, ...]:
    return FUNCTION_ORDER


def get_function(key: str) -> FunctionSpec:
    try:
        return FUNCTIONS[key]
    except KeyError:
        raise KeyError(f"unknown function {key!r}; known: {', '.join(FUNCTIONS)}") from None


def function_for_process(process_key: str) -> FunctionSpec:
    """Which of the four hosts a given calculator. Total by construction, see rule 2 below."""
    for f in FUNCTIONS.values():
        if process_key in f.processes:
            return f
    raise KeyError(f"process {process_key!r} belongs to no function")


def _fn_strings(f: FunctionSpec) -> list[str]:
    """Every string this function can put in front of a user."""
    out = [f.display_label, *f.limits]
    for mech in f.mechanisms:
        out += [mech.label, mech.note]
    return [s for s in out if s]


def validate_functions() -> None:
    """Structural invariants for the function layer, run at import from `validate_registry`."""
    if set(FUNCTION_ORDER) != set(FUNCTIONS):
        raise ValueError("FUNCTION_ORDER must cover exactly the registered functions")

    # RULE 2, the one that makes the merge safe. Every calculator belongs to exactly one function:
    # microplastic cannot be orphaned by moving it under pollutant, and a sixth calculator cannot
    # ship without someone deciding where it goes.
    owned: dict[str, str] = {}
    for key, f in FUNCTIONS.items():
        for pk in f.processes:
            if pk not in PROCESSES:
                raise ValueError(f"function {key!r}: unknown process {pk!r}")
            if pk in owned:
                raise ValueError(f"process {pk!r} is claimed by both {owned[pk]!r} and {key!r}")
            owned[pk] = key
    missing = set(PROCESSES) - set(owned)
    if missing:
        raise ValueError(f"processes belong to no function: {sorted(missing)}. Every calculator "
                         f"needs a host, or it is unreachable in the interface.")

    nodes = set()
    for key, f in FUNCTIONS.items():
        where = f"function {key!r}"
        if f.key != key:
            raise ValueError(f"{where}: key mismatch ({f.key!r})")
        if not f.node_id.startswith("fn."):
            raise ValueError(f"{where}: node_id {f.node_id!r} must start with 'fn.'")
        if f.node_id in nodes:
            raise ValueError(f"{where}: node_id {f.node_id!r} is already used")
        nodes.add(f.node_id)
        if not f.limits:
            raise ValueError(f"{where}: limits is required. Revision spec §9.2 and §14.4: every "
                             f"inferred estimate states what it cannot tell you.")
        for bullet in f.limits:
            if not bullet.strip():
                raise ValueError(f"{where}: empty limits bullet")
            if len(bullet.split()) > 15:
                raise ValueError(f"{where}: limits bullet is {len(bullet.split())} words, over 15")
        if bool(f.mechanisms) != (len(f.processes) > 1):
            raise ValueError(f"{where}: a function needs mechanisms if and only if it hosts more "
                             f"than one process")
        if f.mechanisms and [mech.process for mech in f.mechanisms] != list(f.processes):
            raise ValueError(f"{where}: mechanisms must name the same processes, in order")
        if len({mech.key for mech in f.mechanisms}) != len(f.mechanisms):
            raise ValueError(f"{where}: duplicate mechanism keys")

        for mech in (None, *f.mechanisms):
            label = where if mech is None else f"{where} mechanism {mech.key!r}"
            # EVERY CALCULATOR NEEDS A NODE, or its pane is unreachable. A single-calculator
            # function uses its own; a mechanism must name one of its own, since its siblings
            # cannot share a pane once the radio that switched them is gone.
            if mech is not None:
                if not mech.node_id.startswith("fn."):
                    raise ValueError(f"{label}: node_id {mech.node_id!r} must start with 'fn.'")
                if mech.node_id in nodes:
                    raise ValueError(f"{label}: node_id {mech.node_id!r} is already used")
                nodes.add(mech.node_id)
            pk = mech.process if mech is not None else f.primary_process
            head = f.headline(mech)
            if head and not any(k.key == head for k in PROCESSES[pk].kpis):
                raise ValueError(f"{label}: headline {head!r} is not a KPI of process {pk!r}")
            if not head and not PROCESSES[pk].kpis:
                raise ValueError(f"{label}: process {pk!r} declares no KPI to headline")

        for text in _fn_strings(f):
            if "—" in text:
                raise ValueError(f"{where}: em dash in {text!r}. Project rule: never in "
                                 f"user-facing copy.")
        validate_help(f.help, where)


validate_registry()


__all__ = [
    "ProcessSpec", "PROCESSES", "SECTION_ORDER", "get_process", "process_keys", "validate_registry",
    "PaneRow", "PaneGroup", "PaneKpi", "PaneCurve", "ROW_KINDS", "visible_rows",
    "FunctionSpec", "Mechanism", "FUNCTIONS", "FUNCTION_ORDER",
    "get_function", "function_keys", "function_for_process", "pane_node",
    "validate_functions",
    "ASSUMED_RATE_HELP", "CITED_RATE_HELP", "HYDRAULICS_HELP", "OPPORTUNITY_CURVE_HELP",
    "RETURNING_PATHS_HELP", "DOWNWELLING_CELLS_HELP",
    "NUTRIENT_REDUCTION_HELP", "NUTRIENT_AREAL_HELP", "NUTRIENT_PER_KM_HELP",
    "POLLUTANT_REDUCTION_HELP", "POLLUTANT_AREAL_HELP", "POLLUTANT_PER_KM_HELP",
    "POLLUTANT_REGIME_HELP", "MICROPLASTIC_RETENTION_HELP", "MICROPLASTIC_CAPTURE_HELP",
    "MICROPLASTIC_GATE_HELP",
    "HABITAT_VOLUME_HELP", "THERMAL_BUFFERING_HELP", "THERMAL_FLOW_HELP", "THERMAL_DIEL_HELP",
    "THERMAL_REGIME_HELP",
    "KIND_RESIDENCE_TIME", "KIND_EXTENT", "KIND_PARTICULATE", "KINDS",
    "KINETICS_FIRST_ORDER", "KINETICS_ZERO_ORDER", "KINETICS_RELAXATION", "KINETICS_NONE",
    "KINETICS", "RATE_FIRST_ORDER_PER_DAY", "RATE_ZERO_ORDER_MG_L_DAY", "RATE_TIMESCALE_H",
    "DO_STREAM_DEFAULT_MG_L", "DO_ANOXIC_THRESHOLD_MG_L", "OXYGEN_CONSUMPTION_MG_L_DAY",
    "OXYGEN_CITATION", "OXYGEN_TRANSFERABILITY",
    "NITRATE_DEFAULT_MG_N_L", "MONOD_HALF_SATURATION_MG_N_L", "SATURATION_CITATION",
    "Help", "SOURCES", "format_sources", "source_labels", "render_card", "flat_text",
    "OXYGEN_HELP", "OXYGEN_RATE_HELP", "ANOXIC_THRESHOLD_HELP", "THERMAL_BANDS_HELP",
    "THERMAL_BANDS",
]
