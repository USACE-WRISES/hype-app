"""Results-assembly orchestration tests (spec §11.2)."""
import pytest

from hype_app.assess import build_results
from hype_app.contracts import (
    AssessmentInputSnapshot,
    GradientBoundaryConfigV2,
    GridSettings,
    KSettings,
    StreamflowInput,
)
from hype_app.metrics import ExchangeAccounting
from hype_app.provenance import Provenance


def _snapshot():
    return AssessmentInputSnapshot(
        assessment_id="A1",
        streamflow=StreamflowInput(value_cms=0.736, provenance=Provenance(source="USGS")),
        k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.3),
        gradients=GradientBoundaryConfigV2(),
        grid=GridSettings(cell_size_x=10.0, cell_size_y=10.0, gw_mod_depth=20.0,
                          layer_thickness=0.5))


_HZ_STATS = {"hyporheic": {"volume_m3": 8200.0, "footprint_m2": 3100.0,
                           "thickness_mean_m": 1.3, "thickness_max_m": 2.9}}


def test_full_assembly_computes_all_metrics():
    exch = ExchangeAccounting(total_downwelling=0.09, returning_hyporheic=0.028,
                              losing_to_sides=0.062, unresolved=0.0)
    res = build_results(
        _snapshot(), hz_stats=_HZ_STATS, streamflow_cms=0.736, reach_length_m=500.0,
        exchange=exch, transit_times_days=[0.5, 1.5, 3.0], transit_weights=[1, 1, 1],
        mobile_pore_storage_m3=2460.0, streambed_area_m2=8000.0,
        active_streambed_area_m2=4000.0, porosity=0.3)

    c, z = res.connectivity, res.zone
    # frequency of hyporheic exchange
    assert c.excursions_per_mile == pytest.approx(0.028 / 0.736 * 1609.344 / 500)
    assert c.turnovers_per_km == pytest.approx(0.028 / 0.736 * 1000.0 / 500.0)
    assert c.turnovers_per_km == pytest.approx(1.0 / c.turnover_length_km)   # reciprocal
    assert c.exchange_flux_mm_day == pytest.approx(0.028 * 86400.0 / 8000.0 * 1000.0)
    assert c.active_streambed_fraction == pytest.approx(0.5)
    assert c.mass_balance_error == pytest.approx(0.0, abs=1e-9)
    # extent of hyporheic zone: D_HZ = V_HZ / A_bed (bulk basis)
    assert z.bulk_saturated_volume_m3 == 8200.0
    assert z.equivalent_active_depth_m == pytest.approx(8200.0 / 8000.0)
    assert z.active_volume_basis == "bulk sediment"
    assert z.mobile_pore_storage_m3 == 2460.0
    # duration in hyporheic zone
    assert res.residence_time.weighted_median_days == pytest.approx(1.5)
    # frozen provenance
    assert res.input_hash == _snapshot().input_hash
    assert set(res.group_hashes)


def test_thresholds_monotone_and_functional():
    """report §10: 4 default scenarios; exceedance non-increasing; functional flow = Q_HEF * P."""
    exch = ExchangeAccounting(total_downwelling=0.2, returning_hyporheic=0.12,
                              losing_to_sides=0.08, unresolved=0.0)
    # returning transit times in days: 0.5 h, 8 h, 30 h -> exceedance drops as threshold rises
    res = build_results(
        _snapshot(), hz_stats=_HZ_STATS, streamflow_cms=0.75, reach_length_m=1000.0,
        exchange=exch, transit_times_days=[0.5 / 24, 8.0 / 24, 30.0 / 24],
        transit_weights=[1, 1, 1], streambed_area_m2=8000.0, porosity=0.3)
    ths = {t.threshold_value_h: t for t in res.thresholds}
    assert set(ths) == {1.0, 6.0, 12.0, 24.0}
    fracs = [ths[h].flow_exceedance_fraction for h in (1.0, 6.0, 12.0, 24.0)]
    assert all(a >= b - 1e-9 for a, b in zip(fracs, fracs[1:]))          # monotone non-increasing
    t6 = ths[6.0]
    assert t6.functional_exchange_m3_s == pytest.approx(0.12 * t6.flow_exceedance_fraction)
    assert t6.functional_connectivity_per_km == pytest.approx(
        res.connectivity.turnovers_per_km * t6.flow_exceedance_fraction)
    # QC diagnostics recorded, no QC warnings for a clean run
    assert res.quality_diagnostics.get("residence_order_ok") is True
    assert not any(w.code == "threshold_monotonicity" for w in res.warnings)


def test_connectivity_unavailable_without_exchange():
    res = build_results(_snapshot(), hz_stats=_HZ_STATS, streamflow_cms=0.736,
                        reach_length_m=500.0, exchange=None)
    assert res.connectivity.excursions_per_mile is None
    assert res.connectivity.unavailable_reason
    assert any(w.code == "connectivity_unavailable" for w in res.warnings)


def test_report_generation_from_assembled_results(tmp_path):
    """End-to-end: assemble -> generate every report format from the model."""
    from hype_app.report import generate_report
    exch = ExchangeAccounting(total_downwelling=0.09, returning_hyporheic=0.028,
                              losing_to_sides=0.062, unresolved=0.0)
    res = build_results(_snapshot(), hz_stats=_HZ_STATS, streamflow_cms=0.736,
                        reach_length_m=500.0, exchange=exch,
                        transit_times_days=[0.5, 1.5, 3.0], transit_weights=[1, 1, 1],
                        mobile_pore_storage_m3=2460.0, reference_area_m2=3100.0, porosity=0.3)
    paths = generate_report(res, tmp_path, app_version="2026.07")
    assert "pdf_error" not in paths
    from pathlib import Path
    assert Path(paths["pdf"]).read_bytes().startswith(b"%PDF")
