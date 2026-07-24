"""Pure-numpy transforms between hype's engine grid order and GMS's.

The engine builds DIS arrays SOUTH-first (row 0 = ymin; my_utils.build_model_domain),
the opposite of the MODFLOW/GMS convention (row 0 = north). Every per-cell array,
node id, face-flow array and pathline record must therefore be flipped when writing
GMS files. The one non-trivial case is FLOW FRONT FACE: its sign convention is
"positive toward increasing row index", so a row flip both relabels AND negates it,
with a one-face index shift (the face between engine rows i,i+1 is the face between
GMS rows nrow-2-i, nrow-1-i).

Conventions (locked by the decoded example project):
  - GMS I axis = rows (NumI = nrow), J axis = columns (NumJ = ncol).
  - .mfs `IJK -y +x -z`: rows run southward from the origin, columns eastward, so
    ORIG = the grid's NORTH-WEST corner (xmin, ymax). hype grids are unrotated.
  - ORIG z = the deepest layer bottom (example: bot20); CoordsK are layer INDICES.
  - GMS node ids are 1-based layer-major with north-first rows.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class GmsGrid:
    """GMS-order geometry derived from an engine-order model."""
    nlay: int
    nrow: int
    ncol: int
    delr: np.ndarray            # (ncol,) column widths, metres
    delc: np.ndarray            # (nrow,) row heights, metres
    origin: tuple[float, float, float]   # NW corner (xmin, ymax, z_orig)
    coords_i: np.ndarray        # (nrow+1,) cumulative row gridlines from the origin
    coords_j: np.ndarray        # (ncol+1,) cumulative column gridlines
    coords_k: np.ndarray        # (nlay+1,) layer indices 0..nlay
    total_y: float              # sum(delc)

    @property
    def ncells(self) -> int:
        return self.nlay * self.nrow * self.ncol

    @property
    def ncells_layer(self) -> int:
        return self.nrow * self.ncol


def gms_grid_from(nlay: int, nrow: int, ncol: int, delr, delc,
                  xmin: float, ymin: float, botm) -> GmsGrid:
    delr = np.asarray(delr, dtype=float).reshape(ncol)
    delc = np.asarray(delc, dtype=float).reshape(nrow)
    total_y = float(delc.sum())
    z_orig = float(np.min(np.asarray(botm)[-1]))
    coords_i = np.concatenate([[0.0], np.cumsum(delc)])
    coords_j = np.concatenate([[0.0], np.cumsum(delr)])
    coords_k = np.arange(nlay + 1, dtype=float)
    return GmsGrid(nlay=nlay, nrow=nrow, ncol=ncol, delr=delr, delc=delc,
                   origin=(float(xmin), float(ymin) + total_y, z_orig),
                   coords_i=coords_i, coords_j=coords_j, coords_k=coords_k,
                   total_y=total_y)


def flip_cc(a: np.ndarray) -> np.ndarray:
    """Flip any cell-centered array's row axis (second-to-last): south-first <-> north-first."""
    return np.ascontiguousarray(np.asarray(a)[..., ::-1, :])


def flip_frf(frf_e: np.ndarray) -> np.ndarray:
    return flip_cc(frf_e)           # a j-face quantity: the row flip only relabels rows


def flip_flf(flf_e: np.ndarray) -> np.ndarray:
    return flip_cc(flf_e)           # a k-face quantity: same


def flip_fff(fff_e: np.ndarray, atol: float = 1e-6) -> np.ndarray:
    """Row-flip FLOW FRONT FACE: permute faces AND negate.

    Engine FFF[k,i,j] = flow from engine row i into i+1 (northward, since engine rows
    run south->north). That physical face separates GMS rows nrow-2-i and nrow-1-i,
    and GMS-positive means flow toward increasing GMS row (southward), hence the sign
    flip. The last engine row has no outward face; it must be structurally zero.
    """
    fff_e = np.asarray(fff_e)
    nrow = fff_e.shape[-2]
    last = fff_e[..., -1, :]
    if nrow > 1 and not np.allclose(last, 0.0, atol=atol):
        raise ValueError("engine FFF has nonzero flow across the grid's last row "
                         f"(max |q| = {np.abs(last).max():g}); the .grb/flowja pair "
                         "does not describe a structured DIS grid as expected")
    out = np.zeros_like(fff_e)
    if nrow > 1:
        out[..., :-1, :] = -fff_e[..., nrow - 2::-1, :]
    return out


def eng_node0_to_gms_node1(node0, nlay: int, nrow: int, ncol: int) -> np.ndarray:
    """0-based engine (south-first) layer-major node -> 1-based GMS (north-first) node."""
    node0 = np.asarray(node0, dtype=np.int64)
    per_layer = nrow * ncol
    if node0.size and (node0.min() < 0 or node0.max() >= nlay * per_layer):
        raise ValueError("engine node id out of range for the grid")
    k, rem = np.divmod(node0, per_layer)
    i_e, j = np.divmod(rem, ncol)
    return (k * per_layer + (nrow - 1 - i_e) * ncol + j + 1).astype(np.int64)


def gms_node1_to_kij_e(node1, nlay: int, nrow: int, ncol: int):
    """Inverse of eng_node0_to_gms_node1, returning 0-based engine (k, i_e, j)."""
    node0 = np.asarray(node1, dtype=np.int64) - 1
    per_layer = nrow * ncol
    if node0.size and (node0.min() < 0 or node0.max() >= nlay * per_layer):
        raise ValueError("GMS node id out of range for the grid")
    k, rem = np.divmod(node0, per_layer)
    i_g, j = np.divmod(rem, ncol)
    return k, nrow - 1 - i_g, j
