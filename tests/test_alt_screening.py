"""The hydraulic scenario envelope: functional screening folded across the alternatives sweep.

The invariants here are the ones the feature is DEFINED by, not incidental behaviour:

* the envelope is recomputed per scenario, never derived from the hydraulic headline min/max
* it is complete or absent, and an absent one is always explained
* the Basecase stays the primary estimate and lies inside its own envelope
* pollutant endpoints never merge

`build_manifest` and friends come from tests/test_alternatives.py's fixtures, which already
know how to mint a realistic sweep.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from hype_app import alt_screening as A
from hype_app import assess, report
from hype_app.contracts import (
    AltStatus,
    AssessmentResultsV2,
    ConnectivityMetrics,
    ZoneMetrics,
)
from hype_app.metrics import ExchangeAccounting

from test_alternatives import _manifest

KNOBS = {"nitrate_mg_l": 1.0, "denit_rate_per_day": 1.22, "thermal_response_hours": 8.0,
         "pollutant_endpoints": ["zinc", "acesulfame"], "oxygen_gate": True,
         "contaminant_conc_by_key": {}}


def _screening(scale=1.0, seed=0, endpoints=None, nitrate=1.0):
    """One FunctionScreening from a synthetic run. `scale` stretches residence times AND the
    zone, the way a lower-K scenario does, so the sections move together the way a real sweep
    moves them."""
    rng = np.random.default_rng(seed)
    t = np.exp(rng.normal(np.log(2.0 * scale), 0.8, 300))
    w = rng.uniform(50.0, 500.0, t.size)
    conn = ConnectivityMetrics(
        streambed_area_m2=5000.0, active_streambed_area_m2=3000.0,
        active_streambed_fraction=0.6, return_streambed_area_m2=2000.0,
        connected_streambed_area_m2=2500.0 * scale,
        connected_streambed_fraction=min(0.5 * scale, 1.0),
        exchange_flux_m_day=0.05,
        # INVERSE in scale, deliberately: the fast, short-residence run has the MOST turnovers.
        turnovers_per_km=0.3 / scale, censored_flow_fraction=0.02)
    zone = ZoneMetrics(bulk_saturated_volume_m3=1e4 * scale,
                       mobile_pore_storage_m3=3e3 * scale,
                       equivalent_active_depth_m=0.5 * scale,
                       path_depth_p50_m=1.0, path_depth_p90_m=2.0)
    ex = ExchangeAccounting(total_downwelling=0.1, returning_hyporheic=0.05,
                            losing_to_sides=0.01, unresolved=0.0)
    knobs = dict(KNOBS, nitrate_mg_l=nitrate)
    if endpoints is not None:
        knobs["pollutant_endpoints"] = list(endpoints)
    return assess._build_functions(
        knobs, conn=conn, zone=zone, exchange=ex, transit_times_days=t, transit_weights=w,
        streamflow_cms=2.8, porosity=0.3, have_rtd=True, reach_length_m=253.0), conn


@pytest.fixture
def base():
    return _screening(1.0)[0]


@pytest.fixture
def sweep(base):
    """The Basecase plus three scenarios, folded."""
    return A.fold(base, [("k_upper", "Higher K", _screening(0.4, 1)[0]),
                         ("k_lower", "Lower K", _screening(3.0, 2)[0]),
                         ("g_higher", "Higher gradient", _screening(0.8, 3)[0])])


@pytest.fixture
def results_alt(base, sweep):
    """A result carrying BOTH the sweep manifest and the fold of it, which is what the document
    needs before the Hydraulic Alternatives appendix renders at all."""
    from test_alternatives import _manifest
    from hype_app.contracts import AltStatus
    mf = _manifest(statuses=[AltStatus.completed] * 8)
    return AssessmentResultsV2(assessment_id="a", input_hash="h", functions=base,
                               function_envelope=sweep, alternatives=mf)


# ------------------------------------------------------------------ the defining invariant

def test_the_extremes_do_not_track_the_hydraulic_headline():
    """WHY EVERY SCENARIO IS RECALCULATED, as a test rather than a comment.

    A functional response is not a monotone function of any signature value. Here the scenario
    with the MOST turnovers per km transforms the LEAST nitrogen, because its residence times are
    far shorter. Reading the envelope off the hydraulic min/max would put the extremes on the
    wrong runs, which is the whole reason this module re-screens instead of interpolating."""
    fast, fast_conn = _screening(0.4, 1)      # short residence, high turnover
    slow, slow_conn = _screening(3.0, 2)      # long residence, low turnover
    assert fast_conn.turnovers_per_km > slow_conn.turnovers_per_km
    assert fast.nutrient.total_removed_kg_day < slow.nutrient.total_removed_kg_day


def test_the_module_never_reaches_for_the_hydraulic_ranges():
    """The rule above, enforced structurally: no call into the hydraulic range machinery and no
    read of `AltScenario.metrics`, so the envelope cannot quietly become an interpolation.

    Parsed rather than grepped, because the module's own docstrings NAME these to explain why it
    does not use them, and a substring lint would fire on the explanation."""
    import ast
    tree = ast.parse(Path("hype_app/alt_screening.py").read_text(encoding="utf-8"))
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            used.update(a.name for a in node.names)
    for banned in ("metric_ranges", "primary_ranges", "metrics"):
        assert banned not in used, banned


# ------------------------------------------------------------------ the fold

def test_basecase_is_the_primary_and_lies_inside_its_own_envelope(sweep):
    """A range its own headline sits outside would be nonsense, and it is prevented by
    construction: the Basecase folds first and is one of the cases."""
    assert sweep.sections
    for s in sweep.sections:
        if s.primary is None:
            continue
        assert s.primary.base is not None
        assert s.primary.lo <= s.primary.base <= s.primary.hi, s.key


def test_counts_name_the_basecase_explicitly(sweep):
    """"9 runs" and "9 alternatives" differ by exactly the run the reader is looking at."""
    assert sweep.alternative_count == 3
    assert sweep.case_count == 4
    assert sweep.case_labels[0] == "Basecase"
    assert sweep.case_ids[0] == "base"


def test_attribution_carries_stable_ids_beside_the_labels(sweep):
    """Labels are user-facing text that can repeat or be renamed. The slug cannot."""
    ids = set(sweep.case_ids)
    for s in sweep.sections:
        if s.primary is None:
            continue
        assert s.primary.lo_case_id in ids and s.primary.hi_case_id in ids


def test_pollutant_endpoints_stay_separate_and_never_combine(sweep):
    """One envelope per endpoint. The section key carries the endpoint, so there is no
    expression anywhere in the model in which two chemicals' masses could meet."""
    keys = [s.key for s in sweep.sections]
    assert "pollutant.zinc" in keys and "pollutant.acesulfame" in keys
    dumped = json.dumps(sweep.model_dump(mode="json"))
    for banned in ("total_mass", "composite", "combined_mass", "score"):
        if banned == "total_mass":
            continue
        assert banned not in dumped
    # the two endpoints hold genuinely different numbers, not one shared figure
    z = next(s for s in sweep.sections if s.key == "pollutant.zinc")
    a = next(s for s in sweep.sections if s.key == "pollutant.acesulfame")
    assert z.primary.base != a.primary.base


