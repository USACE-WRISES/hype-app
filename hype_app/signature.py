"""The hyporheic hydraulic signature: one derivation, one registry, one set of cards.

WHAT THIS MODULE IS FOR. Frequency, duration and extent used to be three display strings in
`report.py` and three separate piles of inline arithmetic: `assess.build_results`,
`app._screening_now` and `app._scenario_metrics` each re-derived the exchange flux, the path-depth
statistics, the equivalent depth and the pore volume from the same raw bundle. They agreed by
coincidence rather than by construction, and on one quantity (pore volume) they did not agree at
all, because two of them froze porosity at different moments. `derive()` is now the only place any
of it happens, and every consumer reads the result.

WHAT IT DOES NOT DO. It computes nothing new. Every number comes from `metrics.py`, which is
unchanged: `connectivity`, `exchange_flux`, `residence_time_metrics`, `path_depth_metrics`,
`exceedance_fraction`, `equivalent_active_depth`, `pore_volume`. Revision spec §27 is explicit that
the validated hydraulic calculations are to be preserved, so this module relocates and names them
rather than reworking them.

NO COMPOSITE SCORE, EVER (§4.4, §27). The three dimensions have different units, do not sum to a
whole, and can move in opposite directions when conductivity changes. There is no field here that
multiplies or averages across them, and `validate_signature()` fails the build if a key appears
that reads like one.

WHY THE VALUES ARE DICTS AND NOT DATACLASSES. `frequency`, `duration` and `extent` are plain dicts
keyed by the EXACT field names of `contracts.ConnectivityMetrics`, `ResidenceTimeMetrics` and
`ZoneMetrics`. The pydantic contract already is the schema; mirroring fifty field names in a second
frozen dataclass would just create a new place for the two to drift, which is the failure this
module exists to remove. The contract validates the keys on the way in, so a typo here is a loud
error and not a silently dropped metric.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from . import dims
from . import metrics as m
from .fmt import fmt, fmt_sig
from .functions.helptext import Help, source_labels, validate_help
from .functions.registry import PaneKpi, PaneRow

__all__ = [
    "SignatureDim", "DIMENSIONS", "FREQUENCY", "DURATION", "EXTENT", "by_id",
    "RunProvenance", "SignatureInputs", "HydraulicSignature", "derive", "as_float",
    "connectivity_fields", "residence_fields", "zone_fields", "threshold_fields",
    "screening_fields", "scenario_metrics",
    "signature_cards", "card_view",
    "TurnoverDefinition", "TURNOVER_DEFINITION", "TURNOVER_HELP",
    "RegimeDescription", "regime_description",
    "DEFAULT_THRESHOLD_HOURS", "THRESHOLD_LABELS", "THRESHOLD_NOTE",
    "BANNED_RANKING_WORDS", "validate_signature",
]

SECONDS_PER_DAY = 86400.0
HOURS_PER_DAY = 24.0

#: Residence-time scenarios every run reports (report §10). Lives here rather than in `assess` so
#: `regime_description` can read the same list the thresholds were computed from.
DEFAULT_THRESHOLD_HOURS = (1.0, 6.0, 12.0, 24.0)
THRESHOLD_LABELS = {1.0: "Rapid-exposure scenario", 6.0: "Intermediate-exposure scenario",
                    12.0: "Longer-exposure scenario", 24.0: "Extended-exposure scenario"}
THRESHOLD_NOTE = ("Hydraulic opportunity only: this is the exchanged flow that stays in the "
                  "subsurface at least this long. It does not establish that any reaction "
                  "occurred.")

#: Porosity of last resort, when neither the hyporheic run nor the snapshot recorded one.
FALLBACK_POROSITY = 0.30


def _finite_or_none(x):
    """Keep finite floats AND infinity, which is a meaningful turnover length; drop NaN."""
    if x is None:
        return None
    try:
        return None if math.isnan(x) else x
    except TypeError:
        return x


def as_float(x):
    """A float, or None for anything that will not convert.

    Public because the raw bundles this module reads carry strings, nulls and empty Shiny fields,
    and every caller assembling `SignatureInputs` needs the same coercion."""
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


def _hours(days):
    """Residence times are stored in days and presented in hours, everywhere."""
    return None if days is None else days * HOURS_PER_DAY


# --------------------------------------------------------------------------- turnover definition
@dataclass(frozen=True)
class TurnoverDefinition:
    """Every fact revision spec §5.4 requires before the word "turnover" may appear in the app.

    §5.4 is blunt about it: "Until this definition is finalized, the interface must not use
    'turnover' as if its meaning were self-evident." Six questions have to be answerable, and they
    do not fit in a 70-word tooltip, so they live here once and render twice: the tooltip generates
    itself from these fields (see TURNOVER_HELP) and the report prints them in full alongside the
    three values this run actually used. Neither can drift from the other."""

    equation: str
    symbols: tuple[tuple[str, str], ...]
    denominator: str
    basis: str
    reach_length: str
    inclusion: str
    repeat_rule: str
    reciprocal: str
    sources: tuple[str, ...]

    def answers(self) -> tuple[tuple[str, str], ...]:
        """The six §5.4 questions and their answers, in the order the section asks them."""
        return (("What one turnover means", self.denominator),
                ("What kind of quantity it is", self.basis),
                ("How reach length enters", self.reach_length),
                ("Which pathways are counted", self.inclusion),
                ("Whether flow can count twice", self.repeat_rule),
                ("Its reciprocal", self.reciprocal))


TURNOVER_DEFINITION = TurnoverDefinition(
    equation="C_1km = (Q_HEF / Q_stream) x (1000 / L_reach)",
    symbols=(("C_1km", "streamflow-equivalent turnovers per kilometre of channel"),
             ("Q_HEF", "gross returning hyporheic exchange, m3/s"),
             ("Q_stream", "representative stream discharge, m3/s"),
             ("L_reach", "modeled reach length, m")),
    denominator=("One turnover is one streamflow-equivalent VOLUME exchanged through returning "
                 "hyporheic paths. It is not one hyporheic-zone volume, and it is not one "
                 "completed flow path."),
    basis=("A steady-state ratio normalized by distance, so it is a spatial exchange rate rather "
           "than a temporal frequency. It is not turnovers per day, and it is not a discharge."),
    reach_length=("Reach length enters as 1000 / L_reach, so the value is per kilometre of channel "
                  "whatever length was modeled and reaches of different length compare directly."),
    inclusion=("Only downwelling flow whose particle returned to the river. Flow that leaves a "
               "lateral boundary, gaining and throughflow paths, and particles unresolved inside "
               "the tracking window are all excluded. The unresolved share is reported separately "
               "as censored flow."),
    repeat_rule=("Each downwelling cell's inflow is divided among the particles released on it, so "
                 "a unit of flow contributes at most once and cannot be counted twice."),
    reciprocal=("River turnover length L_T = 1 km / C_1km restates the same quantity as a "
                "distance: the channel length over which one streamflow-equivalent volume is "
                "exchanged."),
    sources=("framework_signature", "harvey2019"),
)

#: GENERATED from TURNOVER_DEFINITION rather than written beside it, the way THERMAL_BANDS_HELP is
#: generated from THERMAL_BANDS. A tooltip that restates a definition in its own words is a
#: definition that will eventually disagree with itself.
#: The equation row is SPLIT FROM `TURNOVER_DEFINITION.equation`, not retyped: `.hype-tip-k` is a
#: narrow flex column, so the whole string in one cell wrapped around itself, but a second copy of
#: the equation is exactly the drift this module exists to prevent. Change the equation once and
#: the tooltip follows.
_EQ_LHS, _EQ_RHS = (s.strip() for s in TURNOVER_DEFINITION.equation.split("=", 1))

#: `rows_label` on all three is a NOUN for what the rows are, never "Definition": the card already
#: renders a Definition slot, and a second section under the same label reads as a duplicate. Keys
#: stay short for the same flex-column reason.
TURNOVER_HELP = Help(
    title="Streamflow-equivalent turnovers",
    definition=("How much streamflow the reach exchanges through returning hyporheic paths, "
                "scaled to one kilometre of channel."),
    method="Gross returning exchange over stream discharge, times 1000 over reach length.",
    rows=((_EQ_LHS, _EQ_RHS), ("1 turnover", "one streamflow-equivalent volume")),
    rows_label="Equation",
    note="A rate per distance, not per day. Only flow that returned to the river counts.",
    sources=TURNOVER_DEFINITION.sources,
)

DURATION_HELP = Help(
    title="Median residence time",
    definition=("The flux-weighted time exchanged streamwater spends underground before returning "
                "to the river."),
    method="Weighted median of returning particle travel times, weighted by each path's flow.",
    rows=(("Range", "P10 to P90"), ("Weighting", "each path's flow")),
    rows_label="Reported as",
    note="Contact opportunity, not proof that any reaction occurred.",
    sources=("framework_signature",),
)

EXTENT_HELP = Help(
    title="Equivalent active depth",
    definition=("Active hyporheic volume divided by streambed area, so reaches of different size "
                "can be compared."),
    method="Bulk sediment volume of the hyporheic class over modeled stream-cell area.",
    rows=(("D_HZ", "V_HZ / A_bed"), ("Basis", "bulk sediment")),
    rows_label="Equation",
    note="A volume normalization, never a uniform layer of that thickness.",
    sources=("framework_signature",),
)


# --------------------------------------------------------------------------- the dimension registry
@dataclass(frozen=True)
class SignatureDim:
    """One of the three dimensions, with everything needed to render it and nothing else.

    Reuses `PaneKpi` and `PaneRow` from the function registry deliberately. Both read from a flat
    dict and `app._fn_val` already formats them, so the Hyporheic Zone pane and the function panes
    share one formatter instead of two that round differently. `PaneGroup` is NOT reused: its
    load-bearing field is `assumed_rate`, and nothing here rests on a rate constant, so the tier
    chip it drives would carry no information at all."""

    id: str
    kpi: PaneKpi
    definition: str
    relevance: str
    caution: str
    supporting: tuple[PaneRow, ...] = ()
    #: Formatted against the card's own already-formatted values, so no caller does arithmetic.
    sub_fmt: str = ""
    help: Help | None = None

    @property
    def label(self) -> str:
        return dims.DIM_LABEL[self.id]

    @property
    def short(self) -> str:
        return dims.DIM_SHORT[self.id]

    @property
    def controls(self) -> str:
        return dims.DIM_CONTROLS[self.id]


FREQUENCY = SignatureDim(
    id=dims.FREQUENCY,
    kpi=PaneKpi(label="Streamflow-equivalent turnovers", key="turnovers_per_km",
                unit="per km", help=TURNOVER_HELP),
    sub_fmt="One turnover every {turnover_length_km} km of channel.",
    definition=("How frequently streamwater is exchanged with returning hyporheic flow paths, "
                "over one kilometer of channel."),
    relevance=("Higher connectivity means more frequent delivery of oxygen, nutrients, carbon, "
               "and heat to the subsurface. It does not by itself indicate longer residence or "
               "greater processing."),
    caution="Frequency represents delivery or replenishment, not reaction completeness.",
    supporting=(PaneRow("returning_hyporheic_l_s", "Gross hyporheic exchange", unit=" L/s"),
                PaneRow("exchange_flux_mm_day", "Exchange intensity", unit=" mm/day"),
                PaneRow("turnover_length_km", "River turnover length", unit=" km")),
    help=TURNOVER_HELP,
)

DURATION = SignatureDim(
    id=dims.DURATION,
    # "hr", not "h": `metric_rows`, the CSV and the P10-to-P90 range line all say hr, and a card
    # whose unit disagrees with the table under it reads as two different quantities.
    kpi=PaneKpi(label="Median residence time", key="weighted_median_hours", unit="hr",
                help=DURATION_HELP),
    # No sub_fmt: this card's sub-line IS its P10-to-P90 range, and `_cards` fills it from
    # `primary_range` so the distribution is stated once rather than formatted in two places.
    sub_fmt="",
    definition=("The flux-weighted time exchanged streamwater remains in the subsurface, "
                "reported as the median with the P10 to P90 range."),
    relevance=("Residence time sets the opportunity for thermal exchange, oxygen consumption, and "
               "nutrient or contaminant transformation. It does not establish that a reaction "
               "occurred."),
    caution="Duration represents contact opportunity, not proof of reaction or ecological response.",
    supporting=(PaneRow("frac_above_1d", "Fraction over 1 day", kind="pct"),
                PaneRow("censored_fraction", "Censored flow", kind="pct")),
    help=DURATION_HELP,
)

EXTENT = SignatureDim(
    id=dims.EXTENT,
    kpi=PaneKpi(label="Equivalent active depth", key="equivalent_active_depth_m", unit="m",
                help=EXTENT_HELP),
    sub_fmt="Over {active_streambed_percent} percent of the modeled streambed.",
    definition=("Active hyporheic volume normalized by streambed area. It is a volume-normalized "
                "equivalent depth, not a uniform layer of that thickness."),
    relevance=("Represents the hydraulically connected subsurface space available for exchange, "
               "reaction, thermal storage, and potential habitat. It is not a measure of habitat "
               "quality."),
    caution=("Extent represents participating capacity, not the physical depth of the zone at "
             "any point."),
    supporting=(PaneRow("bulk_saturated_volume_m3", "Active hyporheic volume", unit=" m³"),
                PaneRow("active_streambed_fraction", "Active streambed", kind="pct"),
                PaneRow("path_depth_p90_m", "P90 max path depth", unit=" m")),
    help=EXTENT_HELP,
)

#: Display order everywhere: delivery, contact time, participating capacity.
DIMENSIONS = (FREQUENCY, DURATION, EXTENT)


def by_id(dim_id: str) -> SignatureDim:
    for d in DIMENSIONS:
        if d.id == dim_id:
            return d
    raise KeyError(f"unknown signature dimension {dim_id!r}; known: "
                   f"{', '.join(d.id for d in DIMENSIONS)}")


# --------------------------------------------------------------------------- inputs
@dataclass(frozen=True)
class RunProvenance:
    """How the extent numbers were produced (revision spec §7.7).

    §7.7 lists what modeled extent is sensitive to -- seeding density, grid resolution, domain
    depth, pathway-inclusion rules -- and requires the app to store and report those settings so
    extent values can be reproduced and compared fairly. Two particle populations answer two
    different questions here, and labelling one with the other's count would be a lie: the ZONE
    pass is what the volume rests on, the INTERFACE pass is what the flux and residence times rest
    on."""

    zone_particles_per_cell: int | None = None
    zone_seeds: int | None = None
    zone_cells_seeded: int | None = None
    zone_classified: int | None = None
    downwelling_cells: int | None = None
    interface_particles_per_cell: int | None = None
    #: "hyporheic run" | "input snapshot" | "fallback". See SignatureInputs.from_hz_bundle.
    porosity_basis: str | None = None


@dataclass(frozen=True)
class SignatureInputs:
    """Raw scalars and arrays only. No dicts, no pydantic models, no knowledge of where they came
    from. `from_hz_bundle` is the one adapter that knows the shape of the HZ workspace."""

    streamflow_cms: float | None = None
    reach_length_m: float | None = None
    porosity: float | None = None
    exchange: object = None                     # metrics.ExchangeAccounting | None, in m3/s
    transit_times_days: object = None
    transit_weights_m3_day: object = None
    path_depths_m: object = None
    path_lengths_m: object = None
    bulk_volume_m3: float | None = None
    footprint_binary_m2: float | None = None
    footprint_weighted_m2: float | None = None
    thickness_mean_m: float | None = None
    thickness_max_m: float | None = None
    streambed_area_m2: float | None = None
    active_streambed_area_m2: float | None = None
    return_streambed_area_m2: float | None = None
    connected_streambed_area_m2: float | None = None
    net_stream_exchange_cms: float | None = None
    censored_fraction: float | None = None
    max_tracking_time_days: float | None = None
    domain_volume_m3: float | None = None
    threshold_hours: tuple = DEFAULT_THRESHOLD_HOURS
    custom_thresholds: tuple = ()
    provenance: RunProvenance = field(default_factory=RunProvenance)

    @classmethod
    def from_hz_bundle(cls, hz_stats: dict, flux_metrics: dict, *, streamflow_cms=None,
                       reach_length_m=None, snapshot_porosity=None, domain_volume_m3=None,
                       threshold_hours=DEFAULT_THRESHOLD_HOURS,
                       custom_thresholds=()) -> "SignatureInputs":
        """Build inputs from the HZ workspace stats plus `app._flux_metrics`.

        THE ONE PLACE that knows `hz_stats["classes"]["hyporheic"]`, `["flux"]["accounting"]`,
        `["knobs"]` and `["counts"]`. Three call sites used to each destructure this bundle their
        own way, which duplicated the shape as well as the arithmetic."""
        full = hz_stats or {}
        classes = full.get("classes") or full or {}
        hyp = classes.get("hyporheic") or {}
        acct = (full.get("flux") or {}).get("accounting") or {}
        knobs = full.get("knobs") or {}
        counts = full.get("counts") or {}
        fm = flux_metrics or {}

        # POROSITY: the run's, not the field's, and specifically the HYPORHEIC run's.
        #
        # Porosity is a MODPATH input. It sets pore velocity, which set the travel times, which set
        # which particles returned inside the tracking window, which set `bulk_volume_m3` itself.
        # The value the zone pass tracked at is therefore the only one consistent with the volume
        # it produced. The input snapshot freezes porosity at the GROUNDWATER run, which is earlier
        # and can differ; preferring it (as the report path used to) reports a pore volume the flow
        # model never produced. `porosity_basis` records which won so the disagreement is visible
        # rather than silent, and validate.py raises a warning when the two differ.
        por_run, por_snap = as_float(knobs.get("porosity")), as_float(snapshot_porosity)
        if por_run is not None:
            porosity, basis = por_run, "hyporheic run"
        elif por_snap is not None:
            porosity, basis = por_snap, "input snapshot"
        else:
            porosity, basis = FALLBACK_POROSITY, "fallback"

        return cls(
            streamflow_cms=as_float(streamflow_cms),
            reach_length_m=as_float(reach_length_m),
            porosity=porosity,
            exchange=fm.get("exchange"),
            transit_times_days=fm.get("transit_times"),
            transit_weights_m3_day=fm.get("transit_weights"),
            path_depths_m=fm.get("path_depths"),
            path_lengths_m=fm.get("path_lengths"),
            bulk_volume_m3=as_float(hyp.get("volume_m3")),
            footprint_binary_m2=as_float(hyp.get("footprint_m2")),
            footprint_weighted_m2=as_float(hyp.get("footprint_m2")),
            thickness_mean_m=as_float(hyp.get("thickness_mean_m")),
            thickness_max_m=as_float(hyp.get("thickness_max_m")),
            streambed_area_m2=as_float(acct.get("streambed_area_m2")),
            active_streambed_area_m2=as_float(acct.get("active_streambed_area_m2")),
            return_streambed_area_m2=as_float(acct.get("return_streambed_area_m2")),
            connected_streambed_area_m2=as_float(acct.get("connected_streambed_area_m2")),
            net_stream_exchange_cms=as_float(acct.get("net_stream_exchange")),
            censored_fraction=fm.get("censored"),
            max_tracking_time_days=as_float(knobs.get("max_tracking_time_days")),
            domain_volume_m3=as_float(domain_volume_m3),
            threshold_hours=tuple(threshold_hours or ()),
            custom_thresholds=tuple(custom_thresholds or ()),
            provenance=RunProvenance(
                zone_particles_per_cell=knobs.get("particles_per_cell"),
                zone_seeds=counts.get("n_seeds"),
                zone_cells_seeded=counts.get("n_seed_cells"),
                zone_classified=counts.get("n_classified"),
                downwelling_cells=fm.get("downwelling_cells"),
                interface_particles_per_cell=fm.get("iface_ppc"),
                porosity_basis=basis))


# --------------------------------------------------------------------------- the result
@dataclass(frozen=True)
class HydraulicSignature:
    """The three dimensions plus the threshold scenarios, computed once.

    `frequency`, `duration` and `extent` are keyed by the field names of `ConnectivityMetrics`,
    `ResidenceTimeMetrics` and `ZoneMetrics` respectively. `thresholds` entries are keyed by
    `ThresholdResult`'s. See the module docstring for why these are dicts."""

    frequency: dict = field(default_factory=dict)
    duration: dict = field(default_factory=dict)
    extent: dict = field(default_factory=dict)
    thresholds: tuple = ()
    provenance: RunProvenance = field(default_factory=RunProvenance)
    have_rtd: bool = False

    def as_dict(self) -> dict:
        """FLAT, for `PaneKpi`/`PaneRow` lookup and for `ScreeningInputs`.

        Carries the three dimensions' contract fields verbatim plus a few DISPLAY duplicates in
        the units the interface shows (hours rather than days, L/s rather than m3/s, percent
        rather than fraction). The duplicates are additive and never replace the canonical field,
        so nothing downstream has to guess which unit it is holding."""
        out = {**self.frequency, **self.duration, **self.extent}
        out["returning_hyporheic_l_s"] = _times(self.frequency.get("returning_hyporheic_cms"))
        out["net_stream_exchange_l_s"] = _times(self.frequency.get("net_stream_exchange_cms"))
        for key in ("weighted_mean", "weighted_median", "p10", "p25", "p50", "p75", "p90"):
            src = f"{key}_days" if key != "p50" else "weighted_median_days"
            out[f"{key}_hours"] = _hours(self.duration.get(src))
        frac = self.frequency.get("active_streambed_fraction")
        out["active_streambed_percent"] = None if frac is None else frac * 100.0
        conn = self.frequency.get("connected_streambed_fraction")
        out["connected_streambed_percent"] = None if conn is None else conn * 100.0
        return out


