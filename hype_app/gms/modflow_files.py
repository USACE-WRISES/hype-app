"""Writers for the <Name>_MODFLOW folder of a GMS 10.7 project.

Formats are byte-modeled on the decoded example project (GMS 10.7.4, MODFLOW-2005
with GMS-HDF5 arrays): text package files carry `HDF5 ...` array-control lines that
point into <Name>.h5, heads/budget are classic SINGLE-precision MODFLOW binaries, and
<Name>.h5 follows Aquaveo's "MFH5 3.0" layout (flat per-layer arrays + numbered
boundary-condition datasets). Every function takes GMS-ORDER arrays (row 0 = north;
see grid.py for the flip) and writes one file; export.py orchestrates.

All \\n line endings; internal file references are bare filenames (the whole folder
is self-contained). Inactive cells are clamped to HNOFLO before any float32 cast so
MF6's 1e30 markers can never turn into inf.
"""
from __future__ import annotations

import shutil
import struct
from pathlib import Path

import numpy as np

HNOFLO = -999.0
HDRY = -888.0
ILPFCB = 40                      # LPF cell-by-cell unit, mirrored in .mfn and .oc

_HEAD_TEXT = b"            HEAD"
_CCF_TEXTS = {"chd": b"   CONSTANT HEAD", "frf": b"FLOW RIGHT FACE ",
              "fff": b"FLOW FRONT FACE ", "flf": b"FLOW LOWER FACE "}


def _fmt(v: float) -> str:
    return f"{float(v):.13g}"


def _row(values) -> str:
    return " ".join(_fmt(v) for v in np.asarray(values)) + " "


def _int_row(values) -> str:
    return " ".join(str(int(v)) for v in np.asarray(values)) + " "


def _hdf5_line(name: str, dataset: str, n: int) -> str:
    return f'HDF5 1.0 -1 "{name}.h5" "{dataset}" 1 0 {n}'


# ---------------------------------------------------------------------------
# tiny text files
# ---------------------------------------------------------------------------

def write_prj(dst_dir: Path, name: str, wkt: str):
    (dst_dir / f"{name}.prj").write_text(wkt, encoding="ascii")


def write_mfr(dst_dir: Path, name: str):
    (dst_dir / f"{name}.mfr").write_text(
        f'GMS RESULT FILE\nHED "{name}.hed"\nCCF "{name}.ccf"\n', encoding="ascii")


def write_mfw(dst_dir: Path, name: str, orig: tuple[float, float, float], rotz: float):
    (dst_dir / f"{name}.mfw").write_text(
        f"ORIG {_fmt(orig[0])} {_fmt(orig[1])} {_fmt(orig[2])}\n"
        f"ROTZ {_fmt(rotz)}\n"
        f'PRJ_FILE "{name}.prj"\n', encoding="ascii")


def write_out_stub(dst_dir: Path, name: str):
    (dst_dir / f"{name}.out").write_text(
        "Translated from a MODFLOW 6 run by the HYPE tool.\n"
        "The full MODFLOW 6 listing lives in the exported project's\n"
        "5_Groundwater/model/gwf_workspace folder (mfsim.lst, gwf_model.lst).\n",
        encoding="ascii")


def write_mfn(dst_dir: Path, name: str):
    (dst_dir / f"{name}.mfn").write_text(f"""# MF2K NAME file
#
# MODFLOW 2005
# Output Files
LIST            2 "{name}.out"
DATA(BINARY)    3 "{name}.hed"
DATA(BINARY)   {ILPFCB} "{name}.ccf"
#
# Global Input Files
DIS             9 "{name}.dis"
#
# Flow Process Input Files
BAS6           10 "{name}.ba6"
LPF            11 "{name}.lpf"
OC             12 "{name}.oc"
CHD            13 "{name}.chd"
PCG            14 "{name}.pcg"
""", encoding="ascii")


