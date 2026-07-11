"""Build a tiny, deterministic MODFLOW6 + MODPATH7 fixture with an analytic solution.

A 1-D confined flow field between two constant-head cells has an *exact* linear head
profile and an exact Darcy flux, so every assertion downstream is hand-checkable rather
than a regression snapshot. The two constant-head cells are split into separately-named
``CHD_RIVER`` and ``CHD_SIDES`` packages — mirroring the app's Option-B package split — so
this same fixture exercises Phase 5's per-package budget reader and mass-balance check.

Geometry (nlay=1, nrow=1, ncol=N, delr=delc=1 m, confined thickness=1 m, K=1 m/day):

    col 0 (CHD_RIVER, head H0) ── linear head ──> col N-1 (CHD_SIDES, head H1)

Analytic results (H0 > H1):
    head[i]        = H0 - (H0 - H1) * i / (N - 1)          # exact at cell centers
    q (m3/day)     = K * (H0 - H1) / ((N - 1) * delr)      # into domain at col 0
    CHD_RIVER flow = +q   ;   CHD_SIDES flow = -q   ;   sum = 0
    a forward particle seeded near col 0 exits at the downgradient boundary (col N-1).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import flopy
import numpy as np

GWF_NAME = "gwf_model"
MP7_NAME = "mp7_model"


def _exe(env_dir: str | None, name: str) -> str:
    """Resolve mf6/mp7 executable from HYPE_MODFLOW_BIN (or PATH as a bare name)."""
    if env_dir:
        for cand in (Path(env_dir) / name, Path(env_dir) / f"{name}.exe"):
            if cand.is_file():
                return str(cand)
    return name  # rely on PATH


def build_fixture(ws: Path, *, mf6_exe: str, mp7_exe: str,
                  ncol: int = 21, H0: float = 10.0, H1: float = 0.0,
                  K: float = 1.0, porosity: float = 0.3) -> dict:
    """Build, write and run the MF6 + MP7 fixture in ``ws``. Returns the analytic metadata."""
    ws = Path(ws)
    ws.mkdir(parents=True, exist_ok=True)

    # --- MODFLOW 6 --------------------------------------------------------------
    sim = flopy.mf6.MFSimulation(sim_name="fx", sim_ws=str(ws), exe_name=mf6_exe)
    flopy.mf6.ModflowTdis(sim, nper=1, perioddata=[(1.0, 1, 1.0)])
    flopy.mf6.ModflowIms(sim, complexity="SIMPLE",
                         inner_dvclose=1e-9, outer_dvclose=1e-9)
    gwf = flopy.mf6.ModflowGwf(sim, modelname=GWF_NAME, save_flows=True)
    flopy.mf6.ModflowGwfdis(gwf, nlay=1, nrow=1, ncol=ncol,
                            delr=1.0, delc=1.0, top=1.0, botm=0.0)
    flopy.mf6.ModflowGwfic(gwf, strt=H0)
    flopy.mf6.ModflowGwfnpf(gwf, save_flows=True, icelltype=0, k=K)  # icelltype=0 -> confined
    # Two separately-named CHD packages (the app's CHD_RIVER / CHD_SIDES split).
    flopy.mf6.ModflowGwfchd(gwf, pname="CHD_RIVER", save_flows=True,
                            filename=f"{GWF_NAME}.river.chd",
                            stress_period_data=[[(0, 0, 0), H0]])
    flopy.mf6.ModflowGwfchd(gwf, pname="CHD_SIDES", save_flows=True,
                            filename=f"{GWF_NAME}.sides.chd",
                            stress_period_data=[[(0, 0, ncol - 1), H1]])
    flopy.mf6.ModflowGwfoc(
        gwf, head_filerecord=f"{GWF_NAME}.hds", budget_filerecord=f"{GWF_NAME}.cbb",
        saverecord=[("HEAD", "ALL"), ("BUDGET", "ALL")])
    sim.write_simulation()
    ok, buff = sim.run_simulation(silent=True)
    if not ok:
        raise RuntimeError(f"MODFLOW6 fixture run failed:\n{''.join(buff) if buff else ''}")

    # --- MODPATH 7 (forward endpoint; endpoint mode has no ~100-day time-point cap) ----
    mp = flopy.modpath.Modpath7(modelname=MP7_NAME, flowmodel=gwf,
                                exe_name=mp7_exe, model_ws=str(ws))
    flopy.modpath.Modpath7Bas(mp, porosity=porosity)
    pdata = flopy.modpath.ParticleData([(0, 0, 1)], structured=True,
                                       localx=0.5, localy=0.5, localz=0.5)
    pg = flopy.modpath.ParticleGroup(particlegroupname="pg", particledata=pdata,
                                     filename=f"{MP7_NAME}.sloc")
    flopy.modpath.Modpath7Sim(mp, simulationtype="endpoint",
                              trackingdirection="forward", weaksinkoption="pass_through",
                              weaksourceoption="pass_through", particlegroups=[pg])
    mp.write_input()
    ok, buff = mp.run_model(silent=True)
    if not ok:
        raise RuntimeError(f"MODPATH7 fixture run failed:\n{''.join(buff) if buff else ''}")

    meta = {
        "ncol": ncol, "H0": H0, "H1": H1, "K": K, "porosity": porosity,
        "gwf_name": GWF_NAME, "mp7_name": MP7_NAME,
        "expected_head": [H0 - (H0 - H1) * i / (ncol - 1) for i in range(ncol)],
        "expected_q_m3_per_day": K * (H0 - H1) / ((ncol - 1) * 1.0),
    }
    (ws / "fixture_meta.json").write_text(json.dumps(meta, indent=2))
    return meta


if __name__ == "__main__":  # build into tests/fixtures/model/ for inspection
    here = Path(__file__).resolve().parent
    env = os.getenv("HYPE_MODFLOW_BIN")
    m = build_fixture(here / "fixtures" / "model" / "build",
                      mf6_exe=_exe(env, "mf6"), mp7_exe=_exe(env, "mp7"))
    print(json.dumps(m, indent=2))
