"""Typed-path interpretation for the desktop picker fallback.

Pure path logic (no Shiny, no UI) so every rule is unit-testable. The typed-path modal
is the last-resort picker (dev browser, or the native dialog failing to open); its free
text is where "I typed my Desktop and the app dumped the project onto it" happened, so
every interpretation rule lives here behind tests.
"""
from __future__ import annotations

from pathlib import Path

from hype_app.bundle import OS_CRUFT
from hype_app.dem import DEM_SUFFIXES
from hype_app.map_layers import RASTER_SUFFIXES, VECTOR_SUFFIXES

MSG_EMPTY = "Enter a full path, for example D:\\Projects\\SiteA\\SiteA.hype."
MSG_ABS = "Enter an absolute path (e.g. D:\\Projects\\SiteA\\SiteA.hype)."
MSG_OPEN_DIR = "That is a folder. Enter the path of the .hype file inside it."
MSG_UNREADABLE = "That path can't be read. Check it and try again."

#: Reference map layers (Map layers tree group) — kept in one place with the module that
#: displays them so the picker filter and the display path can never drift.
REFERENCE_SUFFIXES: frozenset[str] = frozenset(RASTER_SUFFIXES | VECTOR_SUFFIXES)
MSG_REF_EMPTY = "Enter a full path, for example D:\\GIS\\parcels.shp."
MSG_REF_DIR = "That is a folder. Enter the path of the layer file inside it."
MSG_REF_KIND = ("That file type isn't supported. Use .tif, .tiff, .vrt, .shp, "
                ".geojson, or .json.")
MSG_REF_MISSING = "That file doesn't exist. Check the path and try again."

#: Local-DEM import (DEM pane, Terrain source = Local raster) — suffixes live with the
#: import module so the picker filter and the importer can never drift.
MSG_DEM_EMPTY = "Enter a full path, for example D:\\GIS\\site_dem.tif."
MSG_DEM_KIND = "Use a GeoTIFF (.tif or .tiff)."

#: Purposes that pick an EXISTING .hype for reading. Everything else is a save-side pick
#: (create semantics: directories resolve to <dir>/<name>.hype, overwrite gates apply).
#: Purposes that READ an existing main file (no create/overwrite semantics). The comparison
#: workspace routes its picks through its own comparison_* purposes, never through here.
OPEN_PURPOSES: frozenset[str] = frozenset({"open_project"})


def _dir_full_msg(example: str) -> str:
    return ("That folder already has files in it. Add a file name for the new "
            f"project, for example {example}.")


def ensure_hype_suffix(p: Path) -> Path:
    """Append .hype when missing. APPEND, never with_suffix: 'Site v1.2' must become
    'Site v1.2.hype', not 'Site v1.hype'. Case-insensitive check keeps 'A.HYPE'
    untouched."""
    return p if p.suffix.lower() == ".hype" else p.with_name(p.name + ".hype")


def _dir_is_empty(p: Path) -> bool:
    """Empty for project purposes: nothing but OS metadata (Explorer drops desktop.ini
    into customized folders, which must not disqualify a folder the user just made)."""
    return all(e.name.lower() in OS_CRUFT for e in p.iterdir())


def interpret_typed_target(raw: str, *, purpose: str,
                           known_stem: str | None = None) -> tuple[Path | None, str | None]:
    """Interpret the typed-picker text. Returns (target_main, None) on success or
    (None, user_error_message). Filesystem access is read-only probes.

    A bare EXISTING directory means "put the project in this folder": empty (or
    OS-cruft-only) dirs become <dir>/<dirname>.hype; occupied dirs use `known_stem`
    (save_as / import know the project name) and leave the non-empty-folder gate to
    offer the subfolder, or error when no stem is known (new_project needs a name).
    """
    s = raw.strip()
    # Symmetric quote pairs only, at most twice (Explorer "Copy as path" nests once);
    # an interior apostrophe (John's Folder) is never a symmetric pair.
    for _ in range(2):
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            s = s[1:-1].strip()
    if not s:
        return None, MSG_EMPTY
    dir_intent = s[-1] in "\\/"        # recorded before Path() eats the trailing slash
    try:
        p = Path(s)
        if not p.is_absolute():        # catches relative, drive-relative D:x, rootless \x
            return None, MSG_ABS
        is_dir = p.is_dir()
    except (ValueError, OSError):
        return None, MSG_UNREADABLE
    is_open = purpose in OPEN_PURPOSES
    try:
        if is_dir:
            if is_open:
                return None, MSG_OPEN_DIR
            name = p.name or "Project"     # drive-root degenerate: C:\ has no name
            if _dir_is_empty(p):
                return p / f"{name}.hype", None
            if known_stem:
                return p / f"{known_stem}.hype", None
            return None, _dir_full_msg(str(p / "MyProject.hype"))
        if dir_intent:                     # trailing slash on a nonexistent path
            if is_open:
                return None, MSG_OPEN_DIR
            return p / f"{p.name or 'Project'}.hype", None
        if is_open:
            return p, None                 # nonexistent-file error stays downstream
        return ensure_hype_suffix(p), None
    except OSError:
        return None, MSG_UNREADABLE


def interpret_dem_file(raw: str) -> tuple[Path | None, str | None]:
    """Typed-path interpretation for a local DEM raster: an EXISTING GeoTIFF — no create
    semantics, no suffix appending. Returns (path, None) on success or
    (None, user_error_message). Filesystem access is read-only probes."""
    s = raw.strip()
    for _ in range(2):                     # same symmetric quote-strip as the project picker
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            s = s[1:-1].strip()
    if not s:
        return None, MSG_DEM_EMPTY
    try:
        p = Path(s)
        if not p.is_absolute():
            return None, MSG_ABS
        if p.is_dir():
            return None, MSG_REF_DIR
        if p.suffix.lower() not in DEM_SUFFIXES:
            return None, MSG_DEM_KIND
        if not p.is_file():
            return None, MSG_REF_MISSING
        return p, None
    except (ValueError, OSError):
        return None, MSG_UNREADABLE


def interpret_reference_file(raw: str) -> tuple[Path | None, str | None]:
    """Typed-path interpretation for a reference map layer: an EXISTING raster/vector
    file — no create semantics, no suffix appending. Returns (path, None) on success or
    (None, user_error_message). Filesystem access is read-only probes."""
    s = raw.strip()
    for _ in range(2):                     # same symmetric quote-strip as the project picker
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
            s = s[1:-1].strip()
    if not s:
        return None, MSG_REF_EMPTY
    try:
        p = Path(s)
        if not p.is_absolute():
            return None, MSG_ABS
        if p.is_dir():
            return None, MSG_REF_DIR
        if p.suffix.lower() not in REFERENCE_SUFFIXES:
            return None, MSG_REF_KIND
        if not p.is_file():
            return None, MSG_REF_MISSING
        return p, None
    except (ValueError, OSError):
        return None, MSG_UNREADABLE
