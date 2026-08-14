"""RAS liveness watchdog (2026-08-13): a step is killed only when BOTH channels are
silent (no stdout line, no file write under the project dir) past HYPE_RAS_STALL_MIN.
Born from a wedged 151k-cell solve that spun at full CPU for 38 minutes writing
nothing. These tests drive _run_ras with real child processes via a monkeypatched
ras_cmd, so the kill-on-silence and stays-alive-on-output behaviors are proven, not
mocked.
"""
from __future__ import annotations

import os
import sys
import time

import pytest

from hype_app import ras

# ------------------------------------------------------------------ threshold knob


def test_stall_timeout_default_30_min(monkeypatch):
    monkeypatch.delenv("HYPE_RAS_STALL_MIN", raising=False)
    assert ras.stall_timeout_s() == 1800.0


def test_stall_timeout_env_override_and_disable(monkeypatch):
    monkeypatch.setenv("HYPE_RAS_STALL_MIN", "5")
    assert ras.stall_timeout_s() == 300.0
    monkeypatch.setenv("HYPE_RAS_STALL_MIN", "0")
    assert ras.stall_timeout_s() is None
    monkeypatch.setenv("HYPE_RAS_STALL_MIN", "junk")
    assert ras.stall_timeout_s() == 1800.0


# ------------------------------------------------------------------ _latest_mtime


def test_latest_mtime_newest_wins_and_empty_is_none(tmp_path):
    assert ras._latest_mtime(tmp_path) is None
    old = tmp_path / "a.txt"
    new = tmp_path / "sub" / "b.txt"
    old.write_text("x")
    new.parent.mkdir()
    new.write_text("y")
    past = time.time() - 500
    os.utime(old, (past, past))
    got = ras._latest_mtime(tmp_path)
    assert got == pytest.approx(new.stat().st_mtime, abs=0.01)


# ------------------------------------------------------------------ live children


def _fake_ras(monkeypatch, code: str):
    monkeypatch.setattr(
        ras, "ras_cmd",
        lambda: ([sys.executable, "-u", "-c", code], dict(os.environ)))


def test_silent_child_is_stall_killed(tmp_path, monkeypatch):
    _fake_ras(monkeypatch, "import time; time.sleep(30)")
    monkeypatch.setenv("HYPE_RAS_STALL_MIN", "0.02")          # 1.2 s
    lines = []
    t0 = time.monotonic()
    with pytest.raises(ras.RasError, match="stalled: no solver output"):
        ras._run_ras([], cwd=tmp_path, env=None, log=lines.append,
                     cancel_evt=None, proc_holder={}, timeout_s=None,
                     label="Silent step")
    assert time.monotonic() - t0 < 15                          # killed, not slept out
    try:
        ras._run_ras([], cwd=tmp_path, env=None, log=lines.append,
                     cancel_evt=None, proc_holder={}, timeout_s=None,
                     label="Silent step")
    except ras.RasError as e:
        assert "—" not in str(e)


def test_chatty_child_survives_tiny_threshold(tmp_path, monkeypatch):
    _fake_ras(monkeypatch,
              "import time\n"
              "for i in range(8):\n"
              "    print(f'line {i}', flush=True)\n"
              "    time.sleep(0.3)\n")
    monkeypatch.setenv("HYPE_RAS_STALL_MIN", "0.05")           # 3 s >> 0.3 s cadence
    lines = []
    ras._run_ras([], cwd=tmp_path, env=None, log=lines.append,
                 cancel_evt=None, proc_holder={}, timeout_s=None,
                 label="Chatty step")                          # must NOT raise
    assert any("line 7" in ln for ln in lines)


def test_same_percent_spam_counts_as_hung(tmp_path, monkeypatch):
    # A hung compute that keeps reprinting the same percent is NOT alive: only an
    # ADVANCING percent (or a non-progress line, or a file write) resets the clock.
    _fake_ras(monkeypatch,
              "import time\n"
              "while True:\n"
              "    print('Progress: 7%', flush=True)\n"
              "    time.sleep(0.2)\n")
    monkeypatch.setenv("HYPE_RAS_STALL_MIN", "0.02")           # 1.2 s
    with pytest.raises(ras.RasError, match=r"stalled.*last progress 7%"):
        ras._run_ras([], cwd=tmp_path, env=None, log=lambda m: None,
                     cancel_evt=None, proc_holder={}, timeout_s=None,
                     label="Hung compute")


def test_advancing_percent_survives_tiny_threshold(tmp_path, monkeypatch):
    _fake_ras(monkeypatch,
              "import time\n"
              "for i in range(8):\n"
              "    print(f'Progress: {i}%', flush=True)\n"
              "    time.sleep(0.3)\n")
    monkeypatch.setenv("HYPE_RAS_STALL_MIN", "0.05")           # 3 s >> 0.3 s cadence
    ras._run_ras([], cwd=tmp_path, env=None, log=lambda m: None,
                 cancel_evt=None, proc_holder={}, timeout_s=None,
                 label="Slow but moving")                      # must NOT raise
