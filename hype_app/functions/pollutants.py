"""Cited pollutant endpoints for the screening panes (screening reference v2.0).

WHY THIS IS A SEPARATE MODULE. `registry.py` defines PROCESSES and pane layout; this defines
ENDPOINT PARAMETER DATA, which has a different shape and a different failure mode. A process is
authored once; an endpoint is a row in a literature table that a reviewer must be able to check
against its paper without reading any code around it.

THE ONE RULE THIS MODULE EXISTS TO ENFORCE. Nothing here is destruction. With nitrate in its own
section, every endpoint in this file is reversible sorption, biotransformation, or physical
retention (reference §0, §7). `TERMS` carries the reference's terminology table AS DATA and the
pane's headline labels are generated from it, so a banned word cannot reach a card by someone
editing a label string. `validate_presets()` runs at import and rejects one that tries.

WHAT "DERIVED" MEANS. Almost every rate here is a unit conversion the screening reference performed
on a number the original authors reported in other units, or a triple assembled from a reported
mean and standard deviation. `rate_derived` marks that, the pane displays it (reference rule 2), and
`sources` always names BOTH the primary paper and `hype_pollutant_ref`, because the conversion is
the reference document's work and a reviewer chasing a discrepancy needs to know where to look.

UNITS. Concentrations are stored in the unit the literature reports them in, because that is what
makes them checkable; `concentration_mg_l()` converts once for the calculation, which runs entirely
in mg/L (numerically g/m3, which is what makes the mass chain work). Mass display scale travels with
the preset for the same reason: a microgram-per-litre endpoint over a few thousand square metres
produces kilograms-per-day numbers with five leading zeros.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .helptext import SOURCES

#: Endpoint classes. The distinction is not cosmetic: it selects the vocabulary (reference §7),
#: and the two classes fail differently. A metal endpoint is finite-capacity and reversible, so it
#: carries an eligibility gate and a calibration range. An organic endpoint is transformation, so
#: it carries a depth of applicability instead.
ENDPOINT_METAL = "metal"
ENDPOINT_ORGANIC = "organic"
ENDPOINTS = (ENDPOINT_METAL, ENDPOINT_ORGANIC)

#: Concentration units the presets may declare, and their factor to mg/L. mg/L is the calculation
#: basis everywhere downstream.
CONCENTRATION_UNITS = {"mg/L": 1.0, "µg/L": 1e-3, "ng/L": 1e-6}

@dataclass(frozen=True)
class MassScale:
    """How one endpoint's masses are rendered. The calculation never changes.

    `factor` multiplies the canonical kg/day and g/m2/day figures. A microgram-per-litre endpoint
    over a few thousand square metres lands around 1e-5 kg/day, which prints as five leading zeros
    and reads as a broken widget; the same number is 0.06 g/day."""

    factor: float
    total: str
    areal: str
    per_km: str


MASS_SCALES = {
    "kg": MassScale(1.0, "kg/day", "g/m²/day", "kg/day/km"),
    "g": MassScale(1000.0, "g/day", "mg/m²/day", "g/day/km"),
}

#: Words the reference forbids for each endpoint class (§7 terminology table). Checked against
#: every generated label at import, so this is a tripwire and not a style note.
BANNED_WORDS = {
    ENDPOINT_METAL: ("removal", "removed", "destruction", "destroyed", "permanent"),
    ENDPOINT_ORGANIC: ("destruction", "destroyed", "permanent"),
}


@dataclass(frozen=True)
class Terms:
    """The vocabulary one endpoint class is allowed to use.

    `headline`, `areal` and `per_km` become the three KPI labels; `mass` labels the total. The
    pane reads these out of the screening result rather than off the registry, which is how one
    section serves two vocabularies without a branch in `app.py`."""

    headline: str
    areal: str
    per_km: str
    mass: str
    #: Past-participle for prose, e.g. "attenuated" / "transformed".
    verb: str
    #: What KIND of endpoint this is, for the chip beside a pollutant's name on the pane. Here
    #: rather than in app.py so the banned-word sweep below covers it like every other slot: a
    #: class label is exactly the sort of small string that reintroduces "removal" for a metal.
    kind_label: str = ""


#: Reference §7, verbatim in effect. Metals may never say removal: the mechanism is sorption to
#: newly forming Mn oxides and Fuller and Bargar observed desorption as pH fell.
TERMS: dict[str, Terms] = {
    ENDPOINT_METAL: Terms(
        headline="Dissolved-phase attenuation",
        areal="Attenuation per streambed area",
        per_km="Attenuation per stream km",
        mass="Total attenuated",
        verb="attenuated",
        kind_label="Trace metal"),
    ENDPOINT_ORGANIC: Terms(
        headline="Concentration reduction",
        areal="Transformation per streambed area",
        per_km="Transformation per stream km",
        mass="Total transformed",
        verb="transformed",
        kind_label="Trace organic"),
}


@dataclass(frozen=True)
class Preset:
    """One cited screening endpoint.

    `rate` is the (low, central, high) triple the pane sweeps, in /day. It is None only for a
    `stable` endpoint, whose answer is a genuine zero rather than an absent input -- see
    `screen.screen_reactive`, which treats those as different states. `rate_sd` is carried for
    provenance even where the triple was built from it, so the card can say what the bounds mean.

    `eligibility` lists conditions the app CANNOT verify from the model. They are displayed for the
    user to confirm, never silently assumed (reference rule 6)."""

    key: str
    label: str
    endpoint: str
    rate: tuple[float, float, float] | None = None
    rate_sd: float | None = None
    #: False only where the authors themselves fitted and reported the number in /day.
    rate_derived: bool = True
    #: Cited as non-degrading. Distinct from `rate=None`, which would mean "no value exists".
    stable: bool = False
    concentration: float | None = None
    concentration_unit: str = "mg/L"
    #: What the concentration actually IS. The reference forbids calling a laboratory value a site
    #: value, and three of these are laboratory starting concentrations.
    concentration_basis: str = ""
    mass_scale: str = "kg"
    eligibility: tuple[str, ...] = ()
    #: Field calibration travel-time window, minutes. Outside it the kinetic result is an
    #: extrapolation and the pane says so (reference rule 7).
    calibration_minutes: tuple[float, float] | None = None
    #: Depth the rate was fitted over, cm. Beyond it the rate does not apply (reference rule 9).
    depth_limit_cm: float | None = None
    #: Observed per-pass uptake distribution, (mean, low, high) as fractions. The reference's
    #: instruction is to COMPARE the kinetic result against this, never to multiply by it.
    observed_uptake: tuple[float, float, float] | None = None
    sources: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""

    @property
    def terms(self) -> Terms:
        return TERMS[self.endpoint]

    @property
    def rate_central(self) -> float | None:
        if self.stable:
            return 0.0
        return None if self.rate is None else float(self.rate[1])

    def concentration_mg_l(self, value=None) -> float | None:
        """`value` (or the preset default) in mg/L, the calculation basis."""
        v = self.concentration if value is None else value
        if v is None:
            return None
        return float(v) * CONCENTRATION_UNITS[self.concentration_unit]

    @property
    def mass(self) -> MassScale:
        return MASS_SCALES[self.mass_scale]


# --------------------------------------------------------------------------- trace metals
# Reference §4.5.1. Rates are the MEAN OF INDIVIDUAL RATE CONSTANTS x 1440, not the reciprocal of
# the mean time constant: those are different statistics and only the former is correct input to
# exp(-kt). See §1.1 -- the reciprocal route gives 63.16 /day for zinc and is wrong.
#
# The triple is mean +/- 1 SD. Nickel and manganese would go negative at the low end, so both are
# floored; the card states the basis so a floored bound cannot read as a measurement.
_METAL_FLOOR = 0.01

#: Shared by all four. The app can verify none of these, which is exactly why they are shown.
_METAL_ELIGIBILITY = (
    "Circumneutral pH (the study reach ran about 6.5 to 7.5)",
    "Dissolved manganese present, or active manganese-oxide coatings on the bed",
    "Modeled residence times near the field calibration range",
)

_METAL_NOTE = ("Uptake is sorption to newly forming manganese oxides, which Fuller and Bargar "
               "observed reversing as pH fell. Sorption capacity is finite and this model has no "
               "breakthrough term, so treat the result as an upper bound.")


def _metal(key, label, mean, sd, conc, uptake) -> Preset:
    return Preset(
        key=key, label=label, endpoint=ENDPOINT_METAL,
        rate=(max(mean - sd, _METAL_FLOOR), mean, mean + sd), rate_sd=sd, rate_derived=True,
        concentration=conc, concentration_unit="mg/L",
        concentration_basis=("laboratory starting concentration, described by the authors as "
                             "similar to surface water in the study reach"),
        mass_scale="kg",
        eligibility=_METAL_ELIGIBILITY, calibration_minutes=(2.0, 80.0),
        observed_uptake=uptake,
        sources=("fuller2000", "fuller2014", "hype_pollutant_ref"), note=_METAL_NOTE)


# --------------------------------------------------------------------------- trace organics
_FLUME_NOTE = ("Measured in river-simulating flumes on spiked water, with River Erpe sediment "
               "diluted 1:10 with sand and shallow bedform-driven exchange.")

_INSITU_NOTE = ("Fitted over the top 10 cm of the bed, where removal of biodegradable dissolved "
                "organic matter also peaks. Rates are strongly redox dependent.")

#: ln(2) x 24: half-life in hours to a rate in /day.
_HALFLIFE_H_TO_PER_DAY = 16.63553233343869


def _insitu(key, label, central, half_life_h, half_life_sd_h) -> Preset:
    """A Schaper in-situ endpoint, with bounds from the REPORTED half-life uncertainty.

    The bounds come from t_half +/- 1 SD rather than a made-up percentage band, because the paper
    reports that spread and a synthetic one would be the app inventing precision. The rate is
    inverse in the half-life, so the LONGER half-life gives the lower rate. `central` is the
    reference document's own rounded conversion, which agrees with ln2*24/t_half to the precision
    it states (iopromide 166.4 vs 166, tramadol 5.04 vs 5.0)."""
    lo = _HALFLIFE_H_TO_PER_DAY / (half_life_h + half_life_sd_h)
    hi = _HALFLIFE_H_TO_PER_DAY / (half_life_h - half_life_sd_h)
    return Preset(key=key, label=label, endpoint=ENDPOINT_ORGANIC,
                  rate=(lo, central, hi), rate_derived=True,
                  concentration_unit="µg/L", mass_scale="g", depth_limit_cm=10.0,
                  sources=("schaper2019", "schaper2018", "hype_pollutant_ref"),
                  note=_INSITU_NOTE)

PRESETS: tuple[Preset, ...] = (
    _metal("zinc", "Zinc", 83.52, 53.28, 0.602, (0.36, 0.07, 0.92)),
    _metal("cobalt", "Cobalt", 59.04, 50.40, 0.424, (0.52, 0.08, 1.00)),
    _metal("nickel", "Nickel", 28.80, 31.68, 0.440, (0.27, 0.07, 0.74)),
    _metal("manganese", "Manganese", 18.72, 20.16, None, (0.22, 0.05, 0.94)),

    # Reference §4.5.2. Four flumes gave 2.52, 0.455, 0.306 and 0.303 /day. The central is their
    # GEOMETRIC mean: the spread is over eightfold and an arithmetic mean (0.896) would sit near
    # the top of it. The reference's own instruction is to use this as a range.
    Preset(key="acesulfame", label="Acesulfame", endpoint=ENDPOINT_ORGANIC,
           rate=(0.30, 0.571, 2.52), rate_derived=True,
           concentration=11.5, concentration_unit="µg/L",
           concentration_basis="spiked flume surface water, not a measured stream concentration",
           mass_scale="g", sources=("jaeger2021", "hype_pollutant_ref"), note=_FLUME_NOTE),

    # Reference §4.5.3, in-situ River Erpe. These ship no concentration: the paper reports rates,
    # not a survey, and the efficiency headline does not need one. The mass chain reports its own
    # missing-concentration reason, which is the honest outcome.
    _insitu("iopromide", "Iopromide (contrast agent)", 166.0, 0.1, 0.01),
    _insitu("tramadol", "Tramadol", 5.0, 3.3, 0.3),

    # The counterexamples, and the reason they are selectable at all: a tool offering only
    # reactive compounds systematically overstates what hyporheic exchange does (reference
    # §4.5.3). Their rate is a REPORTED zero, not a missing value.
    Preset(key="venlafaxine", label="Venlafaxine (stable)", endpoint=ENDPOINT_ORGANIC,
           stable=True, rate_derived=False,
           concentration_unit="µg/L", mass_scale="g", depth_limit_cm=10.0,
           sources=("schaper2019", "hype_pollutant_ref"),
           note="Reported stable in the hyporheic zone. Exchange moves it; nothing transforms it."),
    Preset(key="o_desmethylvenlafaxine", label="O-desmethylvenlafaxine (stable)",
           endpoint=ENDPOINT_ORGANIC, stable=True, rate_derived=False,
           concentration_unit="µg/L", mass_scale="g", depth_limit_cm=10.0,
           sources=("schaper2019", "hype_pollutant_ref"),
           note="Reported stable in the hyporheic zone. Exchange moves it; nothing transforms it."),
    Preset(key="dihydroxy_carbamazepine", label="Dihydroxy-carbamazepine (stable)",
           endpoint=ENDPOINT_ORGANIC, stable=True, rate_derived=False,
           concentration_unit="µg/L", mass_scale="g", depth_limit_cm=10.0,
           sources=("schaper2019", "hype_pollutant_ref"),
           note="Reported stable in the hyporheic zone. Exchange moves it; nothing transforms it."),
)

PRESET_BY_KEY: dict[str, Preset] = {p.key: p for p in PRESETS}

#: The pane's endpoint checklist, grouped. THERE IS NO CUSTOM ENTRY: a user-supplied rate was the
#: section's primary mode until every rate in it became traceable to a paper, and an unsourced
#: number sitting first in the list invited exactly the invention the library exists to prevent.
#:
#: The leading id is a Shiny input id fragment, so it must stay stable and url-safe: the pane mints
#: one checkbox group per entry as `fn_pol_<group_id>`, and `_KEEP_IDS` names them.
PRESET_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("metals", "Trace metals (mining-impacted)", ("zinc", "cobalt", "nickel", "manganese")),
    ("organics", "Trace organics", ("acesulfame", "iopromide", "tramadol", "venlafaxine",
                                    "o_desmethylvenlafaxine", "dihydroxy_carbamazepine")),
)

#: Ticked when a project has never touched the section. ONE endpoint, so the pane opens with real
#: numbers and a reader's first sight of it is a result rather than three cards reading "n/a".
#: Zinc because it is the best documented of the four metals and heads the list.
DEFAULT_ENDPOINTS: tuple[str, ...] = ("zinc",)


def get_preset(key: str | None) -> Preset | None:
    """The preset for a checklist value, or None for unknown / blank."""
    if not key:
        return None
    return PRESET_BY_KEY.get(key)


def ordered_keys(keys) -> tuple[str, ...]:
    """The given endpoint keys in PRESET_GROUPS order, unknowns dropped, duplicates collapsed.

    One definition of "endpoint order", so the pane, the report and a restored project cannot each
    pick a different one. The checklist sends its own values in group order but a restored project
    carries whatever was stored."""
    want = set(keys or ())
    return tuple(k for _, _, group in PRESET_GROUPS for k in group if k in want)


def validate_presets(presets=None) -> None:
    """Structural invariants, run at import so a malformed endpoint cannot ship."""
    items = PRESETS if presets is None else presets
    seen = set()
    for p in items:
        where = f"preset {p.key!r}"
        if not p.key or p.key in seen:
            raise ValueError(f"{where}: blank or duplicate key")
        seen.add(p.key)
        if not p.label.strip():
            raise ValueError(f"{where}: needs a label")
        if p.endpoint not in ENDPOINTS:
            raise ValueError(f"{where}: endpoint {p.endpoint!r} not in {ENDPOINTS}")
        if p.concentration_unit not in CONCENTRATION_UNITS:
            raise ValueError(f"{where}: unknown concentration unit {p.concentration_unit!r}")
        if p.mass_scale not in MASS_SCALES:
            raise ValueError(f"{where}: unknown mass scale {p.mass_scale!r}")
        # A cited endpoint with no citation is the exact failure this library exists to prevent,
        # and the reference document must always be named beside the primary paper because every
        # derived value is its arithmetic.
        unknown = [k for k in p.sources if k not in SOURCES]
        if unknown:
            raise ValueError(f"{where}: unresolved sources {sorted(unknown)}")
        if not p.sources:
            raise ValueError(f"{where}: a shipped endpoint must cite its source")
        if p.rate_derived and "hype_pollutant_ref" not in p.sources:
            raise ValueError(f"{where}: a derived rate must cite the screening reference, which "
                             f"is where the conversion was performed")
        # Stable and rated are mutually exclusive states, and one of them must hold: an endpoint
        # with neither would silently reach the pane as a missing input.
        if p.stable and p.rate is not None:
            raise ValueError(f"{where}: a stable endpoint carries no rate triple")
        if not p.stable:
            if p.rate is None or len(p.rate) != 3:
                raise ValueError(f"{where}: rate must be (low, central, high)")
            lo, mid, hi = (float(x) for x in p.rate)
            if not (0 < lo <= mid <= hi):
                raise ValueError(f"{where}: rate must be positive and non-decreasing, "
                                 f"got {(lo, mid, hi)}")
        if p.concentration is not None and not p.concentration_basis.strip():
            raise ValueError(f"{where}: a shipped concentration must say what it is; the "
                             f"reference forbids presenting a laboratory value as a site value")
        if p.endpoint == ENDPOINT_METAL and not p.eligibility:
            raise ValueError(f"{where}: a metal endpoint needs its eligibility conditions")
        if p.observed_uptake is not None:
            mean, lo_u, hi_u = p.observed_uptake
            if not (0.0 <= lo_u <= mean <= hi_u <= 1.0):
                raise ValueError(f"{where}: observed uptake must be ordered fractions")
        # The terminology table is the enforcement point for reference §7. Checking the generated
        # labels rather than the table itself means adding a vocabulary cannot bypass it.
        t = p.terms
        if not t.kind_label.strip():
            raise ValueError(f"{where}: {p.endpoint!r} has no kind_label; the pane chips it beside "
                             f"every pollutant name")
        for slot in (t.headline, t.areal, t.per_km, t.mass, t.verb, t.kind_label):
            low = slot.lower()
            for banned in BANNED_WORDS[p.endpoint]:
                if banned in low:
                    raise ValueError(f"{where}: {slot!r} uses {banned!r}, which reference §7 "
                                     f"forbids for a {p.endpoint} endpoint")
    group_ids = set()
    for gid, label, keys in PRESET_GROUPS:
        if not label.strip():
            raise ValueError("every preset group needs a label")
        # The id becomes a Shiny input id, so it has to be an identifier and it has to be unique.
        if not gid.isidentifier() or gid in group_ids:
            raise ValueError(f"group {label!r}: id {gid!r} must be a unique identifier")
        group_ids.add(gid)
        missing = [k for k in keys if k not in PRESET_BY_KEY]
        if missing:
            raise ValueError(f"group {label!r}: unknown presets {missing}")
    grouped = {k for _, _, keys in PRESET_GROUPS for k in keys}
    if grouped != set(PRESET_BY_KEY):
        raise ValueError(f"every preset must appear in exactly one group; "
                         f"ungrouped {sorted(set(PRESET_BY_KEY) - grouped)}")
    unknown_default = [k for k in DEFAULT_ENDPOINTS if k not in PRESET_BY_KEY]
    if unknown_default:
        raise ValueError(f"DEFAULT_ENDPOINTS names unknown presets {unknown_default}")


validate_presets()


__all__ = [
    "ENDPOINT_METAL", "ENDPOINT_ORGANIC", "ENDPOINTS", "BANNED_WORDS",
    "CONCENTRATION_UNITS", "MASS_SCALES", "TERMS", "Terms", "Preset",
    "PRESETS", "PRESET_BY_KEY", "PRESET_GROUPS", "DEFAULT_ENDPOINTS",
    "get_preset", "ordered_keys", "validate_presets",
]
