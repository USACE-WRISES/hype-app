"""The upstream/downstream boundaries are the RAS BC lines and must be STRAIGHT: the domain-ring
slicing in delineate could leave bend/jog vertices on the caps, and end-plane clamping could leave
lead/tail floodplain vertices lying ON the cap line (they belong to the cap, not the side).
condition_boundary_sides straightens the caps to 2-point chords and migrates cap-collinear side
vertices into them; geometry.reach_boundary_issues softly validates that the reach centerline
meets both caps, and geometry.centerline_conflicts is the BLOCKING check that no boundary line
lies across the centerline (floodplain sides: any intersection; caps: only away from the ends).

Conditioning frame (projected metres): up cap on x=0 spanning y=+100..-100, down cap on x=500;
left runs upstream→downstream near y=+100, right near y=-100 (the _sides_from_ring convention:
left ul→dl, right ur→dr, up ul→ur, down dl→dr, shared corners exact)."""
from math import cos, radians

from shapely.geometry import LineString

from hype_app.delineate import condition_boundary_sides
from hype_app.geometry import centerline_conflicts, reach_boundary_issues


def _frame(left=None, right=None, up=None, down=None):
    """The clean rectangular frame, with any side overridden."""
    return (
        LineString(left or [(0.0, 100.0), (500.0, 100.0)]),
        LineString(right or [(0.0, -100.0), (500.0, -100.0)]),
        LineString(up or [(0.0, 100.0), (0.0, -100.0)]),
        LineString(down or [(500.0, 100.0), (500.0, -100.0)]),
    )


def _coords(g):
    return [tuple(c) for c in g.coords]


# --- condition_boundary_sides: straight caps ---------------------------------------------------

def test_jogged_cap_straightens_to_chord():
    left, right, up, down = condition_boundary_sides(
        *_frame(up=[(0.0, 100.0), (3.0, 10.0), (0.0, -100.0)],
                down=[(500.0, 100.0), (497.0, 0.0), (500.0, -100.0)]))
    assert _coords(up) == [(0.0, 100.0), (0.0, -100.0)]
    assert _coords(down) == [(500.0, 100.0), (500.0, -100.0)]
    assert _coords(left) == [(0.0, 100.0), (500.0, 100.0)]      # sides untouched
    assert _coords(right) == [(0.0, -100.0), (500.0, -100.0)]


def test_two_point_side_passes_through():
    left, right, up, down = condition_boundary_sides(*_frame())
    assert _coords(left) == [(0.0, 100.0), (500.0, 100.0)]
    assert _coords(up) == [(0.0, 100.0), (0.0, -100.0)]


# --- condition_boundary_sides: collinear corner migration --------------------------------------

def test_outward_collinear_run_migrates_into_cap():
    # end-plane-clamped lead vertices lie exactly on the up-cap line (x=0), beyond the corner
    left, right, up, down = condition_boundary_sides(
        *_frame(left=[(0.0, 100.0), (0.0, 110.0), (0.0, 118.0), (25.0, 118.0), (500.0, 100.0)]))
    assert _coords(left) == [(0.0, 118.0), (25.0, 118.0), (500.0, 100.0)]
    assert _coords(up) == [(0.0, 118.0), (0.0, -100.0)]         # cap extended to the run's end
    assert _coords(up)[0] == _coords(left)[0]                   # shared corner stays exact


def test_inward_overlap_spike_squares():
    # the lead vertex doubles back along the cap INSIDE its span — squared, not extended
    left, right, up, down = condition_boundary_sides(
        *_frame(left=[(0.0, 100.0), (0.0, 60.0), (35.0, 60.0), (500.0, 100.0)]))
    assert _coords(left) == [(0.0, 60.0), (35.0, 60.0), (500.0, 100.0)]
    assert _coords(up) == [(0.0, 60.0), (0.0, -100.0)]          # cap shrank to the true corner


