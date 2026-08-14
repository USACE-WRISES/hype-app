"""Unit tests for batch.py site selection (pick_sites is pure).

The load-bearing properties: workbook order is preserved, complete sites are
skipped unless --force, the pilot (SKIP_ALWAYS) is excluded by default but an
explicit --sites list overrides that, and unknown ids abort before any run.
"""
from __future__ import annotations

import pytest

from tools.site_factory import batch

IDS = ["CH00156", "CH00173", "LL01096", "SS02286"]


def test_default_excludes_pilot_and_preserves_order():
    run, skipped = batch.pick_sites(IDS, complete=set())
    assert run == ["CH00156", "CH00173", "SS02286"]
    assert skipped == []


def test_complete_sites_skipped_with_reason():
    run, skipped = batch.pick_sites(IDS, complete={"CH00173"})
    assert run == ["CH00156", "SS02286"]
    assert skipped == [("CH00173", "complete")]


def test_force_reruns_complete_sites():
    run, skipped = batch.pick_sites(IDS, complete={"CH00173"}, force=True)
    assert run == ["CH00156", "CH00173", "SS02286"]
    assert skipped == []


def test_explicit_list_overrides_pilot_skip_but_not_complete_skip():
    run, skipped = batch.pick_sites(IDS, complete={"LL01096"}, explicit=["LL01096", "CH00156"])
    assert run == ["CH00156"]
    assert skipped == [("LL01096", "complete")]


def test_explicit_list_keeps_workbook_order():
    run, _ = batch.pick_sites(IDS, complete=set(), explicit=["SS02286", "CH00156"])
    assert run == ["CH00156", "SS02286"]


def test_unknown_explicit_id_aborts():
    with pytest.raises(SystemExit, match="ZZ99999"):
        batch.pick_sites(IDS, complete=set(), explicit=["CH00156", "ZZ99999"])


def _fake_run_workspace(tmp_path, *, with_hz=True):
    """Minimal durable-artifact layout results_state() reads."""
    import json
    work = tmp_path / "SITE1"
    inputs = work / "inputs"
    inputs.mkdir(parents=True)
    depth = work / "ras" / "depth_last.tif"
    depth.parent.mkdir()
    depth.write_bytes(b"x")
    (inputs / "ras_result.json").write_text(json.dumps(
        {"depth_tif": str(depth), "wse_for_gw": str(inputs / "wse_ras.tif")}))
    (inputs / "wse_filter.json").write_text(json.dumps(
        {"path": str(inputs / "wse_gw_filtered.tif")}))
    (inputs / "gw_result.json").write_text(json.dumps(
        {"grid": {"ncol": 2, "nrow": 3, "nlay": 4}}))
    tif_dir = work / "summary" / "head" / "per_layer_tif"
    tif_dir.mkdir(parents=True)
    for i in (2, 1):
        (tif_dir / f"head_L{i:02d}.tif").write_bytes(b"x")
    (work / "model" / "gwf_workspace").mkdir(parents=True)
    if with_hz:
        hz_dir = work / "summary" / "hz"
        hz_dir.mkdir()
        (inputs / "hz_result.json").write_text(json.dumps(
            {"hz_dir": str(hz_dir), "stats": {"version": 1}}))
    (work / "assessment_results.json").write_text(json.dumps(
        {"schema_version": "assessment-results/2.5"}))
    return work, inputs


def test_results_state_builds_all_keys(tmp_path):
    from tools.site_factory.drive import results_state

    work, inputs = _fake_run_workspace(tmp_path)
    st = results_state(work, inputs, "SITE1")
    assert set(st) == {"ras_result", "wse_used", "run_result", "hz_result",
                       "results_model"}
    assert st["wse_used"].endswith("wse_gw_filtered.tif")
    rr = st["run_result"]
    assert rr["points_fc"] is None and rr["contours"] == {}
    import os
    assert [os.path.basename(t) for t in rr["head"]["geotiffs"]] \
        == ["head_L01.tif", "head_L02.tif"]
    assert rr["grid"] == {"ncol": 2, "nrow": 3, "nlay": 4}
    assert st["hz_result"]["stats"] == {"version": 1}


def test_results_state_partial_workspace_omits_missing(tmp_path):
    from tools.site_factory.drive import results_state

    work, inputs = _fake_run_workspace(tmp_path, with_hz=False)
    (inputs / "wse_filter.json").unlink()
    st = results_state(work, inputs, "SITE1")
    assert "hz_result" not in st
    assert st["wse_used"].endswith("wse_ras.tif")   # falls back to ras_result
    assert results_state(tmp_path / "empty", tmp_path / "empty" / "inputs", "X") == {}


def test_merge_state_results_fill_only_when_app_absent():
    from tools.site_factory import appstate

    res = {"run_result": {"grid": {"nlay": 4}}, "hz_result": {"hz_dir": "t"},
           "wse_used": "w"}
    kw = dict(site_id="S", factory_wells=[], aerial_layers=[], format_version=2)
    fresh = appstate.merge_state(None, results_state=res, **kw)
    assert fresh["run_result"] == {"grid": {"nlay": 4}} and fresh["wse_used"] == "w"
    # app-authored keys win; factory fills only the gaps (incl. explicit nulls)
    app = {"run_result": {"grid": {"nlay": 9}}, "hz_result": None}
    merged = appstate.merge_state(app, results_state=res, **kw)
    assert merged["run_result"] == {"grid": {"nlay": 9}}
    assert merged["hz_result"] == {"hz_dir": "t"}


def test_ras_progress_heartbeat_and_milestones():
    """Milestones every 10%, heartbeat at most every heartbeat_s inside a band: slow
    big-mesh solves must never look frozen in the log (the SS02107 false alarm)."""
    from tools.site_factory.drive import make_ras_progress_logger

    t = {"v": 0.0}
    lines = []
    p = make_ras_progress_logger(lines.append, clock=lambda: t["v"], heartbeat_s=300)
    p("Computing", None)                    # stage header only
    p("Computing", 0)                       # 0% milestone
    t["v"] = 200; p("Computing", 3)         # inside band, heartbeat not due: silent
    t["v"] = 350; p("Computing", 4)         # heartbeat due
    t["v"] = 400; p("Computing", 10)        # next milestone
    assert lines == [
        "[Computing]",
        "[Computing] 0% (0.0 min elapsed)",
        "[Computing] still at 4% (5.8 min elapsed, ~140 min left)",
        "[Computing] 10% (6.7 min elapsed, ~60 min left)",
    ]


def test_failed_stage_reads_newest_error_marker(tmp_path):
    (tmp_path / "_error_gw.txt").write_text("older", encoding="utf-8")
    newer = tmp_path / "_error_hz.txt"
    newer.write_text("newer", encoding="utf-8")
    import os
    import time
    old = time.time() - 100
    os.utime(tmp_path / "_error_gw.txt", (old, old))
    assert batch.failed_stage(tmp_path) == "hz"
    assert batch.failed_stage(tmp_path / "nope") == "?"
