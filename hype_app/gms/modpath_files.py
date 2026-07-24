"""Writers for GMS MODPATH particle-set folders, translated from MODPATH 7 output.

GMS 10.7's tracker is its own MODPATH (V4 prompt dialect, "MODPATH 5.0" output tag)
and it DISPLAYS pathlines straight from the binary .pth, so translating hype's MP7
display-sample pathlines gives working particle sets without ever running GMS's
MODPATH. Each set folder gets the full input deck too (.nam/.rsp/.loc/.mdf) so a
re-run inside GMS is possible against the translated .hed/.ccf.

Coordinate translation (see grid.py for the row-flip rationale):
    X_gms  = x_mp7                      both measured from the west gridline
    Y_gms  = total_y - y_mp7            MP7 y=0 at the north edge; GMS-local y=0 south
    yloc_gms = 1 - yloc_mp7             local-in-cell y flips with the row axis
    Zloc/Zelev/time pass through; node ids re-mapped to 1-based north-first.

Binary .pth layout (decoded from the example): 80-byte ASCII header starting
"MODPATH 5.0", then 32-byte records (i4 flag, i4 pid, f4 X, f4 Y, f4 Zloc,
f4 Zelev, f4 time, i4 node) with flag=0 on the file's first record only, then a
4-byte 01 00 00 00 trailer.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .grid import eng_node0_to_gms_node1


@dataclass
class ModpathSet:
    """One translated particle set: per-record arrays (grouped per particle, time
    ascending) plus the per-particle seed rows both the .gpr and .loc need."""
    set_name: str               # "forward particles" / "backward particles"
    direction_code: int         # .gpr Particles/Direction: 0 forward, 1 backward
    rsp_direction: int          # MODPATH prompt answer: 1 forward, 2 backward
    pid: np.ndarray             # (nrec,) 1-based, contiguous per particle
    x: np.ndarray               # (nrec,) GMS-local coords
    y: np.ndarray
    zloc: np.ndarray
    zelev: np.ndarray
    time: np.ndarray
    node1: np.ndarray           # (nrec,) 1-based GMS nodes
    seed_node1: np.ndarray      # (npart,) per-particle seed cell
    seed_locx: np.ndarray       # (npart,) local 0-1 offsets, GMS convention
    seed_locy: np.ndarray
    seed_locz: np.ndarray

    @property
    def n_particles(self) -> int:
        return int(self.seed_node1.size)


def read_mp7_pathlines(mppth: Path, *, nlay: int, nrow: int, ncol: int,
                       total_y: float, direction: str) -> ModpathSet:
    """Translate one MP7 pathline file (flopy zero-bases node/k/ids on load)."""
    from flopy.utils import PathlineFile

    forward = direction == "forward"
    particles = [p for p in PathlineFile(str(mppth)).get_alldata() if len(p)]

    pid_l, x_l, y_l, zloc_l, zelev_l, t_l, node_l = [], [], [], [], [], [], []
    seeds = {"node1": [], "locx": [], "locy": [], "locz": []}
    for new_pid, rec in enumerate(particles, start=1):
        rec = np.sort(rec, order="time")
        node1 = eng_node0_to_gms_node1(rec["node"], nlay, nrow, ncol)
        pid_l.append(np.full(len(rec), new_pid, dtype=np.int32))
        x_l.append(np.asarray(rec["x"], dtype=np.float64))
        y_l.append(total_y - np.asarray(rec["y"], dtype=np.float64))
        zloc_l.append(np.clip(np.asarray(rec["zloc"], dtype=np.float64), 0.0, 1.0))
        zelev_l.append(np.asarray(rec["z"], dtype=np.float64))
        t_l.append(np.asarray(rec["time"], dtype=np.float64))
        node_l.append(node1)
        seeds["node1"].append(int(node1[0]))
        seeds["locx"].append(float(np.clip(rec["xloc"][0], 0.0, 1.0)))
        seeds["locy"].append(float(np.clip(1.0 - rec["yloc"][0], 0.0, 1.0)))
        seeds["locz"].append(float(np.clip(rec["zloc"][0], 0.0, 1.0)))

    def cat(parts, dtype):
        return (np.concatenate(parts).astype(dtype) if parts
                else np.empty(0, dtype=dtype))

    return ModpathSet(
        set_name=("forward particles" if forward else "backward particles"),
        direction_code=0 if forward else 1,
        rsp_direction=1 if forward else 2,
        pid=cat(pid_l, np.int32), x=cat(x_l, np.float64), y=cat(y_l, np.float64),
        zloc=cat(zloc_l, np.float64), zelev=cat(zelev_l, np.float64),
        time=cat(t_l, np.float64), node1=cat(node_l, np.int64),
        seed_node1=np.asarray(seeds["node1"], dtype=np.int64),
        seed_locx=np.asarray(seeds["locx"]), seed_locy=np.asarray(seeds["locy"]),
        seed_locz=np.asarray(seeds["locz"]))


# ---------------------------------------------------------------------------
# per-set writers
# ---------------------------------------------------------------------------

def _wrap_ints(values, per_line: int = 80) -> list[str]:
    vals = [str(int(v)) for v in values]
    return [" ".join(vals[i:i + per_line]) + " " for i in range(0, len(vals), per_line)]


def write_nam(dst_dir: Path, set_name: str, *, modflow_rel: str, model_name: str):
    (dst_dir / f"{set_name}.nam").write_text(
        f'list 97 "{set_name}.sum"\n'
        f'main 10 "{set_name}.mdf"\n'
        f'dis 19 "{modflow_rel}\\{model_name}.dis"\n'
        f'locations 71 "{set_name}.loc"\n'
        f'endpoint 75 "{set_name}.ept"\n'
        f'pathline 76 "{set_name}.pth"\n'
        f'head(binary) 730 "{modflow_rel}\\{model_name}.hed"\n'
        f'budget 40 "{modflow_rel}\\{model_name}.ccf"\n', encoding="ascii")


def write_rsp(dst_dir: Path, set_name: str, *, rsp_direction: int):
    (dst_dir / f"{set_name}.rsp").write_text(f"""@MODPATH Version 4.00 (V4, Release 2, 4-2001)
