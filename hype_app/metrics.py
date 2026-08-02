"""Flux-weighted hyporheic metrics (revision spec §8.3–8.5).

Pure and Shiny-independent. The heavy MODFLOW/MODPATH reading happens in the engine; these
functions take plain arrays so every number is unit-testable and hand-checkable:

* flux-weighted exchange classification + mass balance (§8.3)
* connectivity / excursions-per-mile (§8.4)
* weighted residence-time distribution: weighted quantiles, ECDF, fraction bands (§8.5)
* mobile pore-water storage (§8.2)

"Flux weight" = a returning particle's share of the stream-cell inflow it was released on, so
particle *counts* are never used as discharge fractions.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

SECONDS_PER_HOUR = 3600.0
SECONDS_PER_DAY = 86400.0
METERS_PER_MILE = 1609.344
METERS_PER_KM = 1000.0


# --------------------------------------------------------------------------- weighted stats
def weighted_mean(values, weights) -> float:
    v = np.asarray(values, float)
    w = np.asarray(weights, float)
    tot = w.sum()
    return float((v * w).sum() / tot) if tot > 0 else float("nan")


def weighted_quantile(values, weights, q: float) -> float:
    """Weighted quantile q in [0,1] via the cumulative-weight step function (linear interp)."""
    v = np.asarray(values, float)
    w = np.asarray(weights, float)
    if v.size == 0 or w.sum() <= 0:
        return float("nan")
    order = np.argsort(v)
    v, w = v[order], w[order]
    cw = np.cumsum(w)
    cw = cw / cw[-1]
    # midpoint convention: quantile positions at (cumulative - 0.5*weight)/total
    pos = (cw - 0.5 * w / w.sum())
    return float(np.interp(q, pos, v))


def weighted_ecdf(values, weights):
    """(sorted_values, cumulative_weight_fraction) for a weighted empirical CDF."""
    v = np.asarray(values, float)
    w = np.asarray(weights, float)
    if v.size == 0:
        return np.array([]), np.array([])
    order = np.argsort(v)
    v, w = v[order], w[order]
    cw = np.cumsum(w)
    return v, (cw / cw[-1] if cw[-1] > 0 else cw)


# --------------------------------------------------------------------------- flux-weighted exchange
@dataclass
class ExchangeAccounting:
    """Weighted stream-interface flux split (§8.3), all in the model's flow units (m3/day)."""
    total_downwelling: float = 0.0
    returning_hyporheic: float = 0.0
    losing_to_sides: float = 0.0
    unresolved: float = 0.0

    @property
    def mass_balance_error(self) -> float:
        """Relative closure error of the classified flux vs total downwelling."""
        classified = self.returning_hyporheic + self.losing_to_sides + self.unresolved
        if self.total_downwelling <= 0:
            return float("nan")
        return abs(classified - self.total_downwelling) / self.total_downwelling


def classify_weighted_flux(source_inflow: dict, particle_source_node, particle_class,
                           particle_weight=None) -> ExchangeAccounting:
    """Split downwelling stream inflow across exchange classes by particle flow weight (§8.3).

    source_inflow  : {stream_node: downwelling inflow (>0)} from the CHD_RIVER budget.
    particle_*     : per-released-particle arrays (source node, class, optional explicit weight).
    class values   : "hyporheic" (returns to river), "losing" (leaves a side), else unresolved.
    If particle_weight is None, each source cell's inflow is split EQUALLY across its particles.
    """
    src = np.asarray(particle_source_node)
    cls = np.asarray(particle_class, dtype=object)
    if particle_weight is None:
        counts: dict = {}
        for n in src:
            counts[int(n)] = counts.get(int(n), 0) + 1
        weight = np.array([source_inflow.get(int(n), 0.0) / counts[int(n)] for n in src])
    else:
        weight = np.asarray(particle_weight, float)

    acc = ExchangeAccounting(total_downwelling=float(sum(v for v in source_inflow.values() if v > 0)))
    for c, w in zip(cls, weight):
        if c == "hyporheic":
            acc.returning_hyporheic += w
        elif c == "losing":
            acc.losing_to_sides += w
        else:
            acc.unresolved += w
    return acc


