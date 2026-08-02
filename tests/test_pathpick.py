"""interpret_typed_target: the typed-picker rules that keep a bare folder path from
becoming "dump the project onto the Desktop" (the parent-of-the-typed-path bug)."""
import os
from pathlib import Path

import pytest

from hype_app import pathpick
from hype_app.pathpick import ensure_hype_suffix, interpret_typed_target


# ---------------------------------------------------------------- ensure_hype_suffix

def test_suffix_appended_not_replaced():
    # with_suffix would truncate the dotted stem to "Site v1.hype" — the old bug
    assert ensure_hype_suffix(Path("D:/x/Site v1.2")) == Path("D:/x/Site v1.2.hype")
    assert ensure_hype_suffix(Path("D:/x/SiteA")) == Path("D:/x/SiteA.hype")


def test_suffix_case_insensitive_passthrough():
    assert ensure_hype_suffix(Path("D:/x/A.HYPE")) == Path("D:/x/A.HYPE")
    assert ensure_hype_suffix(Path("D:/x/a.hype")) == Path("D:/x/a.hype")


def test_other_extension_appends_rather_than_replaces():
    assert ensure_hype_suffix(Path("D:/x/A.zip")) == Path("D:/x/A.zip.hype")


# ---------------------------------------------------------------- normalization

def test_empty_and_whitespace():
    assert interpret_typed_target("", purpose="save_as") == (None, pathpick.MSG_EMPTY)
    assert interpret_typed_target("   ", purpose="new_project") == \
        (None, pathpick.MSG_EMPTY)
    assert interpret_typed_target('""', purpose="new_project") == \
        (None, pathpick.MSG_EMPTY)


def test_double_quotes_stripped(tmp_path):
    raw = f'"{tmp_path / "A.hype"}"'
    assert interpret_typed_target(raw, purpose="save_as") == (tmp_path / "A.hype", None)


def test_single_quotes_stripped(tmp_path):
    raw = f"'{tmp_path / 'A.hype'}'"
    assert interpret_typed_target(raw, purpose="save_as") == (tmp_path / "A.hype", None)


def test_interior_apostrophe_survives(tmp_path):
    raw = str(tmp_path / "John's Folder" / "A")
    target, err = interpret_typed_target(raw, purpose="save_as")
    assert err is None
    assert target == tmp_path / "John's Folder" / "A.hype"


def test_relative_rejected():
    assert interpret_typed_target(r"Projects\SiteA.hype", purpose="new_project") == \
        (None, pathpick.MSG_ABS)


@pytest.mark.skipif(os.name != "nt", reason="drive-relative paths are a Windows notion")
def test_drive_relative_rejected():
    assert interpret_typed_target("D:SiteA.hype", purpose="new_project") == \
        (None, pathpick.MSG_ABS)
    assert interpret_typed_target(r"\SiteA.hype", purpose="new_project") == \
        (None, pathpick.MSG_ABS)


# ---------------------------------------------------------------- existing directories

def test_empty_dir_becomes_project_folder(tmp_path):
    d = tmp_path / "Fresh"
    d.mkdir()
    assert interpret_typed_target(str(d), purpose="new_project") == \
        (d / "Fresh.hype", None)


def test_cruft_only_dir_counts_as_empty(tmp_path):
    d = tmp_path / "Fresh"
    d.mkdir()
    (d / "desktop.ini").write_text("")
    (d / "Thumbs.db").write_text("")
    assert interpret_typed_target(str(d), purpose="save_as", known_stem="Mink") == \
        (d / "Fresh.hype", None)


def test_nonempty_dir_with_known_stem_derives_main(tmp_path):
    desk = tmp_path / "Desktop"
    desk.mkdir()
    (desk / "junk.txt").write_text("x")
    # THE incident shape, save_as flavor: the folder gate downstream offers the subfolder
    assert interpret_typed_target(str(desk), purpose="save_as",
                                  known_stem="MinkBrook") == \
        (desk / "MinkBrook.hype", None)


def test_nonempty_dir_without_stem_errors_with_example(tmp_path):
    desk = tmp_path / "Desktop"
    desk.mkdir()
    (desk / "junk.txt").write_text("x")
    target, err = interpret_typed_target(str(desk), purpose="new_project")
    assert target is None
    assert err == pathpick._dir_full_msg(str(desk / "MyProject.hype"))
    assert str(desk) in err


def test_trailing_slash_dir_intent_nonexistent(tmp_path):
    raw = str(tmp_path / "NewSite") + os.sep
    assert interpret_typed_target(raw, purpose="new_project") == \
        (tmp_path / "NewSite" / "NewSite.hype", None)


def test_forward_slash_dir_intent(tmp_path):
    raw = str(tmp_path / "NewSite").replace("\\", "/") + "/"
    target, err = interpret_typed_target(raw, purpose="save_as", known_stem="X")
    assert err is None
    assert target == tmp_path / "NewSite" / "NewSite.hype"


# ---------------------------------------------------------------- file-ish saves

def test_plain_file_path_appends_suffix(tmp_path):
    assert interpret_typed_target(str(tmp_path / "Site v1.2"), purpose="save_as") == \
        (tmp_path / "Site v1.2.hype", None)


def test_hype_path_passthrough(tmp_path):
    assert interpret_typed_target(str(tmp_path / "A.hype"), purpose="import_target") == \
        (tmp_path / "A.hype", None)


# ---------------------------------------------------------------- open_project

def test_open_existing_dir_errors(tmp_path):
    assert interpret_typed_target(str(tmp_path), purpose="open_project") == \
        (None, pathpick.MSG_OPEN_DIR)


def test_open_trailing_slash_errors(tmp_path):
    raw = str(tmp_path / "nope") + os.sep
    assert interpret_typed_target(raw, purpose="open_project") == \
        (None, pathpick.MSG_OPEN_DIR)


def test_open_never_appends_suffix(tmp_path):
    # "file doesn't exist" handling stays downstream; the text must pass through as typed
    raw = str(tmp_path / "SiteA")
    assert interpret_typed_target(raw, purpose="open_project") == \
        (tmp_path / "SiteA", None)
