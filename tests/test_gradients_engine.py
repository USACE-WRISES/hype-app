"""Structured-gradient head-anchor engine tests (spec §7.4–7.6, §14.7, §14.8)."""
import pytest

from hype_app.contracts import (
    GradientBoundaryConfigV2,
    GradientControl,
    GradientQualitative,
    ReferenceSlope,
    Side,
)
from hype_app.gradients import (
    ControlGeometry,
    anchor_head,
    config_from_legacy_corners,
    interpolate_to_stations,
    realized_side_heads,
    reference_slope_from_samples,
    validate_config,
)


def test_anchor_head():
    # head = WSE + gradient * distance
    assert anchor_head(100.0, 0.01, 50.0) == pytest.approx(100.5)
    assert anchor_head(100.0, -0.02, 25.0) == pytest.approx(99.5)   # losing -> below WSE


def test_interpolate_to_stations():
    heads = interpolate_to_stations([0.0, 1.0], [10.0, 20.0], [0.0, 0.25, 0.5, 1.0])
    assert list(heads) == [10.0, 12.5, 15.0, 20.0]


def test_interpolate_clamps_outside_range():
    heads = interpolate_to_stations([0.2, 0.8], [10.0, 20.0], [0.0, 1.0])
    assert heads[0] == 10.0 and heads[1] == 20.0     # np.interp clamps at ends


class TestRealizedSideHeads:
    def test_exact_anchor_and_interpolated_heads(self):
        """§14.7: structured controls produce exact expected anchor + interpolated heads."""
        controls = [GradientControl(id="l0", side=Side.left, station=0.0, preferred=0.01),
                    GradientControl(id="l1", side=Side.left, station=1.0, preferred=0.02)]
        # WSE 100 at both, distances 50 and 25 -> anchor heads 100.5 and 100.5
        geoms = [ControlGeometry(controls[0], wse=100.0, distance=50.0),
                 ControlGeometry(controls[1], wse=100.0, distance=25.0)]
        heads, diag = realized_side_heads(geoms, [0.0, 0.5, 1.0])
        assert diag[0]["anchor_head"] == pytest.approx(100.5)   # 100 + 0.01*50
        assert diag[1]["anchor_head"] == pytest.approx(100.5)   # 100 + 0.02*25
        assert list(heads) == pytest.approx([100.5, 100.5, 100.5])

    def test_lower_upper_scenarios(self):
        c = GradientControl(id="l0", side=Side.left, station=0.0, preferred=0.01,
                            lower=0.0, upper=0.02)
        c1 = GradientControl(id="l1", side=Side.left, station=1.0, preferred=0.01,
                             lower=0.0, upper=0.02)
        geoms = [ControlGeometry(c, wse=100.0, distance=100.0),
                 ControlGeometry(c1, wse=100.0, distance=100.0)]
        pref, _ = realized_side_heads(geoms, [0.0], which="preferred")
        low, _ = realized_side_heads(geoms, [0.0], which="lower")
        high, _ = realized_side_heads(geoms, [0.0], which="upper")
        assert pref[0] == pytest.approx(101.0)   # 100 + 0.01*100
        assert low[0] == pytest.approx(100.0)    # 100 + 0.0*100
        assert high[0] == pytest.approx(102.0)   # 100 + 0.02*100


class TestReferenceSlope:
    def test_positive_slope(self):
        rs = reference_slope_from_samples(105.0, 100.0, 500.0, source="wse_raster")
        assert rs is not None and rs.value == pytest.approx(0.01)
        assert rs.source == "wse_raster"

    def test_flat_or_adverse_returns_none(self):
        assert reference_slope_from_samples(100.0, 100.0, 500.0, source="dem_drop") is None
        assert reference_slope_from_samples(100.0, 105.0, 500.0, source="dem_drop") is None
        assert reference_slope_from_samples(105.0, 100.0, 0.0, source="dem_drop") is None


class TestQualitativeAndValidation:
    def test_qualitative_multipliers_via_config(self):
        """§14.8: locked qualitative multipliers × reference slope."""
        rs = ReferenceSlope(value=0.02, source="wse_raster")
        cfg = GradientBoundaryConfigV2.from_qualitative(
            left=GradientQualitative.strongly_gaining,
            right=GradientQualitative.strongly_losing, reference_slope=rs)
        assert all(c.preferred == pytest.approx(0.02) for c in cfg.left_controls)    # +1.0 * 0.02
        assert all(c.preferred == pytest.approx(-0.02) for c in cfg.right_controls)  # -1.0 * 0.02

    def test_validate_sign_change_warning(self):
        cfg = GradientBoundaryConfigV2(left_controls=[
            GradientControl(id="a", side=Side.left, station=0.0, preferred=0.01),
            GradientControl(id="b", side=Side.left, station=1.0, preferred=-0.01)])
        codes = {w.code for w in validate_config(cfg)}
        assert "gradient_sign_change" in codes

    def test_legacy_corner_upgrade(self):
        controls = config_from_legacy_corners({"g_ul": 0.01, "g_dl": 0.02}, side=Side.left)
        assert [c.station for c in controls] == [0.0, 1.0]
        assert controls[0].preferred == 0.01 and controls[1].preferred == 0.02
        assert all(c.source == "legacy_upgrade" for c in controls)
