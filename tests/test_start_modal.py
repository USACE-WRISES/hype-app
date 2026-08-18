"""The start page (v1.0.5): boot veil + map-ready timing, the three-column page, the examples
view, and the copy rules. Source lints over app.py / www / CSS (the wiring lives in the server
closure); the downloader and pack tool have their own offline tests.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = (ROOT / "app.py").read_text(encoding="utf-8")
CSS = (ROOT / "www" / "styles.css").read_text(encoding="utf-8")
MAPJS = (ROOT / "www" / "map_bounds.js").read_text(encoding="utf-8")


def _body(start: str, end: str, src: str = APP) -> str:
    i = src.index(start)
    return src[i:src.index(end, i)]


class TestBootVeilAndTiming:
    def test_the_veil_is_in_the_layout_and_styled(self):
        assert 'id="hype-boot", class_="hype-boot"' in APP
        assert ".hype-boot {" in CSS and ".hype-boot.is-done" in CSS and ".hype-boot[hidden]" in CSS

    def test_map_bounds_posts_ready_once_the_main_map_attaches(self):
        # attach() is THE moment the #map Leaflet instance is in the DOM; the ping rides it,
        # with a fallback timer after shiny:connected so nobody is trapped behind the veil.
        attach = _body("function attach(map, tries)", "function guardVectors", MAPJS)
        assert "window.__hypeMap = map;" in attach
        assert "bootReady()" in attach
        boot = _body("function bootReady()", "var hooked", MAPJS)
        assert 'Shiny.setInputValue("hype_map_ready"' in boot
        assert 'priority: "event"' in boot
        assert 'getElementById("hype-boot")' in boot
        assert re.search(r'addEventListener\("shiny:connected", function \(\) \{ setTimeout\(bootReady, \d+\)',
                         MAPJS)


class TestThePage:
    def test_three_columns_and_the_scoped_widening(self):
        assert "_START_MODAL_CSS" in APP
        assert "#shiny-modal .modal-dialog{max-width:min(1180px,94vw)" in APP
        show = _body("def _show_welcome(", "def _welcome_cancel()")
        assert "ui.tags.style(_START_MODAL_CSS)" in show
        assert 'class_="hype-start"' in show
        assert "grid-template-columns: 232px minmax(0, 1fr) 300px" in CSS

    def test_the_rail_tiles_and_their_events(self):
        show = _body("def _show_welcome(", "def _welcome_cancel()")
        assert '_start_tile("welcome_new", "New project"' in show
        assert '_start_tile("welcome_open", "Open project"' in show
        assert '_start_tile("start_examples", "Example projects"' in show
        assert "if catalog else []" in show, "the examples tile hides when the catalog is empty"
        assert "_nonce_js(\"start_help\")" in show
        assert "ISSUES_URL" in show and 'target="_blank"' in show

    def test_recents_featured_plus_date_groups(self):
        home = _body("def _start_home_columns(", "def _show_welcome(")
        assert '_nonce_js("welcome_recent", i=0)' in home         # the featured card opens
        assert '_nonce_js("welcome_reveal", i=0)' in home         # Show in folder
        assert "_recent_groups(items)" in home
        groups = _body("def _recent_groups(", "def _start_recent_row(")
        assert '"Today"' in groups and '"Last 7 days"' in groups and '"Older"' in groups
        # rows keep the positional contract: index only, never a path, in inline JS
        row = _body("def _start_recent_row(", "def _start_home_columns(")
        assert '_nonce_js("welcome_recent", i=i)' in row
        assert '_nonce_js("welcome_recent_rm", i=i)' in row
        assert "event.stopPropagation()" in row

    def test_show_in_folder_selects_the_main_file(self):
        body = _body("def _welcome_reveal()", "# ---- Example projects")
        assert '["explorer", "/select,", str(p)]' in body

    def test_cloud_center_is_never_blank(self):
        home = _body("def _start_home_columns(", "def _show_welcome(")
        assert '"Get started"' in home


class TestExamplesView:
    def test_view_switch_and_back(self):
        assert '_show_welcome("examples")' in _body("def _start_examples_open()", "def _start_back()")
        assert '_show_welcome("home")' in _body("def _start_back()", "def _welcome_reveal()")

    def test_columns_are_outputs_registered_unsuspended(self):
        for name in ("start_gallery", "start_detail", "start_dl_status"):
            i = APP.index(f"def {name}()")
            assert "@output(suspend_when_hidden=False)" in APP[i - 120:i], name

    def test_download_follows_the_video_task_recipe(self):
        assert '"example": False' in _body("_task_armed = {", "_gms_pending")
        poll = _body("def _example_poll()", "def start_dl_status()")
        assert "reactive.invalidate_later(0.5)" in poll and "example_tick.set(" in poll
        done = _body("async def _example_done()", "async def _example_open(")
        assert '_task_armed["example"] = False' in done
        assert "examples_mod.ExampleCancelled" in done and "examples_mod.ExampleError" in done
        go = _body("async def _example_go(", "async def _example_done()")
        assert '_task_armed["example"] = True' in go and "example_task({" in go
        # cancel is the cooperative event, never task.cancel()
        assert "_example_cancel.set()" in _body("def _start_dl_cancel()", "def _start_pick()")
        assert "example_task.cancel()" not in APP

    def test_open_reuses_the_import_and_apply_paths(self):
        body = _body("async def _example_open(", "def _show_whatsnew()")
        assert '_pending_import["src"] = str(src)' in body
        assert "await _import_bundle_to(_example_target_for(ex))" in body
        assert "await _apply_project(str(src), fallback_name=ex.title)" in body
        assert "_ensure_welcome()" in body                     # errors never strand the gate

    def test_save_to_purpose_is_wired_through_every_picker_path(self):
        assert '_pick_path("example_target", save=True' in APP
        assert 'elif purpose == "example_target":' in _body("async def _on_project_path(",
                                                            "def _show_clash_modal(")
        assert 'if purpose == "example_target":' in _body("async def _dispatch_picked_result(",
                                                          "async def _start_dialog()")
        assert 'elif purpose == "example_target":' in _body("async def _dev_pick():",
                                                            "def _dev_pick_ow_back()")
        assert '== "example_target"' in _body("def _dev_pick_cancel()", "async def _dev_pick():")
        assert '"example_target": "Save Example Project To"' in APP

    def test_cloud_confirms_before_replacing_a_live_session(self):
        body = _body("async def _start_open()", "def _example_confirm_cancel()")
        assert "if not runmode.IS_DESKTOP and not _gated():" in body
        assert '"example_confirm_go"' in body

    def test_cloud_cache_lives_in_temp(self):
        assert 'examples_mod.set_cache_dir(Path(tempfile.gettempdir()) / "hype_examples")' in APP


class TestCopy:
    @pytest.mark.parametrize("marker_start, marker_end", [
        ("def _nonce_js(", "def _welcome_cancel()"),
        ("# ---- Example projects (the start page's second view)", "def _show_whatsnew()"),
        ("def _show_help()", "def _about()"),
    ])
    def test_no_em_dashes_in_new_ui_copy(self, marker_start, marker_end):
        body = _body(marker_start, marker_end)
        # strings only: comments in this codebase legitimately use em dashes
        strings = re.findall(r'"([^"\n]*)"', body)
        assert not [s for s in strings if "—" in s], "no em dashes in user-facing copy"

    def test_the_two_shipped_thumbnails_are_lean(self):
        for p in (ROOT / "www" / "examples").glob("*.jpg"):
            assert p.stat().st_size <= 80 * 1024, p.name
