"""Gradient-sensitivity scenario generation + aggregation (revision spec §10).

Pure and Shiny-independent. Builds the scenario set (linked / one-at-a-time / left-right crossed /
custom) from a base `GradientBoundaryConfigV2`'s lower/preferred/upper gradients, deduplicates by a
canonical scenario hash (§10.2), and aggregates completed-scenario metrics into preferred / min /
max (with producing scenario) / range / %-change / success-fail counts, plus the dominant capacity
contributor (§10.5). Execution (running MODFLOW/HZ per scenario) is orchestrated by the app; the
sequencing policy lives here as `plan_execution_order`.
"""
from __future__ import annotations

from .contracts import (
    GeneratorType,
    GradientBoundaryConfigV2,
    GradientControl,
    ScenarioSpec,
    SensitivityScenarioManifest,
)
from .hashing import stable_hash


def _bound(control: GradientControl, which: str) -> float:
    if which == "lower":
        return control.lower if control.lower is not None else control.preferred
    if which == "upper":
        return control.upper if control.upper is not None else control.preferred
    return control.preferred


def _apply(config: GradientBoundaryConfigV2, *, left_which: str, right_which: str
           ) -> GradientBoundaryConfigV2:
    """New config with each side's control gradients set to the chosen lower/preferred/upper."""
    def _side(controls, which):
        return [c.model_copy(update={"preferred": _bound(c, which)}) for c in controls]
    return config.model_copy(update={
        "left_controls": _side(config.left_controls, left_which),
        "right_controls": _side(config.right_controls, right_which)})


def canonical_scenario_hash(config: GradientBoundaryConfigV2) -> str:
    """Stable hash of the gradient values that define a scenario (for dedup, §10.2). The method
    version is part of the key: the same gradients under a different head-anchor rule are a
    different scenario, so restored results never mix anchor semantics with fresh runs."""
    key = {
        "method": config.method_version,
        "left": [(c.station, c.preferred) for c in sorted(config.left_controls,
                                                          key=lambda c: c.station)],
        "right": [(c.station, c.preferred) for c in sorted(config.right_controls,
                                                           key=lambda c: c.station)],
    }
    return stable_hash(key)


def generate_scenarios(base: GradientBoundaryConfigV2, generator: GeneratorType,
                       *, custom: list[GradientBoundaryConfigV2] | None = None
                       ) -> list[ScenarioSpec]:
    """Build the deduplicated, preferred-first scenario list for a generator (§10.2)."""
    variants: list[tuple[str, GradientBoundaryConfigV2]] = []
    pref = _apply(base, left_which="preferred", right_which="preferred")
    variants.append(("Preferred", pref))

    if generator == GeneratorType.linked:
        variants.append(("Lower (all controls)", _apply(base, left_which="lower", right_which="lower")))
        variants.append(("Upper (all controls)", _apply(base, left_which="upper", right_which="upper")))
    elif generator == GeneratorType.one_at_a_time:
        for side_name, side_attr in (("left", "left_controls"), ("right", "right_controls")):
            controls = getattr(base, side_attr)
            for i, c in enumerate(controls):
                for which in ("lower", "upper"):
                    new = [cc.model_copy(update={"preferred": (_bound(cc, which) if j == i
                                                               else cc.preferred)})
                           for j, cc in enumerate(controls)]
                    cfg = base.model_copy(update={side_attr: new})
                    variants.append((f"{side_name} {c.id} {which}", cfg))
    elif generator == GeneratorType.crossed:
        for lw in ("lower", "preferred", "upper"):
            for rw in ("lower", "preferred", "upper"):
                variants.append((f"L:{lw} × R:{rw}", _apply(base, left_which=lw, right_which=rw)))
    elif generator == GeneratorType.custom:
        for i, cfg in enumerate(custom or []):
            variants.append((f"Custom {i + 1}", cfg))

    # dedup by canonical hash, preferred first
    seen: set[str] = set()
    specs: list[ScenarioSpec] = []
    for idx, (label, cfg) in enumerate(variants):
        h = canonical_scenario_hash(cfg)
        if h in seen:
            continue
        seen.add(h)
        specs.append(ScenarioSpec(id=f"s{idx}", label=label, is_preferred=(idx == 0),
                                  gradients=cfg, canonical_hash=h))
    # ensure exactly one preferred flag (the first surviving scenario)
    if specs and not any(s.is_preferred for s in specs):
        specs[0].is_preferred = True
    return specs


def build_manifest(base: GradientBoundaryConfigV2, generator: GeneratorType,
                   *, max_scenarios: int = 25, custom=None) -> SensitivityScenarioManifest:
    specs = generate_scenarios(base, generator, custom=custom)
    warnings = []
    if len(specs) > max_scenarios:
        from .provenance import HypeWarning, Severity
        warnings.append(HypeWarning(
            code="scenario_cap", severity=Severity.warning,
            message=f"{len(specs)} scenarios exceeds the cap of {max_scenarios}; "
                    f"only the first {max_scenarios} will run."))
        specs = specs[:max_scenarios]
    preferred = next((s.id for s in specs if s.is_preferred), specs[0].id if specs else "")
    return SensitivityScenarioManifest(generator=generator, preferred_id=preferred,
                                       scenarios=specs, max_scenarios=max_scenarios,
                                       warnings=warnings)


def plan_execution_order(manifest: SensitivityScenarioManifest) -> list[str]:
    """Scenario ids in execution order: preferred FIRST (stop if it fails), then the rest (§10.3)."""
    ids = [s.id for s in manifest.scenarios]
    if manifest.preferred_id in ids:
        ids = [manifest.preferred_id] + [i for i in ids if i != manifest.preferred_id]
    return ids


def aggregate_metric(scenarios: list[ScenarioSpec], key: str, preferred_id: str) -> dict | None:
    """min/max (+producing scenario) / range / %Δ over completed scenarios for one metric (§10.5)."""
    vals = [(s.id, s.metrics.get(key)) for s in scenarios
            if s.status.value == "completed" and isinstance(s.metrics.get(key), (int, float))]
    if not vals:
        return None
    pref = next((v for sid, v in vals if sid == preferred_id), None)
    lo_id, lo = min(vals, key=lambda t: t[1])
    hi_id, hi = max(vals, key=lambda t: t[1])
    out = {"preferred": pref, "min": lo, "min_scenario": lo_id, "max": hi, "max_scenario": hi_id,
           "range": hi - lo, "n_success": len(vals)}
    if pref not in (None, 0):
        out["pct_change_max"] = 100.0 * (hi - pref) / abs(pref)
        out["pct_change_min"] = 100.0 * (lo - pref) / abs(pref)
    return out


__all__ = [
    "canonical_scenario_hash", "generate_scenarios", "build_manifest", "plan_execution_order",
    "aggregate_metric",
]