def write_mfs(dst_dir: Path, name: str, *, orig: tuple[float, float, float],
              rotz: float, porosity: float, vani: float):
    """The GMS MODFLOW super file. Mirrors the example line-for-line (GMS's loader
    is the only consumer) with our placement/material values; the example's
    STARTING_HEADS_EQUAL_TOPS is intentionally absent (hype's strt is anchored to
    the model origin, not the terrain)."""
    (dst_dir / f"{name}.mfs").write_text(f"""MF2K5SUP
MFPRECISION SINGLE
MFPARALLEL ON
# GMSVERS 10.7.4
IJK -y +x -z
NAME   99 "{name}.mfn"
RUNMETHOD2 FORRUN
STO 0 0 0 1 "Empty" "Empty"
PRIORPOW 1.0
ORIG {_fmt(orig[0])} {_fmt(orig[1])} {_fmt(orig[2])}
ROTZ {_fmt(rotz)}
LAYER   0
      1.0000
COMPUTE_OBS_FLOW 1
HFFSTANDARD 1
DMAT 1
MHANI 1 0 1.0
MHC 1 0 0.0
MLD 1 0 0.0
MPOR 1 0 {_fmt(porosity)}
MSPECSTOR 1 0 0.0
MSPECYIELD 1 0 0.0
MVANI 1 0 {_fmt(vani)}
MLK 1 0 0.0
INVMOD 1
ENABLEASP 0
NOCONVSTP 0
HDRYBOT 0
VCONT1 1
REFTIME  s 0 1900 1 1 0 0 0
UNITS "m"\t"d"\t"mg"\t"lb"\t"mg/l"
USESHEAD 0
USESARRAYS 1
GROUPWEIGHTS 1.0 1.0 1.0 1.0 1.0 1.0 1.0
REGULOPT 0.001 0.0011 0.1
AUI noaui
NOPTMAX 20
NPHISTP 3
MAXRELITER 3
PHIREDSTP 0.005
NRELPAR 3
RELPARSTP 0.005
NPHINORED 5.0
FACPARMAX 5.0
ICOV 1
ICOR 1
IEIG 1
PPEST 1 4 0.001
PEST_SVD 1000 1.0e-007 0
""", encoding="ascii")


# ---------------------------------------------------------------------------
# package files (HDF5 array-control lines into <name>.h5)
# ---------------------------------------------------------------------------

def write_dis(dst_dir: Path, name: str, *, nlay: int, nrow: int, ncol: int,
              delr, delc, perlen: float):
    n = nrow * ncol
    lines = ["# MF2K DISCRETIZATION FILE", "#", "#",
             "# NLAY NROW NCOL NPER TIMEUNITS LENUNITS",
             f"{nlay} {nrow} {ncol} 1 4 2",          # days, metres
             _int_row([0] * nlay),                   # LAYCBD
             "INTERNAL 1.0 (free) -1", _row(delr),
             "INTERNAL 1.0 (free) -1", _row(delc),
             _hdf5_line(name, "Arrays/top1", n)]
    lines += [_hdf5_line(name, f"Arrays/bot{k + 1}", n) for k in range(nlay)]
    lines += [f"{_fmt(perlen)} 1 1.0 SS"]
    (dst_dir / f"{name}.dis").write_text("\n".join(lines) + "\n", encoding="ascii")


def write_ba6(dst_dir: Path, name: str, *, nlay: int, nrow: int, ncol: int):
    n = nrow * ncol
    lines = ["#Base", "#HYPE export", "FREE"]
    lines += [_hdf5_line(name, f"Arrays/ibound{k + 1}", n) for k in range(nlay)]
    lines += [_fmt(HNOFLO)]
    lines += [_hdf5_line(name, f"Arrays/StartHead{k + 1}", n) for k in range(nlay)]
    (dst_dir / f"{name}.ba6").write_text("\n".join(lines) + "\n", encoding="ascii")


def write_lpf(dst_dir: Path, name: str, *, nlay: int, nrow: int, ncol: int):
    n = nrow * ncol
    lines = [f"{ILPFCB} {_fmt(HDRY)} 0",
             _int_row([1] * nlay),                   # LAYTYP convertible
             _int_row([0] * nlay),                   # LAYAVG
             _row([-1.0] * nlay),                    # CHANI -1 -> HANI arrays
             _int_row([1] * nlay),                   # LAYVKA 1 -> VANI = Kh/Kv
             _int_row([0] * nlay)]                   # LAYWET
    for k in range(nlay):
        lines += [_hdf5_line(name, f"Arrays/HK{k + 1}", n),
                  _hdf5_line(name, f"Arrays/HANI{k + 1}", n),
                  _hdf5_line(name, f"Arrays/VANI{k + 1}", n)]
    (dst_dir / f"{name}.lpf").write_text("\n".join(lines) + "\n", encoding="ascii")


def write_chd(dst_dir: Path, name: str, *, nbc: int):
    (dst_dir / f"{name}.chd").write_text(
        "#GMS_HDF5_01\n"
        f"{nbc} AUX SHEADFACT AUX EHEADFACT AUX CELLGRP\n"
        f"{nbc:>10}{0:>10}{0:>10}\n"
        f'GMS_HDF5_01 "{name}.h5" "Specified Head" 1\n', encoding="ascii")


