"""End-to-end GMS export against a real MF6 + MP7 run (engine-gated).

The written GMS files are read back with the INDEPENDENT struct readers from
test_gms_writers, and the flipped budget must balance cell-by-cell: that closes the
loop on the row-flip design (grid.py) with real solver output rather than synthetic
arrays.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from build_gms_fixture import GWF_NAME, build_gms_fixture
from build_model_fixture import _exe
from test_gms_writers import read_ccf, read_hed, read_pth

from hype_app.gms import export_gms_project
from hype_app.gms import tree as gtree
from hype_app.gms.grid import flip_cc

pytestmark = pytest.mark.engine

h5py = pytest.importorskip("h5py")

WKT = 'PROJCS["HYPE Test UTM",UNIT["metre",1.0]]'


@pytest.fixture(scope="module")
def exported(tmp_path_factory):
    env = os.getenv("HYPE_MODFLOW_BIN")
    ws = tmp_path_factory.mktemp("gms_fixture")
    meta = build_gms_fixture(ws, mf6_exe=_exe(env, "mf6"), mp7_exe=_exe(env, "mp7"))
    out = tmp_path_factory.mktemp("gms_out")
    result = export_gms_project(ws, out, name="Fix", crs_wkt_esri=WKT,
                                porosity=meta["porosity"],
                                hz_dir=meta["hz_dir"], log=lambda *_: None)
    return ws, out, meta, result


def test_layout_and_manifest(exported):
    ws, out, meta, result = exported
    assert Path(result["gpr"]).is_file()
    mfdir = Path(result["modflow_dir"])
    for ext in ("mfs mfn mfr mfw prj dis ba6 lpf oc chd pcg out h5 hed ccf".split()):
        assert (mfdir / f"Fix.{ext}").is_file(), ext
    assert (mfdir / "Fix.hed.h5").is_file()
    assert len(result["modpath_dirs"]) == 2
    assert result["n_particles"] == {"forward": meta["n_seeds"],
                                     "backward": meta["n_seeds"]}
    assert result["warnings"] == []


def test_heads_roundtrip(exported):
    ws, out, meta, _ = exported
    import flopy
    hds = flopy.utils.HeadFile(Path(meta["gwf_ws"]) / f"{GWF_NAME}.hds",
                               precision="double")
    head_e = np.asarray(hds.get_data())
    idomain = np.asarray(meta["idomain"])
    headers, layers = read_hed(out / "Fix_MODFLOW" / "Fix.hed")
    got = np.stack(layers)
    expect = flip_cc(head_e).astype(np.float32)
    mask = flip_cc(idomain) == 0
    assert (got[mask] == np.float32(-999.0)).all()
    np.testing.assert_allclose(got[~mask], expect[~mask], rtol=1e-6)


def test_written_budget_balances(exported):
    ws, out, meta, _ = exported
    recs = read_ccf(out / "Fix_MODFLOW" / "Fix.ccf")
    chd, frf, fff, flf = recs[0]["list"], recs[1]["array"], recs[2]["array"], recs[3]["array"]
    nlay, nrow, ncol = frf.shape

    def shifted(a, axis):
        pad = [(0, 0)] * 3
        pad[axis] = (1, 0)
        return np.pad(a, pad)[tuple(slice(None, -1) if ax == axis else slice(None)
                                    for ax in range(3))]

    div = (shifted(frf, 2) - frf + shifted(fff, 1) - fff + shifted(flf, 0) - flf)
    net = div.ravel().astype(np.float64)
    net[chd["node"] - 1] += chd["q"].astype(np.float64)
    active = flip_cc(np.asarray(meta["idomain"])).ravel() != 0
    scale = max(np.abs(chd["q"]).max(), 1.0)
    assert np.abs(net[active]).max() <= 1e-4 * scale


def test_pathlines_and_loc_consistency(exported):
    ws, out, meta, _ = exported
    nlay, nrow, ncol = meta["nlay"], meta["nrow"], meta["ncol"]
    total_y = nrow * meta["delc"]
    for dirname, setname in (("Fix_MODPATH_forward particles", "forward particles"),
                             ("Fix_MODPATH_backward particles", "backward particles")):
        recs, trailer = read_pth(out / dirname / f"{setname}.pth")
        assert trailer == 1
        assert recs["flag"][0] == 0 and (recs["flag"][1:] == 1).all()
        assert recs["node"].min() >= 1
        assert recs["node"].max() <= nlay * nrow * ncol
        assert recs["y"].min() >= -1e-5 and recs["y"].max() <= total_y + 1e-5
        assert (recs["zloc"] >= 0).all() and (recs["zloc"] <= 1).all()
        loc_rows = [ln.split() for ln in
                    (out / dirname / f"{setname}.loc").read_text().splitlines()]
        assert len(loc_rows) == meta["n_seeds"]
        for pid in np.unique(recs["pid"]):
            t = recs["time"][recs["pid"] == pid]
            assert (np.diff(t) >= 0).all()
            first_node = int(recs["node"][recs["pid"] == pid][0])
            col, row, lay = (int(v) for v in loc_rows[pid - 1][:3])
            per = nrow * ncol
            expect = (lay - 1) * per + (row - 1) * ncol + col
            assert first_node == expect


def test_gpr_consistency(exported):
    ws, out, meta, _ = exported
    nlay, nrow, ncol = meta["nlay"], meta["nrow"], meta["ncol"]
    ncells = nlay * nrow * ncol
    with h5py.File(out / "Fix.gpr", "r") as f:
        g = f["3DGridModule/3DGrid 0"]
        assert int(np.ravel(g.attrs["NumI"])[0]) == nrow
        assert int(np.ravel(g.attrs["NumJ"])[0]) == ncol
        assert int(np.ravel(g.attrs["NumK"])[0]) == nlay
        origin = np.ravel(g.attrs["Origin"])
        assert origin[0] == pytest.approx(meta["xmin"])
        assert origin[1] == pytest.approx(meta["ymin"] + nrow * meta["delc"])
        assert g["Zone Budget IDs/Values"].shape == (1, ncells)
        wkt = np.ravel(g["Coordinates"].attrs["WKT"])[0]
        assert b"HYPE Test UTM" in wkt

        # seed cells in the .gpr match the forward set's .pth
        recs, _ = read_pth(out / "Fix_MODPATH_forward particles"
                           / "forward particles.pth")
        first_nodes = [int(recs["node"][recs["pid"] == pid][0])
                       for pid in np.unique(recs["pid"])]
        np.testing.assert_array_equal(f["Particles/MODFLOW/Set1/Id"][:], first_nodes)

        tree = gtree.parse_tokens(gtree.read_tokens(f))
        names = [n.name for n in gtree.iter_nodes(tree) if n.trtype == "TIPARTSET"]
        assert names == ["forward particles", "backward particles"]

        sol = f["Solutions"]
        assert sol["Solution File"][0] == b"Fix_MODFLOW\\Fix.mfr"


def test_mfh5_specified_heads_match_solution(exported):
    ws, out, meta, _ = exported
    import flopy
    hds = flopy.utils.HeadFile(Path(meta["gwf_ws"]) / f"{GWF_NAME}.hds",
                               precision="double")
    head_g = flip_cc(np.asarray(hds.get_data()))
    with h5py.File(out / "Fix_MODFLOW" / "Fix.h5", "r") as f:
        sh = f["Specified Head"]
        nodes1 = sh["02. Cell IDs"][:]
        prop = sh["07. Property"][:]
        np.testing.assert_allclose(prop[0, :, 0], head_g.ravel()[nodes1 - 1],
                                   rtol=1e-9)
        iface = sh["06. IFACE"][:]
        assert set(iface.tolist()) == {0, 6}       # river cells carry IFACE 6
