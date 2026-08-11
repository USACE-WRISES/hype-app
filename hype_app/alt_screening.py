"""Functional screening across the hydraulic-alternatives sweep.

Every completed scenario is re-screened at REPORT BUILD TIME under the CURRENT functional inputs,
then folded into a min/max envelope around the Basecase. Recomputed rather than frozen at sweep
time because nitrate, dissolved oxygen, the rate, tau and the ticked endpoints all move
independently of the sweep: a frozen envelope would disagree with the Basecase headline it sits
under. Recomputing is what makes "the same assumptions for every scenario" true by construction
rather than by promise, and it keeps a nitrate edit from invalidating an 8-run MODFLOW sweep.

NOTHING HERE READS `AltScenario.metrics` OR `alternatives.metric_ranges`. The functional envelope
is never derived from the hydraulic headline min/max: those are three signature values, and a
screening mass is not a monotone function of any of them. The alternative with the greatest
exchange frequency may transform the least nitrogen if its residence times are much shorter, so
the extremes have to come from complete scenario calculations.

Disk-reading (the retained per-scenario artifacts) and Shiny-independent, so this runs in the
report worker thread. It is deliberately NOT in `alternatives.py`, which promises to stay pure.
"""
from __future__ import annotations

from pathlib import Path

from . import assess, hz_results, signature
from .contracts import (
    BASECASE_ID,
    BASECASE_LABEL,
    AltStatus,
    EnvelopeRow,
    FunctionEnvelope,
    SectionEnvelope,
)
from .functions.screen import is_numeric, resolve_row, row_specs
from .provenance import HypeWarning, Severity

ENVELOPE_METHOD_VERSION = "fn_envelope_v1"


class EnvelopeUnavailable(RuntimeError):
    """A scenario could not be screened. Carries the user-facing reason.

    RAISED RATHER THAN RETURNED so complete-or-none is structural: no caller can accidentally fold
    a short set, because the partial list never escapes the loop that built it. A silently short
    envelope would carry the same "every scenario" sentence over a different set of runs, and that
    sentence is the whole claim the feature makes."""


# --------------------------------------------------------------------------- artifacts

def scenario_dir(work_dir, scenario) -> Path:
    """`<work_dir>/<rel_dir>` for one scenario. Legacy manifests wrote no `rel_dir`, so fall back
    to the folder convention the sweep has always used."""
    rel = getattr(scenario, "rel_dir", "") or f"alternatives/{scenario.id}"
    return Path(work_dir) / rel


def _artifacts_present(sdir: Path) -> bool:
    hz = hz_results.hz_dir_for(sdir)
    return (hz / "hz_stats.json").is_file() and (hz / "hz_flux.npz").is_file()


def envelope_available(manifest, *, work_dir) -> tuple[bool, str]:
    """(can an envelope be built, why not).

    ONE ANSWER FOR BOTH SURFACES, in the shape `_report_status` already uses: the checkbox on the
    report.fn pane and the build decision in the worker ask the same question, and two conditions
    that can disagree is how a ticked box comes to produce nothing.

    This is the CHEAP check. It stats the artifacts rather than loading them, because a reactive
    pane render may not do eight npz loads. Actually reading them is the build's job, and the
    build raises `EnvelopeUnavailable` if one does not reopen.

    `manifest.completed()` is tested INDEPENDENTLY of `is_partial()`: `is_partial` is
    `any(status != completed)`, and `any([])` is False, so an empty scenario list is "not partial"
    and would otherwise sail through."""
    if manifest is None:
        return False, "Run the hydraulic alternatives sweep to add a range across alternatives."
    if not manifest.scenarios:
        return False, "The sweep recorded no scenarios."
    if not manifest.completed():
        return False, "No alternative runs completed."
    if manifest.is_partial():
        return False, "The sweep is incomplete. A range needs every alternative."
    for s in manifest.completed():
        if not _artifacts_present(scenario_dir(work_dir, s)):
            return False, "One or more alternative runs no longer have their saved results."
    return True, ""


# --------------------------------------------------------------------------- one scenario

