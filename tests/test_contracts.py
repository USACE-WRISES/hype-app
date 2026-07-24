"""Contract-validation, computed-field, round-trip, and migration tests (spec §13.1)."""
import math

import pytest
from pydantic import ValidationError

from hype_app.contracts import (
    SCHEMA_VERSIONS,
    AssessmentInputSnapshot,
    FlowCandidate,
    FlowLookupSnapshot,
    GradientBoundaryConfigV2,
    GradientControl,
    GradientQualitative,
    GridSettings,
    KSettings,
    LatLon,
    ReferenceSlope,
    Side,
    StreamflowInput,
    migrate,
    watershed_display_features,
)
from hype_app.provenance import Provenance, Severity


# --------------------------------------------------------------------------- helpers
def _snapshot(**over) -> AssessmentInputSnapshot:
    base = dict(
        assessment_id="A1",
        streamflow=StreamflowInput(value_cfs=100.0, value_cms=2.83,
                                   provenance=Provenance(source="manual")),
        k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.3),
        gradients=GradientBoundaryConfigV2(),
        grid=GridSettings(cell_size_x=10.0, cell_size_y=10.0,
                          gw_mod_depth=20.0, layer_thickness=0.5),
    )
    base.update(over)
    return AssessmentInputSnapshot(**base)


# --------------------------------------------------------------------------- provenance
def test_provenance_with_warning_is_immutable_copy():
    p = Provenance(source="USGS")
    p2 = p.with_warning("stale", "cached value", Severity.warning, days=30)
    assert len(p.warnings) == 0 and len(p2.warnings) == 1
    assert p2.warnings[0].code == "stale" and p2.warnings[0].context == {"days": 30}


# --------------------------------------------------------------------------- flow
class TestFlowCandidate:
    def test_positive_discharge_is_insertable(self):
        c = FlowCandidate(id="c1", original_value=100.0, original_unit="ft^3/s", value_cfs=100.0)
        assert c.insertable is True

    def test_none_negative_inf_excluded_not_insertable(self):
        assert FlowCandidate(id="c", original_value=0, original_unit="ft^3/s").insertable is False
        assert FlowCandidate(id="c", original_value=-5, original_unit="ft^3/s",
                             value_cfs=-5.0).insertable is False
        assert FlowCandidate(id="c", original_value=1, original_unit="ft^3/s",
                             value_cfs=math.inf).insertable is False
        assert FlowCandidate(id="c", original_value=1, original_unit="ft^3/s",
                             value_cfs=100.0, excluded=True).insertable is False

    def test_json_roundtrip_includes_computed_insertable(self):
        c = FlowCandidate(id="c1", original_value=100.0, original_unit="ft^3/s", value_cfs=100.0)
        dumped = c.model_dump(mode="json")
        assert dumped["insertable"] is True
        assert FlowCandidate.model_validate(dumped) == c   # computed key dropped on the way in

    def test_snapshot_selected(self):
        c = FlowCandidate(id="c1", original_value=1, original_unit="ft^3/s", value_cfs=1.0)
        snap = FlowLookupSnapshot(requested_point=LatLon(lat=43.0, lon=-72.0),
                                  candidates=[c], selected_candidate_id="c1")
        assert snap.selected() is c


# --------------------------------------------------------------------------- gradients
class TestGradients:
    def test_control_station_out_of_range_raises(self):
        with pytest.raises(ValidationError):
            GradientControl(id="x", side=Side.left, station=1.5, preferred=0.01)

    def test_side_requires_stations_0_and_1(self):
        mid = GradientControl(id="m", side=Side.left, station=0.5, preferred=0.01)
        with pytest.raises(ValidationError):
            GradientBoundaryConfigV2(left_controls=[mid])

    def test_duplicate_stations_raise(self):
        dup = [GradientControl(id="a", side=Side.left, station=0.0, preferred=0.01),
               GradientControl(id="b", side=Side.left, station=0.0, preferred=0.02),
               GradientControl(id="c", side=Side.left, station=1.0, preferred=0.03)]
        with pytest.raises(ValidationError):
            GradientBoundaryConfigV2(left_controls=dup)

    def test_valid_two_endpoint_side(self):
        cfg = GradientBoundaryConfigV2(left_controls=[
            GradientControl(id="a", side=Side.left, station=0.0, preferred=0.01),
            GradientControl(id="b", side=Side.left, station=1.0, preferred=0.02)])
        assert len(cfg.left_controls) == 2

    def test_from_qualitative_locked_multipliers(self):
        rs = ReferenceSlope(value=0.01, source="manual")
        cfg = GradientBoundaryConfigV2.from_qualitative(
            left=GradientQualitative.strongly_gaining,
            right=GradientQualitative.slightly_losing, reference_slope=rs)
        assert cfg.mode == "qualitative"
        assert all(c.preferred == pytest.approx(+0.01) for c in cfg.left_controls)   # +1.0 * 0.01
        assert all(c.preferred == pytest.approx(-0.005) for c in cfg.right_controls)  # -0.5 * 0.01

    def test_neutral_is_zero(self):
        rs = ReferenceSlope(value=0.02, source="wse_raster")
        cfg = GradientBoundaryConfigV2.from_qualitative(
            left=GradientQualitative.neutral, right=GradientQualitative.neutral, reference_slope=rs)
        assert all(c.preferred == 0.0 for c in cfg.left_controls + cfg.right_controls)

    def test_bad_mode_raises(self):
        with pytest.raises(ValidationError):
            GradientBoundaryConfigV2(mode="nonsense")


