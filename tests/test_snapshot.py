"""Tests for the run-input snapshot builder (spec §4.2)."""
from hype_app.contracts import AssessmentInputSnapshot
from hype_app.snapshot import BC_CORNER, BC_PROFILE, build_input_snapshot


def _corner_params(**o):
    p = dict(
        cell_size_x=10.0, cell_size_y=10.0, gw_mod_depth=6.0, z=0.25,
        kh=10.0, kv=1.0, porosity=0.3,
        model_origin_elev=101.5, boundary_condition_mode=BC_CORNER,
        upstream_left_fpl_gw_gradient=0.005, upstream_right_fpl_gw_gradient=0.005,
        downstream_left_fpl_gw_gradient=0.005, downstream_right_fpl_gw_gradient=0.005,
    )
    p.update(o)
    return p


def test_builds_valid_snapshot_from_corner_params():
    snap = build_input_snapshot(assessment_id="A1", params=_corner_params(),
                                streamflow_cfs=100.0, app_version="2026.07")
    assert isinstance(snap, AssessmentInputSnapshot)
    assert snap.k.kh_m_day == 10.0 and snap.k.porosity == 0.3
    assert snap.grid.cell_size_x == 10.0 and snap.grid.layer_thickness == 0.25
    assert snap.terrain.model_origin_elev == 101.5
    assert snap.streamflow.value_cfs == 100.0
    assert snap.streamflow.value_cms is not None
    # legacy corner gradients preserved verbatim
    assert snap.gradients.legacy.boundary_condition_mode == BC_CORNER
    assert snap.gradients.legacy.corner_gradients["g_ul"] == 0.005
    assert len(snap.input_hash) == 64
    assert set(snap.group_hashes())  # non-empty, all 7 groups


def test_profile_and_corner_hash_differently():
    corner = build_input_snapshot(assessment_id="A", params=_corner_params(), streamflow_cfs=1.0)
    prof = build_input_snapshot(
        assessment_id="A",
        params=_corner_params(boundary_condition_mode=BC_PROFILE,
                              left_boundary_gradient_profile="0,0.01 1,0.02",
                              right_boundary_gradient_profile="0,0.01 1,0.02"),
        streamflow_cfs=1.0)
    assert corner.group_hashes()["gradients"] != prof.group_hashes()["gradients"]
    assert prof.gradients.legacy.left_profile == "0,0.01 1,0.02"


def test_streamflow_none_is_allowed():
    snap = build_input_snapshot(assessment_id="A", params=_corner_params(), streamflow_cfs=None)
    assert snap.streamflow.value_cfs is None and snap.streamflow.value_cms is None


def test_json_roundtrip():
    snap = build_input_snapshot(assessment_id="A1", params=_corner_params(), streamflow_cfs=50.0)
    assert AssessmentInputSnapshot.model_validate(snap.model_dump(mode="json")) == snap


def test_freeze_persist_restore_chain(tmp_path):
    """The full Phase-1 seam: build snapshot -> archive (config/assessment_input.json) ->
    restore -> reconstruct the model. This is what the app's run + Save + Open exercise."""
    from hype_app import bundle

    snap = build_input_snapshot(assessment_id="A1", params=_corner_params(),
                                streamflow_cfs=100.0, app_version="2026.07")
    src = tmp_path / "s"
    src.mkdir()
    zip_path = bundle.zip_workspace(
        src, vectors={}, state={"format_version": 2, "input_snapshot": snap.model_dump(mode="json")},
        assessment_input=snap.model_dump(mode="json"))
    out = bundle.restore_workspace(zip_path, tmp_path / "d")

    restored = AssessmentInputSnapshot.model_validate(out["assessment_input"])
    assert restored == snap
    assert restored.input_hash == snap.input_hash
