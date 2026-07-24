"""The GMS row-flip transforms (hype_app/gms/grid.py).

The divergence-identity test is the load-bearing one: it pins the FLOW FRONT FACE
sign-and-shift with pure permutation math, no groundwater physics in the loop. If it
holds, flipping arrays and flipping flows commute, so the GMS-order budget balances
exactly where the engine-order budget did.
"""
from __future__ import annotations

import numpy as np
import pytest

from hype_app.gms import grid as g

RNG = np.random.default_rng(20260722)


def _random_faceflows(nlay=3, nrow=5, ncol=4):
    frf = RNG.normal(size=(nlay, nrow, ncol))
    fff = RNG.normal(size=(nlay, nrow, ncol))
    flf = RNG.normal(size=(nlay, nrow, ncol))
    frf[:, :, -1] = 0.0         # no faces beyond the grid
    fff[:, -1, :] = 0.0
    flf[-1, :, :] = 0.0
    return frf, fff, flf


def _divergence(frf, fff, flf):
    """Net outflow per cell from face flows (positive-toward-increasing-index)."""
    def shifted(a, axis):
        pad = [(0, 0)] * a.ndim
        pad[axis] = (1, 0)
        return np.pad(a, pad)[tuple(slice(None, -1) if ax == axis else slice(None)
                                    for ax in range(a.ndim))]
    return (shifted(frf, 2) - frf + shifted(fff, 1) - fff + shifted(flf, 0) - flf)


def test_divergence_identity_under_flip():
    frf, fff, flf = _random_faceflows()
    d_eng = _divergence(frf, fff, flf)
    d_gms = _divergence(g.flip_frf(frf), g.flip_fff(fff), g.flip_flf(flf))
    # not bitwise: the summation interleaves terms in a different order, so IEEE
    # rounding differs by ~1 ulp; a sign/shift bug would be O(1), not 1e-15
    np.testing.assert_allclose(d_gms, g.flip_cc(d_eng), rtol=1e-12, atol=1e-12)


def test_flips_are_involutions():
    frf, fff, flf = _random_faceflows()
    np.testing.assert_array_equal(g.flip_cc(g.flip_cc(frf)), frf)
    np.testing.assert_array_equal(g.flip_fff(g.flip_fff(fff)), fff)


def test_flip_fff_rejects_flow_past_last_row():
    fff = np.zeros((2, 4, 3))
    fff[0, -1, 1] = 1.0
    with pytest.raises(ValueError, match="last row"):
        g.flip_fff(fff)


def test_flip_fff_single_row_grid():
    fff = RNG.normal(size=(2, 1, 4))
    np.testing.assert_array_equal(g.flip_fff(np.zeros_like(fff)), np.zeros_like(fff))


def test_node_remap_roundtrip():
    nlay, nrow, ncol = 4, 7, 6
    node0 = RNG.integers(0, nlay * nrow * ncol, size=200)
    node1 = g.eng_node0_to_gms_node1(node0, nlay, nrow, ncol)
    k, i_e, j = g.gms_node1_to_kij_e(node1, nlay, nrow, ncol)
    np.testing.assert_array_equal(k * nrow * ncol + i_e * ncol + j, node0)


def test_node_remap_matches_example_pathline_record():
    # The example .pth's first record: GMS node 14353 in a 20x77x155 grid is
    # (layer 2, row 16, col 93) 1-based north-first.
    nlay, nrow, ncol = 20, 77, 155
    k, i_e, j = g.gms_node1_to_kij_e(np.array([14353]), nlay, nrow, ncol)
    assert (k[0], j[0]) == (1, 92)
    assert i_e[0] == nrow - 1 - 15          # GMS row 16 -> engine row 61
    node1 = g.eng_node0_to_gms_node1(k * nrow * ncol + i_e * ncol + j,
                                     nlay, nrow, ncol)
    assert node1[0] == 14353


def test_node_remap_rejects_out_of_range():
    with pytest.raises(ValueError):
        g.eng_node0_to_gms_node1(np.array([40]), 2, 4, 5)
    with pytest.raises(ValueError):
        g.gms_node1_to_kij_e(np.array([0]), 2, 4, 5)


def test_gms_grid_from_example_constants():
    # LL01096: 20 layers, 77 rows x 155 cols, delc~9.9314 ft, delr~9.9544 ft.
    nlay, nrow, ncol = 20, 77, 155
    delc, delr = 9.9314010442977, 9.9544047588334
    botm = np.full((nlay, nrow, ncol), 1291.63)
    grid = g.gms_grid_from(nlay, nrow, ncol, np.full(ncol, delr),
                           np.full(nrow, delc), xmin=0.0, ymin=0.0, botm=botm)
    assert grid.coords_i.shape == (nrow + 1,)
    assert grid.coords_j.shape == (ncol + 1,)
    assert grid.coords_i[-1] == pytest.approx(764.7178804109216, abs=1e-6)
    assert grid.coords_j[-1] == pytest.approx(1542.9327376191757, abs=1e-6)
    np.testing.assert_array_equal(grid.coords_k, np.arange(nlay + 1, dtype=float))
    # origin = NW corner at the deepest bottom
    assert grid.origin == (0.0, pytest.approx(764.7178804109216, abs=1e-6), 1291.63)
    assert grid.ncells == 238700
