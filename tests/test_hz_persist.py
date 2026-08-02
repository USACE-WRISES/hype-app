"""_persist_display_pathlines: the sampled .mppth files must survive the HZ run
(the GMS export translates them; hz_ws itself is deleted at the end of the run).

The sources carry the RAW MP7 simulation names (hz_pl_for / hz_pl_bac, from
name=f"hz_pl_{direction[:3]}") — persistence must rename them to the canonical
hz_pl_fwd / hz_pl_bwd the GMS export reads. The original fixture fed the canonical
names as sources, which let a real-world naming mismatch ship: every actual run
persisted for/bac and the export found nothing (fixed 2026-07-26).
"""
from __future__ import annotations

from hypetool.functions.hz_analysis import _persist_display_pathlines

RAW = {"forward": "hz_pl_for.mppth", "backward": "hz_pl_bac.mppth"}
CANON = {"forward": "hz_pl_fwd.mppth", "backward": "hz_pl_bwd.mppth"}


def _seed(tmp_path):
    ws = tmp_path / "hz_ws"
    hz_dir = tmp_path / "hz"
    ws.mkdir()
    hz_dir.mkdir()
    paths = {}
    for direction, name in RAW.items():          # producer-real names, not canonical
        p = ws / name
        p.write_bytes(b"MP7-" + direction.encode())
        paths[direction] = p
    return ws, hz_dir, paths


def test_moves_and_canonicalizes_by_default(tmp_path):
    ws, hz_dir, paths = _seed(tmp_path)
    art = _persist_display_pathlines(paths, hz_dir, keep_raw=False)
    assert art == {"mp7_pathlines_forward": "hz_pl_fwd.mppth",
                   "mp7_pathlines_backward": "hz_pl_bwd.mppth"}
    for direction, src in paths.items():
        assert not src.exists()
        assert (hz_dir / CANON[direction]).read_bytes() == b"MP7-" + direction.encode()
        assert not (hz_dir / RAW[direction]).exists()   # never the raw name in results


def test_copies_when_keeping_raw(tmp_path):
    ws, hz_dir, paths = _seed(tmp_path)
    art = _persist_display_pathlines(paths, hz_dir, keep_raw=True)
    assert len(art) == 2
    for direction, src in paths.items():
        assert src.exists()                      # raw workspace keeps its file
        assert (hz_dir / CANON[direction]).exists()


def test_missing_sources_tolerated(tmp_path):
    ws, hz_dir, paths = _seed(tmp_path)
    paths["backward"].unlink()
    art = _persist_display_pathlines(paths, hz_dir, keep_raw=False)
    assert art == {"mp7_pathlines_forward": "hz_pl_fwd.mppth"}
    assert not (hz_dir / "hz_pl_bwd.mppth").exists()
