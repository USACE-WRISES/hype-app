"""Platform-aware mf6/mp7 resolution (bin/win vs bin/linux) + the availability gate.

Regression for the desktop dev-mode failure where an unset HYPE_MODFLOW_BIN handed
flopy the Linux ELF bin/linux/mf6 on Windows (flopy FileNotFoundError mid-run)."""
import sys
from pathlib import Path

from hype_app import run as runner
from hypetool.functions.path_utils import detect_modflow_exes

_WIN = sys.platform.startswith("win")
_REPO = Path(runner._APP_ROOT)


def test_default_is_platform_bundled_dir(monkeypatch):
    monkeypatch.delenv("HYPE_MODFLOW_BIN", raising=False)
    d = Path(runner.modflow_bin_dir())
    assert d == _REPO / "bin" / ("win" if _WIN else "linux")


def test_valid_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPE_MODFLOW_BIN", str(tmp_path))
    assert runner.modflow_bin_dir() == str(tmp_path)


def test_stale_override_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPE_MODFLOW_BIN", str(tmp_path / "renamed-away"))
    d = Path(runner.modflow_bin_dir())
    assert d == _REPO / "bin" / ("win" if _WIN else "linux")


def test_modflow_available_in_repo(monkeypatch):
    # The bundled solvers ship in-repo for both platforms (bin/win committed 2026-07-18).
    monkeypatch.delenv("HYPE_MODFLOW_BIN", raising=False)
    assert runner.modflow_available() is True


def test_modflow_available_false_for_empty_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HYPE_MODFLOW_BIN", str(tmp_path))
    assert runner.modflow_available() is False


def test_bundled_win_exes_present():
    assert (_REPO / "bin" / "win" / "mf6.exe").is_file()
    assert (_REPO / "bin" / "win" / "mp7.exe").is_file()


def test_cli_settings_default_runs_particles(tmp_path):
    """The CLI/yaml path keeps its per-run MP7 pass; only the app passes run_particles=False."""
    from hypetool.inputs import Settings
    cfg = Settings(output_directory=tmp_path,
                   terrain_elevation_raster=__file__,           # any existing file
                   water_surface_elevation_raster=__file__)
    assert cfg.run_particles is True


def test_detect_never_picks_extensionless_on_windows(tmp_path):
    """A Linux ELF (extensionless mf6) in the folder must not be selected on Windows —
    the exact mis-selection behind the desktop crash."""
    (tmp_path / "mf6").write_bytes(b"\x7fELF-not-runnable-here")
    (tmp_path / "mp7").write_bytes(b"\x7fELF-not-runnable-here")
    found = detect_modflow_exes(tmp_path)
    if _WIN:
        assert found == {"mf6": None, "mp7": None}
        (tmp_path / "mf6.exe").write_bytes(b"MZ")
        (tmp_path / "mp7.exe").write_bytes(b"MZ")
        found = detect_modflow_exes(tmp_path)
        assert found["mf6"].endswith("mf6.exe")
        assert found["mp7"].endswith("mp7.exe")
    else:
        assert found["mf6"].endswith("mf6")
        assert found["mp7"].endswith("mp7")
