"""Patch the committed .gpr template into a project-specific GMS 10.7 project file.

The template (hype_app/data/gms_template/template.gpr, built by
tools/make_gms_template.py) keeps the example project's structure — group attrs,
dtypes, chunking — as parity donors. Patching = copy the file, regenerate every GUID,
rewrite the grid geometry/CRS/per-cell datasets for the exported grid, point the
external references at <Name>_MODFLOW / <Name>_MODPATH_* folders, rebuild the
particle sets, and regenerate the /Tree/Tree explorer stream from scratch.

Fixed-width string datasets whose value length can change are ALWAYS deleted and
recreated at width len+1 (HDF5's NULLTERM conversion clips exact-fit strings — the
ras_h5.py lesson). GUIDs are uuid4 in |S37, written in place.
"""
from __future__ import annotations

import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import tree as gtree
from .grid import GmsGrid

TEMPLATE_DIR = Path(__file__).resolve().parents[1] / "data" / "gms_template"
TEMPLATE_GPR = TEMPLATE_DIR / "template.gpr"
MFH5_SKELETON = TEMPLATE_DIR / "mfh5_skeleton.h5"


@dataclass
class GprParticleSet:
    """What the .gpr stores per MODPATH particle set (starting locations only;
    pathline geometry lives in the external .pth file the .rsp run references)."""
    set_name: str               # display + file stem, e.g. "forward particles"
    direction_code: int         # /Particles .../Direction: 0 = forward, 1 = backward
    rsp_relpath: str            # backslash path relative to the .gpr
    node1_g: np.ndarray         # 1-based GMS node of each particle's seed cell
    locx: np.ndarray            # local 0-1 offsets within the cell (GMS convention)
    locy: np.ndarray
    locz: np.ndarray
    guid: str = field(default_factory=lambda: str(uuid.uuid4()))


def _new_guid() -> str:
    return str(uuid.uuid4())


def _set_string_attr(obj, key: str, value: str):
    obj.attrs[key] = np.array([value.encode("ascii")], dtype=f"S{len(value) + 1}")


def _set_numeric_attr(obj, key: str, value):
    old = obj.attrs[key]
    obj.attrs[key] = np.asarray(value, dtype=old.dtype).reshape(np.shape(old))


def _put_guid(dset, value: str, index: int = 0):
    dset[index] = value.encode("ascii")


def _replace_strings(group, name: str, rows: list[str]):
    """Delete-and-recreate a fixed-width string dataset (width=len+1, gzip).

    maxshape is unlimited so zero-row datasets (e.g. Model Files with no MODPATH
    runs) stay legal with a nonzero chunk."""
    width = max((len(r) for r in rows), default=0) + 1
    if name in group:
        del group[name]
    data = np.array([r.encode("ascii") for r in rows], dtype=f"S{width}")
    group.create_dataset(name, data=data, chunks=(max(len(rows), 1),),
                         compression="gzip", maxshape=(None,))


def _replace_array(group, name: str, data, *, chunks, maxshape):
    if name in group:
        del group[name]
    group.create_dataset(name, data=data, chunks=chunks, compression="gzip",
                         maxshape=maxshape)


def _dataset_scalar(group, *, guid: str, values, mins, maxs, times=(0.0,)):
    """Refill a DATASET SCALAR group's GUID/Values/Mins/Maxs/Times at the new size."""
    values = np.asarray(values)
    ncells = values.shape[-1]
    _put_guid(group["GUID"], guid)
    _replace_array(group, "Values", values.reshape(1, ncells),
                   chunks=(1, ncells), maxshape=(None, ncells))
    for nm, arr in (("Mins", mins), ("Maxs", maxs)):
        old_dtype = group[nm].dtype
        _replace_array(group, nm, np.asarray(arr, dtype=old_dtype),
                       chunks=(10,), maxshape=(None,))
    _replace_array(group, "Times", np.asarray(times, dtype=np.float64),
                   chunks=(10,), maxshape=(None,))


