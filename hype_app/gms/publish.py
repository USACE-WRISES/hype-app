"""On-disk lifecycle for the GMS export: build into a staging dir inside the project
folder, then swap it in as `GMS/`.

`export.py` stays a pure translator (raises on unusable runs, writes wherever it is
pointed); this module owns the "live folder" concerns: unique same-volume staging so a
detached worker thread can never fight a newer build, a last-moment `precheck` hook so
an invalidated build discards itself instead of resurrecting swept results, Windows
lock tolerance (the user may have the .gpr open in GMS), and the EXPORT_ERROR.txt
breadcrumb when there is no usable tree to keep. Never raises.
"""
from __future__ import annotations

import json
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable

from .export import export_gms_project

GMS_DIRNAME = "GMS"
STAGING_PREFIX = "GMS.tmp"

_ERROR_NOTE = ("The Aquaveo GMS project could not be generated.\n"
               "Reason: {err}\n"
               "Re-run the groundwater stage to retry. Your model results are "
               "unaffected.\n")


def _resolve_porosity(work_dir: Path, fallback: float, include_hz: bool) -> float:
    """The HZ run's own porosity knob wins when particle sets ride along (matches the
    delineation the pathlines came from); otherwise the caller's pane value."""
    if include_hz:
        try:
            stats = json.loads((work_dir / "summary" / "hz" / "hz_stats.json")
                               .read_text(encoding="utf-8"))
            v = float(stats["knobs"]["porosity"])
            if 0 < v < 1:
                return v
        except Exception:  # noqa: BLE001 — absent/legacy stats file: fall through
            pass
    return float(fallback)


def refresh_gms_tree(work_dir, *, name: str, crs_wkt_esri: str, porosity: float,
                     include_hz: bool, log: Callable = print,
                     precheck: Callable[[], bool] | None = None,
                     exporter: Callable | None = None) -> dict:
    """Rebuild `work_dir/GMS` via a staging swap. Never raises.

    Returns {"ok", "skipped", "error", "warnings", "n_particles", "kept_old"}:
    ok=True on a completed swap; skipped=True when `precheck` vetoed the swap (the
    build was invalidated while running — nothing on disk was touched); kept_old=True
    when a failure left the previous GMS/ tree in place.
    """
    work_dir = Path(work_dir)
    final = work_dir / GMS_DIRNAME
    out = {"ok": False, "skipped": False, "error": None,
           "warnings": [], "n_particles": {}, "kept_old": False}

    # Sweep staging left by crashed/cancelled builds (unique names, so never ours).
    for stale in work_dir.glob(STAGING_PREFIX + "*"):
        shutil.rmtree(stale, ignore_errors=True)

    staging = work_dir / f"{STAGING_PREFIX}-{uuid.uuid4().hex[:8]}"
    try:
        staging.mkdir(parents=True)
    except OSError as e:
        out["error"] = f"could not create a staging folder: {e}"
        out["kept_old"] = final.is_dir()
        return out

    try:
        res = (exporter or export_gms_project)(
            work_dir, staging, name=name, crs_wkt_esri=crs_wkt_esri,
            porosity=_resolve_porosity(work_dir, porosity, include_hz),
            hz_dir=(work_dir / "summary" / "hz") if include_hz else None,
            log=log)
        out["warnings"] = list(res.get("warnings") or [])
        out["n_particles"] = dict(res.get("n_particles") or {})
    except Exception as e:  # noqa: BLE001 — translator failure must not crash the app
        shutil.rmtree(staging, ignore_errors=True)
        out["error"] = str(e) or repr(e)
        if final.is_dir():
            out["kept_old"] = True          # a stale-but-openable tree beats a note
        else:
            try:
                final.mkdir(parents=True, exist_ok=True)
                (final / "EXPORT_ERROR.txt").write_text(
                    _ERROR_NOTE.format(err=out["error"]), encoding="utf-8")
            except OSError:
                pass
        return out

    # Last-moment veto: the results this build reflects were invalidated meanwhile
    # (cascade sweep, project switch). Discard quietly; the sweeper owns the folder.
    if precheck is not None and not precheck():
        shutil.rmtree(staging, ignore_errors=True)
        out["skipped"] = True
        return out

    try:
        if final.exists():
            shutil.rmtree(final)            # not ignore_errors: a lock must abort the swap
    except OSError as e:
        shutil.rmtree(staging, ignore_errors=True)
        out["error"] = f"the GMS folder is in use: {e}"
        out["kept_old"] = True
        return out

    try:
        os.rename(staging, final)
    except OSError:
        # Windows delete-pending: the just-removed name can stay visible briefly.
        try:
            shutil.copytree(staging, final, dirs_exist_ok=True)
        except OSError as e:
            out["error"] = f"could not place the GMS folder: {e}"
            try:
                final.mkdir(parents=True, exist_ok=True)
                (final / "EXPORT_ERROR.txt").write_text(
                    _ERROR_NOTE.format(err=out["error"]), encoding="utf-8")
            except OSError:
                pass
            return out
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    out["ok"] = True
    return out
