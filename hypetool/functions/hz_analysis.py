"""Hyporheic-zone delineation: post-hoc MODPATH 7 classification of every model cell.

Seeds particles in every active, saturated, non-boundary cell of a COMPLETED groundwater
run (no MODFLOW re-run — the flow solution is read from the existing workspace), tracks
them forward AND backward with MODPATH 7 in ENDPOINT mode (one output record per particle,
so a million seeds stays tractable), and classifies each particle by where its water came
from (backward terminus) and where it goes (forward terminus):

    origin  exit    class
    top     top     hyporheic          (stream -> subsurface -> stream)
    top     side    losing             (streamflow lost to the aquifer)
    side    top     gaining            (groundwater discharging to the stream)
    side    side    throughflow        (groundwater passing beneath the reach)
    (anything unresolved: internal termination / still active at the stop time)

"top" is the stream/wetted CHD_RIVER boundary; "side" is one of the CHD_SIDES faces
(upstream / downstream / left / right — the identity is kept for the stats).

Per-cell class fractions implement a streamtube volume split: each seed samples the
streamline through its own cell, so a cell's hyporheic volume share is (its hyporheic
particles) / (its classified particles). Full pathline geometry is produced only for a
stratified display sample per class (pathline files for every seed would be gigabytes).

Scale guide (endpoint mode, per tracking direction):
    100 k particles ~ 0.5-3 min MP7, ~30 MB .mpend, seconds to parse
    1 M   particles ~ 5-30 min MP7, ~300 MB .mpend, ~1 GB transient RAM while parsing
The caller enforces warn/abort thresholds; raw MP7 outputs are deleted after parsing
unless keep_raw_outputs=True.

Why the sims are built manually instead of via Modpath7.create_mp7: create_mp7 hardcodes
simulationtype="combined", and combined/timeseries simulations carry a time-point table
(default 100 points spanning the stop time) at which MODPATH stops tracking — the source
of a long-standing silent ~100-day truncation of slow paths. Pure "endpoint" / "pathline"
simulations have no time points, so tracking runs to true termination.
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path
from typing import Callable

import numpy as np

import flopy
from flopy.modpath import Modpath7, Modpath7Bas, Modpath7Sim, ParticleData, ParticleGroup
from flopy.utils import EndpointFile, PathlineFile

# ---------------------------------------------------------------------------
# Codes
# ---------------------------------------------------------------------------

CLS = {"unresolved": 0, "hyporheic": 1, "losing": 2, "gaining": 3, "throughflow": 4}
CLS_NAMES = {v: k for k, v in CLS.items()}
HZ_CLASSES = ("hyporheic", "losing", "gaining", "throughflow")

MEMBER = {"none": 0, "top": 1, "left": 2, "right": 3, "upstream": 4, "downstream": 5}
MEMBER_NAMES = {v: k for k, v in MEMBER.items()}
_SIDE_CODES = (MEMBER["left"], MEMBER["right"], MEMBER["upstream"], MEMBER["downstream"])

# MODPATH 7 endpoint STATUS values that mean "tracking finished at a real terminus":
#   2 terminated at a boundary face, 3 stopped in a weak-sink cell, 4 stopped in a
#   weak-source cell (backward tracking), 5 terminated in a cell with no exit face
#   (strong sink), 6 stopped in a stop zone. Anything else (0/1 pending or still
#   active at the stop time, 7 inactive cell, 8 unreleased, 9 unknown) is unresolved.
RESOLVED_STATUSES = frozenset({2, 3, 4, 5, 6})

_HDRY_LIMIT = 1.0e29  # |head| above this = dry/inactive marker in the heads file


# ---------------------------------------------------------------------------
# 1a. Load + boundary membership
# ---------------------------------------------------------------------------

def load_flow_model(gwf_ws: str | Path, gwf_name: str = "gwf_model"):
    """Load the completed MF6 simulation from its workspace. Returns (sim, gwf)."""
    gwf_ws = Path(gwf_ws)
    if not (gwf_ws / "mfsim.nam").exists():
        raise FileNotFoundError(
            f"No MODFLOW 6 workspace at {gwf_ws} — run the groundwater model first.")
    sim = flopy.mf6.MFSimulation.load(sim_ws=str(gwf_ws), verbosity_level=0)
    try:
        gwf = sim.get_model(gwf_name)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Model '{gwf_name}' not found in {gwf_ws}: {e}") from e
    if gwf is None:
        raise RuntimeError(f"Model '{gwf_name}' not found in {gwf_ws}")
    return sim, gwf


def load_heads(gwf) -> np.ndarray:
    """Simulated heads (nlay, nrow, ncol) with dry/no-flow markers as NaN."""
    h = np.asarray(gwf.output.head().get_data(), dtype=float)
    h[np.abs(h) > _HDRY_LIMIT] = np.nan
    return h


def cell_geometry(top2d: np.ndarray, botm3d: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-cell top/bottom arrays T, B of shape (nlay, nrow, ncol).

    Layer 0 is the DEM-following variable-thickness layer (T[0] = terrain top), all
    deeper tops are the bottom of the layer above — layer-0 thickness is top - bed,
    NOT the uniform slab thickness.
    """
    top2d = np.asarray(top2d, dtype=float)
    botm3d = np.asarray(botm3d, dtype=float)
    T = np.concatenate([top2d[None, :, :], botm3d[:-1]], axis=0)
    return T, botm3d


