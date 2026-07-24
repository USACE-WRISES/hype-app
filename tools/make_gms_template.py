"""Build hype_app/data/gms_template/template.gpr from the untracked GMS example project.

The committed template is a stripped copy of notes/Example_GMS_10_7_Project/LL01096.gpr
(GMS 10.7.4): the modules hype does not export (TIN, 2D grid/scatter, GIS shapefiles,
images, MT3D, notes, toolbox history) are removed and /Ini display state is emptied,
while everything the exporter patches per run is kept with its original dtypes,
chunking and attrs (hype_app/gms/gpr.py relies on that parity). Copying into a FRESH
file rather than deleting in place is what actually reclaims the stripped space.

Run manually whenever the template needs regenerating (the example stays untracked):

    .venv\\Scripts\\python.exe tools\\make_gms_template.py

To sanity-check the result in GMS itself, drop the template into a copy of the example
folder (so its .\\LL01096_MODFLOW\\... references resolve) and open it there.
"""
from __future__ import annotations

import sys
from pathlib import Path

import h5py
import numpy as np

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hype_app.gms import tree as gtree  # noqa: E402

EXAMPLE = REPO / "notes" / "Example_GMS_10_7_Project" / "LL01096.gpr"
EXAMPLE_MFH5 = EXAMPLE.parent / "LL01096_MODFLOW" / "LL01096.h5"
OUT = REPO / "hype_app" / "data" / "gms_template" / "template.gpr"
OUT_MFH5 = REPO / "hype_app" / "data" / "gms_template" / "mfh5_skeleton.h5"

# Top-level objects copied verbatim (minus the skip list below). Everything else
# (2D/TIN/GIS/Images/Notes/Text Files/Toolbox/Map Data content) is dropped.
KEEP_TOP = (
    "File Type", "File Version", "GUID",
    "Coordinates", "Color Palettes", "Gridframe", "Layout", "Project",
    "Solutions", "Model Files", "Particles", "3DGridModule",
)
# Paths (relative, '/'-joined) pruned out of the copied subtrees: the user-made
# interpolation dataset on the 3D grid is out of scope for exports.
SKIP_PATHS = frozenset({
    "3DGridModule/3DGrid 0/Datasets/elevation",
})


def copy_filtered(src: h5py.File, dst: h5py.File):
    def rec(s_obj, d_parent: h5py.Group, rel: str):
        for name, child in s_obj.items():
            child_rel = f"{rel}/{name}" if rel else name
            if child_rel in SKIP_PATHS:
                continue
            if isinstance(child, h5py.Group):
                g = d_parent.create_group(name)
                for k, v in child.attrs.items():
                    g.attrs[k] = v
                rec(child, g, child_rel)
            else:
                # H5Ocopy: preserves dtype, chunking, compression, maxshape, attrs
                s_obj.copy(name, d_parent)

    for k, v in src.attrs.items():
        dst.attrs[k] = v
    for name in KEEP_TOP:
        obj = src[name]
        if isinstance(obj, h5py.Group):
            g = dst.create_group(name)
            for k, v in obj.attrs.items():
                g.attrs[k] = v
            rec(obj, g, name)
        else:
            src.copy(name, dst)


def replace_string_rows(group: h5py.Group, name: str, rows: list[str], min_width: int):
    """Delete-and-recreate a fixed-width string dataset (gzip, single chunk).

    Width = max(len)+1: HDF5's NULLTERM conversion clips exact-fit strings.
    """
    width = max(min_width, max((len(r) for r in rows), default=0) + 1)
    if name in group:
        del group[name]
    data = np.array([r.encode("ascii") for r in rows], dtype=f"S{width}")
    group.create_dataset(name, data=data, chunks=(max(len(rows), 1),),
                         compression="gzip", maxshape=(len(rows),))


def strip_solutions(f: h5py.File) -> str:
    """Keep only the MODFLOW solution row; return its GUID."""
    g = f["Solutions"]
    types = [t.decode() for t in g["Solution Type"][:]]
    keep = types.index("MODFSOL")
    guid = g["GUID"][keep].decode()
    geom = g["Geometry Guid"][keep].decode()
    sol_file = g["Solution File"][keep].decode()
    replace_string_rows(g, "GUID", [guid], 37)
    replace_string_rows(g, "Geometry Guid", [geom], 37)
    replace_string_rows(g, "Solution File", [sol_file], 1)
    replace_string_rows(g, "Solution Type", ["MODFSOL"], 1)
    return guid