def _times(cms):
    """m3/s -> L/s, for compact display values."""
    return None if cms is None else cms * 1000.0


# --------------------------------------------------------------------------- the derivation
def derive(inputs: SignatureInputs) -> HydraulicSignature:
    """THE hydraulic derivation. Everything else in the app reads its output.

    Calls only the validated functions in `metrics.py`. Any change to a number produced here is a
    change to a published result, and `tests/test_signature.py` pins every one of them against the
    arithmetic that used to be inlined in three separate call sites."""
    freq = _frequency(inputs)
    dur = _duration(inputs)
    ext = _extent(inputs)
    have_rtd = bool(_size(inputs.transit_times_days) and _size(inputs.transit_weights_m3_day))
    thr = _thresholds(inputs, q_hef=freq.get("returning_hyporheic_cms"),
                      c_1km=freq.get("turnovers_per_km"), have_rtd=have_rtd)
    return HydraulicSignature(frequency=freq, duration=dur, extent=ext, thresholds=thr,
                              provenance=inputs.provenance, have_rtd=have_rtd)


def _size(arr) -> int:
    if arr is None:
        return 0
    try:
        return len(arr)
    except TypeError:
        return int(getattr(arr, "size", 0))


def _frequency(s: SignatureInputs) -> dict:
    """Frequency of hyporheic exchange (report §5). Keyed for `ConnectivityMetrics`."""
    out: dict = {"streamflow_cms": s.streamflow_cms,
                 "net_stream_exchange_cms": s.net_stream_exchange_cms,
                 "streambed_area_m2": s.streambed_area_m2,
                 "active_streambed_area_m2": s.active_streambed_area_m2,
                 "return_streambed_area_m2": s.return_streambed_area_m2,
                 "connected_streambed_area_m2": s.connected_streambed_area_m2}
    ex = s.exchange
    if ex is not None:
        out.update(total_downwelling_cms=ex.total_downwelling,
                   returning_hyporheic_cms=ex.returning_hyporheic,
                   losing_cms=ex.losing_to_sides,
                   unresolved_cms=ex.unresolved,
                   mass_balance_error=ex.mass_balance_error)
        conn = m.connectivity(streamflow=s.streamflow_cms,
                              returning_hyporheic=ex.returning_hyporheic,
                              total_downwelling=ex.total_downwelling,
                              losing=ex.losing_to_sides, unresolved=ex.unresolved,
                              reach_length_m=s.reach_length_m)
        if conn is not None:
            out.update(excursions_per_mile=conn.excursions_per_mile,
                       turnover_length_m=conn.turnover_length_m,
                       turnover_length_km=conn.turnover_length_km,
                       turnovers_per_km=_finite_or_none(conn.turnovers_per_km),
                       gross_exchange_ratio_reach=_finite_or_none(conn.gross_exchange_ratio_reach))
        else:
            out["unavailable_reason"] = ("Streamflow, reach length, or flux-weighted "
                                         "classification unavailable; connectivity not computed.")
        flux = m.exchange_flux(ex.returning_hyporheic, s.streambed_area_m2)
        out["exchange_flux_m_day"] = _finite_or_none(flux["m_per_day"])
        out["exchange_flux_mm_day"] = _finite_or_none(flux["mm_per_day"])
        if ex.total_downwelling and ex.total_downwelling > 0:
            out["returning_flow_fraction"] = ex.returning_hyporheic / ex.total_downwelling
            out["censored_flow_fraction"] = ex.unresolved / ex.total_downwelling
    else:
        out["unavailable_reason"] = ("Streamflow, reach length, or flux-weighted classification "
                                     "unavailable; connectivity not computed.")
    if s.streambed_area_m2 and s.active_streambed_area_m2 is not None:
        out["active_streambed_fraction"] = s.active_streambed_area_m2 / s.streambed_area_m2
    if s.streambed_area_m2 and s.connected_streambed_area_m2 is not None:
        out["connected_streambed_fraction"] = s.connected_streambed_area_m2 / s.streambed_area_m2
    return out