def classify_side_cells(kij: np.ndarray, mg, left_line, right_line, up_line, down_line,
                        ) -> np.ndarray:
    """Side code (MEMBER left/right/upstream/downstream) per boundary cell.

    Nearest-line rule on cell centers, with the same tie-break order as
    classify_boundary_cells_faster (left, right, upstream, downstream — first minimum
    wins). Lines are shapely geometries / GeoDataFrames already in the model CRS.
    """
    import shapely

    def _merged(g):
        geoms = getattr(g, "geometry", None)
        if geoms is not None:  # GeoDataFrame / GeoSeries
            return geoms.union_all() if hasattr(geoms, "union_all") else g.unary_union
        return g  # already a shapely geometry

    lines = [(_merged(left_line), MEMBER["left"]),
             (_merged(right_line), MEMBER["right"]),
             (_merged(up_line), MEMBER["upstream"]),
             (_merged(down_line), MEMBER["downstream"])]

    kij = np.asarray(kij, dtype=int).reshape(-1, 3)
    # Cell centers in ENGINE row order. The engine fills its DIS arrays SOUTH-first (row 0 =
    # southernmost — cell centers built with np.linspace(ymin→ymax) in create_grid), so flopy's
    # north-first mg.ycellcenters would MIRROR the rows N↔S here (mg row conventions describe
    # flopy's grid object, not this engine's array order). Build centers from delr/delc directly.
    delr = np.asarray(mg.delr, dtype=float)
    delc = np.asarray(mg.delc, dtype=float)
    xc1 = float(mg.xoffset) + np.cumsum(delr) - delr / 2.0          # (ncol,) west->east
    yc1 = float(mg.yoffset) + np.cumsum(delc) - delc / 2.0          # (nrow,) SOUTH->north
    pts = shapely.points(xc1[kij[:, 2]], yc1[kij[:, 1]])
    d = np.column_stack([shapely.distance(pts, ln) for ln, _ in lines])
    order = np.argmin(d, axis=1)  # ties -> first (left, right, upstream, downstream)
    codes = np.asarray([code for _, code in lines], dtype=np.uint8)
    return codes[order]


def extract_bc_membership(gwf, left_line, right_line, up_line, down_line,
                          log: Callable = print) -> tuple[np.ndarray, dict]:
    """Boundary membership per flat node: 0 interior, 1 top(river), 2-5 sides.

    River cells come from the CHD_RIVER package, side cells from CHD_SIDES; side
    identity via nearest boundary line. River is written LAST so it wins where a
    cell appears in both packages' footprints.
    """
    nlay, nrow, ncol = gwf.modelgrid.nlay, gwf.modelgrid.nrow, gwf.modelgrid.ncol
    member = np.zeros(nlay * nrow * ncol, dtype=np.uint8)

    def _cellids(pkg) -> np.ndarray:
        spd = pkg.stress_period_data.get_data(0)
        return np.asarray([rec[0] for rec in spd], dtype=int).reshape(-1, 3)

    counts: dict[str, int] = {}
    sides_pkg = gwf.get_package("CHD_SIDES")
    if sides_pkg is not None:
        kij = _cellids(sides_pkg)
        side_codes = classify_side_cells(kij, gwf.modelgrid,
                                         left_line, right_line, up_line, down_line)
        nodes = kij[:, 0] * nrow * ncol + kij[:, 1] * ncol + kij[:, 2]
        member[nodes] = side_codes
        for code in _SIDE_CODES:
            counts[MEMBER_NAMES[code]] = int((side_codes == code).sum())

    river_pkg = gwf.get_package("CHD_RIVER")
    if river_pkg is None:
        raise RuntimeError("CHD_RIVER package not found — cannot identify the top "
                           "(stream) boundary for classification.")
    kij_r = _cellids(river_pkg)
    nodes_r = kij_r[:, 0] * nrow * ncol + kij_r[:, 1] * ncol + kij_r[:, 2]
    member[nodes_r] = MEMBER["top"]  # river wins overlaps
    counts["top"] = int(nodes_r.size)

    log(f"Boundary membership: {counts}")
    return member, {"counts": counts}


# ---------------------------------------------------------------------------
# 1b. Seeding
# ---------------------------------------------------------------------------

# Placement templates: plan-view pattern x z-levels (all local cell coordinates 0..1).
_PLAN_CENTER = ((0.5, 0.5),)
_PLAN_TRIANGLE = ((0.25, 0.25), (0.75, 0.25), (0.5, 0.75))
_Z1 = (0.5,)
_Z2 = (0.25, 0.75)
_Z3 = (0.17, 0.5, 0.83)
SEED_TEMPLATES: dict[int, tuple[tuple[float, float, float], ...]] = {
    1: tuple((x, y, z) for z in _Z1 for (x, y) in _PLAN_CENTER),
    3: tuple((x, y, z) for z in _Z3 for (x, y) in _PLAN_CENTER),
    6: tuple((x, y, z) for z in _Z2 for (x, y) in _PLAN_TRIANGLE),
    9: tuple((x, y, z) for z in _Z3 for (x, y) in _PLAN_TRIANGLE),
}


def build_seed_arrays(*, idomain: np.ndarray, member: np.ndarray, head: np.ndarray,
                      T: np.ndarray, B: np.ndarray, particles_per_cell: int = 1,
                      min_sat_frac: float = 0.05) -> dict:
    """Seed particles in every active, saturated, non-boundary cell.

    Boundary (CHD) cells are excluded — they are the boundary reservoirs themselves and
    seeds there produce zero-length legs; their interface volume is represented by the
    neighboring cells' fractions. Dry / nearly-dry cells are skipped (they would only
    inflate "unresolved"). Template z-positions are scaled by each cell's saturated
    fraction so no particle starts above the water table.
    """
    tpl = SEED_TEMPLATES.get(int(particles_per_cell))
    if tpl is None:
        raise ValueError(f"particles_per_cell must be one of {sorted(SEED_TEMPLATES)}, "
                         f"got {particles_per_cell}")

    idomain = np.asarray(idomain, dtype=int)
    nlay, nrow, ncol = idomain.shape
    thick = np.maximum(T - B, 1e-9)
    sat = np.clip((head - B) / thick, 0.0, 1.0)
    sat = np.where(np.isnan(sat), 0.0, sat)

    eligible = ((idomain == 1)
                & (member.reshape(nlay, nrow, ncol) == 0)
                & (sat >= float(min_sat_frac)))
    kk, ii, jj = np.nonzero(eligible)
    n_cells = int(kk.size)
    if n_cells == 0:
        raise RuntimeError("No eligible cells to seed (all cells are boundary, "
                           "inactive, or dry).")

    npc = len(tpl)
    tx = np.asarray([t[0] for t in tpl], dtype=np.float32)
    ty = np.asarray([t[1] for t in tpl], dtype=np.float32)
    tz = np.asarray([t[2] for t in tpl], dtype=np.float32)

    kij = np.empty((n_cells * npc, 3), dtype=np.int32)
    kij[:, 0] = np.repeat(kk, npc)
    kij[:, 1] = np.repeat(ii, npc)
    kij[:, 2] = np.repeat(jj, npc)
    localx = np.tile(tx, n_cells)
    localy = np.tile(ty, n_cells)
    # clamp template z into the saturated part of each cell
    satf = np.repeat(sat[kk, ii, jj].astype(np.float32), npc)
    localz = np.tile(tz, n_cells) * satf

    n = kij.shape[0]
    seed_node = (kij[:, 0].astype(np.int64) * nrow * ncol
                 + kij[:, 1].astype(np.int64) * ncol + kij[:, 2])
    return {"kij": kij, "localx": localx, "localy": localy, "localz": localz,
            "particleids": np.arange(1, n + 1, dtype=np.int32),
            "seed_node": seed_node, "n_cells_seeded": n_cells, "n_seeds": n}


