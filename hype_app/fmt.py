"""Uniform numeric formatting, shared by every output surface (revision spec §11.4).

Moved out of `report.py` verbatim so `signature.py` can format without importing the report
module, which imports `signature` back. `report.py` re-exports all three names, so every existing
`report.fmt(...)` / `from .report import fmt` call site is unaffected.

This module has NO dependencies beyond the standard library, deliberately. It sits at the bottom
of the package graph and everything else may import it.
"""
from __future__ import annotations

import math

__all__ = ["fmt", "fmt_sig", "fmt_range"]


def fmt(value, digits: int = 3) -> str:
    """Uniform numeric formatting shared by every output format (§11.4). Missing values render as
    'n/a' (never an em dash, which reads as machine-generated in user-facing copy)."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int,)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        if value != value:                 # NaN
            return "n/a"
        if value == 0:
            return "0"
        a = abs(value)
        if a < 0.001:
            return f"{value:.3g}"           # scientific only for tiny magnitudes
        if a >= 10000:
            return f"{value:.0f}"           # 16093, 8200000 — plain integer, CSV-safe (no commas)
        return f"{round(value, digits):g}"  # 0.736, 2460, 1.4
    return str(value)


def fmt_sig(value, sig: int = 3) -> str:
    """`fmt`, but holding SIGNIFICANT figures instead of decimal places.

    For quantities whose magnitude varies over orders of magnitude across sites -- screening masses
    run from under a gram to tens of kilograms per day -- decimal rounding silently destroys
    information. It rendered a genuine 0.5% sensitivity spread (0.06811838 to 0.06848999 kg/day) as
    "0.068 to 0.068", which reads as a broken widget rather than a narrow range.

    Every behaviour `fmt` consumers depend on is preserved: 'n/a' for missing (never an em dash),
    '0' for zero, a plain comma-free integer above 10000, and scientific notation only below 0.001.
    Trailing zeros are INTENTIONAL ("2.40", "0.300") -- that is what three significant figures
    means, and it keeps a column of masses aligned. Do not "fix" it back to %g."""
    if value is None:
        return "n/a"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, int):
        return str(value)
    if not isinstance(value, float):
        return str(value)
    if value != value or value in (float("inf"), float("-inf")):
        return "n/a"
    if value == 0:
        return "0"
    a = abs(value)
    if a < 0.001:
        return f"{value:.{sig}g}"
    if a >= 10000:
        return f"{value:.0f}"
    # max(0, ...) so the integer part is never rounded away: 16093 stays 16093, not 16100.
    places = max(0, sig - 1 - math.floor(math.log10(a)))
    return f"{round(value, places):.{places}f}"


def fmt_range(lo, hi, sig: int = 3) -> str | None:
    """"lo to hi", or the single value when the two agree at the precision shown.

    The collapse test is on the FORMATTED STRINGS, not the floats: bounds that differ in the last
    bit (0.299999998867 vs 0.3) must not print "0.300 to 0.300". A single value here is a real
    result -- the sweep did not move the estimate, usually because removal has saturated."""
    if lo is None or hi is None:
        return None
    s_lo, s_hi = fmt_sig(lo, sig), fmt_sig(hi, sig)
    return s_lo if s_lo == s_hi else f"{s_lo} to {s_hi}"