def write_oc(dst_dir: Path, name: str):
    (dst_dir / f"{name}.oc").write_text(
        "HEAD SAVE UNIT 3\nCOMPACT BUDGET AUX\nPERIOD 1 STEP 1\n"
        "    PRINT BUDGET\n    SAVE HEAD\n    SAVE BUDGET\n", encoding="ascii")


def write_pcg(dst_dir: Path, name: str):
    (dst_dir / f"{name}.pcg").write_text(
        "25 50 1 0\n 0.01 0.01 1.0 0 0 2 1.0 0.0\n", encoding="ascii")


# ---------------------------------------------------------------------------
# <name>.h5 — Aquaveo "MFH5 3.0" arrays + Specified Head
# ---------------------------------------------------------------------------

def write_mfh5(dst_dir: Path, name: str, skeleton: Path, *,
               ibound_g: np.ndarray, strt_g: np.ndarray, top_g: np.ndarray,
               botm_g: np.ndarray, hk_g: np.ndarray, vani_g: np.ndarray,
               chd_node1_g: np.ndarray, chd_head: np.ndarray, chd_iface: np.ndarray):
    """Copy the committed empty MFH5 skeleton and fill Arrays + Specified Head.

    The skeleton (built by tools/make_gms_template.py from the example) carries the
    root identity datasets and the empty per-BC-type template groups GMS's loader
    expects; only the two groups hype populates are (re)written here.
    """
    import h5py

    nlay, nrow, ncol = ibound_g.shape
    n = nrow * ncol
    dst = dst_dir / f"{name}.h5"
    shutil.copyfile(skeleton, dst)

    def put(grp, dname, data, dtype):
        flat = np.asarray(data, dtype=dtype).reshape(-1)
        grp.create_dataset(dname, data=flat, chunks=(flat.size,),
                           compression="gzip", maxshape=(None,))

    with h5py.File(dst, "r+") as f:
        arrays = f["Arrays"]
        put(arrays, "top1", top_g, np.float64)
        for k in range(nlay):
            put(arrays, f"ibound{k + 1}", ibound_g[k], np.int32)
            put(arrays, f"StartHead{k + 1}", strt_g[k], np.float64)
            put(arrays, f"bot{k + 1}", botm_g[k], np.float64)
            put(arrays, f"HK{k + 1}", hk_g[k], np.float64)
            put(arrays, f"HANI{k + 1}", np.ones(n), np.float64)
            put(arrays, f"VANI{k + 1}", vani_g[k], np.float64)

        sh = f["Specified Head"]
        for dname in ("00. Number of BCs", "01. Use Last", "02. Cell IDs",
                      "03. Name", "04. Map ID", "06. IFACE", "07. Property"):
            if dname in sh:
                del sh[dname]
        nbc = int(chd_node1_g.size)

        def bc1d(dname, data, dtype, strlen: int | None = None):
            d = sh.create_dataset(dname, data=np.asarray(data, dtype=dtype),
                                  chunks=(50,), compression="gzip", maxshape=(None,))
            if strlen is not None:
                d.attrs["Max. String Length"] = np.array([strlen], dtype=np.int32)

        bc1d("00. Number of BCs", [nbc], np.int32)
        bc1d("01. Use Last", [0], np.int32)
        bc1d("02. Cell IDs", chd_node1_g, np.int32)
        bc1d("03. Name", np.zeros(nbc), np.int8, strlen=1)
        bc1d("04. Map ID", np.zeros(nbc), np.int8, strlen=1)
        bc1d("06. IFACE", chd_iface, np.int32)
        prop = np.zeros((6, nbc, 1), dtype=np.float64)
        prop[0, :, 0] = chd_head                     # Shead
        prop[1, :, 0] = chd_head                     # Ehead (steady state)
        prop[2:4, :, 0] = 1.0                        # SHEADFACT / EHEADFACT
        sh.create_dataset("07. Property", data=prop, chunks=(6, 500, 50),
                          compression="gzip", maxshape=(None, None, None))
    return dst


# ---------------------------------------------------------------------------
# binary results — classic single-precision MODFLOW files
# ---------------------------------------------------------------------------

def _masked_head_f4(head_g: np.ndarray, ibound_g: np.ndarray) -> np.ndarray:
    """Heads with inactive/dry markers clamped to HNOFLO, as float32 planes."""
    out = np.array(head_g, dtype=np.float64, copy=True)
    out[(ibound_g == 0) | (np.abs(out) >= 1e29)] = HNOFLO
    return out.astype(np.float32)