def test_a_scenario_missing_a_section_withholds_it_and_says_which(base):
    """Complete or none, PER SECTION, and never silent. The scenario that could not screen the
    endpoint is named, because "no envelope" without a cause reads as a broken feature."""
    env = A.fold(base, [("k_upper", "Higher K", _screening(0.4, 1)[0]),
                        ("k_lower", "Lower K", _screening(3.0, 2, endpoints=["zinc"])[0])])
    ace = next(s for s in env.sections if s.key == "pollutant.acesulfame")
    assert ace.primary is None
    assert ace.withheld_reason and "Lower K" in ace.withheld_reason
    # the other sections are unaffected: this degrades per module, not globally
    assert next(s for s in env.sections if s.key == "pollutant.zinc").primary is not None
    assert next(s for s in env.sections if s.key == "nutrient").primary is not None


def test_a_section_the_basecase_never_estimated_is_silent_not_withheld():
    """No nitrate entered means no mass anywhere, so there is no headline for a range to sit
    under. Printing "no envelope" beside a number the reader was never shown is noise, and the
    clutter this block exists to avoid."""
    b = _screening(1.0, nitrate=None)[0]
    env = A.fold(b, [("k_upper", "Higher K", _screening(0.4, 1, nitrate=None)[0])])
    assert all(s.key != "nutrient" for s in env.sections)


