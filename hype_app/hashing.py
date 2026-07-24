"""Canonical hashing + dependency-staleness for HYPE results (revision spec §4.3).

A result is *current* only if every input group it depends on still hashes to the value that
was frozen when the result was produced. Any change to reach/boundary geometry, DEM/WSE,
streamflow, soil/K assignments, gradients, grid/model depth, or porosity/particle settings
invalidates every dependent result. The UI then distinguishes current / stale-retained /
missing / failed (stale results stay viewable but are labeled and never presented as current).

Hashing is order-stable (sorted keys, normalized floats) so logically-equal inputs hash equal
regardless of dict ordering or float formatting noise.
"""
from __future__ import annotations

import hashlib
import json
import math
from enum import Enum
from typing import Any, Iterable, Mapping

# The dependency groups from §4.3, in evaluation order. Each maps to a subset of the frozen
# input snapshot; a result records the hash of every group it depends on.
INPUT_GROUPS: tuple[str, ...] = (
    "geometry",     # reach + boundary geometry
    "terrain",      # DEM + WSE
    "streamflow",   # streamflow value + source
    "soil_k",       # soil / K assignments (incl. porosity)
    "gradients",    # gradient configuration
    "grid",         # grid + model depth
    # "particles" removed 2026-07-18 with the per-run MP7 pass (System A) — hyporheic
    # delineation runs post-solve from ALL cells (hz_analysis) with its own knobs.
)


class ResultStatus(str, Enum):
    current = "current"
    stale = "stale"
    missing = "missing"
    failed = "failed"


def _normalize(obj: Any) -> Any:
    """Recursively normalize for stable hashing: round floats, sort mappings, listify sets."""
    if isinstance(obj, float):
        if math.isnan(obj):
            return "NaN"
        if math.isinf(obj):
            return "Infinity" if obj > 0 else "-Infinity"
        # 12 significant digits kills float-format noise without merging distinct inputs.
        return round(obj, 12) + 0.0        # +0.0 folds -0.0 -> 0.0
    if isinstance(obj, Mapping):
        return {str(k): _normalize(obj[k]) for k in sorted(obj, key=str)}
    if isinstance(obj, (set, frozenset)):
        return sorted((_normalize(v) for v in obj), key=repr)
    if isinstance(obj, (list, tuple)):
        return [_normalize(v) for v in obj]
    if hasattr(obj, "model_dump"):          # pydantic contract -> plain dict first
        return _normalize(obj.model_dump(mode="json"))
    return obj


def canonical_json(obj: Any) -> str:
    """Deterministic JSON string for any JSON-ish / pydantic object."""
    return json.dumps(_normalize(obj), sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(obj: Any) -> str:
    """SHA-256 hex digest of the canonical form. Logically-equal inputs -> equal digest."""
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def group_hashes(groups: Mapping[str, Any]) -> dict[str, str]:
    """Hash each provided dependency group. Unknown group names raise (typo guard)."""
    bad = set(groups) - set(INPUT_GROUPS)
    if bad:
        raise ValueError(f"Unknown input group(s): {sorted(bad)}; valid: {INPUT_GROUPS}")
    return {name: stable_hash(value) for name, value in groups.items()}


def changed_groups(current: Mapping[str, str],
                   recorded: Mapping[str, str] | None) -> list[str]:
    """Group names whose hash differs between now and when a result was produced.

    A group present in one mapping but absent in the other counts as changed.
    """
    if recorded is None:
        return list(current)
    names: Iterable[str] = set(current) | set(recorded)
    return sorted(n for n in names if current.get(n) != recorded.get(n))


def result_status(current: Mapping[str, str] | None,
                  recorded: Mapping[str, str] | None,
                  *, failed: bool = False) -> ResultStatus:
    """Classify a result given the current vs recorded dependency hashes.

    * failed=True                      -> failed (overrides everything)
    * recorded is None / empty         -> missing (never produced)
    * every recorded group matches now -> current
    * otherwise                        -> stale (retained but out of date)
    """
    if failed:
        return ResultStatus.failed
    if not recorded:
        return ResultStatus.missing
    return ResultStatus.current if not changed_groups(current or {}, recorded) \
        else ResultStatus.stale


__all__ = [
    "INPUT_GROUPS", "ResultStatus", "canonical_json", "stable_hash",
    "group_hashes", "changed_groups", "result_status",
]
