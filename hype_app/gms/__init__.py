"""Aquaveo GMS 10.7 project export.

Translates a completed hype groundwater run (MODFLOW 6 + MODPATH 7) into the
MODFLOW-2005-style project layout that GMS 10.7 reads natively: a `.gpr` project
file (patched from the bundled template in hype_app/data/gms_template) plus
`<Name>_MODFLOW\\` and `<Name>_MODPATH_*\\` companion folders. See tree.py for
the project-explorer encoding and tools/make_gms_template.py for the template's
provenance.
"""
from .export import GmsExportError, export_gms_project

__all__ = ["export_gms_project", "GmsExportError"]
