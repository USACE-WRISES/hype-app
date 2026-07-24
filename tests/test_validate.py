"""Quality-control validation tests (report §27)."""
from hype_app.contracts import (
    AssessmentResultsV2,
    ConnectivityMetrics,
    ResidenceTimeMetrics,
    ThresholdResult,
    ZoneMetrics,
)
from hype_app.validate import validate_results


def _results(**over):
    base = dict(assessment_id="A1", input_hash="a" * 64)
    base.update(over)
    return AssessmentResultsV2(**base)


def _codes(warnings):
    return {w.code for w in warnings}


def test_water_balance_over_tolerance_warns():
    res = _results()
    w, diag = validate_results(res, hz_accounting={"mass_balance_error": 0.12}, tol_pct=5.0)
    assert "water_balance" in _codes(w)
    assert diag["mass_balance_error"] == 0.12


def test_water_balance_within_tolerance_ok():
    res = _results()
    w, _ = validate_results(res, hz_accounting={"mass_balance_error": 0.01}, tol_pct=5.0)
    assert "water_balance" not in _codes(w)


def test_connectivity_reciprocal_ok_and_mismatch():
    ok = _results(connectivity=ConnectivityMetrics(turnovers_per_km=0.5, turnover_length_km=2.0))
    assert "connectivity_reciprocal" not in _codes(validate_results(ok)[0])
    bad = _results(connectivity=ConnectivityMetrics(turnovers_per_km=0.5, turnover_length_km=5.0))
    assert "connectivity_reciprocal" in _codes(validate_results(bad)[0])


def test_active_fraction_out_of_range():
    res = _results(connectivity=ConnectivityMetrics(active_streambed_fraction=1.4))
    assert "active_bed_bounds" in _codes(validate_results(res)[0])


def test_residence_order_violation():
    res = _results(residence_time=ResidenceTimeMetrics(
        p10_days=2.0, weighted_median_days=1.0, p90_days=3.0))     # p10 > p50
    w, diag = validate_results(res)
    assert "residence_order" in _codes(w) and diag["residence_order_ok"] is False


def test_residence_order_ok():
    res = _results(residence_time=ResidenceTimeMetrics(
        p10_days=0.5, weighted_median_days=1.0, p90_days=3.0))
    w, diag = validate_results(res)
    assert "residence_order" not in _codes(w) and diag["residence_order_ok"] is True


def test_threshold_monotonicity_violation():
    res = _results(thresholds=[
        ThresholdResult(threshold_value_h=1.0, flow_exceedance_fraction=0.4),
        ThresholdResult(threshold_value_h=6.0, flow_exceedance_fraction=0.6)])   # increases
    assert "threshold_monotonicity" in _codes(validate_results(res)[0])


def test_spatial_volume_exceeds_domain():
    res = _results(zone=ZoneMetrics(bulk_saturated_volume_m3=1000.0))
    assert "volume_exceeds_domain" in _codes(validate_results(res, domain_volume_m3=500.0)[0])


def test_high_censored_flow_warns():
    res = _results(connectivity=ConnectivityMetrics(censored_flow_fraction=0.4))
    assert "censored_flow" in _codes(validate_results(res)[0])