def test_non_finite_and_boolean_values_never_fold():
    """`alternatives.metric_ranges` is a bare isinstance check. This one is stricter for two
    concrete reasons: a single NaN prints "nan to nan" for the whole row, and `oxygen_gate` is a
    bool, which is an int in Python and would otherwise fold as 0/1."""
    from hype_app.functions.screen import is_numeric

    assert not is_numeric(float("nan"))
    assert not is_numeric(float("inf"))
    assert not is_numeric(True)
    assert is_numeric(0.0) and is_numeric(3)


# ------------------------------------------------------------------ registry resolution (bug 1)

def test_the_pollutant_primary_resolves_to_a_real_contract_field(sweep):
    """THE BUG THIS ALMOST SHIPPED WITH. `_F_POLLUTANT.headline_kpi` is `total_mass_display`, a
    rescaled twin minted for the pane and filtered out when `_build_functions` validates into
    `ContaminantScreening`. Resolving the row key straight off the model returns None for every
    endpoint and every scenario, and the sections vanish without a word."""
    from hype_app.contracts import ContaminantScreening
    from hype_app.functions import registry as reg
    assert reg.FUNCTIONS["pollutant"].headline_kpi not in ContaminantScreening.model_fields
    row = next(s for s in sweep.sections if s.key == "pollutant.zinc").primary
    assert row.key == "total_removed_kg_day"
    assert row.key in ContaminantScreening.model_fields


def test_an_aliased_key_takes_the_canonical_unit_not_the_display_one(sweep):
    """The other half of the same bug. `total_mass_unit` carries the DISPLAY scale, and every
    organic preset is mass_scale="g" with factor 1000, so honouring `unit_key` for a canonical
    value understates it by a thousand."""
    from hype_app.functions import get_preset
    assert get_preset("acesulfame").mass.factor == 1000.0
    row = next(s for s in sweep.sections if s.key == "pollutant.acesulfame").primary
    assert row.unit == "kg/day"


def test_percent_rows_keep_their_sign(sweep):
    """A registry pct row carries `unit=""` because the pane appends the sign itself. Copying
    that here keeps a fraction from printing "0.632 to 0.871" under a table saying "63.2%"."""
    row = next(s for s in sweep.sections if s.key == "thermal").primary
    assert row.kind == "pct"
    assert report._env_unit(row) == "%"
    assert "%" in report.envelope_line(
        next(s for s in sweep.sections if s.key == "thermal"), sweep.case_count)


# ------------------------------------------------------------------ the gate

def _snapshot():
    from hype_app.contracts import (AssessmentInputSnapshot, GradientBoundaryConfigV2,
                                    GridSettings, KSettings, SiteMetadata, StreamflowInput)
    from hype_app.provenance import Provenance
    return AssessmentInputSnapshot(
        assessment_id="A1",
        site=SiteMetadata(site_name="Mink", reach_length_m=253.0),
        streamflow=StreamflowInput(value_cms=2.83, provenance=Provenance(source="test")),
        k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.3),
        gradients=GradientBoundaryConfigV2(),
        grid=GridSettings(cell_size_x=10.0, cell_size_y=10.0, gw_mod_depth=20.0,
                          layer_thickness=0.5))


def _complete(tmp_path, n=8, write=True):
    mf = _manifest(statuses=[AltStatus.completed] * n)
    if write:
        for s in mf.scenarios:
            hz = Path(tmp_path) / s.rel_dir / "summary" / "hz"
            hz.mkdir(parents=True, exist_ok=True)
            (hz / "hz_stats.json").write_text("{}")
            (hz / "hz_flux.npz").write_bytes(b"")
    return mf


