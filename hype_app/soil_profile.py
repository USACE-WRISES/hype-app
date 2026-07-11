"""Depth-aware hydraulic-conductivity derivation from NRCS soils (revision spec §6.5–6.10).

Pure and Shiny-independent. Turns a `SoilDataSnapshot`'s components/horizons into per-model-layer
KH/KV, honoring local horizon depth (never treating horizons as independent horizontal polygons),
the aggregation policy, and the precedence order manual-zone > NRCS-override > NRCS-derived >
global-fallback. Every number is unit-testable with deterministic inputs.

Conventions:
* Ksat is micrometres/second (SSURGO ksat_r); KV[m/day] = Ksat[um/s] * 0.0864 (units.py).
* KH = anisotropy_ratio * KV.
* Horizon depths are centimetres below the local ground surface; model layers are elevations.
  Depth d below ground maps to elevation ground_elev - d/100.
* A single model layer spanning multiple horizons aggregates thickness-weighted: ARITHMETIC for
  lateral KH, HARMONIC for vertical KV (§6.6).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .contracts import (
    AggregationPolicy,
    Component,
    DerivedConductivityProfile,
    KOrigin,
    MapUnit,
)
from .units import ksat_ums_to_m_per_day


def horizon_kv_kh(ksat_um_s: float, anisotropy_ratio: float) -> tuple[float, float]:
    """(KV, KH) in m/day from representative Ksat (um/s) and the anisotropy ratio (§6.5)."""
    kv = ksat_ums_to_m_per_day(ksat_um_s)
    return kv, anisotropy_ratio * kv


@dataclass
class KSegment:
    """A vertical slice of a model layer that a single horizon (or fallback) governs."""
    thickness_m: float
    kh: float
    kv: float
    origin: KOrigin = KOrigin.derived


def intersect_layer_horizons(
    layer_top_elev: float, layer_bottom_elev: float, ground_elev: float,
    horizons: list[dict], anisotropy_ratio: float,
    *, fallback_kh: float, fallback_kv: float,
) -> list[KSegment]:
    """Slice a model layer's [bottom, top] elevation span by the component's horizons (§6.7).

    Each horizon covers depth [top_cm, bottom_cm] below `ground_elev`; the deepest horizon is NOT
    extended — any part of the layer below the known profile uses the global fallback KH/KV, and
    that fallback thickness is returned as its own segment so coverage can report it.
    """
    top = max(layer_top_elev, layer_bottom_elev)
    bot = min(layer_top_elev, layer_bottom_elev)
    if top <= bot:
        return []
    segments: list[KSegment] = []

    for h in horizons:
        t_cm, b_cm = h.get("top_cm"), h.get("bottom_cm")
        ksat = h.get("ksat_um_s")
        if t_cm is None or b_cm is None:
            continue
        h_top_elev = ground_elev - float(t_cm) / 100.0     # shallower depth -> higher elevation
        h_bot_elev = ground_elev - float(b_cm) / 100.0
        lo, hi = max(bot, h_bot_elev), min(top, h_top_elev)
        if hi <= lo:
            continue
        if ksat is None:
            kh, kv, origin = fallback_kh, fallback_kv, KOrigin.fallback
        else:
            kv, kh = horizon_kv_kh(float(ksat), anisotropy_ratio)
            origin = KOrigin.derived
        segments.append(KSegment(thickness_m=hi - lo, kh=kh, kv=kv, origin=origin))

    # Any layer thickness NOT covered by a horizon (gaps or below the known profile) -> fallback.
    below = (top - bot) - sum(s.thickness_m for s in segments)
    if below > 1e-9:
        segments.append(KSegment(thickness_m=below, kh=fallback_kh, kv=fallback_kv,
                                 origin=KOrigin.fallback))
    return segments


def aggregate_segments(segments: list[KSegment]) -> tuple[float, float, KOrigin]:
    """Thickness-weighted KH (arithmetic) and KV (harmonic) over a layer's segments (§6.6).

    Returns (kh, kv, origin) where origin is 'fallback' only if EVERY segment is fallback,
    'derived' otherwise (a mixed layer is derived with a fallback tail folded in).
    """
    total = sum(s.thickness_m for s in segments)
    if total <= 0:
        return 0.0, 0.0, KOrigin.fallback
    kh = sum(s.kh * s.thickness_m for s in segments) / total                 # arithmetic
    inv = sum(s.thickness_m / s.kv for s in segments if s.kv > 0)
    kv = (total / inv) if inv > 0 else 0.0                                    # harmonic
    origin = KOrigin.fallback if all(s.origin == KOrigin.fallback for s in segments) \
        else KOrigin.derived
    return kh, kv, origin


def select_components(map_unit: MapUnit, policy: AggregationPolicy,
                      chosen_cokey: str | None = None) -> list[tuple[Component, float]]:
    """(component, weight) list for a map unit under the aggregation policy (§6.6).

    * dominant       -> the single highest-percentage major component, weight 1.
    * user_component -> the chosen component (or dominant if unspecified), weight 1.
    * weighted       -> all components weighted by representative percentage (normalized).
    """
    comps = list(map_unit.components)
    if not comps:
        return []
    if policy == AggregationPolicy.weighted:
        tot = sum((c.comppct_r or 0.0) for c in comps) or 1.0
        return [(c, (c.comppct_r or 0.0) / tot) for c in comps if (c.comppct_r or 0.0) > 0] \
            or [(comps[0], 1.0)]
    if policy == AggregationPolicy.user_component and chosen_cokey:
        pick = next((c for c in comps if c.cokey == chosen_cokey), None)
        if pick:
            return [(pick, 1.0)]
    # dominant (default): highest-% major component, else highest-% overall
    majors = [c for c in comps if c.major] or comps
    dominant = max(majors, key=lambda c: (c.comppct_r or 0.0))
    return [(dominant, 1.0)]


def derive_profiles(map_unit: MapUnit, anisotropy_ratio: float) -> list[DerivedConductivityProfile]:
    """One DerivedConductivityProfile per horizon of every component (for the review/provenance)."""
    out: list[DerivedConductivityProfile] = []
    for c in map_unit.components:
        for h in c.horizons:
            kv = kh = None
            if h.ksat_um_s is not None:
                kv, kh = horizon_kv_kh(h.ksat_um_s, anisotropy_ratio)
            out.append(DerivedConductivityProfile(
                mukey=map_unit.mukey, cokey=c.cokey,
                top_cm=h.top_cm, bottom_cm=h.bottom_cm, ksat_um_s=h.ksat_um_s,
                kv_m_day=kv, anisotropy_ratio=anisotropy_ratio, kh_m_day=kh,
                origin=KOrigin.derived if h.ksat_um_s is not None else KOrigin.fallback))
    return out


def layer_k_for_component(
    component: Component, *, layer_top_elev: float, layer_bottom_elev: float,
    ground_elev: float, anisotropy_ratio: float, fallback_kh: float, fallback_kv: float,
) -> tuple[float, float, KOrigin, float]:
    """(kh, kv, origin, fallback_fraction) for one model layer & component via depth intersection."""
    horizons = [{"top_cm": h.top_cm, "bottom_cm": h.bottom_cm, "ksat_um_s": h.ksat_um_s}
                for h in component.horizons]
    segs = intersect_layer_horizons(
        layer_top_elev, layer_bottom_elev, ground_elev, horizons, anisotropy_ratio,
        fallback_kh=fallback_kh, fallback_kv=fallback_kv)
    if not segs:
        return fallback_kh, fallback_kv, KOrigin.fallback, 1.0
    kh, kv, origin = aggregate_segments(segs)
    fb = sum(s.thickness_m for s in segs if s.origin == KOrigin.fallback)
    tot = sum(s.thickness_m for s in segs)
    return kh, kv, origin, (fb / tot if tot > 0 else 1.0)


@dataclass
class CoverageAccumulator:
    """Volume-weighted coverage tally by K origin (§6.10)."""
    direct: float = 0.0
    derived: float = 0.0
    override: float = 0.0
    fallback: float = 0.0

    def add(self, origin: KOrigin, volume: float) -> None:
        setattr(self, origin.value, getattr(self, origin.value) + volume)

    def as_percentages(self) -> dict[str, float]:
        tot = self.direct + self.derived + self.override + self.fallback
        if tot <= 0:
            return {"direct": 0.0, "derived": 0.0, "override": 0.0, "fallback": 0.0}
        return {k: round(100.0 * getattr(self, k) / tot, 2)
                for k in ("direct", "derived", "override", "fallback")}


__all__ = [
    "horizon_kv_kh", "KSegment", "intersect_layer_horizons", "aggregate_segments",
    "select_components", "derive_profiles", "layer_k_for_component", "CoverageAccumulator",
]
