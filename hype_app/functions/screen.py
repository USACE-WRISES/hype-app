"""Screening-tier function calculations (functions plan Parts I-II).

Pure functions over plain arrays, so every number is unit-testable and hand-checkable without a
model run. Nothing here imports Shiny, flopy, or touches disk.

FOUR SECTIONS, TWO SHAPES.

`residence_time` processes (denitrification, contaminant attenuation, thermal regulation) integrate
a first-order response over the flux-weighted residence-time distribution. `extent` processes
(habitat) read volumes and areas and carry no kinetics at all.

THE OXYGEN GATE. Denitrification does not begin when water enters the bed; oxygen has to be drawn
down first. Consumption is zero-order at stream concentrations (the Monod term is 0.978 at 9 mg/L),
so the onset time is linear in dissolved oxygen:

    t_anox = (C_O2_in - C_O2_threshold) / R_O2

That matters for usability, not just physics: dissolved oxygen is a quantity a user can estimate,
and the onset time is a DERIVED OUTPUT rather than something they have to guess.

THE FOUR-METRIC CHAIN, for the mass-bearing sections:

    E = Σ(wᵢ fᵢ) / Σ(wᵢ)                        removal efficiency, load-based
    M = Σ(wᵢ · C_in · fᵢ) = Q_HEF · C_in · E     total mass removed        [g/day]
    r = M / A_bed = q_HEF · C_in · E             areal removal rate        [g/m²/day]

so that `areal rate = exchange flux × inlet concentration × efficiency`, three factors a reviewer
can check independently.

UNITS. Residence times are DAYS and flow weights are m3/DAY, matching `hz_flux.npz`. Note that
`app.py` divides those weights by 86400 into m3/s before handing them to `ExchangeAccounting` --
pass the RAW weights here, or every mass is wrong by that factor. Registry rates are per-day or
hours; conversion happens here and nowhere else. Concentration in mg/L is numerically g/m3.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .. import metrics as m
from .pollutants import CONCENTRATION_UNITS, MASS_SCALES, get_preset
from .registry import (
    DO_ANOXIC_THRESHOLD_MG_L,
    DO_STREAM_DEFAULT_MG_L,
    KINETICS_FIRST_ORDER,
    KINETICS_RELAXATION,
    KINETICS_ZERO_ORDER,
    KIND_EXTENT,
    KIND_PARTICULATE,
    MONOD_HALF_SATURATION_MG_N_L,
    OXYGEN_CONSUMPTION_MG_L_DAY,
    THERMAL_BANDS,
    ProcessSpec,
)

SCREEN_METHOD_VERSION = "rtd_oxygen_gate_v1"

# --------------------------------------------------------------- exchange-limitation regimes
# Screening reference §4.4. `Da = k * T50` decides which variable actually controls the answer,
# and it is the difference between a result that means something and one that is a restatement of
# the exchange flux. Harvey et al. (2013) found reaction most efficient where Da was near 1,
# excluding both deep substrate-exhausted paths and shallow paths needing repeated entries.
#
# The band names are deliberately not "optimum" across the whole middle: the reference calls only
# Da ~ 1 the optimum, and claiming it at Da = 50 would overstate.
DA_REACTION_LIMITED = 0.01
DA_TRANSPORT_LIMITED = 100.0

REGIME_REACTION = "reaction-limited"
REGIME_RESPONSIVE = "rate and residence time both matter"
REGIME_TRANSPORT = "transport-limited"

#: One sentence per regime, saying what it means for the number above it.
REGIME_NOTES = {
    REGIME_REACTION: ("Reaction-limited: water leaves the bed before much can happen, so the "
                      "result is near zero and insensitive to exchange."),
    REGIME_RESPONSIVE: ("Both the rate and the residence time carry information here, which is "
                        "the regime a screening estimate is most informative in."),
    REGIME_TRANSPORT: ("Transport-limited: the reaction finishes early on the flow path, so this "
                       "is set by exchange flux alone and extra residence time buys nothing."),
}

# ------------------------------------------------------------------ thermal saturation regime
# Same job as the Damkohler bands above and the same placement on the pane, but thermal's ratio is
# residence time over a HEAT response time, so it needs its own cut points. Both come from the
# thermal plan rather than from judgement: 0.5 is §5.6's Diel-coupled band boundary, and 3 is §5.5's
# "at least 95% idealized attenuation", which is where the exponential has nothing left to give.
THERMAL_DA_COUPLED = 0.5
THERMAL_DA_SATURATED = 3.0

THERMAL_COUPLED = "diel-coupled"
THERMAL_RESPONSIVE = "residence time and response time both matter"
THERMAL_SATURATED = "fully damped"

#: One sentence per regime, saying what it means for the headline above it.
THERMAL_REGIME_NOTES = {
    THERMAL_COUPLED: ("Diel-coupled: the median path returns before much of the day's swing is "
                      "damped, so residence time is what limits this."),
    THERMAL_RESPONSIVE: ("Both the residence time and the response time carry information here, "
                         "which is the regime a screening estimate is most informative in."),
    THERMAL_SATURATED: ("Fully damped: the median path stays under far longer than the response "
                        "time, so damping is capped and only the amount of returning flow can "
                        "move this."),
}

#: "the caller said nothing", as distinct from None, which means "the user cleared the field".
#: Omitting `rate` uses the registry's central value; passing `rate=None` explicitly is a blank
#: and produces the rate-free result. Without this the two are indistinguishable and a cleared
#: rate silently reverts to the shipped default.
UNSET = object()

HOURS_PER_DAY = 24.0
GRAMS_PER_POUND = 453.59237
GRAMS_PER_KILOGRAM = 1000.0
METERS_PER_KM = 1000.0

#: Nitrate reported as N or as the nitrate ion. Getting this wrong is a silent 4.43x error, so it
#: is an explicit selector rather than a convention.
NITRATE_BASIS = {"N": 1.0, "NO3": 62.004 / 14.007}
NITRATE_BASIS_LABEL = {"N": "mg/L as N", "NO3": "mg/L as NO3"}

#: Rate-free sweep for the opportunity curve, in hours: 15 min to 40 days, ~4 per decade.
DEFAULT_CURVE_HOURS = (0.25, 0.5, 1.0, 2.0, 4.0, 6.9, 12.0, 24.0, 48.0, 96.0, 240.0, 960.0)

# THERMAL_BANDS lives in `registry` (it is labelled parameter data, like the rates) so the pane's
# legend can be generated from the same tuple this loop classifies with.


def _finite_or_none(x):
    """Keep finite floats, drop NaN/inf/None. Mirrors `assess._finite_or_none`."""
    if x is None:
        return None
    try:
        xf = float(x)
    except (TypeError, ValueError):
        return None
    return None if (math.isnan(xf) or math.isinf(xf)) else xf


def _positive(x):
    v = _finite_or_none(x)
    return v if (v is not None and v > 0) else None


def _count(x):
    """A non-negative whole count, or None. Zero is kept: no particles classified is a result."""
    v = _finite_or_none(x)
    return int(v) if (v is not None and v >= 0) else None


def _rate_or_zero(x):
    """A non-negative rate, KEEPING an explicit zero, or None when there is no usable value.

    `_positive` cannot serve here. Three endpoints in the preset library are cited as stable
    (Schaper et al. 2019 report venlafaxine, O-desmethylvenlafaxine and dihydroxy-carbamazepine as
    non-degrading), and for those a rate of zero IS the literature answer: the honest output is 0%
    transformation, not "no rate supplied". Routing them through `_positive` turned a published
    result into a missing input and hid the exact counterexample the screening reference keeps in
    the list, on the grounds that a tool offering only reactive compounds overstates benefit."""
    v = _finite_or_none(x)
    return v if (v is not None and v >= 0) else None


# --------------------------------------------------------------------------- the oxygen gate
def time_to_anoxia(dissolved_oxygen_mg_l, *, threshold_mg_l=DO_ANOXIC_THRESHOLD_MG_L,
                   consumption_mg_l_day=OXYGEN_CONSUMPTION_MG_L_DAY[1]) -> float | None:
    """Days before dissolved oxygen falls to the anoxic threshold, so denitrification can start.

    Zero-order consumption: `t = (C_in - C_threshold) / R`. At stream concentrations oxygen is the
    saturating substrate (Monod term 0.978 at 9 mg/L with K_O2 = 0.2 mg/L), so the linear form is
    the physically correct one, not a simplification. It also makes the result linear in the
    dissolved oxygen the user supplies.

    Returns None when the inputs cannot produce an onset, and 0.0 when the water arrives already at
    or below the threshold (anoxic stream water denitrifies immediately).

    Every parameter carries its shipped value as the signature default, so omitting one resolves
    while passing None explicitly means the user cleared that field and there is no onset to
    derive. `consumption_mg_l_day` used to default to None and then substitute the central rate
    internally, which made a cleared consumption rate indistinguishable from an unspecified one."""
    c_in = _finite_or_none(dissolved_oxygen_mg_l)
    thr = _finite_or_none(threshold_mg_l)
    rate = _positive(consumption_mg_l_day)
    if c_in is None or thr is None or rate is None or c_in < 0:
        return None
    return max(0.0, (c_in - thr) / rate)


def first_order_saturation(inlet_concentration_mg_l, rate_per_day, *,
                           half_saturation_mg_l=MONOD_HALF_SATURATION_MG_N_L) -> dict:
    """How far the first-order fit is being extrapolated at this concentration.

    First order has no ceiling: `k · C` grows without bound while real denitrification saturates
    once nitrate stops being limiting. The Monod half-saturation constant marks where that starts,
    so the ratio `C / K` is the honest measure of how much trust the mass estimate deserves.

    Returns the implied zero-order rate (`k · C`, the mg/L/day the model is asserting), the ratio,
    and a note when the ratio exceeds 1. Below 1 the note is None, because there is nothing to say:
    that is the regime the rate constants were fitted in."""
    c_in = _positive(inlet_concentration_mg_l)
    k = _positive(rate_per_day)
    k_half = _positive(half_saturation_mg_l)
    out = {"monod_half_saturation_mg_l": k_half, "saturation_ratio": None,
           "implied_zero_order_rate_mg_l_day": None, "first_order_validity_note": None}
    if c_in is None:
        return out
    if k is not None:
        out["implied_zero_order_rate_mg_l_day"] = _finite_or_none(k * c_in)
    if k_half is None:
        return out
    ratio = c_in / k_half
    out["saturation_ratio"] = _finite_or_none(ratio)
    if ratio > 1.0:
        # ONE sentence. This renders as a warn card in a ~360px pane, where the previous
        # four-sentence version pushed the inputs off the fold. The reasoning (why saturation
        # makes first order read high) lives in the Stream nitrate input's tooltip, which is
        # where a reader goes when they want it -- see registry's concentration_help.
        out["first_order_validity_note"] = (
            f"Nitrate is {ratio:.1f} times the {k_half:g} mg/L half-saturation constant, so this "
            f"estimate is an upper bound.")
    return out


@dataclass(frozen=True)
class ScreeningInputs:
    """Everything the four sections read. All optional but the two arrays; outputs that depend on a
    missing input come back None rather than raising, matching how the results pipeline degrades."""

    # residence-time distribution (returning paths only)
    transit_times_days: object = ()
    transit_weights_m3_day: object = ()
    # exchange accounting
    streambed_area_m2: float | None = None
    active_streambed_area_m2: float | None = None
    active_streambed_fraction: float | None = None
    # Bed where returning water LEAVES, and the union of entry and exit. `active_*` above stays
    # entry-only because framework §4.7 defines A_active that way and the report card publishes it;
    # these answer the different question of how much bed is engaged in either direction. Absent on
    # runs delineated before the engine computed them.
    return_streambed_area_m2: float | None = None
    connected_streambed_area_m2: float | None = None
    connected_streambed_fraction: float | None = None
    exchange_flux_m_day: float | None = None            # q_HEF
    returning_hyporheic_cms: float | None = None        # Q_HEF, for the weight-identity check
    streamflow_cms: float | None = None                 # Q_stream, for the thermal leverage ratio
    turnovers_per_km: float | None = None               # C_1km, for attenuation-weighted connectivity
    reach_length_m: float | None = None                 # normalizes removal per km of channel
    # Where the RTD came from. Reported so a small path count reads as site hydrology (few cells
    # downwell) rather than as data loss -- the zone-extent pass counts thousands of particles for
    # a different question, and the two sit next to each other in the tree.
    downwelling_cells: int | None = None
    interface_particles_per_cell: int | None = None
    censored_flow_fraction: float | None = None
    # particulate metrics (microplastics). Path LENGTHS, deliberately not times: retention in a
    # streambed is deep-bed filtration and Munz et al. measured profiles independent of flow
    # duration beyond about two pore volumes, so time is the wrong independent variable here.
    path_lengths_m: object = None
    particle_size_um: float | None = None
    median_grain_size_mm: float | None = None
    # extent metrics (habitat)
    bulk_saturated_volume_m3: float | None = None
    mobile_pore_storage_m3: float | None = None
    equivalent_active_depth_m: float | None = None
    path_depth_p50_m: float | None = None
    path_depth_p90_m: float | None = None
    porosity: float | None = None
    # The porosity currently in the UI, when it differs from the one the run was tracked at.
    # `porosity` above is always the run's; this exists only so the pane can say so.
    porosity_live: float | None = None
    # WHERE that porosity came from: "hyporheic run", "input snapshot" or "fallback". The pore
    # volume and the equivalent depth are both linear in it, so "fallback" means two of the three
    # habitat headlines rest on an assumed 0.3 rather than on anything the model was told.
    porosity_basis: str | None = None
    # Resolution of the ZONE pass, which is what the volume rests on. Deliberately separate from
    # `interface_particles_per_cell` above: that is the flux pass, a different particle population
    # answering a different question, and labelling one with the other's count would be a lie.
    zone_particles_per_cell: int | None = None
    zone_seeds: int | None = None
    zone_cells_seeded: int | None = None
    zone_classified: int | None = None
    # user chemistry
    #: ALWAYS mg/L, whatever unit the pane displayed. A preset declares its own unit and the
    #: caller converts through `Preset.concentration_mg_l` before building this, so the field name
    #: never has to be read as a lie.
    inlet_concentration_mg_l: float | None = None
    #: Which cited endpoint is selected, or None/"custom" for a user-supplied rate. Resolved
    #: through `pollutants.get_preset`; drives the default rate, the vocabulary, the display units
    #: and the three guard notes.
    preset_key: str | None = None
    nitrate_basis: str = "N"
    #: Whether the redox gate applies at all, for a spec that declares `oxygen_gated`. Off asks
    #: what the reach could do if oxygen never had to be consumed first, which is an upper bound
    #: and is labelled as one. Defaults True so every existing caller and saved project keeps the
    #: gated behaviour without knowing this field exists.
    oxygen_gate: bool = True
    # All three carry the shipped value as the dataclass default, so "not specified" resolves and
    # an explicit None means the user cleared the field and the gate cannot be located.
    dissolved_oxygen_mg_l: float | None = DO_STREAM_DEFAULT_MG_L
    anoxic_threshold_mg_l: float | None = DO_ANOXIC_THRESHOLD_MG_L
    oxygen_consumption_mg_l_day: float | None = OXYGEN_CONSUMPTION_MG_L_DAY[1]


def _sensitivity_bounds(central, published: tuple | None, *, allow_zero: bool = False) -> tuple:
    """(low, central, high) for a swept parameter.

    Honours the registry's published triple when it brackets the value in effect; otherwise falls
    back to factor-of-two around the effective central, which covers a user override and a process
    that ships no bounds. Either way a range always exists, because plan §8 requires removal to be
    reported as a range and never as a point."""
    c = _finite_or_none(central)
    if c is None or c < 0 or (c == 0 and not allow_zero):
        return (None, None, None)
    if published is not None and len(published) == 3:
        try:
            lo, _, hi = (float(x) for x in published)
        except (TypeError, ValueError):
            lo = hi = None
        if lo is not None and lo <= c <= hi:
            return (lo, c, hi)
    return (c / 2.0, c, c * 2.0)


def removal_fractions(times_days, *, spec: ProcessSpec, onset_days: float, rate: float | None,
                      inlet_concentration_mg_l: float | None = None):
    """Per-path response fraction fᵢ ∈ [0, 1], or None when the kinetics cannot be evaluated.

    first_order / relaxation: fᵢ = 1 - exp(-k · τᵢ)
    zero_order:               fᵢ = min(1, R₀ · τᵢ / C_in)
    where τᵢ = max(0, tᵢ - onset) is the time past the gate. For `relaxation` the rate is a
    RESPONSE TIME in hours rather than a per-day constant, so it is inverted here."""
    t = np.asarray(times_days, float)
    onset = max(0.0, float(onset_days or 0.0))
    reactive = np.clip(t - onset, 0.0, None)
    # A rate of exactly zero is a real published answer for the stable endpoints and yields zeros;
    # only relaxation still demands a positive value, because there the parameter is a RESPONSE
    # TIME and zero would be division by zero rather than "nothing happens".
    r = (_positive(rate) if spec.kinetics == KINETICS_RELAXATION else _rate_or_zero(rate))
    if r is None:
        return None

    if spec.kinetics == KINETICS_ZERO_ORDER:
        c_in = _positive(inlet_concentration_mg_l)
        if c_in is None:
            return None                              # zero-order removal is meaningless without C_in
        return np.clip(r * reactive / c_in, 0.0, 1.0)

    if spec.kinetics == KINETICS_RELAXATION:
        # r is a response time in HOURS; the anomaly decays as exp(-t / tau).
        return -np.expm1(-reactive / (r / HOURS_PER_DAY))

    if spec.kinetics == KINETICS_FIRST_ORDER:
        return -np.expm1(-r * reactive)              # 1 - exp(-x), stable for small x

    return None


def opportunity_curve(times_days, weights, *, tau_hours=DEFAULT_CURVE_HOURS) -> list[dict]:
    """R(τ) swept across decades: the rate-free view (framework §13).

    R(τ) = Σ wᵢ (1 - exp(-tᵢ/τ)) / Σ wᵢ, with no onset and no rate constant. Framework §37.8.8 names
    the cross-site reading of this as the defensible inference: fewer sites retain substantial
    functional connectivity as the assumed reaction timescale lengthens, and the ranking changes."""
    return [{"tau_hours": float(h),
             "opportunity": _finite_or_none(
                 m.weighted_reaction_fraction(times_days, weights,
                                              timescale=float(h) / HOURS_PER_DAY, onset=0.0))}
            for h in tau_hours]


# --------------------------------------------------------------------------- dispatch
def screen_process(inputs: ScreeningInputs, spec: ProcessSpec, **knobs) -> dict:
    """Run one section and return a flat dict shaped for its contract model.

    ROUTING BY KIND IS LOAD-BEARING (reference rule 1). The particulate branch takes no `rate` at
    all -- passing one is a caller error rather than an ignored argument, because a per-day
    coefficient reaching a distance-driven endpoint is the category error the two-module split
    exists to prevent."""
    if spec.kind == KIND_PARTICULATE:
        if knobs.get("rate", UNSET) is not UNSET or knobs.get("rate_bounds") is not None:
            raise TypeError(f"{spec.key}: a particulate endpoint is distance-driven and takes no "
                            f"rate (reference rule 1)")
        return screen_particulate(inputs, spec)
    if spec.kind == KIND_EXTENT:
        return screen_extent(inputs, spec)
    if spec.kinetics == KINETICS_RELAXATION:
        return screen_thermal(inputs, spec, **knobs)
    return screen_reactive(inputs, spec, **knobs)


def _rtd(inputs: ScreeningInputs):
    """Finite, non-negative-weight (times, weights) from the inputs."""
    t = np.asarray(inputs.transit_times_days, float)
    w = np.asarray(inputs.transit_weights_m3_day, float)
    if t.size == 0 or w.size == 0:
        return np.empty(0), np.empty(0)
    ok = np.isfinite(t) & np.isfinite(w) & (w >= 0)
    return t[ok], w[ok]


def _base(spec: ProcessSpec, inputs: ScreeningInputs, n: int) -> dict:
    out = {
        "process_key": spec.key,
        "process_label": spec.display_label,
        "process_kind": spec.kind,
        "kinetics": spec.kinetics,
        "method_version": SCREEN_METHOD_VERSION,
        "citation": spec.citation,
        "source_keys": list(spec.sources),
        "transferability_note": spec.transferability_note,
        "censored_flow_fraction": _finite_or_none(inputs.censored_flow_fraction),
        "n_paths": int(n),
        # The provenance of n_paths, so the count is legible: paths = downwelling cells x
        # particles per cell, and a small number means few cells downwell, not lost data.
        "downwelling_cells": (None if inputs.downwelling_cells is None
                              else int(inputs.downwelling_cells)),
        "interface_particles_per_cell": (None if inputs.interface_particles_per_cell is None
                                         else int(inputs.interface_particles_per_cell)),
    }
    _signature_echo(out, inputs)
    return out


def _signature_echo(out: dict, inputs: ScreeningInputs) -> None:
    """Echo the three hyporheic-hydraulic-signature dimensions onto every section.

    NO NEW PHYSICS. Two are passthroughs of values the caller already derived, and the third is the
    same flow-weighted median `_damkohler` takes two lines later. They exist so a function pane can
    state which DIRECT MODEL OUTPUTS (revision spec §9.3) its INFERRED estimate rests on, using the
    identical number the Hyporheic Zone pane and the report scorecards publish rather than one
    re-derived per section, which is how these drift.

    All three ride every section, including the ones that do not use them. Which dimensions a pane
    actually shows is registry data (`FunctionSpec.signature`), not a decision made here: the
    microplastic mechanism reads EXTENT and never duration, and that is stated in the registry
    where a reader can see it."""
    out["signature_turnovers_per_km"] = _finite_or_none(inputs.turnovers_per_km)
    out["signature_equivalent_depth_m"] = _finite_or_none(inputs.equivalent_active_depth_m)
    t, w = _rtd(inputs)
    out["signature_t50_days"] = (_finite_or_none(m.weighted_quantile(t, w, 0.5))
                                 if t.size else None)


def _damkohler(out: dict, t, w, rate_eff) -> None:
    """`Da = k * T50` and its regime (reference §4.4, rule 14).

    Reported even when the mass chain never runs, because whether the rate matters at all is
    exactly what a reader needs before deciding to go find a better one."""
    t50 = m.weighted_quantile(t, w, 0.5)
    out["t50_days"] = _finite_or_none(t50)
    da = _finite_or_none(None if (rate_eff is None or out["t50_days"] is None)
                         else rate_eff * out["t50_days"])
    out["damkohler"] = da
    if da is None:
        return
    out["damkohler_regime"] = (REGIME_REACTION if da < DA_REACTION_LIMITED
                               else REGIME_TRANSPORT if da > DA_TRANSPORT_LIMITED
                               else REGIME_RESPONSIVE)
    out["damkohler_note"] = REGIME_NOTES[out["damkohler_regime"]]


def _reach_scale(out: dict, inputs: ScreeningInputs, efficiency: float, c_in) -> None:
    """Stream-water consequences and the processing length (reference §4.3, Eqs. 6, 7, 10, 11).

    THE RULE THIS EXISTS FOR is reference rule 5: `k` may never be shown against a stream
    concentration. `outlet_concentration_mg_l` is the concentration of RETURNING HYPORHEIC WATER,
    `C_in(1-E)`; the stream sees far less, diluted by the exchange ratio. Presenting the former as
    a stream outcome overstates the benefit by exactly `Q_str / Q_HZ`.

    NO STREAM VELOCITY OR DEPTH IS NEEDED, which is not obvious. Writing `k_ex = Q_HZ/(Q_str *
    t_reach)` and `t_reach = L/U` gives a reach exponent

        k_eff * t_reach = (Q_HZ / Q_str) * f_bar

    in which U cancels, and therefore

        Lambda = U / k_eff = L_reach / [ (Q_HZ / Q_str) * f_bar ]

    also free of U and of depth. `k_eff` itself is the one quantity here that still needs a stream
    velocity, so it is deliberately not emitted: Lambda and the reach fraction carry the same
    information in a form this model can actually support."""
    q_hef = _positive(inputs.returning_hyporheic_cms)
    q_str = _positive(inputs.streamflow_cms)
    if q_hef is None or q_str is None:
        return
    ratio = q_hef / q_str
    out["exchange_ratio"] = _finite_or_none(ratio)
    exponent = ratio * efficiency                          # k_eff * t_reach, dimensionless
    out["reach_removal_fraction"] = _finite_or_none(-math.expm1(-exponent))
    if c_in is not None:
        # Eq. (6) is the linear form the reference writes; Eq. (11) is its exact counterpart. Both
        # ship: the linear one is what the document specifies, the exact one is what the report
        # should quote for a large exchange ratio, and the pair makes the approximation visible.
        out["stream_concentration_change_mg_l"] = _finite_or_none(c_in * exponent)
        out["stream_outlet_concentration_mg_l"] = _finite_or_none(c_in * (1.0 - exponent))
    reach_m = _positive(inputs.reach_length_m)
    if reach_m is not None and exponent > 0:
        out["processing_length_m"] = _finite_or_none(reach_m / exponent)
        # Grant et al. computed 275 km for a medium sand-bed stream at a 1.6 d half-life and
        # concluded hyporheic treatment confers little improvement under 1 km. Expressing the
        # processing length in reach lengths is the version a reader can act on.
        out["processing_length_reaches"] = _finite_or_none(1.0 / exponent)


def _endpoint_guards(out: dict, preset, inputs: ScreeningInputs) -> None:
    """The three conditions the screening reference will not let a metals or in-situ result ship
    without: rules 6, 7 and 9. Each is computed where the app can check it and displayed where it
    cannot.

    The labels and units are written here rather than beside the masses because they are
    properties of the ENDPOINT, not of the chain. A run that never reaches a mass still has to
    label its headline, and a pane reading a None unit would print a bare number."""
    scale = preset.mass if preset is not None else MASS_SCALES["kg"]
    out.update({"mass_display_scale": scale.factor, "total_mass_unit": scale.total,
                "areal_rate_unit": scale.areal, "per_km_unit": scale.per_km})
    if preset is None:
        return
    out.update({"preset_key": preset.key, "preset_label": preset.label,
                "endpoint_type": preset.endpoint, "rate_derived": preset.rate_derived,
                "preset_note": preset.note,
                "concentration_basis": preset.concentration_basis,
                "concentration_unit": preset.concentration_unit})
    t = preset.terms
    out.update({"headline_label": t.headline, "areal_label": t.areal, "per_km_label": t.per_km,
                "mass_label": t.mass})
    # Rule 6: conditions the model cannot verify. Shown, never assumed.
    if preset.eligibility:
        out["eligibility_conditions"] = list(preset.eligibility)
    t50_d = out.get("t50_days")
    # Rule 7: outside the field calibration window the kinetic result is an extrapolation, and
    # sorption capacity is finite with no breakthrough term in this model. On a gravel-bed site
    # with day-scale residence times this fires on every metals run, which is correct.
    if preset.calibration_minutes and t50_d is not None:
        lo_min, hi_min = preset.calibration_minutes
        t50_min = t50_d * 24.0 * 60.0
        if not (lo_min <= t50_min <= hi_min):
            obs = ""
            if preset.observed_uptake:
                mean_u, lo_u, hi_u = preset.observed_uptake
                obs = (f" Compare against the observed {lo_u * 100:.0f} to {hi_u * 100:.0f}% "
                       f"per-pass uptake, mean {mean_u * 100:.0f}%.")
            out["calibration_note"] = (
                f"This run's median residence time is {t50_min:,.0f} minutes, outside the "
                f"{lo_min:g} to {hi_min:g} minute range the rate was calibrated over. Treat the "
                f"result as an upper bound.{obs}")
    # Rule 9: the in-situ rates were fitted over the top 10 cm, where labile carbon and microbial
    # activity concentrate. The app already carries the flow-weighted median path depth, so this
    # is a check rather than a standing caveat.
    p50_m = _positive(inputs.path_depth_p50_m)
    if preset.depth_limit_cm and p50_m is not None:
        p50_cm = p50_m * 100.0
        if p50_cm > preset.depth_limit_cm:
            out["depth_note"] = (
                f"This rate was fitted over the top {preset.depth_limit_cm:g} cm of the bed. This "
                f"run's median path reaches {p50_cm:,.0f} cm, so applying it over the whole path "
                f"overstates transformation.")


def _weight_identity(out: dict, q_sum: float, inputs: ScreeningInputs) -> None:
    """Σwᵢ must equal Q_HEF (framework §5.9). Recorded rather than raised, because drift here
    scales every mass by the same factor and nothing else would catch it."""
    reported = _positive(inputs.returning_hyporheic_cms)
    if reported is not None:
        expected = reported * 86400.0
        out["weight_identity_rel_diff"] = _finite_or_none(abs(q_sum - expected) / expected)


# --------------------------------------------------------------------------- reactive sections
def screen_reactive(inputs: ScreeningInputs, spec: ProcessSpec, *,
                    rate: float | None = UNSET,
                    rate_bounds: tuple | None = None) -> dict:
    """Denitrification and contaminant attenuation: the oxygen gate (when the spec sets it) plus
    the four-metric chain.

    `rate` omitted uses the registry central value; `rate=None` is an explicit blank."""
    t, w = _rtd(inputs)
    out = _base(spec, inputs, t.size)
    # A selected endpoint supplies the default rate, its sweep bounds, the vocabulary and the
    # display units. None of it is applied silently: `_endpoint_guards` writes the provenance and
    # the conditions alongside, and the user can override every number (reference rule 16).
    preset = get_preset(inputs.preset_key)

    # ---- inlet concentration, on a stated basis ------------------------------
    c_raw = _positive(inputs.inlet_concentration_mg_l)
    basis = inputs.nitrate_basis if inputs.nitrate_basis in NITRATE_BASIS else "N"
    # Mass is carried as the reported species. Converting NO3 to N would silently restate the
    # user's number, so instead the basis travels with the result and the report labels it.
    c_in = c_raw
    out["inlet_concentration_mg_l"] = c_raw
    if spec.key == "denitrification":
        out["nitrate_basis"] = basis
        out["nitrate_basis_label"] = NITRATE_BASIS_LABEL[basis]

    # ---- the rate, and how far its fit is being pushed -----------------------
    # ABOVE the gate deliberately. These depend only on the spec, the rate argument and the inlet
    # concentration, none of which oxygen touches, and the gate's early return used to delete them:
    # clearing dissolved oxygen silently suppressed the nitrate saturation warning and emitted a
    # null rate for a run that had one in effect.
    # A preset's central beats the spec's, which is None for the contaminant section by design.
    # `rate=None` still means the user cleared the field, and `0.0` still means a cited-stable
    # endpoint -- three states, kept distinct.
    default_rate = preset.rate_central if preset is not None else spec.rate_central
    rate_eff = default_rate if rate is UNSET else _rate_or_zero(rate)
    out["rate_value"] = rate_eff
    out["rate_unit"] = spec.rate_unit
    if preset is not None and preset.stable:
        out["endpoint_stable"] = True

    # Nitrate-specific, so denitrification only: there is no general half-saturation constant for
    # an arbitrary contaminant. Computed from the inputs alone, so it reports even when the gate
    # cannot be located and even when the run has no usable flow paths.
    if spec.key == "denitrification":
        out.update(first_order_saturation(c_raw, rate_eff))

    # Regime and endpoint provenance BEFORE any early return, for the same reason the saturation
    # guard sits above the gate: whether the rate matters at all, and where it came from, are what
    # a reader needs most on exactly the runs that then fail to produce a mass.
    _damkohler(out, t, w, rate_eff)
    _endpoint_guards(out, preset, inputs)

    # ---- the oxygen gate ----------------------------------------------------
    # `gate_off` is the user having switched the redox limitation off on a spec that declares one.
    # It is NOT the same state as a gate that could not be located (a cleared dissolved-oxygen
    # field), which still blocks the section below -- one is a modeling choice and the other is
    # missing data, and collapsing them would let a typo read as a deliberate upper bound.
    onset_days = 0.0
    gate_off = spec.oxygen_gated and not inputs.oxygen_gate
    if spec.oxygen_gated:
        out["oxygen_gate"] = bool(inputs.oxygen_gate)
    if gate_off:
        # WRITTEN AS None, not left absent. Both branches then emit the same key set, which is
        # what a cross-site table needs: a column that silently disappears for half the reaches
        # is a column that gets misaligned. The pane and the report drop None rows either way.
        out.update({"dissolved_oxygen_mg_l": None, "anoxic_threshold_mg_l": None,
                    "oxygen_consumption_mg_l_day": None, "time_to_anoxia_hours": None})
        out["oxygen_gate_note"] = (
            "Oxygen limitation is switched off, so removal starts as soon as water enters the "
            "bed. This is an upper bound on what the reach could transform.")
    elif spec.oxygen_gated:
        do = _finite_or_none(inputs.dissolved_oxygen_mg_l)
        thr = _finite_or_none(inputs.anoxic_threshold_mg_l)
        rox = _positive(inputs.oxygen_consumption_mg_l_day)
        onset = time_to_anoxia(do, threshold_mg_l=thr, consumption_mg_l_day=rox)
        out.update({
            "dissolved_oxygen_mg_l": do,
            "anoxic_threshold_mg_l": thr,
            "oxygen_consumption_mg_l_day": rox,
            "time_to_anoxia_hours": _finite_or_none(
                None if onset is None else onset * HOURS_PER_DAY),
        })
        # NO SILENT ZERO HERE. This used to read `onset_days = 0.0 if onset is None else onset`,
        # which turned a missing dissolved oxygen into "the water is anoxic on arrival": every
        # path counted as fully reactive and removal was OVERSTATED, with the only tell being a
        # Time-to-anoxia row that quietly vanished. A gate that cannot be located blocks the
        # section instead.
        if onset is None:
            missing = ("stream dissolved oxygen" if do is None else
                       "anoxic threshold" if thr is None else "oxygen consumption rate")
            out["unavailable_reason"] = (
                f"No {missing} supplied, so the time to anoxia could not be derived and no "
                f"removal was computed.")
            return out
        onset_days = onset

    if t.size == 0 or w.sum() <= 0:
        out["unavailable_reason"] = (
            "No returning flow paths with positive flow weight. Run the Hyporheic Zone "
            "calculations first.")
        return out

    # ---- rate-free outputs, which stand alone -------------------------------
    # BOTH ANOXIA FRACTIONS STAY None WITH THE GATE OFF. `exceedance_fraction(t, w, 0.0)` is
    # trivially 1.0, so reporting it would print "Exchange reaching anoxia 100%" on a run where
    # anoxia was never modeled at all -- a number that looks measured and means nothing. Left
    # None, the two rows drop themselves out of the pane and the report; `time_to_anoxia_hours`
    # is never written in that branch for the same reason.
    above = None if gate_off else m.exceedance_fraction(t, w, onset_days)
    out["fraction_above_threshold"] = _finite_or_none(above)
    out["fraction_below_threshold"] = (None if above is None or math.isnan(above)
                                       else _finite_or_none(1.0 - above))
    out["reactive_exposure_m3"] = _finite_or_none(m.reactive_exposure(t, w, onset=onset_days))
    out["opportunity_curve"] = opportunity_curve(t, w)

    q_sum = float(w.sum())                             # Σwᵢ IS Q_HEF, in m3/day
    _weight_identity(out, q_sum, inputs)

    # ---- the chain ----------------------------------------------------------
    f = removal_fractions(t, spec=spec, onset_days=onset_days, rate=rate_eff,
                          inlet_concentration_mg_l=c_in)
    if f is None:
        # NO POSITIONAL WORD in either of these. They render above the group tables in the pane
        # and below the row list in the report, so "above" was accurate in at most one place at a
        # time; naming the property instead is true in both.
        out["unavailable_reason"] = (
            f"No {(spec.rate_label or 'rate').lower()} supplied, so the mass estimate was not "
            f"computed. The rate-free results are unaffected.")
        return out

    efficiency = float((w * f).sum() / q_sum)
    out["removal_efficiency"] = _finite_or_none(efficiency)
    # Above the concentration check, because the exchange ratio, the reach fraction and the
    # processing length are all concentration-free: a run with a rate but no concentration should
    # still be able to say whether the reach is long enough to matter.
    _reach_scale(out, inputs, efficiency, c_in)

    a_bed = _positive(inputs.streambed_area_m2)
    a_active = _positive(inputs.active_streambed_area_m2)
    reach_km = None
    if _positive(inputs.reach_length_m) is not None:
        reach_km = float(inputs.reach_length_m) / METERS_PER_KM
    if c_in is None:
        out["unavailable_reason"] = (
            f"No {(spec.concentration_label or 'concentration').lower()} supplied, so the mass "
            f"estimate was not computed. Removal efficiency does not depend on it.")
        return out

    mass_g_day = float((w * c_in * f).sum())           # the path integral, authoritative
    out["total_removed_kg_day"] = _finite_or_none(mass_g_day / GRAMS_PER_KILOGRAM)
    out["total_removed_lb_day"] = _finite_or_none(mass_g_day / GRAMS_PER_POUND)
    # Flow-weighted concentration of the water LEAVING THE BED. Algebraically C_in(1 - E), which is
    # what makes "removal efficiency" and "concentration reduction fraction" the same quantity.
    # This is NOT what the stream sees: reference rule 5 is explicit that a per-day rate may never
    # be shown against a stream concentration, and the stream change is smaller by the exchange
    # ratio. `_reach_scale` above emits the stream figures beside it.
    out["outlet_concentration_mg_l"] = _finite_or_none(c_in * (1.0 - efficiency))
    # ...and the same pair in the unit the pane is LABELLED with. Showing 0.0115 mg/L under a
    # field that reads µg/L is the same basis mix this section spends its guards preventing, just
    # at three orders of magnitude instead of one.
    inv = 1.0 / CONCENTRATION_UNITS.get(out.get("concentration_unit") or "mg/L", 1.0)
    out["inlet_concentration_display"] = _finite_or_none(c_in * inv)
    out["outlet_concentration_display"] = _finite_or_none(c_in * (1.0 - efficiency) * inv)
    # Per kilometre of channel: the scale a manager extrapolates with, and the same normalization
    # the connectivity headline already uses (turnovers per km).
    if reach_km is not None:
        out["removal_per_km_kg_day"] = _finite_or_none(
            mass_g_day / GRAMS_PER_KILOGRAM / reach_km)
        out["reach_length_m"] = float(inputs.reach_length_m)
    if spec.key == "denitrification":
        # Nitrogen-equivalent mass, so a cross-site table cannot silently mix a site reported as N
        # with one reported as NO3. The headline number stays in the species the user entered.
        out["total_removed_kg_n_day"] = _finite_or_none(
            mass_g_day / GRAMS_PER_KILOGRAM / NITRATE_BASIS[basis])
    if a_bed is not None:
        out["areal_removal_rate_g_m2_day"] = _finite_or_none(mass_g_day / a_bed)
        out["reference_area_m2"] = a_bed
        out["reference_area_basis"] = "total streambed"
    if a_active is not None:
        out["areal_rate_active_g_m2_day"] = _finite_or_none(mass_g_day / a_active)

    # QC: the reported decomposition r = q_HEF · C_in · E must reproduce r = M / A_bed. They agree
    # only if the stored exchange flux was built from the same returning flow the path weights sum
    # to, so a mismatch means the weight identity drifted upstream, not that this arithmetic is off.
    q_flux = _positive(inputs.exchange_flux_m_day)
    if q_flux is not None and a_bed is not None:
        r_direct = mass_g_day / a_bed
        out["chain_closure_rel_diff"] = _finite_or_none(
            abs(r_direct - q_flux * c_in * efficiency) / r_direct if r_direct else None)

    # ---- envelope across the parameter corners ------------------------------
    # THE INVARIANT: low <= central <= high, for every input the app can produce. It holds because
    # mass is monotone in both swept parameters, but only if BOTH corners derive from the value in
    # effect. The consumption corners used to read the published triple's ends while the central
    # onset used the user's override, so a rate outside 15.3 to 31.0 (the input has no maximum)
    # produced a headline above its own range.
    #
    # Slow corner = latest onset + slowest rate. The consumption mapping is INVERTED relative to
    # the rate: faster consumption means an earlier onset means MORE removal, so ox_hi pairs with
    # hi_rate. The anoxic threshold is deliberately NOT swept -- it has no published spread (it is
    # a project requirement, not a measured quantity), it shifts the onset by about 3% against 50%
    # for consumption, and the report's range note names exactly two swept parameters.
    # A cited endpoint brings its own published spread; the spec's triple is None for the
    # contaminant section by design. An explicit `rate_bounds` still wins, for a caller sweeping
    # something of its own.
    published = rate_bounds or (preset.rate if preset is not None else None) or spec.rate
    lo_rate, _, hi_rate = _sensitivity_bounds(rate_eff, published)
    onset_lo, onset_hi = onset_days, onset_days
    # `gate_off` and not just `oxygen_gated`: with the gate switched off there is no onset to
    # sweep, and sweeping one anyway would widen the envelope with a parameter that did not enter
    # the central estimate. Both corners collapse onto zero and the range spans the rate alone.
    if spec.oxygen_gated and not gate_off:
        do = _finite_or_none(inputs.dissolved_oxygen_mg_l)
        thr = _finite_or_none(inputs.anoxic_threshold_mg_l)
        rox = _positive(inputs.oxygen_consumption_mg_l_day)
        ox_lo, _, ox_hi = _sensitivity_bounds(rox, OXYGEN_CONSUMPTION_MG_L_DAY)
        fast = time_to_anoxia(do, threshold_mg_l=thr, consumption_mg_l_day=ox_hi)
        slow = time_to_anoxia(do, threshold_mg_l=thr, consumption_mg_l_day=ox_lo)
        onset_lo = onset_days if fast is None else fast      # earliest onset -> most removal
        onset_hi = onset_days if slow is None else slow      # latest onset  -> least removal
    lo = _mass_at(t, w, spec, onset_hi, lo_rate, c_in)
    hi = _mass_at(t, w, spec, onset_lo, hi_rate, c_in)
    out["total_removed_low_kg_day"] = _finite_or_none(
        None if lo is None else lo / GRAMS_PER_KILOGRAM)
    out["total_removed_high_kg_day"] = _finite_or_none(
        None if hi is None else hi / GRAMS_PER_KILOGRAM)
    # The same corners carried through to the normalized headlines, so the pane never re-derives a
    # range from a rounded number.
    if a_bed is not None:
        out["areal_removal_rate_low_g_m2_day"] = _finite_or_none(
            None if lo is None else lo / a_bed)
        out["areal_removal_rate_high_g_m2_day"] = _finite_or_none(
            None if hi is None else hi / a_bed)
    if reach_km is not None:
        out["removal_per_km_low_kg_day"] = _finite_or_none(
            None if lo is None else lo / GRAMS_PER_KILOGRAM / reach_km)
        out["removal_per_km_high_kg_day"] = _finite_or_none(
            None if hi is None else hi / GRAMS_PER_KILOGRAM / reach_km)
    _mass_display(out, preset)
    return out


#: Canonical result key -> (display key, needs a cited endpoint).
#:
#: THE BOUNDS ARE PRESET-ONLY, and that asymmetry is deliberate. `_sensitivity_bounds` falls back
#: to factor-of-two around whatever rate is in effect, which for a user-supplied number is a spread
#: the app invented; labelling it "sensitivity range" would claim a provenance that does not exist.
#: A cited endpoint brings a real one -- +/- 1 SD of the measured rate constants for the metals,
#: the measured 0.30 to 2.52 for acesulfame -- so there the range is shown. The canonical keys are
#: always emitted either way; only the twins the headline reads are gated.
_MASS_DISPLAY = (
    ("total_removed_kg_day", "total_mass_display", False),
    ("areal_removal_rate_g_m2_day", "areal_rate_display", False),
    ("areal_removal_rate_low_g_m2_day", "areal_rate_display_low", True),
    ("areal_removal_rate_high_g_m2_day", "areal_rate_display_high", True),
    ("removal_per_km_kg_day", "per_km_display", False),
    ("removal_per_km_low_kg_day", "per_km_display_low", True),
    ("removal_per_km_high_kg_day", "per_km_display_high", True),
)


#: Display twin -> the canonical key it was scaled from. The inverse of `_MASS_DISPLAY`, published
#: because the registry's headline and two of its rows name TWINS, not contract fields:
#: `_F_POLLUTANT.headline_kpi` is `total_mass_display`, which `_build_functions` filters away when
#: it validates into `ContaminantScreening`. Anything resolving a registry row key against a
#: contract model has to come through here first or it reads None and drops the endpoint silently.
CANONICAL_FOR_DISPLAY = {dst: src for src, dst, _ in _MASS_DISPLAY}

#: Canonical key -> its canonical unit string. Deliberately NOT read from `total_mass_unit` and
#: friends: those carry the DISPLAY scale (every organic preset is `mass_scale="g"`, factor 1000),
#: so pairing a canonical value with them understates by 1000x. A caller that resolves a key
#: through `CANONICAL_FOR_DISPLAY` must take its unit from here and ignore the row's `unit_key`.
CANONICAL_MASS_UNIT = {
    "total_removed_kg_day": MASS_SCALES["kg"].total,
    "areal_removal_rate_g_m2_day": MASS_SCALES["kg"].areal,
    "areal_removal_rate_low_g_m2_day": MASS_SCALES["kg"].areal,
    "areal_removal_rate_high_g_m2_day": MASS_SCALES["kg"].areal,
    "removal_per_km_kg_day": MASS_SCALES["kg"].per_km,
    "removal_per_km_low_kg_day": MASS_SCALES["kg"].per_km,
    "removal_per_km_high_kg_day": MASS_SCALES["kg"].per_km,
}


def row_specs(process_key) -> tuple[object | None, tuple]:
    """(primary spec, supporting specs) for one process, in the registry's own order.

    The registry is the vocabulary: `FunctionSpec.headline_kpi` already declares which result is
    THE primary, and the pane rows already declare every supporting one with its label, unit and
    formatting. Restating any of that here would be a second naming authority.

    SHARED, so the report's headline card and the alternatives fold cannot pick different primaries
    for the same section. It lives here rather than in either caller because both of them resolve
    through `CANONICAL_FOR_DISPLAY` below, and a second copy is how the display-twin trap comes
    back."""
    from .registry import function_for_process, get_process
    spec = get_process(process_key)
    fspec = function_for_process(process_key)
    primary = None
    if fspec is not None:
        want = fspec.headline(fspec.mechanism_for_process(process_key))
        primary = next((k for k in spec.kpis if k.key == want), None)
    if primary is None and spec.kpis:
        primary = spec.kpis[0]          # the fallback `validate_functions` already enforces
    rows: list = [k for k in spec.kpis if primary is None or k.key != primary.key]
    for g in spec.pane_groups:
        # `list_key` groups read a LIST of dicts out of the result (the thermal response bands),
        # not a number, so they have no range to fold.
        if not g.list_key:
            rows.extend(g.rows)
    rows.extend(spec.detail_rows)
    # NEVER `run_settings`: those are the settings the run was configured with, not results. The
    # sweep varies none of them, so every one would fold to a zero-width range.
    return primary, tuple(rows)


def is_numeric(v) -> bool:
    """Stricter than `alternatives.metric_ranges`, which is a bare isinstance check.

    A single NaN poisons min/max for the whole row and prints "nan to nan". `bool` is an `int` in
    Python and `NutrientScreening.oxygen_gate` is a bool, which would otherwise fold as 0/1."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def resolve_row(spec, model) -> tuple[str, str, str] | None:
    """(canonical key, name, unit) for one registry row against one result model.

    THE KEY IS RESOLVED THROUGH THE DISPLAY TWINS FIRST. The registry headlines the pollutant
    section with `total_mass_display`, which is not a field on `ContaminantScreening`: it is a
    rescaled copy minted for the pane and filtered away when `_build_functions` validates the
    model. Reading the row key straight off the model returns None for every endpoint and every
    scenario, and the section vanishes without a word.

    `unit_key` IS IGNORED FOR AN ALIASED KEY. The `*_unit` fields carry the DISPLAY scale (every
    organic preset is mass_scale="g", factor 1000), so pairing them with a canonical value
    understates it by 1000x. `label_key` still applies: it names the metric, and metal
    "attenuation" versus organic "transformation" is a real distinction."""
    key = CANONICAL_FOR_DISPLAY.get(spec.key, spec.key)
    if not hasattr(model, key):
        return None
    lk = getattr(spec, "label_key", "")
    name = (getattr(model, lk, None) or spec.label) if lk else spec.label
    if key != spec.key:
        unit = CANONICAL_MASS_UNIT.get(key, "")
    else:
        uk = getattr(spec, "unit_key", "")
        unit = (getattr(model, uk, None) or spec.unit) if uk else spec.unit
    return key, str(name), str(unit or "")


