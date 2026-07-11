"""Unit-conversion tests (spec §13.1)."""
import pytest

from hype_app.units import (
    CFS_TO_CMS,
    KSAT_UMS_TO_M_PER_DAY,
    cfs_to_cms,
    cms_to_cfs,
    ksat_ums_to_m_per_day,
)


def test_cfs_to_cms_constant():
    assert CFS_TO_CMS == pytest.approx(0.028316846592, abs=1e-15)
    assert cfs_to_cms(1.0) == pytest.approx(0.028316846592, abs=1e-15)


def test_cfs_cms_roundtrip():
    for x in (0.1, 1.0, 100.0, 12345.678):
        assert cms_to_cfs(cfs_to_cms(x)) == pytest.approx(x, rel=1e-12)


def test_ksat_conversion():
    assert KSAT_UMS_TO_M_PER_DAY == pytest.approx(0.0864, abs=1e-12)
    assert ksat_ums_to_m_per_day(1.0) == pytest.approx(0.0864, abs=1e-12)
    assert ksat_ums_to_m_per_day(10.0) == pytest.approx(0.864, abs=1e-12)
    assert ksat_ums_to_m_per_day(0.0) == 0.0