def subset_seeds(seeds: dict, idx: np.ndarray) -> dict:
    """Seed dict restricted to `idx`, re-numbered 1..M, keeping the original pids."""
    idx = np.asarray(idx, dtype=np.int64)
    return {"kij": seeds["kij"][idx],
            "localx": seeds["localx"][idx], "localy": seeds["localy"][idx],
            "localz": seeds["localz"][idx],
            "particleids": np.arange(1, idx.size + 1, dtype=np.int32),
            "orig_pid": seeds["particleids"][idx].astype(np.int64),
            "seed_node": seeds["seed_node"][idx],
            "n_seeds": int(idx.size)}


# ---------------------------------------------------------------------------
# 1c. MODPATH 7 simulation construction
# ---------------------------------------------------------------------------

def resolve_mp7_exe(modflow_bin_dir: str | Path | None) -> str:
    """Path to the mp7 executable (falls back to 'mp7' on PATH)."""
    if modflow_bin_dir:
        exe = Path(modflow_bin_dir) / ("mp7.exe" if sys.platform.startswith("win") else "mp7")
        if exe.exists():
            return str(exe)
    return "mp7"


def build_hz_mp7_sim(gwf, seeds: dict, *, mp7_ws: str | Path, exe: str,
                     direction: str, mode: str, porosity: float = 0.3,
                     max_time_days: float | None = 1.0e6,
                     name: str | None = None) -> Modpath7:
    """One MODPATH 7 simulation (pure endpoint or pathline — never 'combined').

    weak sink AND weak source are stop_at in both directions so particles terminate
    inside the CHD cells (which is what makes terminal node -> membership lookups
    work symmetrically for forward and backward tracking).
    """
    assert mode in ("endpoint", "pathline"), mode
    assert direction in ("forward", "backward"), direction
    mp7_ws = Path(mp7_ws)
    mp7_ws.mkdir(parents=True, exist_ok=True)
    name = name or f"hz_{mode[:3]}_{direction[:3]}"

    mp = Modpath7(modelname=name, flowmodel=gwf, exe_name=str(exe),
                  model_ws=str(mp7_ws))
    Modpath7Bas(mp, porosity=float(porosity))

    pdata = ParticleData(
        [tuple(r) for r in np.asarray(seeds["kij"], dtype=int)],
        structured=True,
        localx=np.asarray(seeds["localx"], dtype=float),
        localy=np.asarray(seeds["localy"], dtype=float),
        localz=np.asarray(seeds["localz"], dtype=float),
        drape=0,
        particleids=np.asarray(seeds["particleids"], dtype=int),
    )
    pg = ParticleGroup(particlegroupname="HZ", particledata=pdata,
                       filename=f"{name}.sloc")

    stop_kwargs = ({"stoptimeoption": "specified", "stoptime": float(max_time_days)}
                   if max_time_days else {"stoptimeoption": "extend"})
    Modpath7Sim(mp, simulationtype=mode, trackingdirection=direction,
                weaksinkoption="stop_at", weaksourceoption="stop_at",
                budgetoutputoption="no", referencetime=0.0,
                particlegroups=[pg], **stop_kwargs)
    return mp


def run_mp7(mp: Modpath7, log: Callable = print) -> None:
    """Write + run one MODPATH sim; raise with the output tail on failure."""
    mp.write_input()
    t0 = time.monotonic()
    success, buff = mp.run_model(silent=True, report=True)
    dt = time.monotonic() - t0
    if not success:
        tail = "\n".join(str(b) for b in (buff or [])[-25:])
        raise RuntimeError(f"MODPATH 7 failed for {mp.name}:\n{tail}")
    log(f"MODPATH {mp.name} finished in {dt:.1f} s")


# ---------------------------------------------------------------------------
# 1d. Classification
# ---------------------------------------------------------------------------

def classify_particles(ep_fwd: np.ndarray, ep_bwd: np.ndarray, member: np.ndarray,
                       n_seeds: int) -> dict:
    """Join forward/backward endpoint records by particle id and classify.

    flopy zero-bases particleid/node/node0 on read, so after sorting by particleid
    both recarrays are in seed order (ids exactly 0..n_seeds-1).
    """
    def _ordered(ep, tag):
        ep = np.sort(np.asarray(ep), order="particleid")
        pids = np.asarray(ep["particleid"], dtype=np.int64)
        if pids.size != n_seeds or pids[0] != 0 or pids[-1] != n_seeds - 1 \
                or not np.array_equal(pids, np.arange(n_seeds)):
            raise RuntimeError(
                f"{tag} endpoint file has {pids.size} particles with ids "
                f"[{pids.min() if pids.size else '-'}..{pids.max() if pids.size else '-'}], "
                f"expected 0..{n_seeds - 1} — cannot join forward/backward runs.")
        return ep

    ep_fwd = _ordered(ep_fwd, "forward")
    ep_bwd = _ordered(ep_bwd, "backward")

    node0_f = np.asarray(ep_fwd["node0"], dtype=np.int64)
    node0_b = np.asarray(ep_bwd["node0"], dtype=np.int64)
    probe = slice(0, min(n_seeds, 5000))
    if not np.array_equal(node0_f[probe], node0_b[probe]):
        raise RuntimeError("forward/backward endpoint start nodes disagree — the two "
                           "runs were not seeded identically.")

    member = np.asarray(member)
    exit_code = member[np.asarray(ep_fwd["node"], dtype=np.int64)]
    origin_code = member[np.asarray(ep_bwd["node"], dtype=np.int64)]
    status_f = np.asarray(ep_fwd["status"], dtype=np.int16)
    status_b = np.asarray(ep_bwd["status"], dtype=np.int16)

    res_f = np.isin(status_f, list(RESOLVED_STATUSES)) & (exit_code != 0)
    res_b = np.isin(status_b, list(RESOLVED_STATUSES)) & (origin_code != 0)
    resolved = res_f & res_b

    from_top = origin_code == MEMBER["top"]
    to_top = exit_code == MEMBER["top"]
    cls = np.zeros(n_seeds, dtype=np.uint8)
    cls[resolved & from_top & to_top] = CLS["hyporheic"]
    cls[resolved & from_top & ~to_top] = CLS["losing"]
    cls[resolved & ~from_top & to_top] = CLS["gaining"]
    cls[resolved & ~from_top & ~to_top] = CLS["throughflow"]

    return {"cls": cls,
            "origin": origin_code.astype(np.uint8),
            "exit": exit_code.astype(np.uint8),
            "status_fwd": status_f, "status_bwd": status_b,
            "fwd_time": np.asarray(ep_fwd["time"], dtype=np.float32),
            "bwd_time": np.asarray(ep_bwd["time"], dtype=np.float32),
            "seed_node": np.asarray(ep_fwd["node0"], dtype=np.int64)}


