"""The HDF5 halves of the GMS export: the MFH5 model file and the patched .gpr."""
from __future__ import annotations

import numpy as np
import pytest

from hype_app.gms import gpr as ggpr
from hype_app.gms import modflow_files as mff
from hype_app.gms import tree as gtree
from hype_app.gms.grid import gms_grid_from

h5py = pytest.importorskip("h5py")


def _tiny_inputs(nlay=2, nrow=3, ncol=4):
    rng = np.random.default_rng(11)
    ncl = nrow * ncol
    ibound = np.ones((nlay, nrow, ncol), dtype=np.int32)
    ibound[0, 0, 0] = 0
    return dict(
        ibound_g=ibound,
        strt_g=rng.normal(100, 1, (nlay, nrow, ncol)),
        top_g=rng.normal(105, 1, (nrow, ncol)),
        botm_g=np.stack([np.full((nrow, ncol), 100.0 - k) for k in range(nlay)]),
        hk_g=np.full((nlay, nrow, ncol), 5.0),
        vani_g=np.full((nlay, nrow, ncol), 3.0),
        chd_node1_g=np.array([1, 4, 2 * ncl], dtype=np.int64),
        chd_head=np.array([104.0, 103.5, 101.0]),
        chd_iface=np.array([6, 6, 0], dtype=np.int32),
    )


def test_write_mfh5_structure(tmp_path):
    inputs = _tiny_inputs()
    mff.write_mfh5(tmp_path, "T", ggpr.MFH5_SKELETON, **inputs)
    with h5py.File(tmp_path / "T.h5", "r") as f:
        assert f["File Type"][0] == b"Xmdf"
        assert float(f["MFH5 Version"][0]) == pytest.approx(3.0)
        for grp in ("River", "Drain", "Well", "Recharge", "General Head"):
            assert grp in f                          # skeleton BC groups ride along
        a = f["Arrays"]
        n = 12
        for k in range(2):
            for stem in ("ibound", "StartHead", "bot", "HK", "HANI", "VANI"):
                assert a[f"{stem}{k + 1}"].shape == (n,), stem
        assert a["top1"].shape == (n,)
        assert a["ibound1"].dtype == np.int32
        assert a["HK1"].dtype == np.float64
        np.testing.assert_array_equal(a["ibound1"][:],
                                      inputs["ibound_g"][0].ravel())
        np.testing.assert_array_equal(a["HANI2"][:], np.ones(n))

        sh = f["Specified Head"]
        assert sh["00. Number of BCs"][0] == 3
        assert sh["01. Use Last"][0] == 0
        np.testing.assert_array_equal(sh["02. Cell IDs"][:], [1, 4, 24])
        np.testing.assert_array_equal(sh["06. IFACE"][:], [6, 6, 0])
        prop = sh["07. Property"][:]
        assert prop.shape == (6, 3, 1)
        np.testing.assert_array_equal(prop[0, :, 0], inputs["chd_head"])
        np.testing.assert_array_equal(prop[1, :, 0], inputs["chd_head"])
        np.testing.assert_array_equal(prop[2:4, :, 0], np.ones((2, 3)))
        np.testing.assert_array_equal(prop[4:6, :, 0], np.zeros((2, 3)))
        assert sh["03. Name"].attrs["Max. String Length"][0] == 1


def test_write_hed_h5_structure(tmp_path):
    inputs = _tiny_inputs()
    head = inputs["strt_g"].copy()
    head[0, 0, 0] = 1e30
    mff.write_hed_h5(tmp_path, "T", head_g=head, ibound_g=inputs["ibound_g"],
                     totim=1.0)
    with h5py.File(tmp_path / "T.hed.h5", "r") as f:
        hd = f["Datasets/Head"]
        assert hd.attrs["Grouptype"][0] == b"DATASET SCALAR"
        assert hd["Values"].shape == (1, 24)
        assert hd["Values"][0, 0] == np.float32(mff.HNOFLO)
        assert hd["Active"][0, 0] == 0 and hd["Active"][0, 1] == 1
        active_vals = hd["Values"][0][hd["Active"][0].astype(bool)]
        assert hd["Mins"][0] == pytest.approx(active_vals.min())
        assert hd["Maxs"][0] == pytest.approx(active_vals.max())
        assert hd["Times"][0] == 1.0


# ---------------------------------------------------------------------------
# .gpr patching
# ---------------------------------------------------------------------------

def _patch(tmp_path, with_sets: bool):
    nlay, nrow, ncol = 2, 3, 4
    grid = gms_grid_from(nlay, nrow, ncol, np.full(ncol, 2.0), np.full(nrow, 3.0),
                         xmin=500.0, ymin=6000.0,
                         botm=np.full((nlay, nrow, ncol), 88.0))
    sets = []
    if with_sets:
        for nm, code in (("forward particles", 0), ("backward particles", 1)):
            sets.append(ggpr.GprParticleSet(
                set_name=nm, direction_code=code,
                rsp_relpath=f"Site_MODPATH_{nm}\\{nm}.rsp",
                node1_g=np.array([1, 5], dtype=np.int64),
                locx=np.array([0.5, 0.5]), locy=np.array([0.5, 0.25]),
                locz=np.array([1.0, 1.0])))
    out = tmp_path / "Site.gpr"
    guids = ggpr.patch_gpr(out, name="Site", grid=grid, wkt="PROJCS[\"UTM Test\"]",
                           porosity=0.31, particle_sets=sets)
    return out, grid, sets, guids