# --------------------------------------------------------------------------- connectivity
@dataclass
class Connectivity:
    streamflow: float
    total_downwelling: float
    returning_hyporheic: float
    losing: float
    unresolved: float
    reach_length_m: float
    excursions_per_mile: float = field(init=False)        # supporting (backward compat)
    turnover_length_m: float = field(init=False)
    turnovers_per_km: float = field(init=False)           # C_1km headline (report §5.1)
    turnover_length_km: float = field(init=False)         # L_T (report §5.2)
    gross_exchange_ratio_reach: float = field(init=False)  # E_reach = Q_HEF / Q_stream (§5.5)

    def __post_init__(self):
        frac = (self.returning_hyporheic / self.streamflow) if self.streamflow > 0 else float("nan")
        self.gross_exchange_ratio_reach = frac
        self.excursions_per_mile = (frac * (METERS_PER_MILE / self.reach_length_m)
                                    if self.reach_length_m > 0 else float("nan"))
        self.turnovers_per_km = (frac * (METERS_PER_KM / self.reach_length_m)
                                 if self.reach_length_m > 0 else float("nan"))
        self.turnover_length_m = (self.reach_length_m / frac
                                  if frac and frac > 0 else float("inf"))
        # L_T in km; inf when there is no returning exchange (reciprocal of C_1km holds)
        self.turnover_length_km = self.turnover_length_m / METERS_PER_KM


def connectivity(*, streamflow, returning_hyporheic, total_downwelling, losing, unresolved,
                 reach_length_m) -> Connectivity | None:
    """Build connectivity metrics, or None when the inputs make them undefined (§8.4)."""
    if streamflow is None or streamflow <= 0 or reach_length_m is None or reach_length_m <= 0:
        return None
    if returning_hyporheic is None or not math.isfinite(returning_hyporheic):
        return None
    return Connectivity(streamflow=streamflow, total_downwelling=total_downwelling,
                        returning_hyporheic=returning_hyporheic, losing=losing,
                        unresolved=unresolved, reach_length_m=reach_length_m)


# --------------------------------------------------------------------------- exchange intensity / depth
def exchange_flux(returning_hyporheic_m3s, streambed_area_m2) -> dict:
    """Streambed-area-normalized exchange intensity q_HEF (report §5.4).

    returning_hyporheic in m3/s, area in m2 -> {"m_per_day", "mm_per_day"}. NaN when the area is
    missing or non-positive (the caller then withholds the metric)."""
    if (returning_hyporheic_m3s is None or streambed_area_m2 is None
            or streambed_area_m2 <= 0):
        return {"m_per_day": float("nan"), "mm_per_day": float("nan")}
    m_per_day = float(returning_hyporheic_m3s) * SECONDS_PER_DAY / float(streambed_area_m2)
    return {"m_per_day": m_per_day, "mm_per_day": m_per_day * 1000.0}


def path_depth_metrics(max_depths, weights) -> dict:
    """Flow-weighted maximum-penetration-depth statistics for returning paths (report §7.4).

    {"p50_m","p90_m","max_m"} via the same flow weights as the RTD; {} when there is nothing to
    summarize (e.g. the optional depth pass did not run)."""
    d = np.asarray(max_depths, float)
    w = (np.ones_like(d) if weights is None else np.asarray(weights, float))
    ok = np.isfinite(d) & np.isfinite(w) & (w > 0)
    d, w = d[ok], w[ok]
    if d.size == 0 or w.sum() <= 0:
        return {}
    return {"p50_m": weighted_quantile(d, w, 0.5),
            "p90_m": weighted_quantile(d, w, 0.9),
            "max_m": float(d.max())}