# ---------------------------------------------------------------------------
# 1e. Fractions, volumes, stats, surfaces, footprints
# ---------------------------------------------------------------------------

def cell_volumes(*, T: np.ndarray, B: np.ndarray, delr: np.ndarray, delc: np.ndarray,
                 head: np.ndarray | None = None, saturated_clip: bool = True,
                 ) -> np.ndarray:
    """Cell volumes (nlay, nrow, ncol) in m3; saturated volume when clipping to heads."""
    area = np.multiply.outer(np.asarray(delc, float), np.asarray(delr, float))  # (nrow, ncol)
    if saturated_clip and head is not None:
        h = np.where(np.isnan(head), -np.inf, head)
        thick = np.clip(np.minimum(T, h) - B, 0.0, None)
    else:
        thick = np.clip(T - B, 0.0, None)
    return thick * area[None, :, :]


def cell_class_fractions(cls: np.ndarray, seed_node: np.ndarray,
                         shape: tuple[int, int, int]) -> dict[str, np.ndarray]:
    """Per-cell class fractions; the denominator is CLASSIFIED particles per cell
    (unresolved seeds don't dilute the split — the streamtube rule)."""
    ncell = int(np.prod(shape))
    seed_node = np.asarray(seed_node, dtype=np.int64)
    cls = np.asarray(cls)
    classified = cls != CLS["unresolved"]
    n_classified = np.bincount(seed_node[classified], minlength=ncell).astype(np.float32)
    out: dict[str, np.ndarray] = {}
    with np.errstate(divide="ignore", invalid="ignore"):
        for name in HZ_CLASSES:
            counts = np.bincount(seed_node[cls == CLS[name]], minlength=ncell)
            frac = np.where(n_classified > 0, counts / n_classified, 0.0)
            out[name] = frac.astype(np.float32).reshape(shape)
    out["n_classified"] = n_classified.reshape(shape)
    return out


def class_stats(fracs: dict[str, np.ndarray], volumes: np.ndarray, *,
                xe: np.ndarray, ye: np.ndarray, T: np.ndarray, B: np.ndarray,
                domain_volume_m3: float) -> dict:
    """Per-class volume/extent statistics from the fraction arrays."""
    dx = np.abs(np.diff(np.asarray(xe, float)))          # (ncol,)
    dy = np.abs(np.diff(np.asarray(ye, float)))          # (nrow,)
    col_area = np.multiply.outer(dy, dx)                 # (nrow, ncol)
    out: dict[str, dict] = {}
    for name in HZ_CLASSES:
        frac = fracs[name]
        vol = float(np.sum(frac * volumes))
        M = frac > 0
        st: dict = {"volume_m3": vol, "n_cells": int(M.sum())}
        if M.any():
            plan = M.any(axis=0)                          # (nrow, ncol)
            st["footprint_m2"] = float(col_area[plan].sum())
            kk, ii, jj = np.nonzero(M)
            x0 = np.minimum(xe[jj], xe[jj + 1]).min(); x1 = np.maximum(xe[jj], xe[jj + 1]).max()
            y0 = np.minimum(ye[ii], ye[ii + 1]).min(); y1 = np.maximum(ye[ii], ye[ii + 1]).max()
            z0 = float(B[M].min()); z1 = float(T[M].max())
            st["bbox_m"] = [round(float(x1 - x0), 2), round(float(y1 - y0), 2),
                            round(z1 - z0, 2)]
            thick = np.where(M, T - B, 0.0).sum(axis=0)   # (nrow, ncol)
            occ = thick[thick > 0]
            st["thickness_mean_m"] = round(float(occ.mean()), 3)
            st["thickness_max_m"] = round(float(occ.max()), 3)
        else:
            st.update(footprint_m2=0.0, bbox_m=[0.0, 0.0, 0.0],
                      thickness_mean_m=0.0, thickness_max_m=0.0)
        st["pct_domain_volume"] = round(100.0 * vol / domain_volume_m3, 2) \
            if domain_volume_m3 > 0 else 0.0
        out[name] = st
    return out


def _edges2x(e: np.ndarray, n2: int) -> np.ndarray:
    """Every-other edge vector for a 2x plan coarsening; last edge preserved."""
    e2 = e[0::2]
    if e2.size < n2 + 1:
        e2 = np.concatenate([e2, e[-1:]])
    return e2[: n2 + 1]