def write_hed(dst_dir: Path, name: str, *, head_g: np.ndarray, ibound_g: np.ndarray,
              pertim: float, totim: float):
    nlay, nrow, ncol = head_g.shape
    vals = _masked_head_f4(head_g, ibound_g)
    with open(dst_dir / f"{name}.hed", "wb") as fh:
        for k in range(nlay):
            fh.write(struct.pack("<2i2f", 1, 1, pertim, totim))
            fh.write(_HEAD_TEXT)
            fh.write(struct.pack("<3i", ncol, nrow, k + 1))
            fh.write(vals[k].astype("<f4").tobytes())


def write_hed_h5(dst_dir: Path, name: str, *, head_g: np.ndarray,
                 ibound_g: np.ndarray, totim: float):
    """The Xmdf display-dataset companion GMS keeps next to .hed. Cheap insurance:
    GMS appears to regenerate it from .hed on load (the example's copy has a zero
    GUID and no compression), but shipping a valid one costs nothing."""
    import h5py

    ncells = head_g.size
    vals = _masked_head_f4(head_g, ibound_g).reshape(1, ncells)
    active = (ibound_g != 0).reshape(1, ncells).astype(np.uint8)
    act_vals = vals[active.astype(bool)]
    with h5py.File(dst_dir / f"{name}.hed.h5", "w") as f:
        f.create_dataset("File Type", data=np.array([b"Xmdf"], dtype="S5"))
        f.create_dataset("File Version", data=np.array([99.99], dtype=np.float32))
        ds = f.create_group("Datasets")
        ds.attrs["Grouptype"] = np.array([b"MULTI DATASETS"], dtype="S15")
        ds.create_dataset("Guid", data=np.array(
            [b"00000000-0000-0000-0000-000000000000"], dtype="S37"))
        head = ds.create_group("Head")
        head.attrs["Data Type"] = np.array([0], dtype=np.int32)
        head.attrs["DatasetCompression"] = np.array([-1], dtype=np.int32)
        head.attrs["DatasetLocation"] = np.array([1], dtype=np.int32)
        head.attrs["DatasetUnits"] = np.array([b""], dtype="S1")
        head.attrs["Grouptype"] = np.array([b"DATASET SCALAR"], dtype="S15")
        head.attrs["TimeUnits"] = np.array([b"Days"], dtype="S5")
        head.attrs["Version"] = np.array([1], dtype=np.int32)
        head.create_dataset("Values", data=vals, chunks=(1, ncells),
                            maxshape=(None, ncells))
        head.create_dataset("Active", data=active, chunks=(1, ncells),
                            maxshape=(None, ncells))
        head.create_dataset("Times", data=np.array([totim], dtype=np.float64),
                            chunks=(10,), maxshape=(None,))
        head.create_dataset("Mins", data=np.array(
            [act_vals.min() if act_vals.size else 0.0], dtype=np.float32),
            chunks=(10,), maxshape=(None,))
        head.create_dataset("Maxs", data=np.array(
            [act_vals.max() if act_vals.size else 0.0], dtype=np.float32),
            chunks=(10,), maxshape=(None,))


def write_ccf(dst_dir: Path, name: str, *, chd_node1_g: np.ndarray, chd_q: np.ndarray,
              frf_g: np.ndarray, fff_g: np.ndarray, flf_g: np.ndarray,
              delt: float, pertim: float, totim: float):
    """Classic COMPACT single-precision cell-by-cell budget, records in the example's
    order: CONSTANT HEAD (imeth=2 list), then FRF/FFF/FLF (imeth=1 full arrays)."""
    nlay, nrow, ncol = frf_g.shape

    def header(fh, text: bytes, imeth: int):
        fh.write(struct.pack("<2i", 1, 1))
        fh.write(text)
        fh.write(struct.pack("<3i", ncol, nrow, -nlay))
        fh.write(struct.pack("<i3f", imeth, delt, pertim, totim))

    order = np.argsort(chd_node1_g, kind="stable")
    nodes = np.asarray(chd_node1_g, dtype=np.int32)[order]
    qs = np.asarray(chd_q, dtype=np.float32)[order]
    with open(dst_dir / f"{name}.ccf", "wb") as fh:
        header(fh, _CCF_TEXTS["chd"], imeth=2)
        fh.write(struct.pack("<i", nodes.size))
        pairs = np.empty(nodes.size, dtype=np.dtype([("node", "<i4"), ("q", "<f4")]))
        pairs["node"] = nodes
        pairs["q"] = qs
        fh.write(pairs.tobytes())
        for key, arr in (("frf", frf_g), ("fff", fff_g), ("flf", flf_g)):
            header(fh, _CCF_TEXTS[key], imeth=1)
            fh.write(np.asarray(arr, dtype="<f4").tobytes())
