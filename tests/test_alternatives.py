"""Hydraulic Alternatives: the order-of-magnitude K / gradient sweep.

Covers the pure scenario logic (hype_app/alternatives.py), the manifest contract, the
report-side range plumbing (Scenario range column + Hydraulic Alternatives section in both
renderers), and the assessment-results migration that retired the old sensitivity field.
"""
import json
from datetime import datetime, timezone

import pytest

from hype_app import alternatives as alt
from hype_app.contracts import (
    ALT_STATUS_LABEL,
    AltScenario,
    AltStatus,
    AssessmentResultsV2,
    HydraulicAlternativesManifest,
    migrate,
)

FULL = dict(alt.DEFAULT_SELECTION)
ALL_IDS = [s.id for s in alt.build_scenarios(FULL)]


# ---------------------------------------------------------------- scenario building

def test_default_selection_values():
    """User spec: K an order of magnitude each way, gradient half and double."""
    assert FULL == {"k_lower": 0.1, "k_upper": 10.0, "g_lower": 0.5, "g_higher": 2.0,
                    "combos": True}


def test_selection_combinations_yield_expected_counts_and_order():
    assert ALL_IDS == ["k_upper", "k_lower", "g_higher", "g_lower",
                       "k_upper_g_higher", "k_upper_g_lower",
                       "k_lower_g_higher", "k_lower_g_lower"]
    k_only = alt.build_scenarios({**FULL, "g_lower": None, "g_higher": None})
    assert [s.id for s in k_only] == ["k_upper", "k_lower"]
    singles = alt.build_scenarios({**FULL, "combos": False})
    assert [s.id for s in singles] == ["k_upper", "k_lower", "g_higher", "g_lower"]
    partial = alt.build_scenarios({"k_upper": 10.0, "g_higher": 2.0, "combos": True})
    assert [s.id for s in partial] == ["k_upper", "g_higher", "k_upper_g_higher"]
    assert alt.build_scenarios({"combos": True}) == []


def test_single_factor_runs_come_before_combined():
    """User spec: an early stop yields the most interpretable partial results."""
    scens = alt.build_scenarios(FULL)
    for s in scens[:4]:
        assert s.k_factor == 1.0 or s.g_factor == 1.0     # single-factor
    for s in scens[4:]:
        assert s.k_factor != 1.0 and s.g_factor != 1.0    # combined


def test_custom_multipliers_flow_into_factors_and_ids_stay_role_based():
    sel = {"k_lower": 0.25, "k_upper": 4.0, "g_lower": 0.5, "g_higher": 3.0, "combos": True}
    scens = {s.id: s for s in alt.build_scenarios(sel)}
    assert scens["k_upper"].k_factor == 4.0 and scens["k_upper"].g_factor == 1.0
    assert scens["g_lower"].g_factor == 0.5 and scens["g_lower"].k_factor == 1.0
    assert scens["k_lower_g_higher"].k_factor == 0.25
    assert scens["k_lower_g_higher"].g_factor == 3.0
    for sid, s in scens.items():
        assert sid == sid.lower() and " " not in sid
        assert s.rel_dir == f"alternatives/{sid}"


def test_validate_selection_matrix():
    assert alt.validate_selection(FULL) == []
    assert alt.validate_selection({**FULL, "combos": False}) == []
    # nothing selected
    assert alt.validate_selection({"combos": True}) == ["Select at least one variation."]
    # zero / negative
    assert "K lower must be above 0." in alt.validate_selection({**FULL, "k_lower": 0.0})
    assert "Gradient higher must be above 0." in \
        alt.validate_selection({**FULL, "g_higher": -2.0})
    # inverted vs the Basecase
    assert "K lower must be below 1 (lower than the Basecase)." in \
        alt.validate_selection({**FULL, "k_lower": 2.0})
    assert "K upper must be above 1 (higher than the Basecase)." in \
        alt.validate_selection({**FULL, "k_upper": 1.0})
    assert "Gradient lower must be below 1 (lower than the Basecase)." in \
        alt.validate_selection({**FULL, "g_lower": 1.0})
    # enabled but empty (the UI's cleared-numeric sentinel)
    assert "Enter a multiplier for Gradient higher." in \
        alt.validate_selection({**FULL, "g_higher": ""})


def test_factor_text():
    assert alt.factor_text(10.0) == "×10"
    assert alt.factor_text(0.5) == "×0.5"
    assert alt.factor_text(1) == "×1"