# --------------------------------------------------------------------------- input snapshot
class TestAssessmentInputSnapshot:
    def test_input_hash_is_stable_hex(self):
        s1, s2 = _snapshot(), _snapshot()
        assert s1.input_hash == s2.input_hash
        assert len(s1.input_hash) == 64

    def test_group_hashes_cover_all_groups(self):
        from hype_app.hashing import INPUT_GROUPS
        assert set(_snapshot().group_hashes()) == set(INPUT_GROUPS)

    def test_streamflow_change_changes_streamflow_group_only(self):
        a = _snapshot()
        b = _snapshot(streamflow=StreamflowInput(value_cfs=200.0, value_cms=5.66,
                                                 provenance=Provenance(source="manual")))
        ga, gb = a.group_hashes(), b.group_hashes()
        assert ga["streamflow"] != gb["streamflow"]
        assert ga["geometry"] == gb["geometry"]
        assert ga["gradients"] == gb["gradients"]

    def test_porosity_change_hits_soil_k(self):
        a = _snapshot()
        b = _snapshot(k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.4))
        ga, gb = a.group_hashes(), b.group_hashes()
        assert ga["soil_k"] != gb["soil_k"]
        assert ga["geometry"] == gb["geometry"]
        # The per-run MP7 pass (System A) was removed 2026-07-18: no "particles" group,
        # and the legacy grid fields stay excluded so grid hashes are stable across it.
        assert "particles" not in ga
        assert "particles_per_cell" not in ga["grid"]

    def test_json_roundtrip(self):
        s = _snapshot()
        assert AssessmentInputSnapshot.model_validate(s.model_dump(mode="json")) == s


# --------------------------------------------------------------------------- registry / migration
def test_schema_versions_present():
    assert "assessment-input-snapshot" in SCHEMA_VERSIONS
    assert SCHEMA_VERSIONS["assessment-results"].startswith("assessment-results/")


def test_migrate_is_noop_for_unregistered_kind():
    data = {"schema_version": "assessment-input-snapshot/2.0", "x": 1}
    assert migrate("assessment-input-snapshot", data) == data


def test_results_migration_drops_hfci_2_0_to_2_1():
    """An older results payload carrying the removed HFCI index still opens: migrate() pops the
    field and stamps the current schema version so the current model validates it."""
    from hype_app.contracts import AssessmentResultsV2
    old = {"schema_version": "assessment-results/2.0", "assessment_id": "A1",
           "input_hash": "a" * 64, "hfci": {"hfci": 0.67, "hfci_class": "Moderate"}}
    out = migrate("assessment-results", old)
    assert "hfci" not in out
    assert out["schema_version"] == SCHEMA_VERSIONS["assessment-results"]
    AssessmentResultsV2.model_validate(out)          # no extra="forbid" rejection


# --------------------------------------------------------------------------- flow review map
class TestWatershedDisplayFeatures:
    """watershed_display_features() feeds the flow modal's review map (never raises)."""

    def _payload(self):
        import json
        from pathlib import Path
        raw = json.loads((Path(__file__).resolve().parent / "fixtures" / "usgs"
                          / "delineate.json").read_text(encoding="utf-8"))
        ws = raw["bcrequest"]["wsresp"]
        # exactly the dict streamstats.lookup_flow stores on the snapshot
        return {"featurecollection": ws["featurecollection"],
                "workspace_id": ws.get("workspace_id")}

    def test_nested_fixture_shape(self):
        ws, pour = watershed_display_features(self._payload())
        assert ws is not None and ws["type"] == "FeatureCollection"
        assert ws["features"][0]["geometry"]["type"] == "Polygon"
        assert pour == pytest.approx((43.686040291133395, -72.23677872156684))

    def test_flat_list_variant(self):
        p = self._payload()
        flat = [it for grp in p["featurecollection"] for it in grp]
        ws, pour = watershed_display_features({"featurecollection": flat})
        assert ws is not None and pour is not None

    def test_malformed_payloads_yield_none(self):
        assert watershed_display_features(None) == (None, None)
        assert watershed_display_features({}) == (None, None)
        assert watershed_display_features({"featurecollection": [{"name": "globalwatershed"}]}) \
            == (None, None)
        assert watershed_display_features(
            {"featurecollection": [{"name": "globalwatershedpoint",
                                    "feature": {"features": []}}]}) == (None, None)
