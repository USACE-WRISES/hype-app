"""Versioning + shipped changelog ("What's new").

CHANGELOG.md at the repo root is the single source of release notes: rendered in-app by
the What's new dialog (opened by clicking the version number in the header chip, the
welcome splash, or the About footer), shipped inside the desktop apps payload via the
git-archive pathspec, and mirrored onto the GitHub v* release body by the shell workflow.
These tests pin the file's format, the version lockstep (app.py / csproj / changelog),
the ship wires, and the app wiring.
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_SRC = (ROOT / "app.py").read_text(encoding="utf-8")
CHANGELOG = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

SECTION_RE = re.compile(r"^## v(\d+\.\d+\.\d+) \((\d{4}-\d{2}-\d{2})\)$", re.MULTILINE)


def _app_version() -> str:
    m = re.search(r'^APP_VERSION = "(\d+\.\d+\.\d+)"', APP_SRC, re.MULTILINE)
    assert m, "APP_VERSION literal not found in app.py"
    return m.group(1)


# ------------------------------------------------------------------ file format

def test_changelog_sections_format_and_order():
    sections = SECTION_RE.findall(CHANGELOG)
    assert sections, "CHANGELOG.md has no '## vX.Y.Z (YYYY-MM-DD)' sections"
    # every '## ' heading must be a well-formed section header (no strays)
    headings = re.findall(r"^## .*$", CHANGELOG, re.MULTILINE)
    assert len(headings) == len(sections)
    for _, date in sections:
        datetime.strptime(date, "%Y-%m-%d")   # a real calendar date, not just digit shapes
    versions = [tuple(int(p) for p in v.split(".")) for v, _ in sections]
    assert versions == sorted(versions, reverse=True), "sections must be newest first"
    assert len(set(versions)) == len(versions), "duplicate version section"


def test_changelog_top_section_is_app_version():
    # The release runbook bumps APP_VERSION and writes the matching section together.
    assert SECTION_RE.search(CHANGELOG).group(1) == _app_version()


def test_changelog_is_clean_user_copy():
    assert "—" not in CHANGELOG, "no em dashes in user-facing copy (project rule)"
    # one H1 and it is the first line; the app strips it before rendering
    assert CHANGELOG.startswith("# Changelog\n")
    assert len(re.findall(r"^# ", CHANGELOG, re.MULTILINE)) == 1
    # the H1-strip in app.py's _changelog_md leaves the newest section on top
    stripped = re.sub(r"^# .*\n+", "", CHANGELOG, count=1)
    assert stripped.startswith("## v")


# ------------------------------------------------------------- version lockstep

def test_csproj_version_matches_app_version():
    csproj = (ROOT / "desktop" / "src" / "Hype.Desktop" / "Hype.Desktop.csproj"
              ).read_text(encoding="utf-8")
    m = re.search(r"<Version>(\d+\.\d+\.\d+)</Version>", csproj)
    assert m, "csproj <Version> not found"
    assert m.group(1) == _app_version(), \
        "bump app.py APP_VERSION and the csproj <Version> together (release runbook)"


# ------------------------------------------------------------------- ship wires

def test_changelog_ships_in_apps_payload():
    # The payload stages tracked content via `git archive HEAD -- $appPaths`; a root file
    # that is not in the pathspec silently never reaches installed desktops.
    ps1 = (ROOT / "desktop" / "scripts" / "build-apps-payload.ps1").read_text(encoding="utf-8")
    m = re.search(r"^\$appPaths = @\((.*)\)$", ps1, re.MULTILINE)
    assert m and "'CHANGELOG.md'" in m.group(1)


def test_changelog_edit_triggers_payload_workflow():
    wf = (ROOT / ".github" / "workflows" / "desktop-payload.yml").read_text(encoding="utf-8")
    assert "- 'CHANGELOG.md'" in wf, \
        "a changelog-only edit must still ship a payload (paths filter)"


def test_shell_release_notes_step_reads_changelog():
    wf = (ROOT / ".github" / "workflows" / "desktop-shell.yml").read_text(encoding="utf-8")
    assert "CHANGELOG.md" in wf
    assert "gh release edit" in wf


# ------------------------------------------------------------------- app wiring

def test_whatsnew_event_and_entry_points():
    # nonce event input: ignore_init would eat the first click (the 2026-07-25 lesson)
    m = re.search(r"@reactive\.event\(input\.whatsnew_evt[^)]*\)", APP_SRC)
    assert m and "ignore_init" not in m.group(0)
    # two inline version-number doors (header chip + welcome splash) ...
    assert APP_SRC.count("Shiny.setInputValue('whatsnew_evt'") == 2
    # ... plus the About-footer button, click-guarded for the rebuilt-modal counter reset
    assert '_clicked_dynamic("about_whatsnew")' in APP_SRC


def test_whatsnew_close_funnels_back_to_the_gate():
    # Opening What's new from the welcome splash replaces the startup gate (one modal at
    # a time), so Close must funnel back through _ensure_welcome, and easy_close must stay
    # off while gated so Esc/backdrop cannot strand a project-less session dialog-less.
    m = re.search(r'if _clicked_dynamic\("whatsnew_close"\):\s*\n'
                  r"\s*ui\.modal_remove\(\)\s*\n"
                  r"\s*_ensure_welcome\(\)", APP_SRC)
    assert m, "whatsnew Close must modal_remove() then _ensure_welcome()"
    assert "easy_close=not _gated()" in APP_SRC


def test_changelog_read_next_to_app_file():
    # _changelog_md reads the file that actually ships beside app.py (payload + dev alike)
    m = re.search(r"def _changelog_md.*?return re\.sub", APP_SRC, re.DOTALL)
    assert m
    assert 'Path(__file__).resolve().parent / "CHANGELOG.md"' in m.group(0)