def test_scenario_payloads_shape():
    p = alt.scenario_payloads(alt.build_scenarios(
        {**FULL, "g_lower": None, "g_higher": None}))
    assert p == [{"id": "k_upper", "label": "Higher K",
                  "k_factor": 10.0, "g_factor": 1.0},
                 {"id": "k_lower", "label": "Lower K",
                  "k_factor": 0.1, "g_factor": 1.0}]


# ---------------------------------------------------------------- profile scaling

def test_scale_profile_multiplies_gradients_only():
    assert alt.scale_profile("0,0.005 1,0.01", 10.0) == "0,0.05 1,0.1"
    assert alt.scale_profile("0,0.005 0.42,0.011 1,0.008", 0.1) == \
        "0,0.0005 0.42,0.0011 1,0.0008"
    # negative (losing) gradients scale in place; stations untouched
    assert alt.scale_profile("0,-0.004 1,0.004", 10.0) == "0,-0.04 1,0.04"
    assert alt.scale_profile("0,0.005 1,0.01", 1.0) == "0,0.005 1,0.01"


def test_scale_profile_rejects_empty():
    with pytest.raises(ValueError):
        alt.scale_profile("", 10.0)


# ---------------------------------------------------------------- manifest contract

def _manifest(statuses=None, sections=None, metrics=None):
    mf = alt.build_manifest(dict(FULL), base_input_hash="h" * 8,
                            base_assessment_id="A1", app_version="t",
                            hz_knobs={"particles_per_cell": 2})
    for i, s in enumerate(mf.scenarios):
        if statuses:
            s.status = statuses[i] if i < len(statuses) else AltStatus.pending
        if sections and s.status == AltStatus.completed:
            s.results_sections = sections
        if metrics and s.status == AltStatus.completed:
            s.metrics = dict(metrics)
    return mf


def test_manifest_json_roundtrip_under_strictness():
    mf = _manifest(statuses=[AltStatus.completed], metrics={"turnovers_per_km": 1.0})
    mf.scenarios[0].started_at = datetime(2026, 8, 1, tzinfo=timezone.utc)
    dumped = mf.model_dump(mode="json")
    back = HydraulicAlternativesManifest.model_validate(json.loads(json.dumps(dumped)))
    assert back.scenarios[0].status == AltStatus.completed
    assert back.base_input_hash == "h" * 8
    assert back.hz_knobs == {"particles_per_cell": 2}
    assert back.selection == FULL              # the user's multipliers ride the index


def test_legacy_first_build_manifest_normalizes_and_validates():
    """Batches saved by the first build carried vary_k/vary_gradient and value-encoded ids
    (k10_gradient1); the strict contract rejects them raw. The app's normalization (pop the
    two keys, default selection) must make them validate, or the pane loses its range cards,
    supporting ranges, AND action buttons all at once (user report, 2026-08-02)."""
    legacy = _manifest(statuses=[AltStatus.completed]).model_dump(mode="json")
    legacy.pop("selection")
    legacy["vary_k"] = True
    legacy["vary_gradient"] = True
    legacy["scenarios"][0]["id"] = "k10_gradient1"
    with pytest.raises(Exception):
        HydraulicAlternativesManifest.model_validate(legacy)
    fixed = dict(legacy)
    fixed.pop("vary_k", None)                  # mirrors app._normalize_alt_manifest
    fixed.pop("vary_gradient", None)
    fixed.setdefault("selection", {})
    back = HydraulicAlternativesManifest.model_validate(fixed)
    assert back.scenarios[0].id == "k10_gradient1"
    assert back.selection == {}


def test_status_labels_cover_every_status():
    assert set(ALT_STATUS_LABEL) == set(AltStatus)
    assert ALT_STATUS_LABEL[AltStatus.cancelled] == "Canceled"
    assert ALT_STATUS_LABEL[AltStatus.not_run] == "Not run"


def test_relaunch_scenarios_retry_vs_continue():
    """After a halt on scenario 3: Retry = failed + remaining queued; Continue = remaining."""
    mf = _manifest(statuses=[AltStatus.completed, AltStatus.completed, AltStatus.failed])
    halted = mf.scenarios[2].id
    retry = alt.relaunch_scenarios(mf, halted, retry=True)
    assert [s.id for s in retry] == [halted] + ALL_IDS[3:]
    cont = alt.relaunch_scenarios(mf, halted, retry=False)
    assert [s.id for s in cont] == ALL_IDS[3:]
    # a completed scenario is never relaunched
    assert ALL_IDS[0] not in [s.id for s in retry]


