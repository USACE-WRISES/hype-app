"""Structured-gradient head-anchor engine (revision spec §7.4–7.6).

Pure and Shiny-independent. Turns `GradientBoundaryConfigV2` controls into realized constant-head
values along a boundary side:

  1. head at each control = WSE_edge + gradient * distance_to_WSE_edge  (§7.5)
  2. interpolate those anchor heads by arc-length station to every boundary cell
  3. record a diagnostics row per control (side, station, gradient, WSE, distance, head, ...)

The WSE and distance at each control come from the model grid (the app injects them, reusing
`my_utils.nearest_wse_edge_distance_and_value`); the numerics here are grid-independent so the
whole thing is unit-testable with deterministic inputs. Reference-slope derivation for qualitative
categories (§7.4) and config validation (§7.6) also live here.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .contracts import (
    GradientBoundaryConfigV2,
    GradientControl,
    GradientQualitative,
    ReferenceSlope,
    Side,
)
from .provenance import HypeWarning, Severity


def anchor_head(wse: float, gradient: float, distance: float) -> float:
    """head = WSE + gradient * distance (§7.5). Positive gradient raises head above stream WSE."""
    return float(wse) + float(gradient) * float(distance)


@dataclass
class ControlGeometry:
    """A control paired with the grid quantities the head-anchor needs."""
    control: GradientControl
    wse: float
    distance: float

    @property
    def head(self) -> float:
        return anchor_head(self.wse, self.control.preferred, self.distance)


def interpolate_to_stations(anchor_stations: list[float], anchor_heads: list[float],
                            target_stations) -> np.ndarray:
    """Linear-interpolate anchor heads (by station) to target stations; clamps outside [0,1]."""
    xs = np.asarray(anchor_stations, dtype=float)
    ys = np.asarray(anchor_heads, dtype=float)
    order = np.argsort(xs)
    return np.interp(np.asarray(target_stations, dtype=float), xs[order], ys[order])


def realized_side_heads(geometries: list[ControlGeometry], cell_stations,
                        *, which: str = "preferred") -> tuple[np.ndarray, list[dict]]:
    """(cell_heads, diagnostics) for one side.

    `which` selects the gradient scenario per control: 'preferred' | 'lower' | 'upper'
    (lower/upper fall back to preferred when unset). Diagnostics is one row per control (§7.5).
    """
    def _grad(c: GradientControl) -> float:
        if which == "lower" and c.lower is not None:
            return c.lower
        if which == "upper" and c.upper is not None:
            return c.upper
        return c.preferred

    geometries = sorted(geometries, key=lambda g: g.control.station)
    stations = [g.control.station for g in geometries]
    heads, diagnostics = [], []
    for g in geometries:
        grad = _grad(g.control)
        h = anchor_head(g.wse, grad, g.distance)
        heads.append(h)
        diagnostics.append({
            "id": g.control.id, "side": g.control.side.value, "station": g.control.station,
            "gradient": grad, "wse": g.wse, "distance": g.distance, "anchor_head": h,
        })
    cell_heads = interpolate_to_stations(stations, heads, cell_stations)
    return cell_heads, diagnostics


def reference_slope_from_samples(upstream_elev: float, downstream_elev: float,
                                 reach_distance_m: float, *, source: str,
                                 method: str | None = None) -> ReferenceSlope | None:
    """Reference slope = (upstream - downstream) / reach distance (§7.4).

    Returns None for a flat/adverse/invalid/indeterminate slope so the caller REQUIRES a manual
    numeric value rather than applying an artificial floor.
    """
    if reach_distance_m is None or reach_distance_m <= 0:
        return None
    if not (np.isfinite(upstream_elev) and np.isfinite(downstream_elev)):
        return None
    slope = (float(upstream_elev) - float(downstream_elev)) / float(reach_distance_m)
    if not np.isfinite(slope) or slope <= 0:      # flat or adverse -> indeterminate (§7.4)
        return None
    return ReferenceSlope(value=slope, source=source, method=method,
                          upstream_sample=upstream_elev, downstream_sample=downstream_elev,
                          reach_distance_m=reach_distance_m)


def validate_config(config: GradientBoundaryConfigV2) -> list[HypeWarning]:
    """Non-blocking validation warnings for a gradient config (§7.6 warn-list subset).

    Blocking conditions (missing endpoints, duplicate/out-of-range stations) are already enforced
    by the contract's validators; this adds the advisory checks (sign changes, close controls).
    """
    warnings: list[HypeWarning] = []
    for side, controls in (("left", config.left_controls), ("right", config.right_controls)):
        if not controls:
            continue
        grads = [c.preferred for c in sorted(controls, key=lambda c: c.station)]
        if any(a * b < 0 for a, b in zip(grads, grads[1:])):
            warnings.append(HypeWarning(
                code="gradient_sign_change",
                message=f"{side} side gradient changes sign between controls "
                        "(gaining↔losing along the reach).", severity=Severity.warning))
        stations = sorted(c.station for c in controls)
        if any(b - a < 0.02 for a, b in zip(stations, stations[1:])):
            warnings.append(HypeWarning(
                code="controls_close", severity=Severity.info,
                message=f"{side} side has controls very close together (<2% of the reach)."))
    return warnings


def parse_control_lines(text: str, side: Side) -> list[GradientControl]:
    """Parse the pane's control-table text into GradientControls (one control per line).

    Line format: ``station, preferred [, lower, upper]`` — commas or whitespace separated.
    Also accepts the legacy single-line ``"f,g f,g …"`` profile string (§7.7), so old
    project values paste straight in. Stations 0 and 1 are enforced by the contract.
    """
    if text is None or not str(text).strip():
        raise ValueError("No gradient controls given.")
    raw_lines = [ln.strip() for ln in str(text).splitlines() if ln.strip()]
    if len(raw_lines) == 1 and raw_lines[0].count(",") >= 2 and " " in raw_lines[0]:
        # legacy one-liner "f,g f,g ..." -> one control per pair
        raw_lines = [p.strip() for p in raw_lines[0].split() if p.strip()]

    controls: list[GradientControl] = []
    for ln in raw_lines:
        parts = [p for p in ln.replace(",", " ").split() if p]
        if len(parts) not in (2, 3, 4):
            raise ValueError(f"Bad control line '{ln}' — use 'station, preferred[, lower, upper]'.")
        try:
            nums = [float(p) for p in parts]
        except ValueError as e:
            raise ValueError(f"Non-numeric value in control line '{ln}'.") from e
        station, preferred = nums[0], nums[1]
        lower = nums[2] if len(nums) >= 3 else None
        upper = nums[3] if len(nums) == 4 else None
        if len(nums) == 3:                    # single third number = symmetric +/- variation
            lower, upper = preferred - abs(nums[2]), preferred + abs(nums[2])
        controls.append(GradientControl(
            id=f"{side.value}-{station:g}", side=side, station=station,
            preferred=preferred, lower=lower, upper=upper, source="manual"))
    return controls


def serialize_profile(controls: list[GradientControl], *, which: str = "preferred") -> str:
    """Controls -> the engine's ``"station,gradient …"`` profile string.

    The engine's spatially-varying path already implements the §7.5 head-anchor method
    (anchor head per fraction, arc-length interpolation), so structured controls feed it
    losslessly — `which` picks the preferred/lower/upper scenario per control.
    """
    def _g(c: GradientControl) -> float:
        if which == "lower" and c.lower is not None:
            return c.lower
        if which == "upper" and c.upper is not None:
            return c.upper
        return c.preferred
    ordered = sorted(controls, key=lambda c: c.station)
    return " ".join(f"{c.station:g},{_g(c):g}" for c in ordered)


# Qualitative sensitivity bounds: one category step down/up (§10.1 default for qualitative mode).
_QUAL_ORDER = [GradientQualitative.strongly_losing, GradientQualitative.slightly_losing,
               GradientQualitative.neutral, GradientQualitative.slightly_gaining,
               GradientQualitative.strongly_gaining]


def qualitative_neighbors(cat: GradientQualitative) -> tuple[GradientQualitative,
                                                             GradientQualitative]:
    """(one step toward losing, one step toward gaining), clamped at the scale ends."""
    i = _QUAL_ORDER.index(cat)
    return _QUAL_ORDER[max(0, i - 1)], _QUAL_ORDER[min(len(_QUAL_ORDER) - 1, i + 1)]


def config_from_legacy_corners(corner_gradients: dict, *, side: Side) -> list[GradientControl]:
    """Upgrade a legacy 4-corner config to structured controls for one side (§7.7 upgrade preview).

    Corner keys: g_ul/g_dl (left upstream/downstream), g_ur/g_dr (right). Station 0 = upstream.
    """
    if side == Side.left:
        up, down = corner_gradients.get("g_ul"), corner_gradients.get("g_dl")
    else:
        up, down = corner_gradients.get("g_ur"), corner_gradients.get("g_dr")
    up = 0.0 if up is None else float(up)
    down = 0.0 if down is None else float(down)
    return [GradientControl(id=f"{side.value}-0", side=side, station=0.0, preferred=up,
                            source="legacy_upgrade"),
            GradientControl(id=f"{side.value}-1", side=side, station=1.0, preferred=down,
                            source="legacy_upgrade")]


__all__ = [
    "anchor_head", "ControlGeometry", "interpolate_to_stations", "realized_side_heads",
    "reference_slope_from_samples", "validate_config", "config_from_legacy_corners",
    "parse_control_lines", "serialize_profile", "qualitative_neighbors",
]
