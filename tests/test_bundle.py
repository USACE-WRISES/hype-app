"""Project-archive v2 round-trip + v1 legacy-adapter tests (spec §4.4, §13.5)."""
import json
import zipfile

import pytest

from hype_app import bundle

_FEATURE = {"type": "Feature",
            "geometry": {"type": "LineString", "coordinates": [[0, 0], [1, 1]]},
            "properties": {}}


def _make_workspace(root):
    """Populate a session work_dir with representative artifacts zip_workspace reads."""
    (root / "inputs").mkdir(parents=True)
    (root / "inputs" / "dem.tif").write_bytes(b"FAKE-DEM")
    (root / "data_sources" / "usgs").mkdir(parents=True)
    (root / "data_sources" / "usgs" / "delineate.json").write_text('{"ok": true}')
    (root / "alternatives").mkdir(parents=True)
    (root / "alternatives" / "index.json").write_text('{"scenarios": []}')
    (root / "alternatives" / "k10_gradient1" / "summary" / "hz").mkdir(parents=True)
    (root / "alternatives" / "k10_gradient1" / "summary" / "hz" / "hz_stats.json"
     ).write_text('{"classes": {}}')
    (root / "report").mkdir(parents=True)
    (root / "report" / "report.html").write_text("<html></html>")
    # heavy computed trees — what the settings-only scope leaves behind
    (root / "ras").mkdir()
    (root / "ras" / "project.prj").write_text("RAS")
    (root / "model" / "gwf_workspace").mkdir(parents=True)
    (root / "model" / "gwf_workspace" / "sim.nam").write_text("nam")
    (root / "summary" / "head").mkdir(parents=True)
    (root / "summary" / "head" / "head_L1.tif").write_bytes(b"FAKE-HEAD")


def test_format_version_is_2():
    assert bundle.FORMAT_VERSION == 2


def test_extra_trees_pack_but_never_restore(tmp_path):
    """extra_trees (the GMS export rides this) pack under ROOT with forward-slash
    arcs, and restore must IGNORE them: a one-way export, never workspace state."""
    src = tmp_path / "session_a"
    src.mkdir()
    _make_workspace(src)
    gms_src = tmp_path / "gms_build"
    (gms_src / "Site_MODFLOW").mkdir(parents=True)
    (gms_src / "Site.gpr").write_bytes(b"FAKE-GPR")
    (gms_src / "Site_MODFLOW" / "Site.mfn").write_text("# name file")

    zip_path = bundle.zip_workspace(src, vectors={"reach": _FEATURE},
                                    state={"format_version": 2},
                                    extra_trees=(("GMS", gms_src),))
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
    assert f"{bundle.ROOT}/GMS/Site.gpr" in names
    assert f"{bundle.ROOT}/GMS/Site_MODFLOW/Site.mfn" in names
    assert not [n for n in names if "\\" in n]

    dst = tmp_path / "session_b"
    dst.mkdir()
    out = bundle.restore_workspace(zip_path, dst)
    assert not (dst / "GMS").exists()
    assert not [p for p in out["restored"] if "GMS" in p or ".gpr" in p]


def test_zip_workspace_without_extra_trees_unchanged(tmp_path):
    src = tmp_path / "session_a"
    src.mkdir()
    _make_workspace(src)
    zip_path = bundle.zip_workspace(src, vectors={"reach": _FEATURE},
                                    state={"format_version": 2})
    with zipfile.ZipFile(zip_path) as zf:
        assert not [n for n in zf.namelist() if n.startswith(f"{bundle.ROOT}/GMS/")]


def test_v2_roundtrip(tmp_path):
    src = tmp_path / "session_a"
    src.mkdir()
    _make_workspace(src)

    vectors = {"reach": _FEATURE, "k_zones": [_FEATURE, _FEATURE]}
    state = {"format_version": 2, "app_version": "2026.07", "sel_node": "gw.res"}
    params = {"kh": 10.0, "kv": 1.0}
    assessment_input = {"schema_version": "assessment-input-snapshot/2.0", "assessment_id": "A1"}
    scoring_profile = {"profile_id": "hfci-v1", "version": "1.0.0"}

    zip_path = bundle.zip_workspace(
        src, vectors=vectors, params=params, run_config={"working_crs": {"epsg": 26919}},
        state=state, assessment_input=assessment_input, scoring_profile=scoring_profile)

    dst = tmp_path / "session_b"
    dst.mkdir()
    out = bundle.restore_workspace(zip_path, dst)

    assert out["state"] == state
    assert out["params"] == params
    assert out["assessment_input"] == assessment_input
    assert out["scoring_profile"] == scoring_profile
    # vectors: single feature vs list preserved
    assert out["vectors"]["reach"] == _FEATURE
    assert out["vectors"]["k_zones"] == [_FEATURE, _FEATURE]
    # v2 trees restored to the workspace layout
    assert (dst / "inputs" / "dem.tif").read_bytes() == b"FAKE-DEM"
    assert (dst / "data_sources" / "usgs" / "delineate.json").exists()
    assert (dst / "alternatives" / "index.json").exists()
    assert (dst / "alternatives" / "k10_gradient1" / "summary" / "hz"
            / "hz_stats.json").exists()
    # the restored-manifest the app gates stage state on
    assert "inputs/dem.tif" in out["restored"]
    assert "model/gwf_workspace/sim.nam" in out["restored"]
    assert any(p.startswith("alternatives/") for p in out["restored"])