def _mass_display(out: dict, preset) -> None:
    """Rescaled copies of the mass chain, for endpoints whose masses are tiny in kilograms.

    The canonical keys never move: the contract, the report tables and every cross-site comparison
    keep reading kg/day and g/m2/day. These are display twins, so a microgram-per-litre endpoint
    reads 0.0592 g/day instead of 0.0000592 kg/day, which looks like a broken widget. The unit
    strings themselves are written by `_endpoint_guards`, which runs on every path."""
    scale = preset.mass if preset is not None else MASS_SCALES["kg"]
    for src, dst, cited_only in _MASS_DISPLAY:
        if cited_only and preset is None:
            continue
        v = out.get(src)
        if v is not None:
            out[dst] = _finite_or_none(v * scale.factor)


def _mass_at(t, w, spec, onset_days, rate, c_in) -> float | None:
    if rate is None or c_in is None:
        return None
    f = removal_fractions(t, spec=spec, onset_days=onset_days, rate=rate,
                          inlet_concentration_mg_l=c_in)
    return None if f is None else float((w * c_in * f).sum())


# --------------------------------------------------------------------------- habitat (extent)
def screen_extent(inputs: ScreeningInputs, spec: ProcessSpec) -> dict:
    """Habitat: physical extent of the hydraulically connected zone. No kinetics, no rate, no mass.

    EVERY headline here is on the PORE-WATER basis -- coverage, depth, volume -- because framework
    §4.6 makes mixing bases its named failure mode and the previous layout mixed them inside one
    card: a pore-water headline sitting above a bulk-basis depth, with nothing saying so. The
    bulk-basis figures still ship, in the detail rows, each carrying its basis in the label.

    COVERAGE IS THE UNION of the bed water enters through and the bed it returns through. Entry
    alone was reporting a reach that returns 100% of its downwelling as 28% "connected", because in
    a gaining reach the return is spread across far more cells than the focused inflow. Framework
    §4.7's entry-only A_active is untouched below and still what the report card publishes.

    The two depths are both real and answer different questions, so the identity between them is
    made checkable rather than left for the reader to stumble into:

        pore_equivalent_depth_m  ==  pore_depth_active_m x connected_streambed_fraction

    i.e. spreading the same water over the whole bed instead of only the part that exchanges. That
    is why the depth's denominator is the CONNECTED area and not A_active: the two must share a
    basis or the identity silently stops holding."""
    t, w = _rtd(inputs)
    out = _base(spec, inputs, t.size)
    pore = _positive(inputs.mobile_pore_storage_m3)
    a_bed = _positive(inputs.streambed_area_m2)
    a_act = _positive(inputs.active_streambed_area_m2)
    a_conn = _positive(inputs.connected_streambed_area_m2)
    out.update({
        "habitable_pore_volume_m3": pore,
        "bulk_volume_m3": _positive(inputs.bulk_saturated_volume_m3),
        "porosity": _positive(inputs.porosity),
        # Pore water over the WHOLE bed: framework §7.6's normalized depth, on the headline basis.
        "pore_equivalent_depth_m": (None if pore is None or a_bed is None else pore / a_bed),
        # Pore water over only the exchanging bed -- "intensity where exchange occurs", the
        # supporting value the functions plan §200-204 called for and only ever built for rates.
        # Deliberately resolves to None rather than falling back to A_active when the connected
        # area is absent: a silent basis swap between runs is the failure this section exists to
        # avoid, and an absent row is honest where a wrong-basis row is not.
        "pore_depth_active_m": (None if pore is None or a_conn is None else pore / a_conn),
        # Bulk basis, kept because it is the framework's D_HZ and the report's Extent headline.
        "equivalent_active_depth_m": _positive(inputs.equivalent_active_depth_m),
        "active_streambed_area_m2": a_act,
        "return_streambed_area_m2": _positive(inputs.return_streambed_area_m2),
        "connected_streambed_area_m2": a_conn,
        "streambed_area_m2": a_bed,
        "active_streambed_fraction": _finite_or_none(inputs.active_streambed_fraction),
        "connected_streambed_fraction": _finite_or_none(inputs.connected_streambed_fraction),
        "path_depth_p50_m": _positive(inputs.path_depth_p50_m),
        "path_depth_p90_m": _positive(inputs.path_depth_p90_m),
        "volume_basis": "pore water",
        # Provenance of the volume: the ZONE pass, never the interface pass `_base` reports.
        "zone_particles_per_cell": _count(inputs.zone_particles_per_cell),
        "zone_seeds": _count(inputs.zone_seeds),
        "zone_cells_seeded": _count(inputs.zone_cells_seeded),
    })
    # Unresolved zone seeds matter more than their share suggests: cell_class_fractions drops them
    # from the denominator (the streamtube rule), so a low classified fraction means each cell's
    # hyporheic share rests on fewer particles than the requested density implies.
    seeds, classified = out["zone_seeds"], _count(inputs.zone_classified)
    if seeds and classified is not None:
        out["zone_classified_fraction"] = classified / seeds
    # TWO WAYS THE POROSITY CAN BE UNTRUSTWORTHY, and they cannot both apply: the drift case needs
    # a recorded run value, and the fallback case is the absence of one. So they share the
    # `advisory_note` slot the Limitations panel already reads, and the row label follows.
    run_n, live_n = _positive(inputs.porosity), _positive(inputs.porosity_live)
    fallback = inputs.porosity_basis == "fallback"
    out["porosity_basis"] = inputs.porosity_basis
    out["porosity_label"] = "Porosity (assumed)" if fallback else "Porosity (as run)"
    if fallback:
        # Not a rounding matter: the pore volume IS bulk x porosity, and the equivalent depth is
        # that over the bed. A row reading "as run" above an assumed number claims a provenance
        # the run does not have, which is the failure this section's basis rules exist to avoid.
        assumed = "" if run_n is None else f", so {run_n:g} was assumed"
        out["advisory_note"] = (
            f"No porosity was recorded for this run{assumed}. The pore-water volume and the "
            f"equivalent depth scale directly with it.")
    # Porosity scales the headline AND set the pore velocities the zone was tracked at, so a field
    # edited since the run cannot be applied by multiplying -- it needs the run redone.
    elif run_n is not None and live_n is not None and abs(run_n - live_n) > 1e-9:
        out["advisory_note"] = (
            f"These use the porosity the zone was tracked at, {run_n:g}. The field now reads "
            f"{live_n:g}. Re-run the Hyporheic Zone calculations to apply it.")
    if out["habitable_pore_volume_m3"] is None and out["bulk_volume_m3"] is None:
        out["unavailable_reason"] = ("No hyporheic volume is available. Run the Hyporheic Zone "
                                     "calculations first.")
    return out


