"""The HYPE start menu: the startup gate, and the header's `Open…` route into it.

WHAT THESE PROTECT. The menu was written as a HARD startup gate, and everything about it assumed
it could never be on screen while a project was open: no title bar, no footer, `easy_close=False`,
and a New Project button that skips the confirmation the header's New shows. The header's `Open…`
link makes it reachable mid-session, so each of those assumptions now needs holding down from both
directions - the gate must STAY a gate, and the mid-session dialog must stay escapable.

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
    def test_open_is_labelled_with_an_ellipsis(self, src):
        """`Save As…` already sets the convention in this nav: an ellipsis means the control
        raises a chooser rather than doing something immediately."""
        assert 'ui.input_action_link("nav_open", "Open…"' in src
        assert 'ui.input_action_link("nav_save_as", "Save As…"' in src, \
            "the convention this borrows from is gone; re-decide the label"
        # New stays a direct action, so it keeps a bare verb
        assert 'ui.input_action_link("nav_new", "New")' in src

    def test_open_raises_the_start_menu_in_both_modes(self, src):
        """It used to branch to the native picker on desktop and an upload modal on cloud,
        skipping the one screen that offers recents and New side by side."""
        body = _body(src, "async def _open_dialog()", "def _show_open_modal()")
        assert "_show_welcome()" in body
        assert "_pick_path(" not in body, "still going straight to the picker"
        assert "_show_open_modal()" not in body, "still going straight to the upload modal"
        assert "runmode.IS_DESKTOP" not in body, "the two modes should not diverge here"

    def test_open_keeps_its_busy_check(self, src):
        """Unchanged from before: every work_dir rebind needs no task holding a captured
        workspace path, and the check belongs before the chooser, not after the pick."""
        body = _body(src, "async def _open_dialog()", "def _show_open_modal()")
        assert "_busy_tasks()" in body
        assert body.index("_busy_tasks()") < body.index("_show_welcome()")

    def test_the_entry_point_is_show_not_ensure(self, src):
        """`_ensure_welcome` is `if _gated(): _show_welcome()`, so it no-ops once a project is
        open. Routing the header through it would make Open do nothing at all, and the ~25
        cancel funnels depend on that no-op staying exactly as it is."""
        assert "def _ensure_welcome():\n        if _gated():\n            _show_welcome()" in src
        body = _body(src, "async def _open_dialog()", "def _show_open_modal()")
        assert "_ensure_welcome()" not in body


class TestTheGateStaysAGate:
    def test_the_modal_is_dismissable_only_off_the_gate(self, src):
        """BOTH directions matter. Under the gate this must have no exit at all: `title=None`
        drops the header and its x, `footer=None` drops Bootstrap's default Dismiss, and
        `easy_close=False` blocks Esc and the backdrop. With a project open, the same three
        properties make it a trap, because nothing would ever remove it."""
        body = _body(src, "def _show_welcome()", "def _welcome_cancel()")
        assert "gated = _gated()" in body
        assert "easy_close=not gated" in body
        assert re.search(r"footer=\(None if gated\s*\n?\s*else ui\.input_action_button\("
                         r"\"welcome_cancel\", \"Cancel\"\)\)", body), \
            "the footer is no longer gate-conditional"

    def test_cancel_uses_the_strict_increment_guard(self, src):
        """The button is rebuilt on every modal show, so its counter resets to 0 each time and
        `@reactive.event` would miss every click after the first. Same trap `_open_cancel`
        avoids."""
        body = _body(src, "def _welcome_cancel()", "def _show_new_project_dialog()")
        assert '_clicked_dynamic("welcome_cancel")' in body
        assert "ui.modal_remove()" in body
        # The DECORATOR, not the word (the comment above the guard names it too), and the
        # decorators sit ABOVE the def, so this has to look backwards from it.
        i = src.index("def _welcome_cancel")
        assert not re.search(r"^\s*@reactive\.event", src[i - 200:i], re.M)

    def test_recents_are_re_read_on_every_show(self, src):
        """`_adopt_workspace` touches the store on every project open and the handlers index
        `_welcome["recents"]` POSITIONALLY, so a cached row set opens the wrong project."""
        body = _body(src, "def _show_welcome()", "def _welcome_cancel()")
        assert "items = recents.load()[:8]" in body
        assert '_welcome["recents"] = items' in body


class TestTheMenusOwnButtons:
    def test_new_cannot_bypass_the_confirmation(self, src):
        """The menu's New Project used to go straight to the pick/name dialog, which was safe
        only while the menu was unreachable with a project open. Reached from `Open…`, that
        would be a one-click session wipe in cloud mode."""
        shared = _body(src, "async def _begin_new_project()", "async def _confirm_new_project()")
        assert "if _gated():" in shared, "the helper must still go straight through at startup"
        assert "confirm_new_create" in shared and "confirm_new_project" in shared

        for handler, end in (("async def _confirm_new_project()", "@reactive.effect"),
                             ("async def _welcome_new()", "@reactive.effect")):
            body = _body(src, handler, end)
            assert "await _begin_new_project()" in body, handler
            assert "_pick_path(" not in body, f"{handler} still bypasses the helper"

    def test_welcome_open_carries_the_busy_check(self, src):
        """`_on_project_path` re-checks, but only after a native dialog has already opened, and
        rejecting a path the user just picked reads as a bug rather than as a guard."""
        body = _body(src, "async def _welcome_open()", "@reactive.effect")
        assert "_busy_tasks()" in body
        assert body.index("_busy_tasks()") < body.index("_pick_path(")

    def test_reopening_the_current_project_is_a_no_op(self, src):
        """The open project is touched into the store on every open, so it sits at the top of
        the list and is the easiest row to hit by accident. Reopening it would parting-save,
        wipe and rehydrate its way back to exactly the same state."""
        body = _body(src, "async def _welcome_recent()", "@reactive.effect")
        assert 'if _ws["project_file"] and Path(_ws["project_file"]) == p:' in body
        # ...and it bails BEFORE the reopen rather than after
        assert body.index('Path(_ws["project_file"]) == p') < body.index("_on_project_path(")