def test_site_metadata_survives_roundtrip(tmp_path):
    """The site identity (name/analyst/org/date) in the frozen snapshot must round-trip through
    the archive so a reopened project regenerates an identical report (§11.1, §14.18). This is
    the exact data _apply_project reads back into the site-metadata inputs."""
    src = tmp_path / "s"
    src.mkdir()
    _make_workspace(src)
    snap = {"schema_version": "assessment-input-snapshot/2.0", "assessment_id": "A1",
            "site": {"site_name": "Mink Brook", "analyst": "A. Hydrologist",
                     "organization": "USACE ERDC", "assessment_date": "2026-07-11",
                     "reach_length_m": 904.7}}
    zip_path = bundle.zip_workspace(src, vectors={}, state={"format_version": 2},
                                    assessment_input=snap)
    dst = tmp_path / "d"
    dst.mkdir()
    site = (bundle.restore_workspace(zip_path, dst)["assessment_input"] or {}).get("site") or {}
    assert site["site_name"] == "Mink Brook"
    assert site["analyst"] == "A. Hydrologist"
    assert site["organization"] == "USACE ERDC"
    assert site["assessment_date"] == "2026-07-11"
    assert site["reach_length_m"] == 904.7
    assert (dst / "report" / "report.html").exists()


def test_full_scope_includes_computed_trees(tmp_path):
    """Default include_computed=True packs every heavy/derived tree (today's behavior)."""
    src = tmp_path / "s"
    src.mkdir()
    _make_workspace(src)
    zip_path = bundle.zip_workspace(src, vectors={}, state={"format_version": 2})
    names = zipfile.ZipFile(zip_path).namelist()
    for arc in (f"{bundle.ROOT}/2_Terrain/dem.tif",
                f"{bundle.ROOT}/4_Surface_Water/HEC-RAS/project.prj",
                f"{bundle.ROOT}/5_Groundwater/model/gwf_workspace/sim.nam",
                f"{bundle.ROOT}/5_Groundwater/Results/head/head_L1.tif",
                f"{bundle.ROOT}/alternatives/index.json",
                f"{bundle.ROOT}/6_Site_Report/report.html"):
        assert arc in names


def test_settings_only_scope(tmp_path):
    """include_computed=False keeps vectors + config + data_sources (small, provenance) but
    drops every computed/derived tree — and the light archive still restores cleanly, with
    the heavy stages simply absent from the new workspace."""
    src = tmp_path / "s"
    src.mkdir()
    _make_workspace(src)
    state = {"format_version": 2, "app_version": "2026.07"}
    zip_path = bundle.zip_workspace(src, vectors={"reach": _FEATURE}, params={"kh": 5.0},
                                    state=state, include_computed=False)

    zf = zipfile.ZipFile(zip_path)
    names = zf.namelist()
    assert f"{bundle.ROOT}/config/state.json" in names
    assert f"{bundle.ROOT}/1_Reach_Centerline/reach_centerline.geojson" in names
    assert f"{bundle.ROOT}/data_sources/usgs/delineate.json" in names
    heavy = ("2_Terrain/", "4_Surface_Water/", "5_Groundwater/", "6_Site_Report/",
             "alternatives/")
    assert not [n for n in names if any(f"/{h}" in n for h in heavy)]
    assert "Saved without computed data" in zf.read(f"{bundle.ROOT}/README.txt").decode()

    dst = tmp_path / "d"
    dst.mkdir()
    out = bundle.restore_workspace(zip_path, dst)
    assert out["state"] == state
    assert out["params"] == {"kh": 5.0}
    assert out["vectors"]["reach"] == _FEATURE
    assert not (dst / "inputs" / "dem.tif").exists()
    assert not (dst / "model" / "gwf_workspace").exists()
    # no computed artifacts in the restored-manifest -> the app restores gw/alts as not-done
    assert not any(p.startswith(("model/gwf_workspace/", "alternatives/"))
                   for p in out["restored"])