# --------------------------------------------------------------------------- microplastics
# Screening reference §5. A SEPARATE CALCULATION FAMILY, and the separation is the point.
#
# Microplastic retention in a streambed is deep-bed filtration, described since Iwasaki (1937) by
# an exponential decline in particle abundance with DISTANCE through the medium. Munz et al. (2024)
# tested the alternative directly: retention profiles were independent of flow duration beyond
# about two exchanged pore volumes, and extending infiltration time had negligible effect against
# grain size and velocity. So `exp(-k*t)` in per-day units is not a modelling preference here, it
# is empirically wrong, and reference rule 1 forbids it.
#
# THE HAZARD THIS SECTION IS BUILT AROUND (reference §5.2): the two distance coefficients differ by
# roughly six orders of magnitude and describe different geometry. They must never be substituted
# for one another and never multiplied together. Keeping them in separately named constants with
# their units in the name is the cheapest guard available.
ALPHA_MP_PER_KM = (0.0305, 0.0513, 0.0834)      # stream distance, reach scale
LAMBDA_F_PER_CM = (0.18, 0.42, 1.00)            # subsurface flowpath length, within the bed

#: Particle-to-grain size ratio `D = d_p / d50`. Below the first, straining is negligible and
#: capture is attachment-controlled; above the second the particle cannot enter the pore network at
#: all and deposits at the interface instead, by a more remobilizable mechanism.
STRAINING_ONSET_RATIO = 0.002
SIZE_EXCLUSION_RATIO = 0.08