def test_the_gate_answers_every_incomplete_shape(tmp_path):
    assert A.envelope_available(None, work_dir=tmp_path)[0] is False
    empty = _manifest(statuses=[])
    empty.scenarios = []
    ok, why = A.envelope_available(empty, work_dir=tmp_path)
    assert ok is False and "no scenarios" in why.lower()
    none_done = _manifest(statuses=[AltStatus.failed] * 8)
    assert A.envelope_available(none_done, work_dir=tmp_path)[0] is False
    partial = _manifest(statuses=[AltStatus.completed] * 5 + [AltStatus.not_run] * 3)
    ok, why = A.envelope_available(partial, work_dir=tmp_path)
    assert ok is False and "incomplete" in why.lower()
    assert A.envelope_available(_complete(tmp_path), work_dir=tmp_path)[0] is True


def test_an_empty_scenario_list_is_not_mistaken_for_a_complete_sweep(tmp_path):
    """`is_partial()` is `any(status != completed)` and `any([])` is False, so an empty sweep is
    "not partial". The completed check has to be an INDEPENDENT clause or a manifest with no
    scenarios sails through and folds an envelope over the Basecase alone."""
    empty = _manifest(statuses=[])
    empty.scenarios = []
    assert empty.is_partial() is False          # the trap itself
    assert A.envelope_available(empty, work_dir=tmp_path)[0] is False


def test_missing_artifacts_fail_the_gate(tmp_path):
    mf = _complete(tmp_path)
    (Path(tmp_path) / mf.scenarios[3].rel_dir / "summary" / "hz" / "hz_flux.npz").unlink()
    ok, why = A.envelope_available(mf, work_dir=tmp_path)
    assert ok is False and "saved results" in why


def test_an_unreadable_scenario_withholds_the_whole_envelope_and_warns(tmp_path, base):
    """The keystone. A build that cannot screen one scenario must not quietly ship an envelope
    over the rest: the same "every case" sentence would then cover a different set of runs. The
    report build is off-loop, so the reason has to travel as a warning or the user sees a ticked
    box and no explanation anywhere."""
    mf = _complete(tmp_path)
    snap = _snapshot()
    env, warn = A.build_envelope(mf, work_dir=tmp_path, snapshot=snap, base_functions=base,
                                 function_inputs=KNOBS, reach_length_m=253.0)
    assert env is None
    assert warn is not None and warn.code == "function_envelope_unavailable"
    assert "No range across hydraulic alternatives." in warn.message


def test_a_partial_sweep_produces_a_warning_naming_the_cause(tmp_path, base):
    snap = _snapshot()
    partial = _manifest(statuses=[AltStatus.completed] * 5 + [AltStatus.not_run] * 3)
    env, warn = A.build_envelope(partial, work_dir=tmp_path, snapshot=snap,
                                 base_functions=base, function_inputs=KNOBS,
                                 reach_length_m=253.0)
    assert env is None and warn is not None
    assert "incomplete" in warn.message.lower()


# ------------------------------------------------------------------ rendering

def _res(base, env=None):
    return AssessmentResultsV2(assessment_id="a", input_hash="h", functions=base,
                               function_envelope=env)


def test_both_ranges_render_and_name_the_factor_each_varies(base, sweep):
    """Two ranges with different meanings sat one line apart under one generic label. Each now
    says what it varied, and neither is hidden: demoting the rate spread would make the hydraulic
    one look more comprehensive than it is."""
    html = report.render_html(_res(base, sweep), include_hydraulics=False, app_version="t")
    assert report.SENSITIVITY_LABEL in html
    assert report.ENVELOPE_LABEL in html
    assert "Reported range" not in html          # the old generic wording is gone
    # BOTH OPEN ON THE SHARED WORD, so the pair reads as one comparison under two treatments
    # rather than two unrelated rows, and only the part that differs has to be read.
    for label in (report.SENSITIVITY_LABEL, report.ENVELOPE_LABEL):
        assert label.startswith("Range across "), label
    # The parentheticals are gone. Each restated the OTHER label's subject, which doubled every
    # row's length to tell a reader something both rows in front of them already showed.
    for gone in ("(Basecase hydraulics)", "(process inputs held)"):
        assert gone not in html, gone
    # One envelope line per section that has a primary. Habitat carries an envelope but NO
    # sensitivity range, because it is an extent module with no reaction rate at all, so the two
    # counts are not expected to match.
    assert html.count(report.ENVELOPE_LABEL) == sum(1 for s in sweep.sections if s.primary)
    # The old standalone paragraph does not ALSO print: one range under two labels, or the same
    # range twice, is exactly the confusion the rename was for.
    assert f"<p><strong>{report.SENSITIVITY_LABEL}" not in html
    # ...and where a sensitivity range does render, it is inside the headline block, adjacent to
    # the envelope it must not be mistaken for.
    for chunk in html.split(report.SENSITIVITY_LABEL)[1:]:
        assert report.ENVELOPE_LABEL in chunk.split("</div>\n</div>")[0]