def _duration(s: SignatureInputs) -> dict:
    """Duration in the hyporheic zone (report §6). Keyed for `ResidenceTimeMetrics`.

    Every central-tendency and percentile statistic is FLUX-weighted (§22.2, §6.4): the weights are
    each path's flow, so a path carrying ten times the water counts ten times. `min_days` and
    `max_days` are the raw extremes by design, since a weighted extreme is not a thing."""
    out: dict = {"porosity": s.porosity}
    if not (_size(s.transit_times_days) and _size(s.transit_weights_m3_day)):
        return out
    stats = m.residence_time_metrics(
        s.transit_times_days, s.transit_weights_m3_day, porosity=s.porosity,
        censored_fraction=s.censored_fraction,
        max_tracking_time_days=s.max_tracking_time_days)
    out.update(stats)
    if s.exchange is not None:
        out["returning_flux_represented_cms"] = s.exchange.returning_hyporheic
    return out


def _extent(s: SignatureInputs) -> dict:
    """Extent of the hyporheic zone (report §7). Keyed for `ZoneMetrics`."""
    out: dict = {
        "bulk_saturated_volume_m3": s.bulk_volume_m3,
        "mobile_pore_storage_m3": m.pore_volume(s.bulk_volume_m3, s.porosity),
        "equivalent_active_depth_m": m.equivalent_active_depth(s.bulk_volume_m3,
                                                               s.streambed_area_m2),
        "footprint_binary_m2": s.footprint_binary_m2,
        "footprint_weighted_m2": s.footprint_weighted_m2,
        "thickness_mean_m": s.thickness_mean_m,
        "thickness_max_m": s.thickness_max_m,
        # Framework §4.6/§17.1 require the basis to be STATED, and the report header prints this as
        # fact. Write it from the quantity the Extent dimension actually headlines rather than
        # inheriting the contract default.
        "active_volume_basis": "bulk sediment" if s.bulk_volume_m3 is not None else None,
    }
    if s.path_depths_m is not None and s.transit_weights_m3_day is not None:
        d = m.path_depth_metrics(s.path_depths_m, s.transit_weights_m3_day)
        out["path_depth_p50_m"] = d.get("p50_m")
        out["path_depth_p90_m"] = d.get("p90_m")
        out["path_depth_max_m"] = d.get("max_m")
    return out