def _pool2x(mask: np.ndarray, xe: np.ndarray, ye: np.ndarray,
            T: np.ndarray, B: np.ndarray):
    """Coarsen plan resolution 2x (OR-pool mask; outer-bound T/B) for the face cap."""
    nlay, nrow, ncol = mask.shape
    pr, pc = (-nrow) % 2, (-ncol) % 2

    def _pad(a):
        return np.pad(a, ((0, 0), (0, pr), (0, pc)), mode="edge") if (pr or pc) else a

    m = _pad(mask)
    m = m.reshape(nlay, m.shape[1] // 2, 2, m.shape[2] // 2, 2).any(axis=(2, 4))
    Tp = _pad(T)
    Tp = Tp.reshape(nlay, Tp.shape[1] // 2, 2, Tp.shape[2] // 2, 2).max(axis=(2, 4))
    Bp = _pad(B)
    Bp = Bp.reshape(nlay, Bp.shape[1] // 2, 2, Bp.shape[2] // 2, 2).min(axis=(2, 4))
    return m, _edges2x(xe, m.shape[2]), _edges2x(ye, m.shape[1]), Tp, Bp


def extract_class_surface(mask: np.ndarray, *, xe: np.ndarray, ye: np.ndarray,
                          T: np.ndarray, B: np.ndarray, max_faces: int = 60_000,
                          log: Callable = print) -> dict | None:
    """Exterior quad faces of a boolean cell mask on the structured grid.

    Returns {"points": (P,3) float32 absolute model coords, "quads": (Q,4) uint32}
    or None for an empty mask. Faces: horizontal at T (up-exposed) / B (down-exposed),
    vertical spanning B..T of the owning cell. No point dedup (vtk tolerates shared
    duplicates and it keeps this fully vectorized).
    """
    mask = np.asarray(mask, dtype=bool)
    if not mask.any():
        return None
    xe = np.asarray(xe, float); ye = np.asarray(ye, float)

    pad = np.zeros(tuple(s + 2 for s in mask.shape), dtype=bool)
    pad[1:-1, 1:-1, 1:-1] = mask
    core = pad[1:-1, 1:-1, 1:-1]
    fams = {
        "up":    core & ~pad[:-2, 1:-1, 1:-1],
        "down":  core & ~pad[2:, 1:-1, 1:-1],
        "north": core & ~pad[1:-1, :-2, 1:-1],
        "south": core & ~pad[1:-1, 2:, 1:-1],
        "west":  core & ~pad[1:-1, 1:-1, :-2],
        "east":  core & ~pad[1:-1, 1:-1, 2:],
    }
    n_faces = int(sum(f.sum() for f in fams.values()))
    if n_faces > max_faces:
        m2, xe2, ye2, T2, B2 = _pool2x(mask, xe, ye, T, B)
        log(f"volume surface has {n_faces} faces > cap {max_faces}; "
            f"coarsening plan resolution 2x")
        return extract_class_surface(m2, xe=xe2, ye=ye2, T=T2, B=B2,
                                     max_faces=max_faces, log=log)

    chunks: list[np.ndarray] = []

    def _emit(pts):
        chunks.append(pts.astype(np.float32).reshape(-1, 3))

    for fam, f in fams.items():
        k, i, j = np.nonzero(f)
        if k.size == 0:
            continue
        zt = T[k, i, j]; zb = B[k, i, j]
        x0, x1 = xe[j], xe[j + 1]
        y0, y1 = ye[i], ye[i + 1]
        pts = np.empty((k.size, 4, 3), dtype=np.float64)
        if fam in ("up", "down"):
            z = zt if fam == "up" else zb
            pts[:, 0, 0], pts[:, 0, 1] = x0, y0
            pts[:, 1, 0], pts[:, 1, 1] = x1, y0
            pts[:, 2, 0], pts[:, 2, 1] = x1, y1
            pts[:, 3, 0], pts[:, 3, 1] = x0, y1
            pts[:, :, 2] = z[:, None]
        elif fam in ("west", "east"):
            x = x0 if fam == "west" else x1
            pts[:, 0, 0], pts[:, 0, 1], pts[:, 0, 2] = x, y0, zb
            pts[:, 1, 0], pts[:, 1, 1], pts[:, 1, 2] = x, y1, zb
            pts[:, 2, 0], pts[:, 2, 1], pts[:, 2, 2] = x, y1, zt
            pts[:, 3, 0], pts[:, 3, 1], pts[:, 3, 2] = x, y0, zt
        else:  # north/south: face along constant y
            y = y0 if fam == "north" else y1
            pts[:, 0, 0], pts[:, 0, 1], pts[:, 0, 2] = x0, y, zb
            pts[:, 1, 0], pts[:, 1, 1], pts[:, 1, 2] = x1, y, zb
            pts[:, 2, 0], pts[:, 2, 1], pts[:, 2, 2] = x1, y, zt
            pts[:, 3, 0], pts[:, 3, 1], pts[:, 3, 2] = x0, y, zt
        _emit(pts)

    points = np.concatenate(chunks, axis=0)
    quads = np.arange(points.shape[0], dtype=np.uint32).reshape(-1, 4)
    return {"points": points, "quads": quads}


def class_footprint_gdf(mask2d: np.ndarray, *, xe: np.ndarray, ye: np.ndarray, crs):
    """Dissolved plan-view footprint polygons of a 2-D column mask (model CRS).

    Built cell-by-cell from the FULL edge arrays — xe: ncol+1 west->east; ye: nrow+1 in the
    MASK's row order (the engine's rows are SOUTH-first, so pass south->north edges — see
    run_hz_analysis). Direction-agnostic and non-uniform-safe: cell (i,j) spans
    [xe[j],xe[j+1]] x [ye[i],ye[i+1]] exactly, mirroring extract_class_surface's face
    placement, so the footprint matches the 3-D shell and the classed pathlines."""
    import geopandas as gpd
    import shapely

    mask2d = np.asarray(mask2d, dtype=bool)
    if not mask2d.any():
        return gpd.GeoDataFrame(geometry=[], crs=crs)
    xe = np.asarray(xe, dtype=float); ye = np.asarray(ye, dtype=float)
    ii, jj = np.nonzero(mask2d)
    x0 = xe[jj]; x1 = xe[jj + 1]; y0 = ye[ii]; y1 = ye[ii + 1]
    rings = np.empty((ii.size, 5, 2), dtype=float)          # closed rings NW,NE,SE,SW,NW
    rings[:, 0, 0], rings[:, 0, 1] = x0, y0
    rings[:, 1, 0], rings[:, 1, 1] = x1, y0
    rings[:, 2, 0], rings[:, 2, 1] = x1, y1
    rings[:, 3, 0], rings[:, 3, 1] = x0, y1
    rings[:, 4, 0], rings[:, 4, 1] = x0, y0
    merged = shapely.unary_union(shapely.polygons(rings))
    return gpd.GeoDataFrame(geometry=[merged], crs=crs).explode(
        index_parts=False).reset_index(drop=True)


# ---------------------------------------------------------------------------
# 1f. Display pathlines
# ---------------------------------------------------------------------------

def sample_display_seeds(cls: np.ndarray, *, per_class: int = 300,
                         unresolved_sample: int = 50, seed: int = 0) -> np.ndarray:
    """Stratified seed indices: up to per_class per real class + a small unresolved
    diagnostic sample. Returns sorted indices into the seed arrays."""
    rng = np.random.default_rng(seed)
    picks: list[np.ndarray] = []
    for name in HZ_CLASSES:
        idx = np.flatnonzero(cls == CLS[name])
        if idx.size > per_class:
            idx = rng.choice(idx, size=per_class, replace=False)
        picks.append(idx)
    un = np.flatnonzero(cls == CLS["unresolved"])
    if un.size > unresolved_sample:
        un = rng.choice(un, size=unresolved_sample, replace=False)
    picks.append(un)
    return np.sort(np.concatenate(picks)).astype(np.int64)


def _pathlines_by_pid(pl_path: Path) -> dict[int, np.ndarray]:
    """particleid (zero-based) -> pathline recarray sorted by time."""
    plf = PathlineFile(str(pl_path))
    out: dict[int, np.ndarray] = {}
    for rec in plf.get_alldata():
        rec = np.asarray(rec)
        if rec.size == 0:
            continue
        pid = int(rec["particleid"][0])
        out[pid] = rec[np.argsort(rec["time"], kind="stable")]
    return out


def build_display_paths(pl_fwd: Path, pl_bwd: Path, sample: dict, classes: dict,
                        gwf, crs, log: Callable = print):
    """Join backward (reversed) + forward pathlines per sampled particle into one
    entry->seed->exit LineString Z GeoDataFrame in the model CRS."""
    import geopandas as gpd
    from shapely.geometry import LineString

    fwd = _pathlines_by_pid(pl_fwd)
    bwd = _pathlines_by_pid(pl_bwd)

    mg = gwf.modelgrid
    xorigin = float(getattr(mg, "xoffset", 0.0) or 0.0)
    yorigin = float(getattr(mg, "yoffset", 0.0) or 0.0)
    total_y = float(np.sum(np.asarray(gwf.dis.delc.array, dtype=float)))

    def _world(rec) -> np.ndarray:
        x = np.asarray(rec["x"], float)
        y = np.asarray(rec["y"], float)
        z = np.asarray(rec["z"], float)
        return np.column_stack([xorigin + x, yorigin + (total_y - y), z])

    orig_pid = np.asarray(sample["orig_pid"], dtype=np.int64)
    cls = classes["cls"]; origin = classes["origin"]; exit_ = classes["exit"]
    ft = classes["fwd_time"]; bt = classes["bwd_time"]

    rows = []
    missing = 0
    for m in range(sample["n_seeds"]):
        f = fwd.get(m); b = bwd.get(m)
        if f is None and b is None:
            missing += 1
            continue
        parts = []
        if b is not None and len(b) > 1:
            parts.append(_world(b)[::-1])            # origin -> seed
        if f is not None and len(f) > 0:
            w = _world(f)
            parts.append(w[1:] if parts else w)       # seed -> exit (drop dup seed)
        if not parts:
            continue
        verts = np.concatenate(parts, axis=0) if len(parts) > 1 else parts[0]
        if verts.shape[0] < 2:
            continue
        seg = np.diff(verts, axis=0)
        length = float(np.sqrt((seg ** 2).sum(axis=1)).sum())
        op = int(orig_pid[m]) - 1                     # zero-based index into classes
        rows.append({
            "particleid": int(orig_pid[m]),
            "hz_class": CLS_NAMES[int(cls[op])],
            "origin_side": MEMBER_NAMES[int(origin[op])],
            "exit_side": MEMBER_NAMES[int(exit_[op])],
            "bwd_time_d": round(float(bt[op]), 3),
            "fwd_time_d": round(float(ft[op]), 3),
            "total_time_d": round(float(bt[op]) + float(ft[op]), 3),
            "length_m": round(length, 2),
            "geometry": LineString(verts),
        })
    if missing:
        log(f"display pathlines: {missing} sampled particles had no pathline records")
    return gpd.GeoDataFrame(rows, geometry="geometry", crs=crs)


# ---------------------------------------------------------------------------
# 1g. Orchestrator
# ---------------------------------------------------------------------------

def _fmt_int(n: float) -> str:
    return f"{int(n):,}"


def run_hz_analysis(work_dir: str | Path, *, crs,
                    left_line, right_line, up_line, down_line,
                    particles_per_cell: int = 1, sample_per_class: int = 300,
                    max_time_days: float | None = 1.0e6, saturated_clip: bool = True,
                    classes_for_volume: tuple[str, ...] = HZ_CLASSES,
                    porosity: float = 0.3, gwf_name: str = "gwf_model",
                    modflow_bin_dir: str | None = None,
                    hard_cap_particles: int = 2_000_000,
                    min_sat_frac: float = 0.05,
                    keep_raw_outputs: bool = False,
                    log: Callable = print) -> dict:
    """Full hyporheic-zone analysis on a completed run directory. Returns
    {"hz_dir": str, "stats": dict} — all artifacts live under summary/hz/."""
    t_all = time.monotonic()
    work_dir = Path(work_dir)
    gwf_ws = work_dir / "model" / "gwf_workspace"
    hz_ws = work_dir / "model" / "hz_workspace"
    hz_dir = work_dir / "summary" / "hz"
    if hz_ws.exists():
        shutil.rmtree(hz_ws, ignore_errors=True)
    if hz_dir.exists():
        shutil.rmtree(hz_dir, ignore_errors=True)
    hz_dir.mkdir(parents=True, exist_ok=True)
    exe = resolve_mp7_exe(modflow_bin_dir)
    runtimes: dict[str, float] = {}

    # ---- STEP 1: load ------------------------------------------------------
    log("HZ STEP 1/7 - Loading the groundwater flow solution")
    t0 = time.monotonic()
    _sim, gwf = load_flow_model(gwf_ws, gwf_name)
    dis = gwf.get_package("DIS")
    top2d = np.asarray(dis.top.array, dtype=float)
    botm3d = np.asarray(dis.botm.array, dtype=float)
    idomain = np.asarray(dis.idomain.array, dtype=int)
    delr = np.asarray(dis.delr.array, dtype=float)
    delc = np.asarray(dis.delc.array, dtype=float)
    nlay, nrow, ncol = botm3d.shape
    T, B = cell_geometry(top2d, botm3d)
    head = load_heads(gwf)
    member, member_info = extract_bc_membership(
        gwf, left_line, right_line, up_line, down_line, log=log)
    runtimes["load"] = round(time.monotonic() - t0, 2)

    # ---- STEP 2: seed ------------------------------------------------------
    log("HZ STEP 2/7 - Seeding particles in every active cell")
    seeds = build_seed_arrays(idomain=idomain, member=member, head=head, T=T, B=B,
                              particles_per_cell=particles_per_cell,
                              min_sat_frac=min_sat_frac)
    n = seeds["n_seeds"]
    log(f"Seeded {_fmt_int(n)} particles in {_fmt_int(seeds['n_cells_seeded'])} cells "
        f"({particles_per_cell}/cell requested)")
    if n > hard_cap_particles:
        raise RuntimeError(
            f"{_fmt_int(n)} particles exceeds the cap of {_fmt_int(hard_cap_particles)} "
            f"— use fewer particles per cell or a coarser grid.")

    # ---- STEPS 3-4: endpoint runs -----------------------------------------
    ep_rec: dict[str, np.ndarray] = {}
    for step, direction in ((3, "forward"), (4, "backward")):
        log(f"HZ STEP {step}/7 - MODPATH endpoints ({direction}, {_fmt_int(n)} particles)")
        t0 = time.monotonic()
        mp = build_hz_mp7_sim(gwf, seeds, mp7_ws=hz_ws, exe=exe, direction=direction,
                              mode="endpoint", porosity=porosity,
                              max_time_days=max_time_days)
        run_mp7(mp, log=log)
        ep_path = hz_ws / f"{mp.name}.mpend"
        ep_rec[direction] = np.asarray(EndpointFile(str(ep_path)).get_alldata())
        runtimes[f"endpoint_{direction}"] = round(time.monotonic() - t0, 2)
        if not keep_raw_outputs:
            for f in (ep_path, hz_ws / f"{mp.name}.sloc"):
                f.unlink(missing_ok=True)

    # ---- STEP 5: classify + volumes ---------------------------------------
    log("HZ STEP 5/7 - Classifying particles and delineating zone volumes")
    t0 = time.monotonic()
    classes = classify_particles(ep_rec["forward"], ep_rec["backward"], member, n)
    cls = classes["cls"]
    counts_by_class = {name: int((cls == code).sum()) for name, code in CLS.items()}
    n_classified = n - counts_by_class["unresolved"]
    log("Classified " + ", ".join(f"{k}: {_fmt_int(v)}" for k, v in counts_by_class.items()))

    fracs = cell_class_fractions(cls, classes["seed_node"], (nlay, nrow, ncol))
    volumes = cell_volumes(T=T, B=B, delr=delr, delc=delc, head=head,
                           saturated_clip=saturated_clip)
    active = idomain == 1
    domain_volume = float(volumes[active].sum())
    # Grid edge coordinates in ENGINE row order (row 0 = SOUTH — the engine fills DIS arrays
    # south-first, verified against the terrain raster: top[0] matches the south edge). flopy's
    # mg.yvertices is north-first and would mirror every footprint/volume/bbox N↔S — the
    # "volumes misaligned from the domain" bug. Build the edges from delr/delc + offsets so
    # index i maps straight onto the classification arrays.
    xe = float(gwf.modelgrid.xoffset) + np.concatenate(([0.0], np.cumsum(delr)))
    ye = float(gwf.modelgrid.yoffset) + np.concatenate(([0.0], np.cumsum(delc)))
    stats_by_class = class_stats(fracs, volumes, xe=xe, ye=ye, T=T, B=B,
                                 domain_volume_m3=domain_volume)

    surfaces: dict[str, dict] = {}
    footprints: dict[str, object] = {}
    for name in HZ_CLASSES:
        if name not in classes_for_volume:
            continue
        M = fracs[name] > 0
        surf = extract_class_surface(M, xe=xe, ye=ye, T=T, B=B, log=log)
        if surf is not None:
            surfaces[name] = surf
        fp = class_footprint_gdf(M.any(axis=0), xe=xe, ye=ye, crs=crs)
        if len(fp):
            footprints[name] = fp
    runtimes["classify"] = round(time.monotonic() - t0, 2)

    # ---- STEP 6: display pathlines -----------------------------------------
    log("HZ STEP 6/7 - Tracing display pathlines (stratified sample)")
    t0 = time.monotonic()
    sample_idx = sample_display_seeds(cls, per_class=int(sample_per_class))
    sample = subset_seeds(seeds, sample_idx)
    log(f"Sampled {_fmt_int(sample['n_seeds'])} paths for display "
        f"(cap {sample_per_class}/class)")
    pl_paths: dict[str, Path] = {}
    for direction in ("forward", "backward"):
        mp = build_hz_mp7_sim(gwf, sample, mp7_ws=hz_ws, exe=exe, direction=direction,
                              mode="pathline", porosity=porosity,
                              max_time_days=max_time_days,
                              name=f"hz_pl_{direction[:3]}")
        run_mp7(mp, log=log)
        pl_paths[direction] = hz_ws / f"{mp.name}.mppth"
    paths_gdf = build_display_paths(pl_paths["forward"], pl_paths["backward"],
                                    sample, classes, gwf, crs, log=log)
    runtimes["pathlines"] = round(time.monotonic() - t0, 2)
    if not keep_raw_outputs:
        for p in pl_paths.values():
            p.unlink(missing_ok=True)

    # ---- STEP 7: export -----------------------------------------------------
    log("HZ STEP 7/7 - Writing artifacts")
    t0 = time.monotonic()
    artifacts: dict[str, str] = {}

    np.savez_compressed(hz_dir / "hz_classification.npz",
                        pid=seeds["particleids"], seed_node=classes["seed_node"],
                        cls=cls, origin=classes["origin"], exit=classes["exit"],
                        status_fwd=classes["status_fwd"], status_bwd=classes["status_bwd"],
                        fwd_time=classes["fwd_time"], bwd_time=classes["bwd_time"])
    artifacts["classification"] = "hz_classification.npz"
    np.savez_compressed(hz_dir / "hz_cell_fractions.npz",
                        n_classified=fracs["n_classified"],
                        **{f"frac_{k}": fracs[k] for k in HZ_CLASSES})
    artifacts["fractions"] = "hz_cell_fractions.npz"

    total_time = classes["bwd_time"].astype(float) + classes["fwd_time"].astype(float)
    for name in HZ_CLASSES:
        sel = cls == CLS[name]
        st = stats_by_class[name]
        st["n_particles"] = counts_by_class[name]
        st["pct_of_classified"] = round(100.0 * counts_by_class[name] / n_classified, 2) \
            if n_classified else 0.0
        if sel.any():
            tt = total_time[sel]
            st["residence_time_days"] = {
                "mean": round(float(tt.mean()), 3), "median": round(float(np.median(tt)), 3),
                "min": round(float(tt.min()), 4), "max": round(float(tt.max()), 2)}
        cg = paths_gdf[paths_gdf["hz_class"] == name] if len(paths_gdf) else paths_gdf
        st["displayed_paths"] = int(len(cg))
        if len(cg):
            st["length_m"] = {"mean": round(float(cg["length_m"].mean()), 2),
                              "median": round(float(cg["length_m"].median()), 2)}
            gj_name = f"hz_paths_{name}_2d.geojson"
            g2 = cg.to_crs(4326)
            from shapely import force_2d as _force_2d
            g2["geometry"] = _force_2d(g2.geometry.values)
            g2.to_file(hz_dir / gj_name, driver="GeoJSON")
            st["paths_2d"] = gj_name
            artifacts[f"paths_{name}"] = gj_name
        if name in footprints:
            fp_name = f"hz_foot_{name}.geojson"
            footprints[name].to_crs(4326).to_file(hz_dir / fp_name, driver="GeoJSON")
            st["footprint"] = fp_name
            artifacts[f"foot_{name}"] = fp_name
        if name in surfaces:
            npz_name = f"hz_vol_{name}.npz"
            np.savez_compressed(hz_dir / npz_name,
                                points=surfaces[name]["points"],
                                quads=surfaces[name]["quads"])
            st["volume_npz"] = npz_name
            st["n_surface_faces"] = int(surfaces[name]["quads"].shape[0])
            artifacts[f"vol_{name}"] = npz_name

    if len(paths_gdf):
        paths_gdf.to_file(hz_dir / "hz_paths_3d.gpkg", driver="GPKG",
                          layer="hz_paths_3d")
        artifacts["paths_3d"] = "hz_paths_3d.gpkg"

    origin_tally = {MEMBER_NAMES[c]: int((classes["origin"] == c).sum())
                    for c in sorted(MEMBER_NAMES)}
    exit_tally = {MEMBER_NAMES[c]: int((classes["exit"] == c).sum())
                  for c in sorted(MEMBER_NAMES)}
    stats = {
        "version": 1,
        "knobs": {"particles_per_cell": int(particles_per_cell),
                  "sample_per_class": int(sample_per_class),
                  "max_time_days": max_time_days, "porosity": float(porosity),
                  "saturated_clip": bool(saturated_clip),
                  "min_sat_frac": float(min_sat_frac)},
        "grid": {"nlay": int(nlay), "nrow": int(nrow), "ncol": int(ncol)},
        "counts": {"n_seed_cells": seeds["n_cells_seeded"], "n_seeds": int(n),
                   "n_classified": int(n_classified),
                   "by_class": counts_by_class,
                   "origin_sides": origin_tally, "exit_sides": exit_tally,
                   "boundary_cells": member_info["counts"]},
        "domain": {"active_saturated_volume_m3": round(domain_volume, 1)},
        "classes": stats_by_class,
        "runtimes_s": runtimes,
        "artifacts": artifacts,
    }
    (hz_dir / "hz_stats.json").write_text(json.dumps(stats, indent=2))
    (hz_dir / "hz_stats.txt").write_text(_stats_text(stats))
    runtimes["export"] = round(time.monotonic() - t0, 2)
    stats["runtimes_s"]["total"] = round(time.monotonic() - t_all, 2)
    (hz_dir / "hz_stats.json").write_text(json.dumps(stats, indent=2))

    if not keep_raw_outputs:
        shutil.rmtree(hz_ws, ignore_errors=True)
    log(f"Hyporheic-zone analysis complete in {stats['runtimes_s']['total']:.1f} s — "
        f"hyporheic volume {stats_by_class['hyporheic']['volume_m3']:.1f} m3")
    return {"hz_dir": str(hz_dir), "stats": stats}


def _stats_text(stats: dict) -> str:
    """Human-readable mirror of hz_stats.json."""
    c = stats["counts"]
    lines = [
        "HYPORHEIC ZONE DELINEATION - SUMMARY",
        "=" * 44,
        f"Particles seeded : {c['n_seeds']:,} in {c['n_seed_cells']:,} cells "
        f"({stats['knobs']['particles_per_cell']}/cell)",
        f"Classified       : {c['n_classified']:,} "
        f"({100.0 * c['n_classified'] / max(c['n_seeds'], 1):.1f}%)",
        "",
    ]
    for name in HZ_CLASSES:
        st = stats["classes"][name]
        lines.append(f"[{name.upper()}]")
        lines.append(f"  particles       : {st.get('n_particles', 0):,} "
                     f"({st.get('pct_of_classified', 0)}% of classified)")
        lines.append(f"  volume          : {st['volume_m3']:.1f} m3 "
                     f"({st['pct_domain_volume']}% of domain)")
        lines.append(f"  footprint       : {st['footprint_m2']:.1f} m2")
        bbox = st.get("bbox_m", [0, 0, 0])
        lines.append(f"  bounding box    : {bbox[0]} x {bbox[1]} x {bbox[2]} m")
        lines.append(f"  thickness       : mean {st.get('thickness_mean_m', 0)} m, "
                     f"max {st.get('thickness_max_m', 0)} m")
        rt = st.get("residence_time_days")
        if rt:
            lines.append(f"  residence time  : mean {rt['mean']} d, median {rt['median']} d, "
                         f"max {rt['max']} d")
        lines.append("")
    lines.append(f"Active saturated domain volume: "
                 f"{stats['domain']['active_saturated_volume_m3']:.1f} m3")
    return "\n".join(lines)
