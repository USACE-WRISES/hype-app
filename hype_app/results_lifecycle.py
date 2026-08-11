"""Canonical assessment-results lifecycle, independent of report rendering.

``assess.build_results`` owns the scientific derivation.  This module owns the two persistence
transitions around it:

* construct the canonical ``AssessmentResultsV2`` as soon as a completed HZ run is available;
* attach (or detach) the hydraulic-alternatives manifest as a sweep changes.

Keeping these operations free of Shiny and report code lets the application persist the same
validated snapshot that reports, comparisons, and exports consume.  A report is therefore a
downstream renderer, never the event that creates canonical results.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import assess
from .contracts import (
    AssessmentInputSnapshot,
    AssessmentResultsV2,
    HydraulicAlternativesManifest,
    migrate,
)


def _results_model(value: AssessmentResultsV2 | Mapping[str, Any]) -> AssessmentResultsV2:
    """Validate a model or persisted payload, applying the public results migration chain."""
    if isinstance(value, AssessmentResultsV2):
        return value
    return AssessmentResultsV2.model_validate(
        migrate("assessment-results", dict(value)))


def current_alternatives(
    *,
    assessment_id: str,
    input_hash: str,
    state: HydraulicAlternativesManifest | Mapping[str, Any] | None,
    extra_hashes: frozenset[str] = frozenset(),
) -> HydraulicAlternativesManifest | None:
    """Return the attachable alternatives manifest for one canonical Basecase.

    ``state`` may be either the manifest itself or the application's persisted wrapper
    (``{"manifest": ..., "running": bool, "halted_on": ...}``).  A manifest is current only
    after the sweep has settled, has at least one completed case, and matches *both* immutable
    Basecase identities.  Invalid or stale state is deliberately treated as unavailable: it
    must never make an otherwise valid hydraulic result unreadable.
    """
    if state is None:
        return None

    raw: HydraulicAlternativesManifest | Mapping[str, Any]
    if isinstance(state, HydraulicAlternativesManifest):
        raw = state
    elif isinstance(state, Mapping):
        # A halted sweep is still awaiting an explicit Retry / Continue / Stop decision.  Once
        # Stop finalizes it, ``halted_on`` disappears and the completed subset may attach as a
        # transparently partial design.
        if bool(state.get("running")) or state.get("halted_on"):
            return None
        nested = state.get("manifest")
        raw = nested if isinstance(nested, (HydraulicAlternativesManifest, Mapping)) else state
    else:
        return None

    try:
        manifest = (raw if isinstance(raw, HydraulicAlternativesManifest)
                    else HydraulicAlternativesManifest.model_validate(dict(raw)))
    except Exception:  # an alternatives failure must not invalidate the Basecase
        return None

    if not manifest.completed():
        return None
    # Missing identity is not a wildcard.  The comparison/source-reader contract explicitly
    # requires both values to match before alternative values may be merged into a Basecase.
    # `extra_hashes` carries the FROZEN snapshot's stored hash: the report path overlays
    # late-entered site metadata onto the snapshot, which re-stamps the results' computed
    # input_hash, while the sweep stays bound to the frozen spelling. The assessment_id
    # equality below still anchors both to the same run.
    acceptable = {input_hash, *extra_hashes}
    if not manifest.base_input_hash or manifest.base_input_hash not in acceptable:
        return None
    if not manifest.base_assessment_id or manifest.base_assessment_id != assessment_id:
        return None
    return manifest


def with_current_alternatives(
    results: AssessmentResultsV2 | Mapping[str, Any],
    state: HydraulicAlternativesManifest | Mapping[str, Any] | None,
    *,
    extra_hashes: frozenset[str] = frozenset(),
) -> AssessmentResultsV2:
    """Return a canonical copy whose alternatives attachment reflects ``state`` now.

    This operation is suitable for the alternatives completion handler: it does not re-run any
    hydraulic calculations.  The functional envelope is cleared because it is a report-time
    derivative of a particular alternatives manifest and would otherwise describe the previous
    sweep after the manifest is replaced or removed.
    """
    model = _results_model(results)
    manifest = current_alternatives(
        assessment_id=model.assessment_id, input_hash=model.input_hash, state=state,
        extra_hashes=extra_hashes)
    return model.model_copy(update={"alternatives": manifest, "function_envelope": None})


def build_canonical_results(
    snapshot: AssessmentInputSnapshot,
    *,
    alternatives_state: HydraulicAlternativesManifest | Mapping[str, Any] | None = None,
    extra_hashes: frozenset[str] = frozenset(),
    **build_kwargs: Any,
) -> AssessmentResultsV2:
    """Build and finalize the canonical model without generating a report.

    ``build_kwargs`` are the documented keyword arguments of :func:`assess.build_results`.  This
    deliberately thin seam keeps the scientific builder's API intact while giving lifecycle
    call sites one operation whose result is ready to persist and later hand to any renderer.
    """
    results = assess.build_results(snapshot, **build_kwargs)
    return with_current_alternatives(results, alternatives_state,
                                     extra_hashes=extra_hashes)


__all__ = [
    "build_canonical_results",
    "current_alternatives",
    "with_current_alternatives",
]
