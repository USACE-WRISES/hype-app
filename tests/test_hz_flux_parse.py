"""Streaming flux-pathline parser (`_flux_path_stats`) vs the flopy reference it
replaced: identical per-particle min-z / 3-D path length / seen-mask, including
single-vertex blocks, absent particles, blocks split across chunk boundaries, and
out-of-range sequence numbers. Fixture coordinates are float32-exact so the flopy
reference (float32 recarrays) and the float64 streaming parse agree tightly."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from hypetool.functions.hz_analysis import (
    _flux_path_stats,
    _path_length,
    _pathlines_by_pid,
    path_max_depth,
)


def _write_mppth(path: Path, blocks):
    """Exact MP7 v7 pathline text layout (flopy's parse() must accept it too)."""
    lines = [
        "MODPATH_PATHLINE_FILE         7         2",
        "    1   0.0000000000E+00  0.0000000000E+00  0.0000000000E+00  0.0000000000E+00",
        "END HEADER",
    ]
    for seq, group, pid, pts in blocks:
        lines.append(f"{seq:10d}{group:10d}{pid:10d}{len(pts):10d}")
        for node, x, y, z, t in pts:
            lines.append(f"    {node:6d}  {x:.8E}  {y:.8E}  {z:.8E}  {t:.8E}"
                         f"  0.50000000E+00  0.50000000E+00  0.50000000E+00"
                         f"         1         1         1")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


BLOCKS = [
    (1, 1, 1, [(11, 100.0, 200.0, 50.0, 0.0),
               (12, 103.0, 204.0, 38.0, 1.5),      # leg: sqrt(9+16+144) = 13
               (13, 103.0, 204.0, 44.0, 3.0)]),    # leg: 6 (doubles back up)
    (2, 1, 2, [(21, 400.0, 100.0, 61.25, 0.0)]),   # single vertex -> length 0.0
    (3, 1, 3, [(31, 10.0, 10.0, 30.0, 0.0),
               (32, 13.0, 14.0, 30.0, 1.0),
               (33, 13.0, 14.0, 18.0, 2.0),
               (34, 16.0, 18.0, 18.0, 3.0),
               (35, 16.0, 18.0, 42.0, 4.0)]),      # 5 + 12 + 5 + 24 = 46
    (99, 1, 99, [(41, 0.0, 0.0, -5.0, 0.0),        # out of range: ignored
                 (42, 3.0, 4.0, -5.0, 1.0)]),
]
N = 5   # seeds 0..4; seq 1/2/3 -> pids 0/1/2; 3 and 4 absent


def _reference(path: Path, n: int):
    """The pre-change code path: flopy full parse + per-particle Python loop."""
    by_pid = _pathlines_by_pid(path)
    min_z = np.full(n, np.nan)
    length = np.full(n, np.nan)
    seen = np.zeros(n, dtype=bool)
    for m in range(n):
        rec = by_pid.get(m)
        if rec is None or np.asarray(rec).size == 0:
            continue
        seen[m] = True
        min_z[m] = float(np.asarray(rec["z"], float).min())
        length[m] = _path_length(rec)
    return min_z, length, seen


@pytest.mark.parametrize("chunk_rows", [3, 10_000])
def test_streaming_matches_flopy_reference(tmp_path, chunk_rows):
    # chunk_rows=3 forces mid-block chunk boundaries (rows include sub-headers), so
    # the carry of the last point across chunks is exercised for real.
    f = tmp_path / "hz_flux_pl.mppth"
    _write_mppth(f, BLOCKS)
    ref_z, ref_len, ref_seen = _reference(f, N)
    got_z, got_len, got_seen = _flux_path_stats(f, N, log=lambda m: None,
                                                chunk_rows=chunk_rows)
    assert got_seen.tolist() == ref_seen.tolist()
    np.testing.assert_allclose(got_z, ref_z, rtol=1e-6, equal_nan=True)
    np.testing.assert_allclose(np.where(got_seen, got_len, np.nan), ref_len,
                               rtol=1e-6, equal_nan=True)
    assert got_len[0] == pytest.approx(19.0)
    assert got_len[1] == 0.0 and bool(got_seen[1])
    assert got_len[2] == pytest.approx(46.0)
    assert got_z[1] == pytest.approx(61.25)


def test_depth_semantics_match_path_max_depth():
    # The caller vectorizes path_max_depth as max(top - min_z, 0); pin the identity.
    z = np.array([50.0, 38.0, 44.0])
    for top in (49.0, 30.0):
        assert max(top - z.min(), 0.0) == path_max_depth(top, z)


def test_progress_lines_emitted(tmp_path):
    f = tmp_path / "hz_flux_pl.mppth"
    _write_mppth(f, BLOCKS)
    lines = []
    _flux_path_stats(f, N, log=lines.append, chunk_rows=2)
    assert lines and all("Flux pathline parse:" in ln for ln in lines)
    assert any("100%" in ln for ln in lines)
    assert all("—" not in ln for ln in lines)
