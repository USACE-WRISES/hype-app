"""Source lints for the cross-project comparison workspace wiring in app.py.

The workspace lives in a server() closure, so these tests slice app.py source (the
established test_report.py style) and match CALL SITES, never bare names in comments.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "app.py").read_text(encoding="utf-8")
JS = (ROOT / "www" / "comparison.js").read_text(encoding="utf-8")
CSS = (ROOT / "www" / "styles.css").read_text(encoding="utf-8")


def _slice(start: str, end: str) -> str:
    return SRC[SRC.index(start):SRC.index(end)]


WORKSPACE = _slice("# ---- Cross-project comparison workspace",
                   "async def _start_dialog():")
LAUNCHER = _slice("# ---- Cross-Site Comparison (desktop only)",
                  "def _pane_chanmod():")


class TestTheOverlayAndSync:
    def test_the_overlay_div_and_script_are_mounted_desktop_only(self):
        assert 'ui.div(id="hype-comparison", class_="hype-compare", hidden=True)' in SRC
        assert '_asset("comparison.js")' in SRC
        mount = SRC[SRC.index('_asset("comparison.js")') - 200:
                    SRC.index('_asset("comparison.js")')]
        assert "runmode.IS_DESKTOP" in SRC[SRC.index('_asset("comparison.js")'):
                                           SRC.index('_asset("comparison.js")') + 120] \
            or "runmode.IS_DESKTOP" in mount

    def test_one_sync_effect_sends_the_full_payload(self):
        assert 'send_custom_message("hype_comparison"' in WORKSPACE
        assert "comparison_ui_payload(collection, inspections=inspections)" in WORKSPACE
        # hidden state is an explicit visible=False message, never a skipped send
        assert '{"visible": False}' in WORKSPACE

    def test_the_workspace_never_touches_the_active_model(self):
        # the read-only boundary: no work_dir rebinding, no engine calls, no map writes
        for forbidden in ("_adopt_workspace(", "restore_workspace(", "_rehydrate(",
                          "hz_task(", "gw_task(", "_set_layer("):
            assert forbidden not in WORKSPACE, forbidden


class TestTheDispatcher:
    def test_every_event_type_is_handled(self):
        for kind in ("add_projects", "refresh", "save", "save_as", "export", "back",
                     "relink_member", "member_select", "view", "axis_scale", "sort_order",
                     "metric_add", "metric_remove", "member_include", "remove_member",
                     "member_alias"):
            assert f'"{kind}"' in WORKSPACE, kind

    def test_the_single_metric_event_is_gone(self):
        # the Metric tab is a panel LIST now; the prototype's single-select event must not
        # come back beside it
        assert 'kind == "metric"' not in WORKSPACE
        assert 'post("metric",' not in JS

    def test_metric_panels_validate_dedupe_and_cap_at_six(self):
        assert "comparison_metrics.METRICS_BY_ID" in WORKSPACE
        assert "len(ids) >= 6" in WORKSPACE
        assert "Up to 6 metric panels" in WORKSPACE
        # a refused add still pokes the reactive so an optimistic client render resyncs
        refusal = WORKSPACE[WORKSPACE.index("Up to 6 metric panels"):]
        assert "comparison_collection_v.set(collection.model_copy())" in refusal
        # the client mirrors the cap instead of trusting the round trip
        assert "MAX_METRIC_PANELS = 6" in JS

    def test_add_paths_enforces_the_ten_project_cap(self):
        assert "10 - len(collection.members)" in WORKSPACE
        assert "up to 10 projects" in WORKSPACE


class TestThePickers:
    def test_comparison_picks_route_through_their_own_dispatcher(self):
        assert "async def _pick_comparison(" in WORKSPACE
        assert "async def _dispatch_picked_result(" in WORKSPACE
        # both reply paths funnel through it; the project flow is its fall-through
        assert "await _dispatch_picked_result(res, purpose)" in SRC
        assert "await _dispatch_picked_result(input.desktop_pick() or {})" in SRC
        assert "await _on_project_path(purpose, Path(str(raw_path)))" in WORKSPACE

    def test_every_comparison_purpose_is_dispatched(self):
        for purpose in ("comparison_add", "comparison_open", "comparison_save",
                        "comparison_save_as", "comparison_export", "comparison_relink:"):
            assert f'"{purpose}"' in WORKSPACE, purpose

    def test_save_pins_the_hypecompare_suffix(self):
        assert 'path.with_suffix(".hypecompare")' in WORKSPACE

    def test_the_tk_fallback_reopens_the_comparison_typed_modal(self):
        pick_done = SRC[SRC.index("async def _pick_done"):SRC.index("async def _pick_path")]
        assert '_show_comparison_typed_pick_modal(purpose)' in pick_done


class TestTheDoors:
    def test_welcome_gains_open_comparison_desktop_only(self):
        # The start page's rail carries "Open a comparison" as a quiet link (v1.0.5), still
        # desktop-only: comparisons need file access, and must not require an open project.
        i = SRC.index('ui.tags.button("Open a comparison"')
        assert '_nonce_js("welcome_compare")' in SRC[i:i + 300]
        assert "if runmode.IS_DESKTOP else []" in SRC[i:i + 300]

    def test_the_hub_row_is_bespoke_and_desktop_gated(self):
        assert "def _comparison_hub_row()" in LAUNCHER
        assert '_evt_btn("comparison_new_evt", "Compare projects…"' in LAUNCHER
        assert "disabled=not desktop" in LAUNCHER
        assert "_comparison_hub_row()" in SRC[SRC.index("def _pane_report_group"):]

    def test_the_tree_node_launcher_survives(self):
        assert '"report.cmp": _pane_report_cmp' in SRC
        assert 'hidden.add("report.cmp")' in SRC          # cloud hides the node
        assert "recents.load_comparisons()" in LAUNCHER
        assert '_evt_btn("comparison_new_evt", "New comparison"' in LAUNCHER
        assert '_evt_btn("comparison_open_evt", "Open comparison…"' in LAUNCHER

    def test_back_offers_save_discard_cancel_and_regates_the_welcome(self):
        assert '"Unsaved comparison"' in WORKSPACE
        for bid in ("comparison_back_cancel", "comparison_back_discard",
                    "comparison_back_save"):
            assert f'"{bid}"' in WORKSPACE, bid
        tail = WORKSPACE[WORKSPACE.index("async def _comparison_finish_back"):]
        body = tail[:tail.index("async def ", 10)]      # up to the NEXT coroutine def
        assert "_show_welcome()" in body


class TestCanonicalResults:
    def test_hz_completion_captures_and_persists(self):
        hz_done = SRC[SRC.index("async def _hz_done"):SRC.index("def _hz_error_text")] \
            if "def _hz_error_text" in SRC else \
            SRC[SRC.index("async def _hz_done"):SRC.index("async def _hz_done") + 4000]
        assert "_capture_canonical_results(hz=res)" in hz_done
        assert "_save_project_file()" in hz_done

    def test_the_capture_helper_is_the_report_gathers_source(self):
        assert "def _capture_canonical_results(" in SRC
        assert "results_lifecycle.build_canonical_results(" in SRC
        assert "_capture_canonical_results(hz, snap_dict)" in SRC   # the report gather call

    def test_alternatives_settle_through_one_sync_helper(self):
        assert "def _sync_canonical_alternatives(" in SRC
        assert "results_lifecycle.with_current_alternatives(" in SRC
        assert "_sync_canonical_alternatives(out)" in SRC           # _alt_done
        assert "_sync_canonical_alternatives(None, persist=False)" in SRC  # sweep wipe

    def test_exports_hold_the_matplotlib_lock_off_loop(self):
        export = WORKSPACE[WORKSPACE.index("async def _comparison_export_to"):]
        export = export[:export.index("@reactive.effect")]
        assert "with _REPORT_MPL_LOCK:" in export
        assert "anyio.to_thread.run_sync" in export


class TestTheOldFlowIsGone:
    def test_no_remnants_of_the_document_pipeline(self):
        for gone in ("compare_task", "cmp_sites", "_cmp_probe", "dl_cmp",
                     "_start_compare_build", "_compare_signature", "_COMPARE_FILES",
                     '"compare_sites"', '"compare_add"', "_compare_add_path",
                     "render_compare_panels"):
            assert gone not in SRC, gone
        assert not re.search(r"\bcompare\.", SRC), "the retired hype_app.compare is back"

    def test_the_module_and_its_tests_are_deleted(self):
        assert not (ROOT / "hype_app" / "compare.py").exists()
        assert not (ROOT / "tests" / "test_compare.py").exists()
        assert not (ROOT / "tests" / "test_compare_ui.py").exists()


class TestTheCopyRules:
    def test_workspace_strings_carry_no_em_dash(self):
        # string literals only: comments legitimately use em dashes
        for chunk in (WORKSPACE, LAUNCHER):
            for lit in re.findall(r'"([^"\n]*)"', chunk):
                assert "—" not in lit, lit

    def test_the_client_and_css_blocks_exist(self):
        assert 'addCustomMessageHandler("hype_comparison"' in JS
        assert 'setInputValue("comparison_event"' in JS
        assert "selected_metric_ids" in JS
        assert ".hype-compare__summary { table-layout: fixed; }" in CSS
        assert ".hype-compare .hype-compare__panel-remove" in CSS
