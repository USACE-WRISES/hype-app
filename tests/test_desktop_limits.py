"""Desktop run-mode limit lifts (2026-08-12): the RAS per-step timeout, the RAS
post-mesh cell cap, and the 3-D grid-preview cap are cloud limits — Desktop Run skips
them (an explicit env override still wins in both modes) — and the flux-pass release
density gained a template generator + a desktop-only app knob (default 4, so results
are unchanged unless it is raised).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from hype_app import mesh, ras, runmode

APP = Path(__file__).resolve().parents[1] / "app.py"


@pytest.fixture
def desktop(monkeypatch):
    monkeypatch.setattr(runmode, "IS_DESKTOP", True)


@pytest.fixture
def cloud(monkeypatch):
    monkeypatch.setattr(runmode, "IS_DESKTOP", False)


# ------------------------------------------------------------- RAS step timeout

def test_ras_timeout_cloud_default(cloud, monkeypatch):
    monkeypatch.delenv("HYPE_RAS_TIMEOUT_S", raising=False)
    assert ras.run_timeout_s() == 1800.0


def test_ras_timeout_desktop_no_deadline(desktop, monkeypatch):
    monkeypatch.delenv("HYPE_RAS_TIMEOUT_S", raising=False)
    assert ras.run_timeout_s() is None


def test_ras_timeout_env_wins_in_both_modes(desktop, monkeypatch):
    monkeypatch.setenv("HYPE_RAS_TIMEOUT_S", "42")
    assert ras.run_timeout_s() == 42.0
    monkeypatch.setattr(runmode, "IS_DESKTOP", False)
    assert ras.run_timeout_s() == 42.0


# ------------------------------------------------------------- RAS post-mesh cap

def test_mesh_cap_gate_cloud_raises(cloud, monkeypatch):
    monkeypatch.setenv("HYPE_RAS_MAX_CELLS", "100")
    with pytest.raises(ras.RasError, match="above the 100"):
        ras.mesh_cap_gate(250, 10.0)


def test_mesh_cap_gate_desktop_advisory(desktop, monkeypatch):
    monkeypatch.setenv("HYPE_RAS_MAX_CELLS", "100")
    lines = []
    ras.mesh_cap_gate(250, 10.0, log=lines.append)
    assert lines and "no limit in Desktop Run" in lines[0]
    assert "—" not in lines[0]


def test_mesh_cap_gate_under_cap_is_silent(cloud, monkeypatch):
    monkeypatch.setenv("HYPE_RAS_MAX_CELLS", "100")
    lines = []
    ras.mesh_cap_gate(100, 10.0, log=lines.append)
    assert lines == []


# ------------------------------------------------------------- silent mesh failure

def test_mesh_build_failure_is_a_clear_raserror(tmp_path):
    # `ras mesh` prints "Failed to build conceptual mesh." but exits 0, leaving the H5
    # without the mesh Attributes (seen on CH00518). The reader must surface a clear
    # RasError, not the raw h5py KeyError.
    h5py = pytest.importorskip("h5py")
    empty = tmp_path / "Geometry.h5"
    with h5py.File(empty, "w"):
        pass
    with pytest.raises(ras.RasError, match="could not build a mesh"):
        ras.read_mesh_summary_checked(empty)
    assert "—" not in ras.MESH_BUILD_FAILED_MSG


# ------------------------------------------------------------- 3-D preview cap

def test_preview_cap_cloud_matches_red_band(cloud, monkeypatch):
    from hype_app import estimate
    monkeypatch.delenv("HYPE_MESH_PREVIEW_MAX_CELLS", raising=False)
    assert mesh.preview_cell_cap() == estimate.AMBER_MAX


def test_preview_cap_desktop_none(desktop, monkeypatch):
    monkeypatch.delenv("HYPE_MESH_PREVIEW_MAX_CELLS", raising=False)
    assert mesh.preview_cell_cap() is None


def test_preview_cap_env_wins_in_desktop(desktop, monkeypatch):
    monkeypatch.setenv("HYPE_MESH_PREVIEW_MAX_CELLS", "1234")
    assert mesh.preview_cell_cap() == 1234


# ------------------------------------------------------------- iface templates

def test_iface_template_presets_unchanged():
    from hypetool.functions.hz_analysis import _IFACE_TEMPLATES, iface_template
    for n in (1, 3, 4):
        assert iface_template(n) == _IFACE_TEMPLATES[n]


def test_iface_template_lattice_deterministic_and_in_cell():
    from hypetool.functions.hz_analysis import iface_template
    for n in (2, 5, 9, 16, 25, 100):
        pts = iface_template(n)
        assert len(pts) == n == len(set(pts))
        assert all(0.0 < x < 1.0 and 0.0 < y < 1.0 for x, y in pts)
        assert pts == iface_template(n)
    # 5 on a 3x3 lattice: row-major, centred offsets
    assert iface_template(5)[:3] == ((1 / 6, 1 / 6), (3 / 6, 1 / 6), (5 / 6, 1 / 6))


def test_iface_template_rejects_nonpositive():
    from hypetool.functions.hz_analysis import iface_template
    with pytest.raises(ValueError, match="must be >= 1"):
        iface_template(0)


# ------------------------------------------------------------- app.py wiring

def test_app_threads_the_iface_knob_and_gates():
    body = APP.read_text(encoding="utf-8")
    # payload -> engine, kept-widget persistence, alternatives knob replay
    assert '"iface_particles_per_cell": int(_safe("hz_iface_ppc", 4))' in body
    assert '"hz_iface_ppc",' in body
    assert '"iface_particles_per_cell"):' in body
    # the grid-preview button now carries the same desktop guard as the Run gate
    assert body.count('if not runmode.IS_DESKTOP and est and '
                      'estimate.band(est["n_cells"]) == "red":') == 2
    # user copy rule: no em dashes in the new knob's copy
    seg = body.split('"Flux particles per cell"')[1][:900]
    assert "—" not in seg
