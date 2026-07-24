"""Gradient-sensitivity generation + aggregation tests (spec §10, §14.15)."""
import pytest

from hype_app.contracts import (
    GeneratorType,
    GradientBoundaryConfigV2,
    GradientControl,
    ScenarioStatus,
    Side,
)
from hype_app.sensitivity import (
    aggregate_metric,
    build_manifest,
    canonical_scenario_hash,
    generate_scenarios,
    plan_execution_order,
)


def _base():
    def side(s):
        return [GradientControl(id=f"{s.value}0", side=s, station=0.0, preferred=0.01,
                                lower=0.0, upper=0.02),
                GradientControl(id=f"{s.value}1", side=s, station=1.0, preferred=0.01,
                                lower=0.0, upper=0.02)]
    return GradientBoundaryConfigV2(left_controls=side(Side.left), right_controls=side(Side.right))


def test_linked_generator_three_scenarios():
    """§14.15: default linked design -> lower/preferred/upper."""
    specs = generate_scenarios(_base(), GeneratorType.linked)
    assert len(specs) == 3
    assert specs[0].is_preferred
    # preferred controls == 0.01, lower == 0.0, upper == 0.02
    assert all(c.preferred == 0.01 for c in specs[0].gradients.left_controls)
    lower = next(s for s in specs if "Lower" in s.label)
    assert all(c.preferred == 0.0 for c in lower.gradients.left_controls)


def test_crossed_generator_nine_combos():
    specs = generate_scenarios(_base(), GeneratorType.crossed)
    # 9 L×R combinations; the (preferred,preferred) one is deduped against the leading preferred
    assert len(specs) == 9


def test_dedup_by_canonical_hash():
    # a base where lower==preferred==upper collapses all linked variants to one
    flat = GradientBoundaryConfigV2(left_controls=[
        GradientControl(id="l0", side=Side.left, station=0.0, preferred=0.01),
        GradientControl(id="l1", side=Side.left, station=1.0, preferred=0.01)])
    specs = generate_scenarios(flat, GeneratorType.linked)
    assert len(specs) == 1                # lower/upper fall back to preferred -> identical hash


def test_canonical_hash_stable_and_distinct():
    b = _base()
    assert canonical_scenario_hash(b) == canonical_scenario_hash(b.model_copy(deep=True))
    other = b.model_copy(update={"left_controls": [
        c.model_copy(update={"preferred": 0.05}) for c in b.left_controls]})
    assert canonical_scenario_hash(b) != canonical_scenario_hash(other)


def test_execution_order_preferred_first():
    m = build_manifest(_base(), GeneratorType.linked)
    order = plan_execution_order(m)
    assert order[0] == m.preferred_id


def test_scenario_cap():
    m = build_manifest(_base(), GeneratorType.one_at_a_time, max_scenarios=3)
    assert len(m.scenarios) == 3
    assert any(w.code == "scenario_cap" for w in m.warnings)


def test_aggregate_metric_over_primary_metric():
    specs = generate_scenarios(_base(), GeneratorType.linked)
    # attach synthetic completed metrics keyed by a primary hydraulic metric
    for s, c1km in zip(specs, (0.5, 0.3, 0.7)):
        s.status = ScenarioStatus.completed
        s.metrics = {"turnovers_per_km": c1km, "equivalent_active_depth_m": 1.2}
    agg = aggregate_metric(specs, "turnovers_per_km", specs[0].id)
    assert agg["preferred"] == 0.5
    assert agg["min"] == 0.3 and agg["max"] == 0.7
    assert agg["range"] == pytest.approx(0.4)
    assert agg["n_success"] == 3


def test_failed_scenarios_excluded_from_aggregation():
    """§14.16: a failed alternative doesn't corrupt the aggregation."""
    specs = generate_scenarios(_base(), GeneratorType.linked)
    specs[0].status = ScenarioStatus.completed
    specs[0].metrics = {"turnovers_per_km": 0.5}
    specs[1].status = ScenarioStatus.failed          # no metrics
    specs[2].status = ScenarioStatus.completed
    specs[2].metrics = {"turnovers_per_km": 0.7}
    agg = aggregate_metric(specs, "turnovers_per_km", specs[0].id)
    assert agg["n_success"] == 2 and agg["max"] == 0.7
