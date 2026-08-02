"""Hydraulic Alternatives: order-of-magnitude K / gradient sweep logic.

Pure and Shiny-independent. Builds the fixed scenario set from the two vary toggles, scales
gradient profile strings, and aggregates completed-scenario metrics into ranges keyed by the
CURRENT report.metric_rows vocabulary (so a saved manifest survives metric label renames).
Execution (running MODFLOW/HZ per scenario) is orchestrated by the app via alt_run.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .contracts import (
    AltScenario,
    AltStatus,
    HydraulicAlternativesManifest,
)

#: Default multipliers for the four variants (user spec: K an order of magnitude each way,
#: gradient half and double). `combos` = include the crossed K x gradient scenarios.
DEFAULT_SELECTION: dict = {"k_lower": 0.1, "k_upper": 10.0,
                           "g_lower": 0.5, "g_higher": 2.0, "combos": True}

#: Single-factor roles in execution order (singles run before combos so an early stop stays
#: interpretable): (role key, label, is_k).
ROLE_ORDER: tuple[tuple[str, str, bool], ...] = (
    ("k_upper", "Higher K", True),
    ("k_lower", "Lower K", True),
    ("g_higher", "Higher gradient", False),
    ("g_lower", "Lower gradient", False),
)

#: Combined scenarios in execution order: (K role, gradient role, label).
COMBO_ORDER: tuple[tuple[str, str, str], ...] = (
    ("k_upper", "g_higher", "Higher K + higher gradient"),
    ("k_upper", "g_lower", "Higher K + lower gradient"),
    ("k_lower", "g_higher", "Lower K + higher gradient"),
    ("k_lower", "g_lower", "Lower K + lower gradient"),
)

#: The three signature primaries stored per scenario (runs-table columns).
PRIMARY_KEYS: tuple[str, ...] = (
    "turnovers_per_km", "rtd_median_days", "equivalent_active_depth_m")

#: Human names for validation messages.
_ROLE_NAME = {"k_lower": "K lower", "k_upper": "K upper",
              "g_lower": "Gradient lower", "g_higher": "Gradient higher"}


def factor_text(v) -> str:
    """Compact factor cell text with the times sign, e.g. 10 -> "×10", 0.5 -> "×0.5" —
    the same notation the sweep-settings inputs carry, so both surfaces read identically.
    (Report factor strings stay ASCII; this is display-only.)"""
    return f"×{float(v):g}"


def validate_selection(sel: dict) -> list[str]:
    """User-facing errors for a selection dict {role: multiplier|None (off), "combos": bool}.

    The constraints are the user's: no zero or negative multipliers, the lower variants
    strictly below the Basecase (x1) and the upper variants strictly above it, and at least
    one variant selected. An invalid selection never reaches the engine."""
    errs: list[str] = []
    picked = 0
    for role, _label, _is_k in ROLE_ORDER:
        v = sel.get(role)
        if v is None:
            continue
        picked += 1
        name = _ROLE_NAME[role]
        try:
            x = float(v)
        except (TypeError, ValueError):
            errs.append(f"Enter a multiplier for {name}.")
            continue
        if not (x > 0.0) or x != x:      # x != x catches NaN
            errs.append(f"{name} must be above 0.")
        elif role.endswith("lower") and x >= 1.0:
            errs.append(f"{name} must be below 1 (lower than the Basecase).")
        elif not role.endswith("lower") and x <= 1.0:
            errs.append(f"{name} must be above 1 (higher than the Basecase).")
    if picked == 0:
        errs.append("Select at least one variation.")
    return errs


def build_scenarios(sel: dict) -> list[AltScenario]:
    """The ordered scenario list for a VALID selection: selected singles first, then (when
    combos is on) every selected K variant crossed with every selected gradient variant.
    Ids are role-based folder slugs (multipliers are user-set, so values cannot name dirs)."""
    out: list[AltScenario] = []
    for role, label, is_k in ROLE_ORDER:
        v = sel.get(role)
        if v is None:
            continue
        out.append(AltScenario(
            id=role, label=label,
            k_factor=float(v) if is_k else 1.0,
            g_factor=1.0 if is_k else float(v),
            rel_dir=f"alternatives/{role}"))
    if sel.get("combos", True):
        for k_role, g_role, label in COMBO_ORDER:
            kv, gv = sel.get(k_role), sel.get(g_role)
            if kv is None or gv is None:
                continue
            sid = f"{k_role}_{g_role}"
            out.append(AltScenario(id=sid, label=label, k_factor=float(kv),
                                   g_factor=float(gv), rel_dir=f"alternatives/{sid}"))
    return out


def build_manifest(sel: dict, *,
                   base_input_hash: str | None = None,
                   base_assessment_id: str | None = None,
                   app_version: str | None = None,
                   method_versions: dict | None = None,
                   hz_knobs: dict | None = None) -> HydraulicAlternativesManifest:
    return HydraulicAlternativesManifest(
        selection={k: v for k, v in sel.items()},
        base_input_hash=base_input_hash, base_assessment_id=base_assessment_id,
        app_version=app_version, method_versions=dict(method_versions or {}),
        hz_knobs=dict(hz_knobs or {}),
        scenarios=build_scenarios(sel),
        created_at=datetime.now(timezone.utc))


def scale_profile(profile: str, factor: float) -> str:
    """Multiply every gradient in a "station,gradient station,gradient" profile string.

    Stations are untouched; the engine forms head = WSE + gradient x distance, so scaling the
    profile scales every boundary head offset above the stream WSE while the WSE anchors and
    river stage stay fixed. Mirrors serialize_profile's "%g,%g" formatting."""
    pairs = []
    for token in str(profile).split():
        st_s, g_s = token.split(",", 1)
        pairs.append((float(st_s), float(g_s) * float(factor)))
    if not pairs:
        raise ValueError(f"empty gradient profile: {profile!r}")
    return " ".join(f"{st:g},{g:g}" for st, g in pairs)