def strip_model_files(f: h5py.File):
    """Drop the MT3D run row. Observed row semantics: GUID/Geometry GUID carry one row
    per in-use model interface (MODFLOW first, with no entry in 'Model Files' because
    its files hang off the grid's Name File; then one row+file per external run)."""
    g = f["Model Files"]
    guids = [x.decode() for x in g["GUID"][:]]
    geoms = [x.decode() for x in g["Geometry GUID"][:]]
    files = [x.decode() for x in g["Model Files"][:]]
    keep_files = [p for p in files if p.lower().endswith(".rsp")]
    n = 1 + len(keep_files)                    # MODFLOW row + one per MODPATH run
    replace_string_rows(g, "GUID", (guids + [""] * n)[:n], 37)
    replace_string_rows(g, "Geometry GUID", (geoms + [geoms[0]] * n)[:n], 37)
    replace_string_rows(g, "Model Files", keep_files, 1)


def strip_ini(f: h5py.File):
    src_attrs = dict(f["Ini"].attrs) if "Ini" in f else {}
    if "Ini" in f:
        del f["Ini"]
    g = f.create_group("Ini")
    for k, v in src_attrs.items():
        g.attrs[k] = v
    for name, width in (("Names", 60), ("Values", 335)):
        g.create_dataset(name, shape=(0,), dtype=f"S{width}", chunks=(1,),
                         compression="gzip", maxshape=(None,))


def set_mt3d_unused(f: h5py.File):
    names = [n.decode() for n in
             f["Project/ModelManager/Model Interfaces/Model Interface Name"][:]]
    usage = f["Project/ModelManager/Model Interfaces/Model Interface Usage"]
    vals = usage[:]
    vals[names.index("MT3DMS")] = 0
    usage[...] = vals


def strip_tree(src: h5py.File, dst: h5py.File):
    """Rebuild /Tree/Tree keeping module 8 (minus MT3D/elevation/CellSummary nodes)
    and module 10 reduced to its Grid Frame child."""
    tree = gtree.parse_tokens(gtree.read_tokens(src))

    def prune8(n: gtree.TreeNode) -> bool:
        if n.trtype == "TIFUNC" and n.name == "elevation":
            return False
        if n.trtype == "TIMT3D":
            return False
        if n.trtype == "TISOLUTION" and n.name.endswith("(MT3DMS)"):
            return False
        if n.trtype == "TITXTFILE" and n.name.endswith(".CellSummary.csv"):
            return False
        n.children = [c for c in n.children if prune8(c)]
        return True

    mod8 = tree.module(8)
    mod10 = tree.module(10)
    if mod8 is None or mod10 is None:
        raise SystemExit("example tree is missing module 8 or 10")
    prune8(mod8)

    frames = [c for c in _walk(mod10) if c.trtype == "TIGRIDFRAME"]
    map_root = gtree.node("TIMAP", "Map Data", trid=-1, expanded=1, state=1,
                          children=frames[:1])

    stripped = gtree.GmsTree(project=tree.project, modules=[(8, mod8), (10, map_root)])
    gtree.write_tokens(dst, gtree.build_tokens(stripped))
    return stripped


def _walk(n: gtree.TreeNode):
    yield n
    for c in n.children:
        yield from _walk(c)


