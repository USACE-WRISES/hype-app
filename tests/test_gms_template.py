"""Integrity of the committed GMS template (hype_app/data/gms_template/template.gpr)
and of the tree token grammar it depends on.

The template is a stripped copy of the GMS 10.7.4 example project built by
tools/make_gms_template.py; the exporter (hype_app/gms/gpr.py) patches it per run and
relies on the structure pinned here. If these fail after regenerating the template,
the strip script and the patcher have drifted apart.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hype_app.gms import tree as gtree

TEMPLATE = Path(__file__).resolve().parents[1] / "hype_app" / "data" / "gms_template" / "template.gpr"

h5py = pytest.importorskip("h5py")


@pytest.fixture(scope="module")
def tpl():
    with h5py.File(TEMPLATE, "r") as f:
        yield f


# ---- tree grammar --------------------------------------------------------------

def test_tree_roundtrip_is_lossless(tpl):
    tokens = gtree.read_tokens(tpl)
    tree = gtree.parse_tokens(tokens)
    assert gtree.build_tokens(tree) == tokens


def test_tree_parser_rejects_imbalance():
    with pytest.raises(gtree.TreeFormatError):
        gtree.parse_tokens(["BEGTREE", "BEGTRNODE", "TRTYPE Project", "ENDTREE"])
    with pytest.raises(gtree.TreeFormatError):
        gtree.parse_tokens(["BEGTREE", "BEGTRNODE", "BEGTRMOD 8", "BEGTRNODE",
                            "TRTYPE TI_ROOT", "ENDTRNODE", "ENDTRMOD 9",
                            "ENDTRNODE", "ENDTREE"])


def test_tree_builder_canonical_node():
    n = gtree.node("TIPARTSET", "forward particles",
                   guid="00000000-0000-0000-0000-000000000000", active=-1, state=1)
    assert [k for k, _ in n.props] == ["TRTYPE", "TRNAME", "TRGUID", "TRACTIVE", "TRSTATE"]
    assert n.name == "forward particles"
    folder = gtree.node("TIPARTFOLDER", "Particle Sets", expanded=1, state=1, children=[n])
    tree = gtree.GmsTree(project=gtree.node("Project", "Project", guid="p", expanded=1),
                         modules=[(8, folder)])
    reparsed = gtree.parse_tokens(gtree.build_tokens(tree))
    assert reparsed.module(8).children[0].name == "forward particles"


# ---- identity + stripped/kept groups -------------------------------------------

def test_root_identity(tpl):
    assert tpl["File Type"][0] == b"Xmdf"
    assert abs(float(tpl["File Version"][0]) - 99.99) < 0.01
    assert len(tpl["GUID"][0]) == 36
    ver = tpl.attrs["GMS Version"]
    ver = ver[0] if getattr(ver, "shape", None) else ver
    assert b"10.7" in (ver if isinstance(ver, bytes) else str(ver).encode())


STRIPPED = ("2DGridModule", "2DScatterModule", "TINModule", "Generic Shapefiles",
            "Images", "Notes", "Text Files", "Toolbox")
KEPT = ("Coordinates", "Color Palettes", "Gridframe", "Layout", "Project",
        "Solutions", "Model Files", "Particles", "3DGridModule", "Map Data", "Ini")


def test_stripped_and_kept_groups(tpl):
    for name in STRIPPED:
        assert name not in tpl, f"{name} should be stripped"
    for name in KEPT:
        assert name in tpl, f"{name} missing"
    assert len(tpl["Map Data"]) == 0                      # kept as an empty shell
    assert tpl["Ini/Names"].shape == (0,)                 # display state emptied
    assert tpl["Ini/Values"].shape == (0,)


def test_grid_group_shape(tpl):
    g = tpl["3DGridModule/3DGrid 0"]
    for key in ("Coordinates", "CoordsI", "CoordsJ", "CoordsK", "Datasets",
                "GridCellProps", "MODFLOW Model 0", "Material Sets", "PROPERTIES",
                "Zone Budget IDs"):
        assert key in g, key
    assert "elevation" not in g["Datasets"]
    nrow, ncol, nlay = (int(g.attrs[k][0] if getattr(g.attrs[k], "shape", None)
                            else g.attrs[k]) for k in ("NumI", "NumJ", "NumK"))
    assert g["CoordsI"].shape == (nrow + 1,)
    assert g["CoordsJ"].shape == (ncol + 1,)
    assert g["CoordsK"].shape == (nlay + 1,)
    ncells = nrow * ncol * nlay
    assert g["Zone Budget IDs/Values"].shape == (1, ncells)
    assert g["MODFLOW Model 0/Zone Budget IDs/Values"].shape == (1, ncells)
    assert g["Material Sets/default/Values"].shape == (1, ncells)
    assert g["GridCellProps/Porosity"].shape == (ncells,)
    wkt = g["Coordinates"].attrs["WKT"]
    assert b"PROJCS" in (wkt if isinstance(wkt, bytes) else wkt[0])


def test_solutions_single_modflow_row(tpl):
    s = tpl["Solutions"]
    assert s["Solution Type"].shape == (1,)
    assert s["Solution Type"][0] == b"MODFSOL"
    grid_guid = tpl["3DGridModule/3DGrid 0/PROPERTIES/GUID"][0]
    assert s["Geometry Guid"][0] == grid_guid


def test_model_files_row_convention(tpl):
    m = tpl["Model Files"]
    n_files = m["Model Files"].shape[0]
    assert m["GUID"].shape[0] == n_files + 1          # MODFLOW row first, no file entry
    assert m["Geometry GUID"].shape[0] == n_files + 1
    assert m["GUID"][0] == tpl["3DGridModule/3DGrid 0/MODFLOW Model 0/GUID"][0]
    for p in m["Model Files"][:]:
        assert p.decode().lower().endswith(".rsp")


def test_model_interfaces_mt3d_off(tpl):
    names = [n.decode() for n in
             tpl["Project/ModelManager/Model Interfaces/Model Interface Name"][:]]
    usage = list(tpl["Project/ModelManager/Model Interfaces/Model Interface Usage"][:])
    on = {n for n, u in zip(names, usage) if u}
    assert on == {"MODFLOW", "MODPATH"}


# ---- tree content vs data groups ----------------------------------------------

def test_tree_modules_and_guid_links(tpl):
    tree = gtree.parse_tokens(gtree.read_tokens(tpl))
    assert [mid for mid, _ in tree.modules] == [8, 10]

    nodes = list(gtree.iter_nodes(tree))
    types = {n.trtype for n in nodes}
    for banned in ("TIMT3D", "TITIN", "TIGRID2D", "TISCAT2D", "TIIMAGE", "TIGIS",
                   "TICONMOD", "TICOVER"):
        assert banned not in types

    by_type = {}
    for n in nodes:
        by_type.setdefault(n.trtype, []).append(n)

    assert [n.name for n in by_type["TISOLUTION"]] == ["LL01096 (MODFLOW)"]
    assert "elevation" not in [n.name for n in by_type.get("TIFUNC", [])]
    assert not [n for n in by_type.get("TITXTFILE", [])
                if n.name.endswith(".CellSummary.csv")]

    g = tpl["3DGridModule/3DGrid 0"]
    assert by_type["TIGRID3D"][0].guid == g["PROPERTIES/GUID"][0].decode()
    assert by_type["TIMODFLOW"][0].guid == g["MODFLOW Model 0/GUID"][0].decode()
    assert by_type["TIPARTSET"][0].guid == tpl["Particles/MODFLOW/Set1/GUID"][0].decode()
    assert by_type["TISOLUTION"][0].guid == tpl["Solutions/GUID"][0].decode()
    assert by_type["TIMAP"][0].children[0].trtype == "TIGRIDFRAME"

    assert tree.project.guid == tpl["GUID"][0].decode()
