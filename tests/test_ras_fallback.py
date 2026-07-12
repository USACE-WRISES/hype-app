"""Windows dev RAS resolution: HYPE_RAS_BIN wins when valid; when it is unset or stale
(e.g. an absolute path left over from a repo-folder rename) the git-ignored in-repo
install under reference/ is used; Linux bundle behavior is untouched."""
import sys
from pathlib import Path

import pytest

from hype_app import ras


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")


@pytest.fixture
def ref_install(tmp_path, monkeypatch):
    """A fake repo root whose reference/ folder holds a HEC-RAS 2025 dev install."""
    exe = tmp_path / "reference" / "HEC-RAS_2025" / "HEC-RAS 2025 Alpha" / "ras.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(ras, "_APP_ROOT", tmp_path)
    return exe


def test_unset_override_uses_in_repo_install(win, ref_install, monkeypatch):
    monkeypatch.delenv("HYPE_RAS_BIN", raising=False)
    assert ras.ras_available()
    argv, _env = ras.ras_cmd()
    assert argv == [str(ref_install)]


def test_stale_override_falls_back_to_in_repo_install(win, ref_install, monkeypatch):
    monkeypatch.setenv("HYPE_RAS_BIN", r"D:\no\such\hype-app_fable\HEC-RAS 2025 Alpha")
    assert ras.ras_available()
    argv, _env = ras.ras_cmd()
    assert argv == [str(ref_install)]


def test_valid_override_beats_in_repo_install(win, ref_install, tmp_path, monkeypatch):
    other = tmp_path / "elsewhere"
    other.mkdir()
    (other / "ras.exe").write_bytes(b"MZ")
    monkeypatch.setenv("HYPE_RAS_BIN", str(other))
    assert ras.ras_available()
    argv, _env = ras.ras_cmd()
    assert argv == [str(other / "ras.exe")]


def test_stale_override_without_install_is_unavailable(win, tmp_path, monkeypatch):
    monkeypatch.setattr(ras, "_APP_ROOT", tmp_path)      # no reference/ install here
    monkeypatch.setenv("HYPE_RAS_BIN", str(tmp_path / "missing"))
    assert not ras.ras_available()


def test_fallback_is_windows_only(ref_install, monkeypatch):
    """On Linux the reference/ install is ignored: no override -> bundle check only."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("HYPE_RAS_BIN", raising=False)
    assert not ras.ras_available()      # fake root has no bin/ras2025/app/ras.dll
    argv, _env = ras.ras_cmd()
    assert argv[0].endswith("dotnet")   # bundle argv, not the reference exe