def _thresholds(s: SignatureInputs, *, q_hef, c_1km, have_rtd) -> tuple:
    """Residence-time exceedance scenarios (report §10). Keyed for `ThresholdResult`."""
    specs = [(float(h), THRESHOLD_LABELS.get(float(h)), "default scenario")
             for h in (s.threshold_hours or ())]
    for c in (s.custom_thresholds or ()):
        specs.append((float(c["value_h"]), c.get("label"), c.get("source") or "user scenario"))
    rows = []
    for t_h, label, source in specs:
        p = _finite_or_none(m.exceedance_fraction(s.transit_times_days, s.transit_weights_m3_day,
                                                  t_h / HOURS_PER_DAY)) if have_rtd else None
        rows.append({
            "threshold_value_h": t_h, "threshold_label": label, "threshold_source": source,
            "flow_exceedance_fraction": p,
            "functional_exchange_m3_s": (q_hef * p) if (q_hef is not None and p is not None)
                                        else None,
            "functional_connectivity_per_km": (c_1km * p) if (c_1km is not None and p is not None)
                                              else None,
            "interpretation_note": THRESHOLD_NOTE})
    return tuple(rows)


# --------------------------------------------------------------------------- consumers
def connectivity_fields(sig: HydraulicSignature) -> dict:
    return dict(sig.frequency)


