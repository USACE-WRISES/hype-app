"""Flux-weighted metric tests (spec §8, §13.1, §14.10, §14.11)."""
import numpy as np
import pytest

from hype_app.metrics import (
    classify_weighted_flux,
    connectivity,
    exceedance_fraction,
    exchange_flux,
    mobile_pore_storage,
    path_depth_metrics,
    residence_time_metrics,
    weighted_ecdf,
    weighted_mean,
    weighted_quantile,
)


class TestWeightedStats:
    def test_weighted_mean(self):
        assert weighted_mean([1, 2, 3], [1, 1, 1]) == pytest.approx(2.0)
        assert weighted_mean([1, 3], [3, 1]) == pytest.approx(1.5)

    def test_weighted_median(self):
        assert weighted_quantile([1, 2, 3, 4], [1, 1, 1, 1], 0.5) == pytest.approx(2.5)

    def test_weighted_quantile_respects_weight(self):
        # almost all weight on the large value -> high quantiles near it
        assert weighted_quantile([1, 100], [1, 99], 0.9) > 50

    def test_weighted_ecdf_monotone(self):
        v, c = weighted_ecdf([3, 1, 2], [1, 1, 1])
        assert list(v) == [1, 2, 3]
        assert c[-1] == pytest.approx(1.0)
        assert np.all(np.diff(c) >= 0)


class TestFluxWeightedExchange:
    def test_mass_balance_closes(self):
        """§14.10: flux-weighted particles pass mass-balance checks."""
        inflow = {0: 10.0, 1: 20.0}
        # node 0: 1 hyporheic particle; node 1: hyporheic + losing (weight split 10 each)
        acc = classify_weighted_flux(inflow, [0, 1, 1], ["hyporheic", "hyporheic", "losing"])
        assert acc.total_downwelling == pytest.approx(30.0)
        assert acc.returning_hyporheic == pytest.approx(20.0)
        assert acc.losing_to_sides == pytest.approx(10.0)
        assert acc.unresolved == pytest.approx(0.0)
        assert acc.mass_balance_error == pytest.approx(0.0, abs=1e-9)

    def test_explicit_weights(self):
        acc = classify_weighted_flux({0: 5.0}, [0, 0], ["hyporheic", "losing"],
                                     particle_weight=[3.0, 2.0])
        assert acc.returning_hyporheic == 3.0 and acc.losing_to_sides == 2.0


class TestConnectivity:
    def test_excursions_per_mile(self):
        """§14.11: connectivity matches the hand-calculated formula."""
        c = connectivity(streamflow=100.0, returning_hyporheic=10.0, total_downwelling=30.0,
                         losing=20.0, unresolved=0.0, reach_length_m=1609.344)
        assert c.excursions_per_mile == pytest.approx(0.1)      # (10/100)*(1609.344/1609.344)
        assert c.turnover_length_m == pytest.approx(16093.44)

    def test_turnovers_per_km_hand_formula(self):
        """report §5.1: C_1km = (Q_HEF/Q_stream) * (1000/L_model)."""
        c = connectivity(streamflow=0.75, returning_hyporheic=0.12, total_downwelling=0.2,
                         losing=0.08, unresolved=0.0, reach_length_m=1000.0)
        assert c.turnovers_per_km == pytest.approx((0.12 / 0.75) * (1000.0 / 1000.0))
        assert c.gross_exchange_ratio_reach == pytest.approx(0.12 / 0.75)

    def test_reciprocal_turnovers_and_length(self):
        """report §27.5: C_1km == 1 / L_T (km)."""
        c = connectivity(streamflow=0.75, returning_hyporheic=0.12, total_downwelling=0.2,
                         losing=0.08, unresolved=0.0, reach_length_m=723.0)
        assert c.turnovers_per_km == pytest.approx(1.0 / c.turnover_length_km)

    def test_turnovers_can_exceed_one(self):
        c = connectivity(streamflow=0.1, returning_hyporheic=0.3, total_downwelling=0.4,
                         losing=0.1, unresolved=0.0, reach_length_m=500.0)
        assert c.turnovers_per_km > 1.0                        # not clamped to [0, 1]

    def test_zero_exchange_gives_zero_c_and_infinite_length(self):
        c = connectivity(streamflow=0.5, returning_hyporheic=0.0, total_downwelling=0.1,
                         losing=0.1, unresolved=0.0, reach_length_m=1000.0)
        assert c.turnovers_per_km == pytest.approx(0.0)
        assert c.turnover_length_m == float("inf") and c.turnover_length_km == float("inf")

    def test_unavailable_when_no_streamflow(self):
        assert connectivity(streamflow=0.0, returning_hyporheic=10.0, total_downwelling=30.0,
                            losing=0.0, unresolved=0.0, reach_length_m=100.0) is None


