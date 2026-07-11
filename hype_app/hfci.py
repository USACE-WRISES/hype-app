"""Hyporheic Functional Capacity Index scoring (revision spec §9).

Loads the versioned literature-derived scoring profile (scoring_profiles/hfci_v1.json) — equations
and thresholds are DATA, not code. Each component's raw driver is mapped through a monotone curve to
a 0–15 score (clamped, round-half-up), classified Low/Moderate/High; HFCI is the equal arithmetic
mean of the three normalized (score/15) components to two decimals, or "Not computable" when any
component is unavailable (§9.5). Labeling is fixed as "Literature-derived HFCI v1 - validation
ongoing"; the module never claims measured ecosystem/biogeochemical performance (§9.7).
"""
from __future__ import annotations

import json
import math
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from pathlib import Path

import numpy as np

from .contracts import ComponentScore, HFCIResult, HFCIScoringProfileV1, ScoreCurve

PROFILE_DIR = Path(__file__).resolve().parent / "scoring_profiles"
DEFAULT_PROFILE = PROFILE_DIR / "hfci_v1.json"


@lru_cache(maxsize=4)
def load_profile(path: str | None = None) -> HFCIScoringProfileV1:
    p = Path(path) if path else DEFAULT_PROFILE
    return HFCIScoringProfileV1.model_validate(json.loads(p.read_text(encoding="utf-8")))


def round_half_up(x: float) -> int:
    return int(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def evaluate_curve(curve: ScoreCurve, raw: float) -> tuple[float, bool]:
    """(clamped continuous score 0..15, extrapolated?) for a raw driver value."""
    score = float(np.interp(raw, curve.knots_x, curve.knots_y))
    score = min(15.0, max(0.0, score))
    lo, hi = (curve.supported_range or [curve.knots_x[0], curve.knots_x[-1]])
    return score, (raw < lo or raw > hi)


def residence_opportunity(transit_days) -> np.ndarray:
    """The §9.4 opportunity curve: 0 at/below 1 h, smooth log-time ramp to 1 at/above 1 day."""
    t = np.asarray(transit_days, float)
    hour, day = 1.0 / 24.0, 1.0
    with np.errstate(divide="ignore", invalid="ignore"):
        ramp = (np.log(t / hour) / np.log(day / hour))     # 0 at 1h, 1 at 1 day
    return np.clip(np.where(t <= hour, 0.0, np.where(t >= day, 1.0, ramp)), 0.0, 1.0)


def processing_driver(transit_days, weights) -> float:
    """Flux-weighted expected value of the residence-time opportunity curve (§9.4)."""
    from .metrics import weighted_mean
    opp = residence_opportunity(transit_days)
    return weighted_mean(opp, weights)


def score_component(curve: ScoreCurve, raw: float | None,
                    profile: HFCIScoringProfileV1) -> ComponentScore:
    if raw is None or not math.isfinite(raw):
        return ComponentScore(raw_value=raw, raw_unit=curve.raw_unit)
    cont, extrap = evaluate_curve(curve, raw)
    score = round_half_up(cont)
    cls = profile.class_for(score)
    return ComponentScore(raw_value=raw, raw_unit=curve.raw_unit, score=score,
                          class_name=cls.name if cls else None,
                          color=cls.color if cls else None, extrapolated=extrap)


def compute_hfci(*, exchange_raw: float | None, storage_raw: float | None,
                 processing_raw: float | None,
                 profile: HFCIScoringProfileV1 | None = None) -> HFCIResult:
    """Full HFCI result from the three raw drivers (§9.5)."""
    profile = profile or load_profile()
    ex = score_component(profile.exchange, exchange_raw, profile)
    st = score_component(profile.storage, storage_raw, profile)
    pr = score_component(profile.processing, processing_raw, profile)

    result = HFCIResult(exchange=ex, storage=st, processing=pr,
                        profile_id=profile.profile_id, profile_version=profile.version,
                        validation_label=profile.validation_label)
    scores = [c.score for c in (ex, st, pr)]
    if any(s is None for s in scores):
        missing = [n for n, c in (("Exchange", ex), ("Storage", st), ("Processing", pr))
                   if c.score is None]
        result.not_computable_reason = f"Missing component(s): {', '.join(missing)}"
        return result
    hfci = sum(s / 15.0 for s in scores) / 3.0
    result.hfci = round(hfci, 2)
    cls = profile.class_for(round_half_up(hfci * 15.0))
    if cls:
        result.hfci_class, result.hfci_color = cls.name, cls.color
    return result


__all__ = [
    "load_profile", "round_half_up", "evaluate_curve", "residence_opportunity",
    "processing_driver", "score_component", "compute_hfci", "DEFAULT_PROFILE",
]