def residence_fields(sig: HydraulicSignature) -> dict:
    """Only the keys `ResidenceTimeMetrics` declares: `residence_time_metrics` also returns
    `effective_particle_count` and friends that the model does carry, but future additions to the
    stats dict must not blow up model construction."""
    from .contracts import ResidenceTimeMetrics
    return {k: v for k, v in sig.duration.items() if k in ResidenceTimeMetrics.model_fields}


def zone_fields(sig: HydraulicSignature) -> dict:
    return dict(sig.extent)


def threshold_fields(sig: HydraulicSignature) -> tuple:
    return tuple(dict(t) for t in sig.thresholds)


#: Which flat keys feed `functions.ScreeningInputs`. Named once here so a new hydraulic field
#: reaches every screening section without an edit in `app.py`.
_SCREENING_KEYS = (
    "streambed_area_m2", "active_streambed_area_m2", "active_streambed_fraction",
    "return_streambed_area_m2", "connected_streambed_area_m2", "connected_streambed_fraction",
    "exchange_flux_m_day", "returning_hyporheic_cms", "streamflow_cms", "turnovers_per_km",
    "bulk_saturated_volume_m3", "mobile_pore_storage_m3", "equivalent_active_depth_m",
    "path_depth_p50_m", "path_depth_p90_m", "porosity",
)