def test_patch_gpr_full(tmp_path):
    out, grid, sets, guids = _patch(tmp_path, with_sets=True)
    ncells = grid.ncells
    with h5py.File(out, "r") as f:
        assert f["GUID"][0].decode() == guids["project"]
        g = f["3DGridModule/3DGrid 0"]
        assert int(np.ravel(g.attrs["NumI"])[0]) == 3
        assert int(np.ravel(g.attrs["NumJ"])[0]) == 4
        assert int(np.ravel(g.attrs["NumK"])[0]) == 2
        assert float(np.ravel(g.attrs["Bearing"])[0]) == 0.0
        np.testing.assert_allclose(np.ravel(g.attrs["Origin"]),
                                   [500.0, 6009.0, 88.0])
        np.testing.assert_allclose(g["CoordsI"][:], [0, 3, 6, 9])
        np.testing.assert_allclose(g["CoordsJ"][:], [0, 2, 4, 6, 8])
        np.testing.assert_allclose(g["CoordsK"][:], [0, 1, 2])
        assert g["Zone Budget IDs/Values"].shape == (1, ncells)
        assert g["Zone Budget IDs/Values"].chunks == (1, ncells)
        assert g["MODFLOW Model 0/Zone Budget IDs/Values"].shape == (1, ncells)
        assert g["Material Sets/default/Values"].shape == (1, ncells)
        assert g["GridCellProps/Porosity"].shape == (ncells,)
        assert g["GridCellProps/Porosity"][0] == pytest.approx(0.31)
        assert g["MODFLOW Model 0/Name File"][0] == b".\\Site_MODFLOW\\Site.mfn"
        assert g["PROPERTIES/GUID"][0].decode() == guids["grid"]
        wkt = g["Coordinates"].attrs["WKT"]
        assert b"UTM Test" in np.ravel(wkt)[0]

        sol = f["Solutions"]
        assert sol["Solution File"][0] == b"Site_MODFLOW\\Site.mfr"
        assert sol["GUID"][0].decode() == guids["solution"]
        assert sol["Geometry Guid"][0].decode() == guids["grid"]

        mfl = f["Model Files"]
        assert [x.decode() for x in mfl["Model Files"][:]] == \
            ["Site_MODPATH_forward particles\\forward particles.rsp",
             "Site_MODPATH_backward particles\\backward particles.rsp"]
        assert mfl["GUID"].shape == (3,)
        assert mfl["GUID"][0].decode() == guids["modflow"]
        assert mfl["GUID"][1] == b"" and mfl["GUID"][2] == b""
        assert {x.decode() for x in mfl["Geometry GUID"][:]} == {guids["grid"]}

        pm = f["Particles/MODFLOW"]
        assert pm["Grid3dGuid"][0].decode() == guids["grid"]
        assert pm["Set1/Name"][0] == b"forward particles"
        assert pm["Set1/Direction"][0] == 0
        assert pm["Set2/Direction"][0] == 1
        np.testing.assert_array_equal(pm["Set1/Id"][:], [1, 5])
        np.testing.assert_allclose(pm["Set2/Y Locations"][:], [0.5, 0.25])
        assert pm["Set1/GUID"][0].decode() == sets[0].guid

        names = [n.decode() for n in
                 f["Project/ModelManager/Model Interfaces/Model Interface Name"][:]]
        usage = f["Project/ModelManager/Model Interfaces/Model Interface Usage"][:]
        assert {n for n, u in zip(names, usage) if u} == {"MODFLOW", "MODPATH"}

        tree = gtree.parse_tokens(gtree.read_tokens(f))
        nodes = list(gtree.iter_nodes(tree))
        by = {}
        for n in nodes:
            by.setdefault(n.trtype, []).append(n)
        assert by["TIGRID3D"][0].guid == guids["grid"]
        assert by["TIMODFLOW"][0].guid == guids["modflow"]
        assert [n.name for n in by["TIPARTSET"]] == ["forward particles",
                                                     "backward particles"]
        assert {n.guid for n in by["TIPARTSET"]} == {s.guid for s in sets}
        assert by["TISOLUTION"][0].name == "Site (MODFLOW)"
        assert [n.name for n in by["TICCFFUNC"]] == ["CCF (Site.ccf)"]
        assert tree.project.guid == guids["project"]

    raw = out.read_bytes()
    assert raw.count(b"LL01096") == 0             # no example residue survives a patch


def test_patch_gpr_without_particles(tmp_path):
    out, grid, _, guids = _patch(tmp_path, with_sets=False)
    with h5py.File(out, "r") as f:
        assert not [k for k in f["Particles/MODFLOW"] if k.startswith("Set")]
        assert f["Model Files/Model Files"].shape == (0,)
        assert f["Model Files/GUID"].shape == (1,)
        names = [n.decode() for n in
                 f["Project/ModelManager/Model Interfaces/Model Interface Name"][:]]
        usage = f["Project/ModelManager/Model Interfaces/Model Interface Usage"][:]
        assert {n for n, u in zip(names, usage) if u} == {"MODFLOW"}
        tree = gtree.parse_tokens(gtree.read_tokens(f))
        types = {n.trtype for n in gtree.iter_nodes(tree)}
        assert "TIPARTSET" not in types and "TIPARTFOLDER" not in types
