"""Child-process native file dialog (tkinter) for desktop mode without the shell.

The WebView2 shell shows real WinForms dialogs itself; this child covers the no-shell
desktop case (browser dev preview, or the bridge not yet attached). Always a spawned
child, never in-process tkinter: Tk wants the process's main thread, and a wedged
dialog stays hard-killable (same posture as gms_run). Mirrors hype_app/run.py's queue
protocol: ('log', line) messages, then ('result', dict) or ('error', traceback).
"""
from __future__ import annotations

import json
import os
import traceback


_PROJECT_FILETYPES = [("HYPE project", "*.hype"), ("All files", "*.*")]
_COMPARISON_FILETYPES = [("HYPE comparison", "*.hypecompare"),
                         ("All files", "*.*")]
_MAPLAYER_FILETYPES = [("Map layers", "*.tif *.tiff *.vrt *.shp *.geojson *.json"),
                       ("Rasters", "*.tif *.tiff *.vrt"),
                       ("Vectors", "*.shp *.geojson *.json"),
                       ("All files", "*.*")]
_DEM_FILETYPES = [("GeoTIFF", "*.tif *.tiff"), ("All files", "*.*")]


def _is_multiple(payload: dict) -> bool:
    mode = str(payload.get("mode") or "").lower()
    return bool(payload.get("multiple")) or mode in {
        "open_multiple", "open-multiple", "multiple",
    }


def _is_comparison(payload: dict) -> bool:
    mode = str(payload.get("mode") or "").lower()
    return str(payload.get("kind") or "").lower() == "comparison" or mode in {
        "comparison_open", "comparison_save", "open_comparison", "save_comparison",
    }


def _is_maplayer(payload: dict) -> bool:
    return str(payload.get("kind") or "").lower() == "maplayer"


def _is_dem(payload: dict) -> bool:
    return str(payload.get("kind") or "").lower() == "demraster"


def _is_save(payload: dict) -> bool:
    return str(payload.get("mode") or "").lower() in {
        "save", "comparison_save", "save_comparison",
    }


def _test_paths(value: str) -> list[str]:
    """Decode the headless-test value for a multi-select dialog.

    A JSON string array is unambiguous even when a Windows path contains spaces or a
    drive colon. A plain non-empty value remains useful as a one-file selection.
    """
    raw = value.strip()
    if not raw:
        return []
    if raw.startswith("["):
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, list):
            return [str(path) for path in decoded if str(path)]
    return [raw]


def child_run(payload: dict, q) -> None:
    """Show a native save/open dialog; put the picked path (or cancel) on `q`.

    Payload: {"mode": "save"|"open"|"open_multiple"|"directory", "title": str,
              "initial_file": str, "initial_dir": str, "purpose": str,
              "kind": "project"|"comparison"|"maplayer"}. ``multiple: true`` is accepted as an
    alias for ``mode: "open_multiple"``. A single selection mirrors the WebView2
    bridge reply: {"purpose", "path" (str|None), "cancelled" (bool)}; a multi-select
    reply is {"purpose", "paths" (list[str]), "cancelled" (bool)}.

    HYPE_PICK_TEST_RESULT (may be "" = simulated cancel) short-circuits BEFORE tkinter
    is imported, so the full spawn round trip is testable headless.
    """
    try:
        purpose = str(payload.get("purpose") or "")
        test = os.environ.get("HYPE_PICK_TEST_RESULT")
        if test is not None:
            if _is_multiple(payload):
                paths = _test_paths(test)
                q.put(("result", {"purpose": purpose, "paths": paths,
                                  "cancelled": not paths}))
            else:
                path = test.strip()
                q.put(("result", {"purpose": purpose, "path": path or None,
                                  "cancelled": not path}))
            return
        q.put(("log", "opening native file dialog (" + str(payload.get("mode")) + ")"))
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        # This process has no foreground rights (hidden-console server child), so the
        # dialog can't steal focus; topmost keeps it visible above the browser at least.
        root.attributes("-topmost", True)
        root.update()
        try:
            opts = {"parent": root, "title": str(payload.get("title") or "HYPE")}
            init_dir = str(payload.get("initial_dir") or "")
            if init_dir and os.path.isdir(init_dir):
                opts["initialdir"] = init_dir
            comparison = _is_comparison(payload)
            filetypes = (_DEM_FILETYPES if _is_dem(payload)
                         else _MAPLAYER_FILETYPES if _is_maplayer(payload)
                         else _COMPARISON_FILETYPES if comparison
                         else _PROJECT_FILETYPES)
            if str(payload.get("mode") or "").lower() == "directory":
                picked = filedialog.askdirectory(mustexist=False, **opts)
            elif _is_save(payload):
                init_file = str(payload.get("initial_file") or "")
                if init_file:
                    opts["initialfile"] = init_file
                # confirmoverwrite stays at its default True: picking an existing
                # project/comparison means replacing it, and the dialog warns natively
                picked = filedialog.asksaveasfilename(
                    defaultextension=".hypecompare" if comparison else ".hype",
                    filetypes=filetypes, **opts)
            elif _is_multiple(payload):
                picked = filedialog.askopenfilenames(filetypes=filetypes, **opts)
            else:
                picked = filedialog.askopenfilename(filetypes=filetypes, **opts)
        finally:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
        if _is_multiple(payload):
            # askopenfilenames returns a tuple (and sometimes an empty string on cancel).
            paths = ([str(path) for path in picked if str(path)]
                     if isinstance(picked, (tuple, list)) else _test_paths(str(picked or "")))
            q.put(("result", {"purpose": purpose, "paths": paths,
                              "cancelled": not paths}))
        else:
            picked = str(picked or "")  # cancel returns "" (or () on some Tk builds)
            q.put(("result", {"purpose": purpose, "path": picked or None,
                              "cancelled": not picked}))
    except Exception:  # noqa: BLE001 — the app-side task falls back to the typed modal
        q.put(("error", traceback.format_exc()))
