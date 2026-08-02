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


def _build_interface_model(ws, mf6_exe, *, river_spd, side_spd):
    """2-layer × 1×11 model for interface-pass tests: CHD_RIVER on the top layer,
    CHD_SIDES at the domain edge. Returns the in-memory gwf after a successful run."""
    import flopy
    sim = flopy.mf6.MFSimulation(sim_name="iface", sim_ws=str(ws), exe_name=mf6_exe)
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
    flopy.mf6.ModflowIms(sim, complexity="SIMPLE", inner_dvclose=1e-9, outer_dvclose=1e-9)
    gwf = flopy.mf6.ModflowGwf(sim, modelname="gwf_model", save_flows=True)
    flopy.mf6.ModflowGwfdis(gwf, nlay=2, nrow=1, ncol=11, delr=1.0, delc=1.0,
                            top=10.0, botm=[5.0, 0.0])
    flopy.mf6.ModflowGwfic(gwf, strt=9.5)
    flopy.mf6.ModflowGwfnpf(gwf, save_flows=True, icelltype=0, k=1.0)
    flopy.mf6.ModflowGwfchd(gwf, pname="CHD_RIVER", save_flows=True,
                            filename="gwf_model.river.chd", stress_period_data=river_spd)
    flopy.mf6.ModflowGwfchd(gwf, pname="CHD_SIDES", save_flows=True,
                            filename="gwf_model.sides.chd", stress_period_data=side_spd)
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord="gwf_model.hds",
                           budget_filerecord="gwf_model.cbb",
                           saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")])
    sim.write_simulation()
    ok, _ = sim.run_simulation(silent=True)
    assert ok, "interface-model MF6 run failed"
    return gwf


def _run_iface(tmp_path, gwf, member):
    import numpy as np

    from hypetool.functions.hz_analysis import cell_geometry, load_heads, run_interface_pass
    env = os.getenv("HYPE_MODFLOW_BIN")
    dis = gwf.get_package("DIS")
    T, B = cell_geometry(np.asarray(dis.top.array), np.asarray(dis.botm.array))
    return run_interface_pass(
        gwf, member=member, idomain=np.ones((2, 1, 11), dtype=int),
        head=load_heads(gwf), T=T, B=B, gwf_ws=tmp_path, hz_ws=tmp_path / "hz",
        exe=_exe(env, "mp7"), porosity=0.3, max_time_days=1.0e6,
        particles_per_cell=4, cell_area2d=np.ones((1, 11)), log=lambda m: None)


def test_interface_pass_returning_loop(tmp_path):
    """§8.3/§14.10: a hyporheic loop (stream in at col 2, back out at col 8; weak side pull)
    yields returning flux with a closed weighted mass balance."""
    import numpy as np

    from hypetool.functions.hz_analysis import MEMBER, read_river_downwelling
    env = os.getenv("HYPE_MODFLOW_BIN")
    # col 8 (9.0) is the LOWEST head -> the strongest sink: subsurface water released under
    # col 2 flows toward it and returns to the stream. The side (9.2) sits between the two
    # stream stages, so col 2 is still the only net-downwelling stream cell.
    gwf = _build_interface_model(
        tmp_path, _exe(env, "mf6"),
        river_spd=[[(0, 0, 2), 9.5], [(0, 0, 8), 9.0]],
        side_spd=[[(0, 0, 10), 9.2], [(1, 0, 10), 9.2]])

    ncol = 11
    member = np.zeros(2 * ncol, dtype=np.uint8)
    member[2] = MEMBER["top"]
    member[8] = MEMBER["top"]
    member[10] = MEMBER["left"]
    member[ncol + 10] = MEMBER["left"]

    downwelling = read_river_downwelling(tmp_path, member)
    assert set(downwelling) == {2} and downwelling[2] > 0

    flux = _run_iface(tmp_path, gwf, member)
    assert flux is not None
    acc = flux["accounting"]
    assert acc["total_downwelling"] == pytest.approx(downwelling[2], rel=1e-6)
    total = acc["returning"] + acc["losing"] + acc["unresolved"]
    assert total == pytest.approx(acc["total_downwelling"], rel=1e-6)
    assert acc["mass_balance_error"] == pytest.approx(0.0, abs=1e-6)
    assert acc["returning"] > 0                    # the loop returns stream water
    assert acc["unresolved"] == pytest.approx(0.0, abs=1e-9)
    pp = flux["per_particle"]
    # side seeds ride the same run since the four-way extension — the downwelling
    # invariant holds for the stream-origin subset (unreleased is 0 here)
    top = pp["origin_code"] == MEMBER["top"]
    assert float(pp["weight"][top].sum()) == pytest.approx(acc["total_downwelling"], rel=1e-6)
    ret = pp["cls"] == 1
    assert ret.any() and (pp["time_days"][ret] > 0).all()
    assert flux["rtd"] is not None and flux["rtd"]["weighted_mean_days"] > 0
    # active streambed area: only the net-downwelling stream column (col 2) feeds returning paths
    assert acc["active_streambed_area_m2"] == pytest.approx(1.0)
    # ...and the loop surfaces at col 8, a DIFFERENT column, so the entry and exit sets are
    # disjoint here and the connected area is their union. Every cell is 1 m2 in this fixture, so
    # these are cell counts: 1 in, 1 out, 2 engaged. Counting only entry would report half the bed
    # that is actually exchanging.
    assert acc["return_streambed_area_m2"] == pytest.approx(1.0)
    assert acc["connected_streambed_area_m2"] == pytest.approx(2.0)
    # the optional second (pathline) pass attached a per-particle max penetration depth
    assert "max_depth_m" in pp
    md = pp["max_depth_m"][ret]
    assert np.isfinite(md).any() and (md[np.isfinite(md)] >= 0).all()