def scenario_payloads(scenarios: list[AltScenario]) -> list[dict]:
    """The JSON-picklable per-scenario records the child runner consumes."""
    return [{"id": s.id, "label": s.label,
             "k_factor": s.k_factor, "g_factor": s.g_factor} for s in scenarios]


def relaunch_scenarios(manifest: HydraulicAlternativesManifest, halted_on: str | None,
                       *, retry: bool) -> list[AltScenario]:
    """The sublist for a halted sweep's relaunch: pending scenarios, with the failed one
    prepended when retrying. Order follows the manifest (= build_scenarios order)."""
    pending = [s for s in manifest.scenarios if s.status == AltStatus.pending]
    if retry and halted_on:
        failed = next((s for s in manifest.scenarios
                       if s.id == halted_on and s.status == AltStatus.failed), None)
        if failed is not None:
            return [failed] + pending
    return pending


def primaries_from_sections(sections: dict) -> dict:
    """The three signature primaries out of a scenario's stored results_sections."""
    conn = sections.get("connectivity") or {}
    res = sections.get("residence_time") or {}
    zone = sections.get("zone") or {}
    out = {"turnovers_per_km": conn.get("turnovers_per_km"),
           "rtd_median_days": res.get("weighted_median_days"),
           "equivalent_active_depth_m": zone.get("equivalent_active_depth_m")}
    return {k: v for k, v in out.items() if isinstance(v, (int, float))}


def _results_from_sections(sections: dict):
    """A minimal AssessmentResultsV2 carrying just the three metric sections, so the ranges
    below run through the CURRENT report.metric_rows instead of a second derivation path."""
    from .contracts import (AssessmentResultsV2, ConnectivityMetrics,
                            ResidenceTimeMetrics, ZoneMetrics)
    return AssessmentResultsV2(
        assessment_id="alternative", input_hash="",
        connectivity=ConnectivityMetrics.model_validate(sections.get("connectivity") or {}),
        residence_time=ResidenceTimeMetrics.model_validate(sections.get("residence_time") or {}),
        zone=ZoneMetrics.model_validate(sections.get("zone") or {}))


def metric_ranges(manifest: HydraulicAlternativesManifest,
                  base_rows: list[dict] | None = None) -> dict:
    """min/max per (section, name) over completed scenarios plus the Basecase rows when given.

    `base_rows` is report.metric_rows(<basecase results>) so the Basecase participates in every
    envelope. Returns {(section, name): {"lo", "hi", "n", "unit"}} for rows whose value_raw is
    numeric in at least one run; string-valued rows never range."""
    from . import report

    samples: dict[tuple[str, str], dict] = {}

    def _fold(rows: list[dict]) -> None:
        for row in rows:
            v = row.get("value_raw")
            if not isinstance(v, (int, float)):
                continue
            key = (row["section"], row["name"])
            slot = samples.setdefault(key, {"lo": v, "hi": v, "n": 0,
                                            "unit": row.get("unit", "")})
            slot["lo"] = min(slot["lo"], v)
            slot["hi"] = max(slot["hi"], v)
            slot["n"] += 1

    if base_rows:
        _fold(base_rows)
    for s in manifest.scenarios:
        if s.status != AltStatus.completed or not s.results_sections:
            continue
        _fold(report.metric_rows(_results_from_sections(s.results_sections)))
    return samples


def primary_ranges(manifest: HydraulicAlternativesManifest,
                   base_metrics: dict | None = None) -> dict:
    """min/max/n per primary key over completed scenarios + the Basecase metrics when given."""
    out: dict[str, dict] = {}
    pools: list[dict] = ([dict(base_metrics)] if base_metrics else [])
    pools += [s.metrics for s in manifest.scenarios if s.status == AltStatus.completed]
    for key in PRIMARY_KEYS:
        vals = [m.get(key) for m in pools if isinstance(m.get(key), (int, float))]
        if vals:
            out[key] = {"lo": min(vals), "hi": max(vals), "n": len(vals)}
    return out


def partial_note(manifest: HydraulicAlternativesManifest) -> str | None:
    """The partial-range label when any planned scenario did not complete. Counts include the
    Basecase (it is always a completed member of the envelope)."""
    total = len(manifest.scenarios) + 1
    done = len(manifest.completed()) + 1
    if done == total:
        return None
    return f"Partial scenario range: {done} of {total} runs"


__all__ = [
    "DEFAULT_SELECTION", "ROLE_ORDER", "COMBO_ORDER", "PRIMARY_KEYS",
    "factor_text", "validate_selection", "build_scenarios", "build_manifest",
    "scale_profile", "scenario_payloads", "relaunch_scenarios",
    "primaries_from_sections", "metric_ranges", "primary_ranges", "partial_note",
]