def test_tolerance_edge():
    inside, *_ = condition_boundary_sides(
        *_frame(left=[(0.0, 100.0), (1.9, 130.0), (60.0, 130.0), (500.0, 100.0)]))
    assert _coords(inside)[0] == (1.9, 130.0)                   # 1.9 m off the chord → migrates
    outside, *_ = condition_boundary_sides(
        *_frame(left=[(0.0, 100.0), (2.5, 130.0), (60.0, 130.0), (500.0, 100.0)]))
    assert _coords(outside)[0] == (0.0, 100.0)                  # 2.5 m off the chord → stays


def test_candidates_tested_against_original_chord():
    # drifting run: each step is <2 m from the PREVIOUS vertex's line, but 2.4 m from the
    # original chord — only the first two migrate (the reference line never drifts)
    left, right, up, down = condition_boundary_sides(
        *_frame(left=[(0.0, 100.0), (0.8, 120.0), (1.6, 140.0), (2.4, 160.0),
                      (80.0, 160.0), (500.0, 100.0)]))
    assert _coords(left)[0] == (1.6, 140.0)
    assert _coords(up) == [(1.6, 140.0), (0.0, -100.0)]


def test_side_keeps_two_vertices_and_opposite_endpoint():
    # left lies entirely on the up-cap line: the walk stops before the opposite endpoint,
    # so the side survives with its last two vertices
    left, right, up, down = condition_boundary_sides(
        *_frame(left=[(0.0, 100.0), (0.0, 80.0), (0.0, 60.0)]))
    assert _coords(left) == [(0.0, 80.0), (0.0, 60.0)]
    assert _coords(up) == [(0.0, 80.0), (0.0, -100.0)]
    assert _coords(down)[0] == (0.0, 60.0)                      # down cap follows the new DL


def test_both_corners_of_one_side_migrate():
    left, right, up, down = condition_boundary_sides(
        *_frame(left=[(0.0, 100.0), (0.0, 120.0), (80.0, 140.0), (420.0, 140.0),
                      (500.0, 120.0), (500.0, 100.0)]))
    assert _coords(left) == [(0.0, 120.0), (80.0, 140.0), (420.0, 140.0), (500.0, 120.0)]
    assert _coords(up) == [(0.0, 120.0), (0.0, -100.0)]
    assert _coords(down) == [(500.0, 120.0), (500.0, -100.0)]


def test_all_four_corners_migrate_and_ring_closes():
    left, right, up, down = condition_boundary_sides(
        *_frame(left=[(0.0, 100.0), (0.0, 115.0), (90.0, 130.0), (410.0, 130.0),
                      (500.0, 115.0), (500.0, 100.0)],
                right=[(0.0, -100.0), (0.0, -115.0), (90.0, -130.0), (410.0, -130.0),
                       (500.0, -115.0), (500.0, -100.0)]))
    L, R, U, D = _coords(left), _coords(right), _coords(up), _coords(down)
    assert L == [(0.0, 115.0), (90.0, 130.0), (410.0, 130.0), (500.0, 115.0)]
    assert R == [(0.0, -115.0), (90.0, -130.0), (410.0, -130.0), (500.0, -115.0)]
    assert len(U) == 2 and len(D) == 2
    # the four shared corners are bit-exact, so the assembled ring closes
    assert U[0] == L[0] and U[-1] == R[0] and D[0] == L[-1] and D[-1] == R[-1]


# --- reach_boundary_issues ----------------------------------------------------------------------

_KX = 111320.0 * cos(radians(45.0))
_KY = 110540.0


def _f(pts_m):
    """Metres in the test frame → a 4326 LineString Feature at lat 45."""
    return {"type": "Feature", "properties": {},
            "geometry": {"type": "LineString",
                         "coordinates": [[x / _KX, 45.0 + y / _KY] for x, y in pts_m]}}


def _bounds():
    return {"up": _f([(0.0, 100.0), (0.0, -100.0)]),
            "down": _f([(500.0, 100.0), (500.0, -100.0)]),
            "left": _f([(0.0, 100.0), (500.0, 100.0)]),
            "right": _f([(0.0, -100.0), (500.0, -100.0)])}


