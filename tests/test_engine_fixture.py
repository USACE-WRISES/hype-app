"""Engine-gated numerical checks against the deterministic MF6+MP7 fixture.

Skipped unless HYPE_MODFLOW_BIN points at native mf6/mp7 (see conftest). Validates the exact
machinery Phase 5 builds on: reading the cell-by-cell budget (per-CHD-package), mass balance,
and MODPATH7 endpoints — all against an analytic 1-D linear-flow solution.
"""
import os
from pathlib import Path

import numpy as np
import pytest

from build_model_fixture import GWF_NAME, MP7_NAME, _exe, build_fixture

pytestmark = pytest.mark.engine


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    env = os.getenv("HYPE_MODFLOW_BIN")
    ws = tmp_path_factory.mktemp("mf6fx")
    meta = build_fixture(ws, mf6_exe=_exe(env, "mf6"), mp7_exe=_exe(env, "mp7"))
    return ws, meta


def test_heads_are_analytic_linear(built):
    from flopy.utils import HeadFile
    ws, meta = built
    head = np.asarray(HeadFile(str(Path(ws) / f"{GWF_NAME}.hds")).get_data())[0, 0, :]
    assert np.allclose(head, meta["expected_head"], atol=1e-6)


def test_chd_mass_balance_and_per_cell_flow(built):
    """MF6 stores only the MODEL name in the budget, so CHD flows can't be filtered by
    package name — the two CHD packages come back as separate records keyed by cell node.
    Phase 5's flux-weighted reader therefore maps river/side flows by node membership (the
    way hz_analysis already identifies boundaries), which this test exercises directly."""
    from flopy.utils import CellBudgetFile
    ws, meta = built
    q = meta["expected_q_m3_per_day"]
    ncol = meta["ncol"]
    cbc = CellBudgetFile(str(Path(ws) / f"{GWF_NAME}.cbb"), precision="double")

    recs = cbc.get_data(text="CHD")          # one record per CHD package cell
    assert len(recs) == 2

    # Whole-CHD mass balance -> ~0.
    total = sum(float(np.asarray(r["q"]).sum()) for r in recs)
    assert abs(total) < 1e-6

    # Identify by node (robust; declaration order is not): smaller node = upgradient river.
    flow_by_node = {int(np.asarray(r["node"]).ravel()[0]): float(np.asarray(r["q"]).sum())
                    for r in recs}
    nodes = sorted(flow_by_node)
    assert flow_by_node[nodes[0]] == pytest.approx(q, abs=1e-6)    # col 0 injects +q
    assert flow_by_node[nodes[-1]] == pytest.approx(-q, abs=1e-6)  # col ncol-1 removes -q
    # nodes span the full 1-D domain (0-based node == column here).
    assert nodes[-1] - nodes[0] == ncol - 1


def test_read_chd_downwelling(built):
    """Phase 5 §8.3: read CHD_RIVER downwelling inflow keyed by node membership."""
    from hype_app.metrics import read_chd_downwelling
    ws, meta = built
    q = meta["expected_q_m3_per_day"]
    # CHD_RIVER cell is node 0 (col 0), injecting +q into the aquifer (downwelling here).
    downwelling = read_chd_downwelling(Path(ws) / f"{GWF_NAME}.cbb", river_nodes={0})
    assert downwelling.get(0) == pytest.approx(q, abs=1e-6)
    # the downgradient CHD_SIDES node removes water (q<0) -> not counted as downwelling
    assert read_chd_downwelling(Path(ws) / f"{GWF_NAME}.cbb",
                                river_nodes={meta["ncol"] - 1}) == {}


def test_forward_particle_exits_downgradient(built):
    from flopy.utils import EndpointFile

    from hypetool.functions.hz_analysis import RESOLVED_STATUSES
    ws, meta = built
    ep = EndpointFile(str(Path(ws) / f"{MP7_NAME}.mpend")).get_alldata()
    assert ep.shape[0] == 1
    # MP7 endpoint records carry a global 0-based `node` (no i/j); single row+layer means
    # node == column. Seeded at column 1, the particle must reach the downgradient CHD cell
    # (col ncol-1) and stop at a real terminus (status in {2,3,4,5,6}; here 5 = strong sink).
    assert int(ep["node"][0]) == meta["ncol"] - 1
    assert int(ep["status"][0]) in RESOLVED_STATUSES
    assert float(ep["time"][0]) > 0.0