def screening_fields(sig: HydraulicSignature) -> dict:
    """The hydraulic half of `ScreeningInputs`. The caller adds the arrays and the chemistry."""
    flat = sig.as_dict()
    out = {k: flat.get(k) for k in _SCREENING_KEYS}
    out["censored_flow_fraction"] = sig.frequency.get("censored_flow_fraction")
    p = sig.provenance
    out["downwelling_cells"] = p.downwelling_cells
    out["interface_particles_per_cell"] = p.interface_particles_per_cell
    out["zone_particles_per_cell"] = p.zone_particles_per_cell
    out["zone_seeds"] = p.zone_seeds
    out["zone_cells_seeded"] = p.zone_cells_seeded
    out["zone_classified"] = p.zone_classified
    # WHICH porosity won, not just its value. Habitat's pore volume and equivalent depth are both
    # linear in it, so a run that recorded none and fell back to FALLBACK_POROSITY has two of its
    # three headlines resting on an assumption. The basis was already computed here and stopped at
    # `RunProvenance`; carrying it one step further is what lets the pane say so.
    out["porosity_basis"] = p.porosity_basis
    return out


def scenario_metrics(sig: HydraulicSignature) -> dict:
    """The four metrics the sensitivity sweep aggregates across scenarios."""
    return {"volume_m3": sig.extent.get("bulk_saturated_volume_m3"),
            "footprint_m2": sig.extent.get("footprint_binary_m2"),
            "pore_storage_m3": sig.extent.get("mobile_pore_storage_m3"),
            "equivalent_active_depth_m": sig.extent.get("equivalent_active_depth_m"),
            "rtd_median_days": sig.duration.get("weighted_median_days"),
            "turnovers_per_km": sig.frequency.get("turnovers_per_km"),
            "exchange_flux_m_day": sig.frequency.get("exchange_flux_m_day")}


# --------------------------------------------------------------------------- cards
def _cards(flat: dict) -> list[dict]:
    """The three scorecards from one flat value dict. Never two, never four, never a fourth
    combined number: §4.4 and §27 both forbid collapsing the dimensions into a score."""
    out = []
    for d in DIMENSIONS:
        k = d.kpi
        value = flat.get(k.key)
        sub = ""
        if d.sub_fmt:
            try:
                sub = d.sub_fmt.format(**{name: fmt(flat.get(name))
                                          for name in _fmt_names(d.sub_fmt)})
            except (KeyError, IndexError):
                sub = ""
        rows = [(r.label, _row_value(r, flat.get(r.key))) for r in d.supporting]
        out.append({
            "dim_id": d.id, "dimension": d.label, "short": d.short, "controls": d.controls,
            "primary_name": k.label, "primary_value": fmt(value), "value_raw": value,
            "primary_unit": k.unit, "primary_range": None,
            "sub": sub, "definition": d.definition, "relevance": d.relevance,
            "caution": d.caution, "help": d.help,
            "supporting": [(name, val, _row_unit(r))
                           for r, (name, val) in zip(d.supporting, rows)],
        })
    # The Duration card's range is the DISTRIBUTION, not a parameter sweep, and it doubles as that
    # card's sub-line. Formatting it once here is why DURATION declares no sub_fmt: a range written
    # in two places is a range that eventually disagrees with itself.
    lo, hi = flat.get("p10_hours"), flat.get("p90_hours")
    if lo is not None and hi is not None:
        dur = next(c for c in out if c["dim_id"] == dims.DURATION)
        dur["primary_range"] = f"P10 to P90: {fmt(lo)} to {fmt(hi)} hr"
        if not dur["sub"]:
            dur["sub"] = dur["primary_range"]
    return out


