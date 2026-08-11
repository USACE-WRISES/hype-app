"""Pure, read-only cross-project hydraulic comparison services.

Reading a ``.hype`` source goes through :func:`bundle.restore_in_place`, whose name reflects the
desktop open workflow but whose implementation only reads ZIP members.  No source is extracted,
adopted as ``work_dir``, or modified.  A ``.hypecompare`` file stores frozen, plot-ready snapshots;
inspection notices external changes while refresh is the only operation that replaces a snapshot.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from pydantic import ValidationError

from . import bundle
from .comparison_metrics import (
    HYDRAULIC_METRICS,
    METRICS_BY_ID,
    PRIMARY_METRIC_IDS,
    extract_metrics,
    is_finite_number,
)
from .contracts import (
    ALTERNATIVES_MANIFEST_SCHEMA_VERSION,
    RESULTS_SCHEMA_VERSION,
    AltStatus,
    AssessmentInputSnapshot,
    AssessmentResultsV2,
    HydraulicAlternativesManifest,
    migrate,
)
from .contracts.comparison import (
    COMPARISON_COLLECTION_SCHEMA_VERSION,
    ComparisonCollectionV1,
    ComparisonFindingV1,
    ComparisonMemberV1,
    ComparisonMetricObservationV1,
    ComparisonScenarioV1,
    ComparisonSnapshotV1,
    ComparisonSourceStatus,
)
from .hashing import canonical_json, changed_groups, stable_hash
from .provenance import Severity


BASECASE_LABEL = "Basecase"
DEFAULT_COLLECTION_NAME = "Untitled hydraulic comparison"


@dataclass(frozen=True, slots=True)
class ProjectComparisonSummary:
    """Structured result of probing one external project."""

    source_path: Path
    label: str
    snapshot: ComparisonSnapshotV1 | None
    findings: tuple[ComparisonFindingV1, ...] = ()

    @property
    def valid(self) -> bool:
        return self.snapshot is not None and self.snapshot.valid


@dataclass(frozen=True, slots=True)
class SourceInspection:
    member_id: str
    status: ComparisonSourceStatus
    resolved_path: Path | None
    current_revision: str | None
    findings: tuple[ComparisonFindingV1, ...] = ()


def _finding(code: str, message: str, severity: Severity = Severity.warning,
             **context) -> ComparisonFindingV1:
    return ComparisonFindingV1(code=code, message=message, severity=severity,
                               context=_json_safe(context))


def _json_safe(value):
    """Keep arbitrary provenance/QC dictionaries strict-JSON round-trippable."""
    if isinstance(value, float) and not is_finite_number(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    if hasattr(value, "model_dump"):
        return _json_safe(value.model_dump(mode="json"))
    return value


def _normal_path(path) -> Path:
    return Path(path).expanduser().resolve()


def _path_key(path) -> str:
    return os.path.normcase(str(_normal_path(path)))


def normalize_legacy_alternatives(data: Mapping | HydraulicAlternativesManifest | None) \
        -> dict | None:
    """Normalize the sole legacy alternatives shape before strict validation.

    The first alternatives build stored ``vary_k``/``vary_gradient`` instead of ``selection``.
    Per-scenario factors retained the actual design, so dropping those UI-only fields is lossless.
    """
    if data is None:
        return None
    if isinstance(data, HydraulicAlternativesManifest):
        return data.model_dump(mode="json")
    out = dict(data)
    out.pop("vary_k", None)
    out.pop("vary_gradient", None)
    out.setdefault("selection", {})
    return out


def _validate_manifest(data, *, results: AssessmentResultsV2,
                       findings: list[ComparisonFindingV1],
                       extra_hashes: frozenset[str] = frozenset()) \
        -> HydraulicAlternativesManifest | None:
    try:
        raw = normalize_legacy_alternatives(data)
    except (TypeError, ValueError) as exc:
        findings.append(_finding("alternatives_invalid",
                                 "The hydraulic alternatives manifest is invalid and was ignored.",
                                 Severity.warning, error=str(exc)))
        return None
    if not raw:
        return None
    version = raw.get("schema_version")
    if version and version != ALTERNATIVES_MANIFEST_SCHEMA_VERSION:
        findings.append(_finding(
            "alternatives_schema_unsupported",
            f"Hydraulic alternatives schema {version!s} is not supported by this version.",
            Severity.warning, schema_version=version))
        return None
    try:
        manifest = HydraulicAlternativesManifest.model_validate(raw)
    except ValidationError as exc:
        findings.append(_finding("alternatives_invalid",
                                 "The hydraulic alternatives manifest is invalid and was ignored.",
                                 Severity.warning, error=str(exc)))
        return None
    if any(not is_finite_number(scenario.k_factor)
           or not is_finite_number(scenario.g_factor) for scenario in manifest.scenarios):
        findings.append(_finding(
            "alternatives_invalid",
            "Hydraulic alternative factors must be finite numbers; the manifest was ignored.",
            Severity.warning))
        return None
    if any(scenario.status in (AltStatus.pending, AltStatus.running)
           for scenario in manifest.scenarios):
        findings.append(_finding(
            "alternatives_unsettled",
            "A pending or running alternatives sweep was not attached to the frozen baseline.",
            Severity.warning))
        return None
    # Both identity values are required here.  Older unbound sweeps remain visible in their source
    # project but cannot be scientifically attached to a cross-project baseline.
    # `extra_hashes` carries the FROZEN snapshot's stored input hash: additive snapshot-schema
    # growth re-stamps `results.input_hash` at build time, so real saved projects hold a
    # manifest bound to the frozen hash beside results stamped with the recomputed one. The
    # assessment_id equality and the stale-marks error still guard genuinely changed inputs.
    acceptable = {results.input_hash, *extra_hashes}
    if (manifest.base_input_hash not in acceptable
            or manifest.base_assessment_id != results.assessment_id):
        findings.append(_finding(
            "alternatives_identity_mismatch",
            "Hydraulic alternatives do not identify this baseline and were excluded.",
            Severity.warning,
            base_input_hash=manifest.base_input_hash,
            base_assessment_id=manifest.base_assessment_id))
        return None
    if not manifest.completed():
        findings.append(_finding(
            "alternatives_no_completed_cases",
            "Hydraulic alternatives contain no completed cases and were excluded.",
            Severity.warning))
        return None
    return manifest


def _results_payload(state: Mapping) -> dict | None:
    raw = state.get("results_model")
    if not isinstance(raw, Mapping):
        return None
    out = dict(raw)
    try:
        nested = normalize_legacy_alternatives(out.get("alternatives"))
    except (TypeError, ValueError):
        nested = out.get("alternatives")
    if nested is not None:
        out["alternatives"] = nested
    return out


def _report_results_payload(folder: Path) -> dict | None:
    """Fallback 1: the results copy a report build publishes beside the project (UTF-8)."""
    rj = folder / "report" / "assessment_results.json"
    try:
        if not rj.is_file():
            return None
        data = json.loads(rj.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    out = dict(data)
    try:
        nested = normalize_legacy_alternatives(out.get("alternatives"))
    except (TypeError, ValueError):
        nested = out.get("alternatives")
    if nested is not None:
        out["alternatives"] = nested
    return out


def _results_from_artifacts(folder: Path, payload: Mapping, state: Mapping) \
        -> AssessmentResultsV2 | None:
    """Fallback 2: re-derive results from the retained ``summary/hz`` artifacts, through the
    same adapter the alternatives sweep uses. Never raises; None falls through to the
    canonical_results_missing finding."""
    snap_dict = payload.get("assessment_input") or state.get("input_snapshot")
    if not isinstance(snap_dict, Mapping):
        return None
    hz = folder / "summary" / "hz"
    try:
        if not ((hz / "hz_stats.json").is_file() and (hz / "hz_flux.npz").is_file()):
            return None
    except OSError:
        return None
    try:
        # Local import, the alt_screening pattern: `import hype_app.report` raises when the
        # committed conceptual-figure assets are missing, and this module has no stake there.
        from . import alt_screening
        snap = AssessmentInputSnapshot.model_validate(dict(snap_dict))
        reach_m = snap.site.reach_length_m if snap.site else None
        return alt_screening.scenario_results(folder, snapshot=snap, reach_length_m=reach_m)
    except Exception:  # noqa: BLE001 -- an unreadable artifact set is just "no results"
        return None


def _error_quality(results: AssessmentResultsV2) -> bool:
    # ``weight_identity`` is an error in the optional functional-screening calculation, not in
    # the hydraulic baseline this workspace compares.  Keep it visible as a warning without
    # suppressing otherwise valid hydraulic points.
    non_hydraulic = {"weight_identity", "chain_closure", "function_envelope_unavailable"}
    if any(warning.severity == Severity.error and warning.code not in non_hydraulic
           for warning in results.warnings):
        return True

    def walk(value) -> bool:
        if isinstance(value, Mapping):
            if str(value.get("severity") or value.get("level") or "").lower() == "error":
                return True
            if value.get("errors"):
                return True
            return any(walk(child) for child in value.values())
        if isinstance(value, (list, tuple)):
            return any(walk(child) for child in value)
        return False

    return walk(results.quality_diagnostics)


def _scenario_records(manifest: HydraulicAlternativesManifest | None) \
        -> list[ComparisonScenarioV1]:
    if manifest is None:
        return []
    records: list[ComparisonScenarioV1] = []
    for scenario in manifest.scenarios:
        values = extract_metrics(scenario.results_sections) \
            if scenario.status == AltStatus.completed else {}
        # The first alternatives build persisted only these three primary values.  Full section
        # payloads win whenever present; the fallback keeps legacy sensitivity useful without
        # parsing report labels or guessing units.
        if scenario.status == AltStatus.completed:
            legacy = {
                "connectivity.turnovers_per_km": scenario.metrics.get("turnovers_per_km"),
                "residence_time.weighted_median_days": scenario.metrics.get("rtd_median_days"),
                "zone.equivalent_active_depth_m": scenario.metrics.get(
                    "equivalent_active_depth_m"),
            }
            for metric_id, raw_value in legacy.items():
                presented = METRICS_BY_ID[metric_id].present(raw_value)
                if metric_id not in values and presented is not None:
                    values[metric_id] = presented
        records.append(ComparisonScenarioV1(
            scenario_id=scenario.id,
            label=scenario.label,
            status=scenario.status,
            k_factor=scenario.k_factor,
            gradient_factor=scenario.g_factor,
            metrics=values,
            error=scenario.error,
        ))
    return records


def _observations(baseline: Mapping[str, float], scenarios: Sequence[ComparisonScenarioV1]) \
        -> dict[str, ComparisonMetricObservationV1]:
    configured = len(scenarios)
    completed = sum(scenario.status == AltStatus.completed for scenario in scenarios)
    observations: dict[str, ComparisonMetricObservationV1] = {}
    for definition in HYDRAULIC_METRICS:
        base = baseline.get(definition.id)
        cases: list[tuple[str, float]] = []
        if base is not None:
            cases.append((BASECASE_LABEL, base))
        incomplete: list[str] = [] if base is not None else [BASECASE_LABEL]
        for scenario in scenarios:
            value = scenario.metrics.get(definition.id)
            if scenario.status == AltStatus.completed and value is not None:
                cases.append((scenario.label, value))
            else:
                incomplete.append(scenario.label)

        values = [value for _, value in cases]
        lo = min(values) if values else None
        hi = max(values) if values else None
        if not values:
            completeness = "unavailable"
        elif base is None:
            completeness = "partial"
        elif configured == 0:
            completeness = "baseline_only"
        elif not incomplete:
            completeness = "complete"
        else:
            completeness = "partial"
        low_names = ([label for label, value in cases if value == lo] if lo is not None else [])
        high_names = ([label for label, value in cases if value == hi] if hi is not None else [])
        observations[definition.id] = ComparisonMetricObservationV1(
            metric_id=definition.id,
            unit=definition.presentation_unit,
            baseline=base,
            low=lo,
            high=hi,
            finite_case_count=len(cases),
            completed_scenario_count=completed,
            configured_scenario_count=configured,
            completeness=completeness,
            has_range=base is not None and len(cases) >= 2,
            low_scenarios=low_names,
            high_scenarios=high_names,
            incomplete_scenarios=incomplete,
        )
    return observations


def _design_signature(manifest: HydraulicAlternativesManifest | None) -> str | None:
    if manifest is None or not manifest.scenarios:
        return None
    # IDs/labels changed between early builds; the scientific design is the set of K/gradient
    # factor pairs, independent of order and UI vocabulary.
    design = sorted(({"k_factor": s.k_factor, "g_factor": s.g_factor}
                     for s in manifest.scenarios),
                    key=lambda row: (row["k_factor"], row["g_factor"]))
    return stable_hash(design)


def _compatibility(results: AssessmentResultsV2,
                   manifest: HydraulicAlternativesManifest | None,
                   state: Mapping | None = None) -> dict:
    snap = results.input_snapshot
    hz_state = (state or {}).get("hz_result")
    hz_stats = hz_state.get("stats") if isinstance(hz_state, Mapping) else None
    base_hz_knobs = hz_stats.get("knobs") if isinstance(hz_stats, Mapping) else None
    return _json_safe({
        "app_version": snap.app_version if snap else None,
        "model_version": snap.model_version if snap else None,
        "results_schema_version": results.schema_version,
        "active_volume_basis": results.zone.active_volume_basis,
        "discharge_normalization": "streamflow_cms",
        "grid": ({key: value for key, value in snap.grid.model_dump(mode="json").items()
                  if key not in ("particles_per_cell", "min_path_mult")} if snap else None),
        "domain": ({"reach_length_m": snap.site.reach_length_m,
                    "crs_epsg": snap.terrain.crs_epsg,
                    "dem_resolution_m": snap.terrain.dem_resolution_m} if snap else None),
        "gradient_method_version": snap.gradients.method_version if snap else None,
        "porosity": (results.residence_time.porosity
                     if results.residence_time.porosity is not None
                     else (snap.k.porosity if snap else None)),
        "hydraulic_conductivity": (snap.k.model_dump(mode="json") if snap else None),
        "gradient_method": (snap.gradients.model_dump(mode="json") if snap else None),
        "hz_knobs": manifest.hz_knobs if manifest and manifest.hz_knobs else base_hz_knobs,
        "alternatives_method_versions": manifest.method_versions if manifest else None,
        "alternative_design_signature": _design_signature(manifest),
    })


def _provenance(results: AssessmentResultsV2) -> dict:
    snap = results.input_snapshot
    if snap is None:
        return {}
    return _json_safe({
        "assessment_id": snap.assessment_id,
        "input_hash": results.input_hash,
        "created_at": snap.created_at.isoformat() if snap.created_at else None,
        "assessment_date": (snap.site.assessment_date.isoformat()
                            if snap.site.assessment_date else None),
        "streamflow": snap.streamflow.provenance.model_dump(mode="json"),
        "terrain": snap.terrain.model_dump(mode="json"),
    })


def _scientific_revision(results: AssessmentResultsV2,
                         baseline: Mapping[str, float],
                         scenarios: Sequence[ComparisonScenarioV1],
                         compatibility: Mapping) -> str:
    """Hash scientific content only (never mtimes, artifact paths, logs, or capture time)."""
    normalized_scenarios = [{
        "status": scenario.status.value,
        "k_factor": scenario.k_factor,
        "gradient_factor": scenario.gradient_factor,
        "metrics": scenario.metrics,
    } for scenario in scenarios]
    normalized_scenarios.sort(key=canonical_json)
    return stable_hash({
        "assessment_id": results.assessment_id,
        "input_hash": results.input_hash,
        "results_schema_version": results.schema_version,
        "baseline": dict(baseline),
        "scenarios": normalized_scenarios,
        "quality_diagnostics": _json_safe(results.quality_diagnostics),
        "warnings": _json_safe([warning.model_dump(mode="json")
                                for warning in results.warnings]),
        "compatibility": dict(compatibility),
    })


def _suggested_label(path: Path, *, site_name: str | None, project_name: str | None) -> str:
    # Project names are retained as provenance but are not part of the fallback ladder: a desktop
    # project's containing folder is the most stable human context after the explicit site name.
    return (str(site_name or "").strip() or path.parent.name.strip() or path.stem)


def _legacy_snapshot_hash(raw_input) -> str | None:
    """Recompute the pre-site-UUID snapshot hash without trusting its persisted computed field."""
    if not isinstance(raw_input, Mapping):
        return None
    body = {key: value for key, value in raw_input.items() if key != "input_hash"}
    site = body.get("site")
    if isinstance(site, Mapping):
        site = dict(site)
        # SiteMetadata.site_id was additive without a snapshot schema bump.  A model round-trip
        # can therefore materialize ``site_id: null`` around a hash computed before the field
        # existed; both shapes must recompute to the same legacy identity.
        if site.get("site_id") is None:
            site.pop("site_id", None)
        body["site"] = site
    return stable_hash(body)


def read_project_summary(path) -> ProjectComparisonSummary:
    """Read and validate a compact hydraulic summary without extracting or modifying a project."""
    source = _normal_path(path)
    findings: list[ComparisonFindingV1] = []
    fallback_label = source.parent.name or source.stem
    if not source.is_file():
        return ProjectComparisonSummary(
            source, fallback_label, None,
            (_finding("source_missing", "The source project could not be found.", Severity.error,
                      path=str(source)),))
    try:
        payload = bundle.restore_in_place(source)
    except Exception as exc:  # ProjectError + invalid/corrupt JSON are equally source-local
        return ProjectComparisonSummary(
            source, fallback_label, None,
            (_finding("source_unreadable", "The source is not a readable HYPE project.",
                      Severity.error, path=str(source), error=str(exc)),))

    state = payload.get("state") or {}
    project_name = str(state.get("project_name") or "").strip() or None
    # Source ladder: the saved snapshot is canonical; projects saved before results were
    # persisted at HZ completion fall back to the published report data, then to a
    # re-derivation from the retained hz artifacts. Fallback reads are disclosed as info
    # findings so provenance stays honest.
    raw_results = _results_payload(state)
    source_kind = "state"
    if raw_results is None:
        raw_results = _report_results_payload(source.parent)
        source_kind = "report"
    results, migrated = None, None
    if raw_results is not None:
        try:
            migrated = migrate("assessment-results", raw_results)
            version = migrated.get("schema_version")
            if version and version != RESULTS_SCHEMA_VERSION:
                raise ValueError(f"unsupported assessment results schema {version}")
            results = AssessmentResultsV2.model_validate(migrated)
        except Exception as exc:
            label = _suggested_label(source, site_name=None, project_name=project_name)
            return ProjectComparisonSummary(
                source, label, None,
                (_finding("canonical_results_invalid",
                          "The canonical hydraulic result snapshot is invalid or from a "
                          "newer schema.", Severity.error, error=str(exc)),))
        if source_kind == "report":
            findings.append(_finding(
                "results_from_report_file",
                "Values were read from the project's published report data because the "
                "project has no saved result snapshot. Open and save the project to "
                "refresh it.", Severity.info))
    else:
        results = _results_from_artifacts(source.parent, payload, state)
        if results is not None:
            migrated = results.model_dump(mode="json")
            findings.append(_finding(
                "results_from_artifacts",
                "Values were re-derived from the project's retained analysis artifacts "
                "because the project has no saved result snapshot. Open and save the "
                "project to refresh it.", Severity.info))
    if results is None:
        label = _suggested_label(source, site_name=None, project_name=project_name)
        return ProjectComparisonSummary(
            source, label, None,
            (_finding("canonical_results_missing",
                      "This project has no readable hydraulic results. Run the "
                      "Hyporheic Zone analysis and save the project first.", Severity.error),))

    # Prefer the snapshot embedded with results.  A separate config copy is accepted for older
    # project saves, but it still has to identify the exact result being compared.  Keep its raw
    # JSON beside the validated model so pre-site-UUID input hashes remain verifiable.
    input_model = results.input_snapshot
    raw_input = (migrated.get("input_snapshot") if isinstance(migrated, Mapping) else None)
    if input_model is None:
        raw_input = payload.get("assessment_input") or state.get("input_snapshot")
        try:
            input_model = AssessmentInputSnapshot.model_validate(raw_input) if raw_input else None
        except ValidationError as exc:
            findings.append(_finding("input_snapshot_invalid",
                                     "The frozen run inputs are invalid.", Severity.error,
                                     error=str(exc)))
    if input_model is None:
        findings.append(_finding("input_snapshot_missing",
                                 "The result has no frozen input snapshot.", Severity.error))
    else:
        if input_model.assessment_id != results.assessment_id:
            findings.append(_finding("assessment_identity_mismatch",
                                     "Result and input snapshot assessment IDs do not match.",
                                     Severity.error))
        if (input_model.input_hash != results.input_hash
                and _legacy_snapshot_hash(raw_input) != results.input_hash):
            findings.append(_finding("input_hash_mismatch",
                                     "Result and frozen input hashes do not match.", Severity.error))
        if results.group_hashes:
            stale = changed_groups(input_model.group_hashes(), results.group_hashes)
            if stale:
                findings.append(_finding(
                    "result_groups_stale",
                    "The canonical result no longer matches its frozen dependency groups.",
                    Severity.error, changed_groups=stale))
        else:
            findings.append(_finding(
                "group_hashes_missing",
                "Dependency-group hashes were not recorded; staleness could not be fully verified.",
                Severity.warning))
        if results.input_snapshot is None:
            results = results.model_copy(update={"input_snapshot": input_model})

    stale_marks = {str(mark).lower() for mark in (state.get("stale_marks") or [])}
    if stale_marks.intersection({"gw", "hz", "groundwater", "hyporheic"}):
        findings.append(_finding(
            "project_results_stale",
            "Groundwater or Hyporheic Zone results are marked stale in the source project.",
            Severity.error, stale_marks=sorted(stale_marks)))

    non_hydraulic_errors = {"weight_identity", "chain_closure",
                             "function_envelope_unavailable"}
    for warning in results.warnings:
        severity = (Severity.warning if warning.severity == Severity.error
                    and warning.code in non_hydraulic_errors else warning.severity)
        findings.append(_finding(warning.code, warning.message, severity, **warning.context))
    if _error_quality(results):
        findings.append(_finding(
            "hydraulic_quality_error",
            "The canonical result contains an error-level quality-control finding.", Severity.error))

    # Every stored spelling of the frozen snapshot's hash is an acceptable Basecase identity
    # for the manifest (see _validate_manifest).
    extra_hashes: set[str] = set()
    for cand in (raw_input, payload.get("assessment_input"), state.get("input_snapshot")):
        if isinstance(cand, Mapping) and cand.get("input_hash"):
            extra_hashes.add(str(cand["input_hash"]))
    legacy_hash = _legacy_snapshot_hash(raw_input)
    if legacy_hash:
        extra_hashes.add(legacy_hash)
    extra = frozenset(extra_hashes)

    # The state manifest is the freshest attachment; the results-embedded manifest (attached
    # by a report build) is the fallback when the state copy is absent or does not attach.
    # Failure findings surface only when NO candidate attaches, so a healthy embedded
    # manifest is not shadowed by a noisy state-copy warning.
    alt_wrapper = state.get("alt_result") if isinstance(state.get("alt_result"), Mapping) else None
    state_alt = (alt_wrapper or {}).get("manifest")
    unsettled = bool(alt_wrapper and (alt_wrapper.get("running") or alt_wrapper.get("halted_on")))
    manifest = None
    if unsettled:
        findings.append(_finding(
            "alternatives_unsettled",
            "A running or halted alternatives sweep was not attached to the frozen baseline.",
            Severity.warning))
    else:
        first_failure: list[ComparisonFindingV1] = []
        for candidate in (state_alt, results.alternatives):
            if candidate is None:
                continue
            trial: list[ComparisonFindingV1] = []
            manifest = _validate_manifest(candidate, results=results, findings=trial,
                                          extra_hashes=extra)
            if manifest is not None:
                break
            if trial and not first_failure:
                first_failure = trial
        if manifest is None:
            findings.extend(first_failure)

    # Extract from raw canonical JSON so a malformed JSON boolean cannot become 1.0 through
    # Pydantic's otherwise useful numeric coercion.  Comparison values are finite non-booleans.
    baseline = extract_metrics(migrated)
    scenarios = _scenario_records(manifest)
    compatibility = _compatibility(results, manifest, state)
    revision = _scientific_revision(results, baseline, scenarios, compatibility)
    invalid = any(finding.severity == Severity.error for finding in findings)
    readiness = "invalid" if invalid else ("warning" if findings else "ready")
    site_name = results.input_snapshot.site.site_name if results.input_snapshot else None
    label = _suggested_label(source, site_name=site_name, project_name=project_name)
    snapshot = ComparisonSnapshotV1(
        source_revision=revision,
        results_schema_version=results.schema_version,
        alternatives_schema_version=manifest.schema_version if manifest else None,
        assessment_id=results.assessment_id,
        input_hash=results.input_hash,
        project_id=(str(state.get("project_id")) if state.get("project_id") else None),
        site_id=(str(state.get("site_id")) if state.get("site_id") else
                 (results.input_snapshot.site.site_id if results.input_snapshot else None)),
        site_name=site_name,
        project_name=project_name,
        run_date=results.created_at or (results.input_snapshot.created_at
                                        if results.input_snapshot else None),
        valid=not invalid,
        readiness=readiness,
        baseline_metrics=baseline,
        scenarios=scenarios,
        observations=_observations(baseline, scenarios),
        findings=findings,
        quality_diagnostics=_json_safe(results.quality_diagnostics),
        provenance=_provenance(results),
        compatibility=compatibility,
    )
    return ProjectComparisonSummary(source, label, snapshot, tuple(findings))


def _relative_source(source: Path, comparison_path) -> str | None:
    if comparison_path is None:
        return None
    try:
        return Path(os.path.relpath(source, Path(comparison_path).resolve().parent)).as_posix()
    except (OSError, ValueError):  # Windows cross-drive paths cannot be relative
        return None


def member_from_project(path, *, comparison_path=None) -> ComparisonMemberV1:
    summary = read_project_summary(path)
    snapshot = summary.snapshot
    return ComparisonMemberV1(
        project_id=snapshot.project_id if snapshot else None,
        site_id=snapshot.site_id if snapshot else None,
        source_relative=_relative_source(summary.source_path, comparison_path),
        source_absolute=str(summary.source_path),
        label=summary.label,
        source_revision=snapshot.source_revision if snapshot else None,
        source_status=(ComparisonSourceStatus.ready if summary.valid
                       else ComparisonSourceStatus.invalid),
        source_findings=([] if snapshot is not None else list(summary.findings)),
        snapshot=snapshot,
    )


def _disambiguate_labels(members: Sequence[ComparisonMemberV1]) -> list[ComparisonMemberV1]:
    bases: list[str] = []
    for member in members:
        snapshot = member.snapshot
        bases.append(_suggested_label(
            Path(member.source_absolute),
            site_name=snapshot.site_name if snapshot else None,
            project_name=snapshot.project_name if snapshot else None))
    counts = {base.casefold(): sum(other.casefold() == base.casefold() for other in bases)
              for base in bases}
    proposed: list[str] = []
    for base, member in zip(bases, members):
        if counts[base.casefold()] == 1:
            proposed.append(base)
        else:
            proposed.append(f"{base} · {Path(member.source_absolute).parent.name}")
    second_counts = {label.casefold(): sum(other.casefold() == label.casefold()
                                           for other in proposed)
                     for label in proposed}
    out = []
    for label, member in zip(proposed, members):
        if second_counts[label.casefold()] > 1:
            label = f"{label} · {Path(member.source_absolute).name}"
        out.append(member.model_copy(update={"label": label}))
    return out


def new_collection(name: str = DEFAULT_COLLECTION_NAME) -> ComparisonCollectionV1:
    return ComparisonCollectionV1(name=str(name).strip() or DEFAULT_COLLECTION_NAME)


def add_projects(collection: ComparisonCollectionV1, paths: Iterable,
                 *, comparison_path=None) -> ComparisonCollectionV1:
    """Capture sources in order; duplicate paths/projects/sites are idempotently ignored."""
    members = list(collection.members)
    known = {_path_key(member.source_absolute) for member in members}
    project_ids = {member.project_id for member in members if member.project_id}
    site_ids = {member.site_id for member in members if member.site_id}
    for path in paths:
        key = _path_key(path)
        if key in known:
            continue
        candidate = member_from_project(path, comparison_path=comparison_path)
        if ((candidate.project_id and candidate.project_id in project_ids)
                or (candidate.site_id and candidate.site_id in site_ids)):
            continue
        members.append(candidate)
        known.add(key)
        if candidate.project_id:
            project_ids.add(candidate.project_id)
        if candidate.site_id:
            site_ids.add(candidate.site_id)
    return collection.model_copy(update={
        "members": _disambiguate_labels(members),
        "updated_at": datetime.now(timezone.utc),
    })


def _resolve_source(member: ComparisonMemberV1, comparison_path=None) -> tuple[Path | None, bool]:
    candidates: list[tuple[Path, bool]] = []
    if member.source_relative and comparison_path is not None:
        candidates.append((Path(comparison_path).resolve().parent / member.source_relative, True))
    candidates.append((Path(member.source_absolute), False))
    seen: set[str] = set()
    for candidate, relative in candidates:
        key = os.path.normcase(str(candidate.resolve()))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file():
            moved = _path_key(candidate) != _path_key(member.source_absolute)
            return candidate.resolve(), moved
    return None, False


def inspect_member(member: ComparisonMemberV1, *, collection_path=None) -> SourceInspection:
    resolved, moved = _resolve_source(member, collection_path)
    if resolved is None:
        finding = _finding("source_missing", "The source project could not be found.",
                           Severity.warning, path=member.source_absolute)
        return SourceInspection(str(member.member_id), ComparisonSourceStatus.missing,
                                None, None, (finding,))
    summary = read_project_summary(resolved)
    if not summary.valid:
        return SourceInspection(str(member.member_id), ComparisonSourceStatus.invalid,
                                resolved, None, summary.findings)
    revision = summary.snapshot.source_revision
    # Inspection reports only the relationship to the frozen source.  Scientific/QC findings
    # remain frozen in the captured snapshot until Refresh.
    findings: list[ComparisonFindingV1] = []
    if revision != member.source_revision:
        findings.append(_finding("source_changed",
                                 "The source has changed since this snapshot was captured.",
                                 Severity.warning))
        status = ComparisonSourceStatus.changed
    elif moved:
        findings.append(_finding("source_moved",
                                 "The source was resolved at a different absolute location.",
                                 Severity.info, path=str(resolved)))
        status = ComparisonSourceStatus.moved
    else:
        status = ComparisonSourceStatus.ready
    return SourceInspection(str(member.member_id), status, resolved, revision, tuple(findings))


def inspect_collection(collection: ComparisonCollectionV1, *, collection_path=None) \
        -> dict[str, SourceInspection]:
    return {str(member.member_id): inspect_member(member, collection_path=collection_path)
            for member in collection.members}


def refresh_member(member: ComparisonMemberV1, *, collection_path=None,
                   source_path=None) -> ComparisonMemberV1:
    """Explicitly replace one frozen snapshot; invalid refreshes preserve the prior snapshot."""
    if source_path is None:
        resolved, _ = _resolve_source(member, collection_path)
        if resolved is None:
            return member.model_copy(update={
                "source_status": ComparisonSourceStatus.missing,
                "source_findings": [_finding("source_missing",
                                             "The source project could not be found.")],
            })
    else:
        resolved = _normal_path(source_path)
    summary = read_project_summary(resolved)
    if not summary.valid:
        return member.model_copy(update={
            "source_status": ComparisonSourceStatus.invalid,
            "source_findings": list(summary.findings),
        })
    snapshot = summary.snapshot
    return member.model_copy(update={
        "project_id": snapshot.project_id,
        "site_id": snapshot.site_id,
        "source_relative": _relative_source(summary.source_path, collection_path),
        "source_absolute": str(summary.source_path),
        "label": summary.label,
        "source_revision": snapshot.source_revision,
        "source_status": ComparisonSourceStatus.ready,
        "source_findings": [],
        "snapshot": snapshot,
    })


def refresh_collection(collection: ComparisonCollectionV1, *, collection_path=None,
                       member_ids: Iterable[str] | None = None) -> ComparisonCollectionV1:
    selected = None if member_ids is None else {str(member_id) for member_id in member_ids}
    members = [
        refresh_member(member, collection_path=collection_path)
        if selected is None or str(member.member_id) in selected else member
        for member in collection.members
    ]
    return collection.model_copy(update={
        "members": _disambiguate_labels(members),
        "updated_at": datetime.now(timezone.utc),
    })


def relink_member(member: ComparisonMemberV1, source_path, *, collection_path=None,
                  refresh: bool = False) -> ComparisonMemberV1:
    """Point a member at a moved project; replacing values remains an explicit option."""
    source = _normal_path(source_path)
    if refresh:
        return refresh_member(member, collection_path=collection_path, source_path=source)
    summary = read_project_summary(source)
    if not summary.valid:
        return member.model_copy(update={
            "source_status": ComparisonSourceStatus.invalid,
            "source_findings": list(summary.findings),
        })
    changed = summary.snapshot.source_revision != member.source_revision
    return member.model_copy(update={
        "source_relative": _relative_source(source, collection_path),
        "source_absolute": str(source),
        "source_status": (ComparisonSourceStatus.changed if changed
                          else ComparisonSourceStatus.ready),
        "source_findings": ([_finding(
            "source_changed", "The relinked source differs from the frozen snapshot.")]
            if changed else []),
    })


def save_collection(collection: ComparisonCollectionV1, path) -> ComparisonCollectionV1:
    """Atomically save human-readable JSON and return the exact persisted collection."""
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    members = []
    for member in collection.members:
        source = _normal_path(member.source_absolute)
        members.append(member.model_copy(update={
            "source_absolute": str(source),
            "source_relative": _relative_source(source, target),
        }))
    persisted = collection.model_copy(update={
        "members": members,
        "name": target.stem if collection.name == DEFAULT_COLLECTION_NAME else collection.name,
        "updated_at": datetime.now(timezone.utc),
    })
    data = persisted.model_dump_json(indent=2)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp",
                                    dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return persisted


def load_collection(path) -> ComparisonCollectionV1:
    source = Path(path).expanduser().resolve()
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("That file is not a readable HYPE comparison collection.") from exc
    version = data.get("schema_version") if isinstance(data, dict) else None
    if version != COMPARISON_COLLECTION_SCHEMA_VERSION:
        raise ValueError(f"Unsupported comparison collection schema: {version!s}")
    try:
        return ComparisonCollectionV1.model_validate(data)
    except ValidationError as exc:
        raise ValueError("The HYPE comparison collection is invalid.") from exc


def collection_findings(collection: ComparisonCollectionV1,
                        inspections: Mapping[str, SourceInspection] | None = None) \
        -> list[ComparisonFindingV1]:
    """Compatibility warnings across currently included, valid frozen snapshots."""
    inspections = inspections or {}
    snapshots = [member.snapshot for member in collection.members
                 if member.included and member.snapshot is not None and member.snapshot.valid
                 and (inspections.get(str(member.member_id)).status
                      if inspections.get(str(member.member_id)) else member.source_status)
                 != ComparisonSourceStatus.invalid]
    if len(snapshots) < 2:
        return []
    labels = {
        "app_version": "application versions",
        "model_version": "model versions",
        "results_schema_version": "hydraulic result schemas",
        "gradient_method_version": "gradient method versions",
        "alternatives_method_versions": "hydraulic-alternative method versions",
        "active_volume_basis": "active-volume bases",
        "discharge_normalization": "discharge normalizations",
        "grid": "grid/domain settings",
        "domain": "modeled domain settings",
        "hz_knobs": "Hyporheic Zone settings",
        "porosity": "porosity values",
        "alternative_design_signature": "hydraulic-alternative designs",
    }
    out: list[ComparisonFindingV1] = []
    for key, label in labels.items():
        values = {canonical_json(snapshot.compatibility.get(key)) for snapshot in snapshots}
        if len(values) > 1:
            message = f"Included sites use different {label}."
            if key == "alternative_design_signature":
                message += " Sensitivity range widths are not directly comparable."
            out.append(_finding(f"compatibility_{key}", message, Severity.warning))
    return out


def plottable_members(collection: ComparisonCollectionV1,
                      inspections: Mapping[str, SourceInspection] | None = None) \
        -> list[ComparisonMemberV1]:
    """Frozen snapshots remain usable when a source is changed/missing/moved; invalid do not."""
    inspections = inspections or {}
    return [member for member in collection.members
            if member.included and member.snapshot is not None and member.snapshot.valid
            and (inspections.get(str(member.member_id)).status
                 if inspections.get(str(member.member_id)) else member.source_status)
            != ComparisonSourceStatus.invalid]


def member_display_label(member: ComparisonMemberV1) -> str:
    return str(member.alias or "").strip() or member.label


def comparison_ui_payload(collection: ComparisonCollectionV1,
                          inspections: Mapping[str, SourceInspection] | None = None) -> dict:
    """JSON-ready payload for the comparison workspace; all values come from frozen snapshots."""
    inspections = inspections or {}
    members = []
    for order, member in enumerate(collection.members):
        inspection = inspections.get(str(member.member_id))
        snapshot = member.snapshot
        status = inspection.status if inspection else member.source_status
        status_value = status.value
        if (status == ComparisonSourceStatus.ready and snapshot is not None
                and snapshot.readiness == "warning"):
            status_value = "warning"
        members.append({
            "member_id": str(member.member_id),
            "order": order,
            "label": member_display_label(member),
            "alias": member.alias,
            "included": member.included,
            "status": status_value,
            "source_path": str(inspection.resolved_path) if inspection and inspection.resolved_path
                           else member.source_absolute,
            "captured_at": snapshot.captured_at.isoformat() if snapshot else None,
            "run_date": snapshot.run_date.isoformat() if snapshot and snapshot.run_date else None,
            "readiness": snapshot.readiness if snapshot else "invalid",
            "alternatives": {
                "completed": (sum(s.status == AltStatus.completed for s in snapshot.scenarios)
                              if snapshot else 0),
                "configured": len(snapshot.scenarios) if snapshot else 0,
            },
            "observations": ({key: value.model_dump(mode="json")
                              for key, value in snapshot.observations.items()}
                             if snapshot else {}),
            "compatibility": snapshot.compatibility if snapshot else {},
            "findings": [finding.model_dump(mode="json") for finding in (
                list(snapshot.findings if snapshot else [])
                + list(inspection.findings if inspection else member.source_findings))],
        })
    return {
        "collection": {
            "collection_id": str(collection.collection_id),
            "name": collection.name,
            "view_settings": collection.view_settings.model_dump(mode="json"),
        },
        "primary_metric_ids": list(PRIMARY_METRIC_IDS),
        "metrics": [{
            "id": definition.id,
            "dimension": definition.dimension,
            "label": definition.label,
            "unit": definition.presentation_unit,
            "log_eligible": definition.log_eligible,
        } for definition in HYDRAULIC_METRICS],
        "members": members,
        "findings": [finding.model_dump(mode="json")
                     for finding in collection_findings(collection, inspections)],
    }


def generate_comparison_report(collection: ComparisonCollectionV1, out_dir,
                               *, include_pdf: bool = True,
                               inspections: Mapping[str, SourceInspection] | None = None) \
        -> dict[str, str]:
    """Lazy public facade, avoiding a module cycle while keeping one app-facing API."""
    from .comparison_report import generate_comparison_report as _generate
    return _generate(collection, out_dir, include_pdf=include_pdf, inspections=inspections)


__all__ = [
    "BASECASE_LABEL", "DEFAULT_COLLECTION_NAME", "ProjectComparisonSummary", "SourceInspection",
    "normalize_legacy_alternatives", "read_project_summary", "member_from_project",
    "new_collection", "add_projects", "inspect_member", "inspect_collection",
    "refresh_member", "refresh_collection", "relink_member", "save_collection",
    "load_collection", "collection_findings", "plottable_members", "member_display_label",
    "comparison_ui_payload",
    "generate_comparison_report",
]