def validate(f: h5py.File, stripped: gtree.GmsTree):
    tree = gtree.parse_tokens(gtree.read_tokens(f))     # re-parse = balance check
    tree_guids = gtree.guids_in_tree(tree)
    # every tree GUID must exist somewhere in the file's GUID datasets
    file_guids = set()

    def collect(name, obj):
        if isinstance(obj, h5py.Dataset) and obj.dtype.kind == "S" \
                and name.split("/")[-1].lower() in ("guid", "guids"):
            file_guids.update(x.decode() for x in np.atleast_1d(obj[:]).ravel())
    f.visititems(collect)
    file_guids.add(f["GUID"][0].decode())
    dangling = tree_guids - file_guids
    # Head/CCF/solution-child GUIDs live only in the external .mfr world, never as
    # datasets in the .gpr; the solution + out/head/ccf child nodes are the known set.
    sol = [n for n in gtree.iter_nodes(tree) if n.trtype == "TISOLUTION"]
    expected_external = {c.guid for s in sol for c in _walk(s) if c.guid} - file_guids
    unexplained = dangling - expected_external
    if unexplained:
        raise SystemExit(f"dangling tree GUIDs: {sorted(unexplained)}")
    return len(tree_guids), len(file_guids)


def build_mfh5_skeleton(src_path: Path, out_path: Path):
    """An emptied copy of the example's GMS-HDF5 model file: root identity + every
    per-BC-type template group, with Arrays cleared and Specified Head reset to the
    same empty pattern the unused BC groups carry. write_mfh5 copies this and fills
    only Arrays + Specified Head, so all the varied empty-group schemas ride along
    without writer code for them."""
    if out_path.exists():
        out_path.unlink()
    with h5py.File(src_path, "r") as src, h5py.File(out_path, "w") as dst:
        def rec(s_obj, d_parent, rel):
            for name, child in s_obj.items():
                child_rel = f"{rel}/{name}" if rel else name
                top = child_rel.split("/")[0]
                if isinstance(child, h5py.Group):
                    g = d_parent.create_group(name)
                    for k, v in child.attrs.items():
                        g.attrs[k] = v
                    rec(child, g, child_rel)
                elif top not in ("Arrays", "Specified Head"):
                    s_obj.copy(name, d_parent)

        for k, v in src.attrs.items():
            dst.attrs[k] = v
        rec(src, dst, "")
        sh = dst["Specified Head"]
        for dname, dtype, shape in (("00. Number of BCs", np.int32, (1,)),
                                    ("01. Use Last", np.int32, (1,)),
                                    ("02. Cell IDs", np.int32, (1,)),
                                    ("03. Name", np.int8, (1,)),
                                    ("04. Map ID", np.int8, (1,)),
                                    ("06. IFACE", np.int32, (1,))):
            sh.create_dataset(dname, data=np.zeros(shape, dtype=dtype), chunks=(50,),
                              compression="gzip", maxshape=(None,))
        sh.create_dataset("07. Property", data=np.zeros((6, 1, 1), dtype=np.float64),
                          chunks=(6, 500, 50), compression="gzip",
                          maxshape=(None, None, None))


def main():
    if not EXAMPLE.exists():
        raise SystemExit(f"example project not found: {EXAMPLE}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    if OUT.exists():
        OUT.unlink()
    with h5py.File(EXAMPLE, "r") as src, h5py.File(OUT, "w") as dst:
        copy_filtered(src, dst)
        dst.create_group("Map Data").attrs["Grouptype"] = src["Map Data"].attrs["Grouptype"]
        strip_ini(dst)
        sol_guid = strip_solutions(dst)
        strip_model_files(dst)
        set_mt3d_unused(dst)
        stripped = strip_tree(src, dst)
        n_tree, n_file = validate(dst, stripped)

        n_groups = [0]
        n_dsets = [0]

        def tally(_, obj):
            (n_groups if isinstance(obj, h5py.Group) else n_dsets)[0] += 1
        dst.visititems(tally)

    print(f"template: {OUT}")
    print(f"  size: {OUT.stat().st_size:,} bytes")
    print(f"  groups: {n_groups[0]}  datasets: {n_dsets[0]}")
    print(f"  tree tokens: {len(gtree.build_tokens(stripped))}  "
          f"tree GUIDs: {n_tree}  file GUIDs: {n_file}")
    print(f"  MODFLOW solution GUID kept: {sol_guid}")

    build_mfh5_skeleton(EXAMPLE_MFH5, OUT_MFH5)
    print(f"mfh5 skeleton: {OUT_MFH5}")
    print(f"  size: {OUT_MFH5.stat().st_size:,} bytes")


if __name__ == "__main__":
    main()