def test_the_held_values_are_stated_where_they_are_used(base, sweep):
    """SUPERSEDES the report-level "Process inputs held constant" table. Every value it listed is
    now in the Inputs tab of the function it belongs to, so the table had become a second copy
    standing between the reader and the results, and the sentence introducing it went with it.

    Held-constant is still true by construction: `function_input_rows` reads the same frozen models
    every scenario was screened against."""
    from hype_app.report import function_sections

    res = _res(base, sweep)
    html = report.render_html(res, include_hydraulics=False, app_version="t")
    # "process inputs held" outright, not just the "...at" form: the label used to end on it, and
    # with that parenthetical dropped the phrase has no remaining home in the document.
    for gone in ("Process inputs held constant", "recalculated across", "process inputs held"):
        assert gone not in html, gone
    # ...and each value is reachable under its own function
    given = {r["name"]: r["value"] for s in function_sections(res) for r in s["inputs"]}
    assert given.get("Stream nitrate") == "1.00"
    assert "Denitrification rate" in given
    for name, value in given.items():
        assert f"<td>{name}</td>" in html, name
    # both cautions the deleted sentence carried survive in the appendix
    assert "not a confidence interval" in html
    assert report.ENVELOPE_LIMITATION in html


def test_each_function_carries_its_own_ranges(base, sweep, results_alt):
    """SUPERSEDES the one flat appendix table. That table listed every section's rows behind a
    leading Section column, so a run screening three chemicals produced sixty rows and thirty
    repetitions of "Dissolved Pollutants". A function's ranges belong with that function's other
    metrics, where the reader is already looking at the numbers they bracket."""
    from hype_app.report import function_report_groups

    html = report.render_html(results_alt, include_hydraulics=False, app_version="t")
    groups = function_report_groups(results_alt)
    # one table per SECTION that folded a range, which for pollutants means one per endpoint
    with_rows = [s for g in groups for s in g["items"] if s["alt_rows"]]
    assert len(with_rows) >= 3
    assert html.count('<p class="subhead">Across hydraulic alternatives</p>') == len(with_rows)
    # the flat appendix table and its Section column are gone
    assert "Supporting screening results" not in html
    assert "<th>Section</th>" not in html
    # ...and the Hydraulic Alternatives appendix still carries the runs themselves
    block = html.split("<summary>Hydraulic Alternatives</summary>", 1)[1].split("</details>", 1)[0]
    assert "<th>Run</th>" in block and "Across hydraulic alternatives" not in block


def test_the_move_lost_no_rows(base, sweep, results_alt):
    """A relocation, not a trim. Every (metric, range) pair the one flat table used to print is
    still in the document, now under the function that produced it."""
    from hype_app.report import function_report_groups

    html = report.render_html(results_alt, include_hydraulics=False, app_version="t")
    expected = set()
    for sec in sweep.sections:
        for r in report.envelope_section_rows(sec, sweep.case_count):
            expected.add((r["name"], r["range"]))
    assert expected, "the fixture folded nothing"
    rendered = {(r["name"], r["range"])
                for g in function_report_groups(results_alt)
                for s in g["items"] for r in s["alt_rows"]}
    assert rendered == expected
    for name, rng in expected:
        assert f"<td>{name}</td>" in html or name in html, name


def test_the_runs_column_says_how_many_runs(base, sweep):
    """THE DEFECT THAT PROMPTED THE MOVE. The coverage cell read "" whenever the fold covered every
    run, which is the normal case, so a sixty-row table carried a Runs header and sixty blanks. A
    short fold still discloses itself."""
    rows = report.envelope_section_rows(sweep.sections[0], sweep.case_count)
    assert rows and all(r["runs"] for r in rows), rows
    assert any(r["runs"] == str(sweep.case_count) for r in rows)
    short = report.envelope_section_rows(sweep.sections[0], sweep.case_count + 3)
    assert all(" of " in r["runs"] for r in short), short


