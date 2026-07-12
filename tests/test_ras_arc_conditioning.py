"""Sub-cell boundary-arc vertices crash the RAS 2025-alpha solver (silent exit -1 at
solver init) because the mesher pins a mesh node at every arc vertex. dedupe_arc_vertices
must drop interior vertices closer than ~cell/2 to their kept neighbor while preserving
the snapped corner endpoints exactly (live failure: Mink Brook floodplain lines carrying
0.68 m / 1.02 m segments at a 10 m mesh)."""
import numpy as np

from hype_app.ras import dedupe_arc_vertices


def test_drops_subcell_interior_vertex_near_endpoint():
    # the real failing left-bank line: endpoint, interior vertex 0.68 m away, far endpoint
    xy = np.array([[0.0, 0.0], [0.68, 0.0], [944.0, 0.0]])
    out = dedupe_arc_vertices(xy, 10.0)
    assert out.tolist() == [[0.0, 0.0], [944.0, 0.0]]


def test_drops_interior_vertex_crowding_far_endpoint():
    xy = np.array([[0.0, 0.0], [500.0, 3.0], [999.4, 0.1], [1000.0, 0.0]])
    out = dedupe_arc_vertices(xy, 10.0)
    assert out.tolist() == [[0.0, 0.0], [500.0, 3.0], [1000.0, 0.0]]


def test_keeps_well_spaced_vertices_verbatim():
    xy = np.array([[0.0, 0.0], [90.0, 5.0], [186.0, -2.0], [300.0, 0.0]])
    out = dedupe_arc_vertices(xy, 20.0)
    assert np.array_equal(out, xy)


def test_endpoints_survive_even_if_everything_else_drops():
    xy = np.array([[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0], [100.0, 0.0]])
    out = dedupe_arc_vertices(xy, 20.0)
    assert out.tolist() == [[0.0, 0.0], [100.0, 0.0]]


def test_two_point_lines_pass_through():
    xy = np.array([[0.0, 0.0], [74.0, 1.0]])
    assert np.array_equal(dedupe_arc_vertices(xy, 10.0), xy)


def test_chain_spacing_is_cumulative_from_last_kept():
    # vertices every 3 m at cell 10 (min spacing 5): keeps every OTHER one (6 m apart)
    xy = np.array([[float(x), 0.0] for x in range(0, 31, 3)])
    out = dedupe_arc_vertices(xy, 10.0)
    diffs = np.diff(out[:, 0])
    assert (diffs[:-1] >= 5.0).all()
    assert out[0].tolist() == [0.0, 0.0] and out[-1].tolist() == [30.0, 0.0]