def _fmt_names(template: str) -> list[str]:
    import string
    return [n for _, n, _, _ in string.Formatter().parse(template) if n]


def _row_value(row: PaneRow, value):
    if value is None:
        return None
    if row.kind == "pct":
        return fmt(value * 100.0)
    if row.kind == "pct_sig":
        return fmt_sig(value * 100.0, row.digits)
    if row.kind == "int":
        return fmt(int(value))
    return fmt(value)


def _row_unit(row: PaneRow) -> str:
    return "%" if row.kind in ("pct", "pct_sig") else row.unit.strip()


def signature_cards(results) -> list[dict]:
    """The three scorecards from an `AssessmentResultsV2` (report §17.2).

    Reads only the results model, so the cards, the detailed metric table and the machine summary
    agree by construction."""
    c, r, z = results.connectivity, results.residence_time, results.zone
    flat = {**c.model_dump(), **r.model_dump(), **z.model_dump()}
    flat["returning_hyporheic_l_s"] = _times(c.returning_hyporheic_cms)
    flat["weighted_median_hours"] = _hours(r.weighted_median_days)
    flat["p10_hours"], flat["p90_hours"] = _hours(r.p10_days), _hours(r.p90_days)
    flat["active_streambed_percent"] = (None if c.active_streambed_fraction is None
                                        else c.active_streambed_fraction * 100.0)
    return _cards(flat)


def card_view(sig: HydraulicSignature) -> list[dict]:
    """The same three cards straight off a `HydraulicSignature`, for the Hyporheic Zone pane, which
    has no results model because the report has not been built yet."""
    return _cards(sig.as_dict())


# --------------------------------------------------------------------------- exchange regime
@dataclass(frozen=True)
class RegimeDescription:
    """A neutral, factual description of the exchange regime (revision spec §8).

    NO RANKING AND NO CUT POINTS. §8.6 forbids "this site has good hyporheic hydraulics" and §18.5
    forbids fixed low/medium/high thresholds until a defensible reference distribution exists.
    §8.1-§8.4 do suggest four labels ("High delivery with limited contact time"), but "high"
    requires a cut point that §18.5 says we may not invent, so those labels are deliberately NOT
    shipped. What is shipped restates the run's own numbers in sentences: every quantity below is
    either a value the model produced or an exceedance fraction from the run's own thresholds."""

    delivery_statement: str
    contact_statement: str
    extent_statement: str
    basis: str


_REGIME_BASIS = ("Derived from this run's own residence-time exceedance fractions and reach "
                 "geometry. No reference distribution of hyporheic signatures exists yet, so "
                 "these statements describe this reach and do not rank it against others.")


def regime_description(results, *, population=None) -> RegimeDescription:
    """Describe the regime without ranking it.

    `population` is reserved for a later phase: once a stated comparison population exists (§18.5
    permits regional reference sites, project alternatives, or this site's own sensitivity
    scenarios, provided the population is named), a caller can pass it and get comparative wording
    with no change here."""
    c, r, z = results.connectivity, results.residence_time, results.zone

    if c.turnovers_per_km is None:
        delivery = "Exchange turnover was not computed for this run."
    elif c.turnover_length_km is not None and math.isfinite(c.turnover_length_km):
        delivery = (f"One streamflow-equivalent volume is exchanged every "
                    f"{fmt(c.turnover_length_km)} km of channel "
                    f"({fmt(c.turnovers_per_km)} turnovers per km).")
    else:
        delivery = f"Exchange turnover is {fmt(c.turnovers_per_km)} per km of channel."

    # Contact time from the run's OWN thresholds. Walk them descending and take the largest whose
    # exceedance still reaches half the flow. "Half" is the median of the app's own distribution,
    # not an imported quality threshold, and the raw percentage rides along (§27: preserve raw
    # values whenever a qualitative label is shown).
    rows = sorted((t for t in results.thresholds
                   if t.flow_exceedance_fraction is not None),
                  key=lambda t: t.threshold_value_h)
    contact = "Residence times were not resolved for this run."
    if rows:
        reached = [t for t in rows if t.flow_exceedance_fraction >= 0.5]
        if reached:
            t = reached[-1]
            contact = (f"Half or more of the exchanged flow stays in the subsurface at least "
                       f"{_dur(t.threshold_value_h)} "
                       f"({_pct1(t.flow_exceedance_fraction)} percent).")
        else:
            t = rows[0]
            contact = (f"Under half of the exchanged flow stays in the subsurface as long as "
                       f"{_dur(t.threshold_value_h)} "
                       f"({_pct1(t.flow_exceedance_fraction)} percent).")
    elif r.weighted_median_days is not None:
        contact = (f"Median residence time is {fmt(_hours(r.weighted_median_days))} hours, "
                   f"flow-weighted.")

    # Whole sentences rather than comma-spliced clauses, because any of the three can be absent
    # and a fragment list reads as broken copy the moment one drops out.
    bits = []
    if c.active_streambed_fraction is not None:
        bits.append(f"Exchange reaches {_pct1(c.active_streambed_fraction)} percent of the "
                    f"modeled streambed.")
    if z.equivalent_active_depth_m is not None:
        vol = (f" over {fmt(z.bulk_saturated_volume_m3)} m³ of bulk sediment"
               if z.bulk_saturated_volume_m3 is not None else "")
        bits.append(f"That is a volume-normalized equivalent depth of "
                    f"{fmt(z.equivalent_active_depth_m)} m{vol}.")
    elif z.bulk_saturated_volume_m3 is not None:
        bits.append(f"The participating volume is {fmt(z.bulk_saturated_volume_m3)} m³ of bulk "
                    f"sediment.")
    extent = " ".join(bits) if bits else "Exchange extent was not computed."

    return RegimeDescription(delivery_statement=delivery, contact_statement=contact,
                             extent_statement=extent, basis=_REGIME_BASIS)


