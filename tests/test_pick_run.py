"""pick_run child protocol: the HYPE_PICK_TEST_RESULT short-circuit (which precedes the
tkinter import, so these run headless) and the real spawn round trip the app uses."""
import json
import multiprocessing as mp
import queue as _queue
import sys
import types

from hype_app import pick_run


class _ListQ:
    """In-process q.put stand-in for direct child_run calls."""

    def __init__(self):
        self.items = []

    def put(self, item):
        self.items.append(item)


def test_short_circuit_result(monkeypatch, tmp_path):
    target = tmp_path / "X.hype"
    monkeypatch.setenv("HYPE_PICK_TEST_RESULT", str(target))
    q = _ListQ()
    pick_run.child_run({"mode": "save", "purpose": "save_as"}, q)
    # single result, no log line: proves tkinter was never reached
    assert q.items == [("result", {"purpose": "save_as", "path": str(target),
                                   "cancelled": False})]


def test_short_circuit_cancel(monkeypatch):
    monkeypatch.setenv("HYPE_PICK_TEST_RESULT", "")
    q = _ListQ()
    pick_run.child_run({"mode": "open", "purpose": "open_project"}, q)
    assert q.items == [("result", {"purpose": "open_project", "path": None,
                                   "cancelled": True})]


def test_short_circuit_multiple_results(monkeypatch, tmp_path):
    targets = [str(tmp_path / "Alpha.hype"), str(tmp_path / "Beta.hype")]
    monkeypatch.setenv("HYPE_PICK_TEST_RESULT", json.dumps(targets))
    q = _ListQ()
    pick_run.child_run({"mode": "open_multiple", "purpose": "comparison_add"}, q)
    assert q.items == [("result", {"purpose": "comparison_add", "paths": targets,
                                   "cancelled": False})]


def test_short_circuit_multiple_cancel(monkeypatch):
    monkeypatch.setenv("HYPE_PICK_TEST_RESULT", "")
    q = _ListQ()
    pick_run.child_run({"mode": "open", "multiple": True,
                        "purpose": "comparison_add"}, q)
    assert q.items == [("result", {"purpose": "comparison_add", "paths": [],
                                   "cancelled": True})]


def test_tk_multiple_uses_askopenfilenames(monkeypatch, tmp_path):
    monkeypatch.delenv("HYPE_PICK_TEST_RESULT", raising=False)
    targets = (str(tmp_path / "Alpha.hype"), str(tmp_path / "Beta.hype"))
    calls = []

    class _Root:
        def withdraw(self):
            pass

        def attributes(self, *_args):
            pass

        def update(self):
            pass

        def destroy(self):
            pass

    filedialog = types.SimpleNamespace(
        askopenfilenames=lambda **opts: calls.append(opts) or targets,
        askopenfilename=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("single picker called")),
        asksaveasfilename=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("save picker called")),
    )
    tkinter = types.ModuleType("tkinter")
    tkinter.Tk = _Root
    tkinter.filedialog = filedialog
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    q = _ListQ()
    pick_run.child_run({"mode": "open_multiple", "purpose": "comparison_add",
                        "title": "Select projects", "initial_dir": str(tmp_path)}, q)

    assert q.items[-1] == ("result", {"purpose": "comparison_add",
                                      "paths": list(targets), "cancelled": False})
    assert calls[0]["filetypes"][0] == ("HYPE project", "*.hype")
    assert calls[0]["initialdir"] == str(tmp_path)


def test_tk_comparison_save_uses_hypecompare_filter(monkeypatch):
    monkeypatch.delenv("HYPE_PICK_TEST_RESULT", raising=False)
    calls = []

    class _Root:
        def withdraw(self):
            pass

        def attributes(self, *_args):
            pass

        def update(self):
            pass

        def destroy(self):
            pass

    filedialog = types.SimpleNamespace(
        asksaveasfilename=lambda **opts: calls.append(opts) or "C:/out/Compare.hypecompare",
        askopenfilename=lambda **_opts: "",
        askopenfilenames=lambda **_opts: (),
    )
    tkinter = types.ModuleType("tkinter")
    tkinter.Tk = _Root
    tkinter.filedialog = filedialog
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    q = _ListQ()
    pick_run.child_run({"mode": "save", "kind": "comparison",
                        "purpose": "comparison_save",
                        "initial_file": "Compare.hypecompare"}, q)

    assert q.items[-1] == ("result", {"purpose": "comparison_save",
                                      "path": "C:/out/Compare.hypecompare",
                                      "cancelled": False})
    assert calls[0]["defaultextension"] == ".hypecompare"
    assert calls[0]["filetypes"][0] == ("HYPE comparison", "*.hypecompare")


