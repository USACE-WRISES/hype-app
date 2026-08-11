"""Cross-project comparison contracts, read-only loading, freezing, and exports."""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from hype_app import bundle, project_meta, recents, results_lifecycle
from hype_app.comparison import (
    add_projects,
    comparison_ui_payload,
    generate_comparison_report,
    inspect_collection,
    inspect_member,
    load_collection,
    new_collection,
    read_project_summary,
    refresh_member,
    save_collection,
)
from hype_app.comparison_metrics import METRICS_BY_ID, PRIMARY_METRIC_IDS, default_scale
from hype_app.contracts import (
    AltScenario,
    AltStatus,
    AssessmentInputSnapshot,
    AssessmentResultsV2,
    ComparisonCollectionV1,
    ComparisonMetricObservationV1,
    ComparisonSourceStatus,
    ComparisonViewSettingsV1,
    ConnectivityMetrics,
    GradientBoundaryConfigV2,
    GridSettings,
    HydraulicAlternativesManifest,
    KSettings,
    ResidenceTimeMetrics,
    SiteMetadata,
    StreamflowInput,
    ZoneMetrics,
)
from hype_app.hashing import stable_hash
from hype_app.provenance import Provenance

WWW = Path(__file__).resolve().parents[1] / "www"


def _snapshot(name="Alpha", assessment_id="A1", site_id="site-1"):
    return AssessmentInputSnapshot(
        assessment_id=assessment_id,
        site=SiteMetadata(site_id=site_id, site_name=name, reach_length_m=800.0),
        streamflow=StreamflowInput(
            value_cms=2.0, value_cfs=70.6293, provenance=Provenance(source="manual")),
        k=KSettings(kh_m_day=10.0, kv_m_day=1.0, porosity=0.3),
        gradients=GradientBoundaryConfigV2(),
        grid=GridSettings(cell_size_x=10.0, cell_size_y=10.0, gw_mod_depth=20.0,
                          layer_thickness=0.5),
        model_version="model-v1", app_version="2026.08",
        created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


def _manifest(snap, *, partial=True):
    scenarios = [AltScenario(
        id="k_hi", label="Higher K", k_factor=10.0, g_factor=1.0,
        status=AltStatus.completed,
        results_sections={
            "connectivity": {"turnovers_per_km": 0.9},
            "residence_time": {"weighted_median_days": 0.5},
            "zone": {"equivalent_active_depth_m": 2.0},
        })]
    if partial:
        scenarios.append(AltScenario(
            id="g_lo", label="Lower gradient", k_factor=1.0, g_factor=0.1,
            status=AltStatus.failed, error="solver failed"))
    return HydraulicAlternativesManifest(
        base_input_hash=snap.input_hash,
        base_assessment_id=snap.assessment_id,
        selection={"k_upper": 10.0, "g_lower": 0.1 if partial else None, "combos": False},
        scenarios=scenarios,
        hz_knobs={"particles_per_cell": 8},
        method_versions={"results": "2.4"},
    )


def _results_for(snap, *, turnover=0.5):
    return AssessmentResultsV2(
        assessment_id=snap.assessment_id,
        input_hash=snap.input_hash,
        input_snapshot=snap,
        group_hashes=snap.group_hashes(),
        connectivity=ConnectivityMetrics(
            turnovers_per_km=turnover, turnover_length_km=1.0 / turnover,
            returning_hyporheic_cms=0.1, streamflow_cms=2.0,
            active_streambed_fraction=0.4),
        residence_time=ResidenceTimeMetrics(
            weighted_median_days=1.0, p10_days=0.25, p90_days=4.0,
            effective_particle_count=80.0, porosity=0.3),
        zone=ZoneMetrics(equivalent_active_depth_m=1.0,
                         bulk_saturated_volume_m3=900.0,
                         active_volume_basis="bulk sediment"),
        created_at=datetime(2026, 8, 2, tzinfo=timezone.utc),
    )


def _write_project(folder: Path, *, site="Alpha", turnover=0.5, partial=True,
                   stale=(), alt_identity_ok=True) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    snap = _snapshot(site, assessment_id=f"assessment-{folder.name}",
                     site_id=f"site-{folder.name}")
    results = _results_for(snap, turnover=turnover)
    manifest = _manifest(snap, partial=partial)
    manifest_data = manifest.model_dump(mode="json")
    if not alt_identity_ok:
        manifest_data["base_input_hash"] = "f" * 64
    state = {
        "format_version": 2,
        "desktop_project": True,
        "project_name": folder.name,
        "project_id": f"project-{folder.name}",
        "site_id": f"site-{folder.name}",
        "results_model": results.model_dump(mode="json"),
        "input_snapshot": snap.model_dump(mode="json"),
        "alt_result": {"manifest": manifest_data},
        "stale_marks": list(stale),
    }
    path = folder / f"{folder.name}.hype"
    bundle.save_bundle_to(folder, path, vectors={}, state=state,
                          assessment_input=snap.model_dump(mode="json"))
    return path


def _rewrite(project: Path, mutate) -> None:
    """Round-trip the saved bundle through `mutate(payload)` (fixture preparation)."""
    with bundle._open_bundle(project) as archive:  # noqa: SLF001
        payload = bundle._read_bundle(archive)     # noqa: SLF001
    mutate(payload)
    bundle.save_bundle_to(project.parent, project, vectors={}, state=payload["state"],
                          assessment_input=payload.get("assessment_input"))


def _hz_artifacts(folder: Path) -> None:
    """Real, minimal hz artifacts the alternatives-sweep adapter can re-derive from."""
    hz = folder / "summary" / "hz"
    hz.mkdir(parents=True, exist_ok=True)
    acct = {"total_downwelling": 8640.0, "returning": 4320.0, "losing": 864.0,
            "unresolved": 0.0, "streambed_area_m2": 5000.0,
            "active_streambed_area_m2": 3000.0, "return_streambed_area_m2": 2000.0,
            "connected_streambed_area_m2": 2500.0, "net_stream_exchange": -1000.0,
            "n_stream_cells_downwelling": 40, "particles_per_cell": 2}
    stats = {"classes": {"hyporheic": {"footprint_m2": 1500.0}},
             "flux": {"accounting": acct},
             "domain": {"active_saturated_volume_m3": 60000.0},
             "knobs": {"porosity": 0.3}}
    (hz / "hz_stats.json").write_text(json.dumps(stats), encoding="utf-8")
    rng = np.random.default_rng(1)
    n = 60
    np.savez(hz / "hz_flux.npz",
             source_node=np.arange(n), weight=rng.uniform(50.0, 500.0, n),
             cls=np.where(np.arange(n) % 3 == 0, 2, 1),
             time_days=np.exp(rng.normal(0.0, 0.8, n)),
             status=np.full(n, 5), max_depth_m=rng.uniform(0.2, 3.0, n),
             path_length_m=rng.uniform(5.0, 60.0, n))


# ---------------------------------------------------------------- contracts

def test_contracts_are_versioned_strict_and_finite():
    collection = ComparisonCollectionV1()
    payload = collection.model_dump(mode="json")
    assert payload["schema_version"] == "comparison-collection/1.0"
    assert ComparisonCollectionV1.model_validate(payload) == collection
    with pytest.raises(ValidationError):
        ComparisonCollectionV1.model_validate(payload | {"typo": True})
    with pytest.raises(ValidationError):
        ComparisonMetricObservationV1(metric_id="x", unit="m", baseline=float("nan"))
    with pytest.raises(ValidationError):
        ComparisonMetricObservationV1(metric_id="x", unit="m", baseline=True,
                                      low=True, high=True, finite_case_count=1)
    with pytest.raises(ValidationError):
        ComparisonMetricObservationV1(metric_id="x", unit="m", baseline=1, low=2, high=3,
                                      finite_case_count=1, has_range=False)


def test_view_settings_hold_a_panel_list_and_accept_the_prototype_shape():
    """The Metric tab is a LIST of aligned panels; prototype files stored one metric_id."""
    assert ComparisonViewSettingsV1().metric_ids == ["connectivity.turnovers_per_km"]
    deduped = ComparisonViewSettingsV1.model_validate(
        {"metric_ids": ["a", "a", " b ", ""]})
    assert deduped.metric_ids == ["a", "b"]
    legacy = ComparisonViewSettingsV1.model_validate(
        {"metric_id": "zone.equivalent_active_depth_m"})
    assert legacy.metric_ids == ["zone.equivalent_active_depth_m"]


def test_site_id_is_excluded_from_the_input_hash():
    a = _snapshot(site_id="site-1")
    b = a.model_copy(update={"site": a.site.model_copy(update={"site_id": "site-2"})})
    c = a.model_copy(update={"site": a.site.model_copy(update={"site_id": None})})
    assert a.input_hash == b.input_hash == c.input_hash


def test_project_meta_surfaces_ids_and_mints_identities():
    meta = project_meta.meta_from_state(
        {"project_name": "X", "project_id": "p", "site_id": "s"})
    assert meta["project_id"] == "p" and meta["site_id"] == "s"
    assert "project_id" not in project_meta.meta_from_state({"project_name": "X"})
    assert project_meta.new_identity() != project_meta.new_identity()


def test_metric_registry_has_stable_paths_transforms_and_scale_policy():
    assert PRIMARY_METRIC_IDS == (
        "connectivity.turnovers_per_km",
        "residence_time.weighted_median_days",
        "zone.equivalent_active_depth_m",
    )
    metric = METRICS_BY_ID["residence_time.weighted_median_days"]
    assert metric.canonical_unit == "day" and metric.presentation_unit == "hr"
    assert metric.extract({"residence_time": {"weighted_median_days": 2.0}}) == 48.0
    assert metric.extract({"residence_time": {"weighted_median_days": True}}) is None
    assert default_scale(metric.id, [1.0, 10.0]) == "log"
    assert default_scale(metric.id, [0.0, 10.0]) == "linear"


def test_metric_registry_fields_exist_on_the_results_contract():
    res = _results_for(_snapshot())
    for definition in METRICS_BY_ID.values():
        section = getattr(res, definition.section)
        assert hasattr(section, definition.field), definition.id


# ---------------------------------------------------------------- the reader

def test_project_summary_is_read_only_and_builds_partial_ranges(tmp_path):
    project = _write_project(tmp_path / "ProjectA", partial=True)
    before = project.stat()
    summary = read_project_summary(project)
    after = project.stat()
    assert summary.valid
    assert before.st_mtime_ns == after.st_mtime_ns and before.st_size == after.st_size
    snapshot = summary.snapshot
    assert snapshot.project_id == "project-ProjectA"
    assert snapshot.site_id == "site-ProjectA"
    turnover = snapshot.observations["connectivity.turnovers_per_km"]
    assert (turnover.baseline, turnover.low, turnover.high) == (0.5, 0.5, 0.9)
    assert turnover.finite_case_count == 2
    assert turnover.completed_scenario_count == 1
    assert turnover.configured_scenario_count == 2
    assert turnover.completeness == "partial"
    assert turnover.incomplete_scenarios == ["Lower gradient"]
    duration = snapshot.observations["residence_time.weighted_median_days"]
    assert (duration.baseline, duration.low, duration.high) == (24.0, 12.0, 24.0)


def test_legacy_manifest_normalizes_but_wrong_identity_is_not_merged(tmp_path):
    project = _write_project(tmp_path / "Legacy", alt_identity_ok=False)

    def _legacy(payload):
        manifest = payload["state"]["alt_result"]["manifest"]
        manifest.pop("selection", None)
        manifest["vary_k"] = True
        manifest["vary_gradient"] = True
        # the results-embedded copy must not rescue this fixture's wrong identity
        payload["state"]["results_model"]["alternatives"] = None

    _rewrite(project, _legacy)
    summary = read_project_summary(project)
    assert summary.valid  # bad alternatives are a warning, not a bad baseline
    assert summary.snapshot.scenarios == []
    assert any(f.code == "alternatives_identity_mismatch" for f in summary.findings)
    collection = add_projects(new_collection(), [project])
    assert comparison_ui_payload(collection)["members"][0]["status"] == "warning"


def test_manifest_attaches_through_the_frozen_snapshot_hash(tmp_path):
    """Additive snapshot-schema growth re-stamps results.input_hash at build time, so real
    projects hold a manifest bound to the FROZEN stored spelling. It must still attach
    (assessment_id anchors identity; observed on disk, the Auto2 lesson)."""
    project = _write_project(tmp_path / "Drift", partial=False)
    frozen = "b" * 64

    def _drift(payload):
        payload["state"]["input_snapshot"]["input_hash"] = frozen
        payload["assessment_input"]["input_hash"] = frozen
        payload["state"]["alt_result"]["manifest"]["base_input_hash"] = frozen
        payload["state"]["results_model"]["alternatives"] = None

    _rewrite(project, _drift)
    summary = read_project_summary(project)
    assert summary.valid
    assert summary.snapshot.scenarios, "the sweep must attach through the frozen spelling"
    assert not any(f.code == "alternatives_identity_mismatch" for f in summary.findings)


def test_embedded_manifest_attaches_when_the_state_copy_does_not(tmp_path):
    project = _write_project(tmp_path / "Embedded", partial=False)

    def _split(payload):
        manifest = payload["state"]["alt_result"]["manifest"]
        payload["state"]["results_model"]["alternatives"] = dict(manifest)
        payload["state"]["alt_result"]["manifest"] = dict(
            manifest, base_input_hash="f" * 64)

    _rewrite(project, _split)
    summary = read_project_summary(project)
    assert summary.valid and summary.snapshot.scenarios
    # the healthy embedded copy attached, so the state copy's failure is not surfaced
    assert not any(f.code == "alternatives_identity_mismatch" for f in summary.findings)


def test_legacy_primary_only_scenario_still_produces_primary_ranges(tmp_path):
    project = _write_project(tmp_path / "PrimaryOnly", partial=False)

    def _legacy(payload):
        scenario = payload["state"]["alt_result"]["manifest"]["scenarios"][0]
        scenario["results_sections"] = {}
        scenario["metrics"] = {"turnovers_per_km": 0.8, "rtd_median_days": 0.25,
                               "equivalent_active_depth_m": 1.8}
        payload["state"]["results_model"]["alternatives"] = None

    _rewrite(project, _legacy)
    snapshot = read_project_summary(project).snapshot
    assert snapshot.observations["connectivity.turnovers_per_km"].high == 0.8
    assert snapshot.observations["residence_time.weighted_median_days"].low == 6.0
    assert snapshot.observations["zone.equivalent_active_depth_m"].high == 1.8


def test_pre_site_uuid_snapshot_hash_remains_verifiable(tmp_path):
    project = _write_project(tmp_path / "PreUuid")

    def _legacy(payload):
        raw = payload["state"]["results_model"]["input_snapshot"]
        raw["site"].pop("site_id", None)
        raw.pop("input_hash", None)
        legacy_hash = stable_hash(raw)
        raw["input_hash"] = legacy_hash
        payload["state"]["results_model"]["input_hash"] = legacy_hash
        payload["state"]["input_snapshot"] = raw
        payload["state"]["alt_result"]["manifest"]["base_input_hash"] = legacy_hash
        payload["assessment_input"] = raw

    _rewrite(project, _legacy)
    summary = read_project_summary(project)
    assert summary.valid
    assert not any(f.code == "input_hash_mismatch" for f in summary.findings)


@pytest.mark.parametrize("stale", [("gw",), ("hz",), ("groundwater",)])
def test_stale_groundwater_or_hz_is_invalid(tmp_path, stale):
    summary = read_project_summary(_write_project(tmp_path / stale[0], stale=stale))
    assert not summary.valid and summary.snapshot is not None
    assert summary.snapshot.readiness == "invalid"
    assert any(f.code == "project_results_stale" for f in summary.findings)


def test_input_identity_and_dependency_hash_mismatches_are_invalid(tmp_path):
    project = _write_project(tmp_path / "Mismatch")

    def _corrupt(payload):
        payload["state"]["results_model"]["group_hashes"]["grid"] = "0" * 64

    _rewrite(project, _corrupt)
    summary = read_project_summary(project)
    assert not summary.valid
    assert any(f.code == "result_groups_stale" for f in summary.findings)


def test_missing_corrupt_and_future_sources_return_structured_findings(tmp_path):
    missing = read_project_summary(tmp_path / "missing.hype")
    assert not missing.valid and missing.findings[0].code == "source_missing"
    corrupt = tmp_path / "broken.hype"
    corrupt.write_text("not a zip", encoding="utf-8")
    bad = read_project_summary(corrupt)
    assert not bad.valid and bad.findings[0].code == "source_unreadable"

    project = _write_project(tmp_path / "Future")

    def _future(payload):
        payload["state"]["results_model"]["schema_version"] = "assessment-results/99.0"

    _rewrite(project, _future)
    future = read_project_summary(project)
    assert not future.valid and future.findings[0].code == "canonical_results_invalid"


# ---------------------------------------------------------------- reader fallbacks

def test_reader_falls_back_to_the_published_report_file(tmp_path):
    project = _write_project(tmp_path / "ReportOnly", partial=False)

    def _strip(payload):
        results = payload["state"].pop("results_model")
        report_dir = project.parent / "report"
        report_dir.mkdir(exist_ok=True)
        (report_dir / "assessment_results.json").write_text(
            json.dumps(results), encoding="utf-8")

    _rewrite(project, _strip)
    summary = read_project_summary(project)
    assert summary.valid
    assert any(f.code == "results_from_report_file" for f in summary.findings)
    assert summary.snapshot.observations["connectivity.turnovers_per_km"].baseline == 0.5


def test_reader_falls_back_to_retained_hz_artifacts(tmp_path):
    project = _write_project(tmp_path / "ArtifactsOnly")

    def _strip(payload):
        payload["state"].pop("results_model")

    _rewrite(project, _strip)
    _hz_artifacts(project.parent)
    summary = read_project_summary(project)
    assert summary.valid, [f.code for f in summary.findings]
    assert any(f.code == "results_from_artifacts" for f in summary.findings)
    turnover = summary.snapshot.observations["connectivity.turnovers_per_km"]
    assert turnover.baseline is not None and turnover.baseline > 0


def test_reader_without_any_source_reports_missing_results(tmp_path):
    project = _write_project(tmp_path / "Nothing")

    def _strip(payload):
        payload["state"].pop("results_model")

    _rewrite(project, _strip)
    summary = read_project_summary(project)
    assert not summary.valid
    assert any(f.code == "canonical_results_missing" for f in summary.findings)


# ---------------------------------------------------------------- results lifecycle

def test_current_alternatives_accepts_the_frozen_hash_spelling():
    snap = _snapshot()
    mf = _manifest(snap, partial=False)
    state = {"manifest": mf.model_dump(mode="json")}
    attached = results_lifecycle.current_alternatives(
        assessment_id=snap.assessment_id, input_hash="e" * 64, state=state,
        extra_hashes=frozenset({snap.input_hash}))
    assert attached is not None
    assert results_lifecycle.current_alternatives(
        assessment_id=snap.assessment_id, input_hash="e" * 64, state=state) is None
    assert results_lifecycle.current_alternatives(
        assessment_id=snap.assessment_id, input_hash=snap.input_hash,
        state=dict(state, halted_on="k_hi")) is None
    assert results_lifecycle.current_alternatives(
        assessment_id="someone-else", input_hash=snap.input_hash, state=state) is None


def test_with_current_alternatives_attaches_detaches_and_clears_the_envelope():
    snap = _snapshot()
    res = _results_for(snap)
    mf = _manifest(snap, partial=False)
    attached = results_lifecycle.with_current_alternatives(
        res, {"manifest": mf.model_dump(mode="json")})
    assert attached.alternatives is not None
    assert attached.function_envelope is None
    detached = results_lifecycle.with_current_alternatives(attached, None)
    assert detached.alternatives is None


# ---------------------------------------------------------------- collections

def test_collection_is_frozen_until_explicit_refresh_and_paths_are_portable(tmp_path):
    project = _write_project(tmp_path / "sources" / "ProjectA", turnover=0.5)
    collection_path = tmp_path / "collections" / "sites.hypecompare"
    collection = add_projects(new_collection("Sites"), [project],
                              comparison_path=collection_path)
    frozen_revision = collection.members[0].source_revision
    frozen_value = collection.members[0].snapshot.baseline_metrics[
        "connectivity.turnovers_per_km"]
    persisted = save_collection(collection, collection_path)
    assert persisted.members[0].source_relative
    reopened = load_collection(collection_path)
    assert reopened.members[0].snapshot.baseline_metrics[
        "connectivity.turnovers_per_km"] == frozen_value

    _write_project(project.parent, turnover=1.5)
    inspection = inspect_member(reopened.members[0], collection_path=collection_path)
    assert inspection.status == ComparisonSourceStatus.changed
    assert reopened.members[0].source_revision == frozen_revision
    assert reopened.members[0].snapshot.baseline_metrics[
        "connectivity.turnovers_per_km"] == 0.5

    refreshed = refresh_member(reopened.members[0], collection_path=collection_path)
    assert refreshed.source_status == ComparisonSourceStatus.ready
    assert refreshed.source_revision != frozen_revision
    assert refreshed.snapshot.baseline_metrics["connectivity.turnovers_per_km"] == 1.5


def test_missing_and_moved_inspection_preserve_frozen_snapshot(tmp_path):
    base = tmp_path / "portable"
    project = _write_project(base / "source", turnover=0.4)
    comparison_path = base / "collection.hypecompare"
    saved = save_collection(add_projects(new_collection(), [project],
                                         comparison_path=comparison_path), comparison_path)
    member = saved.members[0]
    moved_root = tmp_path / "moved"
    shutil.copytree(base, moved_root)
    moved_collection_path = moved_root / "collection.hypecompare"
    moved_collection = load_collection(moved_collection_path)
    moved = inspect_member(moved_collection.members[0], collection_path=moved_collection_path)
    assert moved.status == ComparisonSourceStatus.moved
    project.unlink()
    missing = inspect_member(member, collection_path=comparison_path)
    assert missing.status == ComparisonSourceStatus.missing
    assert member.snapshot.baseline_metrics["connectivity.turnovers_per_km"] == 0.4


def test_labels_disambiguate_and_payload_is_plot_ready(tmp_path):
    first = _write_project(tmp_path / "north" / "One", site="Shared")
    second = _write_project(tmp_path / "south" / "Two", site="Shared")
    collection = add_projects(new_collection(), [first, second])
    labels = [member.label for member in collection.members]
    assert len(set(labels)) == 2
    assert all(label.startswith("Shared ·") for label in labels)   # never an em dash
    payload = comparison_ui_payload(collection)
    assert payload["primary_metric_ids"] == list(PRIMARY_METRIC_IDS)
    assert payload["members"][0]["observations"]["connectivity.turnovers_per_km"][
        "baseline"] == 0.5
    assert payload["metrics"][0]["id"] == "connectivity.turnovers_per_km"
    assert payload["collection"]["view_settings"]["metric_ids"] == [
        "connectivity.turnovers_per_km"]


def test_duplicate_path_project_or_site_is_added_only_once(tmp_path):
    source = _write_project(tmp_path / "original")
    copied_dir = tmp_path / "copy"
    copied_dir.mkdir()
    copied = copied_dir / "copy.hype"
    shutil.copy2(source, copied)
    collection = add_projects(new_collection(), [source, source, copied])
    assert len(collection.members) == 1


# ---------------------------------------------------------------- exports

def test_full_export_is_source_free_and_contains_requested_artifacts(tmp_path):
    first = _write_project(tmp_path / "A", turnover=0.5, partial=False)
    second = _write_project(tmp_path / "B", site="Beta", turnover=1.2, partial=True)
    collection = add_projects(new_collection("Field sites"), [first, second])
    mtimes = {path: path.stat().st_mtime_ns for path in (first, second)}
    paths = generate_comparison_report(collection, tmp_path / "export", include_pdf=True)
    expected = {
        "html", "pdf", "site_summary_csv", "paper_plot_data_csv",
        "sensitivity_results_long_csv", "model_provenance_csv", "quality_control_csv",
        "comparison_snapshot_json", "overview_svg", "overview_png",
    }
    assert set(paths) == expected and all(Path(path).is_file() for path in paths.values())
    assert Path(paths["overview_png"]).stat().st_size > 10_000
    assert "Range across hydraulic alternatives" in Path(paths["html"]).read_text("utf-8")
    with Path(paths["paper_plot_data_csv"]).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 6
    assert {row["metric_id"] for row in rows} == set(PRIMARY_METRIC_IDS)
    with Path(paths["sensitivity_results_long_csv"]).open(encoding="utf-8",
                                                          newline="") as handle:
        long_rows = list(csv.DictReader(handle))
    assert any(row["scenario"] == "Lower gradient" and row["status"] == "failed"
               for row in long_rows)
    assert mtimes == {path: path.stat().st_mtime_ns for path in (first, second)}
    assert json.loads(Path(paths["comparison_snapshot_json"]).read_text("utf-8"))[
        "schema_version"] == "comparison-collection/1.0"
    repeated = generate_comparison_report(collection, tmp_path / "export-again",
                                          include_pdf=True)
    for key in expected:
        assert Path(paths[key]).read_bytes() == Path(repeated[key]).read_bytes(), key


def test_export_rejects_a_source_project_report_folder(tmp_path):
    project = _write_project(tmp_path / "Project")
    collection = add_projects(new_collection(), [project])
    with pytest.raises(ValueError, match="source project's report"):
        generate_comparison_report(collection, project.parent / "report", include_pdf=False)


def test_export_inspection_excludes_currently_invalid_source(tmp_path):
    first = _write_project(tmp_path / "Good")
    second = _write_project(tmp_path / "WillBreak", site="Broken")
    collection = add_projects(new_collection(), [first, second])
    second.write_text("corrupt after capture", encoding="utf-8")
    inspections = inspect_collection(collection)
    assert inspections[str(collection.members[1].member_id)].status \
        == ComparisonSourceStatus.invalid
    paths = generate_comparison_report(collection, tmp_path / "export-invalid",
                                       include_pdf=False, inspections=inspections)
    with Path(paths["paper_plot_data_csv"]).open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 3
    assert "source_unreadable" in Path(paths["quality_control_csv"]).read_text("utf-8")


def test_collection_loader_rejects_future_schema(tmp_path):
    path = tmp_path / "future.hypecompare"
    path.write_text(json.dumps({"schema_version": "comparison-collection/99.0"}), "utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        load_collection(path)


# ---------------------------------------------------------------- copy rules + recents

def test_workspace_and_exports_carry_no_em_dash_and_never_say_envelope(tmp_path):
    """The standing copy rules apply to the client renderer and every export byte."""
    js = (WWW / "comparison.js").read_text(encoding="utf-8")
    for token in ("—", "–", "\\u2014", "\\u2013"):
        assert token not in js, token
    project = _write_project(tmp_path / "Copy", partial=True)
    collection = add_projects(new_collection(), [project])
    paths = generate_comparison_report(collection, tmp_path / "export", include_pdf=False)
    html_text = Path(paths["html"]).read_text(encoding="utf-8")
    assert "—" not in html_text and "–" not in html_text
    assert "envelope" not in html_text.lower()
    # every reader finding is user-facing copy
    for probe in (tmp_path / "gone.hype", project):
        for finding in read_project_summary(probe).findings:
            assert "—" not in finding.message
            assert "envelope" not in finding.message.lower()


def test_comparison_recents_store_is_separate_from_projects(tmp_path, monkeypatch):
    monkeypatch.setenv("HYPE_DATA_ROOT", str(tmp_path))
    comparison = tmp_path / "sites.hypecompare"
    comparison.write_text("{}", encoding="utf-8")
    project = tmp_path / "site.hype"
    project.write_text("", encoding="utf-8")
    recents.touch(project)
    recents.touch_comparison(comparison)
    assert [i["path"] for i in recents.load_comparisons()] == [str(comparison.resolve())]
    assert [i["path"] for i in recents.load()] == [str(project.resolve())]
    recents.forget_comparison(comparison)
    assert recents.load_comparisons() == []