def scenario_results(sdir, *, snapshot, reach_length_m, function_inputs=None,
                     app_version=None):
    """The full `AssessmentResultsV2` for ONE retained alternative run directory.

    THE SHARED ADAPTER. Both the sweep (which keeps the three hydraulic sections) and the envelope
    (which keeps `functions`) come through here, so a scenario cannot report one set of numbers to
    the Hydraulic Alternatives table and a differently-derived set to the screening envelope.
    Calling the same `build_results` is not enough on its own to guarantee that: the porosity
    fallback, the transit-weight units, the exchange conversion and the reach length are all
    per-scenario preprocessing, and duplicating them is exactly how two callers drift.

    POROSITY FOLLOWS THE RUN, not the snapshot: `hz_stats["knobs"]["porosity"]` with the snapshot
    as fallback. The sweep never varies porosity, so in practice every scenario reads the same
    value. Reading it per run is what makes that a fact rather than an assumption.

    Returns None when the run directory has no stats to read."""
    sdir = Path(sdir)
    hz_dir = hz_results.hz_dir_for(sdir)
    full_stats = hz_results.stats(hz_dir)
    if not full_stats:
        return None
    cls_stats = full_stats.get("classes") or full_stats or {}
    hyp = cls_stats.get("hyporheic") or {}
    acct = (full_stats.get("flux") or {}).get("accounting") or {}
    net_exch = acct.get("net_stream_exchange")
    domain_vol = (full_stats.get("domain") or {}).get("active_saturated_volume_m3")
    # transit_rows=False: the per-particle row list is only read by the Basecase transit CSV and
    # the RTD figure. Building it per scenario allocates a dict per particle for nothing.
    fm = hz_results.flux_metrics(full_stats, hz_dir, transit_rows=False)
    porosity = signature.as_float((full_stats.get("knobs") or {}).get("porosity"))
    if porosity is None:
        porosity = snapshot.k.porosity
    return assess.build_results(
        snapshot, hz_stats=cls_stats, streamflow_cms=snapshot.streamflow.value_cms,
        reach_length_m=reach_length_m, exchange=fm["exchange"],
        transit_times_days=fm["transit_times"], transit_weights=fm["transit_weights"],
        path_depths=fm["path_depths"], path_lengths=fm["path_lengths"],
        footprint_weighted_m2=hyp.get("footprint_m2"), porosity=porosity,
        snapshot_porosity=snapshot.k.porosity,
        censored_fraction=fm["censored"],
        streambed_area_m2=acct.get("streambed_area_m2"),
        active_streambed_area_m2=acct.get("active_streambed_area_m2"),
        return_streambed_area_m2=acct.get("return_streambed_area_m2"),
        connected_streambed_area_m2=acct.get("connected_streambed_area_m2"),
        net_stream_exchange_cms=(net_exch / 86400.0 if net_exch is not None else None),
        domain_volume_m3=domain_vol, hz_accounting=acct,
        function_inputs=function_inputs,
        app_version=app_version)


def scenario_functions(manifest, *, work_dir, snapshot, function_inputs, reach_length_m,
                       app_version=None) -> list[tuple[str, str, object]]:
    """(scenario id, label, FunctionScreening) per completed scenario, in manifest order.

    Raises `EnvelopeUnavailable` on the first scenario whose artifacts do not reopen or that
    produced nothing to screen, naming the scenario so the report can say which one."""
    out: list[tuple[str, str, object]] = []
    for s in manifest.scenarios:
        if s.status != AltStatus.completed:
            continue
        try:
            res = scenario_results(scenario_dir(work_dir, s), snapshot=snapshot,
                                   reach_length_m=reach_length_m,
                                   function_inputs=function_inputs, app_version=app_version)
        except Exception as e:  # noqa: BLE001 - the reason travels to the reader
            raise EnvelopeUnavailable(
                f"{s.label} could not be re-screened. {type(e).__name__}: {e}") from e
        if res is None:
            raise EnvelopeUnavailable(
                f"{s.label} no longer has its saved results, so it could not be re-screened.")
        if res.functions is None:
            raise EnvelopeUnavailable(
                f"{s.label} produced no flow paths or zone volume to screen.")
        # `res.warnings` is DROPPED on purpose: `build_results` appends a porosity freeze-point
        # warning on every call, and merging one per scenario would print the same line nine times
        # under Warnings and limitations.
        out.append((s.id, s.label, res.functions))
    return out