class TestExchangeFluxAndDepth:
    def test_exchange_flux_units(self):
        """report §5.4: q_HEF = Q_HEF/A_bed, in m/day and mm/day."""
        f = exchange_flux(0.1, 8640.0)                         # 0.1 m3/s over 8640 m2
        assert f["m_per_day"] == pytest.approx(0.1 * 86400.0 / 8640.0)   # = 1.0 m/day
        assert f["mm_per_day"] == pytest.approx(1000.0)

    def test_exchange_flux_zero_area_is_nan(self):
        import math
        assert math.isnan(exchange_flux(0.1, 0.0)["m_per_day"])
        assert math.isnan(exchange_flux(0.1, None)["mm_per_day"])

    def test_path_depth_metrics_weighted(self):
        d = path_depth_metrics([0.5, 1.0, 2.0, 4.0], [1, 1, 1, 1])
        assert d["max_m"] == 4.0
        assert d["p90_m"] >= d["p50_m"]

    def test_path_depth_metrics_empty(self):
        assert path_depth_metrics([], None) == {}
        assert path_depth_metrics([1.0], [0.0]) == {}          # zero total weight


class TestExceedance:
    def test_exceedance_semantics_and_monotone(self):
        t = [0.5, 1.0, 2.0, 5.0]                               # days
        w = [1.0, 1.0, 1.0, 1.0]
        p1 = exceedance_fraction(t, w, 1.0)                    # >= 1 day -> 3/4
        p2 = exceedance_fraction(t, w, 2.0)                    # >= 2 days -> 2/4
        assert p1 == pytest.approx(0.75) and p2 == pytest.approx(0.5)
        assert p1 >= p2                                        # non-increasing in threshold

    def test_exceedance_all_and_none(self):
        assert exceedance_fraction([1.0, 2.0], [1, 1], 0.0) == pytest.approx(1.0)
        assert exceedance_fraction([1.0, 2.0], [1, 1], 99.0) == pytest.approx(0.0)

    def test_exceedance_empty_is_nan(self):
        import math
        assert math.isnan(exceedance_fraction([], None, 1.0))


class TestResidenceTime:
    def test_fraction_bands(self):
        # 0.01 d (<1h), 0.5 d (1h–1d), 2 d (>1d), equal weights
        m = residence_time_metrics([0.01, 0.5, 2.0], [1, 1, 1], porosity=0.3)
        assert m["frac_above_1h"] == pytest.approx(2 / 3)
        assert m["frac_1h_to_1d"] == pytest.approx(1 / 3)
        assert m["frac_above_1d"] == pytest.approx(1 / 3)
        assert m["min_days"] == 0.01 and m["max_days"] == 2.0
        assert m["porosity"] == 0.3


def test_mobile_pore_storage():
    """§8.2: Σ hyporheic_fraction · saturated volume · porosity."""
    assert mobile_pore_storage([0.5, 1.0], [10.0, 20.0], 0.3) == pytest.approx(7.5)


def test_equivalent_active_depth():
    """D_HZ = V_HZ / A_bed (report §7.4). The framework's primary NORMALIZED extent metric: the
    streambed area already carries both reach length and channel width, which §7.5 requires."""
    from hype_app.metrics import equivalent_active_depth
    assert equivalent_active_depth(2460.0, 6000.0) == pytest.approx(0.41)
    # None, not NaN: every caller stores this straight onto an optional contract field.
    assert equivalent_active_depth(None, 6000.0) is None
    assert equivalent_active_depth(2460.0, None) is None
    assert equivalent_active_depth(2460.0, 0) is None
    assert equivalent_active_depth(2460.0, -1.0) is None


def test_pore_volume():
    """V_HZ · n. Sediment plus water in, water alone out; the two differ by a factor of three and
    are reported side by side, so mislabelling one as the other is the failure to guard."""
    from hype_app.metrics import pore_volume
    assert pore_volume(2460.0, 0.31) == pytest.approx(762.6)
    assert pore_volume(None, 0.3) is None
    assert pore_volume(2460.0, None) is None
    assert pore_volume(2460.0, 0.0) == 0.0          # a real answer, not a missing one
