"""New-project reset contract (2026-08-14): the map returns to the national view and
the session is a true blank slate — nothing carries over from the previously open
project.

Map half: MAP_HOME_* is the single source for the national view, used by the Map
constructor and by _map_home(), which runs ONLY on the New tails (_create_project,
_reset, _new_project_create). It must never run inside _reset_memory_state or
_reset_session_state: both are shared with Open, which flies to the site itself and
must not jump to CONUS first.

Blank half: source-scan pins over the _reset_memory_state body, one per leak closed
(typed-input registry, NHD snap cache, line display prefs, per-project display
prefs, decor identity guards, comparison overlay, video state). Single-key scan
style per tests/test_wells.py.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")


def _fn_body(name: str) -> str:
    i = APP_SRC.index(f"def {name}(")
    j = APP_SRC.index("\n    def ", i + 10)
    k = APP_SRC.index("\n    async def ", i + 10)
    return APP_SRC[i:min(j, k)]


# ------------------------------------------------------------------- national view

def test_map_home_constants_are_the_single_source():
    assert "MAP_HOME_CENTER = (39.5, -98.35)" in APP_SRC
    assert "MAP_HOME_ZOOM = 4" in APP_SRC
    assert "Map(center=MAP_HOME_CENTER, zoom=MAP_HOME_ZOOM" in APP_SRC


def test_map_home_helper_writes_the_traits():
    body = _fn_body("_map_home")
    assert "_MAP.center = MAP_HOME_CENTER" in body
    assert "_MAP.zoom = MAP_HOME_ZOOM" in body


def test_map_home_runs_on_every_new_tail():
    create = _fn_body("_create_project")
    assert "_map_home()" in create
    # the blank view lands AFTER the reset (never on a session still holding layers)
    assert create.index("await _reset_memory_state()") < create.index("_map_home()")
    # cloud tails: destructive-New confirm and the name-dialog create
    reset = _fn_body("_reset")
    assert "_map_home()" in reset
    npc = _fn_body("_new_project_create")
    assert "_map_home()" in npc


def test_map_home_never_runs_on_shared_reset_paths():
    # _reset_memory_state and _reset_session_state also serve Open, which flies to
    # the site itself — a national jump there would churn layers mid-flight.
    assert "_map_home" not in _fn_body("_reset_memory_state")
    assert "_map_home" not in _fn_body("_reset_session_state")


# ------------------------------------------------------------------- blank slate

def _reset_body() -> str:
    return _fn_body("_reset_memory_state")


def test_reset_clears_typed_inputs_wholesale():
    body = _reset_body()
    assert "_kept.clear()" in body
    # grid_wireframe is not pane-mounted, so it keeps its explicit widget update
    assert 'ui.update_checkbox("grid_wireframe", value=False)' in body


def test_reset_clears_nhd_snap_cache():
    body = _reset_body()
    assert '_flow["gdf"] = None' in body
    assert '_flow.pop("bbox", None)' in body   # stale bbox suppressed the refetch


def test_reset_restores_line_display_prefs():
    body = _reset_body()
    assert "fp_line_show_v.set(True)" in body
    assert 'fp_line_mode_v.set("class")' in body
    assert '_fp_rng_applied["rng"] = None' in body


def test_reset_restores_per_project_display_prefs():
    body = _reset_body()
    for pin in ("head_layer_v.set(1)", "head_opacity_v.set(0.85)",
                "hd_contours_v.set(True)", "ras_opacity_v.set(0.7)",
                "dem_hs_v.set(8.0)", "dem_opacity_v.set(0.8)",
                "grid_opacity3d_v.set(1.0)", "grid_color3d_v.set(None)"):
        assert pin in body, pin


def test_reset_clears_geometry_session_leftovers():
    body = _reset_body()
    for pin in ("origin_override.set(None)", "proj_crs.set(None)",
                'delineate_mode.set("auto")', "reach_edit.set(False)",
                "kz_adding.set(False)", '_ras_inputs_sig["sig"] = None',
                "_report_stamp.set(None)"):
        assert pin in body, pin


def test_reset_forgets_decor_identity():
    body = _reset_body()
    assert "_clear_mirror_layers()" in body
    assert "_mirror_features_as_layers()" in body


def test_reset_closes_comparison_and_video():
    body = _reset_body()
    assert "comparison_mode_v.set(False)" in body
    assert "comparison_collection_v.set(None)" in body
    assert "_video_cancel.set()" in body
    assert "_video_result.set(None)" in body


def test_reset_collapses_the_tree():
    assert '"hype_tree_collapse", {"groups": list(ui_tree.GROUP_IDS)}' in _reset_body()


# ------------------------------------------------------------------- changelog

def test_changelog_mentions_the_fix():
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "returns the map to the national view" in text