@
@----------------------------------------------------------
@
@ MODPATH response file built by the HYPE tool (GMS 10.7 layout)
@
@----------------------------------------------------------
@
* ENTER THE NAME FILE:
@RESPONSE: HELP LABEL = 4.2.1
"{set_name}.nam"
* DO YOU WANT TO STOP COMPUTING PATHS AFTER A SPECIFIED LENGTH OF TIME ?
@RESPONSE: HELP LABEL = 2.1.41
n
* SELECT THE OUTPUT MODE:
*     1 = ENDPOINTS
*     2 = PATHLINE
*     3 = TIME SERIES
@RESPONSE: HELP LABEL = 2.1.6
2
* DO YOU WANT TO COMPUTE LOCATIONS AT SPECIFIC POINTS IN TIME ?
@RESPONSE: HELP LABEL = 2.1.43
n
* HOW ARE STARTING LOCATIONS TO BE ENTERED?
*     1 = FROM AN EXISTING DATA FILE
*     2 = ARRAYS OF PARTICLES WILL BE GENERATED INTERNALLY
@RESPONSE: HELP LABEL = 2.1.9
1
* IN WHICH DIRECTION SHOULD PARTICLES BE TRACKED?
*     1 = FORWARD IN THE DIRECTION OF FLOW
*     2 = BACKWARDS TOWARD RECHARGE LOCATIONS
@RESPONSE: HELP LABEL = 2.1.10
{rsp_direction}
* HOW SHOULD PARTICLES BE TREATED WHEN THEY ENTER CELLS WITH INTERNAL SINKS ?
*     1 = PASS THROUGH WEAK SINK CELLS
*     2 = STOP AT WEAK SINK CELLS
*     3 = STOP AT WEAK SINK CELLS THAT EXCEED A SPECIFIED STRENGTH
@RESPONSE: HELP LABEL = 2.1.11
1
* DO YOU WANT TO STOP PARTICLES WHENEVER THEY ENTER ONE SPECIFIC ZONE ?
@RESPONSE: HELP LABEL = 2.1.45
n
* DO YOU WANT TO COMPUTE VOLUMETRIC BUDGETS FOR ALL CELLS ?
@RESPONSE: HELP LABEL = 3.1.4
y
* SPECIFY AN ERROR TOLERANCE (IN PERCENT):
@RESPONSE: HELP LABEL = 3.1.1
0.001
*  DO YOU WANT TO CHECK DATA CELL BY CELL ?
@RESPONSE: HELP LABEL = 3.1.2
n
* SUMMARIZE FINAL STATUS OF PARTICLES IN SUMMARY.PTH FILE ?
@RESPONSE: HELP LABEL = 3.1.3
n
""", encoding="ascii")


def write_loc(dst_dir: Path, set_name: str, ms: ModpathSet, *,
              nlay: int, nrow: int, ncol: int):
    from .grid import gms_node1_to_kij_e

    k, i_e, j = gms_node1_to_kij_e(ms.seed_node1, nlay, nrow, ncol)
    lines = []
    for n in range(ms.n_particles):
        lines.append(f"{j[n] + 1} {nrow - i_e[n]} {k[n] + 1} "
                     f"{ms.seed_locx[n]:g} {ms.seed_locy[n]:g} {ms.seed_locz[n]:g} "
                     f"0 0 0 0.0")
    (dst_dir / f"{set_name}.loc").write_text("\n".join(lines) + "\n", encoding="ascii")


def write_mdf(dst_dir: Path, set_name: str, *, ibound_g: np.ndarray, porosity: float):
    """MODPATH V4 main file: header, budget-format flag, LAYCON row, IBOUND arrays
    (north-first), CONSTANT porosity per layer."""
    nlay, nrow, ncol = ibound_g.shape
    lines = ["0 -999.0 -888.0 0 1 1", "COMPACT BINARY"]
    lines += _wrap_ints([1] * nlay, per_line=19)
    for k in range(nlay):
        lines.append("INTERNAL 1 (free) -1")
        for i in range(nrow):
            lines += _wrap_ints(ibound_g[k, i, :])
    lines += [f"CONSTANT {porosity:g}"] * nlay
    (dst_dir / f"{set_name}.mdf").write_text("\n".join(lines) + "\n", encoding="ascii")


def write_pth(dst_dir: Path, set_name: str, ms: ModpathSet):
    header = b"MODPATH 5.0"
    with open(dst_dir / f"{set_name}.pth", "wb") as fh:
        fh.write(header + b" " * (80 - len(header)))
        rec = np.empty(ms.pid.size, dtype=np.dtype(
            [("flag", "<i4"), ("pid", "<i4"), ("x", "<f4"), ("y", "<f4"),
             ("zloc", "<f4"), ("zelev", "<f4"), ("time", "<f4"), ("node", "<i4")]))
        rec["flag"] = 1
        if rec.size:
            rec["flag"][0] = 0                    # start marker on the FILE's first record
        rec["pid"] = ms.pid
        rec["x"] = ms.x
        rec["y"] = ms.y
        rec["zloc"] = ms.zloc
        rec["zelev"] = ms.zelev
        rec["time"] = ms.time
        rec["node"] = ms.node1
        fh.write(rec.tobytes())
        fh.write(struct.pack("<i", 1))


def write_sum_stub(dst_dir: Path, set_name: str, ms: ModpathSet):
    (dst_dir / f"{set_name}.sum").write_text(
        "MODPATH 5.0\n"
        f"Pathlines translated from a MODPATH 7 run by the HYPE tool: "
        f"{ms.n_particles} particles, {ms.pid.size} records, "
        f"direction={'forward' if ms.direction_code == 0 else 'backward'}.\n",
        encoding="ascii")


def write_set(dst_dir: Path, ms: ModpathSet, *, model_name: str,
              ibound_g: np.ndarray, porosity: float):
    """Write the whole <Name>_MODPATH_<set> folder for one translated set."""
    dst_dir.mkdir(parents=True, exist_ok=True)
    nlay, nrow, ncol = ibound_g.shape
    modflow_rel = f"..\\{model_name}_MODFLOW"
    write_nam(dst_dir, ms.set_name, modflow_rel=modflow_rel, model_name=model_name)
    write_rsp(dst_dir, ms.set_name, rsp_direction=ms.rsp_direction)
    write_loc(dst_dir, ms.set_name, ms, nlay=nlay, nrow=nrow, ncol=ncol)
    write_mdf(dst_dir, ms.set_name, ibound_g=ibound_g, porosity=porosity)
    write_pth(dst_dir, ms.set_name, ms)
    write_sum_stub(dst_dir, ms.set_name, ms)
