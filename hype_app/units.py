"""Unit conversions used across the HYPE revision, kept in one tested place (spec §13.1).

* Discharge: cubic feet per second <-> cubic metres per second (USGS flow normalization).
* Hydraulic conductivity: NRCS representative Ksat (micrometres/second) -> model K (metres/day).
"""
from __future__ import annotations

# 1 ft = 0.3048 m exactly -> 1 ft^3 = 0.3048^3 m^3. Matches app.py's CFS_TO_CMS.
CFS_TO_CMS: float = 0.3048 ** 3           # 0.028316846592
CMS_TO_CFS: float = 1.0 / CFS_TO_CMS

# 1 um/s = 1e-6 m/s; * 86400 s/day = 0.0864 m/day (spec §6.5).
KSAT_UMS_TO_M_PER_DAY: float = 1e-6 * 86400.0     # 0.0864


def cfs_to_cms(cfs: float) -> float:
    return float(cfs) * CFS_TO_CMS


def cms_to_cfs(cms: float) -> float:
    return float(cms) * CMS_TO_CFS


def ksat_ums_to_m_per_day(ksat_um_per_s: float) -> float:
    """Representative Ksat (um/s) -> vertical K (m/day). This is the model KV (§6.5)."""
    return float(ksat_um_per_s) * KSAT_UMS_TO_M_PER_DAY


__all__ = [
    "CFS_TO_CMS", "CMS_TO_CFS", "KSAT_UMS_TO_M_PER_DAY",
    "cfs_to_cms", "cms_to_cfs", "ksat_ums_to_m_per_day",
]