def test_partial_note_counts_include_the_basecase():
    full = _manifest(statuses=[AltStatus.completed] * 8)
    assert alt.partial_note(full) is None
    part = _manifest(statuses=[AltStatus.completed] * 4 + [AltStatus.cancelled]
                     + [AltStatus.not_run] * 3)
    assert alt.partial_note(part) == "Partial scenario range: 5 of 9 runs"


# ---------------------------------------------------------------- ranges

_SECTIONS_LO = {"connectivity": {"turnovers_per_km": 0.5, "streamflow_cms": 2.0},
                "residence_time": {"weighted_median_days": 1.0},
                "zone": {"equivalent_active_depth_m": 0.2}}
_SECTIONS_HI = {"connectivity": {"turnovers_per_km": 5.0, "streamflow_cms": 2.0},
                "residence_time": {"weighted_median_days": 3.0},
                "zone": {"equivalent_active_depth_m": 0.9}}


def test_primaries_from_sections():
    assert alt.primaries_from_sections(_SECTIONS_LO) == {
        "turnovers_per_km": 0.5, "rtd_median_days": 1.0,
        "equivalent_active_depth_m": 0.2}
    assert alt.primaries_from_sections({}) == {}


def test_primary_ranges_pool_basecase_and_completed_only():
    mf = _manifest(statuses=[AltStatus.completed, AltStatus.completed, AltStatus.failed])
    mf.scenarios[0].metrics = {"turnovers_per_km": 0.5}
    mf.scenarios[1].metrics = {"turnovers_per_km": 5.0}
    mf.scenarios[2].metrics = {"turnovers_per_km": 99.0}     # failed: must be excluded
    rng = alt.primary_ranges(mf, {"turnovers_per_km": 2.0})
    assert rng["turnovers_per_km"] == {"lo": 0.5, "hi": 5.0, "n": 3}
    no_base = alt.primary_ranges(mf, None)
    assert no_base["turnovers_per_km"] == {"lo": 0.5, "hi": 5.0, "n": 2}


def test_metric_ranges_key_onto_current_metric_rows():
    from hype_app import report
    mf = _manifest(statuses=[AltStatus.completed, AltStatus.completed])
    mf.scenarios[0].results_sections = _SECTIONS_LO
    mf.scenarios[1].results_sections = _SECTIONS_HI
    ranges = alt.metric_ranges(mf)
    valid_keys = {(r["section"], r["name"])
                  for r in report.metric_rows(alt._results_from_sections(_SECTIONS_LO))}
    assert set(ranges) <= valid_keys
    key = (report.DIM_DURATION, "Median residence time")
    assert ranges[key]["lo"] == pytest.approx(24.0)      # metric_rows presents hours
    assert ranges[key]["hi"] == pytest.approx(72.0)
    assert ranges[key]["n"] == 2
    assert ranges[key]["unit"] == "hr"


def test_metric_ranges_skip_non_numeric_and_fold_base_rows():
    from hype_app import report
    mf = _manifest(statuses=[AltStatus.completed])
    mf.scenarios[0].results_sections = _SECTIONS_HI
    base_rows = report.metric_rows(alt._results_from_sections(_SECTIONS_LO))
    ranges = alt.metric_ranges(mf, base_rows)
    key = (report.DIM_FREQUENCY, "Streamflow-equivalent turnovers")
    assert ranges[key] == {"lo": 0.5, "hi": 5.0, "n": 2, "unit": "turnovers/km"}
    # string-valued rows (e.g. Volume basis) never range
    assert all(isinstance(v["lo"], (int, float)) for v in ranges.values())


# ---------------------------------------------------------------- report integration

def _results_with_alternatives(results, n_completed=2):
    statuses = ([AltStatus.completed] * n_completed
                + [AltStatus.not_run] * (8 - n_completed))
    mf = _manifest(statuses=statuses)
    for i, s in enumerate(mf.scenarios[:n_completed]):
        s.results_sections = _SECTIONS_LO if i % 2 == 0 else _SECTIONS_HI
        s.metrics = alt.primaries_from_sections(s.results_sections)
    return results.model_copy(update={"alternatives": mf})