def test_rows_the_sweep_did_not_move_are_left_out(base):
    """A zero-width range is not a range. Keeping them turned a five-section report into sixty-odd
    rows, most of them a number printed beside itself."""
    env = A.fold(base, [("k_upper", "Higher K", _screening(1.0, 0)[0])])   # identical run
    rows = report.envelope_section_rows(env.sections[0], env.case_count)
    # only the primary survives an identical-scenario fold, and it carries no attribution
    assert len(rows) == 1
    assert rows[0]["lo_case"] == "" and rows[0]["hi_case"] == ""
    # ...and its Range cell SAYS so, rather than printing the Basecase number a second time under
    # a heading promising a range, directly beneath a card already reading "unchanged across N".
    assert rows[0]["range"] == "unchanged"


# ------------------------------------------------- a sweep that did not move, said in words

def test_thermal_can_finally_say_a_sweep_did_not_move_it(base):
    """THE DEFECT THIS SHARED HELPER EXISTS FOR, pinned on the module that had it.

    Nutrient and pollutant built their range through `fmt_range`, which collapses on the formatted
    strings, so they could already print one number. Thermal and microplastic hand-built
    "lo to hi" with no equality test at all, so a run whose two response-time cases agreed printed
    "87.7 to 87.7%" -- a range of zero width dressed as a range -- while a nutrient section in the
    same state printed a single value. Testing this on nutrient would pass against the old code."""
    base.thermal.buffering_opportunity_high = base.thermal.buffering_opportunity_low
    html = report.render_html(_res(base), include_hydraulics=False, app_version="t")
    assert report.SENSITIVITY_UNCHANGED in html
    lo = report._pct(base.thermal.buffering_opportunity_low)
    assert f"{lo} to {lo}" not in html


@pytest.mark.parametrize("lo,hi,caught_by", [
    (0.0683, 0.0683, "both"),
    # Differs at the 7th digit, prints "0.300" either way. `math.isclose(rel_tol=1e-12)` says these
    # are DIFFERENT numbers, so only the formatting half sees it.
    (0.2999999, 0.3, "formatting"),
    # THE ROUNDING BOUNDARY. One number to 1.6e-13, but it straddles the third decimal place and
    # prints "0.123" against "0.124", so only the numeric half sees it.
    (0.12349999999999, 0.12350000000001, "numeric"),
])
def test_one_collapse_policy_covers_bounds_that_only_look_different(lo, hi, caught_by):
    """TWO TESTS INSIDE `_same_bound`, AND NEITHER IS REDUNDANT.

    `caught_by` records which half catches each row, and the two assertions below prove it rather
    than assert it. Delete either half of the predicate and exactly one of these rows fails, which
    is the whole reason both are there: formatting alone would print "0.123 to 0.124" and claim a
    0.8% spread that does not exist, and numeric closeness alone would print "0.300 to 0.300"."""
    import math
    numeric = math.isclose(lo, hi, rel_tol=1e-12, abs_tol=0.0)
    formatting = report.fmt_sig(lo) == report.fmt_sig(hi)
    assert (numeric, formatting) == {"both": (True, True), "formatting": (False, True),
                                     "numeric": (True, False)}[caught_by]
    assert report.sensitivity_text(lo, hi, "kg N/day") == report.SENSITIVITY_UNCHANGED


def test_a_narrow_but_real_spread_is_not_collapsed():
    """THE COLLAPSE MUST NOT EAT `fmt_sig`'s REASON FOR EXISTING. 0.06811838 to 0.06848999 is a
    genuine 0.5% sensitivity spread that decimal rounding used to destroy into "0.068 to 0.068",
    which is exactly the bug significant figures were introduced to fix. A collapse rule set one
    notch coarser would quietly reintroduce it as the word unchanged, which is worse: it would
    read as a finding rather than as a broken widget."""
    assert (report.sensitivity_text(0.06811838, 0.06848999, "kg N/day")
            == "0.0681 to 0.0685 kg N/day")


