"""Build a small true-3D MF6 + MP7 fixture shaped like a hype workspace, for the
GMS-export engine tests.

Differences from build_model_fixture.py (the 1-D analytic fixture): this one is
deliberately 3-D and row-asymmetric (nlay=2, nrow=4, ncol=5, delr != delc), has an
idomain hole, drives flow ACROSS ROWS (north-south) so the FLOW FRONT FACE
translation is exercised, uses the app's external binary ``arrays/*.bin`` storage
(same set_record pattern as my_utils.build_gwf_model), and runs MP7 in PATHLINE
mode forward+backward, persisting the .mppth files the way hz_analysis now does
(summary/hz/hz_pl_fwd.mppth / hz_pl_bwd.mppth).
"""
from __future__ import annotations

import shutil
from pathlib import Path, PurePath

import flopy
import numpy as np

GWF_NAME = "gwf_model"
NLAY, NROW, NCOL = 2, 4, 5
DELR, DELC = 1.5, 2.0
XMIN, YMIN = 100.0, 200.0
H_ROW0, H_ROWN = 10.0, 6.0          # CHD heads driving cross-row flow


def build_gms_fixture(ws: Path, *, mf6_exe: str, mp7_exe: str) -> dict:
    """Build and run everything under ``ws`` (shaped like a hype work_dir).
    Returns metadata incl. the paths the exporter consumes."""
    ws = Path(ws)
    gwf_ws = ws / "model" / "gwf_workspace"
    hz_dir = ws / "summary" / "hz"
    gwf_ws.mkdir(parents=True, exist_ok=True)
    hz_dir.mkdir(parents=True, exist_ok=True)

    # int32 explicitly: NumPy 2's default int is 64-bit even on Windows, and MF6
    # rejects 8-byte binary IDOMAIN records
    idomain = np.ones((NLAY, NROW, NCOL), dtype=np.int32)
    idomain[0, 1, 2] = 0                                # a hole in the top layer
    top = np.full((NROW, NCOL), 12.0) + 0.1 * np.arange(NCOL)[None, :]
    botm = np.stack([np.full((NROW, NCOL), 8.0), np.full((NROW, NCOL), 0.0)])
    k = np.stack([np.full((NROW, NCOL), 1.0), np.full((NROW, NCOL), 2.0)])
    k33 = k / 3.0
    strt = np.full((NLAY, NROW, NCOL), H_ROW0)

    sim = flopy.mf6.MFSimulation(sim_name="hyporheic", sim_ws=str(gwf_ws),
                                 exe_name=mf6_exe)
    flopy.mf6.ModflowTdis(sim, time_units="DAYS", nper=1, perioddata=[(1.0, 1, 1.0)])
    flopy.mf6.ModflowIms(sim, complexity="SIMPLE",
                         inner_dvclose=1e-9, outer_dvclose=1e-9)
    gwf = flopy.mf6.ModflowGwf(sim, modelname=GWF_NAME, save_flows=True)
    dis = flopy.mf6.ModflowGwfdis(gwf, nlay=NLAY, nrow=NROW, ncol=NCOL,
                                  delr=DELR, delc=DELC, top=top, botm=botm,
                                  idomain=idomain, xorigin=XMIN, yorigin=YMIN)
    ic = flopy.mf6.ModflowGwfic(gwf, strt=strt)
    npf = flopy.mf6.ModflowGwfnpf(gwf, icelltype=0, k=k, k33=k33, save_flows=True)
    river = [[(0, 0, j), H_ROW0, 6.0] for j in range(NCOL)]          # aux IFACE
    sides = [[(1, NROW - 1, j), H_ROWN] for j in range(NCOL)]
    flopy.mf6.ModflowGwfchd(gwf, pname="CHD_RIVER", save_flows=True,
                            filename=f"{GWF_NAME}.river.chd", auxiliary=["IFACE"],
                            stress_period_data={0: river})
    flopy.mf6.ModflowGwfchd(gwf, pname="CHD_SIDES", save_flows=True,
                            filename=f"{GWF_NAME}.sides.chd",
                            stress_period_data={0: sides})
    flopy.mf6.ModflowGwfoc(gwf, head_filerecord=f"{GWF_NAME}.hds",
                           budget_filerecord=f"{GWF_NAME}.cbb",
                           saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")])

    # external binary arrays exactly as my_utils.build_gwf_model stores them
    (gwf_ws / "arrays").mkdir(exist_ok=True)

    def layered(array, basename):
        return [{"filename": str(PurePath("arrays") / f"{basename}_L{lay + 1}.bin"),
                 "binary": True, "data": np.asarray(array[lay]),
                 "iprn": 0, "factor": 1.0} for lay in range(array.shape[0])]

    dis.top.set_record({"filename": str(PurePath("arrays") / "top.bin"),
                        "binary": True, "data": top, "iprn": 0, "factor": 1.0})
    dis.botm.set_record(layered(botm, "botm"))
    dis.idomain.set_record(layered(idomain, "idomain"))
    ic.strt.set_record(layered(strt, "strt"))
    npf.k.set_record(layered(k, "k"))
    npf.k33.set_record(layered(k33, "k33"))

    sim.write_simulation()
    ok, buff = sim.run_simulation(silent=True)
    if not ok:
        raise RuntimeError(f"MF6 fixture run failed:\n{''.join(buff or [])}")

    # ---- MP7 pathline runs, forward + backward, hz_analysis style --------------
    hz_ws = ws / "model" / "hz_workspace"
    hz_ws.mkdir(parents=True, exist_ok=True)
    seeds = [(0, 2, 1), (0, 2, 3), (1, 1, 1), (1, 2, 4)]
    for direction, name in (("forward", "hz_pl_fwd"), ("backward", "hz_pl_bwd")):
        mp = flopy.modpath.Modpath7(modelname=name, flowmodel=gwf,
                                    exe_name=mp7_exe, model_ws=str(hz_ws))
        flopy.modpath.Modpath7Bas(mp, porosity=0.3)
        pdata = flopy.modpath.ParticleData(seeds, structured=True,
                                           localx=0.5, localy=0.5, localz=0.5)
        pg = flopy.modpath.ParticleGroup(particlegroupname="pg", particledata=pdata,
                                         filename=f"{name}.sloc")
        flopy.modpath.Modpath7Sim(mp, simulationtype="pathline",
                                  trackingdirection=direction,
                                  weaksinkoption="stop_at",
                                  weaksourceoption="stop_at",
                                  budgetoutputoption="no", referencetime=0.0,
                                  particlegroups=[pg])
        mp.write_input()
        ok, buff = mp.run_model(silent=True)
        if not ok:
            raise RuntimeError(f"MP7 {direction} fixture run failed:"
                               f"\n{''.join(buff or [])}")
        shutil.move(str(hz_ws / f"{name}.mppth"), str(hz_dir / f"{name}.mppth"))

    return {"work_dir": str(ws), "gwf_ws": str(gwf_ws), "hz_dir": str(hz_dir),
            "nlay": NLAY, "nrow": NROW, "ncol": NCOL, "delr": DELR, "delc": DELC,
            "xmin": XMIN, "ymin": YMIN, "n_seeds": len(seeds),
            "idomain": idomain.tolist(), "porosity": 0.3}
