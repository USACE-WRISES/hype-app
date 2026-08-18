"""The HYPE start page: the startup gate, and the header's `Projects` route into it.

WHAT THESE PROTECT. The start page (v1.0.5: New / Open / Example projects rail, recent
projects, what's new) was written as a HARD startup gate, and everything about it assumed it
could never be on screen while a project was open: no title bar, no footer, `easy_close=False`,
and a New project tile that skips the confirmation the old header New showed. The header's
`Projects` link makes it reachable mid-session, so each of those assumptions needs holding down
from both directions - the gate must STAY a gate, and the mid-session dialog must stay escapable.

Source lints rather than a live session, matching the report-pane lints in `test_report.py`: the
wiring is inside `app.py`'s server closure and cannot be imported piecemeal.
"""
from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def src() -> str:
    return open("app.py", encoding="utf-8").read()


def _body(src: str, start: str, end: str) -> str:
    """The source between two markers, so a lint reads one function and not the whole file."""
    i = src.index(start)
    return src[i:src.index(end, i)]


class TestTheHeaderLink:
    def test_projects_is_the_one_door(self, src):
        """One header door (RAS2025's own "Projects"), no bare New/Open pair; no ellipsis
        because it raises the start page, not a chooser. `Save As…` keeps the chooser
        convention it borrows from."""
        assert 'ui.input_action_link("nav_start", "Projects"' in src
        assert 'ui.input_action_link("nav_save_as", "Save As…"' in src, \
            "the convention this borrows from is gone; re-decide the label"
        assert '"nav_new"' not in src and '"nav_open"' not in src

    def test_projects_raises_the_start_page_in_both_modes(self, src):
        """It must never branch to the native picker on desktop or an upload modal on cloud,
        skipping the one screen that offers recents, New and the examples side by side."""
        body = _body(src, "async def _start_dialog()", "def _show_open_modal()")
        assert "_show_welcome()" in body
        assert "_pick_path(" not in body, "still going straight to the picker"
        assert "_show_open_modal()" not in body, "still going straight to the upload modal"
        assert "runmode.IS_DESKTOP" not in body, "the two modes should not diverge here"

    def test_projects_keeps_its_busy_check(self, src):
        """Every work_dir rebind needs no task holding a captured workspace path, and the
        check belongs before the chooser, not after the pick."""
        body = _body(src, "async def _start_dialog()", "def _show_open_modal()")
        assert "_busy_tasks()" in body
        assert body.index("_busy_tasks()") < body.index("_show_welcome()")

    def test_the_entry_point_is_show_not_ensure(self, src):
        """`_ensure_welcome` is `if _gated(): _show_welcome()`, so it no-ops once a project is
        open. Routing the header through it would make Projects do nothing at all, and the ~28
        cancel funnels depend on that no-op staying exactly as it is."""
        assert "def _ensure_welcome():\n        if _gated():\n            _show_welcome()" in src
        body = _body(src, "async def _start_dialog()", "def _show_open_modal()")
        assert "_ensure_welcome()" not in body


