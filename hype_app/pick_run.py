"""Child-process native file dialog (tkinter) for desktop mode without the shell.

The WebView2 shell shows real WinForms dialogs itself; this child covers the no-shell
desktop case (browser dev preview, or the bridge not yet attached). Always a spawned
child, never in-process tkinter: Tk wants the process's main thread, and a wedged
dialog stays hard-killable (same posture as gms_run). Mirrors hype_app/run.py's queue
protocol: ('log', line) messages, then ('result', dict) or ('error', traceback).
"""
from __future__ import annotations

import os
import traceback


def child_run(payload: dict, q) -> None:
    """Show a native save/open dialog; put the picked path (or cancel) on `q`.

    Payload: {"mode": "save"|"open", "title": str, "initial_file": str,
              "initial_dir": str, "purpose": str}. Result mirrors the WebView2 bridge
    reply: {"purpose", "path" (str|None), "cancelled" (bool)}.

    HYPE_PICK_TEST_RESULT (may be "" = simulated cancel) short-circuits BEFORE tkinter
    is imported, so the full spawn round trip is testable headless.
    """
    try:
        purpose = str(payload.get("purpose") or "")
        test = os.environ.get("HYPE_PICK_TEST_RESULT")
        if test is not None:
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
            if payload.get("mode") == "save":
                init_file = str(payload.get("initial_file") or "")
                if init_file:
                    opts["initialfile"] = init_file
                # confirmoverwrite stays at its default True: picking an existing
                # .hype means replacing it, and the dialog warns natively
                picked = filedialog.asksaveasfilename(
                    defaultextension=".hype",
                    filetypes=[("HYPE project", "*.hype")], **opts)
            else:
                picked = filedialog.askopenfilename(
                    filetypes=[("HYPE project", "*.hype"), ("All files", "*.*")],
                    **opts)
        finally:
            try:
                root.destroy()
            except Exception:  # noqa: BLE001
                pass
        picked = str(picked or "")     # cancel returns "" (or () on some Tk builds)
        q.put(("result", {"purpose": purpose, "path": picked or None,
                          "cancelled": not picked}))
    except Exception:  # noqa: BLE001 — the app-side task falls back to the typed modal
        q.put(("error", traceback.format_exc()))
