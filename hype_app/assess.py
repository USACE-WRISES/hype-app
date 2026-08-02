"""Assemble the canonical AssessmentResultsV2 from a completed analysis (revision spec §11.2).

The bridge between the run/HZ outputs and the report: it drives the metrics engine and freezes the
result model that every report format reads. Kept Shiny-independent and testable: the app passes
plain dicts from the HZ workspace; this computes the three hydraulic dimensions (frequency of
hyporheic exchange, duration in hyporheic zone, extent of hyporheic zone), the threshold
functional-opportunity results, and the
quality-control diagnostics, then stamps provenance + group hashes for staleness.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from . import metrics as m
from . import signature
from . import validate as validate_mod
from .contracts import (
    AssessmentInputSnapshot,
    AssessmentResultsV2,
    ConnectivityMetrics,
    ContaminantScreening,
    FunctionScreening,
    HabitatScreening,
    MicroplasticRetention,
    NutrientScreening,
    ResidenceTimeMetrics,
    ThermalOpportunity,
    ThresholdResult,
    ZoneMetrics,
)
from .functions import (
    DEFAULT_ENDPOINTS,
    DO_ANOXIC_THRESHOLD_MG_L,
    DO_STREAM_DEFAULT_MG_L,
    OXYGEN_CONSUMPTION_MG_L_DAY,
    UNSET,
    get_preset,
    ordered_keys,
)
from .provenance import HypeWarning, Severity

#: Re-exported from `signature`, which owns the scenarios now: `regime_description` describes the
#: run using the same thresholds the run was scored against, so the two cannot be defined apart.
DEFAULT_THRESHOLD_HOURS = signature.DEFAULT_THRESHOLD_HOURS


_FUNCTION_MODELS = {"denitrification": ("nutrient", NutrientScreening),
                    "contaminant": ("pollutant", ContaminantScreening),
                    "microplastic": ("microplastic", MicroplasticRetention),
                    "habitat": ("habitat", HabitatScreening),
                    "thermal_regulation": ("thermal", ThermalOpportunity)}


def _build_functions(knobs: dict, *, conn, zone, exchange, transit_times_days, transit_weights,
                     streamflow_cms, porosity, have_rtd, path_lengths=None,
                     reach_length_m=None) -> "FunctionScreening | None":
    """Run every screening section and pack them into the results container.

    Returns None only when there is nothing at all to report, so an absent analysis still
    serialises cleanly. Individual sections degrade on their own via `unavailable_reason`."""
    from .functions import SECTION_ORDER, ScreeningInputs, get_process, screen_process

    have_extent = zone.bulk_saturated_volume_m3 is not None
    if not have_rtd and not have_extent:
        return None

    # Whether the redox gate applies. Unlike the three oxygen knobs below there is no "cleared"
    # state for a checkbox, so absent and None both mean a caller that does not know about the
    # switch, and both must resolve to the gated behaviour every earlier project was screened with.
    _gate = knobs.get("oxygen_gate")
    inputs = ScreeningInputs(
        transit_times_days=(transit_times_days if have_rtd else ()),
        transit_weights_m3_day=(transit_weights if have_rtd else ()),
        streambed_area_m2=conn.streambed_area_m2,
        active_streambed_area_m2=conn.active_streambed_area_m2,
        active_streambed_fraction=conn.active_streambed_fraction,
        return_streambed_area_m2=conn.return_streambed_area_m2,
        connected_streambed_area_m2=conn.connected_streambed_area_m2,
        connected_streambed_fraction=conn.connected_streambed_fraction,
        exchange_flux_m_day=conn.exchange_flux_m_day,
        returning_hyporheic_cms=(exchange.returning_hyporheic if exchange is not None else None),
        streamflow_cms=streamflow_cms,
        turnovers_per_km=conn.turnovers_per_km,
        reach_length_m=reach_length_m,          # normalizes removal per km of channel
        censored_flow_fraction=conn.censored_flow_fraction,
        bulk_saturated_volume_m3=zone.bulk_saturated_volume_m3,
        mobile_pore_storage_m3=zone.mobile_pore_storage_m3,
        equivalent_active_depth_m=zone.equivalent_active_depth_m,
        path_depth_p50_m=zone.path_depth_p50_m,
        path_depth_p90_m=zone.path_depth_p90_m,
        # Particulate module: path LENGTHS and the two particle inputs. Absent on a run delineated
        # before the engine wrote lengths, in which case the capture check says so rather than
        # silently substituting a residence time, which is the wrong variable entirely.
        #
        # ALL THREE STILL TRAVEL with microplastics unregistered (registry.py, 2026-08-01). Nothing
        # reads them today and `knobs` no longer carries the last two, so they resolve None. They
        # stay because this module, `_FUNCTION_MODELS` and `per_section` below are all keyed by
        # section: re-registering the calculator is then a registry-and-tree edit with no change
        # here, which is the whole point of taking it out this way rather than deleting it.
        path_lengths_m=path_lengths,
        particle_size_um=knobs.get("microplastic_size_um"),
        median_grain_size_mm=knobs.get("microplastic_d50_mm"),
        porosity=porosity,
        oxygen_gate=True if _gate is None else bool(_gate),
        dissolved_oxygen_mg_l=knobs.get("dissolved_oxygen_mg_l", DO_STREAM_DEFAULT_MG_L),
        anoxic_threshold_mg_l=knobs.get("anoxic_threshold_mg_l", DO_ANOXIC_THRESHOLD_MG_L),
        # Two-arg get, like its two siblings above: an absent knob resolves to the shipped value,
        # while a knob present as None is the user having cleared the field, which blocks the gate.
        oxygen_consumption_mg_l_day=knobs.get("oxygen_consumption_mg_l_day",
                                              OXYGEN_CONSUMPTION_MG_L_DAY[1]),
        nitrate_basis=knobs.get("nitrate_basis") or "N")

    # Per-section concentration and rate; each section owns its own pair.
    #
    # Two-arg get with UNSET, not a bare get: a knob that is ABSENT means the caller said nothing
    # and the registry's central value applies, while a knob present as None means the user
    # cleared that field and the section must report itself rate-free rather than quietly
    # substituting the default. `_fn_inputs` always sends every key, so the app takes the second
    # path; an API caller that omits them takes the first.
    per_section = {
        "denitrification": (knobs.get("nitrate_mg_l"), knobs.get("denit_rate_per_day", UNSET)),
        "contaminant": (None, UNSET),          # handled by the endpoint loop below
        "microplastic": (None, None),
        "habitat": (None, None),
        "thermal_regulation": (None, knobs.get("thermal_response_hours", UNSET)),
    }

    # WHICH SECTIONS THE USER TURNED ON. An absent map means every section, which is what an API
    # caller and every payload written before the toggles existed intend. A section switched off
    # is not screened at all rather than screened and hidden: the report and the results JSON then
    # agree that no estimate was made, which is the honest meaning of the switch.
    enabled = knobs.get("screening_enabled") or {}

    packed: dict = {}
    for key in SECTION_ORDER:
        if not enabled.get(key, True):
            continue
        spec = get_process(key)
        if key == "contaminant":
            sections = screen_endpoints(inputs, spec, knobs, screen_process)
            if not sections:
                continue
            packed["pollutants"] = [ContaminantScreening.model_validate(
                {k: v for k, v in s.items() if k in ContaminantScreening.model_fields})
                for _, s in sections]
            packed["pollutant"] = packed["pollutants"][0]
            continue
        conc, rate = per_section[key]
        section_inputs = (inputs if conc is None
                          else replace(inputs, inlet_concentration_mg_l=conc))
        # Rateless kinds take no `rate` at all: for the particulate module that is enforced rather
        # than ignored, because a per-day coefficient there is a category error (reference rule 1).
        out = screen_process(section_inputs, spec,
                             **({} if spec.kind in ("extent", "particulate")
                                else {"rate": rate}))
        field, model = _FUNCTION_MODELS[key]
        packed[field] = model.model_validate(
            {k: v for k, v in out.items() if k in model.model_fields})
    # Every section switched off is the same state as no analysis at all, and returning an empty
    # container instead would make the report emit a screening document with nothing in it.
    return FunctionScreening(**packed) if packed else None


def screen_endpoints(inputs, spec, knobs: dict, screen_process) -> list[tuple[str, dict]]:
    """(endpoint key, screening result) per ticked dissolved endpoint, in registry order.

    PUBLIC because the pane calls it too. The pane and this module used to derive the contaminant
    section separately from the same knobs, which is exactly how a pane and a report drift apart;
    one function means the screen on the left and the document it prints cannot disagree.

    THE RATE COMES FROM THE PRESET, never from a knob. Every endpoint in the library is traceable
    to a paper, so there is no user-supplied rate left to honour and no way for a number in this
    section to outrun its citation.

    THE CONCENTRATION UNIT RESOLVES HERE, once, exactly as it does in the pane: a cited endpoint
    declares its own unit and `inlet_concentration_mg_l` always means mg/L downstream."""
    keys = ordered_keys(knobs.get("pollutant_endpoints") or DEFAULT_ENDPOINTS)
    by_key = knobs.get("contaminant_conc_by_key") or {}
    out = []
    for key in keys:
        preset = get_preset(key)
        if preset is None:
            continue
        raw = by_key.get(key, preset.concentration)
        conc = None if raw is None else preset.concentration_mg_l(raw)
        section_inputs = replace(inputs, preset_key=key, inlet_concentration_mg_l=conc)
        # NO `rate` ARGUMENT. Omitting it is what makes the preset's central value authoritative
        # (`screen_reactive` reads `preset.rate_central` when the rate is UNSET) and its published
        # triple the sweep. A stable endpoint's answer is a literal zero, which that path already
        # keeps distinct from a blank.
        out.append((key, screen_process(section_inputs, spec)))
    return out


def build_results(
    snapshot: AssessmentInputSnapshot,
    *,
    hz_stats: dict,
    streamflow_cms: float | None,
    reach_length_m: float | None,
    exchange: m.ExchangeAccounting | None,
    transit_times_days=None,
    transit_weights=None,
    mobile_pore_storage_m3: float | None = None,
    reference_area_m2: float | None = None,
    footprint_weighted_m2: float | None = None,
    streambed_area_m2: float | None = None,
    active_streambed_area_m2: float | None = None,
    return_streambed_area_m2: float | None = None,
    connected_streambed_area_m2: float | None = None,
    net_stream_exchange_cms: float | None = None,
    path_depths=None,
    path_lengths=None,
    domain_volume_m3: float | None = None,
    threshold_hours: tuple = DEFAULT_THRESHOLD_HOURS,
    custom_thresholds: list | None = None,
    function_inputs: dict | None = None,
    porosity: float | None = None,
    #: The porosity frozen in the INPUT SNAPSHOT, when the caller knows it and it may differ from
    #: `porosity` above (which must be the value the hyporheic run tracked at). Supplying it is
    #: what lets the QC pass notice the two freeze points disagreeing; it is never used to compute.
    snapshot_porosity: float | None = None,
    censored_fraction: float | None = None,
    max_tracking_time_days: float | None = None,
    hz_accounting: dict | None = None,
    group_hashes: dict | None = None,
    app_version: str | None = None,
    model_version: str | None = None,
) -> AssessmentResultsV2:
    """Compute every metric and return the immutable results model.

    hz_stats: the hyporheic-class stats from hz_analysis.class_stats (volume_m3, footprint_m2,
    thickness_mean_m/…). exchange: the flux-weighted ExchangeAccounting (§8.3) or None (in m3/s).
    streambed_area_m2 (A_bed) normalizes q_HEF and D_HZ; active_streambed_area_m2 (A_active) gives
    the connected-bed fraction; path_depths (aligned with transit_weights) drive the depth stats.
    """
    warnings: list[HypeWarning] = []
    hyp = (hz_stats or {}).get("hyporheic", {})

    # ---- the hyporheic hydraulic signature (report §5-7, §10) ----------------
    # Frequency, duration, extent and the threshold scenarios all come out of ONE derivation, in
    # `signature.derive`. They used to be computed here and re-computed, slightly differently, in
    # two places in app.py; the screening pane and this report disagreed about pore volume because
    # of it. Nothing about the arithmetic changed in the move (revision spec §27) and
    # tests/test_signature.py pins every value against the expressions that used to live here.
    sig = signature.derive(signature.SignatureInputs(
        streamflow_cms=streamflow_cms, reach_length_m=reach_length_m, porosity=porosity,
        exchange=exchange,
        transit_times_days=transit_times_days, transit_weights_m3_day=transit_weights,
        path_depths_m=path_depths, path_lengths_m=path_lengths,
        bulk_volume_m3=hyp.get("volume_m3"), footprint_binary_m2=hyp.get("footprint_m2"),
        footprint_weighted_m2=footprint_weighted_m2,
        thickness_mean_m=hyp.get("thickness_mean_m"), thickness_max_m=hyp.get("thickness_max_m"),
        streambed_area_m2=streambed_area_m2,
        active_streambed_area_m2=active_streambed_area_m2,
        return_streambed_area_m2=return_streambed_area_m2,
        connected_streambed_area_m2=connected_streambed_area_m2,
        net_stream_exchange_cms=net_stream_exchange_cms, censored_fraction=censored_fraction,
        max_tracking_time_days=max_tracking_time_days, domain_volume_m3=domain_volume_m3,
        threshold_hours=threshold_hours, custom_thresholds=tuple(custom_thresholds or ())))

    conn = ConnectivityMetrics(**signature.connectivity_fields(sig))
    rtd = ResidenceTimeMetrics(**signature.residence_fields(sig))
    zone_kw = signature.zone_fields(sig)
    # An explicitly supplied pore storage WINS over the derived one. `build_results` is a pure
    # function of its arguments and a caller that names this value means it; the derivation is
    # there so a caller that does not have to compute it no longer has to.
    if mobile_pore_storage_m3 is not None:
        zone_kw["mobile_pore_storage_m3"] = mobile_pore_storage_m3
    zone = ZoneMetrics(**zone_kw)
    thresholds = [ThresholdResult(**t) for t in signature.threshold_fields(sig)]
    have_rtd = sig.have_rtd
    if conn.unavailable_reason:
        warnings.append(HypeWarning(code="connectivity_unavailable",
                                    message=conn.unavailable_reason, severity=Severity.warning))
    # Two freeze points for one number. `porosity` is what the hyporheic run tracked at and what
    # every volume here rests on; the snapshot's is frozen earlier, at the groundwater run. They
    # are normally identical and silently differ only when the field was edited between the two,
    # which is exactly the case a reader would never think to check.
    if (snapshot_porosity is not None and porosity is not None
            and abs(float(snapshot_porosity) - float(porosity)) > 1e-9):
        warnings.append(HypeWarning(
            code="porosity_freeze_point",
            message=(f"Porosity was {porosity:g} when the hyporheic zone was delineated and "
                     f"{float(snapshot_porosity):g} when the groundwater model ran. Volumes and "
                     f"pore storage use the delineation value, which is what the particles were "
                     f"tracked at. Re-run the groundwater model to make the two agree."),
            severity=Severity.warning))

    # ---- function screening (functions plan Parts I-II) ----------------------
    # All four sections run whenever the hydraulics allow: the oxygen gate, the threshold split, the
    # R(tau) curve, habitat extent and thermal buffering all need no user chemistry at all. Only the
    # mass estimates wait on a concentration and a rate the user owns.
    #
    # UNITS: `transit_weights` arrives raw in m3/day (app.py:3503) while `exchange` is m3/s
    # (app.py:3491-3494). Pass each as-is; the screen records their consistency as a QC diagnostic
    # rather than assuming it.
    functions = _build_functions(
        function_inputs or {}, conn=conn, zone=zone, exchange=exchange,
        transit_times_days=transit_times_days, transit_weights=transit_weights,
        streamflow_cms=streamflow_cms, porosity=porosity, have_rtd=have_rtd,
        path_lengths=path_lengths, reach_length_m=reach_length_m)

    results = AssessmentResultsV2(
        assessment_id=snapshot.assessment_id, input_hash=snapshot.input_hash,
        input_snapshot=snapshot, group_hashes=group_hashes or snapshot.group_hashes(),
        connectivity=conn, residence_time=rtd, zone=zone, thresholds=thresholds,
        functions=functions, warnings=warnings,
        untested_uncertainty=["K and soil configuration", "Streamflow", "Geometry",
                              "Grid resolution", "Porosity",
                              "Thermal, chemical, and biological conditions"],
        created_at=datetime.now(timezone.utc), report_status=None)

    # ---- quality control (report §27) ----------------------------------------
    acct = dict(hz_accounting or {})
    if exchange is not None:
        acct.setdefault("mass_balance_error", exchange.mass_balance_error)
    qc_warnings, diag = validate_mod.validate_results(
        results, hz_accounting=acct, domain_volume_m3=domain_volume_m3)
    results.warnings.extend(qc_warnings)
    results.quality_diagnostics = diag
    return results


__all__ = ["build_results", "DEFAULT_THRESHOLD_HOURS"]
