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


# --------------------------------------------------------------- §27.8 screening identities
# These exist because drift in the flow-path weights rescales EVERY screening mass by one factor
# and nothing else would notice. They were computed in screen.py and surfaced nowhere, so a
# silent 86400x unit error could ship.
def _with_nutrient(**over):
    from hype_app.contracts import FunctionScreening, NutrientScreening
    return _results(functions=FunctionScreening(nutrient=NutrientScreening(**over)))


def test_weight_identity_drift_warns():
    """86399 is the m3/day-passed-as-m3/s signature; 0.99999 is the reverse."""
    for bad in (86399.0, 0.99999):
        w, diag = validate_results(_with_nutrient(weight_identity_rel_diff=bad))
        assert "weight_identity" in _codes(w), bad
        assert diag["screening_weight_identity_rel_diff"] == bad
    msg = next(x.message for x in validate_results(
        _with_nutrient(weight_identity_rel_diff=86399.0))[0] if x.code == "weight_identity")
    assert "every screening mass" in msg.lower()      # names the consequence, not the symptom


def test_weight_identity_within_tolerance_is_recorded_but_silent():
    """Float summation over many paths lands near 1e-12. Warning there would be noise."""
    w, diag = validate_results(_with_nutrient(weight_identity_rel_diff=1e-12))
    assert "weight_identity" not in _codes(w)
    assert diag["screening_weight_identity_rel_diff"] == 1e-12


def test_chain_closure_drift_warns():
    w, diag = validate_results(_with_nutrient(chain_closure_rel_diff=0.4))
    assert "chain_closure" in _codes(w)
    assert diag["screening_chain_closure_rel_diff"] == 0.4


def test_screening_checks_are_absent_when_screening_did_not_run():
    w, diag = validate_results(_results())
    assert not ({"weight_identity", "chain_closure"} & _codes(w))
    assert not [k for k in diag if k.startswith("screening_")]


def test_the_storage_cross_check_never_warns():
    """It compares RTD-derived mobile storage against bulk pore volume: two genuinely different
    quantities that differ by tens of percent on healthy runs. Warning on it would train users
    to ignore the panel where the weight-identity signal has to land."""
    from hype_app.contracts import FunctionScreening, ThermalOpportunity
    res = _results(functions=FunctionScreening(
        thermal=ThermalOpportunity(storage_cross_check_rel_diff=0.72)))
    w, _ = validate_results(res)
    assert not _codes(w)