@pytest.fixture
def results():
    # borrow the canonical report fixture shape without importing the other test module
    from hype_app.contracts import (AssessmentInputSnapshot, ConnectivityMetrics,
                                    GradientBoundaryConfigV2, GridSettings, KSettings,
                                    ResidenceTimeMetrics, SiteMetadata, StreamflowInput,
                                    ZoneMetrics)
    from hype_app.provenance import Provenance
    snap = AssessmentInputSnapshot(
        assessment_id="A1",
        site=SiteMetadata(site_name="Mink", analyst="Ada", reach_length_m=500.0),
        streamflow=StreamflowInput(value_cms=2.83,
                                   provenance=Provenance(source="USGS StreamStats")),
        k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.3),
        gradients=GradientBoundaryConfigV2(),
        grid=GridSettings(cell_size_x=10.0, cell_size_y=10.0, gw_mod_depth=20.0,
                          layer_thickness=0.5))
    return AssessmentResultsV2(
        assessment_id="A1", input_hash="a" * 64, input_snapshot=snap,
        connectivity=ConnectivityMetrics(streamflow_cms=2.83, turnovers_per_km=1.2),
        residence_time=ResidenceTimeMetrics(weighted_median_days=1.5),
        zone=ZoneMetrics(equivalent_active_depth_m=0.4),
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc))


def test_alternative_range_rows_subset_of_metric_rows(results):
    from hype_app import report
    res = _results_with_alternatives(results)
    rows = report.alternative_range_rows(res)
    assert rows, "completed alternatives must produce range rows"
    names = {(r["section"], r["name"]) for r in report.metric_rows(res)}
    assert {(r["section"], r["name"]) for r in rows} <= names
    assert report.alternative_range_rows(results) == []      # no manifest -> no rows


def test_html_gains_column_and_section_only_with_alternatives(results):
    from hype_app.report import render_html
    plain = render_html(results, app_version="t")
    assert "Scenario range" not in plain
    assert "Hydraulic Alternatives" not in plain
    html = render_html(_results_with_alternatives(results), app_version="t")
    assert "Scenario range" in html
    assert "Hydraulic Alternatives" in html
    assert "not confidence intervals" in html
    assert "2 of 8 alternative runs completed" in html
    assert "—" not in html.split("Hydraulic Alternatives", 1)[1].split("</details>")[0]


def test_pdf_builds_with_alternatives(results, tmp_path):
    from hype_app.report import render_pdf
    out = tmp_path / "alt.pdf"
    render_pdf(_results_with_alternatives(results), out, app_version="t")
    assert out.read_bytes()[:5] == b"%PDF-"


def test_csv_range_column_only_with_alternatives(results, tmp_path):
    from hype_app.report import write_site_metrics_csv
    p1 = tmp_path / "plain.csv"
    write_site_metrics_csv(results, p1)
    assert "scenario_range" not in p1.read_text().splitlines()[0]
    p2 = tmp_path / "alts.csv"
    write_site_metrics_csv(_results_with_alternatives(results), p2)
    assert p2.read_text().splitlines()[0].endswith("scenario_range")


def test_report_module_has_no_legacy_sensitivity_surface():
    from hype_app import report
    assert not hasattr(report, "sensitivity_rows")


# ---------------------------------------------------------------- migration

def test_results_2_2_payload_migrates_and_validates(results):
    old = results.model_dump(mode="json")
    old["schema_version"] = "assessment-results/2.2"
    old["sensitivity"] = None
    old.pop("alternatives", None)
    upgraded = migrate("assessment-results", old)
    assert "sensitivity" not in upgraded
    model = AssessmentResultsV2.model_validate(upgraded)
    assert model.alternatives is None


def test_user_copy_has_no_em_dashes_or_semicolons():
    """Standing rule sweep over this feature's user-facing strings."""
    labels = ([label for _r, label, _k in alt.ROLE_ORDER]
              + [label for _k, _g, label in alt.COMBO_ORDER]
              + list(ALT_STATUS_LABEL.values())
              + alt.validate_selection({"combos": True})
              + alt.validate_selection({**FULL, "k_lower": -1.0, "k_upper": 0.5,
                                        "g_lower": 2.0, "g_higher": ""}))
    for text in labels:
        assert "—" not in text and ";" not in text
    note = alt.partial_note(_manifest(statuses=[AltStatus.failed]))
    assert note and "—" not in note and ";" not in note
