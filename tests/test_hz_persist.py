"""_persist_display_pathlines: the sampled .mppth files must survive the HZ run
(the GMS export translates them; hz_ws itself is deleted at the end of the run)."""
from __future__ import annotations

from hypetool.functions.hz_analysis import _persist_display_pathlines


def _seed(tmp_path):
    ws = tmp_path / "hz_ws"
    hz_dir = tmp_path / "hz"
    ws.mkdir()
    hz_dir.mkdir()
    paths = {}
    for direction, name in (("forward", "hz_pl_fwd.mppth"),
                            ("backward", "hz_pl_bwd.mppth")):
        p = ws / name
        p.write_bytes(b"MP7-" + direction.encode())
        paths[direction] = p
    return ws, hz_dir, paths


def test_moves_by_default(tmp_path):
    ws, hz_dir, paths = _seed(tmp_path)
    art = _persist_display_pathlines(paths, hz_dir, keep_raw=False)
    assert art == {"mp7_pathlines_forward": "hz_pl_fwd.mppth",
                   "mp7_pathlines_backward": "hz_pl_bwd.mppth"}
    for direction, src in paths.items():
        assert not src.exists()
        assert (hz_dir / src.name).read_bytes() == b"MP7-" + direction.encode()


def test_copies_when_keeping_raw(tmp_path):
    ws, hz_dir, paths = _seed(tmp_path)
    art = _persist_display_pathlines(paths, hz_dir, keep_raw=True)
    assert len(art) == 2
    for src in paths.values():
        assert src.exists() and (hz_dir / src.name).exists()


def test_missing_sources_tolerated(tmp_path):
    ws, hz_dir, paths = _seed(tmp_path)
    paths["backward"].unlink()
    art = _persist_display_pathlines(paths, hz_dir, keep_raw=False)
    assert art == {"mp7_pathlines_forward": "hz_pl_fwd.mppth"}
    assert not (hz_dir / "hz_pl_bwd.mppth").exists()