def test_tk_comparison_export_uses_directory_picker(monkeypatch, tmp_path):
    monkeypatch.delenv("HYPE_PICK_TEST_RESULT", raising=False)
    target = str(tmp_path / "comparison-export")
    calls = []

    class _Root:
        def withdraw(self):
            pass

        def attributes(self, *_args):
            pass

        def update(self):
            pass

        def destroy(self):
            pass

    filedialog = types.SimpleNamespace(
        askdirectory=lambda **opts: calls.append(opts) or target,
        asksaveasfilename=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("save picker called")),
        askopenfilename=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("file picker called")),
        askopenfilenames=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("multi picker called")),
    )
    tkinter = types.ModuleType("tkinter")
    tkinter.Tk = _Root
    tkinter.filedialog = filedialog
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    q = _ListQ()
    pick_run.child_run({"mode": "directory", "kind": "comparison",
                        "purpose": "comparison_export",
                        "title": "Export comparison", "initial_dir": str(tmp_path)}, q)

    assert q.items[-1] == ("result", {"purpose": "comparison_export",
                                      "path": target, "cancelled": False})
    assert calls == [{"parent": calls[0]["parent"], "title": "Export comparison",
                      "initialdir": str(tmp_path), "mustexist": False}]


def test_spawn_round_trip(monkeypatch, tmp_path):
    """The exact protocol pick_task uses: spawn context + mp.Queue. The env must be set
    before start() — Windows spawn snapshots the parent environment at CreateProcess."""
    target = tmp_path / "Y.hype"
    monkeypatch.setenv("HYPE_PICK_TEST_RESULT", str(target))
    ctx = mp.get_context("spawn")
    q = ctx.Queue()
    p = ctx.Process(target=pick_run.child_run,
                    args=({"mode": "open", "purpose": "open_project"}, q))
    p.start()
    p.join(30)
    assert p.exitcode == 0
    items = []
    while True:
        try:
            items.append(q.get_nowait())
        except _queue.Empty:
            break
    assert ("result", {"purpose": "open_project", "path": str(target),
                       "cancelled": False}) in items


def test_tk_maplayer_kind_selects_the_maplayer_filter(monkeypatch, tmp_path):
    """kind="maplayer" (the Map layers pane's Add/relink picks) must swap in the
    raster+vector filetypes — the project filter would hide every .tif/.shp."""
    monkeypatch.delenv("HYPE_PICK_TEST_RESULT", raising=False)
    targets = (str(tmp_path / "ortho.tif"), str(tmp_path / "parcels.shp"))
    calls = []

    class _Root:
        def withdraw(self):
            pass

        def attributes(self, *_args):
            pass

        def update(self):
            pass

        def destroy(self):
            pass

    filedialog = types.SimpleNamespace(
        askopenfilenames=lambda **opts: calls.append(opts) or targets,
        askopenfilename=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("single picker called")),
        asksaveasfilename=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("save picker called")),
    )
    tkinter = types.ModuleType("tkinter")
    tkinter.Tk = _Root
    tkinter.filedialog = filedialog
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    q = _ListQ()
    pick_run.child_run({"mode": "open_multiple", "kind": "maplayer",
                        "purpose": "maplayer_add", "title": "Add map layers",
                        "initial_dir": str(tmp_path)}, q)

    assert q.items[-1] == ("result", {"purpose": "maplayer_add",
                                      "paths": list(targets), "cancelled": False})
    assert calls[0]["filetypes"][0] == ("Map layers",
                                        "*.tif *.tiff *.vrt *.shp *.geojson *.json")
    assert ("All files", "*.*") in calls[0]["filetypes"]


def test_tk_demraster_kind_selects_the_geotiff_filter(monkeypatch, tmp_path):
    """kind="demraster" (the DEM pane's local-source pick) must swap in the GeoTIFF-only
    filetypes and use the SINGLE-file picker — a DEM pick is one raster."""
    monkeypatch.delenv("HYPE_PICK_TEST_RESULT", raising=False)
    target = str(tmp_path / "site_dem.tif")
    calls = []

    class _Root:
        def withdraw(self):
            pass

        def attributes(self, *_args):
            pass

        def update(self):
            pass

        def destroy(self):
            pass

    filedialog = types.SimpleNamespace(
        askopenfilename=lambda **opts: calls.append(opts) or target,
        askopenfilenames=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("multi picker called")),
        asksaveasfilename=lambda **_opts: (_ for _ in ()).throw(
            AssertionError("save picker called")),
    )
    tkinter = types.ModuleType("tkinter")
    tkinter.Tk = _Root
    tkinter.filedialog = filedialog
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)

    q = _ListQ()
    pick_run.child_run({"mode": "open", "kind": "demraster",
                        "purpose": "dem_src_pick", "title": "Choose DEM raster",
                        "initial_dir": str(tmp_path)}, q)

    assert q.items[-1] == ("result", {"purpose": "dem_src_pick",
                                      "path": target, "cancelled": False})
    assert calls[0]["filetypes"] == [("GeoTIFF", "*.tif *.tiff"), ("All files", "*.*")]