def equivalent_active_depth(volume_m3, streambed_area_m2) -> float | None:
    """D_HZ, the volume-normalized equivalent depth of the hyporheic zone (report §7.4).

    V_HZ / A_bed. The framework's primary NORMALIZED extent measure, so reaches of different width
    and length can be compared: dividing by the streambed area already carries both reach length
    and channel width, which §7.5 requires.

    It is NOT the physical depth of the zone at any point, and must never be labelled as one. A
    zone that is 2 m deep over a third of the bed and absent elsewhere has the same D_HZ as a
    uniform 0.67 m layer.

    None (not NaN) when either input is missing or the area is non-positive, because every caller
    stores this straight onto an optional contract field."""
    if volume_m3 is None or not streambed_area_m2 or streambed_area_m2 <= 0:
        return None
    return float(volume_m3) / float(streambed_area_m2)


def pore_volume(bulk_volume_m3, porosity) -> float | None:
    """Mobile pore-water storage: the WATER inside a bulk sediment volume.

    V_HZ * n. The distinction matters because the two are reported side by side and differ by a
    factor of three: `bulk_saturated_volume_m3` is sediment plus water, and this is the water alone.

    `porosity` must be the value MODPATH actually tracked at, not a live UI field. Porosity sets
    pore velocity, which set the travel times, which set which particles returned inside the
    tracking window, which set `bulk_volume_m3` itself. Scaling a frozen volume by a since-edited
    field reports a pore volume the flow model never produced."""
    if bulk_volume_m3 is None or porosity is None:
        return None
    return float(bulk_volume_m3) * float(porosity)


def exceedance_fraction(values, weights, threshold) -> float:
    """Flux-weighted exceedance P(value >= threshold) (report §6.1). NaN on empty/zero total weight.

    Uses '>=' (the spec's exceedance convention), distinct from the '>' band cuts in
    residence_time_metrics. Monotone non-increasing in `threshold` by construction."""
    v = np.asarray(values, float)
    w = (np.ones_like(v) if weights is None else np.asarray(weights, float))
    ok = np.isfinite(v) & np.isfinite(w) & (w >= 0)
    v, w = v[ok], w[ok]
    tot = w.sum()
    if v.size == 0 or tot <= 0:
        return float("nan")
    return float(w[v >= threshold].sum() / tot)


def weighted_reaction_fraction(values, weights, *, timescale, onset=0.0) -> float:
    """Flux-weighted mean of 1 - exp(-(t - onset)/timescale), clamped at `onset`.

    The continuous analogue of `exceedance_fraction`: rather than counting the flow whose value
    clears a threshold, it weights each path by how far past `onset` it goes. `values` are
    residence times and `timescale` is the process's characteristic reaction time, both in the
    SAME unit (days everywhere in this codebase).

    Used by the nutrient screen (onset = the observed source-to-sink residence-time threshold)
    and by the thermal screen's B_Q (onset = 0).

    Monotone non-decreasing in 1/timescale and non-increasing in `onset`. NaN on empty input or
    zero total weight, matching `exceedance_fraction`. A non-positive `timescale` is the
    instantaneous-reaction limit and returns the strictly-above-onset flow fraction, which is
    what the general formula tends to (paths sitting exactly at `onset` contribute zero either
    way, so the '>' here is consistent rather than a departure from the '>=' convention)."""
    v = np.asarray(values, float)
    w = (np.ones_like(v) if weights is None else np.asarray(weights, float))
    ok = np.isfinite(v) & np.isfinite(w) & (w >= 0)
    v, w = v[ok], w[ok]
    tot = w.sum()
    if v.size == 0 or tot <= 0:
        return float("nan")
    reactive = np.clip(v - float(onset), 0.0, None)          # time spent past onset
    if timescale is None or float(timescale) <= 0:
        return float(w[reactive > 0].sum() / tot)
    f = -np.expm1(-reactive / float(timescale))              # 1 - exp(-x), stable for small x
    return float((w * f).sum() / tot)


def reactive_exposure(values, weights, *, onset=0.0) -> float:
    """Σ wᵢ · max(0, tᵢ - onset): reaction opportunity with no rate constant at all.

    With weights in m3/day and residence times in days the product is m3, i.e. the volume of
    water standing in the reactive window at any instant (Little's law applied to the
    past-onset portion of the residence-time distribution). NaN on empty/zero-weight input."""
    v = np.asarray(values, float)
    w = (np.ones_like(v) if weights is None else np.asarray(weights, float))
    ok = np.isfinite(v) & np.isfinite(w) & (w >= 0)
    v, w = v[ok], w[ok]
    if v.size == 0 or w.sum() <= 0:
        return float("nan")
    return float((w * np.clip(v - float(onset), 0.0, None)).sum())


