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
METERS_PER_MILE = 1609.344


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
    excursions_per_mile: float = field(init=False)
    turnover_length_m: float = field(init=False)

    def __post_init__(self):
        frac = (self.returning_hyporheic / self.streamflow) if self.streamflow > 0 else float("nan")
        self.excursions_per_mile = (frac * (METERS_PER_MILE / self.reach_length_m)
                                    if self.reach_length_m > 0 else float("nan"))
        self.turnover_length_m = (self.reach_length_m / frac
                                  if frac and frac > 0 else float("inf"))


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
    "mobile_pore_storage", "METERS_PER_MILE",
]