def patch_gpr(out_path: Path, *, name: str, grid: GmsGrid, wkt: str, porosity: float,
              particle_sets: list[GprParticleSet], template: Path = TEMPLATE_GPR) -> dict:
    """Write <out_path> from the template. Returns the GUID map used (for tests)."""
    import h5py

    shutil.copyfile(template, out_path)
    ncells = grid.ncells
    guids = {k: _new_guid() for k in
             ("project", "grid", "modflow", "matset", "solution", "gridframe",
              "zb_grid", "zb_model", "out_txt", "head_func", "ccf_func")}

    with h5py.File(out_path, "r+") as f:
        _put_guid(f["GUID"], guids["project"])

        # CRS: every Coordinates group carries the same Esri WKT
        def stamp_wkt(_, obj):
            if isinstance(obj, h5py.Group) and "WKT" in obj.attrs:
                _set_string_attr(obj, "WKT", wkt)
        f.visititems(stamp_wkt)
        _set_string_attr(f["Coordinates"], "WKT", wkt)

        # ---- the 3D grid -------------------------------------------------------
        g = f["3DGridModule/3DGrid 0"]
        _set_numeric_attr(g, "NumI", grid.nrow)
        _set_numeric_attr(g, "NumJ", grid.ncol)
        _set_numeric_attr(g, "NumK", grid.nlay)
        _set_numeric_attr(g, "Bearing", 0.0)
        g.attrs["Origin"] = np.asarray(grid.origin, dtype=np.float64)
        for nm, data in (("CoordsI", grid.coords_i), ("CoordsJ", grid.coords_j),
                         ("CoordsK", grid.coords_k)):
            del g[nm]
            g.create_dataset(nm, data=np.asarray(data, dtype=np.float64))

        _put_guid(g["Datasets/Guid"], guids["grid"])
        _put_guid(g["PROPERTIES/GUID"], guids["grid"])

        props = g["GridCellProps"]
        chunk = (min(1000, ncells),)
        _replace_array(props, "Material", np.ones(ncells, dtype=np.int32),
                       chunks=chunk, maxshape=(ncells,))
        for nm in ("Porosity", "Porosity Confining Beds"):
            _replace_array(props, nm, np.full(ncells, float(porosity)),
                           chunks=chunk, maxshape=(ncells,))

        _dataset_scalar(g["Zone Budget IDs"], guid=guids["zb_grid"],
                        values=np.ones(ncells, dtype=np.int32), mins=[1], maxs=[1])

        mf = g["MODFLOW Model 0"]
        _put_guid(mf["GUID"], guids["modflow"])
        _replace_strings(mf, "Name File", [f".\\{name}_MODFLOW\\{name}.mfn"])
        _dataset_scalar(mf["Zone Budget IDs"], guid=guids["zb_model"],
                        values=np.ones(ncells, dtype=np.int32), mins=[1], maxs=[1])

        mats = g["Material Sets/default"]
        _dataset_scalar(mats, guid=guids["matset"],
                        values=np.ones(ncells, dtype=np.float32),
                        mins=[1.0], maxs=[1.0])

        # ---- grid frame --------------------------------------------------------
        gf = f["Gridframe"]
        _put_guid(gf["GUID"], guids["gridframe"])
        gf["Angle"][...] = 0.0
        gf["Origin"][...] = [grid.origin[0], grid.origin[1], 0.0]
        gf["Lengths"][...] = [float(grid.delr.sum()), float(grid.delc.sum()), 10.0]

        # ---- solution + external model runs -----------------------------------
        sol = f["Solutions"]
        _put_guid(sol["GUID"], guids["solution"])
        _put_guid(sol["Geometry Guid"], guids["grid"])
        _replace_strings(sol, "Solution File", [f"{name}_MODFLOW\\{name}.mfr"])

        mfl = f["Model Files"]
        n_sets = len(particle_sets)
        _replace_strings(mfl, "GUID", [guids["modflow"]] + [""] * n_sets)
        _replace_strings(mfl, "Geometry GUID", [guids["grid"]] * (n_sets + 1))
        _replace_strings(mfl, "Model Files", [s.rsp_relpath for s in particle_sets])

        # ---- particle sets -----------------------------------------------------
        pm = f["Particles/MODFLOW"]
        _put_guid(pm["Grid3dGuid"], guids["grid"])
        for child in [k for k in pm if k.startswith("Set")]:
            del pm[child]
        for idx, ps in enumerate(particle_sets, start=1):
            sg = pm.create_group(f"Set{idx}")
            sg.attrs["Grouptype"] = np.array([b"Generic"], dtype="S8")
            sg.create_dataset("Active", data=np.array([1], dtype=np.int32))
            sg.create_dataset("Direction",
                              data=np.array([ps.direction_code], dtype=np.int32))
            sg.create_dataset("GUID", data=np.array([ps.guid.encode("ascii")],
                                                    dtype="S37"),
                              chunks=(1,), compression="gzip", maxshape=(1,))
            n = int(ps.node1_g.size)
            _replace_array(sg, "Id", np.asarray(ps.node1_g, dtype=np.int32),
                           chunks=(min(50, max(n, 1)),), maxshape=(None,))
            _replace_strings(sg, "Name", [ps.set_name])
            sg.create_dataset("Start Time", data=np.array([0.0], dtype=np.float64))
            sg.create_dataset("Visible", data=np.array([1], dtype=np.int32))
            for nm, arr in (("X Locations", ps.locx), ("Y Locations", ps.locy),
                            ("Z Locations", ps.locz)):
                _replace_array(sg, nm, np.asarray(arr, dtype=np.float64),
                               chunks=(min(50, max(n, 1)),), maxshape=(None,))

        # MODPATH interface flag follows whether any run is wired in
        names = [x.decode() for x in
                 f["Project/ModelManager/Model Interfaces/Model Interface Name"][:]]
        usage = f["Project/ModelManager/Model Interfaces/Model Interface Usage"]
        vals = usage[:]
        vals[names.index("MODPATH")] = 1 if particle_sets else 0
        usage[...] = vals

        # ---- project explorer --------------------------------------------------
        gtree.write_tokens(f, gtree.build_tokens(_build_tree(name, guids, particle_sets)))

    _repack(out_path)
    return guids