def test_reach_touching_caps_at_endpoints_passes():
    b = _bounds()
    reach = _f([(0.0, 0.0), (500.0, 0.0)])                      # endpoints ON both caps
    assert reach_boundary_issues(reach, b["up"], b["left"], b["right"], b["down"]) == []


def test_reach_endpoint_within_tolerance_passes():
    b = _bounds()
    reach = _f([(8.0, 0.0), (500.0, 0.0)])                      # 8 m short of the up cap
    assert reach_boundary_issues(reach, b["up"], b["left"], b["right"], b["down"]) == []


def test_reach_endpoint_beyond_tolerance_flagged():
    b = _bounds()
    reach = _f([(30.0, 0.0), (500.0, 0.0)])                     # 30 m short of the up cap
    issues = reach_boundary_issues(reach, b["up"], b["left"], b["right"], b["down"])
    assert len(issues) == 1 and "Upstream" in issues[0]


def test_missing_inputs_return_empty():
    b = _bounds()
    reach = _f([(0.0, 0.0), (500.0, 0.0)])
    assert reach_boundary_issues(reach, b["up"], b["left"], None, b["down"]) == []
    assert reach_boundary_issues(None, b["up"], b["left"], b["right"], b["down"]) == []


# --- centerline_conflicts (blocking) --------------------------------------------------------------

def _conflicts(reach, b):
    return centerline_conflicts(reach, b["up"], b["left"], b["right"], b["down"])


def test_clean_frame_has_no_conflicts():
    # Endpoints ON both caps — the legitimate generated layout must never block.
    assert _conflicts(_f([(0.0, 0.0), (500.0, 0.0)]), _bounds()) == []


def test_reach_crossing_floodplain_side_is_a_conflict_not_a_soft_issue():
    b = _bounds()
    reach = _f([(0.0, 0.0), (250.0, 150.0), (500.0, 0.0)])      # bulges across the left line
    conflicts = _conflicts(reach, b)
    assert [c["slot"] for c in conflicts] == ["left"]
    assert "Left floodplain" in conflicts[0]["msg"]
    # the check moved out of the soft list — caps are met, so nothing soft remains
    assert reach_boundary_issues(reach, b["up"], b["left"], b["right"], b["down"]) == []


def test_side_lying_along_the_stream_conflicts():
    b = _bounds()
    b["right"] = _f([(0.0, 0.0), (500.0, 0.0)])                 # drawn on the centerline
    conflicts = _conflicts(_f([(0.0, 0.0), (500.0, 0.0)]), b)
    assert [c["slot"] for c in conflicts] == ["right"]


def test_cap_crossing_mid_reach_conflicts_but_end_touch_does_not():
    b = _bounds()
    b["down"] = _f([(250.0, 100.0), (250.0, -100.0)])           # dragged across the interior
    conflicts = _conflicts(_f([(0.0, 0.0), (500.0, 0.0)]), b)
    assert [c["slot"] for c in conflicts] == ["down"]
    # reach overshooting the up cap by 10 m still crosses it NEAR the end — allowed
    assert _conflicts(_f([(-10.0, 0.0), (500.0, 0.0)]), _bounds()) == []


def test_cap_gap_is_soft_not_a_conflict():
    b = _bounds()
    reach = _f([(30.0, 0.0), (500.0, 0.0)])                     # 30 m short of the up cap
    assert _conflicts(reach, b) == []                           # no overlap → no block
    issues = reach_boundary_issues(reach, b["up"], b["left"], b["right"], b["down"])
    assert len(issues) == 1 and "Upstream" in issues[0]         # the gap stays a soft warning


def test_conflicts_check_sides_independently():
    b = _bounds()
    reach = _f([(0.0, 0.0), (250.0, 150.0), (500.0, 0.0)])
    conflicts = centerline_conflicts(reach, None, b["left"], None, None)
    assert [c["slot"] for c in conflicts] == ["left"]           # half-drawn set still flags
    assert centerline_conflicts(None, b["up"], b["left"], b["right"], b["down"]) == []