#: Profiles stop declining exponentially below a relative abundance of 0.023, so a fraction passes
#: through regardless of path length. Reporting complete capture would deny the very mechanism by
#: which pore-scale microplastics reach alluvial aquifers.
CAPTURE_CAP = 0.977

GATE_ATTACHMENT = "attachment"
GATE_STRAINING = "straining"
GATE_EXCLUDED = "excluded"

GATE_NOTES = {
    GATE_ATTACHMENT: ("Below the straining threshold, so capture is attachment-controlled and the "
                      "filter coefficient sits at the low end of its measured range."),
    GATE_STRAINING: ("Small enough to enter the pore network, so mechanical straining is active. "
                     "This is the regime the filter coefficients were measured in."),
    GATE_EXCLUDED: ("Too large to enter the pore network. These particles deposit at the "
                    "sediment interface instead, by a different and far more remobilizable "
                    "mechanism, and are not filtered by the bed."),
}


def screen_particulate(inputs: ScreeningInputs, spec: ProcessSpec) -> dict:
    """Microplastic retention (reference §5). Distance-based, never time-based.

    TWO TIERS THAT ARE NEVER SUMMED (rule 11). Tier A is the reported number: a reach-scale
    empirical coefficient on stream distance, from Drummond et al.'s cross-class modelling. Tier B
    is a CAPABILITY DIAGNOSTIC on subsurface flowpath length, and §5.5 shows why it cannot be a
    competing estimate: even the lowest measured filter coefficient captures 83% within 10 cm, so
    for typical bedform paths essentially every particle small enough to enter is captured. Tier B
    therefore answers "can this bed catch this size at all", which is close to binary.

    The two reconcile: if per-pass capture is near complete, Drummond's ~5%/km cannot be a capture
    limit, so it must be a delivery-and-retention limit. The gap between the tiers is exactly the
    remobilization term neither module represents, which is why everything here is labelled
    retention rather than removal."""
    out = _base(spec, inputs, 0)
    out["module"] = "particulate"
    out["independent_variable"] = "distance"
    # `_base` seeds a path count of zero, which here would render as a real "0 flow paths
    # measured" row and hold the capture group open with nothing in it. Tier B sets a true count
    # if it runs; until then the section genuinely has none.
    out.pop("n_paths", None)

    # ---- Tier A: reach-scale, the reported number ---------------------------
    reach_m = _positive(inputs.reach_length_m)
    if reach_m is not None:
        reach_km = reach_m / METERS_PER_KM
        lo, mid, hi = ALPHA_MP_PER_KM
        out.update({
            "reach_length_m": reach_m,
            "alpha_mp_per_km": mid,
            "retained_fraction": _finite_or_none(-math.expm1(-mid * reach_km)),
            "retained_fraction_low": _finite_or_none(-math.expm1(-lo * reach_km)),
            "retained_fraction_high": _finite_or_none(-math.expm1(-hi * reach_km)),
        })
    else:
        out["unavailable_reason"] = ("No reach length is available, so reach-scale retention "
                                     "could not be computed. Generate boundaries first.")

    # ---- Tier B: size gate, then flowpath-length capture --------------------
    d_p = _positive(inputs.particle_size_um)
    d50 = _positive(inputs.median_grain_size_mm)
    if d_p is None or d50 is None:
        out["tier_b_reason"] = ("Enter a particle size and a median grain size to check whether "
                                "this bed can capture this size at all.")
        return out
    # Both to millimetres before dividing. The ratio is dimensionless and getting it wrong by 1000
    # would move every particle across the gate.
    ratio = (d_p / 1000.0) / d50
    out["size_ratio"] = _finite_or_none(ratio)
    out["particle_size_um"] = d_p
    out["median_grain_size_mm"] = d50
    gate = (GATE_EXCLUDED if ratio > SIZE_EXCLUSION_RATIO
            else GATE_STRAINING if ratio >= STRAINING_ONSET_RATIO
            else GATE_ATTACHMENT)
    out["size_gate"] = gate
    out["size_gate_note"] = GATE_NOTES[gate]
    if gate == GATE_EXCLUDED:
        # Rule 13: excluded particles are reported separately and flagged, never folded into a
        # filtration number they did not undergo.
        out["interface_deposition"] = True
        return out

    lengths = inputs.path_lengths_m
    if lengths is None:
        out["tier_b_reason"] = ("This run has no per-particle flow path lengths. Re-run the "
                                "Hyporheic Zone calculations to enable the capture check.")
        return out
    ln = np.asarray(lengths, float)
    w = np.asarray(inputs.transit_weights_m3_day, float)
    if w.size != ln.size:
        w = np.ones_like(ln)
    ok = np.isfinite(ln) & np.isfinite(w) & (ln > 0) & (w > 0)
    ln, w = ln[ok], w[ok]
    if ln.size == 0 or w.sum() <= 0:
        out["tier_b_reason"] = "No usable flow path lengths with positive flow weight."
        return out
    ln_cm = ln * 100.0                                  # lambda_f is per CENTIMETRE
    lo_l, mid_l, hi_l = LAMBDA_F_PER_CM

    def capture(lam):
        f = -np.expm1(-lam * ln_cm)
        return min(CAPTURE_CAP, float((w * f).sum() / w.sum()))

    out.update({
        "lambda_f_per_cm": mid_l,
        "path_capture_fraction": _finite_or_none(capture(mid_l)),
        "path_capture_low": _finite_or_none(capture(lo_l)),
        "path_capture_high": _finite_or_none(capture(hi_l)),
        "capture_cap": CAPTURE_CAP,
        "path_length_p50_m": _finite_or_none(m.weighted_quantile(ln, w, 0.5)),
        "n_paths": int(ln.size),
    })
    return out