# --------------------------------------------------------------------------- row vocabulary
#
# `row_specs` / `resolve_row` / `is_numeric` live in `functions.screen` beside the canonical maps
# they read. THE REPORT'S HEADLINE CARD RESOLVES THROUGH THE SAME THREE, so the number printed above
# a range and the number the range was folded around cannot come from different rows.

# --------------------------------------------------------------------------- the fold

def _section_models(functions) -> list[tuple[str, str, object]]:
    """(section key, process key, model) for every screened section of one FunctionScreening.

    The section key is minted by `report.function_section_key`, the same call `function_sections`
    makes, so the envelope joins the document by construction. THIS IS ALSO WHAT KEEPS POLLUTANT
    ENDPOINTS SEPARATE: the key carries the endpoint, so two chemicals' masses have no expression
    here in which they could meet."""
    from . import report
    out: list[tuple[str, str, object]] = []
    if functions is None:
        return out
    for process_key, field in (("denitrification", "nutrient"), ("habitat", "habitat"),
                               ("thermal_regulation", "thermal")):
        model = getattr(functions, field, None)
        if model is not None:
            out.append((report.function_section_key(field, model), process_key, model))
    for p in (functions.pollutants or []):
        out.append((report.function_section_key("pollutant", p), "contaminant", p))
    return out


def _fold_row(spec, cases) -> tuple[EnvelopeRow | None, list[str]]:
    """(row, labels of cases that carried no finite value) for one registry row spec.

    `cases` is [(case_id, case_label, model)] with the BASECASE FIRST, which is what makes
    `lo <= base <= hi` hold by construction: an envelope can never print a range its own headline
    sits outside. Ties keep the first case, so attribution is deterministic rather than dependent
    on iteration order.

    The row is None when it resolves nowhere, or when its name or unit is not identical across
    every case: a row whose meaning changed between realizations is not a range, and folding it
    would put two different quantities under one label."""
    lo = hi = base = None
    lo_id = hi_id = BASECASE_ID
    lo_lab = hi_lab = BASECASE_LABEL
    key = name = unit = None
    n = 0
    missing: list[str] = []
    for i, (cid, clabel, model) in enumerate(cases):
        r = resolve_row(spec, model)
        if r is None:
            missing.append(clabel)
            continue
        k, nm, un = r
        if key is None:
            key, name, unit = k, nm, un
        elif (k, nm, un) != (key, name, unit):
            return None, []             # incomparable across cases
        v = getattr(model, k, None)
        if not is_numeric(v):
            missing.append(clabel)
            continue
        v = float(v)
        if i == 0:
            base = v
        if lo is None or v < lo:
            lo, lo_id, lo_lab = v, cid, clabel
        if hi is None or v > hi:
            hi, hi_id, hi_lab = v, cid, clabel
        n += 1
    if key is None or lo is None or hi is None:
        return None, missing
    return EnvelopeRow(key=key, name=name, unit=unit,
                       kind=getattr(spec, "kind", "num"), digits=getattr(spec, "digits", 3),
                       base=base, lo=lo, hi=hi,
                       lo_case=lo_lab, hi_case=hi_lab,
                       lo_case_id=lo_id, hi_case_id=hi_id, n=n), missing