# --------------------------------------------------------------------------- residence-time distribution
def residence_time_metrics(transit_times_days, weights, *, porosity=None,
                           censored_fraction=None, max_tracking_time_days=None) -> dict:
    """Weighted RTD statistics from returning-particle transit times (days) + flow weights (§8.5)."""
    t = np.asarray(transit_times_days, float)
    w = np.asarray(weights, float)
    if t.size == 0 or w.sum() <= 0:
        return {"effective_particle_count": 0.0}
    hour, day = 1.0 / 24.0, 1.0
    tot = w.sum()
    return {
        "weighted_mean_days": weighted_mean(t, w),
        "weighted_median_days": weighted_quantile(t, w, 0.5),
        "p05_days": weighted_quantile(t, w, 0.05),
        "p10_days": weighted_quantile(t, w, 0.10),
        "p25_days": weighted_quantile(t, w, 0.25),
        "p75_days": weighted_quantile(t, w, 0.75),
        "p90_days": weighted_quantile(t, w, 0.90),
        "p95_days": weighted_quantile(t, w, 0.95),
        "min_days": float(t.min()),
        "max_days": float(t.max()),
        "frac_above_1h": float(w[t > hour].sum() / tot),
        "frac_1h_to_1d": float(w[(t > hour) & (t <= day)].sum() / tot),
        "frac_above_1d": float(w[t > day].sum() / tot),
        "effective_particle_count": float(tot ** 2 / (w ** 2).sum()),   # Kish effective N
        "censored_fraction": censored_fraction,
        "max_tracking_time_days": max_tracking_time_days,
        "porosity": porosity,
    }


# --------------------------------------------------------------------------- pore storage
def mobile_pore_storage(hyporheic_fraction, saturated_cell_volume, porosity) -> float:
    """Σ hyporheic_fraction · saturated cell volume · effective porosity (m3), §8.2."""
    f = np.asarray(hyporheic_fraction, float)
    vol = np.asarray(saturated_cell_volume, float)
    return float(np.sum(f * vol) * float(porosity))


def read_chd_downwelling(cbc_path, river_nodes) -> dict:
    """{river_node: downwelling inflow (>0)} from the MODFLOW cell budget (§8.3 step 1).

    MF6 stores only the MODEL name in the budget, so CHD flows can't be filtered by package name;
    river cells are identified by NODE membership (the set of CHD_RIVER nodes the caller already
    knows from hz_analysis.extract_bc_membership). A CHD flow > 0 is water entering the aquifer —
    i.e. the stream downwelling into the subsurface.

    NODE BASE: the cell budget stores 1-based MODFLOW node numbers, whereas hz_analysis membership
    nodes are 0-based (flopy zero-bases them on read). This converts budget nodes to 0-based so the
    two agree — the source of many subtle off-by-one hyporheic bugs if missed.
    """
    from flopy.utils import CellBudgetFile

    nodes = {int(n) for n in river_nodes}
    cbc = CellBudgetFile(str(cbc_path), precision="double")
    out: dict[int, float] = {}
    for rec in cbc.get_data(text="CHD"):
        for node, q in zip(np.asarray(rec["node"]).ravel(), np.asarray(rec["q"]).ravel()):
            n = int(node) - 1                        # budget is 1-based -> 0-based membership
            if n in nodes and q > 0:                 # >0 = into aquifer = downwelling
                out[n] = out.get(n, 0.0) + float(q)
    return out


__all__ = [
    "weighted_mean", "weighted_quantile", "weighted_ecdf", "ExchangeAccounting",
    "classify_weighted_flux", "Connectivity", "connectivity", "residence_time_metrics",
    "exchange_flux", "path_depth_metrics", "exceedance_fraction", "mobile_pore_storage",
    "weighted_reaction_fraction", "reactive_exposure",
    "METERS_PER_MILE", "METERS_PER_KM", "SECONDS_PER_DAY",
]
