"""Flux-weighted metric tests (spec §8, §13.1, §14.10, §14.11)."""
import numpy as np
import pytest

from hype_app.metrics import (
    classify_weighted_flux,
    connectivity,
    mobile_pore_storage,
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

    def test_unavailable_when_no_streamflow(self):
        assert connectivity(streamflow=0.0, returning_hyporheic=10.0, total_downwelling=30.0,
                            losing=0.0, unresolved=0.0, reach_length_m=100.0) is None


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