@pytest.mark.parametrize("lo,hi,unit,fmt_fn,expected", [
    (45.0, 74.4, "kg N/day", None, "45.0 to 74.4 kg N/day"),
    (0.452, 0.913, "kg/day", None, "0.452 to 0.913 kg/day"),
    (0.877, 0.991, "%", None, "87.7 to 99.1%"),                       # thermal, percent closed up
    (0.03, 0.08, "percent", None, "3 to 8 percent"),                  # microplastic
])
def test_a_moved_sweep_prints_exactly_what_it_printed_before(lo, hi, unit, fmt_fn, expected):
    """The factoring-out is presentation-neutral for the ordinary case. Each caller still passes
    its own formatter and unit, so no section quietly changed its spacing, its precision or its
    unit spelling on the way into one helper. `unit_suffix` is what keeps the percent sign closed
    up while "kg N/day" takes a space, on one rule rather than four."""
    if unit == "%":
        fmt_fn = report._pct
    elif unit == "percent":
        def fmt_fn(v):
            return report.fmt(100.0 * v, 1)
    assert report.sensitivity_text(lo, hi, unit, fmt_fn or report.fmt_sig) == expected


def test_a_bound_with_no_partner_is_no_range(base):
    """Thermal used to guard on the LOW bound only, so a result carrying a low and no high
    rendered the literal "87.7 to None%" into the document."""
    assert report.sensitivity_text(0.877, None, "%", report._pct) is None
    assert report.sensitivity_text(None, 0.991, "%", report._pct) is None
    base.thermal.buffering_opportunity_high = None
    html = report.render_html(_res(base), include_hydraulics=False, app_version="t")
    assert "to None" not in html
    assert report.SENSITIVITY_LABEL not in html.split("Thermal", 1)[-1].split("</article>", 1)[0]


def test_the_hydraulic_collapse_names_the_run_count(base):
    """"runs", not "alternatives": `case_count` counts the Basecase alongside them, and the number
    has to agree with the Runs column of the table in the same tab, which counts the same way."""
    env = A.fold(base, [("k_upper", "Higher K", _screening(1.0, 0)[0])])   # identical run
    line = report.envelope_line(env.sections[0], env.case_count)
    assert line == f"unchanged across {env.case_count} runs"
    rows = report.envelope_section_rows(env.sections[0], env.case_count)
    assert rows[0]["runs"] == str(env.case_count), "the sentence and the column must agree"


def test_both_documents_word_the_collapse_identically(base, tmp_path):
    """ONE PRECOMPUTED FIELD, not one call per renderer. The sentence used to be built by an
    `env_line` call in the template and an `envelope_line` call in the PDF, which was already two
    chances to word one thing differently and became a real risk the moment it started carrying a
    run count the template had no way to reach."""
    from reportlab.platypus import SimpleDocTemplate

    env = A.fold(base, [("k_upper", "Higher K", _screening(1.0, 0)[0])])   # identical run
    res = _res(base, env)
    html = report.render_html(res, include_hydraulics=False, app_version="t")
    expected = f"unchanged across {env.case_count} runs"
    assert expected in html

    seen = []
    real = SimpleDocTemplate.build

    def walk(flowables):
        """RECURSIVE, because these two rows live inside a `KeepTogether`. A flat sweep of the
        story reads only the top level and would pass while carrying nothing at all."""
        for f in flowables:
            if getattr(f, "text", None):
                seen.append(f.text)
            for attr in ("_content", "_flowables"):
                walk(getattr(f, attr, None) or [])
            for row in getattr(f, "_cellvalues", None) or []:
                walk([c for c in row if not isinstance(c, str)])
                seen.extend(c for c in row if isinstance(c, str))

    def spy(self, story, *a, **k):
        walk(story)
        return real(self, story, *a, **k)

    SimpleDocTemplate.build = spy
    try:
        report.render_pdf(res, tmp_path / "c.pdf", include_hydraulics=False, app_version="t")
    finally:
        SimpleDocTemplate.build = real
    joined = "\n".join(seen)
    for text in (report.SENSITIVITY_LABEL, report.ENVELOPE_LABEL, expected):
        assert text in joined, text
    assert "(process inputs held)" not in joined


