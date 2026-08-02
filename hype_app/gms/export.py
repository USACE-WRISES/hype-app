"""Orchestrator: a completed hype workspace -> a self-contained GMS 10.7 project.

Output layout (all references between the files are relative, so the tree can be
moved or zipped as one unit and <Name>.gpr opened in place):

    <out_dir>/<Name>.gpr
    <out_dir>/<Name>_MODFLOW/                 translated model + results
    <out_dir>/<Name>_MODPATH_forward particles/    (when HZ pathlines exist)
    <out_dir>/<Name>_MODPATH_backward particles/

Data flow: load engine-order arrays (loaders.py) -> flip to GMS order (grid.py) ->
write the MODFLOW folder (modflow_files.py) -> translate the persisted MP7 display
pathlines (modpath_files.py) -> patch the .gpr template last (gpr.py), so a partial
failure never leaves a .gpr pointing at files that were not written.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

from .loaders import GmsExportError, load_hype_model
from . import grid as ggrid
from . import gpr as ggpr
from . import modflow_files as mff
from . import modpath_files as mpf

__all__ = ["export_gms_project", "GmsExportError"]

# The sampled display pathlines hz_analysis persists into summary/hz (Phase B of the
# GMS-export feature); absent files simply mean "no particle sets in the export".
MPPTH_FILES = {"forward": "hz_pl_fwd.mppth", "backward": "hz_pl_bwd.mppth"}
# Pre-2026-07-26 runs persisted the raw MP7 simulation names (hz_pl_{direction[:3]});
# accept them so existing projects export particle sets without a re-delineation.
MPPTH_LEGACY = {"forward": "hz_pl_for.mppth", "backward": "hz_pl_bac.mppth"}


def export_gms_project(work_dir: str | Path, out_dir: str | Path, *, name: str,
                       crs_wkt_esri: str, porosity: float,
                       hz_dir: str | Path | None = None,
                       log: Callable = print) -> dict:
    """Write the GMS project for the run in ``work_dir`` into ``out_dir``.

    Returns {"gpr", "modflow_dir", "modpath_dirs", "n_particles", "warnings"}.
    Raises GmsExportError when the groundwater run itself is unusable.
    """
    work_dir = Path(work_dir)
    out_dir = Path(out_dir)
    warnings: list[str] = []

    log(f"GMS export: loading MODFLOW 6 results for '{name}'")
    model = load_hype_model(work_dir / "model" / "gwf_workspace")
    nlay, nrow, ncol = model.nlay, model.nrow, model.ncol
    grid = ggrid.gms_grid_from(nlay, nrow, ncol, model.delr, model.delc,
                               model.xmin, model.ymin, model.botm)

    # ---- engine order -> GMS order ------------------------------------------
    ibound_g = ggrid.flip_cc(model.idomain).astype(np.int32)
    top_g = ggrid.flip_cc(model.top)
    botm_g = ggrid.flip_cc(model.botm)
    strt_g = ggrid.flip_cc(model.strt)
    hk_g = ggrid.flip_cc(model.k)
    with np.errstate(divide="ignore", invalid="ignore"):
        vani = np.where(model.k33 > 0, model.k / model.k33, 1.0)
    vani_g = ggrid.flip_cc(vani)
    head_g = ggrid.flip_cc(model.head)
    frf_g = ggrid.flip_frf(model.frf)
    fff_g = ggrid.flip_fff(model.fff)
    flf_g = ggrid.flip_flf(model.flf)
    chd_node1_g = ggrid.eng_node0_to_gms_node1(model.chd_node0, nlay, nrow, ncol)

    active = model.idomain != 0
    vani_rep = float(np.median(vani[active])) if active.any() else 1.0

    # ---- <Name>_MODFLOW ------------------------------------------------------
    mfdir = out_dir / f"{name}_MODFLOW"
    mfdir.mkdir(parents=True, exist_ok=True)
    log("GMS export: writing the MODFLOW folder")
    mff.write_prj(mfdir, name, crs_wkt_esri)
    mff.write_mfs(mfdir, name, orig=grid.origin, rotz=0.0,
                  porosity=porosity, vani=vani_rep)
    mff.write_mfw(mfdir, name, grid.origin, 0.0)
    mff.write_mfn(mfdir, name)
    mff.write_mfr(mfdir, name)
    mff.write_out_stub(mfdir, name)
    mff.write_dis(mfdir, name, nlay=nlay, nrow=nrow, ncol=ncol,
                  delr=grid.delr, delc=grid.delc, perlen=model.totim)
    mff.write_ba6(mfdir, name, nlay=nlay, nrow=nrow, ncol=ncol)
    mff.write_lpf(mfdir, name, nlay=nlay, nrow=nrow, ncol=ncol)
    mff.write_chd(mfdir, name, nbc=int(chd_node1_g.size))
    mff.write_oc(mfdir, name)
    mff.write_pcg(mfdir, name)
    mff.write_mfh5(mfdir, name, ggpr.MFH5_SKELETON,
                   ibound_g=ibound_g, strt_g=strt_g, top_g=top_g, botm_g=botm_g,
                   hk_g=hk_g, vani_g=vani_g, chd_node1_g=chd_node1_g,
                   chd_head=model.chd_head, chd_iface=model.chd_iface)
    mff.write_hed(mfdir, name, head_g=head_g, ibound_g=ibound_g,
                  pertim=model.pertim, totim=model.totim)
    mff.write_hed_h5(mfdir, name, head_g=head_g, ibound_g=ibound_g,
                     totim=model.totim)
    mff.write_ccf(mfdir, name, chd_node1_g=chd_node1_g, chd_q=model.chd_q,
                  frf_g=frf_g, fff_g=fff_g, flf_g=flf_g,
                  delt=model.totim, pertim=model.pertim, totim=model.totim)

    # ---- MODPATH sets from the persisted display pathlines -------------------
    sets: list[mpf.ModpathSet] = []
    modpath_dirs: list[str] = []
    if hz_dir is not None:
        hz_dir = Path(hz_dir)
        for direction, fname in MPPTH_FILES.items():
            src = hz_dir / fname
            if not src.is_file():
                src = hz_dir / MPPTH_LEGACY[direction]
                fname = src.name
            if not src.is_file():
                continue
            try:
                ms = mpf.read_mp7_pathlines(src, nlay=nlay, nrow=nrow, ncol=ncol,
                                            total_y=grid.total_y, direction=direction)
            except Exception as e:  # noqa: BLE001 — pathlines must never sink the export
                warnings.append(f"could not translate {fname}: {e}")
                continue
            if ms.n_particles:
                sets.append(ms)
        if not sets:
            warnings.append("no flow-path files in the hyporheic results; the GMS "
                            "project has no particle sets")
    else:
        warnings.append("hyporheic delineation has not run; the GMS project has "
                        "no particle sets")

    for ms in sets:
        set_dir = out_dir / f"{name}_MODPATH_{ms.set_name}"
        log(f"GMS export: writing particle set '{ms.set_name}' "
            f"({ms.n_particles} particles)")
        mpf.write_set(set_dir, ms, model_name=name, ibound_g=ibound_g,
                      porosity=porosity)
        modpath_dirs.append(str(set_dir))

    # ---- the .gpr, last ------------------------------------------------------
    log("GMS export: writing the .gpr project file")
    gpr_sets = [ggpr.GprParticleSet(
        set_name=ms.set_name, direction_code=ms.direction_code,
        rsp_relpath=f"{name}_MODPATH_{ms.set_name}\\{ms.set_name}.rsp",
        node1_g=ms.seed_node1, locx=ms.seed_locx, locy=ms.seed_locy,
        locz=ms.seed_locz) for ms in sets]
    gpr_path = out_dir / f"{name}.gpr"
    ggpr.patch_gpr(gpr_path, name=name, grid=grid, wkt=crs_wkt_esri,
                   porosity=porosity, particle_sets=gpr_sets)

    n_particles = {ms.set_name.split()[0]: ms.n_particles for ms in sets}
    log(f"GMS export: done ({gpr_path.name})")
    return {"gpr": str(gpr_path), "modflow_dir": str(mfdir),
            "modpath_dirs": modpath_dirs, "n_particles": n_particles,
            "warnings": warnings}
