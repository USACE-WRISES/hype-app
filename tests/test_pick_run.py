"""pick_run child protocol: the HYPE_PICK_TEST_RESULT short-circuit (which precedes the
tkinter import, so these run headless) and the real spawn round trip the app uses."""
import multiprocessing as mp
import queue as _queue

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