# --------------------------------------------------------------------------- thermal
def screen_thermal(inputs: ScreeningInputs, spec: ProcessSpec, *,
                   rate: float | None = UNSET, rate_bounds: tuple | None = None) -> dict:
    """Temperature regulation (thermal plan §5). Buffering OPPORTUNITY only.

    Reports no degrees and no reach temperature change: stream temperature is set mainly by the
    surface energy budget, which this does not model (thermal plan §10.1-§10.2).

    NOTE ON RETARDATION. `spec.retardation` is carried for provenance and for the Detailed tier,
    but is deliberately NOT applied to the response time here. Marzadri et al. (2013) derived that
    timescale from a heat-transport model that already includes conduction and exchange with the
    solid matrix, so multiplying by a retardation factor would double-count it."""
    t, w = _rtd(inputs)
    out = _base(spec, inputs, t.size)
    tau_h = spec.rate_central if rate is UNSET else _positive(rate)
    bounds = rate_bounds or spec.rate
    out.update({"response_time_hours": tau_h, "retardation_factor": spec.retardation})

    # Named separately, because a cleared response time and an unrun model are different
    # problems and "run the Hyporheic Zone calculations first" is unhelpful for the former.
    if tau_h is None:
        out["unavailable_reason"] = ("No thermal response time supplied, so no buffering "
                                     "opportunity was computed.")
        return out
    if t.size == 0 or w.sum() <= 0:
        out["unavailable_reason"] = ("No returning flow paths with positive flow weight. Run the "
                                     "Hyporheic Zone calculations first.")
        return out

    q_sum = float(w.sum())
    _weight_identity(out, q_sum, inputs)

    def bq(tau_hours):
        return _finite_or_none(m.weighted_reaction_fraction(
            t, w, timescale=float(tau_hours) / HOURS_PER_DAY, onset=0.0))

    b = bq(tau_h)
    out["buffering_opportunity"] = b                       # B_Q
    out["remaining_anomaly_fraction"] = None if b is None else _finite_or_none(1.0 - b)
    # Corners sweep around the response time IN EFFECT, the same way screen_reactive sweeps its
    # rate: reading spec.rate directly meant a user-overridden fn_tau (min 0.5, no maximum) got a
    # range that could exclude its own central estimate.
    lo_tau, _, hi_tau = _sensitivity_bounds(tau_h, bounds)
    if lo_tau is not None:
        # B_Q is non-increasing in tau, so the FAST response (low tau) is the upper bound.
        out["buffering_opportunity_high"] = bq(lo_tau)
        out["buffering_opportunity_low"] = bq(hi_tau)

    q_hef = _positive(inputs.returning_hyporheic_cms)
    if q_hef is not None and b is not None:
        out["attenuation_weighted_flow_cms"] = _finite_or_none(q_hef * b)      # Q_TB
        out["attenuation_weighted_flow_l_s"] = _finite_or_none(q_hef * b * 1000.0)
        q_stream = _positive(inputs.streamflow_cms)
        if q_stream is not None:
            out["attenuation_weighted_exchange_ratio"] = _finite_or_none(q_hef * b / q_stream)
    c_km = _finite_or_none(inputs.turnovers_per_km)
    if c_km is not None and b is not None:
        out["attenuation_weighted_connectivity_per_km"] = _finite_or_none(c_km * b)   # C_TB

    # Persistence: flow fractions clearing one, two and three response times (thermal plan §5.5).
    for mult in (1, 2, 3):
        hrs = tau_h * mult
        out[f"fraction_above_{mult}tau"] = _finite_or_none(
            m.exceedance_fraction(t, w, hrs / HOURS_PER_DAY))
    # FULL-DIEL STORAGE OPPORTUNITY (thermal plan §5.5), fixed at one day and therefore the only
    # persistence number here that does not move when the response-time scenario changes. Water
    # held past a whole cycle comes back with no connection to the day it left. The plan is
    # explicit that this is storage opportunity and NOT a predicted 24-hour temperature lag.
    out["fraction_above_diel"] = _finite_or_none(m.exceedance_fraction(t, w, 1.0))
    t50 = m.weighted_quantile(t, w, 0.5)
    da_t = None if not np.isfinite(t50) else t50 * HOURS_PER_DAY / tau_h
    out["thermal_damkohler_median"] = _finite_or_none(da_t)
    # WHICH VARIABLE ACTUALLY CONTROLS THE ANSWER, on the card. B_Q saturates: once the median path
    # runs for weeks, every reach reads 100% and a reader has no way to tell a strongly buffering
    # site from one whose exchange is negligible. Same field names as the solute regime, so the
    # pane renders it with no per-section branch.
    if da_t is not None and np.isfinite(da_t):
        out["damkohler_regime"] = (THERMAL_COUPLED if da_t < THERMAL_DA_COUPLED
                                   else THERMAL_SATURATED if da_t >= THERMAL_DA_SATURATED
                                   else THERMAL_RESPONSIVE)
        out["damkohler_note"] = THERMAL_REGIME_NOTES[out["damkohler_regime"]]

    # Response bands: mathematical classes, never ecological quality classes.
    bands = []
    for label, lo_h, hi_h in THERMAL_BANDS:
        lo_d, hi_d = lo_h / HOURS_PER_DAY, hi_h / HOURS_PER_DAY
        sel = (t >= lo_d) & (t < hi_d) if math.isfinite(hi_h) else (t >= lo_d)
        bands.append({"label": label, "min_hours": lo_h,
                      "max_hours": None if math.isinf(hi_h) else hi_h,
                      "flow_fraction": _finite_or_none(float(w[sel].sum() / q_sum))})
    out["response_bands"] = bands

    # RTD-derived mobile storage (thermal plan §7). Weights are m3/day and times are days, so the
    # product is m3 directly -- the plan's 3600 factor assumes m3/s and hours.
    v_rtd = float((w * t).sum())
    out["rtd_storage_m3"] = _finite_or_none(v_rtd)
    f = removal_fractions(t, spec=spec, onset_days=0.0, rate=tau_h)
    if f is not None and v_rtd > 0:
        v_tb = float((w * t * f).sum())
        out["attenuation_weighted_storage_m3"] = _finite_or_none(v_tb)
        out["storage_buffered_fraction"] = _finite_or_none(v_tb / v_rtd)
    indep = _positive(inputs.mobile_pore_storage_m3)
    if indep is not None and v_rtd > 0:
        out["storage_cross_check_rel_diff"] = _finite_or_none(abs(v_rtd - indep) / indep)
    return out