class TestTheGateStaysAGate:
    def test_the_modal_is_dismissable_only_off_the_gate(self, src):
        """BOTH directions matter. Under the gate this must have no exit at all: `title=None`
        drops the header and its x, `footer=None` drops Bootstrap's default Dismiss, and
        `easy_close=False` blocks Esc and the backdrop. With a project open, the same three
        properties make it a trap, because nothing would ever remove it: the close control
        rides in the page itself, gate-conditional."""
        body = _body(src, "def _show_welcome(", "def _welcome_cancel()")
        assert "gated = _gated()" in body
        assert "easy_close=not gated" in body
        assert "title=None" in body and "footer=None" in body
        assert re.search(r"close = \(\[\] if gated\s*\n?\s*else \[ui\.input_action_button\("
                         r"\"welcome_cancel\"", body), \
            "the close button is no longer gate-conditional"

    def test_cancel_uses_the_strict_increment_guard(self, src):
        """The button is rebuilt on every modal show, so its counter resets to 0 each time and
        `@reactive.event` would miss every click after the first. Same trap `_open_cancel`
        avoids."""
        body = _body(src, "def _welcome_cancel()", "def _start_examples_open()")
        assert '_clicked_dynamic("welcome_cancel")' in body
        assert "ui.modal_remove()" in body
        # The DECORATOR, not the word (the comment above the guard names it too), and the
        # decorators sit ABOVE the def, so this has to look backwards from it.
        i = src.index("def _welcome_cancel")
        assert not re.search(r"^\s*@reactive\.event", src[i - 200:i], re.M)

    def test_recents_are_re_read_on_every_show(self, src):
        """`_adopt_workspace` touches the store on every project open and the handlers index
        `_welcome["recents"]` POSITIONALLY, so a cached row set opens the wrong project."""
        body = _body(src, "def _show_welcome(", "def _welcome_cancel()")
        assert "items = recents.load() if runmode.IS_DESKTOP else []" in body
        assert '_welcome["recents"] = items' in body

    def test_the_gate_opens_off_the_map_ready_ping(self, src):
        """The start page lands over a painted map: the gate effect is driven by the client's
        hype_map_ready nonce (map_bounds.js, with its own fallback timer), never ignore_init,
        and stays a one-shot `if _gated(): _show_welcome()`."""
        i = src.index("def _welcome_gate()")
        m = re.search(r"@reactive\.event\(input\.hype_map_ready[^)]*\)", src[i - 200:i])
        assert m and "ignore_init" not in m.group(0)
        body = _body(src, "def _welcome_gate()", "def _show_typed_pick_modal(")
        assert "if _gated():\n                _show_welcome()" in body


class TestTheMenusOwnButtons:
    def test_new_cannot_bypass_the_confirmation(self, src):
        """The page's New project tile goes straight to the pick/name dialog only under the
        gate. Reached from Projects with a project open, that would be a one-click session
        wipe in cloud mode."""
        shared = _body(src, "async def _begin_new_project()", "async def _new_create()")
        assert "if _gated():" in shared, "the helper must still go straight through at startup"
        assert "confirm_new_create" in shared and "confirm_new_project" in shared
        body = _body(src, "async def _welcome_new()", "@reactive.effect")
        assert "await _begin_new_project()" in body
        assert "_pick_path(" not in body, "_welcome_new still bypasses the helper"

    def test_welcome_open_carries_the_busy_check(self, src):
        """`_on_project_path` re-checks, but only after a native dialog has already opened, and
        rejecting a path the user just picked reads as a bug rather than as a guard."""
        body = _body(src, "async def _welcome_open()", "@reactive.effect")
        assert "_busy_tasks()" in body
        assert body.index("_busy_tasks()") < body.index("_pick_path(")

    def test_reopening_the_current_project_is_a_no_op(self, src):
        """The open project is touched into the store on every open, so it sits at the top of
        the list (the featured card) and is the easiest one to hit by accident. Reopening it
        would parting-save, wipe and rehydrate its way back to exactly the same state."""
        body = _body(src, "async def _welcome_recent()", "@reactive.effect")
        assert 'if _ws["project_file"] and Path(_ws["project_file"]) == p:' in body
        # ...and it bails BEFORE the reopen rather than after
        assert body.index('Path(_ws["project_file"]) == p') < body.index("_on_project_path(")

    def test_help_from_the_page_funnels_back(self, src):
        """Help raised from the rail replaces the gate (one modal at a time), so its Close is
        a server-side button that funnels through _ensure_welcome; a modal_button would strand
        a project-less session dialog-less."""
        body = _body(src, "def _show_help()", "def _about()")
        assert 'ui.input_action_button("help_close"' in body
        assert "easy_close=not _gated()" in body
        m = re.search(r'if _clicked_dynamic\("help_close"\):\s*\n\s*ui\.modal_remove\(\)\s*\n'
                      r"\s*_ensure_welcome\(\)", src)
        assert m, "help Close must modal_remove() then _ensure_welcome()"
