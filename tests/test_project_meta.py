"""Project identity metadata helpers (hype_app/project_meta.py)."""
from hype_app import project_meta


# ---------------------------------------------------------------- meta_from_state

def test_stored_name_beats_fallback():
    st = {"project_name": "Mink Creek", "project_units": "metric",
          "project_created": "2026-07-23T08:00:00"}
    m = project_meta.meta_from_state(st, fallback_name="upload_stem")
    assert m == {"name": "Mink Creek", "units": "metric",
                 "created": "2026-07-23T08:00:00"}


def test_fallback_stem_used_when_absent():
    assert project_meta.meta_from_state({}, fallback_name="SiteA")["name"] == "SiteA"
    assert project_meta.meta_from_state(None)["name"] is None


def test_whitespace_names_are_none():
    m = project_meta.meta_from_state({"project_name": "   "}, fallback_name="  ")
    assert m["name"] is None


def test_unknown_units_degrade_to_metric():
    # a newer save (or a corrupt one) must never break the pane or the state write
    assert project_meta.meta_from_state({"project_units": "us_ft"})["units"] == "metric"
    assert project_meta.meta_from_state({"project_units": 7})["units"] == "metric"
    assert project_meta.meta_from_state({})["units"] == "metric"


def test_created_passthrough_and_absent():
    assert project_meta.meta_from_state({"project_created": "2026-01-02T03:04:05"})[
        "created"] == "2026-01-02T03:04:05"
    assert project_meta.meta_from_state({})["created"] is None


# ---------------------------------------------------------------- filename_stem

def test_filename_stem_strips_illegal_chars():
    assert project_meta.filename_stem('My: Site<1>/"v2"') == "My Site 1 v2"


def test_filename_stem_trims_trailing_dots_and_spaces():
    # Win32 silently strips these on create; pre-strip so the name round-trips
    assert project_meta.filename_stem("Site.") == "Site"
    assert project_meta.filename_stem("Site . . ") == "Site"


def test_filename_stem_clamps_length():
    assert len(project_meta.filename_stem("x" * 200)) == 80


def test_filename_stem_fallback():
    assert project_meta.filename_stem("", "fb") == "fb"
    assert project_meta.filename_stem("///:::", "fb") == "fb"
    assert project_meta.filename_stem(None, "fb") == "fb"
    assert project_meta.filename_stem("...", "fb") == "fb"


# ---------------------------------------------------------------- created_display

def test_created_display_formats_iso():
    assert project_meta.created_display("2026-07-23T08:15:00") == "Jul 23, 2026"


def test_created_display_tolerates_garbage():
    assert project_meta.created_display(None) == "Not recorded"
    assert project_meta.created_display("") == "Not recorded"
    assert project_meta.created_display("not-a-date") == "Not recorded"