__all__ = [
    "SCREEN_METHOD_VERSION", "DEFAULT_CURVE_HOURS", "THERMAL_BANDS", "UNSET",
    "NITRATE_BASIS", "NITRATE_BASIS_LABEL",
    "ScreeningInputs", "screen_process", "screen_reactive", "screen_extent", "screen_thermal",
    "screen_particulate",
    "removal_fractions", "opportunity_curve", "time_to_anoxia", "first_order_saturation",
    "GRAMS_PER_POUND", "GRAMS_PER_KILOGRAM", "HOURS_PER_DAY",
    # exchange-limitation regimes (reference §4.4)
    "DA_REACTION_LIMITED", "DA_TRANSPORT_LIMITED", "REGIME_REACTION", "REGIME_RESPONSIVE",
    "REGIME_TRANSPORT", "REGIME_NOTES",
    # the thermal equivalents, whose cut points are the thermal plan's own band boundaries
    "THERMAL_DA_COUPLED", "THERMAL_DA_SATURATED", "THERMAL_COUPLED", "THERMAL_RESPONSIVE",
    "THERMAL_SATURATED", "THERMAL_REGIME_NOTES",
    # particulate coefficients (reference §5). Named with their units because substituting one for
    # the other is a six-orders-of-magnitude error.
    "ALPHA_MP_PER_KM", "LAMBDA_F_PER_CM", "STRAINING_ONSET_RATIO", "SIZE_EXCLUSION_RATIO",
    "CAPTURE_CAP", "GATE_ATTACHMENT", "GATE_STRAINING", "GATE_EXCLUDED", "GATE_NOTES",
    # Twin -> canonical resolution, for anything reading a registry row key off a contract model.
    "CANONICAL_FOR_DISPLAY", "CANONICAL_MASS_UNIT",
    "row_specs", "resolve_row", "is_numeric",
]
