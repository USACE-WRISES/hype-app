"""Quality-control validation of assembled results (report §27).

Pure functions returning HypeWarnings + a numeric diagnostics dict, so the report's model-quality
panel and the warnings list are populated from one place. Nothing here mutates the results model.
"""
from __future__ import annotations

import math

from .contracts import AssessmentResultsV2
from .provenance import HypeWarning, Severity


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def validate_results(results: AssessmentResultsV2, *, hz_accounting: dict | None = None,
                     domain_volume_m3: float | None = None,
                     tol_pct: float = 5.0) -> tuple[list[HypeWarning], dict]:
    """Run the §27 quality-control checks. Returns (warnings, diagnostics)."""
    warnings: list[HypeWarning] = []
    diag: dict = {}
    conn, rtd, zone = results.connectivity, results.residence_time, results.zone
    acct = hz_accounting or {}

    def warn(code, message, severity=Severity.warning):
        warnings.append(HypeWarning(code=code, message=message, severity=severity))

    # 27.1 model water balance -------------------------------------------------
    mbe = acct.get("mass_balance_error")
    if _finite(mbe):
        diag["mass_balance_error"] = float(mbe)
        if abs(mbe) * 100.0 > tol_pct:
            warn("water_balance", f"Interface-pass mass-balance error {mbe:.1%} exceeds the "
                                  f"{tol_pct:.0f}% tolerance; connectivity and residence-time "
                                  f"results may be biased.")
    clo = acct.get("closure_error_global")
    if _finite(clo):
        diag["closure_error_global"] = float(clo)

    # 27.2 flow-path accounting (fractions in [0, 1]) --------------------------
    for name, val in (("returning_flow_fraction", conn.returning_flow_fraction),
                      ("censored_flow_fraction", conn.censored_flow_fraction)):
        if _finite(val):
            diag[name] = float(val)
            if not (0.0 <= val <= 1.0):
                warn("flow_fraction_bounds", f"{name} = {val:.3f} is outside [0, 1].")
    if _finite(conn.censored_flow_fraction) and conn.censored_flow_fraction > 0.2:
        warn("censored_flow", f"{conn.censored_flow_fraction:.0%} of downwelling flow is censored "
                              f"(paths that do not resolve to a boundary); residence-time "
                              f"percentiles may be biased.")

    # 27.4 monotone threshold exceedance ---------------------------------------
    ths_sorted = sorted((t for t in results.thresholds
                         if _finite(t.flow_exceedance_fraction)),
                        key=lambda t: t.threshold_value_h)
    prev, mono = None, True
    for t in ths_sorted:
        if prev is not None and t.flow_exceedance_fraction > prev + 1e-9:
            mono = False
        prev = t.flow_exceedance_fraction
    if ths_sorted:
        diag["thresholds_monotone"] = mono
        if not mono:
            warn("threshold_monotonicity", "Threshold exceedance fractions are not monotonically "
                                           "non-increasing with residence time.")

    # 27.5 reciprocal C_1km vs L_T ---------------------------------------------
    c1, ltk = conn.turnovers_per_km, conn.turnover_length_km
    if _finite(c1) and c1 > 0 and _finite(ltk) and ltk > 0:
        residual = abs(1.0 / ltk - c1)
        diag["reciprocal_residual"] = residual
        if residual > 1e-6 * max(1.0, c1):
            warn("connectivity_reciprocal", "Turnovers per km and turnover length are not "
                                            "reciprocal within tolerance.")

    # 27.6 spatial volume ------------------------------------------------------
    frac = conn.active_streambed_fraction
    if _finite(frac):
        diag["active_streambed_fraction"] = float(frac)
        if not (0.0 <= frac <= 1.0):
            warn("active_bed_bounds", f"Active streambed fraction {frac:.3f} is outside [0, 1].")
    if _finite(zone.bulk_saturated_volume_m3) and _finite(domain_volume_m3) and domain_volume_m3 > 0:
        diag["volume_fraction_of_domain"] = zone.bulk_saturated_volume_m3 / domain_volume_m3
        if zone.bulk_saturated_volume_m3 > domain_volume_m3 * (1.0 + 1e-6):
            warn("volume_exceeds_domain", "Active hyporheic volume exceeds the modeled domain "
                                          "volume.")

    # 27.7 residence-time order + nonnegativity --------------------------------
    p10, p50, p90 = rtd.p10_days, rtd.weighted_median_days, rtd.p90_days
    if all(_finite(x) for x in (p10, p50, p90)):
        ordered = (p10 <= p50 + 1e-9) and (p50 <= p90 + 1e-9)
        diag["residence_order_ok"] = ordered
        if not ordered:
            warn("residence_order", "Residence-time percentiles are not ordered "
                                    "T10 <= T50 <= T90.")
        if min(p10, p50, p90) < 0:
            warn("residence_negative", "Residence-time percentiles include a negative value.")

    return warnings, diag


__all__ = ["validate_results"]
