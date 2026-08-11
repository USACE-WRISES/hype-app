"""guard_chd_bottoms: no CHD row may carry a head below its cell bottom.

MF6 aborts while reading gwf_model.chd on head < botm. Side boundaries are
botm-filtered in compile_chd_data, but river cells are placed at each column's
top ACTIVE layer, and at wetted-edge cells the RAS water surface can sit a few
cm below the aggregated bed (LL01096: 2 of ~4,000 cells, deficits 3.2/4.2 cm).
The guard runs at the scenario() choke point, over both lists together.
"""
from __future__ import annotations

import numpy as np
import pytest

from hypetool.functions.my_utils import _CHD_MIN_HEAD_ABOVE_BOT, guard_chd_bottoms

EPS = _CHD_MIN_HEAD_ABOVE_BOT


def grid(nlay=3, nrow=1, ncol=1, bottoms=(1.0, 0.5, 0.0)):
    """(botm, idomain) for a uniform column stack."""
    botm = np.zeros((nlay, nrow, ncol))
    for k, b in enumerate(bottoms):
        botm[k, :, :] = b
    return botm, np.ones((nlay, nrow, ncol), dtype=int)


def test_fringe_cell_moves_down_one_layer():
    """The LL01096 geometry in miniature: bed just above the layer-0 bottom, stage
    just below it. The row must land in layer 1 with the head unchanged."""
    botm, idm = grid(bottoms=(1.0, 0.5, 0.0))
    river = [(0, 0, 0, 1.0 - 0.032)]  # head 3.2 cm below the layer-0 bottom
    rc, cd, moved, dropped = guard_chd_bottoms(river, [], botm, idm, 3)
    assert rc == [(1, 0, 0, pytest.approx(0.968))]
    assert cd == []
    assert (moved, dropped) == (1, 0)


def test_head_below_whole_column_drops():
    botm, idm = grid(bottoms=(1.0, 0.5, 0.0))
    river = [(0, 0, 0, -0.5)]
    rc, cd, moved, dropped = guard_chd_bottoms(river, [], botm, idm, 3)
    assert rc == []
    assert (moved, dropped) == (0, 1)


def test_target_layer_collision_drops_without_duplicating():
    """The layer the row would move to is already occupied: drop, never duplicate."""
    botm, idm = grid(bottoms=(1.0, 0.5, 0.0))
    occupant = (1, 0, 0, 0.9)
    failing = (0, 0, 0, 0.968)  # would move to layer 1, which is taken
    rc, cd, moved, dropped = guard_chd_bottoms([occupant, failing], [], botm, idm, 3)
    assert rc == [occupant]
    assert (moved, dropped) == (0, 1)


def test_clean_rows_pass_through_bit_identical():
    botm, idm = grid(bottoms=(1.0, 0.5, 0.0))
    river = [(0, 0, 0, 1.5), (1, 0, 0, 0.9)]
    chd = [[2, 0, 0, 0.4], [0, 0, 0, 2.0]]
    rc, cd, moved, dropped = guard_chd_bottoms(river, chd, botm, idm, 3)
    assert rc == river and all(isinstance(r, tuple) for r in rc)
    assert cd == chd and all(isinstance(r, list) for r in cd)
    assert (moved, dropped) == (0, 0)


def test_both_lists_repaired_and_inactive_layers_skipped():
    """A river row and its chd_data twin repair consistently, and an inactive
    middle layer is skipped in the scan."""
    botm, idm = grid(nlay=3, nrow=1, ncol=2, bottoms=(1.0, 0.5, 0.0))
    idm[1, 0, 1] = 0                      # layer 1 inactive in column i=1
    river = [(0, 0, 1, 0.968)]            # must skip layer 1, land in layer 2
    chd = [[0, 0, 0, 0.968]]              # twin column i=0, layer 1 active
    rc, cd, moved, dropped = guard_chd_bottoms(river, chd, botm, idm, 3)
    assert rc == [(2, 0, 1, pytest.approx(0.968))]
    assert cd == [[1, 0, 0, pytest.approx(0.968)]]
    assert (moved, dropped) == (2, 0)


def test_epsilon_boundary():
    """Clearly above bottom + eps stays. Inside the eps band moves.

    The exact knife-edge (head == bottom + eps) is float-representation luck,
    so the pinned contract is direction on either side of the band.
    """
    botm, idm = grid(bottoms=(1.0, 0.5, 0.0))
    stay = (0, 0, 0, 1.0 + 2.0 * EPS)
    rc, _, moved, dropped = guard_chd_bottoms([stay], [], botm, idm, 3)
    assert rc == [stay] and (moved, dropped) == (0, 0)

    move = (0, 0, 0, 1.0 + EPS / 2.0)
    rc, _, moved, dropped = guard_chd_bottoms([move], [], botm, idm, 3)
    assert rc == [(1, 0, 0, pytest.approx(1.0 + EPS / 2.0))]
    assert (moved, dropped) == (1, 0)


def test_no_idomain_means_all_layers_eligible():
    botm, _ = grid(bottoms=(1.0, 0.5, 0.0))
    river = [(0, 0, 0, 0.968)]
    rc, _, moved, dropped = guard_chd_bottoms(river, [], botm, None, 3)
    assert rc == [(1, 0, 0, pytest.approx(0.968))]
    assert (moved, dropped) == (1, 0)


def test_summary_line_only_when_something_happened():
    botm, idm = grid(bottoms=(1.0, 0.5, 0.0))
    lines = []
    guard_chd_bottoms([(0, 0, 0, 1.5)], [], botm, idm, 3, log=lines.append)
    assert lines == []
    guard_chd_bottoms([(0, 0, 0, 0.968)], [], botm, idm, 3, log=lines.append)
    assert len(lines) == 1 and "moved 1" in lines[0] and "dropped 0" in lines[0]
