"""Source pins for the 3D display-state restore path.

The saved Wireframe grid checkbox and Grid line opacity slider restore their
VALUES via _kept, but the applied scene state must be re-asserted by
_rebuild_3d_scene: the pre-open clear resets the client, and the toggle
effects' ignore_init swallows the pane-mount value. Losing any of these pins
re-creates the "checkbox says on, scene renders off, toggle off and on to fix"
report (2026-08-10).
"""
from __future__ import annotations

import re
from pathlib import Path


def _app_src() -> str:
    return (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def _fn_body(src: str, name: str) -> str:
    m = re.search(rf"\n(    (?:async )?def {name}\b.*?)(?=\n    (?:async )?def |\n    @|\nclass )",
                  src, flags=re.S)
    assert m, f"{name} not found in app.py"
    return m.group(1)


def _code_only(body: str) -> str:
    body = re.sub(r'""".*?"""', "", body, count=1, flags=re.S)
    return "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())


def test_rebuild_reasserts_wireframe_from_kept():
    body = _code_only(_fn_body(_app_src(), "_rebuild_3d_scene"))
    assert '_kept.get("grid_wireframe"' in body
    assert '"hype3d_wire"' in body
    assert "_wire_state.set(True)" in body      # keeps the toggle's change guard truthful


def test_rebuild_seeds_grid_opacity_from_kept():
    body = _code_only(_fn_body(_app_src(), "_rebuild_3d_scene"))
    assert '_kept.get("grid_opacity3d"' in body
    assert "grid_opacity3d_v.set" in body


def test_wireframe_toggle_keeps_remount_protection():
    """The fix must not soften the toggle effect: ignore_init plus the
    _wire_state change guard are what stop pane remounts from re-sending."""
    src = _app_src()
    at = src.index("async def _grid_wireframe_toggle")
    decorated = src[max(0, at - 200):at]
    assert "ignore_init=True" in decorated
    body = _code_only(_fn_body(src, "_grid_wireframe_toggle"))
    assert "_wire_state()" in body and "_wire_state.set" in body