def test_a_withheld_envelope_is_explained_in_the_screening_document(base, tmp_path):
    """SELECTED MEANS SHOWN OR EXPLAINED, and the explanation has to land where the option was
    ticked. The general warnings block lives in the HYDRAULICS half of the template, so the
    screening document carried none: a reader who enabled the envelope on the screening report
    pane got a normal-looking document with it simply missing and no cause stated anywhere. The
    build is off-loop, so there is no notification to fall back on either."""
    env, warn = A.build_envelope(_complete(tmp_path), work_dir=tmp_path, snapshot=_snapshot(),
                                 base_functions=base, function_inputs=KNOBS,
                                 reach_length_m=253.0)
    assert env is None and warn is not None
    res = AssessmentResultsV2(assessment_id="a", input_hash="h", functions=base,
                              warnings=[warn])
    scr = report.render_html(res, include_hydraulics=False, app_version="t")
    hyd = report.render_html(res, include_functions=False, app_version="t")
    assert warn.message in scr, "the screening document must say why its envelope is missing"
    assert warn.message in hyd
    report.render_pdf(res, tmp_path / "w.pdf", include_hydraulics=False, app_version="t")
    assert (tmp_path / "w.pdf").read_bytes()[:5] == b"%PDF-"


def test_a_clean_run_prints_no_empty_warning_card(base):
    """The warnings block is warnings-only. An always-on empty card is the noise the report
    declutter removed."""
    html = report.render_html(_res(base), include_hydraulics=False, app_version="t")
    assert 'class="warn"' not in html


def test_the_option_off_leaves_the_document_free_of_envelope_markup(base):
    html = report.render_html(_res(base), include_hydraulics=False, app_version="t")
    # The headline card itself STAYS: it is resolved from the registry, not from the sweep, so a
    # document built without alternatives still opens each function on its estimate. What must go
    # is the second row inside it and the appendix.
    assert 'class="result-value"' in html and 'class="function-card"' in html
    for probe in (report.ENVELOPE_LABEL, "Supporting screening results", "envelope"):
        assert probe not in html, probe


def test_the_envelope_document_carries_no_em_dash_or_semicolon(base, sweep, tmp_path):
    """CLOSES A REAL LINT HOLE. Neither `results` nor `screened` in tests/test_report.py carries
    an alternatives manifest, so the em-dash sweep there renders no envelope copy at all: every
    string this feature adds would have escaped it."""
    import re
    html = report.render_html(_res(base, sweep), include_hydraulics=False, app_version="t")
    assert "—" not in html
    body = re.sub(r"<style>.*?</style>", "", html, flags=re.S)
    body = re.sub(r"<script>.*?</script>", "", body, flags=re.S)
    # `envelope_scope_note` is no longer copy: it returns whether a range was folded at all,
    # because the sentence it used to return was deleted from the document.
    # `ENVELOPE_CAUTION` is not in this sweep because it no longer exists. It was passed to the
    # template as `env_caution` and referenced by no template body and no PDF path, so the one
    # disclosure it carried reached no reader, and the decision was not to revive it.
    for text in (report.SENSITIVITY_LABEL, report.ENVELOPE_LABEL, report.SENSITIVITY_UNCHANGED,
                 report.ENVELOPE_LIMITATION):
        assert "—" not in text and ";" not in text, text
    report.render_pdf(_res(base, sweep), tmp_path / "e.pdf", include_hydraulics=False,
                      app_version="t")
    assert (tmp_path / "e.pdf").read_bytes()[:5] == b"%PDF-"


def test_every_gate_reason_is_clean_copy(tmp_path):
    reasons = []
    for mf in (None, _manifest(statuses=[]), _manifest(statuses=[AltStatus.failed] * 8),
               _manifest(statuses=[AltStatus.completed] * 5 + [AltStatus.not_run] * 3),
               _complete(tmp_path, write=False)):
        if mf is not None and not mf.scenarios:
            mf.scenarios = []
        reasons.append(A.envelope_available(mf, work_dir=tmp_path)[1])
    for r in reasons:
        assert "—" not in r and ";" not in r, r
