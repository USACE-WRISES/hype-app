"""Depth-aware conductivity-derivation tests (spec §6.5–6.10, §13.1, §14.4)."""
import pytest

from hype_app.contracts import AggregationPolicy, Component, Horizon, KOrigin, MapUnit
from hype_app.soil_profile import (
    CoverageAccumulator,
    KSegment,
    aggregate_segments,
    derive_profiles,
    horizon_kv_kh,
    intersect_layer_horizons,
    layer_k_for_component,
    select_components,
)


def test_horizon_kv_kh():
    kv, kh = horizon_kv_kh(10.0, 10.0)          # 10 um/s, 10:1 anisotropy
    assert kv == pytest.approx(0.864)           # 10 * 0.0864
    assert kh == pytest.approx(8.64)


class TestAggregateSegments:
    def test_arithmetic_kh_harmonic_kv(self):
        segs = [KSegment(1.0, kh=10.0, kv=1.0), KSegment(1.0, kh=20.0, kv=2.0)]
        kh, kv, origin = aggregate_segments(segs)
        assert kh == pytest.approx(15.0)                    # (10+20)/2 arithmetic
        assert kv == pytest.approx(2.0 / (1.0 / 1.0 + 1.0 / 2.0))  # harmonic = 1.333
        assert origin == KOrigin.derived

    def test_thickness_weighting(self):
        segs = [KSegment(3.0, kh=10.0, kv=1.0), KSegment(1.0, kh=30.0, kv=3.0)]
        kh, kv, _ = aggregate_segments(segs)
        assert kh == pytest.approx((10 * 3 + 30 * 1) / 4)   # 15
        assert kv == pytest.approx(4.0 / (3.0 / 1.0 + 1.0 / 3.0))

    def test_all_fallback_origin(self):
        segs = [KSegment(1.0, 10.0, 1.0, KOrigin.fallback)]
        assert aggregate_segments(segs)[2] == KOrigin.fallback


class TestIntersectLayerHorizons:
    def test_two_horizons_span_layer(self):
        # ground at elev 100; horizon A 0–100 cm (elev 99–100), B 100–200 cm (elev 98–99).
        horizons = [{"top_cm": 0, "bottom_cm": 100, "ksat_um_s": 10.0},
                    {"top_cm": 100, "bottom_cm": 200, "ksat_um_s": 20.0}]
        segs = intersect_layer_horizons(100.0, 98.0, 100.0, horizons, 10.0,
                                        fallback_kh=1.0, fallback_kv=0.1)
        assert len(segs) == 2
        assert segs[0].thickness_m == pytest.approx(1.0)
        assert segs[0].kv == pytest.approx(0.864)           # 10 um/s
        assert segs[1].kv == pytest.approx(1.728)           # 20 um/s
        assert all(s.origin == KOrigin.derived for s in segs)

    def test_below_profile_uses_fallback(self):
        # layer 96–100 elev; horizons only to 200 cm (elev 98) -> [96,98] is fallback.
        horizons = [{"top_cm": 0, "bottom_cm": 200, "ksat_um_s": 10.0}]
        segs = intersect_layer_horizons(100.0, 96.0, 100.0, horizons, 10.0,
                                        fallback_kh=1.0, fallback_kv=0.1)
        fb = [s for s in segs if s.origin == KOrigin.fallback]
        assert len(fb) == 1 and fb[0].thickness_m == pytest.approx(2.0)
        assert fb[0].kh == 1.0 and fb[0].kv == 0.1

    def test_no_horizons_all_fallback(self):
        segs = intersect_layer_horizons(100.0, 98.0, 100.0, [], 10.0,
                                        fallback_kh=2.0, fallback_kv=0.5)
        assert len(segs) == 1 and segs[0].origin == KOrigin.fallback
        assert segs[0].thickness_m == pytest.approx(2.0)


class TestSelectComponents:
    def _mu(self):
        return MapUnit(mukey="1", components=[
            Component(cokey="a", name="A", comppct_r=60, major=True),
            Component(cokey="b", name="B", comppct_r=30, major=True),
            Component(cokey="c", name="C", comppct_r=10, major=False)])

    def test_dominant_picks_highest_major(self):
        sel = select_components(self._mu(), AggregationPolicy.dominant)
        assert len(sel) == 1 and sel[0][0].cokey == "a" and sel[0][1] == 1.0

    def test_weighted_normalizes(self):
        sel = select_components(self._mu(), AggregationPolicy.weighted)
        weights = {c.cokey: w for c, w in sel}
        assert weights["a"] == pytest.approx(0.6)
        assert sum(weights.values()) == pytest.approx(1.0)

    def test_user_component(self):
        sel = select_components(self._mu(), AggregationPolicy.user_component, chosen_cokey="b")
        assert sel[0][0].cokey == "b"


def test_layer_k_for_component_depth_varying():
    """§14.4: a synthetic component produces the expected depth-varying KH/KV for a layer."""
    comp = Component(cokey="a", horizons=[
        Horizon(name="A", top_cm=0, bottom_cm=100, ksat_um_s=10.0),
        Horizon(name="B", top_cm=100, bottom_cm=200, ksat_um_s=20.0)])
    kh, kv, origin, fb = layer_k_for_component(
        comp, layer_top_elev=100.0, layer_bottom_elev=98.0, ground_elev=100.0,
        anisotropy_ratio=10.0, fallback_kh=1.0, fallback_kv=0.1)
    # two 1 m segments: KV harmonic of 0.864 & 1.728; KH arithmetic of 8.64 & 17.28
    assert kh == pytest.approx((8.64 + 17.28) / 2)
    assert kv == pytest.approx(2.0 / (1 / 0.864 + 1 / 1.728))
    assert origin == KOrigin.derived and fb == 0.0


def test_coverage_accumulator_percentages():
    cov = CoverageAccumulator()
    cov.add(KOrigin.derived, 75.0)
    cov.add(KOrigin.fallback, 25.0)
    pct = cov.as_percentages()
    assert pct["derived"] == 75.0 and pct["fallback"] == 25.0


def test_derive_profiles():
    mu = MapUnit(mukey="1", components=[Component(cokey="a", horizons=[
        Horizon(top_cm=0, bottom_cm=50, ksat_um_s=9.0)])])
    profs = derive_profiles(mu, 10.0)
    assert len(profs) == 1
    assert profs[0].kv_m_day == pytest.approx(0.7776)   # 9 * 0.0864
    assert profs[0].kh_m_day == pytest.approx(7.776)
