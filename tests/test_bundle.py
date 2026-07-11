"""Project-archive v2 round-trip + v1 legacy-adapter tests (spec §4.4, §13.5)."""
import json

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
    (root / "sensitivity").mkdir(parents=True)
    (root / "sensitivity" / "manifest.json").write_text('{"scenarios": []}')
    (root / "report").mkdir(parents=True)
    (root / "report" / "report.html").write_text("<html></html>")


def test_format_version_is_2():
    assert bundle.FORMAT_VERSION == 2


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
    assert (dst / "sensitivity" / "manifest.json").exists()


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
