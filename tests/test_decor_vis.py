"""Decor-layer visibility pins — the invisible-boundaries-after-open regression.

Repro the pins guard: open a project SAVED with the Boundaries group unchecked, then check
the group. The label Markers returned but the linework stayed invisible: `_decor_show` used
to construct hidden keys' GeoJSONs with an EMPTY FeatureCollection, `_set_layer` parked the
empty widget, and `_set_keys_visible`'s un-park path cloned it verbatim — a live layer with
no child paths (the real feature sat unused in `_decor_feat`). Two invariants hold the fix:
parked decor widgets are born with their REAL geometry (like the label Markers always
were), and the un-park path re-pushes the cached geometry through the decor channel."""
from __future__ import annotations

import re
from pathlib import Path


def _app_src():
    return (Path(__file__).resolve().parents[1] / "app.py").read_text(encoding="utf-8")


def _fn_body(src: str, name: str) -> str:
    """The source slice from `def name` to the next def at the same indent."""
    m = re.search(rf"\n(    (?:async )?def {name}\b.*?)(?=\n    (?:async )?def |\n    @|\nclass )",
                  src, flags=re.S)
    assert m, f"{name} not found in app.py"
    return m.group(1)


def _code_only(body: str) -> str:
    """Body with the docstring and # comments stripped, so prose can't satisfy (or trip)
    a code-shaped assertion."""
    body = re.sub(r'""".*?"""', "", body, count=1, flags=re.S)
    return "\n".join(ln.split("#", 1)[0] for ln in body.splitlines())


def test_decor_widgets_are_born_with_their_real_geometry():
    body = _code_only(_fn_body(_app_src(), "_decor_show"))
    # construction must NOT reuse the hidden-aware `want` (that is the live-mutate hide);
    # a hidden key's widget gets parked, and a parked widget must carry its geometry
    assert "GeoJSON(data=_fc(feat)" in body
    assert "GeoJSON(data=want" not in body


def test_unpark_repushes_decor_geometry_after_the_clone():
    body = _code_only(_fn_body(_app_src(), "_set_keys_visible"))
    clone_at = body.index("_clone_vector(obj)")
    repush = body.find("_decor_show(k, _decor_feat.get(k), None)", clone_at)
    assert repush > clone_at, "un-park must re-push the cached decor geometry"
