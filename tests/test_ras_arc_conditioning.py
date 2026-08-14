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


# --------------------------------------------------------- conceptual control point
# The mesher's control point must lie INSIDE the 2D area: on a horseshoe reach the mean
# of the four corner nodes falls outside the U and `ras mesh` prints "Failed to build
# conceptual mesh." yet exits 0 (live failure: CH00518, 180-degree bend).

def _topology(up, left, right, down):
    """nodes + arc ring exactly as write_geometry_topology builds them."""
    up, left = np.asarray(up, float), np.asarray(left, float)
    right, down = np.asarray(right, float), np.asarray(down, float)
    nodes = np.vstack([up[-1], up[0], down[0], down[-1]])
    arcs = [up[::-1], left, down, right[::-1]]
    return nodes, arcs


def test_control_point_rectangle_keeps_the_corner_mean():
    from hype_app.ras_h5 import conceptual_control_point

    nodes, arcs = _topology(up=[[0, 10], [10, 10]], left=[[0, 10], [0, 0]],
                            right=[[10, 10], [10, 0]], down=[[0, 0], [10, 0]])
    cp = conceptual_control_point(nodes, arcs)
    assert cp.shape == (1, 2)
    assert np.allclose(cp, np.mean(nodes, axis=0, keepdims=True))


def test_control_point_horseshoe_moves_inside_the_domain():
    from shapely.geometry import Point, Polygon

    from hype_app.ras_h5 import conceptual_control_point

    # U shape: both flow ends at the top, outer boundary wraps the square, inner
    # boundary carves the notch. Corner mean = (5, 10), on the open top edge: outside.
    nodes, arcs = _topology(
        up=[[0, 10], [2, 10]],
        left=[[0, 10], [0, 0], [10, 0], [10, 10]],       # outer bank
        right=[[2, 10], [2, 2], [8, 2], [8, 10]],        # inner bank
        down=[[10, 10], [8, 10]])
    poly = Polygon(np.vstack(arcs))
    assert poly.is_valid
    mean = np.mean(nodes, axis=0)
    assert not poly.contains(Point(mean))                # the failing configuration
    cp = conceptual_control_point(nodes, arcs)
    assert poly.contains(Point(cp[0]))                   # the fix: interior point