def _repack(path: Path):
    """Rewrite the file object-by-object into a fresh container. HDF5 never reclaims
    the space of deleted/replaced datasets, so a patched template otherwise carries
    the full-size donor arrays as dead weight (~2 MB per export)."""
    import h5py

    tmp = path.with_suffix(".repack.tmp")
    with h5py.File(path, "r") as src, h5py.File(tmp, "w") as dst:
        for k, v in src.attrs.items():
            dst.attrs[k] = v
        for name in src:
            src.copy(name, dst)
    path.unlink()
    tmp.rename(path)


def _build_tree(name: str, guids: dict, particle_sets: list[GprParticleSet]) -> gtree.GmsTree:
    node = gtree.node

    modflow = node("TIMODFLOW", "MODFLOW", guid=guids["modflow"], active=-1,
                   expanded=1, state=1, children=[
        node("TIARRAY", "Zone Budget IDs"),
        node("TIPACK", "Global", expanded=1, children=[
            node("TIARRAY", "Top"), node("TIARRAY", "Bottom"),
            node("TIARRAY", "Starting Heads"), node("TIARRAY", "Ibound")]),
        node("TIPACK", "LPF", expanded=1, children=[
            node("TIARRAY", "HK"), node("TIARRAY", "VK-VANI"),
            node("TIARRAY", "HANI")]),
    ])
    solution = node("TISOLUTION", f"{name} (MODFLOW)", guid=guids["solution"],
                    expanded=1, children=[
        node("TITXTFILE", f"{name}.out", guid=guids["out_txt"]),
        node("TIFUNC", "Head", guid=guids["head_func"], active=-1),
        node("TICCFFUNC", f"CCF ({name}.ccf)", guid=guids["ccf_func"], active=-1)])

    grid_children = [modflow]
    if particle_sets:
        grid_children.append(node("TIPARTFOLDER", "Particle Sets", expanded=1, state=1,
                                  children=[
            node("TIPARTSET", ps.set_name, guid=ps.guid,
                 active=(-1 if i == 0 else None), state=1)
            for i, ps in enumerate(particle_sets)]))
    grid_children.append(node("TIMATFOLDER", "Material Sets", expanded=0, children=[
        node("TI3DGRIDMATSET", "default", guid=guids["matset"], active=1)]))
    grid_children.append(solution)

    mod8 = node("TI_ROOT", "3D Grid Data", expanded=1, state=0, children=[
        node("TIGRID3D", "grid", guid=guids["grid"], active=-1, expanded=1, state=1,
             children=grid_children)])
    mod10 = node("TIMAP", "Map Data", expanded=1, state=1, children=[
        node("TIGRIDFRAME", "Grid Frame", state=1)])

    project = node("Project", "Project", guid=guids["project"], expanded=1)
    return gtree.GmsTree(project=project, modules=[(8, mod8), (10, mod10)])