def test_interface_pass_losing_only(tmp_path):
    """§8.3: with a single stream cell and a strong side sink, ALL downwelling flux is
    classified losing (leaves through a model side)."""
    import numpy as np

    from hypetool.functions.hz_analysis import MEMBER
    env = os.getenv("HYPE_MODFLOW_BIN")
    gwf = _build_interface_model(
        tmp_path, _exe(env, "mf6"),
        river_spd=[[(0, 0, 2), 9.5]],
        side_spd=[[(0, 0, 10), 8.0], [(1, 0, 10), 8.0]])

    ncol = 11
    member = np.zeros(2 * ncol, dtype=np.uint8)
    member[2] = MEMBER["top"]
    member[10] = MEMBER["left"]
    member[ncol + 10] = MEMBER["left"]

    flux = _run_iface(tmp_path, gwf, member)
    assert flux is not None
    acc = flux["accounting"]
    assert acc["losing"] == pytest.approx(acc["total_downwelling"], rel=1e-6)
    assert acc["returning"] == pytest.approx(0.0, abs=1e-9)
    assert acc["mass_balance_error"] == pytest.approx(0.0, abs=1e-6)


def test_interface_pass_four_way(tmp_path):
    """§8.3 four-way: a high-head side feeds the domain (gaining + throughflow) while both
    stream cells are net sinks — the pass must run WITHOUT any downwelling (the old code
    skipped entirely), the side ledger must close, and the budget identities must hold."""
    import numpy as np

    from hypetool.functions.hz_analysis import MEMBER, read_boundary_flows
    env = os.getenv("HYPE_MODFLOW_BIN")
    gwf = _build_interface_model(
        tmp_path, _exe(env, "mf6"),
        river_spd=[[(0, 0, 3), 9.4], [(0, 0, 7), 9.1]],
        side_spd=[[(0, 0, 0), 9.8], [(1, 0, 0), 9.8],
                  [(0, 0, 10), 8.9], [(1, 0, 10), 8.9]])

    ncol = 11
    member = np.zeros(2 * ncol, dtype=np.uint8)
    member[3] = MEMBER["top"]
    member[7] = MEMBER["top"]
    for lay in (0, 1):
        member[lay * ncol + 0] = MEMBER["left"]
        member[lay * ncol + 10] = MEMBER["right"]

    flows = read_boundary_flows(tmp_path, member)
    assert flows["side_in"] and all(q > 0 for q in flows["side_in"].values())
    assert flows["up_m3d"] > 0 and flows["side_out_m3d"] > 0
    tin = sum(flows["down"].values()) + sum(flows["side_in"].values())
    tout = flows["up_m3d"] + flows["side_out_m3d"]
    assert tin == pytest.approx(tout, rel=1e-6)    # solver closure, straight from the budget

    flux = _run_iface(tmp_path, gwf, member)
    assert flux is not None
    acc = flux["accounting"]
    assert acc["gaining"] > 0                      # the side water discharging to the stream
    assert acc["throughflow"] >= 0
    assert acc["returning"] + acc["losing"] + acc["unresolved"] == pytest.approx(
        acc["total_downwelling"], rel=1e-6, abs=1e-9)
    assert acc["gaining"] + acc["throughflow"] + acc["side_unresolved"] == pytest.approx(
        acc["total_side_inflow"], rel=1e-6)
    assert acc["closure_error_global"] == pytest.approx(0.0, abs=1e-6)
    assert acc["streambed_area_m2"] == pytest.approx(2.0)   # two stream plan cells × 1 m²
    # exchange-map payload carries every stream cell with its net signed flow
    sc = dict(zip(flux["stream_cells"]["node"], flux["stream_cells"]["q_m3d"]))
    assert set(sc) == {3, 7}
    assert flux["rtd_by_class"].get("gaining", {}).get("weighted_mean_days", 0) > 0
    pp = flux["per_particle"]
    assert set(np.unique(pp["origin_code"])) <= {MEMBER["top"], MEMBER["left"],
                                                 MEMBER["right"]}


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