def fold(base_functions, scenarios) -> FunctionEnvelope | None:
    """Min/max per (section, result key) over the Basecase plus every scenario.

    THE PRIMARY IS COMPLETE OR THE SECTION IS WITHHELD. A primary row must resolve to a finite
    number in every case or that section carries a `withheld_reason` instead of a range: skipping
    None the way the hydraulic ranges do would manufacture a narrow envelope that looks complete.
    Supporting rows may be short, and the report discloses "n of N cases" for them.

    `scenarios` is [(id, label, FunctionScreening)]."""
    if base_functions is None:
        return None
    base_sections = _section_models(base_functions)
    if not base_sections:
        return None
    case_ids = [BASECASE_ID] + [sid for sid, _, _ in scenarios]
    case_labels = [BASECASE_LABEL] + [lab for _, lab, _ in scenarios]
    n_cases = len(case_ids)

    per_scenario = [(sid, lab, {k: mdl for k, _, mdl in _section_models(fns)})
                    for sid, lab, fns in scenarios]

    sections: list[SectionEnvelope] = []
    for skey, process_key, base_model in base_sections:
        cases = [(BASECASE_ID, BASECASE_LABEL, base_model)]
        missing: list[str] = []
        for sid, lab, by_key in per_scenario:
            m = by_key.get(skey)
            if m is None:
                missing.append(lab)
            else:
                cases.append((sid, lab, m))
        title = getattr(base_model, "process_label", "") or skey
        if missing:
            sections.append(SectionEnvelope(
                key=skey, title=title,
                withheld_reason=(f"No range across alternatives. {_join(missing)} did not screen "
                                 f"this estimate, so the cases are not comparable.")))
            continue
        primary_spec, support_specs = row_specs(process_key)
        primary, gaps = ((None, []) if primary_spec is None
                         else _fold_row(primary_spec, cases))
        # THE BASECASE HAVING NO VALUE IS NOT A WITHHELD ENVELOPE. A section that reports no mass
        # at all (no concentration entered, so the estimate is rate-free) has no headline for a
        # range to sit under, and printing "no envelope" beside it would be noise about a number
        # the reader was never shown. Only a Basecase value that the SCENARIOS could not match is
        # a gap worth naming.
        if primary is None or primary.base is None:
            continue
        if primary.n < n_cases:
            sections.append(SectionEnvelope(
                key=skey, title=title,
                withheld_reason=(f"No range across alternatives. {_join(gaps)} produced no value "
                                 f"for this estimate, so the cases are not comparable.")))
            continue
        support: list[EnvelopeRow] = []
        seen = {primary.key}
        for rs in support_specs:
            row, _ = _fold_row(rs, cases)
            if row is None or row.key in seen:
                continue
            seen.add(row.key)
            support.append(row)
        sections.append(SectionEnvelope(key=skey, title=title, primary=primary,
                                        supporting=support))
    return FunctionEnvelope(method_version=ENVELOPE_METHOD_VERSION,
                            alternative_count=len(scenarios), case_count=n_cases,
                            case_ids=case_ids, case_labels=case_labels, sections=sections)


def _join(items) -> str:
    """Human list without an Oxford-comma dependency, and never a semicolon."""
    items = list(items)
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


# --------------------------------------------------------------------------- the whole job

def build_envelope(manifest, *, work_dir, snapshot, base_functions, function_inputs,
                   reach_length_m, app_version=None):
    """(envelope, warning). The whole job: gate, screen every scenario, fold.

    COMPLETE OR NONE, AND NEVER SILENT. A scenario that fails mid-build withholds the WHOLE
    envelope and returns a warning naming the scenario and the reason, which the report prints
    under Warnings and limitations. The report build is off-loop and the user gets no
    notification, so a ticked box that produced nothing would otherwise be unexplainable."""
    ok, reason = envelope_available(manifest, work_dir=work_dir)
    if not ok:
        return None, HypeWarning(code="function_envelope_unavailable",
                                 message=f"No range across hydraulic alternatives. {reason}",
                                 severity=Severity.warning)
    try:
        scenarios = scenario_functions(manifest, work_dir=work_dir, snapshot=snapshot,
                                       function_inputs=function_inputs,
                                       reach_length_m=reach_length_m, app_version=app_version)
    except EnvelopeUnavailable as e:
        return None, HypeWarning(code="function_envelope_unavailable",
                                 message=f"No range across hydraulic alternatives. {e}",
                                 severity=Severity.warning)
    env = fold(base_functions, scenarios)
    if env is None:
        return None, HypeWarning(
            code="function_envelope_unavailable",
            message=("No range across hydraulic alternatives. The Basecase has no screening "
                     "results to compare the alternatives against."),
            severity=Severity.warning)
    return env, None


__all__ = [
    "ENVELOPE_METHOD_VERSION", "EnvelopeUnavailable",
    "scenario_dir", "envelope_available", "scenario_results", "scenario_functions",
    "fold", "build_envelope",
]
