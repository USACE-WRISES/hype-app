"""Golden tests pinning the CURRENT gradient-profile numerics before Phase 4 replaces the
raw profile strings with structured controls. If these change, the replacement changed
behavior — which must be deliberate.

Pins:
* my_utils.parse_fraction_gradient_profile  (string -> sorted (fraction, gradient) pairs)
* my_utils.interpolate_gw_elevation_first_layer_only  (linear head fill across boundary cells)
"""
import pytest

from hypetool.functions.my_utils import (
    interpolate_gw_elevation_first_layer_only,
    parse_fraction_gradient_profile,
)


class TestParseFractionGradientProfile:
    def test_basic_sorted_pairs(self):
        assert parse_fraction_gradient_profile("0,0.01 0.5,0.05 1,0.1") == [
            (0.0, 0.01), (0.5, 0.05), (1.0, 0.1)]

    def test_unsorted_input_is_sorted_ascending(self):
        assert parse_fraction_gradient_profile("1,0.1 0,0.01 0.5,0.05") == [
            (0.0, 0.01), (0.5, 0.05), (1.0, 0.1)]

    def test_negative_gradients_preserved(self):
        assert parse_fraction_gradient_profile("0,-0.01 1,-0.02") == [
            (0.0, -0.01), (1.0, -0.02)]

    def test_duplicate_fraction_keeps_last_occurrence(self):
        # uniq dict keyed on rounded fraction keeps the last value seen after sorting.
        assert parse_fraction_gradient_profile("0,0.01 0.5,0.02 0.5,0.09 1,0.1") == [
            (0.0, 0.01), (0.5, 0.09), (1.0, 0.1)]

    def test_empty_or_blank_raises(self):
        with pytest.raises(ValueError):
            parse_fraction_gradient_profile("")
        with pytest.raises(ValueError):
            parse_fraction_gradient_profile("   ")
        with pytest.raises(ValueError):
            parse_fraction_gradient_profile(None)  # type: ignore[arg-type]

    def test_missing_endpoints_raises(self):
        with pytest.raises(ValueError):
            parse_fraction_gradient_profile("0.2,0.01 0.8,0.02")

    def test_no_parseable_pairs_raises(self):
        with pytest.raises(ValueError):
            parse_fraction_gradient_profile("garbage-no-pairs")

    def test_fraction_out_of_range_raises(self):
        with pytest.raises(ValueError):
            parse_fraction_gradient_profile("0,0.01 1.5,0.02")


class TestInterpolateGwElevationFirstLayerOnly:
    def test_linear_spread_across_five_cells(self):
        cells = [(0, 0, c) for c in range(5)]
        assert interpolate_gw_elevation_first_layer_only(cells, 10.0, 20.0) == [
            10.0, 12.5, 15.0, 17.5, 20.0]

    def test_two_cells_are_the_endpoints(self):
        assert interpolate_gw_elevation_first_layer_only(
            [(0, 0, 0), (0, 0, 1)], 3.0, 7.0) == [3.0, 7.0]

    def test_single_cell_returns_head_first(self):
        assert interpolate_gw_elevation_first_layer_only([(0, 0, 0)], 5.0, 9.0) == [5.0]

    def test_empty_returns_empty(self):
        assert interpolate_gw_elevation_first_layer_only([], 5.0, 9.0) == []