def _pct1(frac) -> str:
    """A percentage inside a SENTENCE. One decimal: three significant figures reads as false
    precision in prose ("63.951 percent") when the underlying exceedance is a screening estimate."""
    return "n/a" if frac is None else fmt(frac * 100.0, 1)


def _dur(hours: float) -> str:
    """"1 hour" / "6 hours" / "1 day", so a sentence reads like a sentence."""
    if hours >= HOURS_PER_DAY and hours % HOURS_PER_DAY == 0:
        d = int(hours // HOURS_PER_DAY)
        return "1 day" if d == 1 else f"{d} days"
    h = int(hours) if float(hours).is_integer() else hours
    return "1 hour" if h == 1 else f"{fmt(h)} hours"


# --------------------------------------------------------------------------- validation
#: Words that would turn a description into a rating. §8.6 and §20 forbid a universal good/bad
#: judgment, and the only reliable way to keep one out is to fail the build when it appears.
BANNED_RANKING_WORDS = ("good", "bad", "poor", "best", "worst", "better", "worse", "excellent",
                        "healthy", "degraded", "optimal", "ideal", "high-quality")

#: A key matching any of these would be a composite score, which §4.4 and §27 both forbid.
BANNED_COMPOSITE_KEYS = ("score", "index", "rating", "overall", "composite")


def _user_strings() -> list[tuple[str, str]]:
    """(where, text) for every string this module can put in front of a user."""
    out = [("SIGNATURE_TITLE", dims.SIGNATURE_TITLE),
           ("SIGNATURE_SUBTITLE", dims.SIGNATURE_SUBTITLE),
           ("SIGNATURE_SENTENCE", dims.SIGNATURE_SENTENCE),
           ("SIGNATURE_ANALOGY", dims.SIGNATURE_ANALOGY),
           ("THRESHOLD_NOTE", THRESHOLD_NOTE),
           ("regime basis", _REGIME_BASIS)]
    td = TURNOVER_DEFINITION
    out += [(f"turnover.{name}", getattr(td, name))
            for name in ("equation", "denominator", "basis", "reach_length", "inclusion",
                         "repeat_rule", "reciprocal")]
    out += [(f"turnover.symbol.{k}", v) for k, v in td.symbols]
    for d in DIMENSIONS:
        out += [(f"{d.id}.label", d.label), (f"{d.id}.controls", d.controls),
                (f"{d.id}.definition", d.definition), (f"{d.id}.relevance", d.relevance),
                (f"{d.id}.caution", d.caution), (f"{d.id}.kpi", d.kpi.label)]
        out += [(f"{d.id}.row.{r.key}", r.label) for r in d.supporting]
    # THE CONCEPTUAL FIGURE'S COPY IS NO LONGER HERE. It used to be, so this sweep could reach it
    # while it was drawn in matplotlib. The figure is now a hand-authored SVG shipped at
    # `data/figure/conceptual_model.svg`, and `tests/test_concept.py` runs this same em-dash and
    # ranking-word sweep over the text it draws. Moving the artwork must not mean losing the lint.
    return out


def validate_signature() -> None:
    """Structural invariants, run at import so a malformed registry cannot ship."""
    if tuple(d.id for d in DIMENSIONS) != dims.SIGNATURE_DIMS:
        raise ValueError(f"DIMENSIONS must be {dims.SIGNATURE_DIMS}, got "
                         f"{tuple(d.id for d in DIMENSIONS)}")
    seen_units = set()
    for d in DIMENSIONS:
        if d.id not in dims.DIM_LABEL:
            raise ValueError(f"{d.id!r} is not a known dimension")
        for slot in ("definition", "relevance", "caution"):
            if not getattr(d, slot).strip():
                raise ValueError(f"{d.id}: {slot} is required")
        if not d.kpi.key or not d.kpi.label:
            raise ValueError(f"{d.id}: the headline needs a key and a label")
        if d.kpi.unit in seen_units:
            # Not a style rule. Two dimensions sharing a unit is the first symptom of one of them
            # having been quietly redefined as a variant of the other (§4.4).
            raise ValueError(f"{d.id}: unit {d.kpi.unit!r} is already used by another dimension")
        seen_units.add(d.kpi.unit)
        for h in (d.help, d.kpi.help):
            if h is not None:
                validate_help(h, where=f"signature.{d.id}")

    for name in ("equation", "denominator", "basis", "reach_length", "inclusion", "repeat_rule",
                 "reciprocal"):
        if not getattr(TURNOVER_DEFINITION, name).strip():
            # §5.4: every one of these must be answered before the interface may say "turnover".
            raise ValueError(f"TURNOVER_DEFINITION.{name} is required by revision spec §5.4")
    if not TURNOVER_DEFINITION.sources:
        raise ValueError("TURNOVER_DEFINITION must cite its source")
    source_labels(TURNOVER_DEFINITION.sources)     # raises on an unresolvable key

    for where, text in _user_strings():
        low = text.lower()
        for word in BANNED_RANKING_WORDS:
            if word in low.split() or f" {word} " in f" {low} ":
                raise ValueError(f"{where}: {word!r} ranks the site. Revision spec §8.6 and §18.5 "
                                 f"forbid a universal good/bad judgment; state the value instead.")
        if "—" in text:
            raise ValueError(f"{where}: em dash in user-facing copy. Project rule: never.")
        # Same standing as the em dash, and the same reason to enforce it here rather than
        # remember it: a semicolon is where two sentences get welded into one long one, which is
        # the failure mode this copy keeps being shortened for.
        if ";" in text:
            raise ValueError(f"{where}: semicolon in user-facing copy. Project rule: never. "
                             f"Use two sentences.")


validate_signature()
