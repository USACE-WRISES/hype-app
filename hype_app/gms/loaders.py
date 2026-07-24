"""Read a completed hype MF6 workspace into plain arrays for the GMS writers.

Everything is returned in ENGINE order (row 0 = south) and model units (metres, days);
the GMS-order flips live in grid.py. Reads files directly rather than reloading the
whole flopy simulation: the .grb carries the grid, the external ``arrays/*.bin`` files
carry strt/k/k33 (my_utils writes them as MF6 binary layer arrays), and the budget's
CHD records carry the boundary cells (user node numbers with the IFACE aux; verified
that MF6 keeps FULL user-node numbering in grb/ia/ja/budget even when idomain has
zeros). Falls back to ``MFSimulation.load`` only if the binary arrays are absent.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

import numpy as np

GWF_NAME = "gwf_model"
INACTIVE_HEAD = 1e30        # MF6 marker magnitude for inactive/dry cells


@dataclass
class HypeModel:
    """Engine-order model + results. Shapes: top (nrow,ncol); botm/idomain/strt/k/k33/
    head/frf/fff/flf (nlay,nrow,ncol); chd_* flat per boundary cell."""
    nlay: int
    nrow: int
    ncol: int
    delr: np.ndarray
    delc: np.ndarray
    xmin: float
    ymin: float
    top: np.ndarray
    botm: np.ndarray
    idomain: np.ndarray
    strt: np.ndarray
    k: np.ndarray
    k33: np.ndarray
    head: np.ndarray
    frf: np.ndarray
    fff: np.ndarray
    flf: np.ndarray
    chd_node0: np.ndarray       # 0-based engine user nodes
    chd_q: np.ndarray           # m3/d, MF6 sign (positive into the aquifer)
    chd_iface: np.ndarray       # per-cell IFACE (0 where the package had none)
    chd_head: np.ndarray        # specified head = simulated head at the CHD cell
    pertim: float
    totim: float


class GmsExportError(RuntimeError):
    """A hype workspace could not be translated to a GMS project."""


def _read_bin_layer_array(path: Path, nrow: int, ncol: int) -> np.ndarray:
    """One MF6 binary READARRAY layer file: 52-byte header + nrow*ncol values.

    Header = (i4 kstp, i4 kper, f8 pertim, f8 totim, 16s text, i4 ncol, i4 nrow,
    i4 ilay). Value dtype (f8 for reals, i4 for ints) is inferred from file size.
    """
    n = nrow * ncol
    raw = path.read_bytes()
    if len(raw) not in (52 + 8 * n, 52 + 4 * n):
        raise GmsExportError(f"{path.name}: unexpected size {len(raw)} for {nrow}x{ncol}")
    h_ncol, h_nrow = struct.unpack_from("<2i", raw, 40)
    if (h_ncol, h_nrow) != (ncol, nrow):
        raise GmsExportError(f"{path.name}: header says {h_nrow}x{h_ncol}, "
                             f"grid is {nrow}x{ncol}")
    dtype = np.float64 if len(raw) == 52 + 8 * n else np.int32
    return np.frombuffer(raw, dtype=dtype, offset=52, count=n).reshape(nrow, ncol)


def _stack_layers(arrays_dir: Path, stem: str, nlay: int, nrow: int, ncol: int) -> np.ndarray:
    return np.stack([_read_bin_layer_array(arrays_dir / f"{stem}_L{k + 1}.bin", nrow, ncol)
                     for k in range(nlay)]).astype(np.float64)


def _load_arrays_via_flopy(gwf_ws: Path):
    """Fallback when arrays/*.bin are missing: reload the sim (slow but complete)."""
    import flopy

    sim = flopy.mf6.MFSimulation.load(sim_ws=str(gwf_ws), verbosity_level=0)
    gwf = sim.get_model(GWF_NAME)
    return (np.asarray(gwf.ic.strt.array, dtype=np.float64),
            np.asarray(gwf.npf.k.array, dtype=np.float64),
            np.asarray(gwf.npf.k33.array, dtype=np.float64))


def load_hype_model(gwf_ws: str | Path) -> HypeModel:
    import flopy
    from flopy.mf6.utils import MfGrdFile
    from flopy.mf6.utils.postprocessing import get_structured_faceflows

    gwf_ws = Path(gwf_ws)
    grb_path = gwf_ws / f"{GWF_NAME}.dis.grb"
    hds_path = gwf_ws / f"{GWF_NAME}.hds"
    cbb_path = gwf_ws / f"{GWF_NAME}.cbb"
    for p in (grb_path, hds_path, cbb_path):
        if not p.is_file():
            raise GmsExportError(f"groundwater run is incomplete: missing {p.name}")

    grb = MfGrdFile(grb_path)
    mg = grb.modelgrid
    nlay, nrow, ncol = grb.nlay, grb.nrow, grb.ncol
    shape = (nlay, nrow, ncol)
    delr = np.asarray(mg.delr, dtype=np.float64)
    delc = np.asarray(mg.delc, dtype=np.float64)
    top = np.asarray(mg.top, dtype=np.float64).reshape(nrow, ncol)
    botm = np.asarray(mg.botm, dtype=np.float64).reshape(shape)
    idomain = np.asarray(mg.idomain, dtype=np.int32).reshape(shape)

    arrays_dir = gwf_ws / "arrays"
    try:
        strt = _stack_layers(arrays_dir, "strt", nlay, nrow, ncol)
        k = _stack_layers(arrays_dir, "k", nlay, nrow, ncol)
        k33 = _stack_layers(arrays_dir, "k33", nlay, nrow, ncol)
    except (GmsExportError, FileNotFoundError):
        strt, k, k33 = _load_arrays_via_flopy(gwf_ws)
        strt, k, k33 = (np.asarray(a, dtype=np.float64).reshape(shape)
                        for a in (strt, k, k33))

    hds = flopy.utils.HeadFile(hds_path, precision="double")
    head = np.asarray(hds.get_data(), dtype=np.float64).reshape(shape)
    totim = float(hds.get_times()[-1])
    kstpkper = hds.get_kstpkper()[-1]

    cbf = flopy.utils.CellBudgetFile(cbb_path, precision="double")
    flowja = np.asarray(cbf.get_data(text="FLOW-JA-FACE", kstpkper=kstpkper)[-1]).flatten()
    frf, fff, flf = get_structured_faceflows(flowja, grb_file=str(grb_path))

    nodes, qs, ifaces = [], [], []
    for rec in cbf.get_data(text="CHD", kstpkper=kstpkper):
        n0 = np.asarray(rec["node"], dtype=np.int64) - 1
        nodes.append(n0)
        qs.append(np.asarray(rec["q"], dtype=np.float64))
        iface = (np.asarray(rec["IFACE"], dtype=np.float64).astype(np.int32)
                 if "IFACE" in rec.dtype.names else np.zeros(n0.size, dtype=np.int32))
        ifaces.append(iface)
    if not nodes:
        raise GmsExportError("no CHD records in the budget file; nothing anchors the model")
    node0 = np.concatenate(nodes)
    q = np.concatenate(qs)
    iface = np.concatenate(ifaces)

    # A cell can appear in both CHD packages only through an upstream bug; the engine
    # dedupes (my_utils), so summing q per unique node is exact. IFACE keeps the max
    # (river's 6 wins over the sides' 0 if a collision ever happens).
    order = np.argsort(node0, kind="stable")
    node0, q, iface = node0[order], q[order], iface[order]
    uniq, start = np.unique(node0, return_index=True)
    q_sum = np.add.reduceat(q, start)
    iface_max = np.maximum.reduceat(iface, start)

    return HypeModel(
        nlay=nlay, nrow=nrow, ncol=ncol, delr=delr, delc=delc,
        xmin=float(mg.xoffset), ymin=float(mg.yoffset),
        top=top, botm=botm, idomain=idomain, strt=strt, k=k, k33=k33,
        head=head, frf=frf, fff=fff, flf=flf,
        chd_node0=uniq, chd_q=q_sum, chd_iface=iface_max,
        chd_head=head.ravel()[uniq].copy(),
        pertim=totim, totim=totim,
    )
