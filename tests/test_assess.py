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
        mobile_pore_storage_m3=2460.0, reference_area_m2=3100.0, porosity=0.3)

    # connectivity: (0.028/0.736) * (1609.344/500)
    assert res.connectivity.excursions_per_mile == pytest.approx(0.028 / 0.736 * 1609.344 / 500)
    assert res.connectivity.mass_balance_error == pytest.approx(0.0, abs=1e-9)
    # zone from hz_stats
    assert res.zone.bulk_saturated_volume_m3 == 8200.0
    assert res.zone.mobile_pore_storage_m3 == 2460.0
    # RTD computed
    assert res.residence_time.weighted_median_days == pytest.approx(1.5)
    # HFCI fully computed (all three drivers present)
    assert res.hfci.hfci is not None and 0.0 <= res.hfci.hfci <= 1.0
    assert all(0 <= c.score <= 15 for c in (res.hfci.exchange, res.hfci.storage, res.hfci.processing))
    # frozen provenance
    assert res.input_hash == _snapshot().input_hash
    assert set(res.group_hashes)


def test_connectivity_unavailable_without_exchange():
    res = build_results(_snapshot(), hz_stats=_HZ_STATS, streamflow_cms=0.736,
                        reach_length_m=500.0, exchange=None)
    assert res.connectivity.excursions_per_mile is None
    assert res.connectivity.unavailable_reason
    assert any(w.code == "connectivity_unavailable" for w in res.warnings)
    # HFCI exchange component is not computable, so HFCI itself is not computable
    assert res.hfci.hfci is None
    assert res.hfci.exchange.score is None


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