def test_legacy_sensitivity_arcs_are_dropped(tmp_path):
    """Archives from the retired gradient-bounds sweep carry sensitivity/ arcs. They restore
    with no error and produce NO output tree: old sensitivity results do not come back, and
    a stray on-disk sensitivity/ folder never trips the clash check as foreign content."""
    src = tmp_path / "legacy"
    (src / "sensitivity").mkdir(parents=True)
    (src / "sensitivity" / "manifest.json").write_text('{"scenarios": []}')
    (src / "inputs").mkdir()
    (src / "inputs" / "dem.tif").write_bytes(b"FAKE-DEM")
    zip_path = bundle.zip_workspace(src, vectors={}, state={"format_version": 2},
                                    extra_trees=(("sensitivity", src / "sensitivity"),))
    dst = tmp_path / "d"
    dst.mkdir()
    out = bundle.restore_workspace(zip_path, dst)
    assert not (dst / "sensitivity").exists()
    assert not any(p.startswith("sensitivity/") for p in out["restored"])
    # clash check: a leftover sensitivity/ dir is ours (LEGACY_DIRS), not foreign content
    proj = tmp_path / "proj"
    (proj / "sensitivity").mkdir(parents=True)
    names, foreign, others = bundle.folder_clash(proj, proj / "Site.hype")
    assert not foreign and "sensitivity" not in others and "sensitivity" not in names


def test_v1_archive_opens_via_legacy_adapter(tmp_path):
    """A v1 project (format_version 1, no assessment_input/scoring_profile) still opens; the new
    pieces come back as None rather than raising."""
    src = tmp_path / "v1"
    src.mkdir()
    _make_workspace(src)
    state = {"format_version": 1, "app_version": "2026.07"}

    zip_path = bundle.zip_workspace(src, vectors={"reach": _FEATURE}, params={"kh": 5.0},
                                    state=state)  # no assessment_input / scoring_profile
    dst = tmp_path / "v1_out"
    dst.mkdir()
    out = bundle.restore_workspace(zip_path, dst)

    assert out["state"]["format_version"] == 1
    assert out["assessment_input"] is None
    assert out["scoring_profile"] is None
    assert out["params"] == {"kh": 5.0}


def test_newer_format_is_rejected(tmp_path):
    src = tmp_path / "future"
    src.mkdir()
    _make_workspace(src)
    zip_path = bundle.zip_workspace(src, vectors={}, state={"format_version": 99})
    dst = tmp_path / "future_out"
    dst.mkdir()
    with pytest.raises(bundle.ProjectError):
        bundle.restore_workspace(zip_path, dst)


def test_non_hype_zip_rejected(tmp_path):
    import zipfile
    bogus = tmp_path / "bogus.zip"
    with zipfile.ZipFile(bogus, "w") as zf:
        zf.writestr("random.txt", "nope")
    with pytest.raises(bundle.ProjectError):
        bundle.restore_workspace(bogus, tmp_path / "out")


# ---------------------------------------------------------------- project metadata keys

def test_project_meta_keys_roundtrip_both_scopes(tmp_path):
    """project_name/units/created are first-class state keys and must ride BOTH bundle
    kinds: Complete and settings-only (the desktop main-file path)."""
    state = {"format_version": 2, "project_name": "Mink Creek",
             "project_units": "metric", "project_created": "2026-07-23T08:15:00"}
    for include_computed in (True, False):
        src = tmp_path / f"src_{include_computed}"
        src.mkdir()
        _make_workspace(src)
        zip_path = bundle.zip_workspace(src, vectors={"reach": _FEATURE},
                                        state=dict(state),
                                        include_computed=include_computed)
        dst = tmp_path / f"dst_{include_computed}"
        dst.mkdir()
        st = bundle.restore_workspace(zip_path, dst)["state"]
        assert st["project_name"] == "Mink Creek", include_computed
        assert st["project_units"] == "metric", include_computed
        assert st["project_created"] == "2026-07-23T08:15:00", include_computed


def test_legacy_state_without_meta_passes_through(tmp_path):
    """Pre-metadata bundles restore with no meta keys injected — migration (stem
    fallback, metric default) is the app's job, not the bundle layer's."""
    src = tmp_path / "src"
    src.mkdir()
    _make_workspace(src)
    zip_path = bundle.zip_workspace(src, vectors={}, state={"format_version": 2})
    dst = tmp_path / "dst"
    dst.mkdir()
    st = bundle.restore_workspace(zip_path, dst)["state"]
    assert "project_name" not in st
    assert "project_units" not in st
    assert "project_created" not in st


def test_fp_line_style_state_keys_round_trip(tmp_path):
    """The flow-path line display prefs ride project state (app.py _project_state ->
    restore). This pins the five key names both ends must agree on."""
    src = tmp_path / "session_fp"
    src.mkdir()
    _make_workspace(src)
    state = {"format_version": 2,
             "fp_line_show": False, "fp_line_weight": 4.5, "fp_line_opacity": 0.35,
             "fp_line_mode": "single", "fp_line_color": "#ba2d8e"}
    zip_path = bundle.zip_workspace(src, vectors={"reach": _FEATURE}, state=state,
                                    include_computed=False)
    dst = tmp_path / "restored"
    dst.mkdir()
    out = bundle.restore_workspace(zip_path, dst)
    st = out["state"]
    assert st["fp_line_show"] is False
    assert st["fp_line_weight"] == 4.5
    assert st["fp_line_opacity"] == 0.35
    assert st["fp_line_mode"] == "single"
    assert st["fp_line_color"] == "#ba2d8e"
