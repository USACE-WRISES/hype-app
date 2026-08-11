"""gms.publish.refresh_gms_tree: staging-swap lifecycle for the live GMS/ folder.

Pure filesystem tests with a stub exporter (no MODFLOW fixtures, no engine mark).
The contracts pinned here keep the app-side task safe: never raises, unique staging
inside the project folder, precheck vetoes before any destruction, locks keep the
old tree, EXPORT_ERROR.txt only when there is nothing better to keep.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hype_app import bundle
from hype_app.gms import publish


def _stub_exporter(calls: list | None = None, *, fail: Exception | None = None,
                   marker: str = "fresh"):
    def exporter(work_dir, out_dir, **kw):
        if calls is not None:
            calls.append({"out_dir": Path(out_dir), **kw})
        if fail is not None:
            raise fail
        (Path(out_dir) / "Site.gpr").write_text(marker, encoding="utf-8")
        (Path(out_dir) / "Site_MODFLOW").mkdir()
        return {"warnings": ["w1"], "n_particles": {"forward": 3}}
    return exporter


def _refresh(wd, **over):
    kw = dict(name="Site", crs_wkt_esri="", porosity=0.3, include_hz=False,
              log=lambda m: None)
    kw.update(over)
    return publish.refresh_gms_tree(wd, **kw)


def test_fresh_build(tmp_path):
    calls = []
    res = _refresh(tmp_path, exporter=_stub_exporter(calls))
    assert res["ok"] is True and res["error"] is None
    assert (tmp_path / "GMS" / "Site.gpr").read_text(encoding="utf-8") == "fresh"
    assert res["warnings"] == ["w1"] and res["n_particles"] == {"forward": 3}
    # staging fully consumed by the swap, and it was same-volume (inside work_dir)
    assert not list(tmp_path.glob(publish.STAGING_PREFIX + "*"))
    assert calls[0]["out_dir"].parent == tmp_path


def test_rebuild_fully_replaces(tmp_path):
    (tmp_path / "GMS").mkdir()
    (tmp_path / "GMS" / "old_only.txt").write_text("stale", encoding="utf-8")
    res = _refresh(tmp_path, exporter=_stub_exporter(marker="v2"))
    assert res["ok"] is True
    assert (tmp_path / "GMS" / "Site.gpr").read_text(encoding="utf-8") == "v2"
    assert not (tmp_path / "GMS" / "old_only.txt").exists()
    assert not list(tmp_path.glob(publish.STAGING_PREFIX + "*"))


def test_exporter_failure_keeps_existing_tree(tmp_path):
    (tmp_path / "GMS").mkdir()
    (tmp_path / "GMS" / "Site.gpr").write_text("good", encoding="utf-8")
    res = _refresh(tmp_path, exporter=_stub_exporter(fail=RuntimeError("boom")))
    assert res["ok"] is False and res["kept_old"] is True
    assert "boom" in res["error"]
    assert (tmp_path / "GMS" / "Site.gpr").read_text(encoding="utf-8") == "good"
    assert not (tmp_path / "GMS" / "EXPORT_ERROR.txt").exists()   # never clobber a good tree
    assert not list(tmp_path.glob(publish.STAGING_PREFIX + "*"))


def test_exporter_failure_without_tree_writes_breadcrumb(tmp_path):
    res = _refresh(tmp_path, exporter=_stub_exporter(fail=RuntimeError("no CHD")))
    assert res["ok"] is False and res["kept_old"] is False
    note = (tmp_path / "GMS" / "EXPORT_ERROR.txt").read_text(encoding="utf-8")
    assert "no CHD" in note and "unaffected" in note
    assert not list(tmp_path.glob(publish.STAGING_PREFIX + "*"))


def test_precheck_veto_touches_nothing(tmp_path):
    (tmp_path / "GMS").mkdir()
    (tmp_path / "GMS" / "Site.gpr").write_text("keep", encoding="utf-8")
    res = _refresh(tmp_path, exporter=_stub_exporter(), precheck=lambda: False)
    assert res == {"ok": False, "skipped": True, "error": None,
                   "warnings": ["w1"], "n_particles": {"forward": 3}, "kept_old": False}
    assert (tmp_path / "GMS" / "Site.gpr").read_text(encoding="utf-8") == "keep"
    assert not list(tmp_path.glob(publish.STAGING_PREFIX + "*"))


def test_locked_final_keeps_old_tree(tmp_path, monkeypatch):
    (tmp_path / "GMS").mkdir()
    (tmp_path / "GMS" / "Site.gpr").write_text("locked", encoding="utf-8")
    real_rmtree = shutil.rmtree

    def deny_final(path, *a, **kw):
        if Path(path).name == "GMS":
            raise PermissionError("held open by GMS")
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(shutil, "rmtree", deny_final)
    res = _refresh(tmp_path, exporter=_stub_exporter())
    assert res["ok"] is False and res["kept_old"] is True
    assert "in use" in res["error"]
    assert (tmp_path / "GMS" / "Site.gpr").read_text(encoding="utf-8") == "locked"


def test_stale_staging_swept_before_build(tmp_path):
    stale = tmp_path / (publish.STAGING_PREFIX + "-00000000")
    stale.mkdir()
    (stale / "half.gpr").write_text("partial", encoding="utf-8")
    res = _refresh(tmp_path, exporter=_stub_exporter())
    assert res["ok"] is True
    assert not stale.exists()


def test_include_hz_routes_hz_dir(tmp_path):
    calls = []
    _refresh(tmp_path, exporter=_stub_exporter(calls), include_hz=False)
    assert calls[-1]["hz_dir"] is None
    _refresh(tmp_path, exporter=_stub_exporter(calls), include_hz=True)
    assert calls[-1]["hz_dir"] == tmp_path / "summary" / "hz"


def test_porosity_prefers_hz_stats_when_including_hz(tmp_path):
    hz = tmp_path / "summary" / "hz"
    hz.mkdir(parents=True)
    (hz / "hz_stats.json").write_text('{"knobs": {"porosity": 0.17}}', encoding="utf-8")
    calls = []
    _refresh(tmp_path, exporter=_stub_exporter(calls), include_hz=True, porosity=0.3)
    assert calls[-1]["porosity"] == pytest.approx(0.17)
    _refresh(tmp_path, exporter=_stub_exporter(calls), include_hz=False, porosity=0.3)
    assert calls[-1]["porosity"] == pytest.approx(0.3)   # pane fallback without particles


def test_export_dirs_ties_to_publish_name():
    assert publish.GMS_DIRNAME in bundle.EXPORT_DIRS
    assert bundle.EXPORT_DIRS[0] == publish.GMS_DIRNAME
