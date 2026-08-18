"""Hyporheic web app — a StreamStats-style Shiny app that builds and runs a MODFLOW 6 +
MODPATH 7 hyporheic model from a map-defined reach, and shows the pathlines/heads.

Flow (six stages, shown as the header stage bar): define the reach (auto NHD pick or manual
draw) → terrain auto-fetches from 3DEP → boundaries auto-generate from bankfull geometry →
choose the water surface (HEC-RAS 2025 2D run / wetted extent / uploaded raster) → review
subsurface properties + grid and run MODFLOW 6 + MODPATH 7 → delineate the hyporheic zone
and explore flow paths, volumes, and heads. Download the whole project as a zip.
"""
from __future__ import annotations

import json
import math
import multiprocessing as mp
import os
import queue as _queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# 3DEP/HyRiver cache -> ephemeral /tmp (set before py3dep import, which happens in hype_app.dem)
os.environ.setdefault("HYRIVER_CACHE_NAME", os.path.join(tempfile.gettempdir(), "hype_hyriver.sqlite"))
os.environ.setdefault("HYRIVER_CACHE_EXPIRE", str(7 * 24 * 3600))

# Quiet two harmless, environment-emitted startup messages on the headless server (set before
# matplotlib / shinywidgets load below): matplotlib scanning the non-scalable Noto color-emoji
# font while building its cache, and shinywidgets' own internal use of the deprecated
# ipywidgets `Widget.widgets` API.
import logging  # noqa: E402
import warnings  # noqa: E402
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
warnings.filterwarnings("ignore", message=r".*Widget\.widgets is deprecated.*")

import anyio  # noqa: E402
from shiny import App, reactive, render, ui  # noqa: E402

from hype_app import (assess, bieger, bundle, carve, changelog, delineate, dem, dims,  # noqa: E402
                      estimate, examples as examples_mod, geocode, geometry, gms, hydro,
                      hz_results, map_layers as ml_mod, mesh,
                      project_meta, ras_results, recents, report as report_mod, results,
                      results_lifecycle, runmode, scene, signature, snapshot, ui_tree,
                      video as video_mod, wells as wells_mod)
from hype_app import alt_screening  # noqa: E402
from hype_app import comparison as comparison_mod  # noqa: E402
from hype_app import comparison_metrics  # noqa: E402
from hype_app import alternatives as alt_mod  # noqa: E402
from hype_app import gms_run  # noqa: E402
from hype_app import hz_run  # noqa: E402
from hype_app import pathpick  # noqa: E402
from hype_app import pick_run  # noqa: E402
from hype_app import alt_run  # noqa: E402
from hype_app import soil_run  # noqa: E402
from hype_app import usgs_run  # noqa: E402
from hype_app import ras as ras_engine  # noqa: E402
from hype_app import run as runner  # noqa: E402
from hype_app.contracts.flow import watershed_display_features  # noqa: E402
from hype_app.services.regions import region_choices  # noqa: E402

# Every screening parameter, label, tooltip and citation comes from the registry, so the panes and
# the report never hardcode one (functions plan §5). `fn_pol` holds the cited pollutant endpoints
# and, in TERMS, the vocabulary each one is allowed to use (screening reference §7).
from dataclasses import replace as dc_replace  # noqa: E402
from hype_app import functions as fn_reg  # noqa: E402
from hype_app.functions import pollutants as fn_pol  # noqa: E402

try:
    from ipyleaflet import (DivIcon, DrawControl, GeoJSON, ImageOverlay, LayerGroup,
                            Map, Marker, ScaleControl, TileLayer, ZoomControl)
    from ipywidgets import Layout
    from shinywidgets import output_widget, reactive_read, render_widget
    _HAS_MAP = True
except Exception:  # pragma: no cover
    _HAS_MAP = False

#: Screening input ids minted from the registry rather than written out, so adding a calculator or
#: a cited endpoint cannot leave one out of the keep mirror. All three are STATIC -- the registry
#: is fixed at import -- so `_KEEP_IDS` stays a tuple and `_keep_inputs` stays a flat sweep.
#: Module scope so the tests can assert against the real tuples rather than against app.py's text.
FN_INCLUDE_IDS = tuple(f"fn_incl_{k}" for k in fn_reg.SECTION_ORDER)
FN_POL_CONC_IDS = tuple(f"fn_pol_conc_{p.key}" for p in fn_pol.PRESETS)

#: ONE id for the whole endpoint picker. It was one `input_checkbox_group` per preset group until
#: 2026-08-01: correct, and about 250 px of pane before the first number. A `selectize` multi-select
#: is the same choice as chips with type-to-search, in roughly 40 px, and the dropdown still shows
#: every endpoint under its group heading -- which is the one thing the checklists were better at.
FN_POL_SELECT_ID = "fn_pol_endpoints"

USGS_IMAGERY = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
USGS_TOPO = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
USGS_ATTR = "USGS The National Map"

# Flow-path layers carry classNames so map_edit_style can route pointer events to THEM on the
# Results step — the wetted-extent polygon's (invisible-ish) fill otherwise sits on top and
# swallows every real click over the domain (synthetic element-targeted clicks can't see this).
PATH_STYLE = {"color": "#08306b", "weight": 2, "opacity": 0.9,
              "className": "hype-fp-line"}                           # flow paths (dark blue)
PATH_HOVER = {"color": "#ffd500", "weight": 4}                       # hovered flow path (gold)
SEL_STYLE = {"color": "#ff9500", "weight": 4, "opacity": 1.0,
             "className": "hype-fp-sel"}                             # selected flow paths (orange)
START_NODE_STYLE = {"color": "#000000", "weight": 1.2, "fillColor": "#2b7bff",
                    "fillOpacity": 1.0, "radius": 4,
                    "className": "hype-fp-node"}                     # path start (blue, black ring)
END_NODE_STYLE = {"color": "#000000", "weight": 1.2, "fillColor": "#e02020",
                  "fillOpacity": 1.0, "radius": 4,
                  "className": "hype-fp-node"}                       # path end (red, black ring)
GRID_STYLE = {"color": "#555555", "weight": 0.5, "opacity": 0.5, "fillOpacity": 0.0}
CONTOUR_STYLE = {"color": "#11161c", "weight": 1, "opacity": 0.85, "fillOpacity": 0.0}
# drawn inputs — thin outlines / minimal fill so they never hide the head raster underneath
DOMAIN_STYLE = {"color": "#caa700", "weight": 2, "opacity": 0.95, "fill": False}
WSE_STYLE = {"color": "#1aa6a6", "weight": 2, "opacity": 0.95, "fillColor": "#1aa6a6", "fillOpacity": 0.12}
# isolated pools the upstream–downstream connectivity filter removed from the GW extent
REMOVED_STYLE = {"color": "#dc2626", "weight": 1.5, "opacity": 0.9, "dashArray": "4 4",
                 "fillColor": "#dc2626", "fillOpacity": 0.08}
LEFT_STYLE = {"color": "#1f6feb", "weight": 3, "opacity": 0.95}      # Left FPL (blue)
RIGHT_STYLE = {"color": "#d83933", "weight": 3, "opacity": 0.95}     # Right FPL (red)
UP_STYLE = {"color": "#f08c00", "weight": 3, "opacity": 0.95}        # Upstream boundary (orange)
DOWN_STYLE = {"color": "#9b59b6", "weight": 3, "opacity": 0.95}      # Downstream boundary (purple)
KZONE_STYLE = {"color": "#7b3fa0", "weight": 2, "opacity": 0.95, "fill": False}
WELL_COLOR = "#15803d"   # observation wells (deep green — a free slot in the map palette)
SOILS_STYLE = {"color": "#8a6d3b", "weight": 1, "opacity": 0.9,        # NRCS SSURGO polygons (tan)
               "fillColor": "#d2b48c", "fillOpacity": 0.22}            # (soils modal review map)
SOILS_SEL_STYLE = {"color": "#1f6feb", "weight": 2, "opacity": 1.0,    # soils modal: units picked
                   "fillColor": "#5eead4", "fillOpacity": 0.35}        # for K-zone import
SOILS_HOVER = {"weight": 2.5, "fillOpacity": 0.35}                     # soils modal: click affordance
NHD_STYLE = {"color": "#00c2ff", "weight": 3.5, "opacity": 0.95}     # clickable NHD flowlines (bold)
NHD_MIN_ZOOM = 16   # flowlines fetch/draw only at this zoom or deeper — wider views stay clean
MAP_HOME_CENTER = (39.5, -98.35)   # the national (CONUS) view: first launch + New project
MAP_HOME_ZOOM = 4
REACH_STYLE = {"color": "#ff2d95", "weight": 5, "opacity": 0.95}     # the analysis reach (magenta — pops on USGS topo, distinct from cyan NHD)
CAP_STYLE = {"color": "#333333", "weight": 2, "opacity": 0.9, "dashArray": "6 5", "fill": False}
USGS_WATERSHED_STYLE = {"color": "#0f766e", "weight": 2, "opacity": 0.9,
                        "fillColor": "#5eead4", "fillOpacity": 0.25}   # flow modal: delineated basin

# An empty FeatureCollection — used by _decor_show to CLEAR a layer's rendered children before a
# visible False→True reveal, so the reveal's addData renders nothing and the following data-set is
# the only addData (guards against the bursty-flush double-add that duplicated the reach line).
_EMPTY_FC = {"type": "FeatureCollection", "features": []}

(STEP_REACH, STEP_DEM, STEP_BOUNDARIES, STEP_SURFACE, STEP_K, STEP_MESH, STEP_RUN,
 STEP_RESULTS, STEP_REPORT) = (
    "reach", "dem", "boundaries", "surface", "k", "mesh", "run", "results", "report")

RAS_UNAVAILABLE_MSG = (
    "The HEC-RAS 2025 engine isn't available here — on Windows set HYPE_RAS_BIN to a "
    "HEC-RAS 2025 install (the folder containing ras.exe); on Linux the bundled "
    "bin/ras2025 runtime is used.")

MODFLOW_UNAVAILABLE_MSG = (
    "MODFLOW 6 / MODPATH 7 not found — expected mf6 and mp7 in the bundled bin/win "
    "(Windows) or bin/linux folder, or set HYPE_MODFLOW_BIN to a folder containing them.")

APP_VERSION = "1.0.5"          # single source of truth: About dialog, header chip, start page,
                               # run_config.json, the project-file manifest, and report footers.
                               # Bump TOGETHER with desktop/src/Hype.Desktop/Hype.Desktop.csproj
                               # <Version> and a matching CHANGELOG.md section (test-pinned).
APP_VERSION_LABEL = f"v{APP_VERSION}"   # user-visible spelling (header chip + start page)
ISSUES_URL = "https://github.com/USACE-WRISES/hype-app/issues"   # start page: Report an issue

# Start page modal geometry. `.modal-dialog` / `.modal-body` are shared Bootstrap classes, so the
# widening is injected INLINE with the modal (scoped to #shiny-modal while it is open) exactly like
# the report modal does, instead of globally in styles.css where it would resize every size="xl"
# dialog. Three columns need the width; the fixed height lets each column scroll on its own.
_START_MODAL_CSS = (
    # The dialog is a flex row that fills the viewport height minus its margins, so the fixed-
    # height page sits vertically CENTERED (Bootstrap's own .modal-dialog-centered recipe) rather
    # than pinned to the top with a dead band under it on tall windows.
    "#shiny-modal .modal-dialog{max-width:min(1180px,94vw);width:94vw;margin:1rem auto;"
    "display:flex;align-items:center;min-height:calc(100% - 2rem)}"
    "#shiny-modal .modal-content{height:min(720px,calc(100vh - 2rem));"
    "max-height:calc(100vh - 2rem);overflow:hidden;display:flex;flex-direction:column;"
    "border-radius:12px;border:0}"
    "#shiny-modal .modal-body{flex:1 1 auto;min-height:0;padding:0;overflow:hidden;display:flex}")

# Placeholder art for a recent-project card (no per-project thumbnail exists yet): a floodplain
# band with a reach through it, in the map-overlay palette. Inline so it ships with the page.
_START_GLYPH_SVG = (
    '<svg viewBox="0 0 160 100" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">'
    '<rect width="160" height="100" rx="8" fill="#eef3fb"/>'
    '<path d="M0 62 C 30 40, 55 84, 85 58 S 135 30, 160 44 L160 100 L0 100 Z" '
    'fill="#c8d8f0" opacity=".7"/>'
    '<path d="M0 50 C 30 28, 55 72, 85 46 S 135 18, 160 32" fill="none" stroke="#2f4b7c" '
    'stroke-width="3" stroke-linecap="round"/>'
    '<path d="M0 26 C 30 6, 55 50, 85 24 S 135 -4, 160 10" fill="none" stroke="#c8d8f0" '
    'stroke-width="2" stroke-dasharray="4 4"/>'
    '<path d="M0 74 C 30 54, 55 96, 85 70 S 135 42, 160 56" fill="none" stroke="#c8d8f0" '
    'stroke-width="2" stroke-dasharray="4 4"/>'
    '</svg>')


def _desktop_build_line() -> str:
    """About-modal desktop build identity. desktop-manifest.json sits next to app.py only
    inside an installed apps payload (build-apps-payload.ps1 via gen_desktop_manifest.py);
    dev checkouts and cloud deploys have no such file and add nothing."""
    if not runmode.IS_DESKTOP:
        return ""
    try:
        mf = json.loads((Path(__file__).resolve().parent / "desktop-manifest.json")
                        .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    apps, env = mf.get("version"), mf.get("requiresEnv")
    if not apps:
        return ""
    return f"\n\nDesktop build {apps}" + (f" (runtime {env})" if env else "")


def _changelog_md() -> str:
    """What's new dialog body. CHANGELOG.md sits next to app.py in a dev checkout AND in an
    installed apps payload (build-apps-payload.ps1 ships it in the git-archive pathspec).
    Best-effort: a missing or unreadable file collapses the dialog to the version line."""
    try:
        text = (Path(__file__).resolve().parent / "CHANGELOG.md").read_text(encoding="utf-8")
    except OSError:
        return ""
    # The modal supplies its own title; the file's H1 would just repeat it.
    return re.sub(r"^# .*\n+", "", text, count=1)

# pyplot state is process-global and report_task renders its ~10 figures on a worker thread,
# so concurrent sessions' report builds must serialize. Event-loop pyplot users (xsect profile,
# colorbar legends) stay unlocked on purpose: blocking the loop on a build's lock would be worse
# than the rare collision — all figure code is per-figure OO on Agg, so a collision is at worst
# a retryable failure toast.
_REPORT_MPL_LOCK = threading.Lock()

# ---- hyporheic-zone delineation (post-run particle classification) ----
HZ_CLASSES = ("hyporheic", "losing", "gaining", "throughflow")
HZ_COLORS = {"hyporheic": "#0d9488", "losing": "#dc2626",       # teal / red
             "gaining": "#2563eb", "throughflow": "#a16207"}     # blue / amber
HZ_LABEL = {"hyporheic": "Hyporheic", "losing": "Streamflow losing",
            "gaining": "Streamflow gaining", "throughflow": "Groundwater throughflow"}
# per-class 2-D pathline style (same className as the monolithic paths → map_edit_style's
# interactive whitelist + tree.js's stroke-only deselect guard apply unchanged)
HZ_PATH_STYLE = {cls: {"color": HZ_COLORS[cls], "weight": 2, "opacity": 0.9,
                       "className": "hype-fp-line"} for cls in HZ_CLASSES}
HZ_FOOT_STYLE = {cls: {"color": HZ_COLORS[cls], "weight": 1.5, "opacity": 0.9,
                       "fillColor": HZ_COLORS[cls], "fillOpacity": 0.18} for cls in HZ_CLASSES}
# streambed exchange map (Flows node): downwelling cells share the losing red, upwelling
# cells the gaining blue — the same direction semantics as the class colors. The className
# makes them click-through (styles.css) so they never smother flow-path selection.
FLOW_DOWN_STYLE = {"color": "#991b1b", "weight": 0.5, "opacity": 0.8,
                   "fillColor": HZ_COLORS["losing"], "fillOpacity": 0.45,
                   "className": "hype-flow-ex"}
FLOW_UP_STYLE = {"color": "#1e40af", "weight": 0.5, "opacity": 0.8,
                 "fillColor": HZ_COLORS["gaining"], "fillOpacity": 0.45,
                 "className": "hype-flow-ex"}
# flow-path particle animation (www/path_anim.js): swatch palette for the Flow paths pane.
# Hot magenta default — no layer uses it (paths are teal/red/blue/amber, hover gold,
# selection orange) and it pops on both USGS basemaps.
FP_ANIM_COLORS = ("#ff2bd6", "#00e5ff", "#ffffff", "#b4ff39", "#000000", "#ff2b2b")
FP_ANIM_COLOR_NAMES = ("Magenta", "Cyan", "White", "Lime", "Black", "Red")
# Particle color modes: "solid" paints every particle the picked swatch color; the two
# rainbow modes map residence time onto a turbo scale (log axis, blue quick to red slow,
# legend drawn by the client/renderer). "total" fixes each particle at its path's total
# residence-time color; "elapsed" shifts the color as the particle ages in transit, so
# it lands on its total-time color exactly as it exits.
FP_ANIM_MODES = ("solid", "total", "elapsed")
# Line color modes share the SAME turbo scale: "class" keeps the identity palette,
# "single" one picked color, "total" one residence-time color per path, "elapsed" a
# gradient along each path (3-D and captures; the 2-D map shows each line's total-time
# color — Leaflet cannot gradient a polyline at this path count).
FP_LINE_MODES = ("class", "total", "elapsed")
FP_LINE_RAINBOW = ("total", "elapsed")
HZ_TOTAL = 7
HZ_STEPS = {0: "Preparing…", 1: "Loading the flow solution", 2: "Seeding particles",
            3: "Tracking forward (endpoints)", 4: "Tracking backward (endpoints)",
            5: "Classifying + delineating volumes", 6: "Tracing display pathlines",
            7: "Writing artifacts"}
HZ_MAX_PARTICLES = runmode.hz_particle_cap()

BC_CORNER = "4 Corner Gradients"   # legacy value: no UI mode anymore; restores migrate to points
BC_PROFILE = "Spatially Varying Gradient"
BC_QUAL = "Qualitative"          # app-side only: category × reference slope -> profile strings
# Qualitative categories (revision §3.4): signed multiplier × reference slope; the slight/strong
# magnitudes are editable in the pane (defaults 0.5/1.0), so the labels stay scale-free.
_QUAL_CHOICES = {"strongly_gaining": "Strongly gaining",
                 "slightly_gaining": "Slightly gaining",
                 "neutral": "Neutral",
                 "slightly_losing": "Slightly losing",
                 "strongly_losing": "Strongly losing"}

# Progress labels keyed by the driver's "STEP N" log markers (the headless run emits 2–7).
RUN_TOTAL = 7
RUN_STEPS = {0: "Preparing terrain & geometry…", 1: "Preprocessing",
             2: "Building model domain", 3: "Boundaries & active domain",
             4: "Computing boundary heads", 5: "Running MODFLOW 6 + MODPATH 7",
             6: "Post-processing pathlines", 7: "Exporting head layers"}

_WWW = Path(__file__).parent / "www"


def _asset(name: str) -> str:
    """Append the file's mtime as a cache-busting ?v= so browsers re-fetch our static assets after
    any edit. Shiny serves styles.css / *.js with no version, so browsers cache them hard and keep
    using the stale copy across server restarts — which is why a fixed CSS/JS silently didn't apply
    (a restarted server serves the new file, but the browser never re-requests it)."""
    try:
        v = int(_WWW.joinpath(name).stat().st_mtime)
    except OSError:
        v = 0
    return f"{name}?v={v}"


app_ui = ui.page_fillable(
    ui.head_content(
        ui.tags.link(rel="preconnect", href="https://fonts.googleapis.com"),
        ui.tags.link(rel="preconnect", href="https://fonts.gstatic.com", crossorigin=""),
        ui.tags.link(rel="stylesheet",
                     href="https://fonts.googleapis.com/css2?family=Instrument+Sans:wght@400;500;600&family=Space+Grotesk:wght@400;500;600;700&display=swap"),
        ui.tags.link(rel="stylesheet", href=_asset("styles.css")),
        ui.tags.script(src=_asset("geocode.js")),
        ui.tags.script(src=_asset("reach_draw.js")),
        ui.tags.script(src=_asset("map_bounds.js")),  # reports the live view bounds to Shiny
        ui.tags.script(src=_asset("flowpath_select.js")),  # Results: box-select flow paths
        ui.tags.script(src=_asset("path_anim.js")),  # flow-path particle animation (client-only)
        ui.tags.script(src=_asset("raster_probe.js")),  # hover value chip for raster layers
        ui.tags.script(src=_asset("measure2d.js")),  # map length ruler (client-only, always on)
        ui.tags.script(src=_asset("xsection.js")),   # terrain cross-section (shown while DEM on)
        ui.tags.script(src=_asset("mesh3d.js")),     # lazy-loads vtk.js from a CDN on first Compute
        ui.tags.script(src=_asset("tree.js")),       # layer tree (left panel) + panel chrome
        ui.tags.script(src=_asset("info_tip.js")),   # hover/focus help chips on .hype-info-tip
        ui.tags.script(src=_asset("export_menu.js")),  # header Export menu + view capture
        # Desktop-only shell bridge (native pickers, window title). Served only when the
        # process runs in desktop mode so the cloud page stays byte-identical; the script
        # itself also no-ops without WebView2 (plain dev browser).
        *([ui.tags.script(src=_asset("desktop_bridge.js"))] if runmode.IS_DESKTOP else []),
        # Cross-project comparison workspace (desktop only): entirely client-rendered from
        # "hype_comparison" custom messages; one comparison_event input carries every action.
        *([ui.tags.script(src=_asset("comparison.js"))] if runmode.IS_DESKTOP else []),
    ),
    ui.div(
        ui.div(
            # Left zone: brand + project badge (both modes), grouped so the badge stays
            # snug against the wordmark at any window width (empty until a project exists).
            ui.div(ui.span("HYPE", ui.tags.small("Hyporheic Exchange Explorer"),
                           class_="hype-brand"),
                   ui.output_ui("project_badge", inline=True),
                   # The version number IS the changelog door: clicking it opens the
                   # What's new dialog (same from the welcome splash and the About footer).
                   ui.span(APP_VERSION_LABEL, class_="hype-version-chip",
                           title="What's new in HYPE",
                           onclick="Shiny.setInputValue('whatsnew_evt', "
                                   "Date.now() + Math.random(), {priority: 'event'})"),
                   class_="hype-header-left"),
            # 2D/3D canvas toggle — plain buttons, delegated via www/tree.js (data-view),
            # active states synced from the hype_tree payload's `view` field. Middle grid
            # column, so it sits at true window center (the map below is full-bleed).
            # The Export menu rides in the same center cell (www/export_menu.js owns its
            # open/close and dispatch; the items post export_evt nonce events).
            ui.div(
                ui.div(ui.tags.button("2D map", type="button",
                                      class_="hype-view-btn active",
                                      **{"data-view": "2d"}),
                       ui.tags.button("3D view", type="button", class_="hype-view-btn",
                                      **{"data-view": "3d"}),
                       class_="hype-view-toggle"),
                # Snipping-tool-style capture control: the icon button EXECUTES the
                # capture (camera = still, video = recording, per the selected mode),
                # the slim arrow opens the options. www/export_menu.js owns the icon
                # swap, the mode/target state, and the whole dispatch matrix.
                ui.div(
                    ui.tags.button(type="button", id="hype-export-btn",
                                   class_="hype-export-btn",
                                   title="Capture the view",
                                   **{"aria-label": "Capture"}),
                    ui.tags.button(type="button", id="hype-export-arrow",
                                   class_="hype-export-arrow",
                                   title="Capture options",
                                   **{"aria-label": "Capture options"}),
                    ui.div(
                        ui.div("Mode", class_="hype-export-head"),
                        ui.div(
                            ui.tags.button("Camera", type="button",
                                           class_="active", **{"data-mode": "camera"}),
                            ui.tags.button("Video", type="button",
                                           **{"data-mode": "video"}),
                            class_="hype-export-modes"),
                        ui.div("Capture", class_="hype-export-head"),
                        ui.tags.button(
                            ui.tags.span("Specified Window"),
                            ui.tags.small("Drag a rectangle over the view"),
                            type="button", class_="hype-export-target sel",
                            **{"data-target": "view"}),
                        ui.tags.button(
                            ui.tags.span("Full View Extent"),
                            ui.tags.small("Everything currently in view"),
                            type="button", class_="hype-export-target",
                            **{"data-target": "full"}),
                        # Video-mode-only settings (export_menu.js shows/hides + posts
                        # the values with each export_evt, so no Shiny inputs needed)
                        ui.div(
                            ui.div("Video settings", class_="hype-export-head"),
                            ui.div(ui.span("Length (s)"),
                                   ui.tags.input(type="number", min="2", max="30",
                                                 step="1", value="8",
                                                 **{"data-k": "secs"}),
                                   ui.tags.button("30 fps", type="button",
                                                  class_="active",
                                                  **{"data-fps": "30"}),
                                   ui.tags.button("15 fps", type="button",
                                                  **{"data-fps": "15"}),
                                   class_="hype-export-vidrow"),
                            class_="hype-export-vidset", style="display:none;"),
                        id="hype-export-menu", class_="hype-export-menu"),
                    id="hype-export", class_="hype-export"),
                class_="hype-header-center"),
            # One door to the start page (New, Open, Example projects, recents, What's new),
            # the way RAS2025 reopens its own project dialog through "Projects". No ellipsis:
            # it raises the start page, not a chooser.
            ui.div(ui.input_action_link("nav_start", "Projects",
                                        title="New, open, example and recent projects"),
                   ui.output_ui("save_project"),
                   ui.tags.span(class_="hype-nav-sep"),
                   ui.input_action_link("nav_about", "About"),
                   ui.input_action_link("nav_help", "Help"), class_="hype-nav"),
            class_="hype-header",
        ),
        # Stage bar — the visible workflow (6 numbered chips under the header). Chips are plain
        # data-jump buttons; tree.js's delegated handler routes clicks through tree_event select.
        ui.output_ui("stage_bar"),
        ui.div(
            output_widget("map", height="100%") if _HAS_MAP
            else ui.div("Map requires ipyleaflet + shinywidgets.", class_="p-3"),
            class_="hype-map-wrap",
        ),
        # Layer tree (left floating panel). The rows are rendered/reconciled CLIENT-side by
        # www/tree.js from "hype_tree" custom messages — zero Shiny inputs in this subtree.
        ui.div(
            ui.div(ui.span(class_="hype-panel-caret"),
                   ui.span("Layers", class_="hype-panel-title"),
                   class_="hype-panel-head"),
            ui.div(id="hype-tree-body", class_="hype-tree-body"),
            id="hype-tree-panel", class_="hype-tree-panel",
        ),
        # Properties panel (right floating card) — content is the server-rendered pane for the
        # selected tree node; www/tree.js shows/hides the card as the output fills/empties.
        ui.div(ui.output_ui("propspane"), id="hype-props-panel", class_="hype-props-panel"),
        # Hidden Shiny file input, ALWAYS mounted: mesh3d.js drops the recorded 3D-view
        # webm into it so it rides Shiny's own chunked uploader (dynamic routes are
        # GET-only, and multi-MB payloads must never ride the websocket). It lives in the
        # main layout, not the Flow paths pane, so the header capture control can record
        # while that pane is closed.
        ui.div(ui.input_file("fp3d_webm", None, accept=[".webm"]), style="display:none;"),
        # Same pattern for a browser-3D still: export_menu.js drops the captured canvas
        # PNG here so it reaches the preview modal through the chunked uploader.
        ui.div(ui.input_file("capture_png", None, accept=[".png"]),
               style="display:none;"),
        # Floating bottom-right progress card for the pathline animation build. Lives
        # in the main layout so it shows regardless of which pane is open.
        ui.output_ui("fp_video_status"),
        ui.output_ui("readout"),
        ui.output_ui("flow_loading"),
        ui.output_ui("alt_banner"),
        ui.output_ui("map_edit_style"),
        ui.output_ui("xsect_style"),
        ui.div(id="hype-mesh3d", class_="hype-mesh3d"),     # 3D mesh viewer overlay (vtk.js)
        ui.output_ui("mesh3d_style"),
        # Cross-project comparison workspace overlay. Hidden until the server sends a
        # comparison payload, so the normal single-project canvas is untouched; comparison.js
        # owns everything inside this div.
        ui.div(id="hype-comparison", class_="hype-compare", hidden=True),
        # Boot veil: covers the shell from the first byte until the main Leaflet map is in the
        # DOM (www/map_bounds.js hides it and posts `hype_map_ready`, with a 6 s fallback so
        # nobody is ever trapped behind it). Same palette as the desktop launcher page, so the
        # shell's handoff reads as one continuous loading screen; in the cloud it is the only
        # loading screen. The start page (_welcome_gate) opens off that same ready ping, so it
        # always lands over a painted map, never over a blank one.
        ui.div(
            ui.div(ui.span("HYPE", class_="hype-boot-mark"),
                   ui.div("Hyporheic Exchange Explorer", class_="hype-boot-sub"),
                   ui.div(class_="hype-boot-spin"),
                   ui.div("Loading the map", class_="hype-boot-msg"),
                   class_="hype-boot-card"),
            id="hype-boot", class_="hype-boot"),
        class_="hype-shell",
    ),
    title="HYPE — Hyporheic Exchange Explorer",
    padding=0,
    fillable=True,
)


def server(input, output, session):
    work_dir = Path(tempfile.mkdtemp(prefix="hype_session_"))
    # Desktop project-folder mode: when a project is open, work_dir IS the folder holding the
    # main .hype and nothing here may delete it. _ws is a plain mirror of project_file readable
    # from teardown (session.on_ended must not touch reactives); project_file drives the UI.
    _ws: dict = {"project_file": None,       # str | None — absolute path of the main .hype
                 "project_name": None}       # str | None — display name (desktop: file stem)
    project_file = reactive.value(None)
    # Project identity metadata: name + locked units token + created stamp. Rides
    # config/state.json as first-class keys (additive), so BOTH bundle kinds round-trip
    # it. The _ws mirror lets non-reactive readers (teardown, _gated, filename lambdas)
    # see the name; the reactive drives the badge, pane, and tab title.
    project_meta_v = reactive.value({"name": None, "units": project_meta.UNITS_METRIC,
                                     "created": None, "project_id": None, "site_id": None})

    def _set_project_meta(name, created, units: str = project_meta.UNITS_METRIC, *,
                          project_id: str | None = None, site_id: str | None = None,
                          mint_missing: bool = False):
        name = (str(name).strip() if name else "") or None
        if mint_missing and name:
            # Immutable identities: minted once for a named project, then carried through
            # every save. Cross-project comparison keys duplicate detection off these.
            project_id = project_id or project_meta.new_identity()
            site_id = site_id or project_meta.new_identity()
        _ws["project_name"] = name
        project_meta_v.set({"name": name, "units": units or project_meta.UNITS_METRIC,
                            "created": created, "project_id": project_id,
                            "site_id": site_id})

    # Cross-project comparison: a second, read-only UI mode over frozen snapshots, not
    # another modeling workspace. These values never participate in work_dir rebinding,
    # solver inputs, the map, or project autosave.
    comparison_mode_v = reactive.value(False)
    comparison_collection_v = reactive.value(None)
    comparison_file_v = reactive.value(None)
    comparison_dirty_v = reactive.value(False)
    comparison_selected_member_v = reactive.value(None)
    comparison_inspections_v = reactive.value({})

    _autosave: dict = {"restoring": False}   # suppresses the autosave effect during restores

    current_step = reactive.value(STEP_REACH)
    # ---- layer-tree selection (the tree is the navigation; current_step stays the machinery
    # key). sel_node drives current_step through a GUARDED effect — never a reactive.calc: a
    # calc would invalidate every step-keyed effect on each same-group tree click (bnd.up →
    # bnd.left), churning the DrawControl; the guard fires dependents only on real group
    # crossings, matching the stepper's cadence exactly. ----
    sel_node = reactive.value(None)        # selected tree node id ("bnd.left", …) or None;
    #                                        starts empty — the props card shows Get started
    sel_src = reactive.value("tree")       # who selected last: "tree" | "map" (zoom gating)
    view_mode_v = reactive.value("2d")     # canvas: "2d" (leaflet) | "3d" (vtk scene)

    # Auto-chain bookkeeping (reach → terrain → boundaries). The generation counters mark each
    # (re)commit; _chain records the last generation each auto-launch attempted, so a failed
    # attempt never retries itself (the pane button is the retry) — plain dict, like _nav_seen.
    reach_gen = reactive.value(0)
    dem_gen = reactive.value(0)
    _chain: dict = {"dem": None, "bnd": None}

    # Selecting a tree node never switches the 2-D/3-D canvas (2026-07-17: the gw.mesh
    # auto-flip was removed at the user's request) — only the header toggle and an explicit
    # "Build grid" completion change the view.

    @reactive.effect
    def _step_from_sel():
        tgt = ui_tree.node_step(sel_node())          # None → basemaps etc.: leave the step
        if tgt is None:
            return
        with reactive.isolate():
            if current_step() != tgt:
                current_step.set(tgt)

    def _select(nid, src="tree"):
        """Programmatic tree selection (run-flow jumps, prerequisite links, map picks)."""
        sel_src.set(src)
        sel_node.set(nid)
        step = ui_tree.node_step(nid)
        if step is not None:
            with reactive.isolate():
                if current_step() != step:
                    current_step.set(step)

    # Four named boundary lines (4326) that close into the domain (the domain is DERIVED from them
    # via geometry.assemble_domain_from_sides — see the domain_feat calc below).
    up_feat = reactive.value(None)         # Upstream boundary LineString Feature
    left_feat = reactive.value(None)       # Left FPL boundary LineString Feature
    right_feat = reactive.value(None)      # Right FPL boundary LineString Feature
    down_feat = reactive.value(None)       # Downstream boundary LineString Feature
    bnd_slot = reactive.value(None)        # boundary being drawn/edited: up|left|right|down|wse|None
    bnd_commit = reactive.value(0)         # ++ to ask the client to Save the active edit (legend Save)
    kz_adding = reactive.value(False)      # True while a guided "Add K-zone" polygon draw is armed
    grad_pts = reactive.value([])          # intermediate gradient points on the L/R boundary lines:
    #                                        [{"id","side","station","gradient"[,"lower","upper"]}]
    #                                        (stations 0/1 = the corner numerics; project-state key)
    grad_ver = reactive.value(0)           # ++ on in-place gradient edits (heads recompute without
    #                                        remounting the per-point numeric being typed in)
    grad_adding = reactive.value(None)     # None|"left"|"right" — armed "add gradient point" click
    obs_wells = reactive.value([])         # observation wells (field data; project-state key):
    #                                        [{"id","name","lat","lon","screen_elev","obs_head"}]
    #                                        records mutate IN PLACE via _wells_mirror + wells_ver
    well_pairs = reactive.value([])        # tracked head-gradient pairs [{"id","a","b"}] (well ids;
    #                                        project-state key)
    wells_ver = reactive.value(0)          # ++ on in-place well edits (samples recompute without
    #                                        remounting the input being typed in)
    wells_adding = reactive.value(False)   # True while an "Add well on map" click is armed
    map_layers = reactive.value([])        # user reference layers (path POINTERS, never copies;
    #                                        project-state key): [{"id","path","name","kind",
    #                                        "opacity","color","visible"}] — records mutate IN
    #                                        PLACE for opacity/color/visible via map_layers_ver
    map_layers_ver = reactive.value(0)     # ++ on in-place record edits (style updates apply
    #                                        without remounting the slider being dragged)
    mesh_geom = reactive.value(None)       # last computed 3D mesh geometry (for status + viewer)
    kzone_feats = reactive.value([])       # list of GeoJSON polygon features (4326)
    wse_extent_feat = reactive.value(None)  # drawn water-surface (wetted) extent polygon (4326)
    wse_mode_v = reactive.value("model")    # mirror of the WSE-mode radio; persists across steps
    #                                         (model-first: the HEC-RAS surface run is the default
    #                                          water surface; draw/upload are the fallbacks)
    delineate_mode = reactive.value("auto")  # "auto" (pick 2 NHD points) | "manual" (draw)
    pick_pts = reactive.value([])           # snapped points: [{lat,lon,comid,dist_ft}, ...]
    reach_feat = reactive.value(None)       # traced reach LineString Feature (4326)
    reach_edit = reactive.value(False)      # True while the centerline is loaded for vertex editing
    auto_meta = reactive.value(None)        # {da_sqkm, length_m, bankfull_depth_m, division, ...}
    last_click = reactive.value(None)       # (lat, lon) from Map.on_interaction
    nhd_status = reactive.value("")         # NHD-streams loading/status message
    _flow = {"gdf": None}                   # cached NHD flowlines GDF (for snapping)
    proj_crs = reactive.value(None)
    dem_path = reactive.value(None)
    dem_meta = reactive.value(None)        # {"resolution_m", "source", "src_name"} of the working DEM
    dem_src = reactive.value(dem.normalize_dem_source(None))  # terrain source: 3DEP download or a
    #                                        local-GeoTIFF POINTER {"mode", "path", "src_mtime"}
    carve_active = reactive.value(False)   # a carved channel is applied to the terrain
    carve_meta = reactive.value(None)      # {path, diff_path, cells_cut, max_cut_m}
    _stale_marks = reactive.value(frozenset())  # {"sw","gw"} whose results predate a carve/revert
    origin_override = reactive.value(None)  # user-set Model Origin (streambed elev, m); None = computed default
    ref_slope_override = reactive.value(None)  # user-set Reference slope (m/m); None = track auto

    @reactive.calc
    def active_dem():
        """THE terrain every consumer reads (engine payloads, wetted-extent clips, hillshade,
        3-D surface): the carved raster while a channel modification is applied, else the
        fetched DEM. Carving swaps ALL downstream terrain in one place."""
        if carve_active():
            m = carve_meta()
            if m and m.get("path") and Path(m["path"]).exists():
                return m["path"]
        return dem_path()
    dem_hs_v = reactive.value(8.0)         # hillshade strength (vertical exaggeration; 0 = flat tint)
    dem_opacity_v = reactive.value(0.8)    # DEM overlay opacity while on the DEM step
    dem_stretch_v = reactive.value(None)   # (vmin, vmax) color stretch, or None = full-raster 2-98%
    dem_lohi_v = reactive.value(None)      # effective (vmin, vmax) of the rendered overlay (legend)
    _dem_shade_sig: dict = {}              # last-rendered (path, hs, stretch) — skip no-op renders
    run_result = reactive.value(None)
    input_snapshot = reactive.value(None)   # frozen AssessmentInputSnapshot dict for the active run
    flow_lookup = reactive.value(None)      # last USGS FlowLookupSnapshot (dict) from the flow modal
    flow_source = reactive.value(None)      # {"source","candidate_id","inserted_cfs"} provenance
    soil_snapshot = reactive.value(None)    # NRCS SoilDataSnapshot (dict) from the soils fetch
    soil_overrides = reactive.value([])     # list[SoilOverride dict] applied by the analyst
    results_model = reactive.value(None)    # AssessmentResultsV2 (dict) — the canonical report model
    report_paths = reactive.value(None)     # {format: path} of the last generated report
    _report_stamp = reactive.value(None)    # _report_signature() at the last build (staleness)
    _report_shown_for = reactive.value(None)  # input_hash of the run whose report auto-opened
    # Monotonic build counter. A superseded worker (the user re-opened after an edit while the
    # first build was still in its scenario loop) must not land its older documents on top of a
    # newer build's, so `_report_done` drops any result whose id is not the latest launched.
    _report_build_id = reactive.value(0)
    gms_status_v = reactive.value(None)     # last GMS folder export: None | {"ok": bool, ...}
    head_tifs = reactive.value([])          # per-layer head GeoTIFF paths (index 0 = top layer)
    head_rng = reactive.value(None)         # global (vmin, vmax) for consistent head coloring
    head_layer_v = reactive.value(1)        # persisted slider state (survives pane re-renders)
    head_opacity_v = reactive.value(0.85)   # persisted slider state (survives pane re-renders)
    hd_contours_v = reactive.value(True)    # show contour lines/labels in the head display
    # flow-path particle animation (session-only view state; deliberately NOT in project
    # saves — a freshly opened project starts calm, with the checkbox off)
    fp_anim_on_v = reactive.value(False)
    fp_anim_speed_v = reactive.value(3.0)   # slider 0.5..10; median path loops in 36/v seconds
    fp_anim_color_v = reactive.value(FP_ANIM_COLORS[0])
    fp_anim_style_v = reactive.value("comet")   # "comet" (fading tail) or "dots"
    fp_anim_mode_v = reactive.value("solid")    # FP_ANIM_MODES: swatch color or a rainbow
    # flow-path LINE styling (display prefs; persisted in project saves like head_opacity).
    # Show=False styles the lines to opacity 0 instead of unmounting them, so the animator
    # keeps its geometry and particles stay visible over an invisible network.
    fp_line_show_v = reactive.value(True)
    fp_line_weight_v = reactive.value(1.0)
    fp_line_opacity_v = reactive.value(0.9)
    fp_line_mode_v = reactive.value("class")    # one of FP_LINE_MODES
    # Layer visibility lives in the TREE checkboxes (server-side — ipyleaflet's LayersControl
    # dies under the run-completion layer burst, a client race in jupyter-leaflet 0.20).
    _layer_shadow: dict = {}                # hidden layers parked here so toggles can restore them
    _head_cache: dict = {}                  # layer idx -> overlay payload (avoid re-render)
    _contour_cache: dict = {}               # layer idx -> contour GeoJSON
    _wse_used: dict = {}                    # WSE raster the run consumed (Results overlay fallback)
    stage = reactive.value("")
    log_lines: list[str] = []
    log_tick = reactive.value(0)
    run_t0 = reactive.value(0.0)           # monotonic start of the current run
    elapsed_v = reactive.value(0)          # seconds elapsed (updated by the poller)
    step_v = reactive.value(0)             # current STEP number parsed from the log
    _proc: dict = {"p": None}              # handle to the running child process (for cancel)
    # ---- surface-water (HEC-RAS 2025) model state ----
    ras_result = reactive.value(None)      # dict from ras_engine.run_surface_model (or None)
    wetted_filter_res = reactive.value(None)  # upstream–downstream connectivity split of the
    #   wetted extent: {kept_feat, removed_feat, n_removed, removed_m2, wse_path} from
    #   _wetted_filter_sync; {"failed": True} when no part spans both caps; None = filter off
    ras_log_lines: list[str] = []
    ras_log_tick = reactive.value(0)
    ras_t0 = reactive.value(0.0)
    ras_elapsed = reactive.value(0)
    _ras_proc: dict = {"proc": None}       # live RAS CLI subprocess (for cancel)
    _ras_cancel = threading.Event()
    # Live progress: the worker thread writes this dict (scalar writes, GIL-safe); the
    # 0.5 s poller copies it into the reactives so the UI never touches worker state.
    _ras_prog: dict = {"stage": "", "pct": None, "stage_t0": 0.0}
    ras_stage = reactive.value("")
    ras_pct = reactive.value(None)         # 0-100 within the current stage, or None
    ras_stage_t0 = reactive.value(0.0)     # monotonic start of the current stage (for ETA)
    ras_mesh_prev = reactive.value(None)   # dict from ras_engine.build_mesh_preview (or None)
    _mesh_proc: dict = {"proc": None}      # RAS mesh-preview subprocess (independent of the run)
    _mesh_auto = {"on": False}             # current mesh_prev_task launched by _ras_done, not
    #                                        the button — its done-handler stays quiet
    _ras_mesh_payload: dict = {}           # launch-time snapshot of the run's mesh inputs, so
    #                                        _ras_done can auto-mesh without reactive reads
    _mesh3d_proc: dict = {"p": None}       # 3-D grid-preview child process (Mesh step; cancellable)
    _grid_auto = {"on": False}             # current mesh_task launched by _run_done, not the
    #                                        button — store + push the grid, but never force 3-D
    _ras_overlays: dict = {}               # "depth"/"wse" -> ImageOverlay payloads (big data URIs)
    ras_opacity_v = reactive.value(0.7)    # shared opacity for the surface-result rasters
    # ---- hyporheic-zone delineation (post-run analysis; spawned-child task family) ----
    hz_result = reactive.value(None)       # {"hz_dir", "stats"} from hz_run.child_run (or None)
    hz_log_lines: list[str] = []
    hz_log_tick = reactive.value(0)
    hz_t0 = reactive.value(0.0)
    hz_elapsed = reactive.value(0)
    hz_step_v = reactive.value(0)          # current HZ STEP parsed from the log
    _hz_proc: dict = {"p": None}           # spawned child (for cancel)
    _gms_proc: dict = {"p": None}          # spawned GMS-refresh child (for cancel/sweep)
    _pick_proc: dict = {"p": None}         # spawned tk-dialog child (for kill on reset/end)
    hz_sel_pids = reactive.value(())       # selected classed-path particleids (globally unique)
    hz_gdf = reactive.value(None)          # 4326 classed display gdf (particleid + hz_class)

    def _on_ras_progress(stage: str, pct):
        # Called from the RAS worker thread on every stage change / percent tick.
        if stage != _ras_prog["stage"]:
            _ras_prog["stage"] = stage
            _ras_prog["stage_t0"] = time.monotonic()
            _ras_prog["pct"] = None
        if pct is not None:
            _ras_prog["pct"] = pct

    def _terminate_child():
        p = _proc.get("p")
        if p is not None:
            try:
                if p.is_alive():
                    p.terminate()
            except Exception:  # noqa: BLE001
                pass

    def _kill_ras_proc():
        _ras_cancel.set()
        p = _ras_proc.get("proc")
        if p is not None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass

    def _on_session_end():
        _terminate_child()
        _kill_ras_proc()
        # A tk file dialog left open must die with the session, not linger to server exit.
        for h in (_mesh3d_proc, _pick_proc):
            p = h.get("p")
            if p is not None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        # Teardown reads the plain _ws mirror, never a reactive. A project folder is the
        # user's data: parting-save the settings, drop only the transient 3-D drape, and
        # leave everything else in place. Temp sessions are wiped exactly as before.
        if _ws["project_file"]:
            try:
                _save_project_file()
            except Exception:  # noqa: BLE001
                pass
            shutil.rmtree(work_dir / "scene", ignore_errors=True)
        else:
            shutil.rmtree(work_dir, ignore_errors=True)

    session.on_ended(_on_session_end)

    def _safe(name, default):
        """Read an input that may be hidden (conditional panel) or unmounted (another step);
        return `default` if it never received a value — avoids Shiny's SilentException
        silently halting the Run handler before the task launches.

        _KEEP_IDS parameters have one more wrinkle: after Open (project restore) the input
        registry still holds the PREVIOUS session's values until each pane remounts, so a
        registry value whose last real change predates the restore is stale — the restored
        _kept value wins. The input is still read first so reactive callers (params, the
        estimates) keep their subscription either way."""
        try:
            v = input[name]()
        except Exception:  # noqa: BLE001
            v = None
        if name in _KEEP_SET:
            stale = _kept_ts.get(name, 0.0) <= _restore_stamp.get("t", -1.0)
            if v is None or (stale and name in _kept):
                return _kept.get(name, default)
            return v
        return default if v is None else v

    _layers: dict = {}
    _draw_ctl: dict = {}                     # holds the DrawControl so effects can clear it
    _bnd_shown: dict = {}                    # Boundaries step: per-layer signature (see _bnd_show)
    _map_ui: dict = {}                       # small map-view bookkeeping (last step seen, …)
    _MISSING = object()                      # sentinel: "layer not tracked yet" vs "tracked as None"
    # Task done-handlers apply a finished result exactly once: armed at launch, consumed by the
    # first completion firing, cleared by _reset_session_state. Their bodies read other reactives
    # (layer prefs, domain, CRS), so without the guard any of those writes — project restore in
    # particular — re-fires the handler and grafts the stale task result onto the fresh session.
    _task_armed = {"sw": False, "gw": False, "hz": False, "alt": False, "report": False,
                   "video": False, "video3d": False, "still": False,
                   "gms": False, "pick": False, "example": False}
    _gms_pending: dict = {}    # one-slot newest-wins payload while a GMS build is in flight
    _gms_epoch = {"n": 0}      # bumped by sweeps/resets; stale builds are undone post-hoc
    _gms_flight: dict = {}     # {"epoch", "work_dir"} of the GMS build in flight

    _hidden_keys: set = set()      # layer keys the user unchecked in the tree (the vis funnel)
    _group_hold: dict = {}         # id(LayerGroup) -> stashed children while the group is hidden

    def _visible_hide_works(obj):
        # jupyter-leaflet (this build) honors visible=False for raster/image (→opacity 0) and marker
        # (→removed) layers, but it is a NO-OP for GeoJSON vectors — a hidden GeoJSON stays drawn
        # (confirmed live: reach, HZ paths/footprints, head contour line). Vectors hide by REMOVAL.
        return hasattr(obj, "visible") and not isinstance(obj, GeoJSON)

    def _set_layer(key, layer):
        # THE single funnel every layer owner goes through — which is what makes user-hidden
        # state survive owner re-renders: a hidden key's incoming layer either gets its
        # `visible` trait cleared (widgets that support it — the client toggles the EXISTING
        # leaflet object, no widget-view lifecycle, immune to the add/remove race) or is
        # parked in _layer_shadow (LayerGroups). _set_layer(key, None) clears live AND shadow
        # (ownership transitions can't resurrect stale shadows). Re-setting the SAME object
        # is a no-op — never churn the client for nothing.
        old = _layers.get(key)
        if old is layer and layer is not None:
            if key in _hidden_keys:
                if _visible_hide_works(layer):
                    try:
                        layer.visible = False
                    except Exception:  # noqa: BLE001
                        pass
                else:                      # GeoJSON / vector groups: park — returning here would leave
                    _layer_shadow[key] = layer     # the object mapped despite the hidden key
                    try:
                        _MAP.remove(layer)
                    except Exception:  # noqa: BLE001
                        pass
                    _layers[key] = None
            return
        if old is not None:
            try:
                _MAP.remove(old)
            except Exception:  # noqa: BLE001
                pass
        _layer_shadow.pop(key, None)
        if layer is not None and key in _hidden_keys:
            # NEVER add a hidden widget. ipyleaflet 0.20 gives every layer a `visible`
            # trait, but the jupyter-leaflet client only honors trait CHANGES — a widget
            # ADDED with visible=False renders anyway (root cause of the ghost flow paths:
            # relayer clones and fresh delineation layers for unchecked keys all showed).
            # Park it; _set_keys_visible(True) re-adds it when the key is shown.
            _layer_shadow[key] = layer
            _layers[key] = None
            return
        if layer is not None:
            _MAP.add(layer)
        _layers[key] = layer

    def _group_children_visible(grp, on):
        """Show/hide a LayerGroup by emptying/restoring its OWN `layers` tuple — a group-INTERNAL
        mutation (never a `_MAP` add/remove, which would rebuild sibling GeoJSON views into ghosts).
        Per-child `visible` toggling can't hide a GeoJSON child (visible=False is a no-op for vectors
        in this build — the head contour line lingered), so drop ALL children when hidden and put the
        same objects back when shown; the stash is keyed by id(grp) in `_group_hold`."""
        key = id(grp)
        try:
            if on:
                held = _group_hold.pop(key, None)
                if held is not None and not (getattr(grp, "layers", None) or ()):
                    grp.layers = held
            else:
                if getattr(grp, "layers", None):
                    _group_hold[key] = tuple(grp.layers)
                    grp.layers = ()
        except Exception:  # noqa: BLE001
            pass

    def _clone_vector(lyr):
        """Fresh widget (new model id) for a GeoJSON. Re-adding the SAME object can race the
        client's view teardown — or hit a ZOMBIE view: when the original removal message was
        dropped client-side and a sweep force-removed the leaflet layer, the stale view object
        survives in jupyter-leaflet's registry and a re-add of the same model renders nothing
        (observed live 2026-07-07: re-checking Flow paths after a group-uncheck stayed empty).
        A brand-new widget always materializes. Non-GeoJSON objects return unchanged."""
        if not _HAS_MAP or not isinstance(lyr, GeoJSON):
            return lyr
        try:
            fresh = GeoJSON(data=lyr.data, style=dict(lyr.style or {}),
                            hover_style=dict(getattr(lyr, "hover_style", {}) or {}),
                            point_style=dict(getattr(lyr, "point_style", {}) or {}),
                            name=getattr(lyr, "name", "") or "")
            # Carry the pane placement or the clone jumps to overlayPane above app layers.
            # The "pane" entry in the OPTIONS list (not the pane trait alone) is what reaches
            # the L.geoJson constructor and therefore the child paths — see _sync_map_layers.
            pane = getattr(lyr, "pane", "") or ""
            if pane:
                fresh.pane = pane
                fresh.options = list(getattr(lyr, "options", []) or [])
            cbs = getattr(getattr(lyr, "_click_callbacks", None), "callbacks", [])
            for cb in list(cbs):
                fresh.on_click(cb)
            return fresh
        except Exception:  # noqa: BLE001
            return lyr

    def _clone_layer(lyr):
        """Fresh widget (new model id) for ANY map layer — the wedge heal's workhorse
        (_widget_heal). Same rationale as _clone_vector, per class; a LayerGroup's children
        are cloned recursively (the wedge kills their models too), rescuing a hidden group's
        stashed children out of _group_hold (its clone gets a new id, so the old stash would
        otherwise strand them). Unknown classes return unchanged — better one stale layer
        than a crashed heal."""
        if not _HAS_MAP or lyr is None:
            return lyr
        if isinstance(lyr, GeoJSON):          # GeoJSON IS-A LayerGroup — test it FIRST
            return _clone_vector(lyr)
        if isinstance(lyr, ImageOverlay):
            try:
                return ImageOverlay(url=lyr.url, bounds=lyr.bounds,
                                    opacity=float(getattr(lyr, "opacity", 1.0)),
                                    visible=bool(getattr(lyr, "visible", True)),
                                    pane=getattr(lyr, "pane", "") or "",
                                    name=getattr(lyr, "name", "") or "")
            except Exception:  # noqa: BLE001
                return lyr
        if isinstance(lyr, Marker):
            try:
                kw = {"location": tuple(lyr.location),
                      "draggable": bool(getattr(lyr, "draggable", False)),
                      "visible": bool(getattr(lyr, "visible", True)),
                      "name": getattr(lyr, "name", "") or ""}
                ic = getattr(lyr, "icon", None)
                if isinstance(ic, DivIcon):    # label pills / rotated glyphs live in the html
                    kw["icon"] = DivIcon(html=ic.html,
                                         icon_size=list(ic.icon_size or []) or None,
                                         icon_anchor=list(ic.icon_anchor or []) or None)
                return Marker(**kw)
            except Exception:  # noqa: BLE001
                return lyr
        if isinstance(lyr, LayerGroup):
            try:
                kids = tuple(getattr(lyr, "layers", ()) or ())
                if not kids:                   # unchecked group: children live in the stash
                    kids = tuple(_group_hold.pop(id(lyr), ()) or ())
                return LayerGroup(layers=tuple(_clone_layer(c) for c in kids),
                                  name=getattr(lyr, "name", "") or "")
            except Exception:  # noqa: BLE001
                return lyr
        return lyr

    def _tag_hz(gj, key):
        """Stamp every feature with its layer key — the client-side sweep (hype_map_sweep,
        map_bounds.js) identifies leaflet groups by this property."""
        for f in (gj or {}).get("features", ()):
            f.setdefault("properties", {})["hz_lyr"] = key
        return gj

    async def _sweep_hz(keys):
        """Client-side heal for jupyter-leaflet's dropped-REMOVE flakiness: bursty layer
        removals (re-delineate clear, group unchecks) sometimes never reach the leaflet pane,
        leaving orphaned vector groups that no server-side action can touch — diagnosed live
        2026-07-07 (full gaining+throughflow generations stuck visible, server state clean).
        Asks the client to drop any group whose features are tagged with one of these keys;
        always send BEFORE re-adding fresh widgets so the sweep can't eat the new views."""
        keys = [k for k in keys if k]
        if keys and _HAS_MAP:
            try:
                await session.send_custom_message("hype_map_sweep", {"keys": keys})
            except Exception:  # noqa: BLE001
                pass

    _head_img: dict = {}                     # live ImageOverlay inside the head group (opacity)

    def _render_head_layer(idx: int):
        """Draw the hydraulic-head display for layer `idx` (1 = top), cached: color overlay +
        contour lines + labels, grouped so the layers control shows ONE toggleable entry
        (contours always belong to the layer the slider selects — no separate checkbox)."""
        tifs = head_tifs(); rng = head_rng()
        if not tifs or rng is None:
            return
        idx = max(1, min(int(idx), len(tifs))); k = idx - 1
        if k not in _head_cache:
            _head_cache[k] = results.raster_overlay(tifs[k], vmin=rng[0], vmax=rng[1])
        ov = _head_cache[k]
        with reactive.isolate():                 # current opacity, without subscribing this caller
            op = float(head_opacity_v())
        img = ImageOverlay(url=ov["url"], bounds=ov["bounds"], opacity=op)
        if k not in _contour_cache:
            import numpy as _np
            levels = list(_np.linspace(rng[0], rng[1], 9))[1:-1]   # ~7 interior levels
            gj = results.head_contours_geojson(tifs[k], levels=levels)
            _contour_cache[k] = (gj, results.head_contour_labels(gj))
        gj, labels = _contour_cache[k]
        with reactive.isolate():
            want_contours = bool(hd_contours_v())
        parts = [img]
        if gj and want_contours:
            parts.append(GeoJSON(data=gj, style=CONTOUR_STYLE))
            parts += [Marker(location=(la, lo), draggable=False, icon=DivIcon(
                         html=("<div style=\"font:600 11px/1 system-ui,sans-serif;color:#11161c;"
                               "white-space:nowrap;text-shadow:0 0 2px #fff,0 0 2px #fff,"
                               f"0 0 2px #fff\">{txt}</div>"), icon_anchor=[14, 8]))
                      for la, lo, txt in labels]
        _head_img["lyr"] = img
        # NEVER remove+re-add the head group: a _MAP layer removal makes jupyter-leaflet rebuild the
        # sibling GeoJSON views, which re-add ignoring visible=False (ghost flow paths). Mutate the
        # EXISTING group's children in place (a group-internal change, not a _MAP change) and honor the
        # current toggle state on the fresh children. Only the first creation touches _MAP.
        grp = _layers.get("head")
        if isinstance(grp, LayerGroup):
            grp.layers = tuple(parts)
            grp.name = f"Hydraulic head — L{idx}"
            _group_children_visible(grp, "head" not in _hidden_keys)
        else:
            _set_layer("head", LayerGroup(layers=parts, name=f"Hydraulic head — L{idx}"))
            if _layers.get("wse_raster") is not None:  # keep the WSE raster above the fresh group
                _set_layer("wse_raster", _layers.get("wse_raster"))

    @reactive.calc
    def _domain_build():
        """Assemble the domain (+ normalized left/right upstream→downstream) from the four boundary
        lines, or None until all four exist / if they can't close into a valid ring."""
        return geometry.assemble_domain_from_sides(up_feat(), left_feat(), right_feat(), down_feat())

    @reactive.calc
    def domain_feat():
        """The domain polygon Feature, DERIVED from the four boundary lines (None until buildable)."""
        b = _domain_build()
        return b["domain"] if b else None

    @reactive.calc
    def bnd_conflicts():
        """Blocking boundary-vs-centerline overlaps ({slot,label,msg} per offending side).
        Non-empty gates the surface run + mesh preview; the Boundaries pane shows the details."""
        return geometry.centerline_conflicts(reach_feat(), up_feat(), left_feat(),
                                             right_feat(), down_feat())

    @reactive.calc
    def streambed_elevs():
        """Min (thalweg) elevation of the CARVED terrain along the upstream and downstream boundary
        caps → {"up", "down"} in metres, or None until the boundaries + terrain exist. The upstream
        value is the default Model Origin (streambed elevation); the downstream value is surfaced so
        the user can confirm the model reaches below it."""
        build = _domain_build()
        dem_p = active_dem()
        if not build or not dem_p:
            return None
        try:
            up = delineate.min_elevation_along_line(build["up"], dem_p)
            down = delineate.min_elevation_along_line(build["down"], dem_p)
        except Exception as e:  # noqa: BLE001 — a raster hiccup must degrade, never raise:
            print(f"[elev] streambed sampling failed: {e}")   # a calc error propagates into
            return None                                       # effects and KILLS the session
        if up is None and down is None:
            return None
        return {"up": up, "down": down}

    @reactive.calc
    def origin_default():
        """Default Model Origin = the upstream streambed elevation (None until sampleable)."""
        se = streambed_elevs()
        return se.get("up") if se else None

    @reactive.calc
    def model_origin_effective():
        """The Model Origin actually fed to BOTH the 3-D preview and the run: the user's override
        when set, else the computed upstream-streambed default (None until available)."""
        ov = origin_override()
        return ov if ov is not None else origin_default()

    @reactive.effect
    @reactive.event(input.model_origin, ignore_init=True)
    def _capture_model_origin():
        """Persist a user edit to the Model Origin. A value equal to the computed default (the
        programmatic prefill, or the user typing the default back) clears the override so it tracks
        the streambed again; anything else sticks until New run / new reach."""
        try:
            v = float(input.model_origin())
        except Exception:  # noqa: BLE001 — input absent until the Model grid pane mounts
            return
        with reactive.isolate():
            d = origin_default()
        if d is not None and abs(v - float(d)) < 1e-6:
            origin_override.set(None)
        else:
            origin_override.set(v)

    @reactive.effect
    def _reset_origin_on_reach_cleared():
        # New run / clear / redraw all null the reach → drop any stale streambed/reference-slope
        # override so the defaults recompute for the new reach. (An in-place boundary edit keeps
        # the reach, and the overrides, per "persist until the app is closed or a new run".)
        if reach_feat() is None:
            origin_override.set(None)
            ref_slope_override.set(None)

    # ---- reference slope: centerline-length method + manual override (revision 2026-07) ----
    @reactive.calc
    def wse_preview_path():
        """The WSE raster previews sample (slopes + gradient-point heads). _wse_path() COPIES
        uploads and WRITES the wetted-extent clip as side effects, so it must only run inside
        this cached calc — recomputing only when its real inputs change (idempotent paths)."""
        try:
            return _wse_path()
        except Exception:  # noqa: BLE001
            return None

    @reactive.calc
    def reach_len_live_m():
        """Length (m) of the LIVE drawn centerline. The slope denominators must track the current
        line, so no snapshot fallback here (unlike _reach_length_m, which prefers the frozen run)."""
        try:
            crs = proj_crs()
            gdf = geometry.single_feature_gdf(reach_feat())
            if crs is None or gdf is None or not len(gdf):
                return None
            length = float(gdf.to_crs(crs).length.iloc[0])
            return length if length > 0 else None
        except Exception:  # noqa: BLE001
            return None

    @reactive.calc
    def dem_slope_centerline():
        """Average DEM slope along the reach: (min terrain touching the upstream cap − touching
        the downstream cap) / drawn centerline length."""
        from hype_app import gradients as grad_mod
        se, length = streambed_elevs(), reach_len_live_m()
        if not se or length is None or se.get("up") is None or se.get("down") is None:
            return None
        return grad_mod.reference_slope_from_samples(
            se["up"], se["down"], length, source="dem_drop",
            method="min DEM at caps / centerline length")

    @reactive.calc
    def wse_cap_elevs():
        """Min WSE-raster value touching each boundary cap → {"up","down"} in m, or None. Sampled
        at half-pixel density (the cap-anchor sampler) — the wetted crossing at a cap can be a
        pixel or two wide, and a fixed-count sampler steps right over it (observed: a 2-px
        downstream crossing left the water-surface slope reading n/a on every RAS run)."""
        from hype_app import wse_index
        build, p = _domain_build(), wse_preview_path()
        if not build or not p:
            return None
        try:
            out = {}
            for k in ("up", "down"):
                raw = wse_index.valid_samples_along_line(p, build[k])
                if raw is None:
                    return None
                out[k] = float(raw["value"].min())
            return out
        except Exception as e:  # noqa: BLE001 — same soft-degrade rule as streambed_elevs
            print(f"[elev] WSE cap sampling failed: {e}")
            return None

    @reactive.calc
    def wse_slope_centerline():
        """Water-surface slope along the reach: (WSE touching the upstream cap − touching the
        downstream cap) / drawn centerline length."""
        from hype_app import gradients as grad_mod
        we, length = wse_cap_elevs(), reach_len_live_m()
        if not we or length is None:
            return None
        return grad_mod.reference_slope_from_samples(
            we["up"], we["down"], length, source="wse_raster",
            method="min WSE at caps / centerline length")

    @reactive.calc
    def ref_slope_auto():
        """Auto reference slope: the water-surface slope when a WSE exists, else the DEM slope
        (§7.4 priority, both on the centerline-length method). None = flat/adverse/nothing yet."""
        return wse_slope_centerline() or dem_slope_centerline()

    @reactive.effect
    @reactive.event(input.g_ref_slope, ignore_init=True)
    def _capture_ref_slope():
        """Persist a user edit to the Reference slope (the model_origin pattern). A value equal to
        the auto slope — the programmatic prefill, or the user typing it back — clears the override
        so the field tracks auto again; comparisons use the same round(…, 6) the updates send."""
        try:
            v = float(input.g_ref_slope())
        except Exception:  # noqa: BLE001 — input absent until the pane mounts
            return
        with reactive.isolate():
            a = ref_slope_auto()
            cur = ref_slope_override()
        if a is not None and abs(v - round(a.value, 6)) < 1e-12:
            if cur is not None:
                ref_slope_override.set(None)
        elif v > 0 and cur != v:
            ref_slope_override.set(v)

    @reactive.effect
    def _track_ref_slope_auto():
        # Keep the numeric showing the LIVE auto value while no override is set (no-op when the
        # pane is unmounted). Sends round(…, 6) so _capture_ref_slope recognizes it as auto.
        # try/except is load-bearing: an error raised out of an EFFECT destroys the whole
        # session (the 2026-07-16 terrain-race crash escaped through exactly this effect).
        try:
            a = ref_slope_auto()
            if a is not None and ref_slope_override() is None:
                ui.update_numeric("g_ref_slope", value=round(a.value, 6))
        except Exception as e:  # noqa: BLE001
            print(f"[slope] auto tracking skipped: {type(e).__name__}: {e}")

    @reactive.effect
    @reactive.event(input.g_ref_auto_evt)
    def _reset_ref_slope():
        ref_slope_override.set(None)
        a = ref_slope_auto()
        if a is not None:
            ui.update_numeric("g_ref_slope", value=round(a.value, 6))

    def _domain_gdf_4326():
        f = domain_feat()
        return geometry.single_feature_gdf(f) if f else None

    @reactive.effect
    def _set_proj_crs():
        g = _domain_gdf_4326()
        proj_crs.set(g.estimate_utm_crs() if g is not None else None)

    # wse_mode_v stays pinned to "model": the draw/upload water-surface paths were removed from
    # the UI (no radio to sync), but the mode-guarded consumers remain so old saves stay inert.

    @reactive.effect
    def _sync_delineate_mode():
        try:
            v = input.delineate_mode()
        except Exception:  # noqa: BLE001
            return
        if v:
            delineate_mode.set(v)

    def _fc(feat):
        return feat if (feat or {}).get("type") == "FeatureCollection" else {
            "type": "FeatureCollection", "features": [feat]}

    _LBL_KEYS = {"up": "lbl_up", "left": "lbl_left", "right": "lbl_right", "down": "lbl_down",
                 "wse": "lbl_wse"}
    _MIRROR_NAMES = ("Domain", "Water-surface extent", "Left boundary", "Right boundary",
                     "Upstream boundary", "Downstream boundary", "K-zones", "Reach",
                     *_LBL_KEYS.values())
    # Boundary slot → (map-layer name, style). The active slot lives in the DrawControl; the rest
    # render as static colored layers so all four sides stay visible while you edit one.
    _BND_STATIC = {"up": ("Upstream boundary", UP_STYLE), "left": ("Left boundary", LEFT_STYLE),
                   "right": ("Right boundary", RIGHT_STYLE), "down": ("Downstream boundary", DOWN_STYLE)}

    def _slot_value(slot):
        return {"up": up_feat, "left": left_feat, "right": right_feat, "down": down_feat,
                "wse": wse_extent_feat}.get(slot)

    _mirror_shown: dict = {}    # layer name -> id(feature) shown (identity guard, like _bnd_shown)
    _decor_feat: dict = {}      # decor layer name -> its wanted feature (for data-driven show/hide)

    # Mirrored LINE layers are click-to-select (polygons are excluded — their fills would
    # swallow every click over the domain, the documented interception gotcha).
    _MIRROR_CLICK_NODE = {"Reach": "reach", "Upstream boundary": "bnd.up",
                          "Left boundary": "bnd.left", "Right boundary": "bnd.right",
                          "Downstream boundary": "bnd.down"}

    def _wire_mirror_click(lyr, nm):
        """Idempotently attach the click-to-select handler for a mirrored line. Widgets are
        NEVER-REMOVE (created once, trait-mutated forever), so whichever code path created
        one first must not decide clickability for the whole session — every _decor_show
        touch re-checks. Existing callbacks (incl. relayer copies) count as wired."""
        nid = _MIRROR_CLICK_NODE.get(nm)
        if nid is None:
            return
        try:
            if lyr._click_callbacks.callbacks:
                return
        except Exception:  # noqa: BLE001
            pass
        lyr.on_click(_mk_mirror_click(nid))

    def _mk_mirror_click(nid):
        def _h(**kw):
            with reactive.isolate():        # widget callback: never while a draw/edit/add is live
                if bnd_slot() is not None or kz_adding() or grad_adding() or wells_adding():
                    return
                if current_step() == STEP_REACH and reach_feat() is None:
                    return                  # clicks are point picks / draw vertices until traced
                if sel_node() == nid:
                    return
            _map_ui["map_sel_ts"] = time.monotonic()   # this click selected — mapclear skips it
            sel_src.set("map")              # map pick → tree follows without flying the view
            sel_node.set(nid)
        return _h

    def _decor_show(nm, feat, style):
        """NEVER-REMOVE decorative layer with DATA-DRIVEN visibility. The jupyter-leaflet client
        reliably applies `.data` changes (clearLayers + addData on the EXISTING leaflet object, no
        widget-view lifecycle) — but `visible=False` was NOT clearing these widgets at runtime, so
        toggling a layer off left its line shown. So a decor layer is SHOWN by pushing its feature
        data and HIDDEN by pushing an EMPTY FeatureCollection; the `visible` trait is left True and
        never used to hide. The wanted feature is cached in `_decor_feat` so the checkbox path
        (`_set_keys_visible`) re-renders show/hide by re-calling this with the cached feature. An
        empty `.data` also clears child paths, so a hidden line can't ghost-catch clicks either."""
        _decor_feat[nm] = feat
        hidden = nm in _hidden_keys
        want = _fc(feat) if (feat is not None and not hidden) else _EMPTY_FC
        old = _layers.get(nm)
        if isinstance(old, GeoJSON):
            try:
                if old.data != want:                  # only push on a real change — no churn
                    old.data = want
                _wire_mirror_click(old, nm)           # widget may predate the mirror renderer
                return
            except Exception:  # noqa: BLE001 — fall through to a clean re-create
                pass
        if feat is None or style is None:
            return                                    # style-less call (a vis toggle) with no widget
        # Construct with the REAL geometry, never the hidden-aware `want`: the empty-FC hide
        # belongs to the live mutate path above. A hidden key's widget never reaches the map
        # (_set_layer parks it), and a parked widget must carry its geometry or the un-park
        # clone renders nothing (the invisible-boundaries-after-open bug).
        lyr = GeoJSON(data=_fc(feat), style=style, name=nm, visible=True)
        _wire_mirror_click(lyr, nm)
        _set_layer(nm, lyr)

    def _mirror_show(nm, feat, style):
        """Idempotent (identity-guarded) wrapper over _decor_show for the mirror renderer."""
        sig = id(feat) if feat is not None else None
        if _mirror_shown.get(nm, _MISSING) == sig:
            return
        _mirror_shown[nm] = sig
        _decor_show(nm, feat, style)

    def _mirror_features_as_layers():
        """Show the geometry as named, toggleable, thin static layers (features read isolated).
        The hand-drawn/auto WSE polygon is suppressed whenever the surface MODEL owns the water
        surface — on the Surface step (whose own result layers replace it) and everywhere once
        wse_mode is "model" — so a stale drawn extent can never masquerade as model output."""
        model_owns_wse = wse_mode_v() == "model"     # subscribing read: mode flips re-mirror
        with reactive.isolate():
            dom, wse, lf, rf, uf, df, kz, rch = (
                domain_feat(), wse_extent_feat(), left_feat(), right_feat(),
                up_feat(), down_feat(), list(kzone_feats()), reach_feat())
            if model_owns_wse or current_step() == STEP_SURFACE:
                wse = None
        for nm, feat, style in (("Domain", dom, DOMAIN_STYLE),
                                ("Water-surface extent", wse, WSE_STYLE),
                                ("Left boundary", lf, LEFT_STYLE), ("Right boundary", rf, RIGHT_STYLE),
                                ("Upstream boundary", uf, UP_STYLE),
                                ("Downstream boundary", df, DOWN_STYLE)):
            _mirror_show(nm, feat, style)
        kz_fc = {"type": "FeatureCollection", "features": kz} if kz else None
        if _mirror_shown.get("K-zones", _MISSING) != (tuple(id(f) for f in kz) or None):
            _mirror_shown["K-zones"] = tuple(id(f) for f in kz) or None
            _decor_show("K-zones", kz_fc, KZONE_STYLE)
        _mirror_show("Reach", rch, REACH_STYLE)
        label_sig = (id(uf), id(lf), id(rf), id(df), id(wse) if wse else None)
        if _mirror_shown.get("Boundary labels", _MISSING) != label_sig:
            _mirror_shown["Boundary labels"] = label_sig
            _render_boundary_labels({"up": uf, "left": lf, "right": rf, "down": df}, wse)

    def _hide_key(nm):
        """Hide a layer without removing it (visible trait when supported — the churn-free
        path); non-trait layers (groups) still fall back to removal."""
        lyr = _layers.get(nm)
        if lyr is None:
            return
        if hasattr(lyr, "visible"):
            try:
                lyr.visible = False
                return
            except Exception:  # noqa: BLE001
                pass
        _set_layer(nm, None)

    def _clear_mirror_layers():
        for nm in _MIRROR_NAMES:
            _hide_key(nm)
        _bnd_shown.clear()
        _mirror_shown.clear()

    def _bnd_show(nm, feat, style):
        # Boundaries-step statics: identity-guarded _decor_show (never-remove; selecting a
        # boundary swaps ONE line in/out of the DrawControl with zero other layer churn).
        sig = id(feat) if feat is not None else None
        if _bnd_shown.get(nm, _MISSING) == sig:
            return
        _bnd_shown[nm] = sig
        _decor_show(nm, feat, style)

    _BND_LABELS = {"up": ("Upstream", UP_STYLE["color"]),
                   "left": ("Left floodplain", LEFT_STYLE["color"]),
                   "right": ("Right floodplain", RIGHT_STYLE["color"]),
                   "down": ("Downstream", DOWN_STYLE["color"])}

    def _label_point(feat, polygon=False):
        """A (lat, lon) anchor to place a boundary's label: the polygon's representative point, or the
        LineString's mid-arc point. None if the geometry can't be read."""
        try:
            from shapely.geometry import shape as _shape
            g = _shape((feat or {}).get("geometry") or {})
            if g.is_empty:
                return None
            p = g.representative_point() if polygon else g.interpolate(0.5, normalized=True)
            return (float(p.y), float(p.x))
        except Exception:  # noqa: BLE001
            return None

    def _label_marker(pt, text, color):
        """A non-interactive label pill centred on `pt`. Styling lives in `.hype-map-label`
        (styles.css) — ipyleaflet's DivIcon has no class_name to drop Leaflet's default box, so that
        default is neutralized in CSS and the pill is drawn by our class. `color` drives the text and
        (via currentColor) the border; pointer-events:none so it never intercepts line-select clicks."""
        html = f'<div class="hype-map-label" style="color:{color}">{text}</div>'
        return Marker(location=pt, draggable=False,
                      icon=DivIcon(html=html, icon_size=[0, 0], icon_anchor=[0, 0]))

    def _render_boundary_labels(feats, wse):
        """Per-slot persistent label Markers: created once, then only `location`/`visible`
        trait-mutated (Marker.visible = client setOpacity on the existing view) — labels never
        participate in layer add/remove churn. Their icon wrappers are pointer-events:none via
        CSS so an invisible label can't intercept line clicks."""
        want = {}
        for slot, (text, color) in _BND_LABELS.items():
            pt = _label_point(feats.get(slot)) if feats.get(slot) else None
            if pt:
                want[_LBL_KEYS[slot]] = (pt, text, color)
        wpt = _label_point(wse, polygon=True) if wse else None
        if wpt:
            want["lbl_wse"] = (wpt, "Water surface", WSE_STYLE["color"])
        for key in _LBL_KEYS.values():
            lyr = _layers.get(key)
            spec = want.get(key)
            if spec is None:
                if lyr is not None and getattr(lyr, "visible", False):
                    try:
                        lyr.visible = False
                    except Exception:  # noqa: BLE001
                        pass
                continue
            pt, text, color = spec
            if lyr is not None:
                try:
                    lyr.location = pt
                    if key not in _hidden_keys and not lyr.visible:
                        lyr.visible = True
                    continue
                except Exception:  # noqa: BLE001
                    pass
            _set_layer(key, _label_marker(pt, text, color))

    def _grad_overlay_children(rows):
        """LayerGroup children for the gradient-point overlay: per point a side-colored glyph, a
        dashed shaft to the WSE cell whose value anchors its head (rotated ▲ at the cell), and a
        head-value pill. Rows come from grad_point_heads()."""
        import math
        feats, marks = [], []
        for r in rows:
            color = LEFT_STYLE["color"] if r["side"] == "left" else RIGHT_STYLE["color"]
            plon, plat = r["pt"]
            feats.append({"type": "Feature",
                          "properties": {"hz_lyr": "grad_pts",
                                         "style": {"color": color, "fillColor": color,
                                                   "weight": 2, "fillOpacity": 0.9}},
                          "geometry": {"type": "Point", "coordinates": [plon, plat]}})
            if r.get("edge") is not None:
                elon, elat = r["edge"]
                feats.append({"type": "Feature",
                              "properties": {"hz_lyr": "grad_pts",
                                             "style": {"color": color, "weight": 1.5,
                                                       "opacity": 0.85, "dashArray": "4 3"}},
                              "geometry": {"type": "LineString",
                                           "coordinates": [[plon, plat], [elon, elat]]}})
                # ▲ points north at 0°; rotate(90−θ) aims it along the shaft bearing
                ang = math.degrees(math.atan2(
                    elat - plat, (elon - plon) * math.cos(math.radians(plat))))
                marks.append(Marker(location=(elat, elon), draggable=False, icon=DivIcon(
                    html=(f'<div class="hype-grad-head" style="color:{color};'
                          f'transform:translate(-50%,-50%) rotate({90.0 - ang:.0f}deg)">▲</div>'),
                    icon_size=[0, 0], icon_anchor=[0, 0])))
            txt = "h —" if r.get("head") is None else f"h {r['head']:.2f} m"
            marks.append(_label_marker((plat, plon), txt, color))
        gj = GeoJSON(data={"type": "FeatureCollection", "features": feats},
                     point_style={"radius": 4, "weight": 2, "fillOpacity": 0.9},
                     name="Gradient points")
        return (gj, *marks)

    @reactive.effect
    def _sync_grad_overlay():
        # Gradient overlay (Groundwater hub, BOTH BC modes — qualitative shows the four corner
        # rows): ONE LayerGroup under "grad_pts" added once via _set_layer, then children swapped
        # by assigning grp.layers — group-internal mutation, never bursty map add/removes (the
        # ipyleaflet drop gotcha). Hidden state = empty children. Mode scoping lives inside
        # grad_point_heads; not tree-checkbox wired: node scoping IS the story.
        if not _HAS_MAP:
            return
        on = sel_node() == "gw"
        rows = grad_point_heads() if on else []
        grp = _layers.get("grad_pts")
        if not rows:
            if isinstance(grp, LayerGroup) and grp.layers:
                grp.layers = ()
            return
        kids = _grad_overlay_children(rows)
        if isinstance(grp, LayerGroup):
            grp.layers = kids
        else:
            _set_layer("grad_pts", LayerGroup(layers=kids, name="Gradient points"))

    def _wells_overlay_children(wls):
        """LayerGroup children for the observation-well overlay: one ringed glyph per well
        (per-feature properties.style — never a layer-level style=, which merges over it)
        plus a name pill."""
        feats, marks = [], []
        for w in wls:
            feats.append({"type": "Feature",
                          "properties": {"hz_lyr": "obs_wells",
                                         "style": {"color": "#ffffff", "fillColor": WELL_COLOR,
                                                   "weight": 2, "fillOpacity": 1.0}},
                          "geometry": {"type": "Point",
                                       "coordinates": [float(w["lon"]), float(w["lat"])]}})
            marks.append(_label_marker((w["lat"], w["lon"]), w.get("name") or "well",
                                       WELL_COLOR))
        gj = GeoJSON(data={"type": "FeatureCollection", "features": feats},
                     point_style={"radius": 5, "weight": 2, "fillOpacity": 1.0},
                     name="Observation wells")
        return (gj, *marks)

    @reactive.effect
    def _sync_wells_overlay():
        # Observation-well markers: ONE LayerGroup under "obs_wells", children swapped in
        # place (grad_pts discipline). UNLIKE grad_pts this key IS tree-checkbox wired, so
        # every swap re-honors the current checkbox via _group_children_visible (the
        # head-layer owner's discipline): while hidden, fresh children are re-stashed in
        # _group_hold; re-checking restores them. The empty branch also drops the stash and
        # any parked shadow so deleted wells can never resurrect on the next re-check.
        if not _HAS_MAP:
            return
        wls = obs_wells()
        wells_ver()                                    # live name edits repaint the pills
        grp = _layers.get("obs_wells")
        if not wls:
            if isinstance(grp, LayerGroup):
                _group_hold.pop(id(grp), None)
                if grp.layers:
                    grp.layers = ()
            _layer_shadow.pop("obs_wells", None)
            return
        kids = _wells_overlay_children(wls)
        if isinstance(grp, LayerGroup):
            grp.layers = kids
            _group_children_visible(grp, "obs_wells" not in _hidden_keys)
        else:
            _set_layer("obs_wells", LayerGroup(layers=kids, name="Observation wells"))

    # ---- user reference map layers (Map layers tree group) --------------------------------
    # Path pointers to files on the user's machine; missing/error are DISPLAY states surfaced
    # in the tree row + pane, never grounds for touching the record. Widgets live in the
    # dedicated hype-ref pane (declared in _build_map) so they always stack above the basemap
    # and terrain and below every app overlay, whatever order the heal machinery re-adds.
    _ml_cache: dict = {}     # uid -> {"sig": (path, mtime), "kind", "overlay"|"fc", "bounds"}
    _ml_status: dict = {}    # uid -> "ok" | "missing" | "error"
    _ml_err: dict = {}       # uid -> short user-facing reason (status "error" only)
    _ml_paint = reactive.value(0)   # ++ on status/err transitions (and relink) so the pane
    #                                 rows repaint — NEVER bumped by slider/color edits, which
    #                                 would remount the control being dragged

    def _ml_key(uid) -> str:
        return f"ml:{uid}"

    def _ml_eff(rec) -> bool:
        # Effective visibility = the row's own checkbox AND the Map layers group checkbox
        # (dynamic rows are not in ui_tree.NODES, so _eff_checked can't walk them).
        return bool(rec.get("visible", True)) and _node_checked("maplyr")

    def _apply_ml_vis():
        """Re-apply EFFECTIVE visibility to every reference-layer key. _set_keys_visible
        keeps _hidden_keys in sync, so the _set_layer funnel and the heal machinery keep
        honoring the state without knowing these keys are dynamic."""
        with reactive.isolate():
            recs = list(map_layers())
        for rec in recs:
            _set_keys_visible((_ml_key(rec.get("id")),), _ml_eff(rec))
        _bump_vis()

    def _ml_bounds_union():
        """[[s, w], [n, e]] union of every LOADED layer's bounds (group zoom-to-extent)."""
        bs = [c.get("bounds") for c in _ml_cache.values() if c.get("bounds")]
        if not bs:
            return None
        return [[min(b[0][0] for b in bs), min(b[0][1] for b in bs)],
                [max(b[1][0] for b in bs), max(b[1][1] for b in bs)]]

    @reactive.effect
    async def _sync_map_layers():
        # Owner of every "ml:<uid>" key. Subscribes to the record list (add/remove/relink)
        # and the version counter (in-place opacity/color/visible edits). File loads run
        # off-loop; a records/ver change during the await supersedes this pass (the newer
        # pass redoes the apply), so a stale load can never clobber fresh edits.
        if not _HAS_MAP:
            return
        recs = [dict(r) for r in map_layers()]
        ver = map_layers_ver()
        snap0 = [(r.get("id"), r.get("path"), r.get("kind")) for r in recs]
        paint0 = (dict(_ml_status), dict(_ml_err))
        need = []
        for rec in recs:
            uid = rec["id"]
            p = Path(rec["path"])
            try:
                sig = (rec["path"], p.stat().st_mtime) if p.is_file() else None
            except OSError:
                sig = None
            if sig is None:                       # the linked file is gone: warn, keep the record
                _ml_status[uid] = "missing"
                _ml_err.pop(uid, None)
                _ml_cache.pop(uid, None)
                _set_layer(_ml_key(uid), None)
                continue
            c = _ml_cache.get(uid)
            if c is None or c.get("sig") != sig or c.get("kind") != rec["kind"]:
                need.append((rec, sig))
        if need:
            def _load_batch():
                # Worker-thread matplotlib (colormaps + PNG encode) serializes with the
                # report builds via the shared lock — same rationale as report_task.
                out = {}
                with _REPORT_MPL_LOCK:
                    for rec, sig in need:
                        if rec["kind"] == "raster":
                            ov, err = ml_mod.load_raster_overlay(rec["path"])
                            out[rec["id"]] = {"sig": sig, "kind": "raster", "overlay": ov,
                                              "bounds": (ov or {}).get("bounds"),
                                              "err": err, "simplified": False}
                        else:
                            fc, bounds, err, simplified = ml_mod.load_vector_fc(rec["path"])
                            if fc is not None:
                                _tag_hz(fc, _ml_key(rec["id"]))   # sweep/verify heal opt-in
                            out[rec["id"]] = {"sig": sig, "kind": "vector", "fc": fc,
                                              "bounds": bounds, "err": err,
                                              "simplified": simplified}
                return out
            loaded = await anyio.to_thread.run_sync(_load_batch)
            with reactive.isolate():
                stale = (map_layers_ver() != ver
                         or [(r.get("id"), r.get("path"), r.get("kind"))
                             for r in map_layers()] != snap0)
            if stale:
                return
            for uid, entry in loaded.items():
                err = entry.pop("err", None)
                simplified = entry.pop("simplified", False)
                nm = next((r["name"] for r in recs if r["id"] == uid), "layer")
                if err:
                    _ml_cache.pop(uid, None)
                    _ml_status[uid] = "error"
                    _ml_err[uid] = err
                    _set_layer(_ml_key(uid), None)
                    ui.notification_show(f"Couldn't read {nm}: {err}",
                                         type="warning", duration=8)
                else:
                    _ml_cache[uid] = entry
                    _ml_status[uid] = "ok"
                    _ml_err.pop(uid, None)
                    if simplified:
                        ui.notification_show(f"{nm} was simplified for display.", duration=6)
        # Build or refresh widgets (live prefs mutate traits — no widget churn on a drag).
        # Per-record try/except: a wedged widget comm or one bad record must never kill
        # the owner effect for the session (the mesh3d per-handler discipline).
        for rec in recs:
            uid = rec["id"]
            key = _ml_key(uid)
            c = _ml_cache.get(uid)
            if not c or _ml_status.get(uid) != "ok":
                continue
            live = _layers.get(key) or _layer_shadow.get(key)
            try:
                if rec["kind"] == "raster":
                    ov = c.get("overlay") or {}
                    if isinstance(live, ImageOverlay) and live.url == ov.get("url"):
                        if abs(float(getattr(live, "opacity", 1.0))
                               - float(rec["opacity"])) > 1e-9:
                            live.opacity = float(rec["opacity"])
                    else:
                        _set_layer(key, ImageOverlay(url=ov.get("url"),
                                                     bounds=ov.get("bounds"),
                                                     opacity=float(rec["opacity"]),
                                                     pane=ml_mod.PANE_REF,
                                                     name=rec["name"]))
                else:
                    style = ml_mod.vector_style(rec["color"], rec["opacity"])
                    if isinstance(live, GeoJSON) and live.data is c.get("fc"):
                        if dict(live.style or {}) != style:
                            live.style = style    # client setStyle: restyles children live
                    else:
                        gj = GeoJSON(data=c.get("fc"), style=style,
                                     point_style=ml_mod.vector_point_style(),
                                     pane=ml_mod.PANE_REF, name=rec["name"])
                        # "pane" must ride the OPTIONS list: the client builds L.geoJson's
                        # constructor options from it, which is the only way the pane
                        # reaches the child paths (the pane trait alone applies after
                        # children exist).
                        gj.options = list(gj.options or []) + ["pane"]
                        _set_layer(key, gj)
            except Exception:  # noqa: BLE001
                pass
        # Tear down removed layers everywhere they could hide (live map, shadow park,
        # hidden set, caches) — the wells lesson: miss one stash and deletes resurrect.
        want = {_ml_key(r["id"]) for r in recs}
        for key in {k for k in list(_layers) + list(_layer_shadow)
                    if isinstance(k, str) and k.startswith("ml:")} - want:
            _set_layer(key, None)
            _hidden_keys.discard(key)
        for uid in [u for u in list(_ml_cache) if _ml_key(u) not in want]:
            _ml_cache.pop(uid, None)
        for uid in [u for u in list(_ml_status) if _ml_key(u) not in want]:
            _ml_status.pop(uid, None)
            _ml_err.pop(uid, None)
        _apply_ml_vis()
        if (dict(_ml_status), dict(_ml_err)) != paint0:
            with reactive.isolate():
                _ml_paint.set(_ml_paint() + 1)

    def _render_boundaries(active):
        """Boundaries-step display: each side except the `active` one (which is in the DrawControl)
        as a static colored layer, plus the WSE (unless active) and the reach. Idempotent via
        `_bnd_show`, so re-running on a slot change only touches what actually changed (the active
        line moving in/out of the DrawControl) — never a full clear + re-add. The derived-domain gold
        ring is intentionally NOT drawn here: the four coloured sides already trace the domain, and
        drawing it on top masked their distinct legend colours (worse after edits re-stacked it)."""
        model_owns_wse = wse_mode_v() == "model"     # subscribing read: mode flips re-render
        with reactive.isolate():
            feats = {"up": up_feat(), "left": left_feat(), "right": right_feat(), "down": down_feat()}
            wse = wse_extent_feat(); rch = reach_feat()
            if model_owns_wse:                # the RAS model owns the water surface now
                wse = None
        for slot, (nm, style) in _BND_STATIC.items():
            _bnd_show(nm, feats[slot] if slot != active else None, style)
        _bnd_show("Domain", None, DOMAIN_STYLE)   # clear any domain ring carried in from another step
        _bnd_show("Water-surface extent", wse if active != "wse" else None, WSE_STYLE)
        _bnd_show("Reach", rch, REACH_STYLE)
        _bnd_show("K-zones", None, KZONE_STYLE)
        _render_boundary_labels(feats, wse)
        _mirror_shown.clear()      # labels/layers now owned by the Boundaries renderer; re-mirror fresh

    @reactive.effect
    def _sync_map_shapes():
        # Fires on STEP change (features isolated). Reach/K load their shapes into the DrawControl;
        # Boundaries is driven per-active-slot by _sync_bnd_slot; other steps clear + mirror statics.
        if not _HAS_MAP:
            return
        step = current_step()
        dc = _draw_ctl.get("dc")
        if step != STEP_REACH:                      # the auto-pick markers are Reach-only
            _set_layer("pick1", None); _set_layer("pick2", None)
        with reactive.isolate():
            # reach_edit isolated: this is the step-ENTRY setup — the edit toggle is owned by
            # _sync_reach_edit (a bursty _clear_mirror_layers on every toggle raced the client).
            kz = list(kzone_feats()); rch = reach_feat(); editing = reach_edit()
        if step == STEP_REACH:
            _clear_mirror_layers()
            # Not editing → clickable static (auto OR manual); editing → loaded in the DrawControl
            # for vertex dragging (magenta baked in via _edit_feature's REACH_STYLE default).
            _mirror_show("Reach", rch if (rch and not editing) else None, REACH_STYLE)
            _load_into_drawcontrol([_edit_feature(rch, "reach")] if (rch and editing) else [])
        elif step == STEP_K:
            _clear_mirror_layers()
            _mirror_show("Reach", rch if rch else None, REACH_STYLE)
            # keep the four boundary lines (+ labels) visible for orientation while the
            # K-zones themselves live in the DrawControl for editing
            with reactive.isolate():
                feats = {"up": up_feat(), "left": left_feat(),
                         "right": right_feat(), "down": down_feat()}
            for slot, (nm, style) in _BND_STATIC.items():
                _mirror_show(nm, feats[slot], style)
            _render_boundary_labels(feats, None)
            _load_into_drawcontrol(kz)
        elif step == STEP_BOUNDARIES:
            pass                                   # _sync_bnd_slot owns the Boundaries display
        else:
            if dc is not None:
                try:
                    dc.clear(); dc.data = []
                except Exception:  # noqa: BLE001
                    pass
            _mirror_features_as_layers()

    @reactive.effect
    def _sync_reach_edit():
        # Owns the reach's edit transition on the Reach step — the analog of _sync_bnd_slot for
        # boundaries. Swaps ONLY the reach between its static mirror and the DrawControl edit copy,
        # deliberately WITHOUT _sync_map_shapes' _clear_mirror_layers(): hiding+reshowing every mirror
        # in the same bursty flush as the DrawControl update raced jupyter-leaflet into a duplicate
        # line (the dropped-update flakiness _sweep_hz documents). Fires on the edit toggle only (step
        # isolated). Two gotchas: the auto-trace shows the reach via a direct _decor_show (~L1590) that
        # leaves _mirror_shown["Reach"] stale, so pop the guard or the hide/show short-circuits; and
        # dc.clear() is what actually removes the edit copy (dc.data=[] alone leaves it rendered).
        if not _HAS_MAP:
            return
        editing = reach_edit()
        dc = _draw_ctl.get("dc")
        with reactive.isolate():
            if current_step() != STEP_REACH:
                return
            rch = reach_feat()
        _mirror_shown.pop("Reach", None)                          # defeat the stale trace-time guard
        if rch and editing:
            _mirror_show("Reach", None, REACH_STYLE)               # hide the static (visible=False)
            _load_into_drawcontrol([_edit_feature(rch, "reach")])
        else:
            if dc is not None:
                try:
                    dc.clear(); dc.data = []                       # clear() removes the edit copy
                except Exception:  # noqa: BLE001
                    pass
            _mirror_show("Reach", rch, REACH_STYLE)                # reshow (clean reveal via _decor_show)

    @reactive.effect
    async def _dem_overlay_opacity():
        # The hillshade's visibility belongs to its "DEM (hillshade)" entry in the layers control
        # (a client-side checkbox: checked = shown, on every step, state persists because the
        # layer object is created once at fetch and only trait-mutated afterwards). This effect
        # keeps its opacity in step with the DEM-step slider, and mirrors the same value onto
        # the 3-D terrain surface so the slider is the see-through control in both views.
        opacity = float(dem_opacity_v())
        try:
            await session.send_custom_message(
                "hype3d_style", {"key": "terrain", "opacity": opacity})
        except Exception:  # noqa: BLE001
            pass
        lyr = _layers.get("dem")       # created with the current opacity at fetch time
        if lyr is None:
            return
        try:
            lyr.opacity = opacity
        except Exception:  # noqa: BLE001
            pass

    @reactive.effect
    def _dem_shade_sync():
        # Re-render the DEM hillshade image when its LOOK changes (hillshade strength slider or
        # a recalculated color stretch) — mutating the existing overlay's url trait, never
        # remove+add (the ipyleaflet churn lesson). Signature-guarded so fetch-time creation
        # doesn't trigger a duplicate identical render.
        p = active_dem()                    # a carve re-renders the hillshade automatically
        hs = float(dem_hs_v())
        stretch = dem_stretch_v()
        if not (_HAS_MAP and p):
            return
        lyr = _layers.get("dem")
        if lyr is None:
            return
        sig = (p, hs, stretch)
        if _dem_shade_sig.get("sig") == sig:
            return
        _dem_shade_sig["sig"] = sig
        try:
            vmin, vmax = (stretch if stretch else (None, None))
            ov = dem.dem_overlay(p, vert_exag=hs, vmin=vmin, vmax=vmax)
            lyr.url = ov["url"]
            dem_lohi_v.set((ov["vmin"], ov["vmax"]))
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"DEM render issue: {e}", type="warning", duration=5)

    @reactive.effect
    def _dem_stretch_from_view():
        if not _clicked_dynamic("dem_stretch_btn"):
            return
        # "Recalculate legend based on current view": re-stretch the elevation colors to the
        # terrain visible in the map viewport (classic GIS stretch-to-extent) so subtle relief
        # pops when zoomed into a subarea. Zooming back out + clicking again widens it back.
        p = dem_path()
        if not (p and _HAS_MAP):
            return
        # View bounds come from www/map_bounds.js (input.map_bounds) — ipyleaflet's own
        # `bounds` trait arrives degenerate ((center, center)) in this stack.
        try:
            b = input.map_bounds()
        except Exception:  # noqa: BLE001
            b = None
        if not b or b.get("east") is None or b["east"] <= b["west"]:
            ui.notification_show("Pan or zoom the map once, then try again.", duration=4)
            return
        lohi = dem.stretch_for_bounds(
            p, (float(b["west"]), float(b["south"]), float(b["east"]), float(b["north"])))
        if lohi is None:
            ui.notification_show("No terrain in the current view — pan to the DEM first.",
                                 type="warning", duration=5)
            return
        dem_stretch_v.set(lohi)
        ui.notification_show(f"Legend re-stretched to the view: {lohi[0]:.1f}–{lohi[1]:.1f} m.",
                             duration=4)

    @reactive.effect
    def _mirror_dem_hs():
        try:
            v = input.dem_hs()
        except Exception:  # noqa: BLE001
            return
        if v is not None:
            dem_hs_v.set(float(v))

    @reactive.effect
    def _mirror_dem_opacity():
        try:
            v = input.dem_opacity()
        except Exception:  # noqa: BLE001
            return
        if v is not None:
            dem_opacity_v.set(float(v))


    @reactive.effect
    def _sync_bnd_slot():
        # Owns the Boundaries-step map: load ONLY the active boundary into the DrawControl (so
        # Leaflet.draw never has to disambiguate four similar lines) and mirror the rest as statics.
        # `_render_boundaries` is idempotent, so selecting a slot only swaps the one active line in/
        # out of the DrawControl — it never clears + re-adds every overlay (which blanked the map).
        if not _HAS_MAP:
            return
        step = current_step()
        slot = bnd_slot()
        if step != STEP_BOUNDARIES:
            if slot is not None:
                with reactive.isolate():
                    bnd_slot.set(None)             # reset when leaving so re-entry starts clean
            _bnd_shown.clear()                     # rebuild cleanly on the next entry
            return
        with reactive.isolate():
            sv = _slot_value(slot)
            active_feat = sv() if sv is not None else None
        _load_into_drawcontrol([_edit_feature(active_feat, slot)] if active_feat else [])
        _render_boundaries(slot)

    @reactive.effect
    def _refresh_boundary_display():
        # Re-render the boundary statics + labels whenever ANY feature changes (e.g. "Snap corners
        # together" rewrites all four sides, or a committed edit changes one) — not just the derived
        # Domain outline. Without this, Snap-corners updated the features but the side lines stayed at
        # their old positions on the map. Idempotent via _bnd_show, so unchanged layers aren't touched.
        if not _HAS_MAP or current_step() != STEP_BOUNDARIES:
            return
        up_feat(); left_feat(); right_feat(); down_feat()      # subscribe to every boundary feature so
        wse_extent_feat(); reach_feat(); domain_feat()          # Snap-corners / edits re-render the lines
        with reactive.isolate():
            slot = bnd_slot()
        _render_boundaries(slot)

    # (Removed `_frame_boundaries_on_entry`: landing on the Boundaries step no longer auto-fits the
    # map. Per user request, ONLY the props-pane Zoom-to-extent button (tree "zoom" → hype_fly) moves
    # the view — never a tree selection, step change, or completed operation.)

    def _features_of(gj):
        """Feature dicts from an on_draw `geo_json` payload (Feature / FeatureCollection / bare
        geometry). On an EDIT, ipyleaflet hands the fresh edited geometry here but does NOT update
        dc.data at the same time (that trait syncs via a separate, unordered message), so this is the
        reliable source for a just-committed shape — reading dc.data would re-save the old geometry."""
        if not isinstance(gj, dict):
            return []
        t = gj.get("type")
        if t == "FeatureCollection":
            return [f for f in (gj.get("features") or []) if isinstance(f, dict)]
        if t == "Feature":
            return [gj]
        if gj.get("coordinates") is not None:              # bare geometry
            return [{"type": "Feature", "properties": {}, "geometry": gj}]
        return []

    def _snap_boundary_endpoints(slot, feat):
        """Snap the committed boundary line's two endpoints onto the nearest endpoint of the OTHER
        three boundaries when within a zoom-scaled tolerance (~16 px), so shared corners actually
        meet. Returns the (possibly snapped) Feature — unchanged if there's no projected CRS yet or
        nothing is close. Reuses the px→m metric from _bnd_pick_on_click."""
        geom = (feat or {}).get("geometry") or {}
        if geom.get("type") != "LineString":
            return feat
        coords = [list(c) for c in (geom.get("coordinates") or [])]
        crs = proj_crs()
        if len(coords) < 2 or crs is None:
            return feat
        neighbours = []
        for k, v in {"up": up_feat, "left": left_feat, "right": right_feat, "down": down_feat}.items():
            if k == slot:
                continue
            c = (((v() or {}).get("geometry") or {}).get("coordinates")) or []
            if len(c) >= 2:
                neighbours.append(tuple(c[0][:2])); neighbours.append(tuple(c[-1][:2]))
        if not neighbours:
            return feat
        try:
            import math
            import geopandas as gpd
            from shapely.geometry import Point
            ep_idx = [0, len(coords) - 1]
            lonlat = [tuple(coords[i][:2]) for i in ep_idx] + neighbours
            proj = list(gpd.GeoSeries([Point(lo, la) for lo, la in lonlat], crs=4326).to_crs(crs))
            z = getattr(_MAP, "zoom", None) or 16   # read the trait directly — on_draw isn't reactive,
            mpp = 156543.03 * math.cos(math.radians(float(coords[0][1]))) / (2 ** int(z))  # so _view() (a
            tol = 28.0 * mpp                        # reactive.calc) could raise here and silently no-op
            n = len(ep_idx)
            for j, i in enumerate(ep_idx):
                p = proj[j]
                best_d, best_k = None, None
                for m in range(len(neighbours)):
                    d = p.distance(proj[n + m])
                    if best_d is None or d < best_d:
                        best_d, best_k = d, m
                if best_d is not None and best_d <= tol:
                    coords[i] = list(neighbours[best_k])       # snap endpoint onto the neighbour
            return {"type": "Feature", "properties": (feat or {}).get("properties") or {},
                    "geometry": {"type": "LineString", "coordinates": coords}}
        except Exception:  # noqa: BLE001
            return feat

    def _reclassify_drawn(action=None, geo_json=None):
        """Re-derive feature values from the just-drawn/edited shape, routed by step: Reach (manual) →
        the drawn line is the reach centerline; K → polygons are K-zones; Boundaries → the single
        shape goes to the active boundary slot (up/left/right/down = line, wse = polygon). Prefer the
        fresh `geo_json` from the draw event; dc.data is stale on edits (see _features_of)."""
        dc = _draw_ctl.get("dc")
        data_feats = list(getattr(dc, "data", None) or [])
        fresh = _features_of(geo_json)
        step = current_step()
        if step == STEP_REACH:
            src = fresh or data_feats
            lines = [f for f in src if (f.get("geometry") or {}).get("type") == "LineString"]
            if lines:                               # manual draw OR an edit (auto or manual) → save it
                ln = lines[0]
                if isinstance(ln.get("properties"), dict):
                    ln["properties"].pop("style", None)   # drop the edit-only colour (see _edit_feature)
                reach_feat.set(ln); reach_edit.set(False)   # commit ends edit (mirror boundary deselect)
                with reactive.isolate():
                    reach_gen.set(reach_gen() + 1)        # manual (re)commit → auto-chain marker
            return
        if step == STEP_K:
            # Fresh draws get a uid + default KH/KV; zones already carrying properties pass
            # through (the DrawControl round-trips them, so shape edits keep per-zone K).
            polys = [f for f in data_feats if (f.get("geometry") or {}).get("type") == "Polygon"]
            kzone_feats.set(geometry.normalize_kzone_features(polys, **_kz_defaults()))
            kz_adding.set(False)             # a guided "Add K-zone" draw just completed
            return
        if step != STEP_BOUNDARIES:
            return
        slot = bnd_slot()
        if not slot:
            return
        want = "Polygon" if slot == "wse" else "LineString"
        src = fresh or data_feats
        match = next((f for f in src if (f.get("geometry") or {}).get("type") == want), None)
        sv = _slot_value(slot)
        if match is not None and sv is not None:
            if slot != "wse":
                match = _snap_boundary_endpoints(slot, match)   # snap ends onto nearby neighbour ends
            if slot in ("up", "down"):
                cs = ((match.get("geometry") or {}).get("coordinates")) or []
                if len(cs) > 2:              # BC lines are straight by design → keep the chord
                    match = {"type": "Feature",
                             "properties": (match.get("properties") or {}),
                             "geometry": {"type": "LineString",
                                          "coordinates": [cs[0], cs[-1]]}}
            if isinstance(match.get("properties"), dict):
                match["properties"].pop("style", None)   # drop the edit-only colour (see _edit_feature)
            sv.set(match)                                 # so stored features stay pristine for statics/engine
            bnd_slot.set(None)          # commit done → deselect (line becomes a clickable static)

    def _edit_feature(feat, slot):
        """Copy `feat` with the slot's own colour baked into properties.style, so the line keeps its
        colour while loaded in the DrawControl for editing. Without this the DrawControl paints the
        loaded feature Leaflet's default #3388ff blue — indistinguishable from the Left FPL line.
        ipyleaflet's DrawControl honours per-feature properties.style (the same field it writes when
        persisting a drawn shape), so this is the styling hook for loaded-for-edit geometry."""
        if not feat:
            return feat
        base = WSE_STYLE if slot == "wse" else _BND_STATIC.get(slot, ("", REACH_STYLE))[1]
        props = dict(feat.get("properties") or {})
        props["style"] = {**base, "weight": max(4, int(base.get("weight", 3)) + 1)}
        return {"type": "Feature", "properties": props, "geometry": feat.get("geometry")}

    def _load_into_drawcontrol(feats):
        """Put generated GeoJSON Features into the DrawControl so the user can edit them."""
        dc = _draw_ctl.get("dc")
        if dc is None:
            return
        try:
            dc.data = [f for f in feats if f]
        except Exception:  # noqa: BLE001
            pass

    # ---- persistent map + draw control ----
    _base_layers: dict = {}                  # "imagery"/"topo" -> TileLayer handles

    if _HAS_MAP:
        def _build_map():
            # Custom panes are the ONLY z-order mechanism in this app (stacking is otherwise
            # pure Map.add append order, and the heal/relayer machinery re-adds in arbitrary
            # order). Terrain rasters live in hype-terrain (320) and user reference layers in
            # hype-ref (340), both under the default overlayPane (400) where every
            # app-generated overlay stays. CONSTRUCTOR-ONLY: mutating Map.panes later makes
            # the jupyter-leaflet client re-render the entire map.
            m = Map(center=MAP_HOME_CENTER, zoom=MAP_HOME_ZOOM, scroll_wheel_zoom=True,
                    zoom_control=False, max_zoom=19, layout=Layout(height="100%"),
                    panes={ml_mod.PANE_TERRAIN: {"zIndex": 320},
                           ml_mod.PANE_REF: {"zIndex": 340, "pointerEvents": "none"}})
            m.clear()
            m.add(ZoomControl(position="topright"))
            # USGS basemap caches stop at zoom 16 — cap max_native_zoom so Leaflet upscales the
            # deepest real tiles past 16 instead of showing blank tiles. Handles are kept so the
            # tree's Basemaps checkboxes can flip their `visible` traits (no LayersControl).
            _base_layers["imagery"] = TileLayer(
                url=USGS_IMAGERY, name="USGS Imagery", base=True, attribution=USGS_ATTR,
                max_native_zoom=16, max_zoom=19, visible=False)
            _base_layers["topo"] = TileLayer(
                url=USGS_TOPO, name="USGS Topo", base=True, attribution=USGS_ATTR,
                max_native_zoom=16, max_zoom=19)
            # NHD Hydrography is no longer a raster overlay: the base.hydro checkbox now
            # drives the "NHD streams" flowline VECTORS (see ui_tree layers tuple).
            for lyr in _base_layers.values():
                m.add(lyr)
            dc = DrawControl(
                position="topright",
                polygon={"shapeOptions": {"color": "#caa700", "fillColor": "#fdf24a",
                                          "fillOpacity": 0.1}},
                polyline={"shapeOptions": {"color": "#ff2d95", "weight": 4}},
                rectangle={}, circle={}, circlemarker={}, marker={},
            )
            _draw_ctl["dc"] = dc

            def _on_draw(target, action, geo_json):
                _reclassify_drawn(action=action, geo_json=geo_json)  # re-derive from the drawn shape

            dc.on_draw(_on_draw)
            m.add(dc)
            m.add(ScaleControl(position="bottomright"))   # bottom-left is the zoom/CRS chip

            def _on_interaction(**kw):     # capture map clicks for upstream/downstream picking
                if kw.get("type") == "click":
                    c = kw.get("coordinates") or [None, None]
                    if c[0] is not None:
                        last_click.set((float(c[0]), float(c[1])))
            m.on_interaction(_on_interaction)
            return m

        _MAP = _build_map()

        @render_widget
        def map():  # noqa: A001
            return _MAP

        @reactive.calc
        def _view():
            return reactive_read(_MAP, "zoom"), reactive_read(_MAP, "center")

    def _map_home():
        # Back to the national view — the New-project tails ONLY (Open flies to the
        # site itself, and a national jump first would churn layers mid-flight).
        # Direct trait writes are the geocode precedent; flyToBounds cannot express
        # "zoom out to 4 over CONUS" cleanly.
        if not _HAS_MAP:
            return
        _MAP.center = MAP_HOME_CENTER
        _MAP.zoom = MAP_HOME_ZOOM

    # ---- DEM acquire (3DEP download or local-raster import) ----
    @reactive.extended_task
    async def dem_task(payload: dict) -> dict:
        def _work():
            g = geometry.single_feature_gdf(payload["aoi"])
            if payload.get("mode") == "local":
                # Desktop-only path (the local-DEM radio is desktop-gated): no pixel
                # budget, the imported raster is clipped verbatim at full resolution.
                info = dem.import_local_dem(payload["src"], g, payload["out"],
                                            reach_feat_4326=payload.get("reach"),
                                            max_pixels=None)
            else:
                info = dem.fetch_dem(g, payload["out"], resolution=payload["resolution"])
            return {**info, "mode": payload.get("mode", "3dep"),
                    "summary": dem.dem_summary(info["path"])}
        return await anyio.to_thread.run_sync(_work)

    def _reach_meta():
        """Drainage area + midpoint + Bieger bankfull geometry for the current reach. AUTO reads
        the NHD-derived auto_meta; MANUAL derives it from the drawn centerline + the user's
        Drainage-area input. Returns None if there's no reach yet."""
        if delineate_mode() != "manual":
            return auto_meta()
        rf = reach_feat()
        if rf is None:
            return None
        import geopandas as gpd
        from shapely.geometry import shape as _shape
        try:
            da = float(_safe("manual_da", None))
        except (TypeError, ValueError):
            return None                    # no drainage area yet — no Bieger geometry to derive
        if da <= 0:
            return None
        line = _shape(rf["geometry"])
        mid = line.interpolate(0.5, normalized=True)
        try:
            length_m = float(gpd.GeoSeries([line], crs=4326).to_crs(5070).length.iloc[0])
        except Exception:  # noqa: BLE001
            length_m = 0.0
        bf = bieger.bankfull_geometry(da, mid.y, mid.x)
        return {"da_sqkm": da, "length_m": length_m, "lat": float(mid.y),
                "lon": float(mid.x), **bf}

    @reactive.calc
    def _manual_da_valid():
        # Subscribing read — recomputes the moment a drainage area is typed (input first,
        # falling back to the remount-mirror for panes that haven't mounted yet).
        try:
            v = input.manual_da()
        except Exception:  # noqa: BLE001
            v = _kept.get("manual_da")
        try:
            return v is not None and float(v) > 0
        except (TypeError, ValueError):
            return False

    _dem_last = {"mode": "3dep"}       # which acquire branch launched last (error wording)

    def _dem_local_active() -> bool:
        """Local-raster mode is honored on desktop only: a cloud-restored local project
        degrades to the 3DEP download (no local filesystem to link against)."""
        return runmode.IS_DESKTOP and (dem_src() or {}).get("mode") == "local"

    def _launch_dem_fetch(rf):
        # Shared by the pane buttons, the source-pick handler, and the reach→terrain
        # auto-chain. Branches on the terrain source; both branches write inputs/dem.tif.
        import geopandas as gpd
        from shapely.geometry import mapping, shape as _shape
        meta = _reach_meta() or {}
        half = min(max(8.0 * max(meta.get("width_m", 1.0), 1.0), 250.0), 800.0)
        buf = (gpd.GeoSeries([_shape(rf["geometry"])], crs=4326).to_crs(5070)
               .buffer(half + 60.0).to_crs(4326).iloc[0])
        aoi = {"type": "Feature", "properties": {}, "geometry": mapping(buf)}
        out = str(work_dir / "inputs" / "dem.tif")
        with reactive.isolate():       # source reads must not re-trigger the calling effect
            local = _dem_local_active()
            src = (dem_src() or {}).get("path")
        if local:
            if not src:
                ui.notification_show("Choose a raster file first.", type="warning",
                                     duration=5)
                return
            if not Path(src).is_file():
                ui.notification_show("The linked raster was not found. Use \"Locate the "
                                     "file...\" to repoint it.", type="warning", duration=6)
                return
            _dem_last["mode"] = "local"
            stage.set("Importing terrain from the local raster…")
            dem_task({"mode": "local", "src": src, "aoi": aoi, "reach": rf, "out": out})
        else:
            _dem_last["mode"] = "3dep"
            stage.set("Downloading 3DEP terrain for the reach…")
            dem_task({"mode": "3dep", "aoi": aoi, "out": out,
                      "resolution": _safe("dem_res", "auto")})

    @reactive.effect
    def _fetch_dem():
        if not _clicked_dynamic("fetch_dem"):
            return
        rf = reach_feat()
        if rf is None:
            ui.notification_show("Define a reach first — stage 1 in the bar above.",
                                 type="warning", duration=5)
            return
        if delineate_mode() == "manual" and not _manual_da_valid():
            ui.notification_show("Enter the drainage area (km²) first. It sizes the terrain "
                                 "download and the boundaries.", type="warning", duration=6)
            return
        _launch_dem_fetch(rf)

    @reactive.effect
    def _mirror_dem_src_mode():
        # Radio → record mirror. A mode switch mid-acquire cancels the in-flight task so the
        # wrong terrain can never land after the user changed their mind; nothing else runs
        # automatically (switching modes must never clobber terrain without a click).
        try:
            v = input.dem_src_mode()
        except Exception:  # noqa: BLE001 — pane not mounted yet
            return
        v = "local" if v == "local" else "3dep"
        with reactive.isolate():
            rec = dict(dem_src() or {})
        if rec.get("mode") == v:
            return
        rec["mode"] = v
        dem_src.set(rec)
        if _task_state(dem_task) == "running":
            try:
                dem_task.cancel()
            except Exception:  # noqa: BLE001
                pass
            stage.set("")

    @reactive.effect
    @reactive.event(input.dem_import_evt)
    def _dem_import_click():
        rf = reach_feat()
        if rf is None:
            ui.notification_show("Define a reach first (stage 1 in the bar above).",
                                 type="warning", duration=5)
            return
        if delineate_mode() == "manual" and not _manual_da_valid():
            ui.notification_show("Enter the drainage area (km²) first. It sizes the terrain "
                                 "extent and the boundaries.", type="warning", duration=6)
            return
        _launch_dem_fetch(rf)

    def _show_dem_overlay(path):
        """Hillshade backdrop for `path` — the render block every DEM producer shares
        (fetch completion, project restore)."""
        if not _HAS_MAP:
            return
        try:
            with reactive.isolate():
                hs = float(dem_hs_v()); op = float(dem_opacity_v())
            ov = dem.dem_overlay(path, vert_exag=hs)
            _set_layer("dem", ImageOverlay(url=ov["url"], bounds=ov["bounds"],
                                           name="DEM (hillshade)", opacity=op,
                                           pane=ml_mod.PANE_TERRAIN))
            _dem_shade_sig["sig"] = (path, hs, None)   # skip the duplicate re-render
            dem_lohi_v.set((ov["vmin"], ov["vmax"]))
            _map_ui["dem_bounds"] = ov["bounds"]              # zoom-to-extent target
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"DEM loaded; overlay render issue: {e}", duration=5)

    @reactive.effect
    def _dem_done():
        if dem_task.status() in ("initial", "running"):
            return
        stage.set("")
        try:
            res = dem_task.result()
        except dem.DemImportError as e:    # local-import validation: message written for the user
            ui.notification_show(str(e), type="error", duration=9)
            return
        except Exception:                  # cancelled stays silent; only a real error toasts
            if dem_task.status() == "error":
                ui.notification_show(
                    "Terrain import failed. Check that the file is a valid GeoTIFF."
                    if _dem_last["mode"] == "local" else
                    "DEM fetch failed at all 3DEP resolutions. Try a smaller area.",
                    type="error", duration=8)
            return
        dem_path.set(res["path"])
        with reactive.isolate():
            dem_gen.set(dem_gen() + 1)     # fetch completed → terrain→boundaries chain marker
            # Manual draws can run downhill→uphill (auto NHD traces are upstream-first by
            # construction) — correct a backwards line NOW, in the same flush, so the
            # boundary auto-chain always consumes the corrected reach. The NHD flow
            # direction (_manual_dir_check → dir_task) is the authority; the terrain check
            # runs only when NHD said undecidable. A lookup still in flight defers the
            # decision to _dir_done, which sees the DEM present and finishes the job
            # (recommitting via reach_gen, because this flush's chain consumed the
            # uncorrected line).
            if delineate_mode() == "manual" and reach_feat() is not None:
                if (_reach_dir["sig"] == _dir_sig(reach_feat())
                        and _reach_dir["verdict"] is None):
                    _dem_orient_reach(res["path"])
        dem_meta.set({"resolution_m": res.get("resolution_m"), "source": res.get("source"),
                      "src_name": res.get("src_name")})
        dem_stretch_v.set(None)            # a fresh DEM starts at the full-raster stretch
        _unhide_node_layers("terrain.dem")  # a fresh fetch always shows itself
        _show_dem_overlay(res["path"])      # hillshade backdrop
        if res.get("mode") == "local":
            # Stamp the source file's mtime so the pane's "changed on disk" hint resets.
            with reactive.isolate():
                rec = dict(dem_src() or {})
            try:
                rec["src_mtime"] = (Path(rec["path"]).stat().st_mtime
                                    if rec.get("path") else None)
            except OSError:
                rec["src_mtime"] = None
            dem_src.set(rec)
            if res.get("note"):            # partial coverage / decimation: warn, don't block
                ui.notification_show(res["note"], type="warning", duration=9)
        # A fresh original DEM invalidates any carve built on the previous one.
        with reactive.isolate():
            had_carve = carve_active()
        if had_carve:
            carve_active.set(False)
            carve_meta.set(None)
            _set_layer("dem_carve", None)
        _mark_stale_from_results()         # results computed on the replaced terrain go amber
        with reactive.isolate():           # chain-aware wording (light, isolated reads)
            will_chain = (_domain_build() is None
                          and not any(f is not None for f in
                                      (up_feat(), left_feat(), right_feat(), down_feat())))
        ui.notification_show("Terrain ready. Generating boundaries…" if will_chain
                             else "Terrain ready.", duration=4)

    # ---- channel carving (Terrain ▸ Channel modification) ----
    @reactive.extended_task
    async def carve_task(dem_p: str, feat: dict, out_p: str, diff_p: str,
                         bw: float, depth: float, slope: float) -> dict:
        # Spawned child, NOT a worker thread: shapely's vectorized creation inside
        # carve_channel can hard-crash the interpreter under this stack (Windows 0x80000003
        # while "Garbage-collecting"; numpy 2.5.0 + shapely 2.1.2, reproduced 3/3 in-app on
        # 2026-07-07 yet never offline — heap-state dependent). Isolation keeps the server
        # alive and surfaces "Carving failed" instead of killing the session.
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=carve.child_carve,
                            args=({"dem": dem_p, "feat": feat, "out": out_p, "diff": diff_p,
                                   "bw": bw, "depth": depth, "slope": slope}, q), daemon=True)
            p.start()
            result = error = None
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "log":
                        print(data)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:
                try:
                    kind, data = q.get_nowait()
                    if kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            if error is not None:
                raise RuntimeError(error)
            if result is None:
                raise RuntimeError("The carve stopped unexpectedly.")
            return result
        return await anyio.to_thread.run_sync(_work)

    def _mark_stale_from_results():
        """Terrain changed under existing results — badge what was computed on the old one.
        ALL reads isolated: callers are task-completion handlers (the shared-helper
        subscription poison documented in _delineate_done)."""
        with reactive.isolate():
            marks = set()
            if ras_result() is not None or ras_mesh_prev() is not None:
                marks.add("sw")
            if run_result() is not None:
                marks.add("gw")
            if marks:
                _stale_marks.set(frozenset(_stale_marks() | marks))
        if marks:
            ui.notification_show("Terrain changed. Existing surface/groundwater results "
                                 "were computed on the previous terrain; re-run to update "
                                 "them.", type="warning", duration=8)

    @reactive.effect
    def _carve_btn():
        if not _clicked_dynamic("carve_btn"):
            return
        rf = reach_feat()
        p = dem_path()
        if rf is None or p is None:
            ui.notification_show("Define the reach and fetch terrain first.",
                                 type="warning", duration=5)
            return
        if carve_task.status() == "running":
            return
        out = work_dir / "inputs" / "dem_carved.tif"
        diff = work_dir / "inputs" / "dem_carve_diff.tif"
        stage.set("Carving the channel…")
        carve_task(p, rf, str(out), str(diff),           # always carve from the ORIGINAL
                   float(_safe("carve_bw", 4.0)), float(_safe("carve_depth", 1.5)),
                   float(_safe("carve_slope", 2.0)))

    def _show_carve_overlay(meta):
        """Carve-difference overlay from `meta` — shared by the carve handler and restore."""
        if _HAS_MAP and meta and meta.get("diff_path"):
            try:
                mx = max(float(meta.get("max_cut_m") or 0.5), 0.25)
                ov = results.raster_overlay(meta["diff_path"], vmin=0.0, vmax=mx, cmap="Blues")
                _set_layer("dem_carve", ImageOverlay(url=ov["url"], bounds=ov["bounds"],
                                                     name="Channel modification", opacity=0.85,
                                                     pane=ml_mod.PANE_TERRAIN))
                _unhide_node_layers("terrain.chanmod")
            except Exception:  # noqa: BLE001
                pass

    @reactive.effect
    def _carve_done():
        if carve_task.status() in ("initial", "running"):
            return
        stage.set("")
        if carve_task.status() == "cancelled":
            return                         # a cancelled result() would toast "Carving failed: "
        try:
            res = carve_task.result()
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Carving failed: {e}", type="error", duration=8)
            return
        carve_meta.set(res)
        carve_active.set(True)              # active_dem() flips → hillshade/3-D re-render
        _dem_shade_sig.pop("sig", None)     # force the hillshade re-render for the new raster
        _show_carve_overlay(res)
        _mark_stale_from_results()
        ui.notification_show(f"Channel carved — max cut {res.get('max_cut_m', 0):.2f} m over "
                             f"{res.get('cells_cut', 0):,} cells. All later steps now use the "
                             f"modified terrain.", duration=7)

    @reactive.effect
    def _carve_revert():
        if not _clicked_dynamic("carve_revert"):
            return
        if not carve_active():
            return
        carve_active.set(False)
        m = carve_meta()
        carve_meta.set(None)
        _dem_shade_sig.pop("sig", None)
        _set_layer("dem_carve", None)
        for pth in ((m or {}).get("path"), (m or {}).get("diff_path")):
            if pth:
                try:
                    Path(pth).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass
        _mark_stale_from_results()
        ui.notification_show("Reverted to the original terrain.", duration=5)

    # ---- auto-delineation: NHD streams → pick 2 points → reach → cross-sections ----
    @reactive.extended_task
    async def flow_task(bbox: tuple) -> dict:
        return await anyio.to_thread.run_sync(lambda: hydro.flowlines_bbox(*bbox) or {})

    def _do_flow_fetch(force=False):
        # Fetch box from the map CENTER + zoom-scaled radius (the viewport `bounds` trait is
        # unreliable per EASI; center/zoom always update via _view).
        if not _HAS_MAP:
            return
        z, c = _view()
        if not c or z is None or int(z) < NHD_MIN_ZOOM:
            nhd_status.set("Zoom in to load streams.")
            return
        lat, lon = float(c[0]), float(c[1])
        delta = min(0.08, 0.03 * (2 ** (15 - int(z))))   # half-box in degrees
        bbox = (round(lon - delta, 3), round(lat - delta, 3),
                round(lon + delta, 3), round(lat + delta, 3))
        if not force and _flow.get("bbox") == bbox:      # already fetched this view
            return
        _flow["bbox"] = bbox
        nhd_status.set("")                 # the bottom "Loading streams…" spinner shows progress
        flow_task(bbox)

    @reactive.effect
    def _load_flowlines():
        if delineate_mode() != "auto" or current_step() != STEP_REACH:
            return
        _do_flow_fetch()                                 # reads _view() → fires on pan/zoom

    @reactive.effect
    @reactive.event(input.address_pick)
    def _on_address_pick():
        # A suggestion was chosen in the type-ahead dropdown (coords come from the client-side
        # Photon query in www/geocode.js) — recenter the map; _load_flowlines then auto-fetches
        # the NHD streams at the new view.
        if not _HAS_MAP:
            return
        p = input.address_pick() or {}
        lat, lon = p.get("lat"), p.get("lon")
        if lat is None or lon is None:
            return
        _MAP.center = (float(lat), float(lon))
        _MAP.zoom = NHD_MIN_ZOOM   # land deep enough that the stream fetch actually fires

    @reactive.effect
    def _find_address():
        if not _clicked_dynamic("find_address"):
            return
        # Button fallback: geocode server-side (Photon → Nominatim) and recenter.
        if not _HAS_MAP:
            return
        hit = geocode.geocode_address(_safe("address", ""))
        if hit:
            _MAP.center = (float(hit[0]), float(hit[1]))
            _MAP.zoom = NHD_MIN_ZOOM
        else:
            ui.notification_show("Place not found — try a city, address, or stream name.",
                                 type="warning", duration=5)

    @reactive.effect
    def _flow_done():
        if flow_task.status() in ("initial", "running"):
            return
        if flow_task.status() == "error":
            nhd_status.set("Couldn't load streams — try again.")
            try:
                flow_task.result()
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"NHD streams failed: {e}", type="warning", duration=8)
            return
        try:
            gj = flow_task.result()
        except Exception:  # noqa: BLE001
            return
        n = len(gj.get("features", [])) if gj else 0
        if n:
            _set_layer("NHD streams", GeoJSON(data=gj, style=NHD_STYLE, name="NHD streams"))
            try:
                import geopandas as gpd
                _flow["gdf"] = gpd.GeoDataFrame.from_features(gj["features"], crs=4326)
            except Exception:  # noqa: BLE001
                _flow["gdf"] = None
            nhd_status.set("")     # streams are visible on the map — no status line needed
        else:
            nhd_status.set("No streams here — pan to a stream, or draw manually.")

    @reactive.extended_task
    async def snap_task(lat: float, lon: float) -> dict:
        return await anyio.to_thread.run_sync(lambda: hydro.snap(lat, lon, _flow.get("gdf")) or {})

    @reactive.effect
    @reactive.event(last_click)
    def _on_click_pick():
        if delineate_mode() != "auto" or reach_feat() is not None:
            return
        if len(pick_pts()) >= 2 or snap_task.status() == "running":
            return
        c = last_click()
        if not c:
            return
        stage.set("Snapping to the nearest stream…")
        snap_task(c[0], c[1])

    @reactive.effect
    def _snap_done():
        if snap_task.status() in ("initial", "running"):
            return
        stage.set("")
        if snap_task.status() == "error":
            ui.notification_show("Couldn't reach the NHD service — try again, or use manual drawing.",
                                 type="warning", duration=6)
            return
        try:
            sp = snap_task.result()
        except Exception:  # noqa: BLE001
            return
        if not sp or sp.get("comid") is None:
            ui.notification_show("No NHD stream near that click — zoom in and click a cyan "
                                 "flowline.", type="warning", duration=6)
            return
        # Read+write pick_pts inside isolate() so this effect doesn't depend on pick_pts —
        # otherwise the .set() below would re-trigger it, appending the same point forever.
        with reactive.isolate():
            pts = list(pick_pts())
            same_as_last = (pts and pts[-1].get("comid") == sp.get("comid")
                            and abs(pts[-1]["lat"] - sp["lat"]) < 1e-7
                            and abs(pts[-1]["lon"] - sp["lon"]) < 1e-7)
            if len(pts) >= 2 or same_as_last:
                return                                   # already have two, or a duplicate re-fire
            pts.append(sp)
            pick_pts.set(pts)
        n = len(pts)
        _set_layer(f"pick{n}", Marker(location=(sp["lat"], sp["lon"]), draggable=False,
                   title=("Upstream point" if n == 1 else "Downstream point")))
        if n == 2:
            stage.set("Tracing the reach along the NHD…")
            reach_task(pts[0], pts[1])
        else:
            ui.notification_show("Upstream point set — now click the downstream point.", duration=4)

    @reactive.extended_task
    async def reach_task(up: dict, dn: dict) -> dict:
        return await anyio.to_thread.run_sync(lambda: hydro.reach_between(up, dn))

    @reactive.effect
    def _reach_done():
        if reach_task.status() in ("initial", "running"):
            return
        stage.set("")
        if reach_task.status() == "error":
            try:
                reach_task.result()
            except Exception as e:  # noqa: BLE001
                print(f"[reach] error: {e!r}", flush=True)
                ui.notification_show(str(e), type="error", duration=10)
            pick_pts.set([]); _set_layer("pick1", None); _set_layer("pick2", None)
            return
        try:
            r = reach_task.result()
        except Exception:  # noqa: BLE001
            return
        bf = bieger.bankfull_geometry(r["da_sqkm"], r["lat"], r["lon"])
        auto_meta.set({"da_sqkm": r["da_sqkm"], "length_m": r["length_m"],
                       "lat": r["lat"], "lon": r["lon"], **bf})
        reach_feat.set(r["reach"])
        with reactive.isolate():
            reach_gen.set(reach_gen() + 1)                # auto trace commit → auto-chain marker
        _decor_show("Reach", r["reach"], REACH_STYLE)   # wired click-to-select; never-remove
        _set_layer("pick1", None); _set_layer("pick2", None)   # drop the transient pick markers
        for w in r.get("warnings", []):
            ui.notification_show(w, duration=5)

    # ---- manual-mode drainage area: prefill from NHD once a centerline exists ----
    _DA_SNAP_MAX_FT = 500.0        # snapped farther than this → likely a different stream
    _da_snap = {"sig": None}       # one lookup per drawn geometry (no retry loop on a miss)

    def _reach_endpoints(rf):
        """First/last vertices of the centerline as {lat, lon} dicts, or None."""
        try:
            coords = rf["geometry"]["coordinates"]
            c0, c1 = coords[0], coords[-1]
            return ({"lat": float(c0[1]), "lon": float(c0[0])},
                    {"lat": float(c1[1]), "lon": float(c1[0])})
        except Exception:  # noqa: BLE001
            return None

    @reactive.extended_task
    async def da_task(p1: dict, p2: dict) -> dict:
        # Both endpoints go in symmetrically (manual draws can run backwards until the
        # DEM-based flip) and snap_reach_da keeps the largest qualifying drainage area —
        # the mainstem, never a tributary mouth. Echo the query for the stale guard.
        def _work():
            best = hydro.snap_reach_da(p1, p2, _flow.get("gdf"), max_ft=_DA_SNAP_MAX_FT)
            return {"best": best, "q": [p1, p2]}
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    def _manual_da_prefill():
        # Manual centerlines carry no NHD attributes, so look the drainage area up for the
        # user (still editable). Subscribing reads: re-fires on draw/edit/restore commits and
        # mode flips; a typed value is authoritative (_manual_da_valid blocks the lookup).
        if delineate_mode() != "manual":
            return
        rf = reach_feat()
        if rf is None or _manual_da_valid():
            return
        eps = _reach_endpoints(rf)
        if eps is None:
            return
        # Direction-insensitive sig: the DA lookup is orientation-proof (both endpoints go
        # in symmetrically), so a direction flip of the same line must not re-query.
        sig = tuple(sorted((round(e["lat"], 7), round(e["lon"], 7)) for e in eps))
        if _da_snap["sig"] == sig:
            return                      # this exact line already tried (no retry loop)
        _da_snap["sig"] = sig
        stage.set("Looking up the drainage area from NHD…")
        da_task(*eps)

    @reactive.effect
    def _da_prefill_done():
        if da_task.status() in ("initial", "running"):
            return
        stage.set("")
        if da_task.status() == "cancelled":
            return
        if da_task.status() == "error":
            try:
                da_task.result()
            except Exception:  # noqa: BLE001
                pass
            _da_snap["sig"] = None      # transient service failure — an edit commit retries
            ui.notification_show("Couldn't reach the NHD service for the drainage area. "
                                 "Enter it manually.", type="warning", duration=6)
            return
        try:
            res = da_task.result()
        except Exception:  # noqa: BLE001
            return
        with reactive.isolate():        # the done-handler re-fire lesson: status is the only dep
            rf = reach_feat()
            mode = delineate_mode()
            valid = _manual_da_valid()
        if mode != "manual" or rf is None or valid:
            return
        eps = _reach_endpoints(rf)
        q = res.get("q") or []

        # Direction-insensitive compare: a direction flip mid-lookup must not discard the
        # result (the DA is orientation-proof by construction).
        def _sym(pair):
            return tuple(sorted((round(p["lat"], 7), round(p["lon"], 7)) for p in pair))

        if eps is None or len(q) != 2 or _sym(eps) != _sym(q):
            print("[da] snap result is for an older line — ignored", flush=True)
            return                      # the line changed while we were snapping — stale
        best = res.get("best")
        if best is None:
            print("[da] no NHD flowline near the drawn endpoints", flush=True)
            ui.notification_show("Stream not found in NHD. Enter the drainage area manually.",
                                 type="message", duration=6)
            return
        da = float(best["da_sqkm"])
        val = round(da, 2) if da < 1.0 else round(da, 1)
        _kept["manual_da"] = val
        ui.update_numeric("manual_da", value=val)
        nm = best.get("name") or "unnamed stream"
        print(f"[da] set {val} km2 from NHD ({nm}, {best.get('dist_ft', 0):.0f} ft off "
              f"the line)", flush=True)
        ui.notification_show(f"Drainage area set from NHD: {val:g} km² ({nm}). Adjust it "
                             f"if needed.", duration=6)

    # ---- manual-mode direction: the NHD flow direction is the authority ----
    # A manual draw can run either way regardless of the pane's instruction. The old
    # terrain-only check compared mean end elevations against a 5 cm threshold, which on a
    # low-gradient meander is decided by lidar water-surface noise and bank pixels under the
    # hand-drawn line — the reversed-horseshoe bug. Now NHD decides first (the same
    # authority Auto mode is built on) and the hardened terrain check is the fallback for
    # unmapped streams.
    _reach_dir = {"sig": None, "verdict": "pending"}  # verdict: pending | ok | reversed | None

    def _dir_sig(rf):
        """Directional endpoints signature of a reach Feature (order matters), or None."""
        eps = _reach_endpoints(rf)
        if eps is None:
            return None
        return tuple((round(e["lat"], 7), round(e["lon"], 7)) for e in eps)

    def _dem_orient_reach(dem_p, *, bump_gen: bool = False) -> None:
        """Terrain fallback for the direction check (call under isolate): flip the manual
        centerline when the DEM says it was drawn decisively uphill. bump_gen recommits the
        corrected line to the auto-chain — needed only when the flip lands AFTER the chain
        already consumed the uncorrected draw (the deferred _dir_done path)."""
        rf = reach_feat()
        if rf is None:
            return
        try:
            fixed, was_flipped = delineate.orient_reach_downstream(rf, dem_p)
        except Exception:  # noqa: BLE001
            fixed, was_flipped = None, False
        if not was_flipped or fixed is None:
            return
        _reach_dir["sig"] = _dir_sig(fixed)
        _reach_dir["verdict"] = "ok"    # settled — the corrected line never re-checks
        reach_feat.set(fixed)
        if bump_gen:
            reach_gen.set(reach_gen() + 1)
        _decor_show("Reach", fixed, REACH_STYLE)
        ui.notification_show("Centerline direction corrected to upstream → "
                             "downstream (from the terrain).", duration=6)

    @reactive.effect
    def _manual_dir_check():
        # Subscribing reads: re-fires on draw/edit/restore commits and mode flips. One
        # lookup per drawn geometry (sig guard); a flip pre-stores the corrected line's sig
        # as settled, so corrections never re-check (and never loop). Restores pre-seed the
        # sig, so an opened project is never re-oriented.
        if delineate_mode() != "manual":
            return
        rf = reach_feat()
        if rf is None:
            return
        sig = _dir_sig(rf)
        if sig is None or _reach_dir["sig"] == sig:
            return
        _reach_dir["sig"] = sig
        _reach_dir["verdict"] = "pending"
        dir_task(*_reach_endpoints(rf))

    @reactive.extended_task
    async def dir_task(p1: dict, p2: dict) -> dict:
        # Echo the query for the stale guard (the da_task pattern).
        def _work():
            v = hydro.reach_flow_direction(p1, p2, _flow.get("gdf"), max_ft=_DA_SNAP_MAX_FT)
            return {"dir": v, "q": [p1, p2]}
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    def _dir_done():
        if dir_task.status() in ("initial", "running", "cancelled"):
            return
        failed = dir_task.status() == "error"
        res = None
        if failed:
            try:
                dir_task.result()
            except Exception:  # noqa: BLE001
                pass
            print("[dir] NHD direction lookup failed — the terrain decides", flush=True)
        else:
            try:
                res = dir_task.result()
            except Exception:  # noqa: BLE001
                return
        with reactive.isolate():        # the done-handler re-fire lesson: status is the only dep
            rf = reach_feat()
            mode = delineate_mode()
            dem_p = dem_path()
        if mode != "manual" or rf is None:
            return
        verdict = None                  # a service failure means NHD can't decide
        if not failed:
            eps = _reach_endpoints(rf)
            q = res.get("q") or []
            if eps is None or len(q) != 2 or any(
                    abs(e["lat"] - p["lat"]) > 1e-7 or abs(e["lon"] - p["lon"]) > 1e-7
                    for e, p in zip(eps, q)):
                print("[dir] direction result is for an older line — ignored", flush=True)
                return
            verdict = res.get("dir")
        _reach_dir["verdict"] = verdict
        if verdict == "reversed":
            flipped = delineate.reversed_feature(rf)
            if flipped is None:
                return
            _reach_dir["sig"] = _dir_sig(flipped)
            _reach_dir["verdict"] = "ok"   # settled — the corrected line never re-checks
            with reactive.isolate():
                reach_feat.set(flipped)
                if dem_p is not None:      # the chain consumed the old line — recommit
                    reach_gen.set(reach_gen() + 1)
            _decor_show("Reach", flipped, REACH_STYLE)
            ui.notification_show("Centerline direction corrected to upstream → downstream "
                                 "(from the NHD flow direction).", duration=6)
            print("[dir] flipped to match the NHD flow direction", flush=True)
            return
        if verdict == "ok":
            print("[dir] draw direction matches the NHD flow", flush=True)
            return
        # NHD couldn't decide — the terrain fallback decides: right now if the DEM beat
        # this lookup, otherwise in _dem_done at terrain completion.
        print("[dir] NHD could not decide the flow direction", flush=True)
        if dem_p is not None:
            with reactive.isolate():
                _dem_orient_reach(dem_p, bump_gen=True)

    @reactive.extended_task
    async def delineate_task(reach, dem_p, da, lat, lon, x_mult, want_wse) -> dict:
        return await anyio.to_thread.run_sync(
            lambda: delineate.auto_delineate(reach, dem_p, da_sqkm=da, lat=lat, lon=lon,
                                             x_mult=x_mult, want_wse=want_wse))

    @reactive.effect
    def _delineate_done():
        if delineate_task.status() in ("initial", "running"):
            return
        stage.set("")
        if delineate_task.status() == "error":
            try:
                delineate_task.result()
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"Auto-delineation failed: {e}. You can switch to manual "
                                     f"drawing.", type="error", duration=10)
            return
        try:
            d = delineate_task.result()
        except Exception:  # noqa: BLE001
            return
        # Fill the four named boundary slots (domain derives from them); up/down are now first-class
        # editable boundaries, not static caps.
        up_feat.set(d.get("up_cap")); left_feat.set(d["left"])
        right_feat.set(d["right"]); down_feat.set(d.get("down_cap"))
        with reactive.isolate():
            wse_wanted = wse_mode_v() == "draw"
        if wse_wanted:                         # extent derived only for the "Wetted extent" mode;
            wse_extent_feat.set(d["wse_extent"])   # modeled/uploaded modes bring their own WSE
        #                                        (model-first default — don't clobber the mode)
        # Imperative map pushes below run ISOLATED: _render_boundaries /
        # _mirror_features_as_layers deliberately take a subscribing wse_mode_v read for
        # their OWNER effects — inherited here it would re-run this whole handler on a
        # WSE-mode radio flip, re-setting the features and clobbering the mode back to
        # "draw" (and wiping any boundary edits with stale delineation output).
        with reactive.isolate():
            on_boundaries = current_step() == STEP_BOUNDARIES
            bnd_slot.set(None)                 # deselect; nothing armed until a boundary button is clicked
            if on_boundaries:
                _load_into_drawcontrol([])
                _render_boundaries(None)
            else:                              # generated before reaching Boundaries → show statics
                _mirror_features_as_layers()
            # No auto-fit on generation: per user request the view only moves when the user clicks
            # the props-pane Zoom-to-extent button (tree "zoom" → hype_fly), never on an operation.
            # NO relayer re-assert here. The boundary / WSE / reach layers are never-remove
            # `_decor_show` widgets — created once, then trait-mutated — so they render reliably on
            # their own. Re-asserting them rebuilt a FRESH widget per key (remove + add), trickled
            # ~0.45 s apart, which was the post-generation flicker (4–5 sequential blinks, on both
            # first generation and every regenerate). The relayer still guards the genuinely
            # add/removed result layers (head / grid / pathlines) elsewhere — just not these.
        ui.notification_show(("Domain, boundaries & wetted extent generated — review or edit "
                              "each side in the Layers panel, then continue to Water surface.")
                             if wse_wanted else
                             ("Domain & boundaries generated — review or edit each side in the "
                              "Layers panel, then continue to Water surface."), duration=8)

    def _launch_boundaries():
        # Shared by the pane button and the terrain→boundaries auto-chain.
        meta = _reach_meta() or {}
        stage.set("Building cross-sections…")
        # The wetted extent is only consumed when the WSE mode is "draw" — skip the extra DEM
        # pass otherwise (flipping the radio to "draw" later derives it lazily, see _wse_auto).
        delineate_task(reach_feat(), active_dem(), meta.get("da_sqkm", 0.0),
                       meta.get("lat"), meta.get("lon"), float(_safe("fp_mult", 10)),
                       wse_mode_v() == "draw")

    @reactive.effect
    def _regenerate():
        if not _clicked_dynamic("regen"):
            return
        if reach_feat() is None or dem_path() is None:
            ui.notification_show("Define a reach and fetch the DEM first.", type="warning", duration=5)
            return
        if delineate_mode() == "manual" and not _manual_da_valid():
            ui.notification_show("Enter the drainage area (km²) first. Boundaries are sized "
                                 "from it.", type="warning", duration=6)
            return                # without it bieger clamps to a 0.01 km² stream — silently tiny
        _launch_boundaries()

    # ---- auto-chain: reach → terrain → boundaries. Kills the two dead clicks; the pane
    # buttons remain as Re-fetch / Regenerate for a deliberate redo. ----
    @reactive.effect
    def _chain_dem():
        g = reach_gen()
        if g == 0 or reach_feat() is None or dem_path() is not None:
            return                # no reach yet — or an edit: the fetched buffer already covers it
        if _task_state(dem_task) == "running" or _chain["dem"] == g:
            return                # in flight, or this generation already tried (no retry loop)
        if delineate_mode() == "manual" and not _manual_da_valid():
            return                # subscribing read — fires again the moment a valid DA arrives
        with reactive.isolate():  # source reads stay isolated: the PICK handler launches the
            rec = dem_src() or {}  # import when a raster arrives, never this chain
            if _dem_local_active() and not (rec.get("path") and Path(rec["path"]).is_file()):
                return            # local mode without a usable raster: nothing to auto-run
        _chain["dem"] = g
        _launch_dem_fetch(reach_feat())

    @reactive.effect
    def _chain_bnd():
        g = dem_gen()
        if g == 0 or dem_path() is None or _domain_build() is not None:
            return                # regeneration stays a deliberate click (it cascade-clears results)
        if _task_state(delineate_task) == "running" or _chain["bnd"] == g:
            return
        with reactive.isolate():
            if any(f is not None for f in (up_feat(), left_feat(), right_feat(), down_feat())):
                return            # partial manual boundaries — never clobber user work
            if delineate_mode() == "manual" and not _manual_da_valid():
                return            # boundaries are sized by the drainage area
        _chain["bnd"] = g
        _launch_boundaries()

    @reactive.extended_task
    async def wse_task(reach, dem_p, da, lat, lon):
        return await anyio.to_thread.run_sync(
            lambda: delineate.auto_wse_extent(reach, dem_p, da_sqkm=da, lat=lat, lon=lon))

    @reactive.effect
    def _wse_auto():
        # Lazily derive the auto wetted extent when the user switches the water-surface mode to
        # "Wetted extent" and none exists yet (generate ran in a mode that skipped it). Subscribes
        # ONLY to the mode flip — a user clearing the drawn extent must not trigger a re-derive.
        if wse_mode_v() != "draw":
            return
        if delineate_mode() == "manual" and not _manual_da_valid():
            return                # the ribbon is sized by the drainage area; subscribing read —
        #                           fires again the moment a valid DA arrives (cf. _chain_dem)
        with reactive.isolate():
            have = wse_extent_feat() is not None
            reach = reach_feat(); dem_p = dem_path()
            meta = _reach_meta() or {}
        if have or reach is None or dem_p is None or wse_task.status() == "running":
            return
        stage.set("Deriving the wetted extent…")
        with reactive.isolate():
            dem_p = active_dem() or dem_p      # a carved channel reshapes the wetted extent
        wse_task(reach, dem_p, meta.get("da_sqkm", 0.0), meta.get("lat"), meta.get("lon"))

    @reactive.effect
    def _wse_auto_done():
        if wse_task.status() in ("initial", "running"):
            return
        stage.set("")
        if wse_task.status() == "error":
            try:
                wse_task.result()
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"Couldn't derive the wetted extent: {e}. You can draw one "
                                     f"(click Water surface in the legend).", type="warning",
                                     duration=8)
            return
        try:
            feat = wse_task.result()
        except Exception:  # noqa: BLE001
            return
        with reactive.isolate():
            if wse_extent_feat() is not None:   # user drew one while the task ran — keep theirs
                return
        if feat is None:
            ui.notification_show("Couldn't derive a wetted extent from the DEM — draw one via "
                                 "the Water surface legend row.", type="warning", duration=8)
            return
        wse_extent_feat.set(feat)
        ui.notification_show("Wetted extent derived from the DEM — select Wetted extent in the "
                             "tree to edit it.", duration=5)

    # ---- parameters + estimate ----
    @reactive.calc
    def params():
        bc = _safe("bc_mode", BC_QUAL)
        base = dict(
            cell_size_x=float(_safe("cell_size", 10.0)), cell_size_y=float(_safe("cell_size", 10.0)),
            gw_mod_depth=float(_safe("gw_mod_depth", 6.0)), z=float(_safe("z", 0.25)),
            model_origin_elev=model_origin_effective(),
            kh=float(_safe("kh", 10.0)), kv=float(_safe("kv", 1.0)),
            porosity=float(_safe("porosity", 0.3)),
            length_units="meters", time_units="days",
            # steady hyporheic screening defaults — no stress-period fields in the UI
            nper=1, nstp=1, perlen=1.0, tsmult=1.0, sim_name="hyporheic",
            boundary_condition_mode=bc,
        )
        # Qualitative and gradient-points controls both serialize losslessly onto the engine's
        # spatially-varying path (§7.5 head-anchor method: anchor head per station, linear head
        # interpolation between). A config error here falls back to a flat default; _start_run
        # re-validates and blocks with the message. (The legacy 4-corner mode is gone — a stale
        # kept value rides the points branch on the same corner numerics.)
        from hype_app import gradients as grad_mod
        base["boundary_condition_mode"] = BC_PROFILE     # the engine's mode name
        try:
            cfg = _gradient_config()
            base["left_boundary_gradient_profile"] = grad_mod.serialize_profile(cfg.left_controls)
            base["right_boundary_gradient_profile"] = grad_mod.serialize_profile(cfg.right_controls)
        except Exception:  # noqa: BLE001
            base["left_boundary_gradient_profile"] = "0,0.005 1,0.005"
            base["right_boundary_gradient_profile"] = "0,0.005 1,0.005"
        return base

    @reactive.calc
    def grid_estimate():
        g = _domain_gdf_4326()
        crs = proj_crs()
        if g is None or crs is None:
            return None
        try:
            return estimate.estimate_cells(g.to_crs(crs), float(input.cell_size()),
                                           float(input.gw_mod_depth()), float(input.z()))
        except Exception:  # noqa: BLE001
            return None

    # ---- 3D mesh preview (server builds geometry in pure numpy → vtk.js renders client-side).
    # The build runs in a SPAWNED CHILD PROCESS: the engine discretization allocates full-grid
    # arrays, and an over-fine cell size used to OOM-kill the whole app — now the child dies
    # alone, and Cancel can hard-kill it mid-build. ----
    @reactive.extended_task
    async def mesh_task(payload: dict) -> dict:
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=mesh.child_build, args=(payload, q), daemon=True)
            _mesh3d_proc["p"] = p
            p.start()
            result = error = None
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:                       # drain whatever was queued right before exit
                try:
                    kind, data = q.get_nowait()
                    if kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            cancelled = _mesh3d_proc.pop("cancelled", False)
            _mesh3d_proc["p"] = None
            if cancelled:
                return {"cancelled": True}
            if error is not None:
                return {"error": error}
            if result is None:
                return {"error": "The mesh build stopped unexpectedly (likely out of memory). "
                                 "Try a coarser cell size."}
            return result
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    def _compute_mesh():
        if not _clicked_dynamic("compute_mesh"):
            return
        build = _domain_build()
        if not (build and dem_path() and proj_crs() is not None):
            ui.notification_show("Need the four boundaries and terrain first.",
                                 type="warning", duration=5)
            return
        est = grid_estimate()   # same red band that blocks Run — cloud refuses, desktop proceeds
        if not runmode.IS_DESKTOP and est and estimate.band(est["n_cells"]) == "red":
            ui.notification_show(estimate.band_message(est), type="error", duration=10)
            return
        stage.set("Building the 3D mesh…")
        _origin, _z0 = _scene_frame()          # shared z datum → vexag-safe layer alignment
        _grid_auto["on"] = False               # button build — full notifications + 3-D flip
        mesh_task({
            "domain": build["domain"],
            "sides": {k: build[k] for k in ("up", "left", "right", "down")},
            "dem": active_dem(), "crs": proj_crs().to_wkt(),
            "cell_size": float(_safe("cell_size", 10.0)),
            "depth": float(_safe("gw_mod_depth", 6.0)), "z": float(_safe("z", 0.25)),
            "origin": model_origin_effective(),
            "scene_z0": _z0,
        })

    _wire_state = reactive.value(False)

    @reactive.effect
    @reactive.event(input.grid_wireframe, ignore_init=True)
    async def _grid_wireframe_toggle():
        # Model-grid pane checkbox -> 3-D wireframe (applyWireframe in mesh3d.js). @reactive.event
        # takes the dependency robustly (a bare try/except read swallows the pre-render
        # SilentException AND its dependency, so the effect never re-fires). Guarded so pane
        # remounts (which re-register the input at its kept value) don't re-send.
        v = bool(input.grid_wireframe())
        if v == _wire_state():
            return
        _wire_state.set(v)
        await session.send_custom_message("hype3d_wire", {"on": v})

    # 3-D grid style (Model grid pane, "3D display"): opacity on the body+top actors,
    # color on the wireframe lines and surface cell edges. None color = the stock
    # elevation coloring. Session-scoped like the wireframe toggle.
    grid_opacity3d_v = reactive.value(1.0)
    grid_color3d_v = reactive.value(None)

    async def _send_grid_style():
        with reactive.isolate():
            await session.send_custom_message("hype3d_grid_style", {
                "opacity": float(grid_opacity3d_v()),
                "color": grid_color3d_v()})

    @reactive.effect
    @reactive.event(input.grid_opacity3d, ignore_init=True)
    async def _grid_opacity3d_change():
        try:
            v = max(0.05, min(float(input.grid_opacity3d()), 1.0))
        except Exception:  # noqa: BLE001
            return
        if abs(v - float(grid_opacity3d_v())) < 1e-9:
            return          # pane remounts re-register at the kept value: no re-send
        grid_opacity3d_v.set(v)
        await _send_grid_style()

    @reactive.effect
    @reactive.event(input.grid_color3d_evt)    # nonce event input: no ignore_init
    async def _grid_color3d_change():
        c = (input.grid_color3d_evt() or {}).get("c")
        if c == "default":
            c = None
        elif not isinstance(c, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            return
        if c == grid_color3d_v():
            return
        grid_color3d_v.set(c)
        await _send_grid_style()

    @reactive.effect
    def _cancel_mesh3d():
        if not _clicked_dynamic("mesh3d_cancel"):
            return
        p = _mesh3d_proc.get("p")
        if p is not None:
            _mesh3d_proc["cancelled"] = True
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        ui.notification_show("Mesh preview cancelled.", duration=3)

    @reactive.effect
    async def _mesh_done():
        if mesh_task.status() in ("initial", "running"):
            return
        auto = _grid_auto["on"]          # run-triggered rebuild: quiet, and never force 3-D
        _grid_auto["on"] = False
        stage.set("")
        if mesh_task.status() == "error":
            try:
                mesh_task.result()
            except Exception as e:  # noqa: BLE001
                if auto:                 # the run itself succeeded — log, don't toast
                    log_lines.append("[auto grid] failed: " + str(e)[:300])
                    log_tick.set(len(log_lines))
                else:
                    ui.notification_show(f"Mesh build failed: {e}", type="error", duration=8)
            return
        try:
            g = mesh_task.result()
        except Exception:  # noqa: BLE001
            return
        if g.get("cancelled"):
            return
        if g.get("error"):
            if auto:
                log_lines.append("[auto grid] failed: " + str(g["error"])[:300])
                log_tick.set(len(log_lines))
                print("[auto grid] failed:", str(g["error"])[:300], flush=True)
            else:
                ui.notification_show(f"Mesh build failed: {g['error']}", type="error", duration=10)
            return
        mesh_geom.set(g)
        if not auto:                     # the run must not yank the user out of the 2-D map;
            if alt_view() is not None:   # entering 3-D always returns to the Basecase (D6)
                await _set_displayed_run(None, quiet=True)
                ui.notification_show("3D is available for the Basecase only. Returned to "
                                     "Basecase.", duration=6)
            view_mode_v.set("3d")        # the client stashes a hidden build until 3-D opens
        await session.send_custom_message("hype_mesh", g)
        # Sync the basemap drape to the Basemaps radio (imagery and topo are two textures on
        # one actor), so a fresh mesh honors the current choice instead of always showing
        # the imagery.
        await session.send_custom_message(
            "hype3d_vis", {"key": "basemap", "on": _eff_checked("base.imagery")})
        await session.send_custom_message(
            "hype3d_vis", {"key": "basemap_topo", "on": _eff_checked("base.topo")})

    def _wse_path():
        """Resolve the WSE raster the engine will use: the surface-model result, the uploaded
        raster, or the DEM clipped to the drawn wetted-extent polygon. None if unavailable."""
        if wse_mode_v() == "model":
            res = ras_result()
            p = (res or {}).get("wse_for_gw")
            if p and _safe("wetted_filter", True):
                fp = (wetted_filter_res() or {}).get("wse_path")
                if fp and Path(fp).exists():   # isolated pools nulled out for the GW model
                    p = fp
            return p if p and Path(p).exists() else None
        if wse_mode_v() == "upload":
            up = _safe("wse_upload", None)
            if not up:
                return None
            dst = work_dir / "inputs" / "wse_upload.tif"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(up[0]["datapath"], dst)
            return str(dst)
        feat = wse_extent_feat()
        if not feat or active_dem() is None:
            return None
        out = work_dir / "inputs" / "wse_extent.tif"
        out.parent.mkdir(parents=True, exist_ok=True)
        return dem.clip_dem_to_polygon(active_dem(), geometry.single_feature_gdf(feat), str(out))

    # ---- surface-water model (HEC-RAS 2025, in a worker thread; the solver is a subprocess) ----
    CFS_TO_CMS = 0.028316846592

    @reactive.calc
    def ras_slope_default():
        """DEM-derived prefill for the Normal Depth friction slope (None until derivable)."""
        build = _domain_build()
        if not (build and active_dem()):
            return None
        return ras_engine.default_friction_slope(active_dem(), build["up"], build["down"])

    @reactive.extended_task
    async def ras_task(payload: dict) -> dict:
        def _work():
            return ras_engine.run_surface_model_safe(
                payload, log=ras_log_lines.append,
                cancel_evt=_ras_cancel, proc_holder=_ras_proc,
                progress=_on_ras_progress)
        return await anyio.to_thread.run_sync(_work)

    @reactive.extended_task
    async def mesh_prev_task(payload: dict) -> dict:
        def _work():
            return ras_engine.build_mesh_preview_safe(
                payload, log=ras_log_lines.append, proc_holder=_mesh_proc)
        return await anyio.to_thread.run_sync(_work)

    def _clicked_dynamic(bid: str) -> bool:
        """Strict-increment click guard for action buttons living inside re-rendered output_ui
        containers (their counts reset to 0 on each re-render; @reactive.event would misfire —
        the shared-go_next footgun, see _continue_nav)."""
        try:
            n = int(input[bid]() or 0)
        except Exception:  # noqa: BLE001
            n = 0
        last = _nav_seen.get(bid, 0)
        if n != last:
            _nav_seen[bid] = n
            return n > last
        return False

    # Parameter inputs rendered with hard-coded defaults reset whenever their pane remounts —
    # a nuisance under the stepper, a real bug once panes remount on every tree selection.
    # One effect mirrors every listed input into _kept; pane builders prefill via _keep().
    # Open (project restore) overwrites _kept wholesale, so the mirror is change-guarded:
    # an input's stale registry value (it outlives the pane unbind) must not re-clobber the
    # restored parameter just because some OTHER input fired the effect.
    _kept: dict = {}
    _kept_seen: dict = {}      # last value the mirror saw per id (the change guard)
    _kept_ts: dict = {}        # monotonic of each id's last real change (vs _restore_stamp)
    _restore_stamp: dict = {}  # {"t": monotonic} of the last Open — see _safe's staleness rule
    _KEEP_IDS = ("address", "manual_da", "dem_res", "fp_mult", "bc_mode",
                 "g_ul", "g_ur", "g_dl", "g_dr",
                 "g_qual_left", "g_qual_right", "g_ref_slope",
                 "g_mult_slight", "g_mult_strong",
                 "usgs_region", "usgs_lat", "usgs_lon", "usgs_national",
                 "soil_policy", "use_soil_k", "soil_use_mode",
                 "alt_k_lo_on", "alt_k_hi_on", "alt_g_lo_on", "alt_g_hi_on",
                 "alt_k_lo", "alt_k_hi", "alt_g_lo", "alt_g_hi", "alt_combos",
                 "site_name", "site_analyst", "site_org", "site_date", "site_notes",
                 "report_fn_envelope",
                 "ras_flow", "ras_slope", "ras_n", "ras_cell", "ras_hours", "ras_dt",
                 "ras_out_min", "wetted_filter", "show_removed_pools",
                 "kh", "kv", "porosity", "use_kzones", "kzone_kh", "kzone_kv",
                 "cell_size", "gw_mod_depth", "z",
                 "grid_wireframe", "grid_opacity3d",
                 "carve_bw", "carve_depth", "carve_slope", "hz_ppc", "hz_sample",
                 "hz_iface_ppc",
                 "fn_do", "fn_no3", "fn_o2_rate", "fn_do_thresh", "fn_do_gate",
                 "fn_denit_rate", "fn_tau",
                 # One "Include in report" per calculator, one endpoint picker, one stream
                 # concentration per cited endpoint. All registry-derived above.
                 FN_POL_SELECT_ID, *FN_INCLUDE_IDS, *FN_POL_CONC_IDS)
    _KEEP_SET = frozenset(_KEEP_IDS)

    #: Inputs the user is allowed to empty. Everywhere else a None read is a parked/unmounted
    #: input and must not overwrite the kept value -- but for these the blank IS the value, and
    #: skipping it makes the field impossible to clear: the pane re-renders from `_kept`, puts the
    #: old number back in the DOM, and the binding echoes it straight to the server. That echo,
    #: not the read path, is what made a cleared screening input snap back.
    #: A checkbox is never blank, so neither the include toggles nor the endpoint picker belong
    #: here: a selectize with every chip removed legitimately reports [], which is a value, and the
    #: mirror stores it because [] is not None. `fn_tau` left when it became a radio, for the same
    #: reason: one of its three scenarios is always selected, so a None from it is a parked pane.
    _CLEARABLE_IDS = frozenset({"fn_do", "fn_no3", "fn_o2_rate", "fn_do_thresh", "fn_denit_rate",
                                *FN_POL_CONC_IDS})

    @reactive.effect
    def _keep_inputs():
        for _iid in _KEEP_IDS:
            try:
                v = input[_iid]()
            except Exception:  # noqa: BLE001
                continue
            if v is None and _iid not in _CLEARABLE_IDS:
                continue
            if v == _kept_seen.get(_iid, _MISSING):
                continue
            _kept_seen[_iid] = v
            _kept[_iid] = v
            _kept_ts[_iid] = time.monotonic()

    def _keep(iid: str, default):
        """Last user-set value of a pane input (or `default` before any) — remount-proof."""
        return _kept.get(iid, default)

    # ------------------------------------------------------------------
    # USGS StreamStats / NSS flow lookup (revision §5): modal fetch + insert
    # ------------------------------------------------------------------
    # Spawn-child (NOT to_thread): running the sync httpx chain on an in-process worker
    # wedged the session's flush pipeline in live testing — see hype_app/usgs_run.py.
    _usgs_proc: dict = {}

    @reactive.extended_task
    async def usgs_flow_task(payload: dict) -> dict:
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=usgs_run.child_run, args=(payload, q), daemon=True)
            _usgs_proc["p"] = p
            p.start()
            result = error = None
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "log":
                        print(f"[usgs] {data}")
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:
                try:
                    kind, data = q.get_nowait()
                    if kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            cancelled = _usgs_proc.pop("cancelled", False)
            _usgs_proc["p"] = None
            if cancelled:
                return {"cancelled": True}
            if error is not None:
                print(f"[usgs] lookup failed:\n{error}")
                return {"error": error.strip().splitlines()[-1] if error.strip() else "failed"}
            return result or {"error": "The USGS lookup stopped unexpectedly."}
        return await anyio.to_thread.run_sync(_work)

    def _usgs_outlet_latlon():
        """(lat, lon) to seed the outlet marker: the oriented reach's downstream end (§5.1)."""
        try:
            coords = ((reach_feat() or {}).get("geometry") or {}).get("coordinates") or []
            if coords:
                lon, lat = coords[-1][0], coords[-1][1]   # GeoJSON lon,lat; last vertex = downstream
                return float(lat), float(lon)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _evt_btn(evt_id, label, cls, *, disabled=False):
        """Event-nonce button for panes that re-render themselves. input_action_button counters
        RESET to 0 on every pane rebind, and a rebind landing in the same server batch as the
        click's increment swallows the click (observed live on the flow panel). A nonce'd
        setInputValue with priority:'event' survives rebinds — the tree.js pattern."""
        kw = {} if disabled else {
            "onclick": f"Shiny.setInputValue('{evt_id}', Date.now() + Math.random(), "
                       "{priority: 'event'})"}
        return ui.tags.button(label, type="button", class_=f"btn {cls}",
                              **({"disabled": ""} if disabled else {}), **kw)

    # The lookup UI is a MODAL. Its dynamic outputs (usgs_flow_body, usgs_review_map) must
    # render with @output(suspend_when_hidden=False): they live inside a late-bound ui.modal,
    # and with default suspension a hidden-at-registration output is never computed — the
    # session flush then wedges on it ("recalculating" forever). Commit 3d2f140 moved the old
    # flow UI inline for exactly that, before the dl_save/propspane eager-render fix was found.
    usgs_pick_v = reactive.value(None)      # candidate id picked by a table-row click
    usgs_flow_err = reactive.value(None)    # last lookup failure line, shown in the modal body
    usgs_modal_gen = reactive.value(0)      # bumped per modal open — rebuilds the review map
    _USGS_REGION_CHOICES = {"": "Auto-detect (from the point)", **region_choices()}

    @reactive.effect
    def _usgs_open():
        if not _clicked_dynamic("get_usgs_flow"):
            return
        pt = _usgs_outlet_latlon()
        if pt is None:
            ui.notification_show("Define the reach before looking up USGS flow statistics.",
                                 type="warning", duration=6)
            return
        with reactive.isolate():
            usgs_modal_gen.set(usgs_modal_gen() + 1)   # fresh map build for this open
        lat0, lon0 = pt
        sel0 = str(_keep("usgs_region", "") or "").strip().upper()
        if sel0 not in _USGS_REGION_CHOICES:
            sel0 = ""
        ui.modal_show(ui.modal(
            ui.div(
                ui.input_numeric("usgs_lat", "Outlet latitude",
                                 value=_keep("usgs_lat", round(lat0, 6)),
                                 min=-90, max=90, step=0.0001),
                ui.input_numeric("usgs_lon", "Outlet longitude",
                                 value=_keep("usgs_lon", round(lon0, 6)),
                                 min=-180, max=180, step=0.0001),
                ui.input_select("usgs_region", "StreamStats region",
                                choices=_USGS_REGION_CHOICES, selected=sel0),
                class_="hype-usgs-controls"),
            ui.input_checkbox("usgs_national",
                              "Also request national estimates for comparison",
                              value=bool(_keep("usgs_national", False))),
            ui.div(
                (output_widget("usgs_review_map", height="320px") if _HAS_MAP
                 else ui.div("Map preview unavailable.", class_="hype-instr")),
                ui.div(
                    ui.tags.strong("Review map"),
                    ui.div("Analysis reach", class_="hype-map-key reach"),
                    ui.div("Model domain", class_="hype-map-key domain"),
                    ui.div("Requested point (drag, then Fetch)",
                           class_="hype-map-key requested"),
                    ui.div("Delineated watershed + pour point",
                           class_="hype-map-key watershed"),
                    class_="hype-service-map-legend"),
                class_="hype-service-map-grid"),
            ui.output_ui("usgs_flow_body"),
            # Leaflet measures a zero/partial container when the widget mounts during the
            # modal fade-in and then draws a single tile — nudge it once the modal is fully
            # shown (plus a fallback tick; Leaflet re-measures on window resize).
            ui.tags.script(
                "(function(){var f=function(){window.dispatchEvent(new Event('resize'))};"
                "document.addEventListener('shown.bs.modal', f, {once: true});"
                "setTimeout(f, 400);})();"),
            title="USGS StreamStats flow review", size="xl", easy_close=False,
            footer=ui.TagList(
                # Recreated per modal_show — _clicked_dynamic guards (never @reactive.event;
                # the counter resets on every open, see confirm_new_project).
                ui.input_action_button("usgs_fetch", "Fetch statistics", class_="btn-primary"),
                ui.input_action_button("usgs_use", "Select and Close", class_="btn-success"),
                ui.input_action_button("usgs_modal_cancel", "Cancel",
                                       class_="btn-outline-secondary"))))

    if _HAS_MAP:
        @output(suspend_when_hidden=False)   # late-bound modal output — see the note above
        @render_widget
        def usgs_review_map():
            # ipyleaflet layer mutation is unreliable in this app — REBUILD the whole Map per
            # change instead of touching layers. Re-renders ONLY per modal open (the gen nonce;
            # the eager first render at session start predates any reach) and on flow_lookup
            # changes; the point/reach/domain are read isolated so typing or marker drags never
            # rebuild the map under the user (the dragged marker already moved client-side).
            usgs_modal_gen()
            fl = flow_lookup()
            with reactive.isolate():
                try:
                    pt = (float(input.usgs_lat()), float(input.usgs_lon()))
                except Exception:  # noqa: BLE001
                    pt = _usgs_outlet_latlon()
                lat, lon = pt or (39.5, -98.35)
                rch, dom = reach_feat(), domain_feat()
            m = Map(center=(lat, lon), zoom=13, scroll_wheel_zoom=True, zoom_control=False,
                    max_zoom=19, layout=Layout(height="320px"))
            m.clear()
            m.add(TileLayer(url=USGS_TOPO, name="USGS Topo", base=True, attribution=USGS_ATTR,
                            max_native_zoom=16, max_zoom=19))
            m.add(ZoomControl(position="topright"))
            m.add(ScaleControl(position="bottomright"))
            ws, pour = watershed_display_features((fl or {}).get("watershed_geojson"))
            if ws:
                m.add(GeoJSON(data=ws, style=USGS_WATERSHED_STYLE, name="Delineated watershed"))
            if dom:
                m.add(GeoJSON(data={"type": "FeatureCollection", "features": [dom]},
                              style=DOMAIN_STYLE, name="Model domain"))
            if rch:
                m.add(GeoJSON(data={"type": "FeatureCollection", "features": [rch]},
                              style=REACH_STYLE, name="Analysis reach"))
            if pour:
                m.add(Marker(location=pour, draggable=False, name="Pour point",
                             icon=DivIcon(html='<span class="hype-service-marker snapped" '
                                               'title="Delineated pour point"></span>',
                                          icon_size=[18, 18], icon_anchor=[9, 9])))
            mk = Marker(location=(lat, lon), draggable=True, name="Requested point",
                        icon=DivIcon(html='<span class="hype-service-marker requested" '
                                          'title="Requested point — drag, then Fetch"></span>',
                                     icon_size=[18, 18], icon_anchor=[9, 9]))

            def _dragged(change):
                # Runs server-side (shinywidgets dispatches widget comms inside a reactive
                # effect — same mechanism as the main map's click capture). Update the
                # numerics ONLY; the refetch stays manual.
                if change.get("name") != "location" or not change.get("new"):
                    return
                try:
                    nlat, nlon = (float(v) for v in change["new"])
                except Exception:  # noqa: BLE001
                    return
                ui.update_numeric("usgs_lat", value=round(nlat, 6))
                ui.update_numeric("usgs_lon", value=round(nlon, 6))

            mk.observe(_dragged, names="location")
            m.add(mk)
            return m

    @output(suspend_when_hidden=False)       # late-bound modal output — see the note above
    @render.ui
    def usgs_flow_body():
        if usgs_flow_task.status() == "running":
            return ui.TagList(
                ui.div(ui.div(class_="hype-spinner"),
                       ui.span("Contacting USGS StreamStats… watershed delineation can "
                               "take ~30 s."), class_="hype-busy"),
                # Nonce button, NOT input_action_button: it re-renders with this output, so a
                # plain button's click counter would reset (see _evt_btn).
                ui.div(_evt_btn("usgs_cancel_evt", "Cancel lookup", "btn-sm btn-outline-danger"),
                       class_="hype-actions"))
        err = usgs_flow_err()
        if err:
            return ui.div(f"USGS lookup failed: {err}", class_="hype-warn")
        fl = flow_lookup()
        if not fl:
            return ui.div("Fetch to review peak-flow statistics for this outlet.",
                          class_="hype-instr")
        import json as _json
        body = []
        bits = []
        if fl.get("selected_region"):
            bits.append(f"Region {fl['selected_region']}")
        if fl.get("basin_characteristics", {}).get("DRNAREA") is not None:
            bits.append(f"drainage area {fl['basin_characteristics']['DRNAREA']:g} mi²")
        if bits:
            body.append(ui.div(" · ".join(bits), class_="hype-instr"))
        for w in [w.get("message", "") for w in (fl.get("warnings") or [])][:4]:
            body.append(ui.div(w, class_="hype-warn"))
        cands = fl.get("candidates") or []
        if not cands:
            body.append(ui.div("No flow statistics returned for this point.",
                               class_="hype-instr"))
            return ui.TagList(*body)
        pick = usgs_pick_v()
        region_lbl = fl.get("selected_region") or "Regional"
        rows = []
        for c in cands:
            cid = c["id"]
            cfs, cms, recur = c.get("value_cfs"), c.get("value_cms"), c.get("recurrence_years")
            ok = bool(c.get("insertable"))
            flags = [f for f, on in (("national", c.get("is_national")),
                                     ("extrapolated", c.get("is_extrapolated"))) if on]
            status = ", ".join(flags + ([] if ok else ["not insertable"])) or "ok"
            attrs = {"class_": "hype-flow-row"
                               + ((" sel" if cid == pick else "") if ok else " disabled")}
            if ok:   # the click only posts the id — the .sel highlight is server-re-rendered
                attrs["onclick"] = ("Shiny.setInputValue('usgs_row_pick', "
                                    f"{_json.dumps(cid)}, {{priority: 'event'}})")
            rows.append(ui.tags.tr(
                ui.tags.td(c.get("description") or c.get("statistic_code") or cid),
                ui.tags.td(f"{recur:g}-yr" if isinstance(recur, (int, float)) else "—"),
                ui.tags.td(f"{cfs:.1f}" if isinstance(cfs, (int, float)) else "—"),
                ui.tags.td(f"{cms:.3f}" if isinstance(cms, (int, float)) else "—"),
                ui.tags.td("National" if c.get("is_national") else region_lbl),
                ui.tags.td(status), **attrs))
        body.append(ui.div("Click a row to choose the discharge to insert; greyed rows can't "
                           "populate the flow input.", class_="hype-instr"))
        body.append(ui.tags.table(
            ui.tags.thead(ui.tags.tr(*[ui.tags.th(h) for h in
                ("Statistic", "Recurrence", "Flow (cfs)", "Flow (m³/s)", "Source", "Status")])),
            ui.tags.tbody(*rows), class_="table table-sm hype-flow-table"))
        return ui.TagList(*body)

    @reactive.effect
    @reactive.event(input.usgs_row_pick)
    def _usgs_row_pick():
        usgs_pick_v.set(str(input.usgs_row_pick()))

    @reactive.effect
    def _usgs_fetch():
        if not _clicked_dynamic("usgs_fetch"):
            return
        if usgs_flow_task.status() == "running":
            ui.notification_show("A lookup is already running — cancel it first.",
                                 type="warning")
            return
        try:
            lat, lon = float(input.usgs_lat()), float(input.usgs_lon())
        except Exception:  # noqa: BLE001
            ui.notification_show("Enter a valid latitude and longitude.", type="warning")
            return
        region = str(input.usgs_region() or "").strip().upper()   # select: "" = auto-detect
        if region:
            _kept["usgs_region"] = region
        try:
            want_nat = bool(input.usgs_national())
        except Exception:  # noqa: BLE001
            want_nat = False
        usgs_flow_err.set(None)
        usgs_pick_v.set(None)
        flow_lookup.set(None)     # clears the stale table AND rebuilds the review map bare
        print("[usgs] invoking lookup task")
        usgs_flow_task({"region": region, "lat": lat, "lon": lon, "want_national": want_nat,
                        "cache_dir": str(work_dir / "data_sources" / "usgs")})

    @reactive.effect
    @reactive.event(input.usgs_cancel_evt)
    def _usgs_cancel_click():
        p = _usgs_proc.get("p")
        if p is not None and p.is_alive():
            _usgs_proc["cancelled"] = True
            p.kill()

    @reactive.effect
    def _usgs_modal_cancel():
        if not _clicked_dynamic("usgs_modal_cancel"):
            return
        print("[usgs] modal cancel clicked")
        p = _usgs_proc.get("p")
        if p is not None and p.is_alive():
            _usgs_proc["cancelled"] = True
            p.kill()
        ui.modal_remove()

    @reactive.effect
    def _usgs_done():
        if usgs_flow_task.status() in ("initial", "running"):
            return
        if usgs_flow_task.status() == "error":
            usgs_flow_err.set("service error — check the point/region and try again.")
            ui.notification_show("USGS lookup failed — check the point/region and try again.",
                                 type="error", duration=8)
            return
        try:
            res = usgs_flow_task.result()
        except Exception:  # noqa: BLE001
            return
        if isinstance(res, dict) and res.get("cancelled"):
            usgs_flow_err.set(None)
            return
        if isinstance(res, dict) and res.get("error"):
            usgs_flow_err.set(str(res["error"]))
            ui.notification_show(f"USGS lookup failed: {res['error']}", type="error", duration=8)
            return
        flow_lookup.set(res)
        resolved = str(res.get("selected_region") or "").upper()
        if resolved and resolved in _USGS_REGION_CHOICES:
            # Reflect the auto-detected region in the (possibly open) modal + the kept mirror,
            # so a reopened modal renders it server-side even if the update landed while closed.
            _kept["usgs_region"] = resolved
            ui.update_select("usgs_region", selected=resolved)

    @reactive.effect
    def _usgs_use():
        if not _clicked_dynamic("usgs_use"):
            return
        fl = flow_lookup()
        if not fl:
            ui.notification_show("Fetch flow statistics first.", type="warning")
            return
        pick = usgs_pick_v()
        print(f"[usgs] use clicked, pick={pick!r}")
        cand = next((c for c in (fl.get("candidates") or []) if c.get("id") == pick), None)
        if not cand or not isinstance(cand.get("value_cfs"), (int, float)):
            ui.notification_show("Click a row to select an insertable discharge statistic "
                                 "first.", type="warning")
            return
        cfs = round(float(cand["value_cfs"]), 2)
        ui.update_numeric("ras_flow", value=cfs)
        _kept["ras_flow"] = cfs
        fl = dict(fl); fl["selected_candidate_id"] = pick
        flow_lookup.set(fl)
        flow_source.set({"source": "USGS StreamStats", "candidate_id": pick, "inserted_cfs": cfs})
        try:
            import json as _json
            d = work_dir / "data_sources" / "usgs"
            d.mkdir(parents=True, exist_ok=True)
            (d / "flow_lookup.json").write_text(_json.dumps(fl, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
        ui.modal_remove()
        ui.notification_show(f"Inserted {cfs:g} cfs from USGS StreamStats.", duration=6)

    # (No pane note for the flow source — flow_source still feeds the frozen run snapshot;
    # the modal's notification is the only insert feedback.)

    # ------------------------------------------------------------------
    # NRCS soils fetch + review (revision §6.1–6.3): SSURGO layer + attributes
    # ------------------------------------------------------------------
    _soil_proc: dict = {}

    @reactive.extended_task
    async def soil_task(payload: dict) -> dict:
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=soil_run.child_run, args=(payload, q), daemon=True)
            _soil_proc["p"] = p
            _soil_proc.pop("killed", None)        # stale flag from an earlier cancel
            p.start()
            result = error = None
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "log":
                        print(f"[soil] {data}")
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:                           # drain anything queued right before exit
                try:
                    kind, data = q.get_nowait()
                    if kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            exitcode = p.exitcode
            _soil_proc["p"] = None
            killed = bool(_soil_proc.pop("killed", False))
            if error is not None:
                # New child protocol: {"message", "trace"}. Tolerate a legacy plain string.
                msg = error.get("message") if isinstance(error, dict) else None
                trace = error.get("trace") if isinstance(error, dict) else error
                print(f"[soil] fetch failed:\n{trace or error}")
                return {"error": str(msg or trace or error)}
            if result is not None:
                return result
            if killed:                            # the modal's Cancel, not a failure
                return {"cancelled": True}
            # Died without a word — a hard crash leaves only the exit code behind. Windows
            # NTSTATUS codes (e.g. an access violation, 0xC0000005) only read in hex.
            code = "unknown" if exitcode is None else (
                f"0x{exitcode & 0xFFFFFFFF:08X}"
                if (exitcode < 0 or exitcode > 0xFFFF) else str(exitcode))
            return {"error": "The soils fetch process ended unexpectedly "
                             f"(exit code {code}). Try again."}
        return await anyio.to_thread.run_sync(_work)

    # ---- NRCS soils review MODAL (opened from the Subsurface-properties pane) ----
    # Same skeleton as the USGS flow modal: gen-nonce rebuilt review map, late-bound outputs
    # with suspend_when_hidden=False, footer buttons guarded by _clicked_dynamic. The soils
    # never touch the main map — the modal's own map is the review surface.
    soil_modal_gen = reactive.value(0)         # bumped per modal open — rebuilds the review map
    soil_sel_units = reactive.value(frozenset())   # mukeys ticked for K-zone import
    soil_source = reactive.value(None)         # {"mode","policy","units"} — the applied decision
    soil_fetch_err = reactive.value(None)      # last fetch failure line, shown in the modal body
    soil_inspect = reactive.value(None)        # mukey under inspection (transient, never saved)

    def _soil_unit_k(mu, policy, depth_m, aniso, fallback_kh, fallback_kv):
        """Representative (KH, KV, derived?) for one map unit over the top `depth_m` of the
        profile — the same horizon-intersection math the run's K builder uses, collapsed to a
        single layer, so the review table shows the model-ready number a zone import commits."""
        from hype_app import soil_profile as sp
        comps = (mu or {}).get("components") or []
        if not comps:
            return fallback_kh, fallback_kv, False
        if policy == "weighted":
            tot = sum((c.get("comppct_r") or 0.0) for c in comps) or 1.0
            pairs = [(c, (c.get("comppct_r") or 0.0) / tot)
                     for c in comps if (c.get("comppct_r") or 0.0) > 0] or [(comps[0], 1.0)]
        else:
            majors = [c for c in comps if c.get("major")] or comps
            pairs = [(max(majors, key=lambda c: c.get("comppct_r") or 0.0), 1.0)]
        kh_acc, kv_inv, derived = 0.0, 0.0, False
        for c, w in pairs:
            horizons = [{"top_cm": h.get("top_cm"), "bottom_cm": h.get("bottom_cm"),
                         "ksat_um_s": h.get("ksat_um_s")} for h in (c.get("horizons") or [])]
            segs = sp.intersect_layer_horizons(0.0, -float(depth_m), 0.0, horizons, aniso,
                                               fallback_kh=fallback_kh, fallback_kv=fallback_kv)
            if segs:
                kh_c, kv_c, origin = sp.aggregate_segments(segs)
                if getattr(origin, "value", str(origin)) == "derived":
                    derived = True
            else:
                kh_c, kv_c = fallback_kh, fallback_kv
            kh_acc += w * kh_c                             # arithmetic across components
            if kv_c > 0:
                kv_inv += w / kv_c                         # harmonic across components
        kv = (1.0 / kv_inv) if kv_inv > 0 else fallback_kv
        return kh_acc, kv, derived

    def _soil_k_inputs():
        """(policy, depth_m, aniso, fallback_kh, fallback_kv) from the live pane values."""
        kv0 = float(_safe("kv", 1.0)) or 1.0
        kh0 = float(_safe("kh", 10.0))
        return (str(_safe("soil_policy", "dominant")), float(_safe("gw_mod_depth", 6.0)),
                kh0 / kv0, kh0, kv0)

    @reactive.effect
    def _soils_open():
        if not _clicked_dynamic("get_nrcs_soils"):
            return
        if not domain_feat():
            ui.notification_show("Generate the domain boundaries first.", type="warning",
                                 duration=6)
            return
        with reactive.isolate():
            soil_modal_gen.set(soil_modal_gen() + 1)   # fresh map build for this open
        mode0 = str(_keep("soil_use_mode",
                          "aggregated" if bool(_kept.get("use_soil_k")) else "none"))
        ui.modal_show(ui.modal(
            # Modal-local CSS (report-modal precedent): the standing decision keeps
            # .modal-dialog/.modal-body untouched globally, so fixed-height internals
            # (table scroll, profile panel) are scoped here and die with the modal.
            ui.tags.style(
                "#shiny-modal .hype-soil-tablewrap{max-height:230px;overflow-y:auto;"
                "margin-top:var(--sp-2)}"
                "#shiny-modal .hype-soil-tablewrap thead th{position:sticky;top:0;"
                "background:#fff;z-index:1}"
                "#shiny-modal .hype-soil-units{margin-bottom:0}"
                "#shiny-modal .hype-soil-units th.hype-soil-pick,"
                "#shiny-modal .hype-soil-units td.hype-soil-pick{display:none}"
                "#shiny-modal.hype-soil-zones .hype-soil-units th.hype-soil-pick,"
                "#shiny-modal.hype-soil-zones .hype-soil-units td.hype-soil-pick"
                "{display:table-cell}"
                "#shiny-modal:not(.hype-soil-zones) .hype-map-key.soilsel{display:none}"
                "#shiny-modal .hype-soil-footnote{font-size:var(--fs-1);"
                "color:var(--hype-text-muted);margin:4px 0 var(--sp-3)}"
                "#shiny-modal .hype-soil-detailpanel{height:190px;display:flex;"
                "flex-direction:column;gap:4px;margin-top:var(--sp-3);"
                "padding:var(--sp-2) var(--sp-3);border:1px solid var(--hype-border);"
                "border-radius:var(--r-1);background:var(--hype-surface-soft)}"
                "#shiny-modal .hype-soil-detailpanel #soil_detail{flex:1 1 auto;"
                "min-height:0;overflow-y:auto}"
                "#shiny-modal .hype-soil-table{font-size:var(--fs-1);margin:2px 0 var(--sp-2)}"
                "#shiny-modal .hype-soil-table th{white-space:nowrap}"
                "#shiny-modal .hype-soil-comp{margin:var(--sp-2) 0}"
                "#shiny-modal .hype-soil-comp+.hype-soil-comp{border-top:1px solid "
                "var(--hype-rule);padding-top:var(--sp-2)}"
                "#shiny-modal .hype-soil-fetchsum{margin-top:var(--sp-3);"
                "padding-top:var(--sp-2);border-top:1px solid var(--hype-rule);"
                "font-size:var(--fs-1);color:var(--hype-text-muted)}"),
            ui.div(
                (output_widget("soils_review_map", height="320px") if _HAS_MAP
                 else ui.div("Map preview unavailable.", class_="hype-instr")),
                ui.div(
                    ui.tags.strong("Review map"),
                    ui.div("Analysis reach", class_="hype-map-key reach"),
                    ui.div("Model domain", class_="hype-map-key domain"),
                    ui.div("SSURGO soils", class_="hype-map-key soils"),
                    ui.div("Selected for import", class_="hype-map-key soilsel"),
                    ui.output_ui("soil_fetch_summary"),
                    class_="hype-service-map-legend"),
                class_="hype-service-map-grid"),
            ui.output_ui("soils_modal_body"),
            # Leaflet measures a zero/partial container during the modal fade-in — nudge it
            # once fully shown (same fix as the flow modal).
            ui.tags.script(
                "(function(){var f=function(){window.dispatchEvent(new Event('resize'))};"
                "document.addEventListener('shown.bs.modal', f, {once: true});"
                "setTimeout(f, 400);})();"),
            # Zones-mode class on #shiny-modal (drives the pick column + legend key,
            # survives body re-renders), the row-highlight helper, and the map-click
            # highlight sync. The radio mounts post-fetch, hence the delegated listener,
            # attached to the modal element so it dies with the modal.
            ui.tags.script(
                "(function(){var z0=" + ("true" if mode0 == "zones" else "false") + ";"
                "var root=document.getElementById('shiny-modal');if(!root)return;"
                "root.classList.toggle('hype-soil-zones',z0);"
                "root.addEventListener('change',function(e){"
                "if(e.target&&e.target.name==='soil_use_mode')"
                "root.classList.toggle('hype-soil-zones',e.target.value==='zones');});"
                "window.hypeSoilSel=function(mk){"
                "root.querySelectorAll('.hype-soil-units tr.hype-flow-row')"
                ".forEach(function(tr){tr.classList.toggle('sel',"
                "mk!=null&&tr.getAttribute('data-mukey')===String(mk));});};"
                "Shiny.addCustomMessageHandler('hype_soil_insp',function(m){"
                "window.hypeSoilSel(m?m.mukey:null);});})();"),
            title="NRCS soils review (SSURGO)", size="xl", easy_close=False,
            footer=ui.TagList(
                # Recreated per modal_show — _clicked_dynamic guards (never @reactive.event).
                ui.input_action_button("soils_fetch", "Fetch NRCS soils", class_="btn-primary"),
                ui.input_action_button("soils_apply", "Apply and Close", class_="btn-success"),
                ui.input_action_button("soils_modal_cancel", "Cancel",
                                       class_="btn-outline-secondary"))))

    if _HAS_MAP:
        @output(suspend_when_hidden=False)   # late-bound modal output (see the flow-modal note)
        @render_widget
        def soils_review_map():
            # Full rebuild per change, like usgs_review_map: per open (gen nonce), on fetch
            # results, and on import-selection changes (restyles the picked units).
            soil_modal_gen()
            snap = soil_snapshot()
            sel = soil_sel_units()
            with reactive.isolate():
                rch, dom = reach_feat(), domain_feat()
            center = (39.5, -98.35)
            try:
                ring = ((dom or rch).get("geometry") or {}).get("coordinates")
                while ring and isinstance(ring[0][0], (list, tuple)):
                    ring = ring[0]
                xs = [c[0] for c in ring]
                ys = [c[1] for c in ring]
                center = (sum(ys) / len(ys), sum(xs) / len(xs))
            except Exception:  # noqa: BLE001
                pass
            m = Map(center=center, zoom=14, scroll_wheel_zoom=True, zoom_control=False,
                    max_zoom=19, layout=Layout(height="320px"))
            m.clear()
            m.add(TileLayer(url=USGS_TOPO, name="USGS Topo", base=True, attribution=USGS_ATTR,
                            max_native_zoom=16, max_zoom=19))
            m.add(ZoomControl(position="topright"))
            m.add(ScaleControl(position="bottomright"))
            polys = (snap or {}).get("polygons") or []
            if polys:
                feats = [{"type": "Feature", "geometry": p["geometry"],
                          "properties": {"mukey": p.get("mukey"),
                                         "style": (SOILS_SEL_STYLE if p.get("mukey") in sel
                                                   else SOILS_STYLE)}}
                         for p in polys if p.get("geometry")]
                # No layer-level style= here: ipyleaflet merges it OVER every per-feature
                # properties.style, which is exactly what kept SOILS_SEL_STYLE invisible.
                lyr = GeoJSON(data={"type": "FeatureCollection", "features": feats},
                              hover_style=SOILS_HOVER, name="SSURGO soils")
                lyr.on_click(_on_soil_poly_click)
                m.add(lyr)
            if dom:
                m.add(GeoJSON(data={"type": "FeatureCollection", "features": [dom]},
                              style=DOMAIN_STYLE, name="Model domain"))
            if rch:
                m.add(GeoJSON(data={"type": "FeatureCollection", "features": [rch]},
                              style=REACH_STYLE, name="Analysis reach"))
            return m

    @output(suspend_when_hidden=False)       # late-bound modal output (see the flow-modal note)
    @render.ui
    def soils_modal_body():
        import json as _json
        snap = soil_snapshot()
        sel = soil_sel_units()
        if soil_task.status() == "running":
            return ui.div(ui.div(class_="hype-spinner"),
                          ui.span("Querying NRCS Soil Data Access…", class_="hype-run-label"),
                          class_="hype-run-head")
        if not snap:
            err = soil_fetch_err()
            return ui.TagList(
                (ui.div(err, class_="hype-warn") if err else None),
                ui.div("No soils fetched yet. Click Fetch NRCS soils below.",
                       class_="hype-instr"))
        polys, mus = snap.get("polygons") or [], snap.get("map_units") or []
        # Service-side diagnostics ride the snapshot (no coverage, skipped geometry...) —
        # surface them here so "0 polygons" arrives with its explanation.
        diags = [(str(w.get("message")), str(w.get("severity") or ""))
                 for w in (snap.get("missing_diagnostics") or [])
                 if isinstance(w, dict) and w.get("message")]
        policy, depth_m, aniso, kh0, kv0 = _soil_k_inputs()
        with reactive.isolate():
            insp = soil_inspect()   # isolated: inspecting must never re-render the body,
        #                             but natural rebuilds repaint the row highlight
        n_by_mu: dict = {}
        for p in polys:
            n_by_mu[p.get("mukey")] = n_by_mu.get(p.get("mukey"), 0) + 1
        rows = []
        for mu in mus:
            mk = str(mu.get("mukey"))
            zkh, zkv, derived = _soil_unit_k(mu, policy, depth_m, aniso, kh0, kv0)
            picked = mk in sel
            rows.append(ui.tags.tr(
                # The pick cell owns the import toggle (zones mode only, CSS-gated).
                # Row click elsewhere = inspect: instant client highlight + server event.
                ui.tags.td("☑" if picked else "☐", class_="hype-soil-pick",
                           onclick=("event.stopPropagation();"
                                    "Shiny.setInputValue('soil_unit_pick', "
                                    f"{_json.dumps(mk)}, {{priority: 'event'}})")),
                ui.tags.td(f"{mu.get('musym') or mk} · {mu.get('name') or ''}"),
                ui.tags.td(str(n_by_mu.get(mu.get("mukey"), 0))),
                ui.tags.td(f"{zkh:.2f}" + ("" if derived else " *")),
                ui.tags.td(f"{zkv:.3f}" + ("" if derived else " *")),
                class_="hype-flow-row" + (" sel" if mk == insp else ""),
                data_mukey=mk,
                onclick=(f"window.hypeSoilSel({_json.dumps(mk)});"
                         "Shiny.setInputValue('soil_unit_inspect', "
                         f"{_json.dumps(mk)}, {{priority: 'event'}})")))
        units = ui.tags.table(
            ui.tags.thead(ui.tags.tr(
                ui.tags.th("", class_="hype-soil-pick"), ui.tags.th("Map unit"),
                ui.tags.th("Polygons"),
                ui.tags.th(f"KH m/d (top {depth_m:g} m)"), ui.tags.th("KV m/d"))),
            ui.tags.tbody(*rows), class_="table table-sm hype-flow-table hype-soil-units")
        return ui.TagList(
            *[ui.div(msg, class_=("hype-warn" if sev in ("warning", "error")
                                  else "hype-instr hype-dim"))
              for msg, sev in diags],
            ui.div(units, class_="hype-soil-tablewrap"),
            ui.div("* uses the pane KH and KV where the profile has no data",
                   class_="hype-soil-footnote"),
            ui.div(
                ui.input_radio_buttons(
                    "soil_use_mode", "How should these soils set the model K?",
                    {"none": "Do not use these Ks",
                     "aggregated": "Single K aggregated from NRCS soils data",
                     "zones": ui.TagList(
                         "Import NRCS soils as separate K-zones",
                         # The tip must not toggle the radio when clicked (it sits
                         # inside the option's label).
                         ui.tags.span(
                             _info_tip("Each selected map unit becomes an editable "
                                       "K-zone. Hand-drawn K-zones always override "
                                       "the soils where they overlap."),
                             onclick="event.preventDefault();event.stopPropagation();"))},
                    selected=str(_keep("soil_use_mode",
                                       "aggregated" if bool(_kept.get("use_soil_k"))
                                       else "none"))),
                # Client-side reveals. The outer indexOf form fails safe on the first
                # post-fetch render, when the radio value is not yet in the client cache
                # (undefined !== 'none' would flash the select).
                ui.panel_conditional(
                    "['aggregated','zones'].indexOf(input.soil_use_mode) > -1",
                    ui.input_select("soil_policy", "Aggregation method",
                                    {"dominant": "Dominant component",
                                     "weighted": "Weighted by component %"},
                                    selected=str(_keep("soil_policy", "dominant"))),
                    ui.panel_conditional("input.soil_use_mode === 'aggregated'",
                                         ui.output_ui("soil_k_coverage_note")),
                    ui.panel_conditional("input.soil_use_mode === 'zones'",
                                         ui.output_ui("soil_zone_count"))),
                class_="hype-soil-decide"),
            ui.div(ui.tags.strong("Soil profile"),
                   ui.output_ui("soil_detail"),
                   class_="hype-soil-detailpanel"))

    @reactive.effect
    @reactive.event(input.soil_unit_pick)
    def _soil_unit_pick():
        mk = str(input.soil_unit_pick())
        cur = set(soil_sel_units())
        cur.symmetric_difference_update({mk})
        soil_sel_units.set(frozenset(cur))

    @reactive.effect
    @reactive.event(input.soil_unit_inspect)
    def _soil_unit_inspect():
        soil_inspect.set(str(input.soil_unit_inspect()))

    def _on_soil_poly_click(**kw):
        # ipyleaflet click callbacks run inside the shinywidgets comm effect, so a plain
        # reactive set is safe here (the hz-path click precedent).
        mk = ((kw.get("feature") or {}).get("properties") or {}).get("mukey")
        if mk is not None:
            soil_inspect.set(str(mk))

    @reactive.effect
    async def _push_soil_insp():
        # send_custom_message is async-only, so the sync click paths set the reactive and
        # this effect relays it. The modal-local handler moves the row highlight (and
        # no-ops with the modal closed).
        mk = soil_inspect()
        try:
            await session.send_custom_message("hype_soil_insp", {"mukey": mk})
        except Exception:  # noqa: BLE001
            pass

    @reactive.effect
    def _soils_fetch():
        if not _clicked_dynamic("soils_fetch"):
            return
        dom = domain_feat()
        if not dom:
            ui.notification_show("Generate the domain boundaries first.", type="warning")
            return
        try:
            crs = proj_crs()
            epsg = crs.to_epsg() if crs else None
            kv = float(_safe("kv", 1.0))
            aniso = (float(_safe("kh", 10.0)) / kv) if kv else None
            payload = {
                "domain_geojson": dom.get("geometry") or dom,
                "working_crs_epsg": int(epsg) if epsg else None,
                "anisotropy_ratio": aniso,
                "cache_dir": str(work_dir / "data_sources" / "nrcs"),
            }
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Couldn't start soils fetch: {e}", type="error")
            return
        soil_fetch_err.set(None)
        soil_snapshot.set(None)
        soil_sel_units.set(frozenset())
        soil_inspect.set(None)                # a new snapshot invalidates the mukeys
        soil_task(payload)

    @reactive.effect
    def _soil_done():
        status = soil_task.status()
        if status in ("initial", "running", "cancelled"):
            return
        if status == "error":
            # The task itself raised (not the child) — result() re-raises the exception.
            try:
                soil_task.result()
                detail = ""
            except Exception as e:  # noqa: BLE001
                detail = str(e).strip()
            msg = ("NRCS soils fetch failed" + (f": {detail}" if detail else "."))[:220]
            soil_fetch_err.set(msg)
            ui.notification_show(msg, type="error", duration=8)
            return
        try:
            res = soil_task.result()
        except Exception:  # noqa: BLE001 — defensive only, non-success handled above
            return
        if isinstance(res, dict) and res.get("cancelled"):
            soil_fetch_err.set(None)
            return
        if isinstance(res, dict) and res.get("error"):
            lines = [ln.strip() for ln in str(res["error"]).splitlines() if ln.strip()]
            tail = lines[-1] if lines else "Unknown error."
            msg = f"NRCS soils fetch failed: {tail[:200]}"
            soil_fetch_err.set(msg)
            ui.notification_show(msg, type="error", duration=10)
            return
        soil_fetch_err.set(None)
        soil_snapshot.set(res)
        ui.notification_show(f"Fetched {len(res.get('polygons') or [])} NRCS soil polygons.",
                             duration=5)

    @reactive.effect
    def _soils_apply():
        if not _clicked_dynamic("soils_apply"):
            return
        snap = soil_snapshot()
        mode = str(_safe("soil_use_mode", "none"))
        if mode in ("aggregated", "zones") and not snap:
            ui.notification_show("Fetch NRCS soils first.", type="warning")
            return
        kz = [z for z in kzone_feats()
              if (z.get("properties") or {}).get("src") != "nrcs"]   # re-import replaces
        sel = soil_sel_units()
        if mode == "zones":
            if not sel:
                ui.notification_show("Check at least one map unit in the table to import.",
                                     type="warning")
                return
            policy, depth_m, aniso, kh0, kv0 = _soil_k_inputs()
            mus = {str(mu.get("mukey")): mu for mu in (snap.get("map_units") or [])}
            new = []
            for p in snap.get("polygons") or []:
                mk = str(p.get("mukey"))
                if mk not in sel or not p.get("geometry"):
                    continue
                mu = mus.get(mk) or {}
                zkh, zkv, _der = _soil_unit_k(mu, policy, depth_m, aniso, kh0, kv0)
                label = f"{mu.get('musym') or mk} {mu.get('name') or ''}".strip()
                new.append({"type": "Feature",
                            "properties": {"KH": round(zkh, 4), "KV": round(zkv, 4),
                                           "LABEL": label, "src": "nrcs"},
                            "geometry": p["geometry"]})
            new = geometry.normalize_kzone_features(new, **_kz_defaults())   # assigns uids
            kz = kz + new
            _kept["use_kzones"] = True
            ui.update_checkbox("use_kzones", value=True)
            _kept["use_soil_k"] = False
            msg = (f"Imported {len(new)} soil polygon{'s' if len(new) != 1 else ''} "
                   f"({len(sel)} map unit{'s' if len(sel) != 1 else ''}) as K-zones.")
        elif mode == "aggregated":
            _kept["use_soil_k"] = True
            msg = "Aggregated NRCS soils K will be used for the model."
        else:
            _kept["use_soil_k"] = False
            msg = "NRCS soils are not used for the model K."
        kzone_feats.set(kz)
        _load_into_drawcontrol(kz)
        soil_source.set({"mode": mode, "policy": str(_safe("soil_policy", "dominant")),
                         "units": sorted(sel) if mode == "zones" else []})
        ui.modal_remove()
        ui.notification_show(msg, duration=6)

    @reactive.effect
    def _soils_modal_cancel():
        if not _clicked_dynamic("soils_modal_cancel"):
            return
        p = _soil_proc.get("p")
        if p is not None:
            _soil_proc["killed"] = True   # deliberate cancel — must not read as a failure
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
        soil_fetch_err.set(None)
        ui.modal_remove()

    @render.ui
    def soil_k_status():
        # The Subsurface-properties pane's one-line soils summary. soil_source is the
        # invalidation source (use_soil_k has no live input — it lives in _kept only).
        _ = soil_source()
        n_nrcs = sum(1 for f in kzone_feats()
                     if (f.get("properties") or {}).get("src") == "nrcs")
        if bool(_kept.get("use_soil_k")):
            pol = {"dominant": "dominant component",
                   "weighted": "weighted components"}.get(str(_kept.get("soil_policy")
                                                              or "dominant"), "")
            return ui.p(ui.span(class_="hype-st st-done"),
                        f"Soils K: aggregated over the domain ({pol}).", class_="hype-chk ok")
        if n_nrcs:
            return ui.p(ui.span(class_="hype-st st-done"),
                        f"Soils K: {n_nrcs} polygon{'s' if n_nrcs != 1 else ''} imported "
                        "as K-zones.", class_="hype-chk ok")
        return ui.p("Soils K: not used.", class_="hype-chk")

    @output(suspend_when_hidden=False)       # rendered inside the soils modal (late-bound)
    @render.ui
    def soil_zone_count():
        snap = soil_snapshot()
        n = len(soil_sel_units())
        total = len((snap or {}).get("map_units") or [])
        if not n:
            return ui.div("Check boxes in the table to choose map units.", class_="hype-dim")
        return ui.div(f"{n} of {total} map units selected for import.")

    @output(suspend_when_hidden=False)       # rendered inside the soils modal (late-bound)
    @render.ui
    def soil_fetch_summary():
        snap = soil_snapshot()
        if not snap:
            return None
        polys, mus = snap.get("polygons") or [], snap.get("map_units") or []
        cols = snap.get("source_columns_used") or {}
        return ui.div(
            ui.tags.strong(f"{len(polys)} polygons · {len(mus)} map units"),
            ui.div("Columns used: " + (", ".join(f"{k}={v}" for k, v in cols.items())
                                       or "n/a"), class_="hype-dim"),
            class_="hype-soil-fetchsum")

    @output(suspend_when_hidden=False)       # rendered inside the soils modal (late-bound)
    @render.ui
    def soil_k_coverage_note():
        _ = run_result()                          # re-render after each run
        # The coverage file survives mode switches, so gate on the APPLIED decision or a
        # stale prior run keeps reporting under a decision that no longer uses it.
        if ((soil_source() or {}).get("mode")) != "aggregated":
            return None
        p = work_dir / "summary" / "soil_k_coverage.json"
        if not p.is_file():
            return None
        try:
            import json as _json
            rep = _json.loads(p.read_text())
            pct = rep.get("volume_pct_by_origin") or {}
            return ui.div(f"Last run: soils covered {rep.get('domain_area_covered_pct', 0)}% "
                          f"of the domain. K volume {pct.get('derived', 0)}% derived, "
                          f"{pct.get('fallback', 0)}% global fallback.", class_="hype-dim")
        except Exception:  # noqa: BLE001
            return None

    @output(suspend_when_hidden=False)       # rendered inside the soils modal (late-bound)
    @render.ui
    def soil_detail():
        snap = soil_snapshot()
        insp = soil_inspect()
        mus = {mu["mukey"]: mu for mu in ((snap or {}).get("map_units") or [])}
        mu = mus.get(insp)                        # no default: the empty state teaches the click
        if not mu:
            return ui.div("Click a soil on the map or a row in the table to view its "
                          "profile.", class_="hype-dim")
        blocks = [ui.div(f"{mu.get('musym') or insp} · {mu.get('name') or ''}",
                         class_="hype-dim")]
        for c in mu.get("components", []):
            hrows = []
            for h in c.get("horizons", []):
                ksat = h.get("ksat_um_s")
                kv = f"{ksat * 0.0864:.3f}" if isinstance(ksat, (int, float)) else "n/a"  # um/s→m/day
                hrows.append(ui.tags.tr(
                    ui.tags.td(h.get("name") or "n/a"),
                    ui.tags.td(f"{h.get('top_cm')}–{h.get('bottom_cm')} cm"),
                    ui.tags.td(f"{ksat:.2f}" if isinstance(ksat, (int, float)) else "n/a"),
                    ui.tags.td(kv), ui.tags.td(h.get("texture") or "n/a")))
            table = ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("Horizon"), ui.tags.th("Depth"), ui.tags.th("Ksat µm/s"),
                    ui.tags.th("KV m/day"), ui.tags.th("Texture"))),
                ui.tags.tbody(*hrows), class_="table table-sm hype-soil-table")
            bedrock = next((r for r in c.get("restrictions", []) if r.get("is_bedrock")), None)
            blocks.append(ui.div(
                ui.tags.strong(f"{c.get('name') or c.get('cokey')} · "
                               f"{c.get('comppct_r') or '?'}%"
                               + (" · major" if c.get("major") else "")),
                table,
                ui.div(f"Bedrock: {bedrock.get('kind')} at {bedrock.get('top_cm')} cm",
                       class_="hype-dim") if bedrock else None,
                class_="hype-soil-comp"))
        return ui.div(*blocks)

    # ------------------------------------------------------------------
    # Site Summary report (revision §11): assemble canonical model -> HTML/PDF/CSV/JSON
    # ------------------------------------------------------------------
    def _reach_length_m():
        try:
            snap = input_snapshot() or {}
            rl = (snap.get("site") or {}).get("reach_length_m")
            if rl:
                return float(rl)
            crs = proj_crs()
            gdf = geometry.single_feature_gdf(reach_feat())
            if crs is not None and gdf is not None and len(gdf):
                return float(gdf.to_crs(crs).length.iloc[0])
        except Exception:  # noqa: BLE001
            pass
        return None

    def _streamflow_cms():
        """Modeled stream discharge, for the reach-scale diagnostics.

        `Q_str` is what turns a per-pass efficiency into something the stream sees: the exchange
        ratio `Q_HZ / Q_str` sets both the stream concentration change and the processing length
        (screening reference §4.3). Read off the same snapshot `build_results` uses, so the pane
        and the report cannot disagree."""
        try:
            snap = input_snapshot() or {}
            v = ((snap.get("streamflow") or {}).get("value_cms")
                 if isinstance(snap.get("streamflow"), dict) else None)
            return float(v) if v else None
        except Exception:  # noqa: BLE001
            return None

    def _reach_endpoints_latlon():
        """(upstream, downstream) LatLon of the oriented reach centerline, in WGS84."""
        try:
            from hype_app.contracts import LatLon
            feat = reach_feat()
            coords = ((feat or {}).get("geometry") or {}).get("coordinates") or []
            if len(coords) >= 2:
                up, dn = coords[0], coords[-1]
                return (LatLon(lat=float(up[1]), lon=float(up[0])),
                        LatLon(lat=float(dn[1]), lon=float(dn[0])))
        except Exception:  # noqa: BLE001
            pass
        return None, None

    def _site_metadata():
        """SiteMetadata (§11.1) from the analyst-entered fields + geometry-derived reach length,
        outlet, and endpoints. Descriptive metadata — it never affects the physics or staleness."""
        from datetime import date as _date

        from hype_app.contracts import SiteMetadata
        up, dn = _reach_endpoints_latlon()
        ad = _safe("site_date", "")
        try:
            adate = _date.fromisoformat(ad) if ad else None
        except Exception:  # noqa: BLE001
            adate = None
        return SiteMetadata(
            site_id=(project_meta_v() or {}).get("site_id"),
            site_name=(str(_safe("site_name", "")).strip() or None),
            analyst=(str(_safe("site_analyst", "")).strip() or None),
            organization=(str(_safe("site_org", "")).strip() or None),
            notes=(str(_safe("site_notes", "")).strip() or None),
            assessment_date=adate,
            upstream_point=up, downstream_point=dn, outlet=dn,
            reach_length_m=_reach_length_m())

    def _report_modal(res, paths, doc="hydraulics"):
        # ONE DOCUMENT PER OPEN. The two reports are separate nodes in the tree now, so the tree
        # is the switcher and a tab strip inside the modal would be a second one.
        #
        # Prefer the generated HTML file — it already embeds the figures — so the modal preview
        # matches the download byte-for-byte. Fall back to a fresh render.
        if doc == "concept":
            # No file, no results, no staleness: the document is a shipped asset wrapped in a
            # page, so it is rendered here rather than read off a build.
            key, route, kw = (None, "conceptual_model", {})
            html = report_mod.concept_html((project_meta_v() or {}).get("name"))
        else:
            key, route, kw = (("screening_html", "screening_report",
                               {"include_hydraulics": False}) if doc == "screening"
                              else ("html", "site_report", {"include_functions": False}))
            html = None
            p = (paths or {}).get(key)
            if p and Path(p).is_file():
                try:
                    html = Path(p).read_text(encoding="utf-8")
                except Exception:  # noqa: BLE001
                    html = None
            if html is None:
                html = report_mod.render_html(res, app_version=APP_VERSION, **kw)
        # Serve the multi-MB self-contained document over HTTP and point the iframe at the URL.
        # As an iframe srcdoc attribute it rode the websocket, and a message that size can hit
        # the websockets-legacy concurrent-drain race and close the session 1008 mid-send (the
        # project-restore kill of 2026-07-24; the off-loop report build made it reachable from a
        # plain Generate click too). dynamic_route re-registration is idempotent per name and
        # every call returns a freshly nonced URL, so each open bypasses the browser cache.
        def _serve_report(_req):
            from starlette.responses import HTMLResponse
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
        report_url = session.dynamic_route(route, _serve_report)
        # No modal title: the report document carries its own header, so a title bar would
        # duplicate it. With title=None Shiny omits the modal-header entirely (close stays in
        # the footer + easy_close).
        # The report renders inside a borderless iframe that fills the modal body, so the iframe is
        # the ONLY scroll region (EASI/SFARI pattern). Downloads + Close live in the footer, keeping
        # the body 100% report. The full-height DIALOG sizing is injected here scoped to #shiny-modal
        # (Shiny's fixed modal id) so it applies ONLY while the report is open — the other size="xl"
        # modals (USGS, NRCS) share .modal-dialog/.modal-body and must keep their default sizing.
        return ui.modal(
            ui.tags.style(
                "#shiny-modal .modal-dialog{max-width:min(1280px,96vw);width:96vw;"
                "height:calc(100vh - 1.25rem);margin:.6rem auto}"
                "#shiny-modal .modal-content{height:100%;max-height:100%;overflow:hidden;"
                "display:flex;flex-direction:column}"
                "#shiny-modal .modal-body{flex:1 1 auto;min-height:0;padding:0;overflow:hidden;"
                "display:flex}"
                "#shiny-modal .hype-report-frame{min-height:0}"),
            ui.tags.iframe(src=report_url, class_="hype-report-frame", title="Site summary report"),
            size="xl", easy_close=True,
            # This document's own formats first, then the data exports, which are shared.
            # THE CONCEPTUAL MODEL GETS NEITHER SET OF DATA EXPORTS: it carries no results, so a
            # Metrics CSV button beside it would offer another document's numbers.
            footer=ui.div(
                *([ui.download_button("dl_concept_pdf", "PDF",
                                      class_="btn-sm btn-outline-secondary"),
                   ui.download_button("dl_concept_html", "HTML",
                                      class_="btn-sm btn-outline-secondary")]
                  if doc == "concept" else
                  [ui.download_button("dl_screening_pdf", "PDF",
                                      class_="btn-sm btn-outline-secondary"),
                   ui.download_button("dl_screening_html", "HTML",
                                      class_="btn-sm btn-outline-secondary")]
                  if doc == "screening" else
                  [ui.download_button("dl_report_pdf", "PDF",
                                      class_="btn-sm btn-outline-secondary"),
                   ui.download_button("dl_report_html", "HTML",
                                      class_="btn-sm btn-outline-secondary")]),
                # The data exports are the CURRENT SITE's numbers; the Conceptual Model
                # carries no results at all.
                *([] if doc == "concept" else [
                    ui.download_button("dl_report_csv", "Metrics CSV", class_="btn-sm btn-outline-secondary"),
                    ui.download_button("dl_report_summary", "Run summary", class_="btn-sm btn-outline-secondary"),
                    ui.download_button("dl_report_rtd", "RTD data", class_="btn-sm btn-outline-secondary"),
                    ui.download_button("dl_report_json", "JSON", class_="btn-sm btn-outline-secondary")]),
                ui.modal_button("Close", class_="btn-sm btn-primary"),
                class_="hype-report-actions"))

    # Flux-weighted §8.3 interface metrics. THE BODY LIVES IN `hz_results` because it captures
    # nothing from the session and the scenario-envelope build calls it from the report worker
    # thread, where a `server()` closure has no business being. Kept as a local name so the call
    # sites below read the same as they always did.
    def _flux_metrics(hz_stats: dict, hz_dir, *, transit_rows: bool = True):
        return hz_results.flux_metrics(hz_stats, hz_dir, transit_rows=transit_rows)

    def _migrate_pollutant_keys() -> None:
        """Carry a project saved under either older endpoint shape onto the current picker.

        TWO HOPS, oldest first, because a project can be either vintage:

        1. Until 2026-07-31 the section screened ONE endpoint, chosen in `fn_pol_preset` with its
           concentration in `fn_pol_conc`.
        2. Until 2026-08-01 it screened several, held in one `fn_pol_<group>` list per preset group.

        Both now land in a single `fn_pol_endpoints` selectize. Without this the saved endpoints
        would silently collapse to the DEFAULT_ENDPOINTS single chip and the saved concentration
        would vanish -- data loss with nothing on screen to reveal it, since the pane would look
        perfectly healthy screening the wrong chemical.

        `fn_pol_rate` is deliberately NOT carried: the rate now comes from the citation, and
        restoring a user-typed one would reintroduce the very override the Custom removal took
        away."""
        key = str(_kept.pop("fn_pol_preset", "") or "").strip()
        conc = _kept.pop("fn_pol_conc", None)
        for gone in ("fn_pol_rate", "fn_pol_name", "fn_pol_mode"):
            _kept.pop(gone, None)
            _kept_seen.pop(gone, None)
        # Hop 1 -> the group lists, which hop 2 then folds up. Writing through the intermediate
        # shape rather than straight to the picker keeps the two migrations independent: a project
        # of either vintage takes the same path from here down.
        preset = fn_pol.get_preset(key)
        if preset is not None:
            for gid, _, keys in fn_pol.PRESET_GROUPS:
                _kept[f"fn_pol_{gid}"] = [preset.key] if preset.key in keys else []
            if conc is not None:
                _kept[f"fn_pol_conc_{preset.key}"] = conc
        # Hop 2 -> one picker. Only when a group id is actually present: an absent one means this
        # project predates them entirely, and defaulting here would overwrite a genuine empty
        # selection the user made under the new picker with the shipped default.
        gids = [f"fn_pol_{gid}" for gid, _, _ in fn_pol.PRESET_GROUPS]
        if any(g in _kept for g in gids):
            picked: list[str] = []
            for g in gids:
                picked += list(_kept.pop(g, None) or [])
                _kept_seen.pop(g, None)
            _kept[FN_POL_SELECT_ID] = list(fn_pol.ordered_keys(picked))

    def _fn_inputs() -> dict:
        """Screening knobs as a plain dict for assess.build_results. Blank means 'not supplied',
        which is a real state: without a rate the section still reports its rate-free results.

        READS THE LIVE INPUT FIRST, then falls back to `_kept`. Both halves matter:

        * `_kept` is a PLAIN dict, so reading it takes no reactive dependency. On `_keep` alone
          the pane never invalidated, and editing dissolved oxygen or nitrate changed nothing on
          screen -- which defeats the entire point of the oxygen gate, where the user supplies a
          DO value precisely so the onset time is derived rather than guessed.
        * the fallback is still required, because a pane that is not on screen has no mounted
          input; that is what `_kept` exists for (remount survival), and the report can be built
          while the user is looking at some other node.

        Reading the input directly also sidesteps an ordering race: `_keep_inputs` is a separate
        effect, so `_kept` is not guaranteed to be updated before this render runs.

        A BLANK IS AN ANSWER, NOT AN ABSENCE. `_live` reports the two apart: Shiny raises for an
        id the client has never sent, and returns None once a mounted numeric has been emptied
        (its `getValue` returns null for a whitespace-only field). Only the first case falls back.
        Collapsing them is what made a value impossible to clear: `_keep_inputs` also skips None,
        so `_kept` never learned about the clear and kept handing the old number back forever.
        Same shape as `_manual_da_valid`, which already gets this right."""
        def _live(iid):
            """(value, reported). `reported` is False only when the client has never sent this
            id at all -- a pane that has not been opened this session. Server-side input values
            outlive their DOM element, so once a pane has mounted the live read stays authoritative
            even while the user is looking at another node."""
            try:
                return input[iid](), True
            except Exception:      # noqa: BLE001 - never set; same guard as _keep_inputs
                return None, False

        def num(iid, default=None):
            v, reported = _live(iid)
            if not reported:
                v = _keep(iid, default)
            try:
                return None if v is None or v == "" else float(v)
            except (TypeError, ValueError):
                return None

        def _txt(iid):
            v, reported = _live(iid)
            return (v if reported else _keep(iid, "")) or ""

        def _flag(iid, default=True):
            v, reported = _live(iid)
            if not reported:
                v = _keep(iid, default)
            return bool(default if v is None else v)

        def _ticked():
            """The endpoint keys the picker has selected, in registry order.

            A pane the user has never opened contributes DEFAULT_ENDPOINTS rather than nothing, so a
            report built without visiting it still screens something. `ordered_keys` re-sorts into
            registry order whatever order the chips were added in, so the pane, the report and the
            CSV list the same endpoints the same way."""
            v, reported = _live(FN_POL_SELECT_ID)
            if not reported:
                v = _keep(FN_POL_SELECT_ID, list(fn_pol.DEFAULT_ENDPOINTS))
            return fn_pol.ordered_keys([k for k in (v or []) if k in fn_pol.PRESET_BY_KEY])
        return {
            # WHICH SECTIONS ARE TURNED ON. Off means not screened at all: the section is absent
            # from the results model and from the report, rather than computed and hidden.
            "screening_enabled": {k: _flag(f"fn_incl_{k}") for k in fn_reg.SECTION_ORDER},
            # WHETHER THE REDOX GATE APPLIES. The three oxygen knobs below still travel when it is
            # off -- they are the pane's remembered values, so reticking the switch restores what
            # the user last entered rather than the shipped defaults -- and `screen_reactive`
            # ignores them entirely in that state.
            "oxygen_gate": _flag("fn_do_gate"),
            "dissolved_oxygen_mg_l": num("fn_do", fn_reg.DO_STREAM_DEFAULT_MG_L),
            "anoxic_threshold_mg_l": num("fn_do_thresh", fn_reg.DO_ANOXIC_THRESHOLD_MG_L),
            "oxygen_consumption_mg_l_day": num("fn_o2_rate",
                                               fn_reg.OXYGEN_CONSUMPTION_MG_L_DAY[1]),
            # The default lives HERE as well as on the input, not only on the input: a report can
            # be generated before the Nutrient Cycling pane has ever mounted, and `_keep` is empty
            # until it does. Split them and the pane and the report disagree.
            "nitrate_mg_l": num("fn_no3", fn_reg.NITRATE_DEFAULT_MG_N_L),
            # Pinned to nitrogen, which is what every source in the chain reports and what
            # nutrient crediting is written in. `screen.py` still accepts an as-NO3 basis from an
            # API caller; the app just never offers the choice.
            "nitrate_basis": "N",
            "denit_rate_per_day": num("fn_denit_rate",
                                      fn_reg.get_process("denitrification").rate_central),
            # WHICH CITED ENDPOINTS ARE SCREENED, and each one's stream concentration in its own
            # reported unit. THERE IS NO RATE KNOB: every endpoint in the library carries a cited
            # rate, so the calculation reads it from the preset and no number in this section can
            # outrun its citation. A cleared concentration stays None, which is the endpoint
            # reporting its own missing-concentration reason rather than a silent substitution.
            "pollutant_endpoints": list(_ticked()),
            "contaminant_conc_by_key": {
                p.key: num(f"fn_pol_conc_{p.key}", p.concentration) for p in fn_pol.PRESETS},
            "thermal_response_hours": num("fn_tau",
                                          fn_reg.get_process("thermal_regulation").rate_central),
        }

    def _screening_now() -> dict:
        """Every screening section, recomputed from the CURRENT pane inputs, keyed by process.

        Deliberately not cached with the run: the calculations are milliseconds on arrays already
        in memory, so a changed dissolved-oxygen value updates the pane without re-running
        anything. The report passes the same knobs through assess.build_results, so the two agree.

        UNITS: fm["transit_weights"] is raw m3/day; `exchange` is m3/s. Both go in as-is and the
        screen records their consistency as a QC diagnostic."""
        hz = hz_result()
        if not hz:
            return {}
        try:
            full_stats = hz.get("stats") or {}
            fm = _flux_metrics(full_stats, hz.get("hz_dir"))
            from hype_app.functions import ScreeningInputs, get_process, screen_process
            # ONE derivation, shared with the report and the Hyporheic Zone pane. Every hydraulic
            # field below used to be re-derived here -- the exchange flux, the path-depth stats,
            # the pore volume, the equivalent depth, the three bed fractions -- which is how the
            # pane and the report came to disagree about pore volume. `screening_fields` names the
            # mapping once, so a new hydraulic field reaches all five sections without an edit
            # here. `from_hz_bundle` also owns the porosity rule: the HYPORHEIC run's value wins,
            # because that is what MODPATH tracked at and therefore what produced the volume.
            sig = signature.derive(signature.SignatureInputs.from_hz_bundle(
                full_stats, fm,
                # Reach length normalizes removal per km of channel; the frozen-run snapshot wins
                # over the live centerline, same as everywhere else that reads it.
                streamflow_cms=_streamflow_cms(), reach_length_m=_reach_length_m(),
                snapshot_porosity=_safe("porosity", None)))
            k = _fn_inputs()
            base = ScreeningInputs(
                **signature.screening_fields(sig),
                transit_times_days=(fm["transit_times"]
                                    if fm["transit_times"] is not None else ()),
                transit_weights_m3_day=(fm["transit_weights"]
                                        if fm["transit_weights"] is not None else ()),
                # The porosity currently in the UI, which is session state rather than a modeled
                # quantity, so it stays out of the signature. The screen shows it only to disclose
                # that it differs from the one the run was tracked at.
                porosity_live=signature.as_float(_safe("porosity", None)),
                # REACH LENGTH REACHES THE PANE TOO. `screening_fields` does not carry it (it
                # feeds the signature's turnover, and the flat dict never re-emits it), so every
                # reach-scale number was blank here while the report computed it from the same
                # knobs: removal per stream km, and every reach-scale pollutant diagnostic, which
                # led with "no reach length is available" on a project that has one.
                reach_length_m=_reach_length_m(),
                # Path LENGTHS still travel even though the only calculator that read them is
                # unregistered: `_flux_metrics` produces them either way, and `screen_particulate`
                # wants them back unchanged the day microplastics is re-registered.
                path_lengths_m=fm["path_lengths"],
                # WITH the three inputs it governs, never apart from them. This block is threaded
                # by hand while `assess._build_functions` reads the same knobs dict by name, so a
                # knob added there and forgotten here is silently ignored on the pane and honoured
                # in the report -- which is exactly what happened to this flag: the oxygen inputs
                # disappeared when it was unticked and every number stayed put.
                # `test_the_pane_threads_every_screening_knob_it_sends` now fails on that.
                oxygen_gate=k["oxygen_gate"],
                dissolved_oxygen_mg_l=k["dissolved_oxygen_mg_l"],
                anoxic_threshold_mg_l=k["anoxic_threshold_mg_l"],
                oxygen_consumption_mg_l_day=k["oxygen_consumption_mg_l_day"],
                nitrate_basis=k["nitrate_basis"])
            conc_rate = {
                "denitrification": (k["nitrate_mg_l"], k["denit_rate_per_day"]),
                "contaminant": (None, None),      # driven by the endpoint loop below
                "habitat": (None, None),
                "thermal_regulation": (None, k["thermal_response_hours"]),
            }
            out = {}
            #: Kinds that take no rate at all. Passing one to the particulate path is a caller
            #: error rather than an ignored argument (screening reference rule 1).
            rateless = (fn_reg.KIND_EXTENT, fn_reg.KIND_PARTICULATE)
            for key in fn_reg.SECTION_ORDER:
                spec = get_process(key)
                if key == "contaminant":
                    continue                      # handled below, once per ticked endpoint
                conc, rate = conc_rate[key]
                si = base if conc is None else dc_replace(base, inlet_concentration_mg_l=conc)
                out[key] = screen_process(
                    si, spec, **({} if spec.kind in rateless else {"rate": rate}))
            # ONE RESULT PER TICKED ENDPOINT, sharing the report's derivation exactly:
            # `assess._screen_endpoints` takes the same knobs and does the same thing, so the pane
            # and the document cannot disagree about what zinc does here. `out["contaminant"]` is
            # the FIRST of them, because every helper that formats a section takes one flat dict
            # and the pending-state path needs something to paint when nothing is ticked.
            eps = assess.screen_endpoints(base, get_process("contaminant"), k, screen_process)
            out["contaminant_endpoints"] = eps
            out["contaminant"] = (eps[0][1] if eps else
                                  screen_process(base, get_process("contaminant")))
            return out
        except Exception:  # noqa: BLE001 — the screen is a read-only view; never break the pane
            return {}

    def _report_spatial(hz_dir):
        """Best-effort spatial data for the report figures (report §17.4): plan-view GeoJSON plus
        the returning-path GeoDataFrame with the reach centerline reprojected to the same metric CRS
        for the longitudinal section. Any failure degrades to a missing figure, never a broken run."""
        if not hz_dir:
            return None
        rf = reach_feat()
        reach_lonlat = (rf["geometry"]["coordinates"]
                        if rf and (rf.get("geometry") or {}).get("type") == "LineString" else None)
        df = domain_feat() if callable(domain_feat) else None
        domain_lonlat = (df["geometry"]["coordinates"][0]
                         if df and (df.get("geometry") or {}).get("type") == "Polygon" else None)
        planview = {
            "down_fc": hz_results.flow_exchange_geojson(hz_dir, "down"),
            "up_fc": hz_results.flow_exchange_geojson(hz_dir, "up"),
            "footprint_fc": hz_results.footprint_geojson(hz_dir, "hyporheic"),
            "reach_lonlat": reach_lonlat, "domain_lonlat": domain_lonlat}
        paths_gdf = reach_line = None
        try:
            gdf = hz_results.class_paths_gdf(hz_dir)
            if gdf is not None and len(gdf):
                if "hz_class" in gdf.columns:
                    gdf = gdf[gdf["hz_class"] == "hyporheic"]
                if len(gdf) and reach_lonlat:
                    import geopandas as gpd
                    from shapely.geometry import LineString
                    rl = gpd.GeoSeries([LineString(reach_lonlat)], crs="EPSG:4326").to_crs(gdf.crs)
                    paths_gdf, reach_line = gdf, rl.iloc[0]
        except Exception:  # noqa: BLE001 — the section figure is optional
            paths_gdf = reach_line = None
        spatial = {"planview": planview, "paths_gdf": paths_gdf, "reach_line": reach_line}
        try:                                     # site-map inputs (report §10) — all optional
            crs = proj_crs()
            spatial["crs_wkt"] = (crs.to_wkt() if crs is not None else
                                  (paths_gdf.crs.to_wkt()
                                   if paths_gdf is not None and paths_gdf.crs else None))
            wse_tif = work_dir / "model" / "cropped_water_surface_raster.tif"
            spatial["wse_tif"] = str(wse_tif) if wse_tif.exists() else _wse_used.get("path")
            head_tifs_l = results.head_rasters(work_dir)
            if head_tifs_l:
                _hl = results.full_coverage_layer(head_tifs_l)
                spatial["head_tif"] = str(head_tifs_l[_hl - 1])
                spatial["head_layer"] = _hl
            else:
                spatial["head_tif"] = None
            gwf = work_dir / "model" / "gwf_workspace"
            spatial["gwf_ws"] = str(gwf) if next(gwf.glob("*.dis.grb"), None) else None
            spatial["dem_path"] = active_dem()
            spatial["sides_lonlat"] = {
                k: f["geometry"]["coordinates"]
                for k, f in (("up", up_feat()), ("left", left_feat()),
                             ("right", right_feat()), ("down", down_feat()))
                if f and (f.get("geometry") or {}).get("type") == "LineString"}
            spatial["wells_lonlat"] = [(float(w["lon"]), float(w["lat"]),
                                        str(w.get("name") or ""))
                                       for w in obs_wells()]
        except Exception:  # noqa: BLE001 — the site maps are optional
            pass
        return spatial

    @reactive.extended_task
    async def report_task(payload: dict) -> dict:
        # Worker thread, NOT a spawn child: generate_report is a pure function of the payload
        # (no reactive reads anywhere under hype_app/report.py), and a Windows spawn child
        # would pay 2-4 s of interpreter+import tax per report.
        def _work():
            res = payload["res"]
            # THE SCENARIO ENVELOPE, off-loop and OUTSIDE the matplotlib lock (it needs no
            # pyplot). Every completed alternative is re-screened here from its retained
            # per-particle artifacts, which is minutes of MODFLOW already paid for and
            # milliseconds of numpy now. Withheld whole rather than short, and never silently:
            # the warning rides `res.warnings` into both documents, because the build is
            # off-loop and a ticked box that produced nothing would otherwise be unexplainable.
            envp = payload.get("envelope")
            if envp:
                env, warn = alt_screening.build_envelope(
                    envp["manifest"], work_dir=envp["work_dir"], snapshot=envp["snapshot"],
                    base_functions=res.functions, function_inputs=envp["knobs"],
                    reach_length_m=envp["reach_length_m"], app_version=APP_VERSION)
                upd = {}
                if env is not None:
                    upd["function_envelope"] = env
                if warn is not None:
                    upd["warnings"] = list(res.warnings) + [warn]
                if upd:
                    res = res.model_copy(update=upd)
            with _REPORT_MPL_LOCK:
                paths = report_mod.generate_report(
                    res, payload["out_dir"],
                    transit_rows=payload["transit_rows"], spatial=payload["spatial"],
                    app_version=APP_VERSION,
                    model_version="MODFLOW 6 / MODPATH 7",
                    project_name=payload["project_name"],
                    include_functions=payload.get("include_functions", True))
            return {"paths": paths, "doc": payload.get("doc"),
                    "build_id": payload.get("build_id"), "res": res}
        return await anyio.to_thread.run_sync(_work)

    def _alt_manifest_current(input_hash=None):
        """The alternatives manifest when it is CURRENT, else None.

        ONE DEFINITION OF CURRENT, because two surfaces ask the question: the report attaches the
        manifest so its Scenario range column is honest, and the screening pane offers the
        envelope option only when there is something to build it from. Those used to be the same
        four conditions written out twice, which is how they come to disagree.

        Current means computed against THIS basecase (hash match), at least one completed run, and
        not mid-sweep or halted. A stale manifest must never print ranges against a basecase it
        never ran on."""
        try:
            _ar = alt_result() or {}
            _amf = _ar.get("manifest")
            if not _amf or _ar.get("running") or _ar.get("halted_on"):
                return None
            if input_hash is None:
                input_hash = (input_snapshot() or {}).get("input_hash")
            if _amf.get("base_input_hash") != input_hash:
                return None
            if not any(s.get("status") == "completed" for s in _amf.get("scenarios") or []):
                return None
            from hype_app.contracts import HydraulicAlternativesManifest
            return HydraulicAlternativesManifest.model_validate(_amf)
        except Exception:  # noqa: BLE001 — the report never fails on the sweep's account
            return None

    def _envelope_state() -> tuple[bool, str]:
        """(can a hydraulic scenario envelope be built, why not). Drives the checkbox AND the
        build, so a ticked box cannot mean one thing to the pane and another to the worker."""
        mf = _alt_manifest_current()
        return alt_screening.envelope_available(mf, work_dir=work_dir)

    def _envelope_on() -> bool:
        """The EFFECTIVE selection. A saved tick whose sweep has since been wiped, gone stale or
        gone partial resolves to False here rather than lingering as a checked box whose value is
        quietly ignored, which is the state the option must never sit in."""
        return bool(_keep("report_fn_envelope", False)) and _envelope_state()[0]

    def _report_signature() -> str:
        """Everything a built report depends on, as one comparable string.

        The Open buttons rebuild when this has moved since the last build, which is what let the
        Generate button go: a reader should not have to know that editing a concentration made the
        PDF on disk out of date. Deliberately cheap and deliberately over-inclusive -- a false
        rebuild costs ten seconds, a missed one prints a stale number."""
        snap = input_snapshot() or {}
        try:
            # The alternatives manifest rides along so finishing (or wiping) a sweep marks a
            # built report stale — the document gains or loses its Scenario range column.
            _ar = alt_result() or {}
            # Well records mutate IN PLACE, so wells_ver() is read for reactivity (an edit
            # re-evaluates every consumer) but the SERIALIZED values are what decide staleness
            # — a heal/no-op bump must not stale a built document.
            wells_ver()
            return json.dumps({"hash": snap.get("input_hash"),
                               "fn": _fn_inputs(), "site": _site_metadata(),
                               "alts": (None if _ar.get("running") or _ar.get("halted_on")
                                        else _ar.get("manifest")),
                               # Ticking the envelope must mark a built document stale, or the
                               # Open button reopens the file that has no envelope in it.
                               "env": _envelope_on(),
                               # Observation wells feed the calibration table: any edit to the
                               # wells or the tracked pairs makes the built file stale.
                               "wells": [dict(w) for w in obs_wells()],
                               "pairs": [dict(p) for p in well_pairs()]},
                              sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 — an unhashable knob means "assume stale"
            return ""

    #: document -> the built file that proves it exists.
    _STALE_WANT = {"hydraulics": "html", "screening": "screening_html"}

    def _report_stale(doc: str) -> bool:
        """Whether the named document has to be rebuilt before it can be opened."""
        want = _STALE_WANT[doc]
        if not _report_files().get(want):
            return True
        stamp = _report_stamp()
        return not stamp or stamp != _report_signature()

    def _capture_canonical_results(hz=None, snap_dict=None):
        """Build and persist the validated result snapshot independently of any report.

        Called the moment HZ completes and again by the report gather; both routes produce the
        SAME canonical contract, so a saved project is comparable without ever opening a
        report (a report is a downstream renderer, never the event that creates results)."""
        hz = hz if hz is not None else hz_result()
        snap_dict = snap_dict if snap_dict is not None else input_snapshot()
        if not hz or not snap_dict:
            raise ValueError("Hyporheic results and frozen inputs are required")
        from hype_app.contracts import AssessmentInputSnapshot
        snap = AssessmentInputSnapshot.model_validate(snap_dict)
        # Overlay the CURRENT site metadata (name/analyst/date…) so fields filled after the
        # run still appear — descriptive metadata, never physics, so this doesn't reopen the
        # frozen inputs. The overlay re-stamps the results' computed input hash, which is why
        # the alternatives attach below also accepts the frozen spelling (extra_hashes).
        try:
            snap = snap.model_copy(update={"site": _site_metadata()})
        except Exception:  # noqa: BLE001
            pass
        full_stats = hz.get("stats") or {}
        stats = full_stats.get("classes") or full_stats or {}
        hyp = stats.get("hyporheic") or {}
        acct = (full_stats.get("flux") or {}).get("accounting") or {}
        net_exch = acct.get("net_stream_exchange")
        domain_vol = (full_stats.get("domain") or {}).get("active_saturated_volume_m3")
        fm = _flux_metrics(full_stats, hz.get("hz_dir"))
        transit_rows = fm["transit_rows"]
        # POROSITY: the HYPORHEIC run's, not the snapshot's.
        #
        # This used to read `snap.k.porosity`, which is frozen at the GROUNDWATER run, while
        # the screening panes read the knobs the hyporheic run tracked at. Editing porosity
        # between the two runs made the pane and this report print different pore volumes.
        # The hyporheic value is the correct one: porosity is a MODPATH input, so it set the
        # pore velocity, the travel times, which particles returned inside the tracking window,
        # and therefore the volume the pore storage scales. `snapshot_porosity` stays as the
        # fallback for a run whose knobs predate the field, and validate.py raises a warning
        # when the two disagree rather than letting the choice pass silently.
        porosity = signature.as_float((full_stats.get("knobs") or {}).get("porosity"))
        if porosity is None:
            porosity = snap.k.porosity
        _fn_knobs = _fn_inputs()
        frozen_hash = frozenset(h for h in (snap_dict.get("input_hash"),) if h)
        # Observation-well calibration rides the RESULTS side (never the input snapshot — a
        # wells edit must not re-stamp input_hash). Isolated read: the HZ-done effect and the
        # report gather both land here, and neither may re-fire on later well edits (staleness
        # is _report_signature's job).
        cal = None
        try:
            with reactive.isolate():
                srows, prows = well_samples(), well_pair_rows()
            if srows:
                from hype_app.contracts import (CalibrationPair, CalibrationStats,
                                                CalibrationWell, GroundwaterCalibration)
                stats_d = wells_mod.residual_stats(srows)
                cal = GroundwaterCalibration(
                    wells=[CalibrationWell(
                        well_id=r["id"], name=r["name"], lat=r["lat"], lon=r["lon"],
                        screen_elevation_m=r["screen_elev"], observed_head_m=r["obs_head"],
                        model_layer=r["layer"], computed_head_m=r["computed"],
                        residual_m=r["residual"], note=r["reason"]) for r in srows],
                    pairs=[CalibrationPair(
                        pair_id=r["id"], well_a=r["name_a"], well_b=r["name_b"],
                        distance_m=r["distance"], computed_gradient=r["computed_gradient"],
                        observed_gradient=r["observed_gradient"], note=r["reason"])
                        for r in prows],
                    stats=(CalibrationStats(
                        n_observed=stats_d["n"], mean_error_m=stats_d["mean_error"],
                        mean_absolute_error_m=stats_d["mean_abs_error"],
                        rmse_m=stats_d["rmse"]) if stats_d else None))
        except Exception:  # noqa: BLE001 — calibration is observation data, never a blocker
            cal = None
        # Attaches the Hydraulic Alternatives manifest when it is CURRENT: computed against
        # this basecase (identity match), at least one completed run, and not mid-sweep or
        # halted. A stale manifest must never print ranges against a basecase it never ran on.
        res = results_lifecycle.build_canonical_results(
            snap, alternatives_state=alt_result(), extra_hashes=frozen_hash,
            hz_stats=stats, streamflow_cms=snap.streamflow.value_cms,
            reach_length_m=_reach_length_m(), exchange=fm["exchange"],
            transit_times_days=fm["transit_times"], transit_weights=fm["transit_weights"],
            path_depths=fm["path_depths"], path_lengths=fm["path_lengths"],
            footprint_weighted_m2=hyp.get("footprint_m2"), porosity=porosity,
            snapshot_porosity=snap.k.porosity,
            censored_fraction=fm["censored"],
            streambed_area_m2=acct.get("streambed_area_m2"),
            active_streambed_area_m2=acct.get("active_streambed_area_m2"),
            return_streambed_area_m2=acct.get("return_streambed_area_m2"),
            connected_streambed_area_m2=acct.get("connected_streambed_area_m2"),
            net_stream_exchange_cms=(net_exch / 86400.0 if net_exch is not None else None),
            domain_volume_m3=domain_vol, hz_accounting=acct,
            # the same knobs the Screening panes show live, so the two never disagree. Held
            # in a local because the scenario envelope must screen every alternative with
            # THIS dict, not a second `_fn_inputs()` call that could resolve differently.
            function_inputs=_fn_knobs, calibration=cal,
            app_version=APP_VERSION)
        results_model.set(res.model_dump(mode="json"))
        return res, transit_rows, snap, _fn_knobs

    def _start_report_build(doc: str | None = None) -> bool:
        """GATHER phase of the report build, shared by the Open buttons and the auto-open effect:
        every reactive read plus the fast numeric assembly stays on the loop; the slow,
        reactive-free figure/HTML/PDF generation runs in report_task (the modal opens from
        _report_done). `doc` names the document to show when it lands, or None to build quietly.
        Returns True when the task was launched."""
        hz, snap_dict = hz_result(), input_snapshot()
        if not hz or not snap_dict:
            return False
        try:
            res, transit_rows, snap, _fn_knobs = _capture_canonical_results(hz, snap_dict)
            _cur_mf = res.alternatives
            spatial = None
            try:
                spatial = _report_spatial(hz.get("hz_dir"))
            except Exception:  # noqa: BLE001 — figures are best-effort
                spatial = None
            # THE ENVELOPE'S IMMUTABLE BUILD SNAPSHOT. Everything the worker needs is frozen
            # here as plain data: the manifest object, the work dir, the validated snapshot and
            # THE SAME knobs dict `build_results` just used for the Basecase. Passing the same
            # object rather than calling `_fn_inputs()` twice is what makes "the same assumptions
            # for every case" structural. The worker reads no live state, so artifacts changing
            # after the checkbox enabled cannot race the build.
            _env_on = _envelope_on() and res.functions is not None
            payload = {"res": res, "out_dir": work_dir / "report",
                       "transit_rows": transit_rows, "spatial": spatial,
                       "project_name": (project_meta_v() or {}).get("name"),
                       "doc": doc,
                       "build_id": _report_build_id() + 1,
                       "envelope": ({"manifest": _cur_mf, "work_dir": work_dir,
                                     "snapshot": snap, "knobs": _fn_knobs,
                                     "reach_length_m": _reach_length_m()}
                                    if _env_on and _cur_mf is not None else None),
                       # Part B is droppable, Part A never is (spec §9.4: a complete hydraulic
                       # signature must be obtainable without entering any chemistry). It drops
                       # when every screening is switched off, which is the same state that leaves
                       # `res.functions` None, so the document would have had nothing in it.
                       "include_functions": res.functions is not None}
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Report generation failed: {type(e).__name__}: {e}",
                                 type="error", duration=8)
            return False
        _task_armed["report"] = True
        _report_stamp.set(_report_signature())
        _report_build_id.set(payload["build_id"])
        report_task(payload)
        return True

    @reactive.effect
    def _auto_open_report():
        """Open the report once when a delineation run completes (EASI pattern). Isolated so later
        site-metadata edits do not reopen it; the Open buttons handle every later build."""
        hz, snap_d = hz_result(), input_snapshot()
        if not hz or not isinstance(snap_d, dict):
            return
        # Deliberate status subscription: a build already in flight defers this auto-open
        # (unmarked), and the completion status flip re-fires the effect to start it then.
        if report_task.status() == "running":
            return
        cur = snap_d.get("input_hash")
        with reactive.isolate():
            if not report_mod.should_autoopen(_report_shown_for(), cur):
                return
            _report_shown_for.set(cur)      # mark before building so a failure never loops
            _start_report_build("hydraulics")

    @reactive.effect
    def _report_done():
        if report_task.status() in ("initial", "running", "cancelled"):
            return
        if not _task_armed["report"]:   # already applied (or session reset) — see _task_armed
            return
        _task_armed["report"] = False
        try:
            out = report_task.result()
            paths = out["paths"]
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Report generation failed: {type(e).__name__}: {e}",
                                 type="error", duration=8)
            return
        with reactive.isolate():
            if hz_result() is None:     # results cleared mid-build; never resurrect them
                return
            # A SUPERSEDED WORKER LANDS NOTHING. Re-opening after an edit while a build is still
            # in its scenario loop launches a second one; without this the slower first build
            # would overwrite the newer documents and the paths pointing at them.
            if out.get("build_id") is not None and out["build_id"] != _report_build_id():
                return
            res_d = results_model()
        if not res_d:
            return
        try:
            from hype_app.contracts import AssessmentResultsV2
            # The worker's copy carries the scenario envelope and any warning it raised; the
            # reactive model predates both. Preferring the worker's keeps the modal's fallback
            # render (which reads `results_model`) agreeing with the files on disk.
            res = out.get("res") or AssessmentResultsV2.model_validate(res_d)
            if out.get("res") is not None:
                results_model.set(res.model_dump(mode="json"))
            report_paths.set(paths)
            # WHICHEVER DOCUMENT WAS ASKED FOR. It used to always be the hydraulics one, which was
            # right while the only trigger was a Generate button, and wrong the moment the
            # screening node grew an Open button that builds.
            doc = out.get("doc")
            if doc:
                ui.modal_show(_report_modal(res, paths, doc=doc))
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Report generation failed: {type(e).__name__}: {e}",
                                 type="error", duration=8)

    def _open_built_report(doc):
        """Open a document from its own tree node, rebuilding it first when it has gone stale.

        THIS IS WHERE THE GENERATE BUTTON WENT. Freshness is the app's job: a concentration edited
        after the last build changes what the screening report should say, and asking a reader to
        notice that and press a separate button is asking them to track state the app already
        tracks. A fresh document opens with no delay, a stale one shows the busy row and opens when
        it lands (`_report_done` reads the doc off the payload)."""
        if doc == "concept":
            # Nothing to build and nothing to go stale: a shipped figure wrapped in a page. It
            # deliberately does not defer to a report build in flight either, since it shares
            # nothing with one.
            try:
                ui.modal_show(_report_modal(None, None, doc="concept"))
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"Could not open the report: {type(e).__name__}: {e}",
                                     type="error", duration=8)
            return
        if report_task.status() == "running":
            return                                       # the pane already shows the busy row
        if not hz_result() or not input_snapshot():
            ui.notification_show("Run the Hyporheic Zone calculations first (their stats feed "
                                 "the report).", type="warning", duration=5)
            return
        if _report_stale(doc):
            _start_report_build(doc)
            return
        res_d = results_model()
        if not res_d:                                    # files on disk, model not loaded
            _start_report_build(doc)
            return
        try:
            from hype_app.contracts import AssessmentResultsV2
            ui.modal_show(_report_modal(AssessmentResultsV2.model_validate(res_d),
                                        _report_files(), doc=doc))
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Could not open the report: {type(e).__name__}: {e}",
                                 type="error", duration=8)

    @reactive.effect
    @reactive.event(input.open_report_hyd)
    def _open_report_hyd():
        _open_built_report("hydraulics")

    @reactive.effect
    @reactive.event(input.open_report_fn)
    def _open_report_fn():
        _open_built_report("screening")

    @reactive.effect
    @reactive.event(input.open_report_concept)
    def _open_report_concept():
        _open_built_report("concept")

    #: What `generate_report` writes, by the key every reader uses. Only needed to recognise files
    #: on disk that this session did not write.
    _REPORT_FILES = {"html": "site_report.html", "pdf": "site_report.pdf",
                     "screening_html": "screening_report.html",
                     "screening_pdf": "screening_report.pdf",
                     "json": "assessment_results.json", "csv_metrics": "site_metrics.csv",
                     "csv_transit": "hyporheic_transit_times.csv",
                     "run_summary": "run_summary.json", "rtd_json": "rtd_distribution.json"}

    def _report_files() -> dict:
        """The built report files: this session's if it built them, otherwise whatever is on disk.

        `report_paths` is only set by a build, so a REOPENED project had the documents sitting in
        its folder and no way to reach them: the report nodes offer Open and Download off the back
        of this, and both went missing until you regenerated. Probing the folder makes a reopened
        project behave like the session that made it."""
        paths = report_paths()
        if paths:
            return paths
        d = work_dir / "report"
        if not d.is_dir():
            return {}
        return {k: str(d / f) for k, f in _REPORT_FILES.items() if (d / f).is_file()}

    def _report_bytes(fmt):
        p = _report_files().get(fmt)
        return Path(p).read_bytes() if p and Path(p).is_file() else b""

    @render.download(filename="site_report.html")
    def dl_report_html():
        yield _report_bytes("html")

    @render.download(filename="site_report.pdf")
    def dl_report_pdf():
        yield _report_bytes("pdf")

    # The screening document is a separate file, not a section of the one above. Both keys are
    # absent from an older project's report directory, so these yield empty rather than raising.
    @render.download(filename="screening_report.html")
    def dl_screening_html():
        yield _report_bytes("screening_html")

    @render.download(filename="screening_report.pdf")
    def dl_screening_pdf():
        yield _report_bytes("screening_pdf")

    # ONE SURFACE, ONE ID. Every one of these renders on the modal footer and nowhere else. The
    # report NODES used to carry a second copy of PDF and HTML, which forced a parallel id set
    # (a download button is an output, and the pane stays mounted behind the open modal, so the
    # two could not share one). Dropping the pane row dropped six handlers with it.

    # The Conceptual Model, built on demand rather than read off disk: it is a shipped figure
    # wrapped in a page, identical for every project, so `generate_report` never writes it and it
    # has no `_REPORT_FILES` entry.
    def _concept_pdf():
        return report_mod.concept_pdf_bytes((project_meta_v() or {}).get("name"))

    def _concept_html():
        return report_mod.concept_html((project_meta_v() or {}).get("name")).encode("utf-8")

    @render.download(filename="conceptual_model.pdf")
    def dl_concept_pdf():
        yield _concept_pdf()

    @render.download(filename="conceptual_model.html")
    def dl_concept_html():
        yield _concept_html()

    @render.download(filename="site_metrics.csv")
    def dl_report_csv():
        yield _report_bytes("csv_metrics")

    @render.download(filename="assessment_results.json")
    def dl_report_json():
        yield _report_bytes("json")

    @render.download(filename="run_summary.json")
    def dl_report_summary():
        yield _report_bytes("run_summary")

    @render.download(filename="rtd_distribution.json")
    def dl_report_rtd():
        yield _report_bytes("rtd_json")

    # ------------------------------------------------------------------
    # Hydraulic Alternatives: order-of-magnitude K / gradient sweep against the Basecase
    # ------------------------------------------------------------------
    alt_result = reactive.value(None)   # {"manifest": dict, "running": bool, "halted_on": str?}
    alt_view = reactive.value(None)     # displayed run: None = Basecase. NEVER persisted.
    _alt_proc: dict = {}
    _alt_payload: dict = {}             # stashed child payload; Retry/Continue relaunch over it
    _alt_stats_cache: dict = {}         # sid -> parsed hz_stats.json for the displayed-run panes
    _alt_switch = {"busy": False}       # re-entrancy guard for _set_displayed_run
    alt_log_lines: list = []
    alt_scen_recs: list = []            # incremental per-scenario records = the live status feed
    alt_log_tick = reactive.value(0)
    alt_t0 = reactive.value(None)
    alt_elapsed = reactive.value(0)

    @reactive.extended_task
    async def alt_task(payload: dict) -> dict:
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=alt_run.child_run, args=(payload, q), daemon=True)
            _alt_proc["p"] = p
            p.start()
            result = error = None
            scen_recs = []
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "log":
                        alt_log_lines.append(data)
                    elif kind == "scenario":
                        scen_recs.append(data)
                        alt_scen_recs.append(data)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:
                try:
                    kind, data = q.get_nowait()
                    if kind == "log":
                        alt_log_lines.append(data)
                    elif kind == "scenario":
                        scen_recs.append(data)
                        alt_scen_recs.append(data)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            cancelled = _alt_proc.pop("cancelled", False)
            _alt_proc["p"] = None
            if cancelled:
                return {"cancelled": True, "scenarios": scen_recs}
            if error is not None:
                return {"error": error, "scenarios": scen_recs}
            return result or {"scenarios": scen_recs}
        return await anyio.to_thread.run_sync(_work)

    def _alt_manifest():
        """The stored manifest dict (None when absent) — display readers use this raw."""
        ar = alt_result()
        return (ar or {}).get("manifest") or None

    def _normalize_alt_manifest(mf: dict) -> dict:
        """Upgrade a manifest dict written by an earlier build IN PLACE: the first release
        carried vary_k/vary_gradient (rejected by the strict contract now) and no selection
        key. Per-scenario k_factor/g_factor carry everything ranges and the table need, and
        old value-encoded ids stay viewable (their dirs exist on disk), so this is a
        pop-and-default. Harmless on current manifests."""
        mf.pop("vary_k", None)
        mf.pop("vary_gradient", None)
        mf.setdefault("selection", {})
        return mf

    def _alt_manifest_obj():
        """Validated manifest object, or None. Normalizes legacy shapes first: a validation
        miss here once hid the range cards, the supporting-ranges accordion, AND the pane's
        action buttons all at once (user report, 2026-08-02)."""
        from hype_app.contracts import HydraulicAlternativesManifest
        mf = _alt_manifest()
        if not mf:
            return None
        try:
            return HydraulicAlternativesManifest.model_validate(
                _normalize_alt_manifest(dict(mf)))
        except Exception:  # noqa: BLE001
            return None

    def _alt_scenario(sid):
        for s in (_alt_manifest() or {}).get("scenarios") or []:
            if s.get("id") == sid:
                return s
        return None

    def _alt_dir(sid) -> Path:
        return work_dir / "alternatives" / str(sid)

    def _alt_hz_dir(sid) -> Path:
        return _alt_dir(sid) / "summary" / "hz"

    def _alt_stats(sid):
        """One alternative's full hz stats bundle, disk-loaded once per sweep."""
        if sid in _alt_stats_cache:
            return _alt_stats_cache[sid]
        try:
            with open(_alt_hz_dir(sid) / "hz_stats.json", "r", encoding="utf-8") as f:
                st = json.load(f)
        except Exception:  # noqa: BLE001
            return None
        _alt_stats_cache[sid] = st
        return st

    def _alt_scenario_results(stats: dict, hz_dir) -> dict:
        """Full metric sections for ONE alternative, through the SAME assess.build_results call
        the report makes, so an alternative cannot report a different number than the Basecase
        it is compared against. Porosity follows the run's own knobs (the report-build rule).
        Returns the index-ready pieces: results_sections, the three primaries, QA, warnings."""
        from hype_app.contracts import AssessmentInputSnapshot
        snap = AssessmentInputSnapshot.model_validate(input_snapshot())
        full_stats = stats or {}
        cls_stats = full_stats.get("classes") or full_stats or {}
        hyp = cls_stats.get("hyporheic") or {}
        acct = (full_stats.get("flux") or {}).get("accounting") or {}
        net_exch = acct.get("net_stream_exchange")
        domain_vol = (full_stats.get("domain") or {}).get("active_saturated_volume_m3")
        fm = _flux_metrics(full_stats, hz_dir)
        porosity = signature.as_float((full_stats.get("knobs") or {}).get("porosity"))
        if porosity is None:
            porosity = snap.k.porosity
        res = assess.build_results(
            snap, hz_stats=cls_stats, streamflow_cms=snap.streamflow.value_cms,
            reach_length_m=_reach_length_m(), exchange=fm["exchange"],
            transit_times_days=fm["transit_times"], transit_weights=fm["transit_weights"],
            path_depths=fm["path_depths"], path_lengths=fm["path_lengths"],
            footprint_weighted_m2=hyp.get("footprint_m2"), porosity=porosity,
            snapshot_porosity=snap.k.porosity,
            censored_fraction=fm["censored"],
            streambed_area_m2=acct.get("streambed_area_m2"),
            active_streambed_area_m2=acct.get("active_streambed_area_m2"),
            return_streambed_area_m2=acct.get("return_streambed_area_m2"),
            connected_streambed_area_m2=acct.get("connected_streambed_area_m2"),
            net_stream_exchange_cms=(net_exch / 86400.0 if net_exch is not None else None),
            domain_volume_m3=domain_vol, hz_accounting=acct,
            function_inputs=None,          # screening never rides an alternative
            app_version=APP_VERSION)
        sections = {"connectivity": res.connectivity.model_dump(mode="json"),
                    "residence_time": res.residence_time.model_dump(mode="json"),
                    "zone": res.zone.model_dump(mode="json")}
        return {"results_sections": sections,
                "metrics": alt_mod.primaries_from_sections(sections),
                "quality": res.quality_diagnostics,
                "warnings": list(res.warnings)}

    def _alts_input_drift() -> list[str]:
        """What changed since the Basecase snapshot froze. K/grid/porosity text edits do not
        cascade-clear today, so this guard is what keeps reuse of the completed main run honest:
        alternatives only launch when the live inputs still match the frozen ones."""
        try:
            from hype_app.contracts import AssessmentInputSnapshot
            snap = AssessmentInputSnapshot.model_validate(input_snapshot())
        except Exception:  # noqa: BLE001
            return ["run inputs unreadable"]
        drift: list[str] = []

        def _diff(a, b):
            return a is not None and b is not None and abs(float(a) - float(b)) > 1e-9

        if _diff(snap.k.kh_m_day, _safe("kh", 10.0)) or _diff(snap.k.kv_m_day, _safe("kv", 1.0)):
            drift.append("hydraulic conductivity")
        if _diff(snap.k.porosity, _safe("porosity", 0.3)):
            drift.append("porosity")
        n_kz = len(kzone_feats() or []) if bool(_safe("use_kzones", False)) else 0
        if bool(snap.k.use_kzones) != bool(_safe("use_kzones", False)) or \
                (snap.k.use_kzones and snap.k.kzone_count != n_kz):
            drift.append("K zones")
        if _diff(snap.grid.cell_size_x, _safe("cell_size", 10.0)) or \
                _diff(snap.grid.gw_mod_depth, _safe("gw_mod_depth", 6.0)) or \
                _diff(snap.grid.layer_thickness, _safe("z", 0.25)):
            drift.append("grid settings")
        try:
            from hype_app import gradients as grad_mod
            cfg = _gradient_config()
            for side in ("left_controls", "right_controls"):
                if grad_mod.serialize_profile(getattr(snap.gradients, side)) != \
                        grad_mod.serialize_profile(getattr(cfg, side)):
                    drift.append("boundary gradients")
                    break
        except Exception:  # noqa: BLE001
            drift.append("boundary gradients")
        return drift

    def _write_alt_index(manifest_dict: dict) -> None:
        """alternatives/index.json: the on-disk record of the sweep (best-effort)."""
        try:
            d = work_dir / "alternatives"
            d.mkdir(parents=True, exist_ok=True)
            (d / "index.json").write_text(json.dumps(manifest_dict, indent=2, default=str))
        except Exception:  # noqa: BLE001
            pass

    def _sync_canonical_alternatives(state, *, persist: bool = True) -> None:
        """Attach/detach the settled, identity-matched sweep on the canonical results without
        rebuilding hydraulics. `state` is the alt_result wrapper (or None to detach)."""
        current = results_model()
        if not current:
            return
        try:
            frozen = frozenset(h for h in ((input_snapshot() or {}).get("input_hash"),) if h)
            updated = results_lifecycle.with_current_alternatives(current, state,
                                                                  extra_hashes=frozen)
            results_model.set(updated.model_dump(mode="json"))
            if persist and not _autosave["restoring"]:
                _save_project_file()
        except Exception as exc:  # noqa: BLE001 — alternatives must never cost the Basecase
            print(f"[alt] canonical attachment skipped: {exc}", flush=True)

    def _sweep_alt_dir():
        """Kill + disarm + wipe the alternatives batch: reactives, displayed run, on-disk dir.
        Fires on every {"gw","hz"} cascade slice and on re-delineation — the batch is only
        comparable to the Basecase it was computed against."""
        _task_armed["alt"] = False
        p = _alt_proc.get("p")
        if p is not None and p.is_alive():
            _alt_proc["cancelled"] = True
            p.kill()
        try:
            alt_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        _alt_payload.clear()
        _alt_stats_cache.clear()
        shutil.rmtree(work_dir / "alternatives", ignore_errors=True)
        with reactive.isolate():
            alt_view.set(None)
            alt_result.set(None)
            # Detach from the canonical results too (no immediate save: the cascade that
            # called this persists through the normal autosave path).
            _sync_canonical_alternatives(None, persist=False)

    def _alt_selection() -> dict:
        """The variant selection off the setup inputs: {role: multiplier|None, combos: bool}.
        An enabled variant with an empty numeric keeps the raw None VALUE so
        validate_selection can name the field (float(None) there -> 'Enter a multiplier')."""
        def _v(on_id, val_id, dflt):
            if not bool(_safe(on_id, True)):
                return None                     # checkbox off = variant off
            v = _safe(val_id, dflt)             # dflt while the input is unregistered
            # A cleared numeric yields None, but None means OFF to the selection model —
            # hand validate_selection an unfloatable sentinel so it says "Enter a multiplier".
            return "" if v is None else v
        return {"k_lower": _v("alt_k_lo_on", "alt_k_lo", 0.1),
                "k_upper": _v("alt_k_hi_on", "alt_k_hi", 10.0),
                "g_lower": _v("alt_g_lo_on", "alt_g_lo", 0.5),
                "g_higher": _v("alt_g_hi_on", "alt_g_hi", 2.0),
                "combos": bool(_safe("alt_combos", True))}

    @reactive.effect
    @reactive.event(input.run_alt_evt)
    async def _start_alts():
        sel = _alt_selection()
        errs = alt_mod.validate_selection(sel)
        if errs:
            ui.notification_show(errs[0], type="warning", duration=6)
            return
        if run_result() is None or hz_result() is None:
            ui.notification_show("Run the groundwater model and delineate the Hyporheic Zone "
                                 "first. Alternatives reuse that run as the Basecase.",
                                 type="warning", duration=7)
            return
        snap_dict = input_snapshot()
        if not snap_dict:
            ui.notification_show("This project predates frozen run inputs. Re-run the "
                                 "groundwater model first.", type="warning", duration=7)
            return
        if {"gw", "hz"} & _stale_marks():
            ui.notification_show("The groundwater results are stale. Re-run the model before "
                                 "computing alternatives.", type="warning", duration=7)
            return
        if any(_task_state(t) == "running" for t in (run_task, hz_task, alt_task)):
            ui.notification_show("Another model task is still running.", type="warning",
                                 duration=6)
            return
        if not runner.modflow_available():
            ui.notification_show(MODFLOW_UNAVAILABLE_MSG, type="error", duration=8)
            return
        drift = _alts_input_drift()
        if drift:
            ui.notification_show("Inputs changed since the groundwater run "
                                 f"({', '.join(sorted(set(drift)))}). Re-run it before "
                                 "computing alternatives.", type="warning", duration=8)
            return
        try:
            build = _domain_build()
            if not (build and dem_path() and _wse_path()):
                ui.notification_show("Alternatives need the domain, terrain, and a water "
                                     "surface. Same inputs as the groundwater run.",
                                     type="warning", duration=6)
                return
            _gradient_config()      # validate now: params() would silently fall back otherwise
            base_params = params()
            knobs = dict(((hz_result() or {}).get("stats") or {}).get("knobs") or {})
            # The MAIN delineation's particle settings, so alternative metrics are comparable
            # to the Basecase at the same density (frozen from the run's own record).
            hz_kw = {
                "particles_per_cell": int(knobs.get("particles_per_cell", 1)),
                "sample_per_class": int(knobs.get("sample_per_class", 300)),
                "porosity": float(knobs.get("porosity") or _safe("porosity", 0.3)),
                "hard_cap_particles": (10**9 if runmode.IS_DESKTOP else HZ_MAX_PARTICLES),
                "classes_for_volume": list(HZ_CLASSES),
            }
            for opt in ("max_time_days", "saturated_clip", "min_sat_frac",
                        "iface_particles_per_cell"):
                if knobs.get(opt) is not None:
                    hz_kw[opt] = knobs[opt]
            from hype_app.contracts import GRADIENT_METHOD_VERSION, RESULTS_SCHEMA_VERSION
            manifest = alt_mod.build_manifest(
                sel,
                base_input_hash=snap_dict.get("input_hash"),
                base_assessment_id=snap_dict.get("assessment_id"),
                app_version=APP_VERSION,
                method_versions={"results": RESULTS_SCHEMA_VERSION,
                                 "gradients": GRADIENT_METHOD_VERSION,
                                 "report": report_mod.REPORT_METHOD_VERSION},
                hz_knobs=hz_kw)
            crs = proj_crs()
            crs_id = crs.to_epsg() or crs.to_wkt()
            use_kz = bool(_safe("use_kzones", False))
            payload = {
                "crs": crs_id, "domain": build["domain"], "left": build["left"],
                "right": build["right"], "up": build["up"], "down": build["down"],
                "dem": active_dem(), "params": base_params,
                "wse_mode": "dem", "wse_path": _wse_path(),
                "wse_relief_thresh": float(_safe("wse_relief", 0.2)),
                "kzones": (kzone_feats() if use_kz else []),
                "kzone_kh": float(_safe("kzone_kh", 50.0)),
                "kzone_kv": float(_safe("kzone_kv", 5.0)),
                "left_profile": base_params["left_boundary_gradient_profile"],
                "right_profile": base_params["right_boundary_gradient_profile"],
                "hz": hz_kw,
                "alt_root": str(work_dir / "alternatives"),
                "scenarios": alt_mod.scenario_payloads(manifest.scenarios),
            }
            if bool(_safe("use_soil_k", False)) and soil_snapshot():
                from hype_app.soil_k import prepare_soil_k_payload
                kv0 = float(_safe("kv", 1.0)) or 1.0
                payload["soil_k"] = prepare_soil_k_payload(
                    soil_snapshot(), policy=str(_safe("soil_policy", "dominant")),
                    anisotropy_ratio=float(_safe("kh", 10.0)) / kv0,
                    fallback_kh=float(_safe("kh", 10.0)), fallback_kv=kv0)
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Couldn't start alternatives: {e}", type="error", duration=8)
            return
        await _set_displayed_run(None, quiet=True)
        _sweep_alt_dir()                    # a new sweep always starts clean
        _alt_payload["p"] = payload
        alt_log_lines.clear()
        alt_scen_recs.clear()
        alt_log_tick.set(0)
        alt_t0.set(time.monotonic())
        alt_result.set({"manifest": manifest.model_dump(mode="json"), "running": True})
        _task_armed["alt"] = True
        alt_task(dict(payload))

    def _relaunch_alts(scens) -> bool:
        """Retry/Continue after a halt: relaunch the child over the STASHED payload with a
        scenario sublist, so every leg of the sweep runs byte-identical inputs even if settings
        were edited while it sat halted (edits that matter cascade-wipe the batch anyway)."""
        payload = _alt_payload.get("p")
        sr = alt_result()
        mf = (sr or {}).get("manifest")
        if not payload or not mf or not scens:
            return False
        relaunch_ids = {s.id for s in scens}
        for row in mf.get("scenarios") or []:
            if row.get("id") in relaunch_ids:
                row["status"] = "pending"
                row["error"] = None
        p2 = dict(payload)
        p2["scenarios"] = alt_mod.scenario_payloads(scens)
        alt_result.set({"manifest": mf, "running": True})
        alt_t0.set(time.monotonic())
        _task_armed["alt"] = True
        alt_task(p2)
        return True

    def _alt_finalize_halt():
        """Stop after a halt: remaining scenarios become Not run and the index is written."""
        sr = alt_result() or {}
        mf = sr.get("manifest")
        if not mf:
            return
        for row in mf.get("scenarios") or []:
            if row.get("status") in ("pending", "running"):
                row["status"] = "not_run"
        alt_result.set({"manifest": mf, "running": False})
        _write_alt_index(mf)
        _sync_canonical_alternatives({"manifest": mf, "running": False})

    @reactive.effect
    @reactive.event(input.alt_retry_evt)
    def _alt_retry():
        sr = alt_result() or {}
        mfo = _alt_manifest_obj()
        if mfo is None or sr.get("running") or not sr.get("halted_on"):
            return
        if not _relaunch_alts(alt_mod.relaunch_scenarios(mfo, sr.get("halted_on"), retry=True)):
            ui.notification_show("Nothing to retry.", type="warning", duration=5)

    @reactive.effect
    @reactive.event(input.alt_continue_evt)
    def _alt_continue():
        sr = alt_result() or {}
        mfo = _alt_manifest_obj()
        if mfo is None or sr.get("running") or not sr.get("halted_on"):
            return
        scens = alt_mod.relaunch_scenarios(mfo, sr.get("halted_on"), retry=False)
        if not scens:
            _alt_finalize_halt()        # nothing left to run: same as Stop
            return
        _relaunch_alts(scens)

    @reactive.effect
    @reactive.event(input.alt_halt_stop_evt)
    def _alt_halt_stop():
        sr = alt_result() or {}
        if sr.get("running") or not sr.get("halted_on"):
            return
        _alt_finalize_halt()

    @reactive.effect
    @reactive.event(input.stop_alt_evt)
    def _alt_cancel():
        """Stop: flag-then-kill. The task returns {"cancelled": True, ...} with the completed
        records, so the done handler keeps finished runs and discards the incomplete one."""
        p = _alt_proc.get("p")
        if p is not None and p.is_alive():
            _alt_proc["cancelled"] = True
            p.kill()

    @reactive.effect
    def _alt_poll():
        if alt_task.status() != "running":
            return
        reactive.invalidate_later(0.5)
        alt_log_tick.set(len(alt_log_lines) + len(alt_scen_recs))
        t0 = alt_t0()
        if t0:
            alt_elapsed.set(int(time.monotonic() - t0))

    @reactive.effect
    def _alt_done():
        if alt_task.status() in ("initial", "running"):
            return
        if not _task_armed["alt"]:      # already applied (or session reset) — see _task_armed
            return
        _task_armed["alt"] = False
        try:
            res = alt_task.result()
        except Exception:  # noqa: BLE001
            return
        sr = alt_result()
        if not sr or not sr.get("running"):
            return
        from hype_app.contracts import AltStatus, HydraulicAlternativesManifest
        try:
            manifest = HydraulicAlternativesManifest.model_validate(sr["manifest"])
        except Exception:  # noqa: BLE001
            return

        def _dt(v):
            try:
                return datetime.fromisoformat(v) if v else None
            except Exception:  # noqa: BLE001
                return None

        recs = {r["id"]: r for r in (res.get("scenarios") or [])}
        for s in manifest.scenarios:
            rec = recs.get(s.id)
            if rec is None:
                continue
            s.duration_s = rec.get("duration_s")
            s.started_at = _dt(rec.get("started_at"))
            s.finished_at = _dt(rec.get("finished_at"))
            s.log_tail = [str(x) for x in (rec.get("log_tail") or [])][-40:]
            if rec.get("ok"):
                s.status = AltStatus.completed
                try:
                    pieces = _alt_scenario_results(rec.get("stats") or {}, rec.get("hz_dir"))
                    s.results_sections = pieces["results_sections"]
                    s.metrics = pieces["metrics"]
                    s.quality = pieces["quality"]
                    s.warnings = pieces["warnings"]
                except Exception as me:  # noqa: BLE001
                    s.results_sections = {}
                    s.metrics = {}
                    print(f"[alt] metrics failed for {s.id}: {me}")
            else:
                s.status = AltStatus.failed
                s.error = (rec.get("error") or "")[-1500:]
        halted_on = res.get("halted_on")
        cancelled = bool(res.get("cancelled"))
        child_error = res.get("error")
        if cancelled:
            # Stop discards the incomplete current run; later scenarios were never reached.
            first = True
            for s in manifest.scenarios:
                if s.status == AltStatus.pending:
                    s.status = AltStatus.cancelled if first else AltStatus.not_run
                    first = False
            manifest.cancelled = True
        elif child_error:
            for s in manifest.scenarios:
                if s.status == AltStatus.pending:
                    s.status = AltStatus.not_run
        # Failed dirs are removed by the child; a killed or crashed child can still leave the
        # current scenario's half-written dir behind — sweep everything not completed. Retry
        # briefly: a just-killed MODFLOW's file handles can outlive the process on Windows
        # (observed live: the dir deleted cleanly a moment after the first attempt failed).
        for s in manifest.scenarios:
            if s.status not in (AltStatus.completed, AltStatus.pending):
                d = _alt_dir(s.id)
                for attempt in range(3):
                    shutil.rmtree(d, ignore_errors=True)
                    if not d.exists():
                        break
                    time.sleep(0.3 * (attempt + 1))
                _alt_stats_cache.pop(s.id, None)
        out = {"manifest": manifest.model_dump(mode="json"), "running": False}
        if halted_on and not cancelled and not child_error:
            out["halted_on"] = halted_on
        alt_result.set(out)
        if not out.get("halted_on"):
            _write_alt_index(out["manifest"])
        # Canonical results reflect the settled sweep immediately (a halted wrapper detaches;
        # Stop re-attaches the completed subset through _alt_finalize_halt).
        _sync_canonical_alternatives(out)
        if child_error:
            ui.notification_show("The alternatives run failed. See the run log.",
                                 type="error", duration=8)
        elif cancelled:
            ui.notification_show("Alternatives stopped. Completed runs are kept.", duration=6)
        elif out.get("halted_on"):
            lbl = next((s.label for s in manifest.scenarios if s.id == halted_on), halted_on)
            ui.notification_show(f"Alternative {lbl} failed. Choose how to continue on the "
                                 "Hydraulic Alternatives pane.", type="error", duration=10)
        else:
            n_ok = len(manifest.completed())
            ui.notification_show(f"Alternatives complete. {n_ok} of "
                                 f"{len(manifest.scenarios)} runs succeeded.", duration=6)

    # ---- displayed-run mechanism -------------------------------------------------------

    @reactive.calc
    def hz_view():
        """The hz bundle for the DISPLAYED run: hz_result() on the Basecase, else the
        alternative's stats + hz_dir in the SAME shape, so a switching pane is a one-token
        substitution. Basecase-bound surfaces (report, screening, GMS, 3D) keep reading
        hz_result() directly."""
        sid = alt_view()
        if sid is None:
            return hz_result()
        st = _alt_stats(sid)
        return {"stats": st, "hz_dir": str(_alt_hz_dir(sid))} if st else None

    async def _set_displayed_run(sid, *, quiet=False):
        """Re-point every run-dependent 2-D surface at the Basecase (sid=None) or one
        alternative. The single owner of alt_view; every entry point funnels through here."""
        with reactive.isolate():
            cur = alt_view()
        if _alt_switch["busy"] or sid == cur:
            return
        _alt_switch["busy"] = True
        try:
            alt_view.set(sid)
            hz_sel_pids.set(())             # a path selection never survives a run switch
            if sid is None:
                res = run_result()
                if res is None:             # cascade-clear path: layers are already dropped
                    return
                _show_run_layers(res)
                hz = hz_result()
                if hz and hz.get("hz_dir") and Path(hz["hz_dir"]).is_dir():
                    await _show_hz_layers(hz["hz_dir"], reset_checks=False)
            else:
                if not _alt_hz_dir(sid).is_dir():
                    return
                _show_run_layers(None, base_dir=_alt_dir(sid))
                await _show_hz_layers(str(_alt_hz_dir(sid)), reset_checks=False)
            if not quiet:
                lbl = (_alt_scenario(sid) or {}).get("label", sid) if sid else "the Basecase"
                ui.notification_show(f"Displaying {lbl}.", duration=4)
        finally:
            _alt_switch["busy"] = False

    @reactive.effect
    @reactive.event(input.alt_row_view)
    async def _alt_row_view():
        sid = str(input.alt_row_view() or "")
        sr = alt_result() or {}
        if sr.get("running") or sr.get("halted_on"):
            return
        await _set_displayed_run(None if sid in ("", "base") else sid, quiet=True)

    @reactive.effect
    @reactive.event(input.alt_return_evt)
    async def _alt_return():
        await _set_displayed_run(None, quiet=True)

    def _base_only_note(what: str):
        """Amber chip for Basecase-bound surfaces while an alternative is displayed, so they
        are never silently mistaken for the alternative on the map."""
        if alt_view() is None:
            return None
        return ui.div(f"{what} always reads the Basecase run. The map is currently showing "
                      "an alternative.", class_="hype-warn")

    @render.ui
    def alt_banner():
        sid = alt_view()
        if sid is None:
            return None
        s = _alt_scenario(sid) or {}
        return ui.div(
            ui.span("Viewing alternative: ", class_="hype-alt-banner-lead"),
            ui.tags.b(s.get("label") or sid),
            ui.span("Basecase remains the project's primary result.",
                    class_="hype-alt-banner-note"),
            _evt_btn("alt_return_evt", "Return to Basecase", "btn-sm btn-outline-secondary"),
            class_="hype-alt-banner")

    # ---- pane --------------------------------------------------------------------------

    _ALT_STATUS_WORD = {"pending": "Queued", "running": "Running", "completed": "Complete",
                        "failed": "Failed", "cancelled": "Canceled", "not_run": "Not run"}

    def _alt_live_progress():
        """Live per-scenario status during a sweep: the incremental records plus the newest
        ALT/STEP markers scraped from the child log (run-pane precedent)."""
        _ = alt_log_tick()
        mf = _alt_manifest() or {}
        order = [s["id"] for s in mf.get("scenarios") or []]
        by_rec: dict = {}
        for r in alt_scen_recs:
            by_rec[r.get("id")] = "completed" if r.get("ok") else "failed"
        cur_label = cur_id = None
        cur_i = n = None
        for line in reversed(alt_log_lines[-200:]):
            m = re.match(r"ALT\s+(\d+)/(\d+)\s+\[(.+?)\]\s+(starting|complete|FAILED)", line)
            if m:
                if m.group(4) == "starting":
                    cur_i, n, cur_label = int(m.group(1)), int(m.group(2)), m.group(3)
                break
        if cur_label:
            cur_id = next((s["id"] for s in mf.get("scenarios") or []
                           if s.get("label") == cur_label), None)
            if cur_id in by_rec:        # its record already arrived: no longer running
                cur_id = None
        stage_txt = None
        for line in reversed(alt_log_lines[-80:]):
            m = re.search(r"HZ STEP\s+(\d+)/(\d+)", line)
            if m:
                stage_txt = f"Hyporheic analysis step {m.group(1)} of {m.group(2)}"
                break
            m = re.search(r"STEP\s+(\d+)", line)
            if m:
                stage_txt = f"Groundwater model step {m.group(1)} of {RUN_TOTAL}"
                break
        return {"by_rec": by_rec, "current": cur_id, "i": cur_i,
                "n": n or len(order), "done": len(by_rec),
                "label": cur_label, "stage": stage_txt}

    @render.ui
    def alt_status():
        if _task_state(alt_task) != "running":
            return None
        prog = _alt_live_progress()
        n = max(int(prog["n"] or 0), 1)
        head = (f"Alternative {prog['i']} of {n}: {prog['label']}"
                if prog.get("i") else "Waiting for the first alternative to start.")
        secs = int(alt_elapsed())
        mm, ss = divmod(secs, 60)
        pct = max(4, min(100, int(round(100.0 * prog["done"] / n))))
        sub = " · ".join(x for x in (f"{prog['done']} of {n} complete",
                                     prog.get("stage")) if x)
        return ui.div(
            ui.div(ui.div(class_="hype-spinner"), ui.span(head, class_="hype-run-label"),
                   ui.span(f"{mm}:{ss:02d}", class_="hype-elapsed"), class_="hype-run-head"),
            ui.div(ui.div(class_="hype-prog-bar", style=f"width:{pct}%;"), class_="hype-prog"),
            ui.div(sub, class_="hype-prog-label"),
            class_="hype-run-status")

    @render.text
    def alt_log():
        alt_log_tick()
        return "\n".join(alt_log_lines[-200:]) or "Starting the alternatives run."

    @reactive.effect
    async def _alt_status_push():
        """Live status words for the runs table, patched IN PLACE client-side
        (hype_alt_status -> tree.js). The pane never re-renders on a tick, so the user's
        vertical pane scroll and the table's horizontal scroll survive the whole sweep."""
        if _task_state(alt_task) != "running":
            return
        _ = alt_log_tick()
        live = _alt_live_progress()
        payload = {}
        for s in (_alt_manifest() or {}).get("scenarios") or []:
            st = live["by_rec"].get(
                s["id"], "running" if s["id"] == live.get("current")
                else (s.get("status") or "pending"))
            payload[s["id"]] = _ALT_STATUS_WORD.get(st, st)
        try:
            await session.send_custom_message("hype_alt_status", {"statuses": payload})
        except Exception:  # noqa: BLE001
            pass

    @reactive.effect
    @reactive.event(input.alt_clear_evt)
    async def _alt_clear():
        if (alt_result() or {}).get("running"):
            return
        await _set_displayed_run(None, quiet=True)
        _sweep_alt_dir()
        ui.notification_show("Alternatives cleared.", duration=4)

    def _alt_base_metrics():
        """The Basecase's three primaries, derived exactly as hz_signature derives them."""
        res = hz_result()
        if not res:
            return {}
        try:
            full_stats = res.get("stats") or {}
            sig = signature.derive(signature.SignatureInputs.from_hz_bundle(
                full_stats, _flux_metrics(full_stats, res.get("hz_dir")),
                streamflow_cms=_streamflow_cms(), reach_length_m=_reach_length_m(),
                snapshot_porosity=_safe("porosity", None)))
            sm, f = signature.scenario_metrics(sig), sig.frequency
            out = {"turnovers_per_km": f.get("turnovers_per_km"),
                   "rtd_median_days": sm.get("rtd_median_days"),
                   "equivalent_active_depth_m": sm.get("equivalent_active_depth_m")}
            return {k: v for k, v in out.items() if isinstance(v, (int, float))}
        except Exception:  # noqa: BLE001
            return {}

    @reactive.calc
    def _alt_base_rows():
        """report.metric_rows for the Basecase, so the supporting-range table and the report
        column include it in every envelope. Cached until the delineation changes."""
        res = hz_result()
        if not res or not input_snapshot():
            return None
        try:
            pieces = _alt_scenario_results(res.get("stats") or {}, res.get("hz_dir"))
            from hype_app.contracts import (AssessmentResultsV2, ConnectivityMetrics,
                                            ResidenceTimeMetrics, ZoneMetrics)
            secs = pieces["results_sections"]
            model = AssessmentResultsV2(
                assessment_id="base", input_hash="",
                connectivity=ConnectivityMetrics.model_validate(secs["connectivity"]),
                residence_time=ResidenceTimeMetrics.model_validate(secs["residence_time"]),
                zone=ZoneMetrics.model_validate(secs["zone"]))
            return report_mod.metric_rows(model)
        except Exception:  # noqa: BLE001
            return None

    def _alt_envelope_cards(mfo):
        """The three scenario-envelope ranges as ONE compact card row (.hype-kpi-grid), the
        partial label printed once below instead of on every card."""
        base_m = _alt_base_metrics()
        rng = alt_mod.primary_ranges(mfo, base_m)
        note = alt_mod.partial_note(mfo)
        items = []
        for name, key, unit, mult in (
                (report_mod.DIM_FREQUENCY, "turnovers_per_km", "turnovers/km", 1.0),
                (report_mod.DIM_DURATION, "rtd_median_days", "hr", 24.0),
                (report_mod.DIM_EXTENT, "equivalent_active_depth_m", "m", 1.0)):
            r = rng.get(key)
            val = (report_mod.fmt_range(r["lo"] * mult, r["hi"] * mult) or "n/a") if r else "n/a"
            base_v = base_m.get(key)
            items.append(ui.div(
                ui.div(name, class_="hype-kpi-name"),
                ui.div(ui.span(val, class_="hype-kpi-num" + ("" if val != "n/a" else " pending")),
                       ui.span(unit, class_="hype-kpi-unit"), class_="hype-kpi-val"),
                *([ui.div(f"Basecase: {report_mod.fmt(base_v * mult)}", class_="hype-kpi-sub")]
                  if base_v is not None else []),
                class_="hype-kpi-small"))
        return ui.TagList(
            ui.div(*items, class_="hype-kpi-grid hype-alt-cards"),
            *([ui.div(note, class_="hype-props-note")] if note else []))

    def _alt_runs_table(mf, *, interactive, viewing, live=None):
        """The one scenario table every pane state shares: Basecase row first, then the sweep
        in manifest order, with the user-specified columns."""
        def _cells(m):
            dur = (m or {}).get("rtd_median_days")
            vals = ((m or {}).get("turnovers_per_km"),
                    None if dur is None else dur * 24.0,
                    (m or {}).get("equivalent_active_depth_m"))
            return [ui.tags.td(report_mod.fmt(v)) for v in vals]

        def _row(sid, label, kf, gf, word, m, clickable, is_view):
            glyph = "●" if is_view else ("○" if clickable else "")
            cls = "hype-flow-row" + (" sel" if is_view else ("" if clickable else " disabled"))
            attrs = {"class_": cls, "data_alt_sid": sid}
            if clickable:
                attrs["onclick"] = ("Shiny.setInputValue('alt_row_view', "
                                    f"{json.dumps(sid)}, {{priority: 'event'}})")
            st_cls = "alt-status" + (" hype-alt-statecell"
                                     if word not in ("Current", "Complete") else "")
            return ui.tags.tr(
                ui.tags.td(glyph, class_="hype-alt-pick"),
                ui.tags.td(label),
                ui.tags.td(alt_mod.factor_text(kf)),
                ui.tags.td(alt_mod.factor_text(gf)),
                ui.tags.td(word, class_=st_cls),
                *_cells(m), **attrs)

        rows = [_row("base", "Basecase", 1.0, 1.0, "Current", _alt_base_metrics(),
                     interactive, viewing == "base")]
        for s in (mf or {}).get("scenarios") or []:
            st = s.get("status") or "pending"
            if live is not None:
                st = live["by_rec"].get(s["id"], "running" if s["id"] == live.get("current")
                                        else "pending")
            ok = st == "completed" and _alt_hz_dir(s["id"]).is_dir()
            rows.append(_row(s["id"], s.get("label") or s["id"], s.get("k_factor", 1.0),
                             s.get("g_factor", 1.0), _ALT_STATUS_WORD.get(st, st),
                             s.get("metrics") or {}, interactive and ok,
                             viewing == s["id"]))
        head = ui.tags.thead(ui.tags.tr(
            ui.tags.th("", title="Displayed run"),
            ui.tags.th("Scenario"),
            ui.tags.th("K", title="Hydraulic conductivity factor"),
            ui.tags.th("Gradient", title="Head gradient factor"),
            ui.tags.th("Status"),
            ui.tags.th("Frequency", ui.tags.span("turnovers/km", class_="hype-alt-unit"),
                       title=report_mod.DIM_FREQUENCY),
            ui.tags.th("Duration", ui.tags.span("median hr", class_="hype-alt-unit"),
                       title=f"{report_mod.DIM_DURATION}: median residence time"),
            ui.tags.th("Extent", ui.tags.span("m", class_="hype-alt-unit"),
                       title=f"{report_mod.DIM_EXTENT}: equivalent active depth")))
        return ui.div(ui.tags.table(head, ui.tags.tbody(*rows),
                                    class_="table table-sm hype-flow-table hype-alt-table"),
                      class_="hype-alt-tablewrap")

    def _alt_supporting_ranges(mfo):
        """Collapsed full-vocabulary range table: Metric | Basecase | Range."""
        base_rows = _alt_base_rows()
        ranges = alt_mod.metric_ranges(mfo, base_rows)
        base_map = {(r["section"], r["name"]): r for r in (base_rows or [])}
        order = ([(r["section"], r["name"]) for r in base_rows] if base_rows
                 else list(ranges.keys()))
        trs = []
        seen = set()
        for key in order:
            r = ranges.get(key)
            if key in seen or r is None or r["n"] < 2:
                continue
            seen.add(key)
            _sec, name = key
            b = base_map.get(key)
            base_txt = (report_mod.fmt(b["value_raw"])
                        if b and isinstance(b.get("value_raw"), (int, float)) else "n/a")
            unit = r.get("unit") or ""
            trs.append(ui.tags.tr(
                ui.tags.td(name + (f" ({unit})" if unit else "")),
                ui.tags.td(base_txt),
                ui.tags.td(report_mod.fmt_range(r["lo"], r["hi"]) or "n/a")))
        if not trs:
            return None
        note = alt_mod.partial_note(mfo)
        tbl = ui.tags.table(
            ui.tags.thead(ui.tags.tr(ui.tags.th("Metric"), ui.tags.th("Basecase"),
                                     ui.tags.th("Range"))),
            ui.tags.tbody(*trs), class_="hype-props-table")
        body = ([ui.div(note, class_="hype-instr")] if note else []) + [tbl]
        return ui.accordion(ui.accordion_panel("Supporting metric ranges", *body),
                            open=False, id="alt_more_acc")

    def _alt_variant_row(on_id, val_id, label, dflt):
        """One setup row: checkbox | x-prefixed multiplier numeric (kept ids, _KEEP_IDS).
        The times glyph + the Multiplier column header say what the number IS, so the pane
        needs no explanatory note."""
        return ui.div(
            ui.input_checkbox(on_id, label, value=bool(_keep(on_id, True))),
            ui.div(ui.span("×", class_="hype-alt-x"),
                   ui.input_numeric(val_id, None, value=_keep(val_id, dflt), step=0.1,
                                    width="88px"),
                   class_="hype-alt-multcell"),
            class_="hype-alt-setuprow")

    def _alt_settings_rows():
        """The variant rows + combos checkbox, and nothing else: buttons never live in here
        (they were once inside a collapsed accordion and the user could not find them)."""
        return [
            ui.div(ui.span(""), ui.span("Multiplier", class_="hype-alt-multlabel"),
                   class_="hype-alt-setuprow hype-alt-multhead"),
            _alt_variant_row("alt_k_lo_on", "alt_k_lo", "K lower", 0.1),
            _alt_variant_row("alt_k_hi_on", "alt_k_hi", "K upper", 10.0),
            _alt_variant_row("alt_g_lo_on", "alt_g_lo", "Gradient lower", 0.5),
            _alt_variant_row("alt_g_hi_on", "alt_g_hi", "Gradient higher", 2.0),
            ui.input_checkbox("alt_combos", "Include combined scenarios",
                              value=bool(_keep("alt_combos", True)))]

    def _alt_action_bar(*, rerun, clearable, run=True):
        """One thin status line + the persistent bottom action row. Always the LAST block of
        the pane in every non-running state, never inside an accordion, and never gated on
        manifest validity."""
        parts = []
        row = []
        if run:
            sel = _alt_selection()
            errs = alt_mod.validate_selection(sel)
            n = 0 if errs else len(alt_mod.build_scenarios(sel))
            note = errs[0] if errs else (f"{n} alternative runs."
                                         + (" Replaces the current results." if rerun else ""))
            parts.append(ui.div(note, class_=("hype-warn" if errs else "hype-instr")))
            row.append(_evt_btn("run_alt_evt", "Run alternatives", "btn-primary",
                                disabled=bool(errs)))
        if clearable:
            row.append(_evt_btn("alt_clear_evt", "Clear alternatives",
                                "btn-sm btn-outline-danger"))
        parts.append(ui.div(*row, class_="hype-actions"))
        return parts

    def _alt_log_accordion(open_=False):
        """The run log, collapsed by default and height-capped: diagnostic, not headline."""
        return ui.accordion(
            ui.accordion_panel("Run log",
                               ui.tags.pre(ui.output_text("alt_log"),
                                           class_="hype-log hype-alt-log")),
            open=("Run log" if open_ else False), id="alt_log_acc")

    def _alt_table_title():
        return ui.div("Alternative runs",
                      _info_tip("Click a Complete run to display it on the map and in the "
                                "results panes. The Basecase remains the primary result."),
                      class_="hype-props-title")

    def _alt_failure_card(sr):
        sid = sr.get("halted_on")
        s = _alt_scenario(sid) or {}
        return ui.TagList(
            ui.div(f"Alternative {s.get('label') or sid} failed. See the log for details.",
                   class_="hype-warn"),
            ui.div(_evt_btn("alt_retry_evt", "Retry this alternative", "btn-sm btn-primary"),
                   _evt_btn("alt_continue_evt", "Continue with remaining",
                            "btn-sm btn-outline-secondary"),
                   _evt_btn("alt_halt_stop_evt", "Stop the sweep", "btn-sm btn-outline-danger"),
                   class_="hype-actions"))

    def _pane_alt():
        """States: setup (no manifest) / running / halted / results. THE RUNNING BRANCH READS
        NO TICK REACTIVES: the pane renders once per state, live data flows through the
        alt_status output, the alt_log text output, and hype_alt_status in-place cell patches,
        so the pane's vertical scroll and the table's horizontal scroll survive the sweep."""
        sr = alt_result() or {}
        mf = sr.get("manifest")
        running = _task_state(alt_task) == "running" or bool(sr.get("running"))
        halted = bool(sr.get("halted_on")) and not running
        if running:
            return ui.TagList(
                ui.output_ui("alt_status"),
                ui.div(_evt_btn("stop_alt_evt", "Stop", "btn-sm btn-outline-danger"),
                       class_="hype-actions"),
                _alt_runs_table(mf, interactive=False, viewing="base"),
                _alt_log_accordion())
        if not mf:
            return ui.TagList(
                ui.div("Test how sensitive the results are to changes in hydraulic "
                       "conductivity and head gradient. Each alternative re-runs the "
                       "groundwater model and the Hyporheic Zone analysis with all other "
                       "settings unchanged. Your completed main run is the Basecase.",
                       class_="hype-instr"),
                *_alt_settings_rows(),
                *_alt_action_bar(rerun=False, clearable=False))
        mfo = _alt_manifest_obj()
        viewing = alt_view() or "base"
        parts = []
        if halted:
            parts.append(_alt_failure_card(sr))
        else:
            cur = (input_snapshot() or {}).get("input_hash")
            if mf.get("base_input_hash") and cur and mf["base_input_hash"] != cur:
                parts.append(ui.div("These alternatives were run against an earlier Basecase. "
                                    "Run them again to match the current model inputs.",
                                    class_="hype-warn"))
            if mfo is not None:
                parts.append(_alt_envelope_cards(mfo))
        parts.append(_alt_table_title())
        parts.append(_alt_runs_table(mf, interactive=not halted, viewing=viewing))
        if halted:
            parts.append(_alt_log_accordion(open_=True))
            parts.extend(_alt_action_bar(rerun=True, clearable=True, run=False))
        else:
            sup = _alt_supporting_ranges(mfo) if mfo is not None else None
            if sup is not None:
                parts.append(sup)
            parts.append(ui.accordion(
                ui.accordion_panel("Sweep settings", *_alt_settings_rows()),
                open=False, id="alt_rerun_acc"))
            parts.extend(_alt_action_bar(rerun=True, clearable=True))
        return ui.TagList(*parts)

    # ------------------------------------------------------------------
    # Structured / qualitative gradients (revision §7): config + reference slope
    # ------------------------------------------------------------------
    def _reference_slope():
        """ReferenceSlope for qualitative categories: the user's override when set, else the auto
        centerline-method slope (water-surface first, DEM fallback — ref_slope_auto). None when
        neither exists (flat/adverse and nothing typed)."""
        from hype_app import gradients as grad_mod
        ov = ref_slope_override()
        if ov is not None and float(ov) > 0:
            return grad_mod.ReferenceSlope(value=float(ov), source="manual",
                                           method="user override")
        return ref_slope_auto()

    def _g4(x):
        """Gradients are a 4-decimal quantity everywhere they're consumed (input echo, head
        preview, engine profile) — one convention, no drift between surfaces."""
        return round(float(x), 4)

    def _points_controls(side):
        """GradientControls for one side of the points mode: the corner numerics are the mandatory
        station-0/1 controls, plus the intermediate map points. Explicit lower/upper bounds on a
        saved point still ride along (contract compat) but nothing consumes them anymore — the
        Hydraulic Alternatives sweep scales the whole profile instead."""
        from hype_app.contracts import GradientControl
        grad_ver()                                   # in-place gradient edits invalidate too
        k0, k1 = ("g_ul", "g_dl") if side.value == "left" else ("g_ur", "g_dr")
        ctls = [GradientControl(id=f"{side.value}-0", side=side, station=0.0,
                                preferred=_g4(_safe(k0, 0.005)), source="manual"),
                GradientControl(id=f"{side.value}-1", side=side, station=1.0,
                                preferred=_g4(_safe(k1, 0.005)), source="manual")]
        for p in grad_pts():
            if p["side"] == side.value:
                ctls.append(GradientControl(
                    id=f"{side.value}-{p['id']}", side=side, station=float(p["station"]),
                    preferred=_g4(p["gradient"]), lower=p.get("lower"), upper=p.get("upper"),
                    source="manual"))
        return ctls

    def _gradient_config():
        """GradientBoundaryConfigV2 from the current UI (qualitative or gradient-points modes).
        Raises ValueError with a user-facing message when qualitative has no usable slope. A
        stale legacy corner bc_mode falls into the points branch — same corner numerics."""
        from hype_app.contracts import (GradientBoundaryConfigV2, GradientQualitative, Side)
        bc = _safe("bc_mode", BC_QUAL)
        if bc == BC_QUAL:
            rs = _reference_slope()
            if rs is None:
                raise ValueError("No usable reference slope (flat or adverse reach) — enter one "
                                 "in the Reference slope field.")
            slight = max(0.0, float(_safe("g_mult_slight", 0.5) or 0.0))
            strong = max(0.0, float(_safe("g_mult_strong", 1.0) or 0.0))
            left = GradientQualitative(_safe("g_qual_left", "slightly_gaining"))
            right = GradientQualitative(_safe("g_qual_right", "slightly_gaining"))
            return GradientBoundaryConfigV2.from_qualitative(
                left=left, right=right, reference_slope=rs, slight=slight, strong=strong)
        return GradientBoundaryConfigV2(mode="quantitative",
                                        left_controls=_points_controls(Side.left),
                                        right_controls=_points_controls(Side.right))

    @reactive.calc
    def wse_edge_samples():
        """Wetted-edge samples of the current WSE raster, in BOTH the metric project CRS (x/y —
        distance math; the raster itself may be geographic, e.g. py3dep DEMs) and 4326 (lon/lat —
        display). None until a water surface + projected CRS exist."""
        p, crs = wse_preview_path(), proj_crs()
        if not p or crs is None:
            return None
        try:
            from pyproj import Transformer

            from hype_app import wse_index
            raw = wse_index.build_edge_samples(p)
            if raw is None:
                return None
            xm, ym = Transformer.from_crs(raw["crs"], crs,
                                          always_xy=True).transform(raw["x"], raw["y"])
            lon, lat = Transformer.from_crs(raw["crs"], "EPSG:4326",
                                            always_xy=True).transform(raw["x"], raw["y"])
            return {"x": xm, "y": ym, "value": raw["value"], "lon": lon, "lat": lat}
        except Exception:  # noqa: BLE001
            return None

    @reactive.calc
    def cap_wse_anchors():
        """Cap-anchored WSE for the four domain corners: per corner, the valid WSE sample
        nearest it (metric distance) along its OWN boundary cap — upstream cap for ul/ur,
        downstream cap for dl/dr. {"ul": {"wse","dist","edge"} | None, ...} with dist in
        proj-CRS metres and edge as (lon, lat) for display. None overall until domain + CRS +
        water surface exist; per-corner None when that cap never crosses wetted WSE (callers
        fall back to the nearest-edge snap). The engine anchors along the corner-to-corner
        chord, so a hand-bowed multi-vertex cap can diverge slightly — post-conditioning caps
        are 2-pt chords, where the two coincide."""
        build, crs, p = _domain_build(), proj_crs(), wse_preview_path()
        if not build or crs is None or not p:
            return None
        try:
            import numpy as np
            from pyproj import Transformer

            from hype_app import wse_index
            to_ll = None
            out = {}
            for cap_key, corner_keys in (("up", ("ul", "ur")), ("down", ("dl", "dr"))):
                feat = build.get(cap_key)
                raw = wse_index.valid_samples_along_line(p, feat) if feat else None
                if raw is None:
                    out[corner_keys[0]] = out[corner_keys[1]] = None
                    continue
                if to_ll is None:
                    to_m = Transformer.from_crs(raw["crs"], crs, always_xy=True)
                    to_ll = Transformer.from_crs(raw["crs"], "EPSG:4326", always_xy=True)
                    ll_to_m = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
                xm, ym = to_m.transform(raw["x"], raw["y"])
                lon, lat = to_ll.transform(raw["x"], raw["y"])
                xm, ym = np.asarray(xm), np.asarray(ym)
                cc = feat["geometry"]["coordinates"]        # cap runs left→right: [0]=l, [-1]=r
                cx, cy = ll_to_m.transform([cc[0][0], cc[-1][0]], [cc[0][1], cc[-1][1]])
                for k, x0, y0 in ((corner_keys[0], cx[0], cy[0]),
                                  (corner_keys[1], cx[1], cy[1])):
                    dx, dy = xm - float(x0), ym - float(y0)
                    i = int(np.argmin(dx * dx + dy * dy))
                    out[k] = {"wse": float(raw["value"][i]),
                              "dist": float(np.hypot(dx[i], dy[i])),
                              "edge": (float(np.asarray(lon)[i]), float(np.asarray(lat)[i]))}
            return out
        except Exception:  # noqa: BLE001
            return None

    @reactive.calc
    def grad_point_heads():
        """Preview rows for every gradient-specified point: the four corners (both modes — the
        ONLY rows in qualitative mode, where the gradient is the signed multiplier × reference
        slope) plus the intermediate points (points mode). Corner rows anchor to the WSE along
        their OWN boundary cap (cap_wse_anchors — the valid sample nearest the corner on the cap
        line), falling back to the nearest wetted edge when that cap never crosses water;
        intermediate rows snap to the nearest wetted WSE cell. head = WSE + gradient × distance,
        the same anchor formula the engine applies along each side at run time. edge/wse/dist/
        head stay None until a water surface exists; in qualitative mode with no usable
        reference slope, gradient/head stay None (anchors still shown, pills read "h —")."""
        build, crs = _domain_build(), proj_crs()
        if not build or crs is None:
            return []
        grad_ver()                                   # in-place gradient edits recompute heads
        bc_qual = str(_safe("bc_mode", BC_QUAL)) == BC_QUAL   # legacy corner rides points branch
        pts = [] if bc_qual else grad_pts()
        qual_g = {"left": None, "right": None}
        if bc_qual:
            try:
                cfg = _gradient_config()
                qual_g = {"left": _g4(cfg.left_controls[0].preferred),
                          "right": _g4(cfg.right_controls[0].preferred)}
            except Exception:  # noqa: BLE001 — no usable slope: anchors shown, heads "h —"
                pass
        idx = wse_edge_samples()
        caps = cap_wse_anchors()
        import geopandas as gpd
        from pyproj import Transformer
        from shapely.geometry import shape as _shape

        from hype_app import gradients as grad_mod
        from hype_app import wse_index
        rows = []
        try:
            back = Transformer.from_crs(crs, "EPSG:4326", always_xy=True)
            for side, k0, k1 in (("left", "g_ul", "g_dl"), ("right", "g_ur", "g_dr")):
                feat = build[side]
                ln = gpd.GeoSeries([_shape(feat["geometry"])], crs=4326).to_crs(crs).iloc[0]
                if ln.is_empty or ln.length <= 0:
                    continue
                c4326 = feat["geometry"]["coordinates"]
                first, last = ln.coords[0], ln.coords[-1]
                g0 = qual_g[side] if bc_qual else _g4(_safe(k0, 0.005))
                g1 = qual_g[side] if bc_qual else _g4(_safe(k1, 0.005))
                entries = [(0.0, g0, k0[2:],
                            (float(c4326[0][0]), float(c4326[0][1])), (first[0], first[1])),
                           (1.0, g1, k1[2:],
                            (float(c4326[-1][0]), float(c4326[-1][1])), (last[0], last[1]))]
                for p in pts:
                    if p["side"] != side:
                        continue
                    q = ln.interpolate(float(p["station"]) * ln.length)
                    lon, lat = back.transform(q.x, q.y)
                    entries.append((float(p["station"]), _g4(p["gradient"]), p["id"],
                                    (lon, lat), (q.x, q.y)))
                for stn, g, uid, lonlat, xy in sorted(entries, key=lambda e: e[0]):
                    row = {"uid": uid, "side": side, "station": stn, "gradient": g, "pt": lonlat,
                           "edge": None, "wse": None, "dist": None, "head": None}
                    cap = (caps.get(uid)
                           if caps and uid in ("ul", "dl", "ur", "dr") else None)
                    if cap is not None:              # corner: anchored along its own cap line
                        row.update(edge=cap["edge"], wse=cap["wse"], dist=cap["dist"],
                                   head=(None if g is None else
                                         grad_mod.anchor_head(cap["wse"], g, cap["dist"])))
                    elif idx is not None:
                        d, w, _ex, _ey, i = wse_index.nearest_edge(idx, xy[0], xy[1])
                        row.update(edge=(float(idx["lon"][i]), float(idx["lat"][i])),
                                   wse=w, dist=d,
                                   head=(None if g is None else
                                         grad_mod.anchor_head(w, g, d)))
                    rows.append(row)
        except Exception:  # noqa: BLE001
            return rows
        return rows

    _wells_grid_cache: dict = {}    # (grb path, mtime) -> wells_mod.load_grid dict (one entry)
    _wells_seen: dict = {}          # last input value applied per well field (mirror change guard)

    @reactive.calc
    def well_samples():
        """Sampled rows for every observation well (computed head at the screen elevation,
        residual, or a reason string).

        Reads the BASECASE artifacts directly (work_dir paths) — never head_tifs(), which
        follows the displayed alternative, and never alt_view(): calibration is defined
        against the Basecase, exactly like the report. run_result() is the lifecycle dep
        that blanks every computed cell the moment GW results clear and refills them on
        the next completed run."""
        wls = obs_wells()
        wells_ver()                                     # in-place edits recompute
        crs = proj_crs()
        if not wls:
            return []
        run = run_result()
        try:
            if run is None:
                return wells_mod.sample_wells(wls, crs=crs, no_run=True)
            tifs = results.head_rasters(work_dir)
            gwf_ws = work_dir / "model" / "gwf_workspace"
            grid = None
            grb = next(gwf_ws.glob("*.dis.grb"), None) if gwf_ws.exists() else None
            if grb is not None:
                key = (str(grb), grb.stat().st_mtime)
                grid = _wells_grid_cache.get(key)
                if grid is None:
                    _wells_grid_cache.clear()           # one run's geometry at a time
                    grid = wells_mod.load_grid(gwf_ws)
                    if grid is not None:
                        _wells_grid_cache[key] = grid
            return wells_mod.sample_wells(wls, crs=crs, tifs=tifs, grid=grid)
        except Exception:  # noqa: BLE001 — sampling must never take the session down
            return wells_mod.sample_wells(wls, crs=None, no_run=(run is None))

    @reactive.calc
    def well_pair_rows():
        return wells_mod.pair_rows(well_pairs(), {r["id"]: r for r in well_samples()})

    @render.ui
    def gradient_qual_preview():
        # Text-only (the slope + multiplier inputs live statically in the pane — re-rendering
        # here never remounts an input mid-keystroke). One dim note with the two candidate
        # slopes so the user can choose what to type into Reference slope; the reset link
        # appears only while a manual override is active.
        try:
            _ = (input.g_qual_left(), input.g_qual_right(),
                 input.g_mult_slight(), input.g_mult_strong())      # subscribe
        except Exception:  # noqa: BLE001
            pass
        ov, auto = ref_slope_override(), ref_slope_auto()
        dem_s, wse_s = dem_slope_centerline(), wse_slope_centerline()

        def _f(s):
            return f"{s.value:.5f}" if s is not None else "n/a"
        reset = (ui.tags.button(
                     "reset to auto", type="button", class_="hype-link",
                     onclick="Shiny.setInputValue('g_ref_auto_evt',Date.now(),{priority:'event'})")
                 if ov is not None and auto is not None else None)
        bits = [ui.div(ui.span(f"DEM slope: {_f(dem_s)}. Water surface slope: {_f(wse_s)}."),
                       reset, class_="hype-instr hype-dim")]
        try:
            _gradient_config()                       # no usable slope -> visible error
        except ValueError as e:
            bits.append(ui.div(str(e), class_="hype-warn"))
        return ui.TagList(*bits)

    def _gpt_cell_text(r):
        """Display strings for one gradient-point row — shared by the initial table paint and
        the hype_gpt_cells patch so both surfaces always agree."""
        if r.get("head") is None:
            return {"wse": "—", "dist": "—", "head": "—"}
        return {"wse": f"{r['wse']:.2f}", "dist": f"{r['dist']:.0f}",
                "head": f"{r['head']:.2f}"}

    @reactive.effect
    async def _push_gpt_cells():
        # Typing a gradient recomputes heads, but the table output must NOT re-render per
        # keystroke (a re-render remounts the input being typed in and drops focus) — so the
        # computed cells are patched in place instead (tree.js: hype_gpt_cells). Gated like the
        # map overlay so heads aren't recomputed while the pane can't show them.
        from hype_app import gradients as grad_mod
        if sel_node() != "gw" or str(_safe("bc_mode", BC_QUAL)) != BC_PROFILE:
            return
        rows = grad_point_heads()
        warn = grad_mod.downstream_wse_warnings(rows)
        cells = {r["uid"]: {**_gpt_cell_text(r), "warn": r["uid"] in warn} for r in rows}
        if cells:
            await session.send_custom_message("hype_gpt_cells", {"cells": cells})

    @render.ui
    def gradient_pts_table():
        # STRUCTURAL renderer only (grad_pts + grad_adding): the gradient numerics mount here
        # once per add/remove. Corner edits are plain reactive input reads elsewhere and point
        # edits route through _gpt_mirror; the computed cells start from an isolated snapshot
        # and stay live via _push_gpt_cells. Nothing here may subscribe to heads or input values.
        from hype_app import gradients as grad_mod
        pts, arm = grad_pts(), grad_adding()
        with reactive.isolate():
            hrows = grad_point_heads()
            snap = {r["uid"]: _gpt_cell_text(r) for r in hrows}
            warn0 = grad_mod.downstream_wse_warnings(hrows)

        def _row(uid, side, stn, iid, val, cap=None):
            cells = snap.get(uid) or {"wse": "—", "dist": "—", "head": "—"}
            rm = (ui.tags.td() if cap else ui.tags.td(          # corners are mandatory anchors
                ui.tags.button("×", type="button", class_="hype-gpt-rm", title="Remove point",
                               onclick=("Shiny.setInputValue('gpt_rm','" + uid
                                        + ":'+Date.now(),{priority:'event'})"))))
            return ui.tags.tr(
                ui.tags.td(f"{side.capitalize()} · {stn:.0%}",
                           (ui.span(f" {cap[0]}", class_="hype-gpt-cap",
                                    title=f"{cap[1]} corner — required") if cap else None)),
                ui.tags.td(ui.div(
                    ui.input_numeric(iid, None, value=_g4(val), step=0.0001, width="64px"),
                    ui.span("⚠", class_="hype-gpt-warn gpt-warn",
                            title="WSE is higher downstream of this point — verify the snapped "
                                  "WSE cell location (dashed arrow) and the hydraulic-model "
                                  "water surface here.",
                            style=(None if uid in warn0 else "display:none")),
                    class_="hype-gpt-gcell")),
                ui.tags.td(cells["wse"], class_="gpt-wse"),
                ui.tags.td(cells["dist"], class_="gpt-dist"),
                ui.tags.td(cells["head"], class_="gpt-head"),
                rm, data_uid=uid)

        trs = []
        for side, k0, k1 in (("left", "g_ul", "g_dl"), ("right", "g_ur", "g_dr")):
            trs.append(_row(k0[2:], side, 0.0, k0, _keep(k0, 0.005), ("↑", "upstream")))
            for p in sorted(pts, key=lambda p: p["station"]):
                if p["side"] == side:
                    trs.append(_row(p["id"], side, float(p["station"]),
                                    f"gpt_g_{p['id']}", p["gradient"]))
            trs.append(_row(k1[2:], side, 1.0, k1, _keep(k1, 0.005), ("↓", "downstream")))
        table = ui.tags.table(
            ui.tags.thead(ui.tags.tr(
                ui.tags.th("Point"),
                ui.tags.th("Gradient (m/m)", title="+ gaining · − losing"),
                ui.tags.th("WSE (m)"), ui.tags.th("Dist (m)"), ui.tags.th("Head (m)"),
                ui.tags.th(""))),
            ui.tags.tbody(*trs), class_="table table-sm hype-gpt-table")
        if arm:
            tail = ui.div(
                ui.span(f"Click the {arm} floodplain line on the map…"),
                ui.tags.button("cancel", type="button", class_="hype-link",
                               onclick="Shiny.setInputValue('gpt_arm','off:'+Date.now(),"
                                       "{priority:'event'})"),
                class_="hype-instr")
        else:
            def _arm_btn(side):
                return ui.tags.button(
                    f"+ Point on {side}", type="button",
                    class_="btn btn-sm btn-outline-secondary",
                    onclick=(f"Shiny.setInputValue('gpt_arm','{side}:'+Date.now(),"
                             + "{priority:'event'})"))
            tail = ui.div(_arm_btn("left"), _arm_btn("right"), class_="hype-actions hype-gpt-add")
        return ui.TagList(table, tail)

    @render.ui
    def gradient_pts_msgs():
        # Hints + validation warnings only; free to re-render (contains no inputs).
        from hype_app import gradients as grad_mod
        rows = grad_point_heads()
        if not rows:
            return None
        parts = []
        if rows[0]["head"] is None:
            parts.append(ui.div("Run the surface model, draw a wetted extent, or upload a water "
                                "surface to compute heads.", class_="hype-instr"))
        try:
            parts += [ui.div(w.message, class_="hype-warn")
                      for w in grad_mod.validate_config(_gradient_config())]
        except ValueError as e:
            parts.append(ui.div(str(e), class_="hype-warn"))
        return ui.TagList(*parts)

    # ---- observation wells pane (gw.wells) --------------------------------------------------
    def _wells_cell_text(r):
        """Display strings for one well row — shared by the initial table paint and the
        hype_wells_cells patch so both surfaces always agree. Missing values render "n/a"
        (the fmt convention), with the reason in the cell tooltip."""
        if r.get("computed") is None:
            return {"comp": "n/a", "resid": "n/a", "title": r.get("reason") or ""}
        resid = "n/a" if r.get("residual") is None else f"{r['residual']:+.2f}"
        return {"comp": f"{r['computed']:.2f}", "resid": resid,
                "title": f"model layer {r['layer']}" if r.get("layer") else ""}

    def _wells_pair_text(r):
        """Display strings for one tracked-pair row (same shared-formatter discipline)."""
        return {"dist": "n/a" if r.get("distance") is None else f"{r['distance']:.1f}",
                "cg": ("n/a" if r.get("computed_gradient") is None
                       else f"{r['computed_gradient']:.4f}"),
                "og": ("n/a" if r.get("observed_gradient") is None
                       else f"{r['observed_gradient']:.4f}"),
                "title": r.get("reason") or ""}

    @reactive.effect
    async def _push_wells_cells():
        # Typing a screen elevation / observed head recomputes samples, but the table output
        # must NOT re-render per keystroke (a re-render remounts the input being typed in and
        # drops focus) — the computed cells are patched in place instead (tree.js:
        # hype_wells_cells). Names ride along so pair rows and the pickers follow renames live.
        if sel_node() != "gw.wells":
            return
        srows, prows = well_samples(), well_pair_rows()
        if not srows and not prows:
            return
        await session.send_custom_message("hype_wells_cells", {
            "wells": {r["id"]: _wells_cell_text(r) for r in srows},
            "names": {r["id"]: (r.get("name") or "") for r in srows},
            "pairs": {p["id"]: _wells_pair_text(p) for p in prows}})

    @render.ui
    def wells_table():
        # STRUCTURAL renderer only (obs_wells + wells_adding): the per-well inputs mount here
        # once per add/remove. Edits route through _wells_mirror; the computed cells start
        # from an isolated snapshot and stay live via _push_wells_cells. Nothing here may
        # subscribe to well_samples() or input values (the gradient-table rule).
        wls, arm = obs_wells(), wells_adding()
        with reactive.isolate():
            snap = {r["id"]: _wells_cell_text(r) for r in well_samples()}

        def _row(w):
            uid = w["id"]
            cells = snap.get(uid) or {"comp": "n/a", "resid": "n/a", "title": ""}
            return ui.tags.tr(
                ui.tags.td(ui.input_text(f"wl_nm_{uid}", None, value=w.get("name") or "",
                                         width="92px")),
                ui.tags.td(ui.input_numeric(f"wl_se_{uid}", None, value=w.get("screen_elev"),
                                            step=0.01, width="78px")),
                ui.tags.td(ui.input_numeric(f"wl_oh_{uid}", None, value=w.get("obs_head"),
                                            step=0.01, width="78px")),
                ui.tags.td(cells["comp"], class_="gwl-comp", title=cells["title"]),
                ui.tags.td(cells["resid"], class_="gwl-resid"),
                ui.tags.td(ui.tags.button(
                    "×", type="button", class_="hype-gpt-rm", title="Remove well",
                    onclick=("Shiny.setInputValue('wells_rm','" + uid
                             + ":'+Date.now(),{priority:'event'})"))),
                data_uid=uid)

        parts = []
        if wls:
            parts.append(ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("Well"),
                    ui.tags.th("Screen elev (m)",
                               title="Elevation of the well screen midpoint, same vertical "
                                     "datum as the terrain. Picks the model layer sampled."),
                    ui.tags.th("Observed (m)",
                               title="Hydraulic head measured in the well, when available"),
                    ui.tags.th("Computed (m)", class_="num",
                               title="Head from the groundwater model at the screen elevation"),
                    ui.tags.th("Residual (m)", class_="num", title="Computed minus observed"),
                    ui.tags.th(""))),
                ui.tags.tbody(*[_row(w) for w in wls]),
                class_="table table-sm hype-wells-table"))
        else:
            parts.append(ui.div("No wells yet. Add a well on the map, then enter its screen "
                                "elevation and any observed head.", class_="hype-instr"))
        if arm:
            parts.append(ui.div(
                ui.span("Click the map to place the well…"),
                ui.tags.button("cancel", type="button", class_="hype-link",
                               onclick="Shiny.setInputValue('wells_arm','off:'+Date.now(),"
                                       "{priority:'event'})"),
                class_="hype-instr"))
        else:
            parts.append(ui.div(ui.tags.button(
                "+ Add well on map", type="button", class_="btn btn-sm btn-outline-secondary",
                onclick="Shiny.setInputValue('wells_arm','on:'+Date.now(),{priority:'event'})"),
                class_="hype-actions"))
        return ui.TagList(*parts)

    @render.ui
    def wells_pairs():
        # STRUCTURAL renderer (obs_wells + well_pairs): pair cells are painted from an
        # isolated snapshot and patched live; renames patch through the same message.
        wls, prs = obs_wells(), well_pairs()
        if len(wls) < 2 and not prs:
            return None
        with reactive.isolate():
            snap = {p["id"]: _wells_pair_text(p) for p in well_pair_rows()}
        names = {w["id"]: (w.get("name") or w["id"]) for w in wls}
        parts = [ui.tags.h6("Head gradient between wells", class_="hype-wells-h")]
        if prs:
            def _prow(p):
                cells = snap.get(p["id"]) or {"dist": "n/a", "cg": "n/a", "og": "n/a",
                                              "title": ""}
                return ui.tags.tr(
                    ui.tags.td(ui.span(names.get(p["a"], "?"), data_wname=p["a"]), " to ",
                               ui.span(names.get(p["b"], "?"), data_wname=p["b"]),
                               title=cells["title"]),
                    ui.tags.td(cells["dist"], class_="gwp-dist"),
                    ui.tags.td(cells["cg"], class_="gwp-cg"),
                    ui.tags.td(cells["og"], class_="gwp-og"),
                    ui.tags.td(ui.tags.button(
                        "×", type="button", class_="hype-gpt-rm", title="Stop tracking",
                        onclick=("Shiny.setInputValue('wells_pair_rm','" + p["id"]
                                 + ":'+Date.now(),{priority:'event'})"))),
                    data_pid=p["id"])
            parts.append(ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("Pair"), ui.tags.th("Distance (m)", class_="num"),
                    ui.tags.th("Computed (m/m)", class_="num",
                               title="(head A minus head B) / distance, from the model"),
                    ui.tags.th("Observed (m/m)", class_="num",
                               title="Needs an observed head at both wells"),
                    ui.tags.th(""))),
                ui.tags.tbody(*[_prow(p) for p in prs]),
                class_="table table-sm hype-wells-pairs"))
        if len(wls) >= 2:
            parts.append(ui.div(
                ui.input_select("wlp_a", None, names, width="108px"),
                ui.span("to", class_="hype-wells-to"),
                ui.input_select("wlp_b", None, names, width="108px"),
                ui.tags.button("Track pair", type="button",
                               class_="btn btn-sm btn-outline-secondary",
                               onclick="Shiny.setInputValue('wells_pair_add',Date.now(),"
                                       "{priority:'event'})"),
                class_="hype-actions hype-wells-pickrow"))
        return ui.TagList(*parts)

    @render.ui
    def wells_msgs():
        # Hints + the residual summary; free to re-render (contains no inputs).
        rows = well_samples()
        if not rows:
            return None
        parts = []
        if any(r.get("reason") == "no groundwater run" for r in rows):
            parts.append(ui.div("Run the groundwater model to sample computed heads.",
                                class_="hype-instr hype-dim"))
        stats = wells_mod.residual_stats(rows)
        if stats:
            parts.append(ui.div(
                f"Residuals (n = {stats['n']}): mean error {stats['mean_error']:+.2f} m, "
                f"mean absolute error {stats['mean_abs_error']:.2f} m, "
                f"RMSE {stats['rmse']:.2f} m.", class_="hype-instr hype-dim"))
        return ui.TagList(*parts) if parts else None

    @render.ui
    def maplyr_rows():
        # STRUCTURAL renderer for the Map layers list: subscribes the record list
        # (add/remove/reorder) and _ml_paint (status transitions, relink) — NEVER
        # map_layers_ver, which bumps on every slider drag and would remount the slider
        # mid-drag. Slider/color values are painted from the record; the mirror keeps the
        # record current, so a structural repaint never jumps a control backwards.
        recs = map_layers()
        _ml_paint()
        with reactive.isolate():
            status, errs = dict(_ml_status), dict(_ml_err)
        add_btn = ui.div(_evt_btn("ml_add", "Add layers...", "btn-sm btn-outline-secondary"),
                         class_="hype-actions")
        if not recs:
            return ui.TagList(
                ui.div('No map layers yet. Click "Add layers..." to link GeoTIFF, VRT, '
                       'Shapefile, or GeoJSON files.', class_="hype-instr hype-dim"),
                add_btn)

        def _row(rec):
            # A whole-row button: clicking opens the layer's own pane (its tree row).
            # Opacity, color, locate, and remove all live there now.
            uid = rec["id"]
            st = status.get(uid)
            bits = [ui.span(rec.get("name") or "layer", class_="hype-ml-name",
                            title=rec.get("path") or ""),
                    ui.span("Raster" if rec.get("kind") == "raster" else "Vector",
                            class_="hype-ml-kind")]
            if st == "missing":
                bits.append(ui.span(class_="hype-st st-warn", title="file is missing"))
            elif st == "error":
                bits.append(ui.span(errs.get(uid) or "could not be displayed",
                                    class_="hype-ml-err",
                                    title=errs.get(uid) or ""))
            return ui.tags.button(
                *bits, type="button", class_="hype-ml-row hype-ml-openrow",
                data_uid=uid, title="Open this layer's settings",
                onclick=("Shiny.setInputValue('ml_open','" + uid
                         + ":'+Date.now(),{priority:'event'})"))

        return ui.TagList(ui.div(*[_row(r) for r in recs], class_="hype-ml-list"), add_btn)

    @reactive.effect
    async def _start_surface():
        if not _clicked_dynamic("run_surface"):
            return
        build = _domain_build()
        if not (build and dem_path()):
            ui.notification_show("Need all four boundaries (closing into a domain) plus terrain "
                                 "before running the surface model.", type="warning", duration=6)
            return
        if bnd_conflicts():
            ui.notification_show("A boundary line crosses the stream centerline. Adjust the "
                                 "boundary or the reach centerline in the Boundaries step, "
                                 "then try again.", type="error", duration=8)
            return
        if not ras_engine.ras_available():
            ui.notification_show(RAS_UNAVAILABLE_MSG, type="error", duration=8)
            return
        cell = float(_safe("ras_cell", 10.0))
        est = ras_engine.estimate_cell_count(_domain_gdf_4326(), cell)
        _green, cap = ras_engine.cell_budget()
        if est > cap:
            if not runmode.IS_DESKTOP:
                need = cell * (est / cap) ** 0.5
                ui.notification_show(f"~{est:,} cells at {cell:g} m — over the {cap:,} limit. "
                                     f"Increase the cell size to ~{need:.0f} m.",
                                     type="error", duration=10)
                return
            ui.notification_show(f"~{est:,} cells at {cell:g} m — no limit in Desktop Run, "
                                 "but a mesh this size may take a long time.", duration=8)
        slope = float(_safe("ras_slope", 0.0) or 0.0)
        if slope <= 0:
            slope = ras_slope_default() or 0.001
        payload = {
            "up": build["up"], "left": build["left"], "right": build["right"],
            "down": build["down"], "domain": build["domain"], "dem": active_dem(),
            "flow_cms": float(_safe("ras_flow", 100.0)) * CFS_TO_CMS,
            "friction_slope": slope,
            "manning_n": float(_safe("ras_n", 0.06)),
            "cell_size_m": cell,
            "duration_hr": float(_safe("ras_hours", 6.0)),
            "timestep_s": float(_safe("ras_dt", 10.0)),
            "output_interval_s": max(60.0, float(_safe("ras_out_min", 15.0)) * 60.0),
            "work_dir": str(work_dir),
        }
        ras_log_lines.clear()
        ras_log_tick.set(0)
        _ras_cancel.clear()
        _ras_prog.update(stage="Starting", pct=None, stage_t0=time.monotonic())
        ras_stage.set("Starting"); ras_pct.set(None)
        ras_t0.set(time.monotonic())
        ras_elapsed.set(0)
        _ras_mesh_payload.clear()       # _ras_done auto-meshes from this snapshot on success
        _ras_mesh_payload.update({k: payload[k] for k in (
            "up", "left", "right", "down", "domain", "dem", "cell_size_m", "work_dir")})
        await _cascade_clear("sw")      # re-running the water surface invalidates groundwater + results
        _task_armed["sw"] = True
        ras_task(payload)

    @reactive.effect
    def _ras_poll():
        if ras_task.status() != "running":
            return
        reactive.invalidate_later(0.5)
        ras_log_tick.set(len(ras_log_lines))
        ras_elapsed.set(int(time.monotonic() - ras_t0()))
        ras_stage.set(_ras_prog["stage"])
        ras_pct.set(_ras_prog["pct"])
        ras_stage_t0.set(_ras_prog["stage_t0"])

    @reactive.effect
    async def _ras_done():
        status = ras_task.status()
        if status in ("initial", "running"):
            return
        if not _task_armed["sw"]:       # already applied (or aborted by a cascade) — see _task_armed
            return
        _task_armed["sw"] = False
        if status == "cancelled":
            return
        try:
            res = ras_task.result()
        except Exception as e:  # noqa: BLE001
            res = {"error": str(e)}
        ras_log_tick.set(len(ras_log_lines))
        if "error" in res:
            if not _ras_cancel.is_set():
                ui.notification_show("Surface model failed — see the log on the Surface step.",
                                     type="error", duration=8)
                ras_log_lines.append("FAILED: " + res["error"])
                ras_log_tick.set(len(ras_log_lines))
            return
        _show_ras_overlays(res)
        ras_result.set(res)                         # extent + overlay effects draw from this
        with reactive.isolate():                    # fresh SW result → its stale badge clears
            _stale_marks.set(frozenset(_stale_marks() - {"sw"}))
        # The run's mesh is a result too: build the overlay even when Preview mesh was never
        # clicked, so the tree's "2D mesh" row has something to show. Same child path as the
        # button; skipped when a preview at this cell size already exists. Reads are isolated —
        # this effect must depend on ras_task.status() only (the done-handler re-fire lesson).
        with reactive.isolate():
            _prev_mesh = ras_mesh_prev()
            _meshing = mesh_prev_task.status() == "running"
        _cell = float(_ras_mesh_payload.get("cell_size_m", 0.0) or 0.0)
        if (_ras_mesh_payload and not _meshing
                and (_prev_mesh is None
                     or abs(float(_prev_mesh.get("cell_size_m", _cell)) - _cell) > 1e-9)):
            try:
                _mesh_auto["on"] = True
                mesh_prev_task(dict(_ras_mesh_payload))
            except Exception:  # noqa: BLE001 — the mesh is a nicety; the run result stands
                _mesh_auto["on"] = False
        origin, z0 = _scene_frame()                 # 3-D drapes for the surface results
        if origin is not None:
            try:
                await _send_3d(scene.drape_payload("depth", _ras_overlays.get("depth"),
                                                   _scene.get("crs"), origin,
                                                   lift=0.45, opacity=0.85))
                await _send_3d(scene.drape_payload("wse", _ras_overlays.get("wse"),
                                                   _scene.get("crs"), origin,
                                                   lift=0.35, opacity=0.85))
            except Exception:  # noqa: BLE001
                pass
        wse_mode_v.set("model")                     # the modeled WSE now feeds the groundwater run
        ui.notification_show("Surface model complete — the modeled water surface will be used "
                             "as the groundwater top boundary.", duration=6)

    def _show_ras_overlays(res):
        """Depth/WSE overlay payloads + fresh-result visibility for a surface result `res` —
        shared by the run-completion handler and project restore."""
        _ras_overlays.clear()
        try:
            _ras_overlays["depth"] = ras_results.result_overlay(res["depth_tif"], "depth")
            _ras_overlays["wse"] = ras_results.result_overlay(res["wse_tif"], "wse")
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Surface model done; raster render issue: {e}", duration=6)
        # Fresh result: depth shows by default; the WSE rasters start unchecked (they cover the
        # same footprint — stacking both muddies the display). The tree rows toggle them.
        for k in ("sw_depth", "sw_wse", "wse_raster"):
            _layer_shadow.pop(k, None)      # brand-new widgets replace these — drop stale parks
        _check_state["sw.depth"] = True
        _check_state["sw.wse"] = False
        _apply_check_effective("sw.depth")
        _apply_check_effective("sw.wse")

    _upsert_sig: dict = {}      # layer name -> id(feature) currently shown (change guard)

    def _upsert_image(key, ov, opacity):
        """Set/refresh an ImageOverlay, mutating the EXISTING widget's traits when possible and
        doing NOTHING when nothing changed. Remove+add churn — and even a same-value trait
        resync — makes the ipyleaflet client rebuild layers, and rebuilds racing inside a bursty
        flush is how layers got dropped (observed: mesh + extent lost at run completion / step
        change)."""
        old = _layers.get(key)
        if ov is None:
            if old is not None:
                _set_layer(key, None)
            return
        if old is not None and isinstance(old, ImageOverlay):
            try:
                if old.url is ov["url"] and abs(float(old.opacity) - opacity) < 1e-9:
                    return                       # unchanged — leave the client alone
                if old.url is not ov["url"]:
                    old.url = ov["url"]
                    old.bounds = ov["bounds"]
                old.opacity = opacity
                return
            except Exception:  # noqa: BLE001 — fall through to a clean re-add
                pass
        _set_layer(key, ImageOverlay(url=ov["url"], bounds=ov["bounds"], name=key,
                                     opacity=opacity))

    def _upsert_geojson(key, feat, style):
        """GeoJSON flavor of _upsert_image: identity-guarded, mutate .data only on real change."""
        old = _layers.get(key)
        if feat is None:
            if old is not None:
                _set_layer(key, None)
                _upsert_sig.pop(key, None)
            return
        if old is not None and isinstance(old, GeoJSON):
            if _upsert_sig.get(key) == id(feat):
                return                           # unchanged — leave the client alone
            try:
                old.data = _fc(feat)
                _upsert_sig[key] = id(feat)
                return
            except Exception:  # noqa: BLE001
                pass
        _set_layer(key, GeoJSON(data=_fc(feat), style=style, name=key))
        _upsert_sig[key] = id(feat)

    @reactive.effect
    def _wetted_filter_sync():
        # Owns wetted_filter_res: the upstream–downstream connectivity split of the modeled
        # wetted extent, plus the pool-masked WSE raster the GW run consumes (_wse_path).
        # Recomputes on RAS completion/restore and on the pane toggle — cheap geometry, no
        # RAS re-run. try/except is load-bearing: an error raised out of an EFFECT destroys
        # the whole session; any failure degrades to the unfiltered extent instead.
        res = ras_result()
        try:
            on = bool(input.wetted_filter())
        except Exception:  # noqa: BLE001 — pane not rendered yet; the kept mirror rules
            on = bool(_safe("wetted_filter", True))
        try:
            depth_p = (res or {}).get("depth_tif")
            src_wse = (res or {}).get("wse_for_gw")
            with reactive.isolate():           # caps can't change without cascading sw away
                build = _domain_build()
            if not (on and depth_p and Path(depth_p).exists() and src_wse and build):
                wetted_filter_res.set(None)
                return
            split = ras_results.split_wetted_by_connectivity(
                depth_p, build["up"], build["down"])
            if split is None:                  # nothing spans both caps — filter can't apply
                wetted_filter_res.set({"failed": True})
                return
            split["wse_path"] = (
                ras_results.mask_out_polygons(src_wse, split["removed_feat"],
                                              str(work_dir / "inputs" / "wse_ras_gw.tif"))
                if split.get("removed_feat") is not None else None)
            wetted_filter_res.set(split)
        except Exception as e:  # noqa: BLE001
            print(f"[wetted filter] failed: {e}")
            wetted_filter_res.set(None)

    @reactive.effect
    def _ras_extent_sync():
        # Owns the "Modeled extent" polygon (persists while a result exists — it's the water
        # surface the groundwater run consumes) and its "Removed pools" twin (the isolated
        # parts the upstream–downstream filter excluded from the GW extent). Pure upsert:
        # transitions no longer churn the layer list, so the old force-fresh-per-step
        # workaround is gone.
        if not _HAS_MAP:
            return
        ext = (ras_result() or {}).get("extent_feat")
        filt = wetted_filter_res()
        removed = None
        if filt and filt.get("kept_feat") is not None:
            ext = filt["kept_feat"]
            removed = filt.get("removed_feat")
        try:
            show_rm = bool(input.show_removed_pools())
        except Exception:  # noqa: BLE001 — pane not rendered yet
            show_rm = bool(_safe("show_removed_pools", False))
        _upsert_geojson("Modeled extent", ext, WSE_STYLE)
        _upsert_geojson("Removed pools", removed if show_rm else None, REMOVED_STYLE)

    @reactive.effect
    def _ras_result_overlays():
        # Owns the surface-result rasters as their OWN tree-toggleable layers (sw.depth /
        # sw.wse rows) — visibility belongs to the tree checkboxes via the _set_layer funnel;
        # this effect only keeps content + opacity current.
        if not _HAS_MAP:
            return
        res = ras_result()
        opacity = float(ras_opacity_v())
        _upsert_image("sw_depth", _ras_overlays.get("depth") if res else None, opacity)
        _upsert_image("sw_wse", _ras_overlays.get("wse") if res else None, opacity)

    @reactive.effect
    def _ras_mesh_sync():
        # Owns the "RAS mesh" overlay. Rasterized PNG, not vector — thousands of face edges
        # as SVG paths make Leaflet unusably slow. Checkbox-driven via the "2D mesh" tree
        # row (the _hidden_keys park machinery), shown on ANY step once a preview exists —
        # a restored project lands on its saved step, so a Surface-step-only gate would hide
        # the rebuilt mesh. Also re-asserts after a run completes (ras_result read) — the
        # completion flush is exactly when the client historically lost this layer.
        if not _HAS_MAP:
            return
        prev = ras_mesh_prev()
        ras_result()                               # re-run on run completion (see docstring)
        ov = (prev or {}).get("overlay")
        show = prev and not prev.get("too_big") and ov
        try:
            _upsert_image("RAS mesh", ov if show else None, 0.9)
        except Exception as e:  # noqa: BLE001 — a bad overlay degrades to "no overlay",
            ras_mesh_prev.set(None)                # never a dead session
            ui.notification_show(f"Couldn't draw the mesh overlay: {e}",
                                 type="error", duration=8)

    @reactive.effect
    def _cancel_surface():
        if not _clicked_dynamic("cancel_surface"):
            return
        _kill_ras_proc()
        ras_log_lines.append("[surface model cancelled by user]")
        ras_log_tick.set(len(ras_log_lines))
        try:
            ras_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        ui.notification_show("Surface model cancelled.", type="warning", duration=4)

    @reactive.effect
    def _start_mesh_preview():
        if not _clicked_dynamic("ras_mesh_btn"):
            return
        build = _domain_build()
        if not (build and dem_path()):
            ui.notification_show("Need all four boundaries (closing into a domain) plus terrain "
                                 "before meshing.", type="warning", duration=6)
            return
        if bnd_conflicts():
            ui.notification_show("A boundary line crosses the stream centerline. Adjust the "
                                 "boundary or the reach centerline in the Boundaries step, "
                                 "then try again.", type="error", duration=8)
            return
        if not ras_engine.ras_available():
            ui.notification_show(RAS_UNAVAILABLE_MSG, type="error", duration=8)
            return
        try:
            _mesh_auto["on"] = False                # button build — full notifications
            mesh_prev_task({
                "up": build["up"], "left": build["left"], "right": build["right"],
                "down": build["down"], "domain": build["domain"], "dem": active_dem(),
                "cell_size_m": float(_safe("ras_cell", 10.0)), "work_dir": str(work_dir),
            })
        except Exception as e:  # noqa: BLE001 — a failed launch must notify, never kill the session
            ui.notification_show(f"Couldn't start meshing: {e}", type="error", duration=8)

    @reactive.effect
    def _mesh_preview_done():
        status = mesh_prev_task.status()
        if status in ("initial", "running", "cancelled"):
            return
        try:
            res = mesh_prev_task.result()
        except Exception as e:  # noqa: BLE001
            res = {"error": str(e)}
        if not isinstance(res, dict):               # belt-and-braces: never let a malformed
            res = {"error": f"meshing returned {res!r}"}   # result kill the session
        auto = _mesh_auto["on"]                     # run-triggered build: store, but no toasts
        _mesh_auto["on"] = False
        ras_log_tick.set(len(ras_log_lines))
        if "error" in res:
            if auto:                                # the run already succeeded — log only
                ras_log_lines.append("[auto mesh] failed: " + str(res["error"])[:300])
                ras_log_tick.set(len(ras_log_lines))
            else:
                ui.notification_show("Meshing failed: " + str(res["error"])[:300],
                                     type="error", duration=8)
            return
        ras_mesh_prev.set(res)                      # _ras_mesh_sync draws it
        if auto:
            return
        if res.get("too_big"):
            ui.notification_show(f"Mesh built: {res.get('cell_count', 0):,} cells — too many "
                                 f"faces ({res.get('n_faces', 0):,}) to draw as an overlay; "
                                 "the run itself is unaffected.", type="warning", duration=8)
        else:
            ui.notification_show(f"Mesh built: {res.get('cell_count', 0):,} cells at "
                                 f"{res.get('cell_size_m', 0):g} m.", duration=5)

    @reactive.effect
    def _mirror_ras_opacity():
        try:
            v = input.ras_opacity()
        except Exception:  # noqa: BLE001
            return
        if v is not None:
            ras_opacity_v.set(float(v))

    @reactive.effect
    def _mesh_preview_stale_on_cell():
        # A mesh preview is only meaningful for the cell size it was built at.
        try:
            cell = float(input.ras_cell())
        except Exception:  # noqa: BLE001
            return
        prev = ras_mesh_prev()
        if prev and abs(float(prev.get("cell_size_m", cell)) - cell) > 1e-9:
            ras_mesh_prev.set(None)

    _ras_inputs_sig: dict = {}

    def _drop_ras_artifacts():
        """Clear every surface-model product (result, overlays, mesh preview + their layers)."""
        ras_result.set(None)
        ras_mesh_prev.set(None)
        wetted_filter_res.set(None)
        _ras_overlays.clear()
        for nm in ("Modeled extent", "Removed pools", "sw_depth", "sw_wse", "RAS mesh"):
            _set_layer(nm, None)

    def _drop_gw_artifacts():
        """Clear every groundwater product (grid preview, run, head results + their 2-D layers).
        The missing counterpart to _drop_ras_artifacts / _clear_hz_outputs."""
        mesh_geom.set(None)
        run_result.set(None)
        head_tifs.set([])
        head_rng.set(None)
        _head_cache.clear()
        _contour_cache.clear()
        for nm in ("head", "grid", "wse_raster"):
            _set_layer(nm, None)

    def _sweep_gms_dir():
        """Invalidation beats builder: drop the exported GMS tree AND any in-flight or
        pending build that would resurrect it (the epoch bump makes _gms_done undo a
        build that slips through between the kill and the sweep)."""
        _gms_epoch["n"] += 1
        _gms_pending.clear()
        p = _gms_proc.get("p")
        if p is not None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001 — already gone is fine
                pass
        try:
            gms_task.cancel()
        except Exception:  # noqa: BLE001 — nothing running is fine
            pass
        shutil.rmtree(work_dir / "GMS", ignore_errors=True)
        gms_status_v.set(None)

    def _drop_bnd_artifacts():
        """Clear the generated boundary geometry (the four lines + drawn wetted extent). The domain
        calc goes None automatically and the boundary map layers drop via their owner effects."""
        for sv in (up_feat, left_feat, right_feat, down_feat, wse_extent_feat):
            sv.set(None)
        origin_override.set(None)      # streambed origin derives from the (now-gone) upstream line

    def _abort_stage_tasks(todo):
        """Invalidation beats builder (cf. _sweep_gms_dir): disarm each cleared stage's
        done-handler, kill its child process, cancel the task. The DISARM is the real guard —
        cancel() no-ops on a just-completed task whose status-flip flush is still queued, and
        only a disarmed handler stops that result landing on the cleared state."""
        aborted = []
        if "sw" in todo and ras_task.status() == "running":
            aborted.append("surface")
        if "gw" in todo and run_task.status() == "running":
            aborted.append("groundwater")
        if "hz" in todo and hz_task.status() == "running":
            aborted.append("hyporheic")
        if "sw" in todo:
            _task_armed["sw"] = False
            _kill_ras_proc()                   # sets _ras_cancel: the error path stays silent
            for t in (ras_task, mesh_prev_task):
                try:
                    t.cancel()
                except Exception:  # noqa: BLE001
                    pass
            _mesh_auto["on"] = False
            _ras_mesh_payload.clear()
        if "gw" in todo:
            _task_armed.update(gw=False)
            _terminate_child()
            for t in (run_task, mesh_task):
                try:
                    t.cancel()
                except Exception:  # noqa: BLE001
                    pass
            _grid_auto["on"] = False
            p = _mesh_proc.get("proc")
            if p is not None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
            stage.set("")                      # a disarmed _run_done bails before its stage.set
        if "hz" in todo:
            _task_armed["hz"] = False
            p = _hz_proc.get("p")
            if p is not None:
                try:
                    if p.is_alive():
                        p.terminate()
                except Exception:  # noqa: BLE001
                    pass
            try:
                hz_task.cancel()
            except Exception:  # noqa: BLE001
                pass
            # Alternatives ride the hz slice, NOT gw: every cascade slice that contains gw
            # also contains hz, while _cascade_clear("gw") (a plain GW re-run) produces
            # todo={"hz"} only — the old sens kill sat in the gw branch and a re-run never
            # reached it. The sweep also resets the displayed run to the Basecase.
            _sweep_alt_dir()
        if aborted:
            print(f"[cascade] aborted in-flight {', '.join(aborted)} work — its stage was "
                  f"cleared", flush=True)

    async def _clear_3d(*keys):
        """Remove named 3-D layers (grid mesh / drapes) WITHOUT a full-scene reset: an empty lines3d
        payload routes to removeLayer3d(key), which is type-agnostic (mesh3d.js). Keeps terrain +
        basemap (unlike hype3d_clear, which _reset uses)."""
        for k in keys:
            await session.send_custom_message(
                "hype3d_layer", {"key": k, "kind": "lines3d", "data": {"polylines": []}})

    async def _cascade_clear(stage, *, include_self=False):
        """Clear a pipeline stage's outputs and everything downstream. Order: bnd -> sw -> gw -> hz
        (hz = the head + hyporheic-zone results subtree). include_self=False clears STRICTLY
        downstream — used when the stage is being re-run and replaces its own output."""
        order = ["bnd", "sw", "gw", "hz"]
        todo = set(order[order.index(stage) + (0 if include_self else 1):])
        _abort_stage_tasks(todo)       # in-flight work on a cleared stage must never land
        if "bnd" in todo:
            _drop_bnd_artifacts()
        if "sw" in todo:
            _drop_ras_artifacts()
            await _clear_3d("depth", "wse")
        if "gw" in todo:
            _drop_gw_artifacts()
            await _clear_3d("gw_mesh", "head", "wse")
        if "hz" in todo:
            await _clear_hz_outputs()
        if {"gw", "hz"} & todo:            # the exported GMS project mirrors gw+hz results
            _sweep_gms_dir()

    @reactive.effect
    async def _ras_stale_on_edit():
        # Any boundary change (regenerate OR a manual vertex edit — both replace the feature objects)
        # makes every downstream product stale, so cascade-clear surface + groundwater + hyporheic;
        # nothing computed on the old domain should linger. Signature by feature identity (features
        # are replaced, never mutated). The boundaries themselves persist (they were re-made).
        sig = tuple(id(f) for f in (up_feat(), left_feat(), right_feat(), down_feat()))
        prev = _ras_inputs_sig.get("sig")
        _ras_inputs_sig["sig"] = sig
        if prev is None or sig == prev:
            return
        with reactive.isolate():
            had_downstream = any(v is not None for v in (
                ras_result(), ras_mesh_prev(), mesh_geom(), run_result(), hz_result()))
        if not had_downstream:
            return
        # The WSE mode deliberately stays "model" (model-first): the groundwater Run stays blocked
        # with a clear message until the surface model is re-run on the new boundaries.
        await _cascade_clear("bnd")
        ui.notification_show("Boundaries changed — surface, groundwater and hyporheic results were "
                             "discarded; re-run from Water surface.", type="warning", duration=6)

    # ---- run (in a spawned child process so a Cancel can hard-kill MODFLOW) ----
    @reactive.extended_task
    async def run_task(payload: dict) -> dict:
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=runner.child_run, args=(payload, q), daemon=True)
            _proc["p"] = p
            p.start()
            result = error = None

            def _consume(item):
                nonlocal result, error
                kind, data = item
                if kind == "log":
                    log_lines.append(data)
                elif kind == "result":
                    result = data
                elif kind == "error":
                    error = data

            while True:
                try:
                    _consume(q.get(timeout=0.3))
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:                       # drain whatever was queued right before exit
                try:
                    _consume(q.get_nowait())
                except _queue.Empty:
                    break
            p.join(timeout=5)
            _proc["p"] = None
            if error is not None:
                raise RuntimeError(error)
            if result is None:
                raise RuntimeError("Run produced no result (it may have been cancelled).")
            return result
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    async def _start_run():
        if not _clicked_dynamic("run_model"):
            return
        build = _domain_build()                 # assembled domain + left/right oriented upstream→downstream
        if not (build and dem_path()):
            ui.notification_show("Need all four boundaries (Upstream/Left/Right/Downstream) that close "
                                 "into a domain, plus terrain.", type="warning", duration=6)
            return
        est = grid_estimate()
        if not runmode.IS_DESKTOP and est and estimate.band(est["n_cells"]) == "red":
            ui.notification_show(estimate.band_message(est), type="error", duration=10)
            return
        wse = _wse_path()
        if wse is None:
            ui.notification_show("No water surface yet — run the water-surface model first "
                                 "(Water surface step).", type="warning", duration=6)
            return
        try:
            gradients_cfg = _gradient_config()   # qualitative or gradient-points (never None)
        except ValueError as ge:
            ui.notification_show(f"Fix the boundary gradients first: {ge}",
                                 type="warning", duration=8)
            return
        if not runner.modflow_available():
            ui.notification_show(MODFLOW_UNAVAILABLE_MSG, type="error", duration=8)
            return
        _wse_used["path"] = wse             # for the Results "Water surface (raster)" overlay
        try:
            crs = proj_crs()
            crs_id = crs.to_epsg() or crs.to_wkt()      # picklable for the child process
            use_kz = bool(_safe("use_kzones", False))
            payload = {
                "crs": crs_id, "domain": build["domain"], "left": build["left"],
                "right": build["right"], "dem": active_dem(), "params": params(),
                "work_dir": str(work_dir),
                "wse_mode": "dem",          # fallback only; wse_path (below) always wins
                "wse_path": wse,
                "wse_relief_thresh": float(_safe("wse_relief", 0.2)),
                "kzones": (kzone_feats() if use_kz else []),
                "kzone_kh": float(_safe("kzone_kh", 50.0)),
                "kzone_kv": float(_safe("kzone_kv", 5.0)),
            }
            # NRCS-derived K (§6): only when fetched AND explicitly enabled with a confirmed
            # aggregation policy. Reduced to a picklable per-mukey payload here; the child
            # builds the per-cell arrays after the grid exists.
            if bool(_safe("use_soil_k", False)) and soil_snapshot():
                from hype_app.soil_k import prepare_soil_k_payload
                kv0 = float(_safe("kv", 1.0)) or 1.0
                payload["soil_k"] = prepare_soil_k_payload(
                    soil_snapshot(), policy=str(_safe("soil_policy", "dominant")),
                    anisotropy_ratio=float(_safe("kh", 10.0)) / kv0,
                    fallback_kh=float(_safe("kh", 10.0)), fallback_kv=kv0)
                if payload["soil_k"] is None:
                    ui.notification_show("NRCS soils have no usable conductivity data — "
                                         "running with uniform K.", type="warning", duration=6)
            # Freeze the run inputs (§4.2): from here on the report/download read this snapshot,
            # not live UI. Guarded so a snapshot problem can never abort the actual run.
            try:
                import uuid
                epsg = crs.to_epsg()
                ras_cfs = _safe("ras_flow", None)
                fs = flow_source()
                if fs:
                    src = fs.get("source", "USGS StreamStats")
                    edited = (ras_cfs is not None
                              and abs(float(ras_cfs) - float(fs.get("inserted_cfs", ras_cfs))) > 1e-6)
                    flow_id, user_mod = fs.get("candidate_id"), edited
                else:
                    src, flow_id, user_mod = "manual", None, True
                snap = snapshot.build_input_snapshot(
                    assessment_id=uuid.uuid4().hex[:12], params=payload["params"],
                    streamflow_cfs=(None if ras_cfs is None else float(ras_cfs)),
                    streamflow_source=src, streamflow_user_modified=user_mod,
                    flow_lookup_id=flow_id,
                    reach_geojson=reach_feat(), domain_geojson=domain_feat(),
                    boundary_geojson={"upstream": up_feat(), "left": left_feat(),
                                      "right": right_feat(), "downstream": down_feat()},
                    site=_site_metadata(),
                    terrain=snapshot.TerrainSource(
                        wse_mode=wse_mode_v(), crs_epsg=(int(epsg) if epsg else None),
                        dem_source=((dem_meta() or {}).get("source") or ("3DEP" if active_dem() else None)),
                        dem_resolution_m=(dem_meta() or {}).get("resolution_m"),
                        model_origin_elev=payload["params"].get("model_origin_elev")),
                    use_kzones=use_kz, kzone_count=len(payload["kzones"]),
                    kzone_kh=payload["kzone_kh"], kzone_kv=payload["kzone_kv"],
                    gradients_config=gradients_cfg,
                    soil_snapshot_id=("nrcs" if payload.get("soil_k") else None),
                    soil_aggregation_policy=(str(_safe("soil_policy", "dominant"))
                                             if payload.get("soil_k") else None),
                    anisotropy_ratio=(payload["soil_k"]["anisotropy_ratio"]
                                      if payload.get("soil_k") else None),
                    app_version=APP_VERSION)
                snap_dict = snap.model_dump(mode="json")
                input_snapshot.set(snap_dict)
                payload["input_snapshot"] = snap_dict
            except Exception as se:  # noqa: BLE001
                print(f"[snapshot] could not freeze input snapshot: {type(se).__name__}: {se}")
                input_snapshot.set(None)
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Could not start the run: {type(e).__name__}: {e}",
                                 type="error", duration=8)
            return
        log_lines.clear()
        log_tick.set(0)
        step_v.set(0)
        run_t0.set(time.monotonic())
        elapsed_v.set(0)
        stage.set("Running MODFLOW 6 + MODPATH 7…")
        _select("gw.run")               # the run row appears in the tree; its pane shows progress
        # An alternative must never stay on screen while its files are swept by the cascade.
        await _set_displayed_run(None, quiet=True)
        await _cascade_clear("gw")      # re-running groundwater invalidates the hyporheic results
        _task_armed["gw"] = True
        run_task(payload)

    @reactive.effect
    def _run_poll():
        if run_task.status() != "running":
            return
        reactive.invalidate_later(0.4)
        log_tick.set(len(log_lines))
        elapsed_v.set(int(time.monotonic() - run_t0()))
        for line in reversed(log_lines[-80:]):       # newest STEP marker wins
            m = re.search(r"STEP\s+(\d+)", line)
            if m:
                step_v.set(int(m.group(1)))
                break

    def _show_run_layers(res, *, base_dir=None):
        """Head layers + model-grid + consumed-WSE raster from the on-disk run artifacts —
        shared by the run-completion handler, project restore, and the displayed-run switch
        (base_dir points at an alternative's folder). The WSE raster block deliberately keeps
        reading work_dir: the consumed water surface is an input, identical across scenarios,
        and pruned alternative dirs do not carry it. Returns (tifs, wov) for the caller's
        3-D drapes."""
        base = Path(base_dir) if base_dir else work_dir
        tifs = results.head_rasters(base, res)       # per-layer head color map + grid
        _head_cache.clear(); _contour_cache.clear()
        head_tifs.set(tifs)
        if tifs:
            head_rng.set(results.head_value_range(tifs))
            grid = results.grid_overlay(tifs)            # active cells only (≈ idomain)
            _set_layer("grid", ImageOverlay(url=grid["url"], bounds=grid["bounds"],
                                            name="Model grid", opacity=0.7) if grid else None)
            # default to the first layer whose head covers the WHOLE active footprint —
            # upper layers are clipped by the above-ground idomain deactivation, so their
            # contours stop mid-domain
            _hl = results.full_coverage_layer(tifs)
            head_layer_v.set(_hl)
            _render_head_layer(_hl)
        # Water-surface raster the model consumed, as its own toggleable layer. Prefer the
        # engine's domain-cropped copy; fall back to the raster resolved at launch.
        wse_tif = work_dir / "model" / "cropped_water_surface_raster.tif"
        wse_src = str(wse_tif) if wse_tif.exists() else _wse_used.get("path")
        wov = None
        if wse_src and Path(wse_src).exists():
            wrng = results.head_value_range([wse_src])
            wov = results.raster_overlay(wse_src, vmin=wrng[0], vmax=wrng[1])
            _set_layer("wse_raster", ImageOverlay(url=wov["url"], bounds=wov["bounds"],
                                                  name="Water surface (raster)",
                                                  opacity=0.85))
        else:
            _set_layer("wse_raster", None)
        _schedule_relayer(("head", "grid", "wse_raster", "dem"), 3.0)
        return tifs, wov

    @reactive.effect
    async def _run_done():
        status = run_task.status()
        if status in ("initial", "running"):
            return
        if not _task_armed["gw"]:       # already applied (or session reset) — see _task_armed
            return
        _task_armed["gw"] = False
        stage.set("")
        if status == "cancelled":
            return  # the Cancel handler already reset the UI
        if status == "error":
            msg = "Model run failed."
            try:
                run_task.result()
            except Exception as e:  # noqa: BLE001
                detail = str(e)
                if "No particles" in detail:
                    msg = ("No hyporheic pathlines were produced (all particles exited at the "
                           "boundaries). Try a stronger floodplain gradient, a different "
                           "water-surface option, or a larger domain.")
                else:
                    msg = f"Model run failed: {detail}"
            log_lines.append(msg); log_tick.set(len(log_lines))
            ui.notification_show(msg, type="error", duration=12)
            return                      # stay on the Model-run node: error icon + log in view
        try:
            res = run_task.result()
        except Exception:
            ui.notification_show("Model run failed.", type="error", duration=8)
            return
        run_result.set(res)
        with reactive.isolate():                       # fresh GW result → its stale badge clears
            _stale_marks.set(frozenset(_stale_marks() - {"gw"}))
        _reset_res_layer_vis()                         # fresh layers → all visible, shadows dropped
        if _HAS_MAP:
            try:
                # Flow paths (and their entry/return dots) are produced ONLY by delineation now
                # (Results ▸ Zone ▸ Flow paths). A plain GW run maps head/grid/WSE — no pathlines.
                # No auto-fit on run completion — only the props-pane Zoom-to-extent button moves the view.
                tifs, wov = _show_run_layers(res)
                # 3-D scene: head/WSE drapes only (same frame as terrain/mesh); paths come from HZ
                origin, z0 = _scene_frame()
                if origin is not None:
                    crs_s = _scene.get("crs")
                    try:
                        if tifs and _head_cache.get(0):
                            await _send_3d(scene.drape_payload(
                                "head", _head_cache.get(0), crs_s, origin,
                                lift=0.6, opacity=0.8))
                        if wov is not None:
                            await _send_3d(scene.drape_payload(
                                "wse", wov, crs_s, origin, lift=0.35, opacity=0.85))
                    except Exception:  # noqa: BLE001
                        pass
                    # 3-D "Model grid" → the RUN's real DIS. The pre-run preview derives its
                    # flat bed from min(DEM) over a different raster crop and can sit metres
                    # ABOVE the run's bed — zone volumes (built from the run) then hang below
                    # the preview blocks (the "volumes below the grid" bug).
                    try:
                        gwf_ws = work_dir / "model" / "gwf_workspace"
                        if next(gwf_ws.glob("*.dis.grb"), None) is not None:
                            build = _domain_build()
                            _grid_auto["on"] = True     # quiet build — stay in the current view
                            mesh_task({
                                "run_ws": str(gwf_ws), "crs": proj_crs().to_wkt(),
                                "sides": ({k: build[k] for k in ("up", "left", "right", "down")}
                                          if build else None),
                                "scene_z0": z0,
                            })
                    except Exception as e:  # noqa: BLE001 — the grid is a nicety; the run stands
                        _grid_auto["on"] = False
                        log_lines.append("[auto grid] skipped: " + str(e)[:300])
                        log_tick.set(len(log_lines))
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"Results computed; map render issue: {e}", duration=6)
        await _clear_hz_outputs()      # a new GW run rewrites the workspaces HZ reads
        _request_gms_build(include_hz=False)   # refresh the GMS folder (no particle sets yet)
        _select("gw.res")
        ui.notification_show("Run complete.", duration=4)

    # ---- hyporheic-zone delineation task family (post-run; spawned child) ----
    _ALL_HZ_KEYS = tuple([f"hz_paths_{c}" for c in HZ_CLASSES]
                         + [f"hz_nodes_{c}_start" for c in HZ_CLASSES]
                         + [f"hz_nodes_{c}_end" for c in HZ_CLASSES]
                         + [f"hz_foot_{c}" for c in HZ_CLASSES]
                         + ["hz_flow_down", "hz_flow_up"]      # streambed exchange map
                         + ["hz_paths_sel"])
    # the tree-checkbox-driven subset: hz_paths_sel is owned by _hz_selection_layer and has no
    # tree node — parking it (e.g. _hz_done's creation park) would hide selections forever
    _HZ_TREE_KEYS = tuple(k for k in _ALL_HZ_KEYS if k != "hz_paths_sel")

    async def _clear_hz_outputs():
        """Drop every hyporheic-zone artifact: map layers (classed paths, entry/return dots,
        footprints), 3-D lines+volumes, selection, result. Called when a new GW run (or New run)
        invalidates the prior HZ analysis — flow paths exist only after delineation."""
        hz_result.set(None)
        hz_sel_pids.set(())
        hz_gdf.set(None)
        results_model.set(None)        # the site report is built FROM the HZ results, so it
        report_paths.set(None)         # goes with them (auto-open re-arms for the next run)
        _report_shown_for.set(None)
        for cls in HZ_CLASSES:
            _set_layer(f"hz_paths_{cls}", None)
            _set_layer(f"hz_foot_{cls}", None)
            _set_layer(f"hz_nodes_{cls}_start", None)
            _set_layer(f"hz_nodes_{cls}_end", None)
        _set_layer("hz_flow_down", None)
        _set_layer("hz_flow_up", None)
        _set_layer("hz_paths_sel", None)
        await _sweep_hz(_ALL_HZ_KEYS)       # heal any client copies the removals missed
        for key in ("hz3d_paths_", "hz3d_vol_"):
            for cls in HZ_CLASSES:
                try:
                    await session.send_custom_message(
                        "hype3d_layer", {"key": key + cls, "kind": "lines3d",
                                         "data": {"polylines": [], "color": HZ_COLORS[cls],
                                                  "origin": None}})
                except Exception:  # noqa: BLE001
                    pass

    @reactive.extended_task
    async def hz_task(payload: dict) -> dict:
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=hz_run.child_run, args=(payload, q), daemon=True)
            _hz_proc["p"] = p
            p.start()
            result = error = None
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "log":
                        hz_log_lines.append(data)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:
                try:
                    kind, data = q.get_nowait()
                    if kind == "log":
                        hz_log_lines.append(data)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            _hz_proc["p"] = None
            if error is not None:
                raise RuntimeError(error)
            if result is None:
                raise RuntimeError("Analysis produced no result (it may have been cancelled).")
            return result
        return await anyio.to_thread.run_sync(_work)

    def _hz_particle_estimate(ppc: int) -> int:
        """Rough seed count: active cells × ppc, active cells ≈ the run's total cell count
        scaled by the domain's share of its bounding box."""
        res = run_result() or {}
        grid = res.get("grid") or {}
        n_cells = int(grid.get("n_cells_total") or 0)
        if not n_cells:
            return 0
        frac = 1.0
        try:
            g = _domain_gdf_4326()
            if g is not None:
                geom = g.geometry.iloc[0]
                minx, miny, maxx, maxy = geom.bounds
                bbox_area = (maxx - minx) * (maxy - miny)
                if bbox_area > 0:
                    frac = min(1.0, float(geom.area) / bbox_area)
        except Exception:  # noqa: BLE001
            pass
        return int(n_cells * frac * ppc)

    @reactive.effect
    async def _start_hz():
        if not _clicked_dynamic("run_hz"):
            return
        if run_result() is None:
            ui.notification_show("Run the groundwater model first.", type="warning", duration=6)
            return
        if "gw" in _stale_marks():
            ui.notification_show("The groundwater results are stale (terrain changed) — "
                                 "re-run the model before delineating.", type="warning", duration=7)
            return
        build = _domain_build()
        if build is None:
            ui.notification_show("The four boundaries are needed to classify the zone faces.",
                                 type="warning", duration=6)
            return
        ppc = int(_safe("hz_ppc", 1))
        est = _hz_particle_estimate(ppc)
        if not runmode.IS_DESKTOP and est > HZ_MAX_PARTICLES:
            ui.notification_show(
                f"~{est:,} particles at {ppc}/cell is over the {HZ_MAX_PARTICLES:,} limit — "
                f"use fewer particles per cell.", type="error", duration=10)
            return
        if est > 500_000:
            ui.notification_show(f"~{est:,} particles — this may take several minutes.",
                                 duration=7)
        if not runner.modflow_available():
            ui.notification_show(MODFLOW_UNAVAILABLE_MSG, type="error", duration=8)
            return
        try:
            crs = proj_crs()
            crs_id = crs.to_epsg() or crs.to_wkt()
            payload = {
                "work_dir": str(work_dir), "crs": crs_id,
                "left": build["left"], "right": build["right"],
                "up": build["up"], "down": build["down"],
                "params": {
                    "particles_per_cell": ppc,
                    "sample_per_class": int(_safe("hz_sample", 500)),
                    "iface_particles_per_cell": int(_safe("hz_iface_ppc", 4)),
                    "porosity": float(_safe("porosity", 0.3)),
                    "modflow_bin_dir": runner.modflow_bin_dir(),
                    "hard_cap_particles": (10**9 if runmode.IS_DESKTOP else HZ_MAX_PARTICLES),
                },
            }
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Could not start the analysis: {type(e).__name__}: {e}",
                                 type="error", duration=8)
            return
        # Re-delineation replaces the outputs every alternative was compared against, and
        # _start_hz cascades nothing — return the display to the Basecase and wipe the batch.
        await _set_displayed_run(None, quiet=True)
        _sweep_alt_dir()
        hz_log_lines.clear()
        hz_log_tick.set(0)
        hz_step_v.set(0)
        hz_t0.set(time.monotonic())
        hz_elapsed.set(0)
        _select("gw.res.hz")
        _task_armed["hz"] = True
        hz_task(payload)

    @reactive.effect
    def _hz_poll():
        if hz_task.status() != "running":
            return
        reactive.invalidate_later(0.4)
        hz_log_tick.set(len(hz_log_lines))
        hz_elapsed.set(int(time.monotonic() - hz_t0()))
        for line in reversed(hz_log_lines[-40:]):     # newest HZ STEP marker wins
            m = re.search(r"HZ STEP\s+(\d+)", line)
            if m:
                hz_step_v.set(int(m.group(1)))
                break

    def _hz_node_fc(sub, cls):
        """(start_fc, end_fc) point FeatureCollections from a classed-path gdf subset — the
        entry (blue) / return (red) dots, tagged particleid + hz_class so a dot click selects
        its path and class (via _on_hz_path_click). Vertices are time-ordered, so coords[0] /
        coords[-1] are each path's entry / return points."""
        starts, ends = [], []
        for pid, geom in zip(sub["particleid"], sub.geometry):
            if geom is None or geom.is_empty:
                continue
            parts = list(geom.geoms) if geom.geom_type == "MultiLineString" else [geom]
            try:
                c0 = parts[0].coords[0]; c1 = parts[-1].coords[-1]
            except Exception:  # noqa: BLE001
                continue
            # separate dicts per feature — _tag_hz stamps the layer key into properties, and a
            # shared dict would give the start AND end dots the same tag (sweep collisions)
            starts.append({"type": "Feature",
                           "properties": {"particleid": int(pid), "hz_class": cls},
                           "geometry": {"type": "Point",
                                        "coordinates": [float(c0[0]), float(c0[1])]}})
            ends.append({"type": "Feature",
                         "properties": {"particleid": int(pid), "hz_class": cls},
                         "geometry": {"type": "Point",
                                      "coordinates": [float(c1[0]), float(c1[1])]}})
        fc = lambda f: ({"type": "FeatureCollection", "features": f} if f else None)  # noqa: E731
        return fc(starts), fc(ends)

    async def _show_hz_layers(hz_dir, *, reset_checks=True):
        """Classed flow paths, entry/return dots, and footprints re-read from `hz_dir` — the
        map half of delineation completion, shared with project restore and the displayed-run
        switch (reset_checks=False preserves the user's class checkboxes across a switch).
        Parks everything at creation and schedules the reveal (choreography notes inline).
        Returns the classed 4326 gdf for the caller's 3-D payloads."""
        combined_4326 = None
        # the classed paths replace the monolithic forward set (2-D + 3-D)
        _set_layer("paths", None)
        _set_layer("paths_sel", None)
        # visibility FIRST — classed paths on; only the hyporheic volume on by default (four
        # overlapping translucent shells would be unreadable). Fresh results also re-arm the
        # GROUP checks — an earlier group-uncheck must not leave the new delineation invisible
        # while the tree shows its leaves ticked.
        # Results defaults (revision §8.1): fresh delineation shows ONLY the hyporheic paths
        # + volume; losing/gaining/throughflow are opt-in via the tree.
        if reset_checks:
            for cls in HZ_CLASSES:
                suf = ui_tree.HZ_CLASS_SUFFIX[cls]
                _check_state[f"gw.res.paths.{suf}"] = (cls == "hyporheic")
                _check_state[f"gw.res.hz.{suf}"] = (cls == "hyporheic")
            for gid in ("gw.res.hz", "gw.res.paths", "gw.res.hz.vols", "gw.res.hz.flows"):
                _check_state[gid] = True           # flows = the streambed exchange map
        _apply_check_effective("gw.res.hz")     # one cascade covers the whole Zone subtree
        # ... then park EVERYTHING at creation, even the default-visible keys. NO hz original
        # ever rides the run-completion burst: the client is busy building the 3-D scene right
        # then, and a burst-added original that the post-burst verify falsely reports missing
        # gets remove+re-added by the heal — a removal that races the in-flight view creation
        # and strands the original's leaflet layer as an ORPHAN no checkbox can ever remove
        # (diagnosed live 2026-07-09: all 4 path classes + the hyporheic footprint stuck
        # visible; the born-parked volumes were immune). _hz_reveal flips the checked keys
        # live as fresh clones once the burst settles — the same calm clone-add path the
        # default-hidden volumes have always used.
        _hidden_keys.update(_HZ_TREE_KEYS)
        if _HAS_MAP:
            try:
                await _send_3d({"key": "paths", "kind": "lines3d",
                                "data": {"polylines": [], "color": "#08306b", "origin": None}})
            except Exception:  # noqa: BLE001
                pass
            combined_4326 = None
            try:
                combined = hz_results.class_paths_gdf(hz_dir)   # model CRS, 3-D + hz_class
                combined_4326 = combined.to_crs(4326) if combined is not None else None
            except Exception:  # noqa: BLE001
                combined_4326 = None
            hz_gdf.set(combined_4326)
            # sweep BEFORE re-adding: stale client copies of the previous generation would
            # otherwise survive as ghost paths no checkbox can clear (dropped-remove flakiness)
            await _sweep_hz(_ALL_HZ_KEYS)
            for cls in HZ_CLASSES:
                gj = _tag_hz(hz_results.class_paths_geojson(hz_dir, cls), f"hz_paths_{cls}")
                if gj:
                    # style composed from the user's line prefs, not the raw constant —
                    # this rebuild path runs on run switching and restore, and the styled
                    # look must survive it.
                    lyr = GeoJSON(data=gj, style=_fp_line_style(cls),
                                  hover_style=_fp_hover_style(), name=f"{HZ_LABEL[cls]} paths")
                    lyr.on_click(_on_hz_path_click)
                    _set_layer(f"hz_paths_{cls}", lyr)
                    # entry/return dots for this class — folded into the class node's ui_tree
                    # layers, so one checkbox toggles the paths AND their dots together.
                    sub = (combined_4326[combined_4326["hz_class"] == cls]
                           if combined_4326 is not None else None)
                    s_gj, e_gj = (_hz_node_fc(sub, cls) if sub is not None and len(sub)
                                  else (None, None))
                    _tag_hz(s_gj, f"hz_nodes_{cls}_start")
                    _tag_hz(e_gj, f"hz_nodes_{cls}_end")
                    ns = (GeoJSON(data=s_gj, point_style=START_NODE_STYLE,
                                  name=f"{HZ_LABEL[cls]} entry") if s_gj else None)
                    ne = (GeoJSON(data=e_gj, point_style=END_NODE_STYLE,
                                  name=f"{HZ_LABEL[cls]} return") if e_gj else None)
                    for nd in (ns, ne):
                        if nd is not None:
                            nd.on_click(_on_hz_path_click)
                    _set_layer(f"hz_nodes_{cls}_start", ns)
                    _set_layer(f"hz_nodes_{cls}_end", ne)
                else:
                    _set_layer(f"hz_paths_{cls}", None)
                    _set_layer(f"hz_nodes_{cls}_start", None)
                    _set_layer(f"hz_nodes_{cls}_end", None)
                foot = _tag_hz(hz_results.footprint_geojson(hz_dir, cls), f"hz_foot_{cls}")
                if foot:
                    _set_layer(f"hz_foot_{cls}", GeoJSON(
                        data=foot, style=HZ_FOOT_STYLE[cls], name=f"{HZ_LABEL[cls]} footprint"))
                else:
                    _set_layer(f"hz_foot_{cls}", None)
            # streambed exchange map (Flows node): downwelling / upwelling cell rectangles
            for direction, style, label in (("down", FLOW_DOWN_STYLE, "Stream downwelling"),
                                            ("up", FLOW_UP_STYLE, "Stream upwelling")):
                ex = _tag_hz(hz_results.flow_exchange_geojson(hz_dir, direction),
                             f"hz_flow_{direction}")
                _set_layer(f"hz_flow_{direction}",
                           GeoJSON(data=ex, style=style, name=label) if ex else None)
            # everything above went into _layer_shadow (creation park) — reveal the checked
            # keys once the burst + 3-D churn settle; the reveal schedules its own verify.
            _schedule_hz_reveal()
        # Fresh layers carry class/single styling only: a persisted rainbow line mode
        # needs its per-feature bake (and the 3-D scalars) re-applied over them.
        with reactive.isolate():
            _lm = fp_line_mode_v()
        if _lm in FP_LINE_RAINBOW:
            await _apply_fp_line_style()
        # heal any client leftovers of keys the server considers gone (parked/nulled) —
        # never live trait-hidden widgets (a sweep would strip their views' layers)
        await _sweep_hz([k for k in _hidden_keys
                         if k.startswith("hz_") and _layers.get(k) is None])
        return combined_4326

    @reactive.effect
    async def _hz_done():
        status = hz_task.status()
        if status in ("initial", "running", "cancelled"):
            return
        if not _task_armed["hz"]:       # already applied (or session reset) — see _task_armed
            return
        _task_armed["hz"] = False
        hz_log_tick.set(len(hz_log_lines))
        if status == "error":
            msg = "Hyporheic-zone analysis failed."
            try:
                hz_task.result()
            except Exception as e:  # noqa: BLE001
                msg = f"Hyporheic-zone analysis failed: {e}"
            hz_log_lines.append(msg); hz_log_tick.set(len(hz_log_lines))
            ui.notification_show(msg, type="error", duration=12)
            return
        try:
            res = hz_task.result()
        except Exception:  # noqa: BLE001
            ui.notification_show("Hyporheic-zone analysis failed.", type="error", duration=8)
            return
        hz_result.set(res)
        hz_sel_pids.set(())
        hz_dir = res["hz_dir"]
        combined_4326 = await _show_hz_layers(hz_dir)
        with reactive.isolate():
            _stale_marks.set(frozenset(_stale_marks() - {"hz"}))
        # Canonical results are captured the moment HZ completes (readiness was established
        # just above, so the saved state cannot pair a fresh result with a stale HZ marker).
        # Reports and cross-project comparisons are downstream renderers of this snapshot.
        try:
            _capture_canonical_results(hz=res)
            _save_project_file()
        except Exception as exc:  # noqa: BLE001 — the successful HZ run itself remains usable
            hz_log_lines.append(f"[canonical results] capture failed: {exc}")
            hz_log_tick.set(len(hz_log_lines))
            ui.notification_show("Hyporheic results completed, but the canonical result "
                                 f"snapshot could not be saved: {exc}", type="warning",
                                 duration=10)
        # 3-D per class: classed lines + translucent zone volumes (shared with the
        # restore/reconnect scene rebuild)
        await _send_hz_3d(hz_dir, combined_4326)
        st = (res.get("stats") or {}).get("classes", {}).get("hyporheic", {})
        vol = st.get("volume_m3", 0.0)
        counts = (res.get("stats") or {}).get("counts", {})
        pct = (100.0 * counts.get("n_classified", 0) / max(counts.get("n_seeds", 1), 1))
        _request_gms_build(include_hz=True)   # refresh the GMS folder with the particle sets
        ui.notification_show(
            f"Hyporheic zone delineated — {vol:,.0f} m³ ({pct:.0f}% of particles classified).",
            duration=7)

    @reactive.effect
    def _cancel_hz():
        if not _clicked_dynamic("cancel_hz"):
            return
        p = _hz_proc.get("p")
        if p is not None:
            try:
                if p.is_alive():
                    p.terminate()
            except Exception:  # noqa: BLE001
                pass
        try:
            hz_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        hz_log_lines.append("[analysis cancelled by user]")
        hz_log_tick.set(len(hz_log_lines))
        ui.notification_show("Hyporheic-zone analysis cancelled.", type="warning", duration=4)

    def _on_hz_path_click(**kw):
        with reactive.isolate():
            if current_step() != STEP_RESULTS:
                return
        props = (kw.get("feature") or {}).get("properties") or {}
        pid = props.get("particleid")
        cls = props.get("hz_class")
        if pid is None:
            return
        _map_ui["map_sel_ts"] = time.monotonic()       # consumed → mapclear skips it
        hz_sel_pids.set((int(pid),))
        suf = ui_tree.HZ_CLASS_SUFFIX.get(cls)
        node = f"gw.res.paths.{suf}" if suf else "gw.res.paths"
        with reactive.isolate():
            if sel_node() != node:
                sel_src.set("map")
                sel_node.set(node)

    @reactive.effect
    async def _hz_selection_layer():
        # Sole owner of the "hz_paths_sel" highlight (selected classed path).
        pids = hz_sel_pids()
        if not _HAS_MAP:
            return
        with reactive.isolate():
            gdf = hz_gdf()
        sel = None
        if pids and gdf is not None:
            sub = gdf[gdf["particleid"].isin(pids)]
            if len(sub):
                try:
                    sub2 = sub.copy()
                    sub2["geometry"] = sub2.geometry.force_2d()
                except Exception:  # noqa: BLE001
                    sub2 = sub
                sel = GeoJSON(data=_tag_hz(results.gdf_geojson(sub2), "hz_paths_sel"),
                              style=SEL_STYLE, name="Selected classed path")
        await _sweep_hz(["hz_paths_sel"])   # sweep BEFORE the add so it can't eat the new one
        _set_layer("hz_paths_sel", sel)

    @reactive.effect
    async def _update_head_layer():
        try:
            idx = input.head_layer()        # slider exists only on the head props pane
        except Exception:  # noqa: BLE001
            return
        if idx is None or not head_tifs():
            return
        head_layer_v.set(int(idx))          # persist so the slider survives pane re-renders
        try:
            _render_head_layer(idx)
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Head layer render issue: {e}", duration=5)
            return
        # The 3-D head drape follows the slider, but ONLY for the Basecase: while an
        # alternative is displayed the caches hold ITS pixels, and pushing them would leak
        # alternative heads into the Basecase-only 3-D scene.
        origin, z0 = _scene_frame()
        if origin is not None and alt_view() is None:
            try:
                ov = _head_cache.get(int(idx) - 1)
                await _send_3d(scene.drape_payload("head", ov, _scene.get("crs"), origin,
                                                   lift=0.6, opacity=0.8))
            except Exception:  # noqa: BLE001
                pass

    async def _send_hz_3d(hz_dir=None, combined_4326=None):
        """Classed 3-D pathlines + zone volumes for the Basecase HZ result. Fired at
        delineation completion and by the restore/reconnect scene rebuild; inputs
        default to the session's (restored) state."""
        with reactive.isolate():
            if hz_dir is None:
                hz_dir = (hz_result() or {}).get("hz_dir")
            if combined_4326 is None:
                combined_4326 = hz_gdf()
            origin, z0 = _scene_frame()
        if not hz_dir or origin is None:
            return
        crs_s = _scene.get("crs")
        for cls in HZ_CLASSES:
            try:
                sub = combined_4326
                if sub is not None:
                    sub = sub[sub["hz_class"] == cls]
                _lst = _fp_line_style(cls)
                with reactive.isolate():
                    _lw3 = max(1.0, float(fp_line_weight_v()) * 1.5)
                p3 = (scene.flowpaths_payload(sub, crs_s, origin, z0,
                                              key=f"hz3d_paths_{cls}",
                                              # rainbow modes carry no layer color —
                                              # the class color seeds the depth ramp
                                              # until the style push recolors it
                                              color=(_lst.get("color")
                                                     or HZ_COLORS.get(cls, "#0d9488")),
                                              width=_lw3, opacity=_lst["opacity"])
                      if sub is not None and len(sub) else None)
                await _send_3d(p3)
            except Exception:  # noqa: BLE001
                pass
            try:
                va = hz_results.volume_arrays(hz_dir, cls)
                if va is not None:
                    await _send_3d(scene.volume_payload(
                        f"hz3d_vol_{cls}", va[0], va[1], origin, z0,
                        color=HZ_COLORS[cls], opacity=0.35))
            except Exception:  # noqa: BLE001
                pass
        # A rainbow line mode must survive a scene rebuild: the freshly built line
        # layers carry only their seed color, so re-push the style (lmode + trng)
        # once the geometry messages are out. No 2-D re-bake — data is untouched.
        with reactive.isolate():
            _lm3 = fp_line_mode_v()
        if _lm3 in FP_LINE_RAINBOW:
            await _send_fp_line_3d()

    async def _rebuild_3d_scene():
        """Re-send the whole 3-D scene from current state. Every content send lives in
        a compute-time completion effect, so a restored project (or a reloaded client,
        which loses all vtk state) would otherwise show bare terrain: no grid, no
        basemap drape (its textures ride ONLY the hype_mesh payload), no classed
        paths, no volumes. Idempotent and quiet; each step no-ops without its inputs.
        Terrain needs no step here — _push_terrain_3d self-heals off the DEM."""
        if not _HAS_MAP:
            return
        with reactive.isolate():
            g = mesh_geom()
        if g:
            # Reconnect: the payload (drape textures included) is still warm in this
            # session — resend directly, no mesh-child respawn, no basemap refetch.
            await session.send_custom_message("hype_mesh", g)
            await session.send_custom_message(
                "hype3d_vis", {"key": "basemap", "on": _eff_checked("base.imagery")})
            await session.send_custom_message(
                "hype3d_vis", {"key": "basemap_topo", "on": _eff_checked("base.topo")})
        else:
            # Restore: rebuild from the run's DIS on disk (the _run_done recipe — the
            # pre-run preview bed can sit metres above the run's, so never use it here).
            try:
                gwf_ws = work_dir / "model" / "gwf_workspace"
                if next(gwf_ws.glob("*.dis.grb"), None) is not None:
                    with reactive.isolate():
                        origin0, z00 = _scene_frame()
                        build = _domain_build()
                        # proj_crs() can still be None mid-restore; _scene_frame() just
                        # resolved the frame CRS (same source as every other payload).
                        crs_obj = _scene.get("crs")
                        crs_wkt = (crs_obj.to_wkt()
                                   if origin0 is not None and crs_obj is not None else None)
                    if origin0 is not None and crs_wkt is not None:
                        _grid_auto["on"] = True     # quiet build — stay in the current view
                        mesh_task({
                            "run_ws": str(gwf_ws), "crs": crs_wkt,
                            "sides": ({k: build[k] for k in ("up", "left", "right", "down")}
                                      if build else None),
                            "scene_z0": z00,
                        })
                        print("[3d rebuild] grid mesh task fired", flush=True)
                    else:
                        print("[3d rebuild] grid skipped: no scene frame", flush=True)
            except Exception as e:  # noqa: BLE001 — the grid is a nicety
                _grid_auto["on"] = False
                print("[3d rebuild] grid skipped:", repr(e)[:300], flush=True)
        # Re-assert the saved 3D display prefs. The checkbox/slider VALUES restore via
        # _kept, but the pre-open clear reset the client (S.wireframe false) and the
        # server mirrors, and the toggle effects' ignore_init swallows the pane-mount
        # value — so without this send the pane shows ON while the scene renders OFF
        # (the toggle-off-and-on report). Read _kept, not the inputs: the pane may
        # never have mounted.
        wire = bool(_kept.get("grid_wireframe", False))
        if wire:
            with reactive.isolate():
                _wire_state.set(True)    # keep the toggle's change guard truthful
            await session.send_custom_message("hype3d_wire", {"on": True})
            print("[3d rebuild] wireframe re-asserted", flush=True)
        try:
            op_kept = max(0.05, min(float(_kept.get("grid_opacity3d", 1.0)), 1.0))
        except (TypeError, ValueError):
            op_kept = 1.0
        with reactive.isolate():
            if abs(op_kept - float(grid_opacity3d_v())) > 1e-9:
                grid_opacity3d_v.set(op_kept)
            non_default_grid = (grid_color3d_v() is not None
                                or abs(float(grid_opacity3d_v()) - 1.0) > 1e-9)
        if non_default_grid:
            await _send_grid_style()     # a reloaded client lost its stored grid style
        await _send_hz_3d()
        with reactive.isolate():
            origin, z0 = _scene_frame()
        if origin is None:
            return
        crs_s = _scene.get("crs")
        try:
            if _ras_overlays.get("depth") is not None:
                await _send_3d(scene.drape_payload("depth", _ras_overlays.get("depth"),
                                                   crs_s, origin, lift=0.45, opacity=0.85))
            if _ras_overlays.get("wse") is not None:
                await _send_3d(scene.drape_payload("wse", _ras_overlays.get("wse"),
                                                   crs_s, origin, lift=0.35, opacity=0.85))
        except Exception:  # noqa: BLE001
            pass
        with reactive.isolate():
            av = alt_view()
            try:
                hl = int(head_layer_v() or 1)
            except Exception:  # noqa: BLE001
                hl = 1
        if av is None:
            try:
                ov = _head_cache.get(hl - 1)
                if ov is not None:
                    await _send_3d(scene.drape_payload("head", ov, crs_s, origin,
                                                       lift=0.6, opacity=0.8))
            except Exception:  # noqa: BLE001
                pass

    @reactive.effect
    async def _rebuild_3d_on_reconnect():
        # A page reload drops every vtk actor client-side while the server stays warm:
        # the tree's ready ping (fires on every (re)connect) re-arms the whole scene
        # exactly like restore does. No-ops on a fresh session; every read inside the
        # rebuild is isolated, so _tree_ready is this effect's only dependency.
        if _tree_ready() == 0:
            return
        await _rebuild_3d_scene()

    _relayer_due = reactive.value(0.0)      # monotonic deadline to re-assert scheduled layers
    _relayer_keys: set = set()              # layer keys queued for the post-burst re-add

    def _schedule_relayer(keys, delay=2.4):
        """Queue layer keys for a re-add once the current update burst settles. Bursty flushes
        (step changes, run completion) can exceed what the ipyleaflet client applies atomically —
        freshly added layers sometimes vanish client-side while server state stays correct. A
        late remove+re-add of the same widget makes them stick (the proven pathline fix)."""
        _relayer_keys.update(keys)
        with reactive.isolate():
            _relayer_due.set(time.monotonic() + delay)

    @reactive.effect
    def _reassert_layers():
        # ONE layer per pass, trickled 0.3 s apart — re-adding several at once is itself a
        # burst the client can drop from, which defeats the repair.
        due = _relayer_due()
        if not due or not _HAS_MAP:
            return
        remaining = due - time.monotonic()
        if remaining > 0.05:
            reactive.invalidate_later(max(remaining, 0.1))
            return
        key = None
        while _relayer_keys:
            k = _relayer_keys.pop()
            if k in _hidden_keys:           # unchecked since queueing — never resurrect it
                continue
            if _layers.get(k) is not None:
                key = k
                break
        if key is None:                     # queue drained
            with reactive.isolate():
                _relayer_due.set(0.0)
            return
        lyr = _layers.get(key)
        if isinstance(lyr, GeoJSON):
            # Rebuild a FRESH widget (new model id) — re-adding the SAME object races the
            # client's in-flight view teardown and can wipe it entirely (the LayersControl
            # "kick" failure mode); a fresh widget added after the churn sticks (the
            # Modeled-extent precedent). Click handlers are carried over (_clone_vector).
            _set_layer(key, _clone_vector(lyr))
        elif key == "head":
            with reactive.isolate():        # owner rebuild: fresh group + cached payloads
                idx = int(head_layer_v())
            try:
                _render_head_layer(idx)
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(lyr, ImageOverlay):
            try:                            # fresh overlay, same payload — same rationale
                _set_layer(key, ImageOverlay(url=lyr.url, bounds=lyr.bounds,
                                             opacity=float(getattr(lyr, "opacity", 1.0)),
                                             pane=getattr(lyr, "pane", "") or "",
                                             name=getattr(lyr, "name", key) or key))
            except Exception:  # noqa: BLE001
                _set_layer(key, lyr)
        elif isinstance(lyr, LayerGroup):
            try:                            # fresh group, same children — same rationale
                _set_layer(key, LayerGroup(layers=tuple(lyr.layers),
                                           name=getattr(lyr, "name", key) or key))
            except Exception:  # noqa: BLE001
                _set_layer(key, lyr)
        else:
            _set_layer(key, lyr)            # images: remove + re-add of the same object
        with reactive.isolate():            # stagger the next key onto its own tick
            _relayer_due.set(time.monotonic() + 0.45)

    # --- targeted post-burst heal (replaces the blind HZ relayer that flickered) ---------------
    # After a layer-add burst settles, ask the client which expected layers actually dropped and
    # re-add ONLY those. The common case (nothing dropped) does zero work — no flicker.
    _verify_due = reactive.value(0.0)       # monotonic deadline to ask the client what dropped
    _verify_keys: list = []                 # keys we expect visible after the current burst

    def _schedule_verify(keys, delay=1.5):
        _verify_keys[:] = [k for k in keys if k]
        with reactive.isolate():
            _verify_due.set(time.monotonic() + delay)

    @reactive.effect
    async def _send_verify():
        due = _verify_due()
        if not due or not _HAS_MAP:
            return
        remaining = due - time.monotonic()
        if remaining > 0.05:
            reactive.invalidate_later(max(remaining, 0.1))
            return
        keys = list(_verify_keys)
        with reactive.isolate():
            _verify_due.set(0.0)
        if keys:
            try:
                await session.send_custom_message(
                    "hype_map_verify", {"keys": keys, "nonce": time.monotonic_ns()})
            except Exception:  # noqa: BLE001
                pass

    _miss_once: set = set()                 # keys reported missing ONCE — awaiting confirmation

    @reactive.effect
    @reactive.event(input.hype_map_missing)
    async def _heal_missing():
        # TWO-STRIKE confirm before healing. A "missing" report taken while the client is busy
        # (run-completion burst, 3-D scene builds) can be a FALSE positive — the view is merely
        # in-flight — and healing then remove+re-adds a widget whose removal races that view's
        # creation, stranding an orphaned leaflet layer no checkbox can remove (the 2026-07-09
        # stuck-flow-paths root cause). Heal only keys missing in two consecutive verifies; a
        # view that was in-flight is present by the second look ~2 s later.
        evt = input.hype_map_missing() or {}
        missing = [k for k in (evt.get("keys") or [])
                   if k not in _hidden_keys    # user hid it since the verify — don't resurrect
                   and isinstance(_layers.get(k), GeoJSON)]   # only GeoJSON views are healable
        confirmed = [k for k in missing if k in _miss_once]
        _miss_once.clear()
        _miss_once.update(k for k in missing if k not in confirmed)
        if confirmed:
            # sweep BEFORE re-adding (the _sweep_hz doctrine): any late-materialized ghost of
            # the doomed widget must die before its fresh clone lands, or it lingers forever
            await _sweep_hz(confirmed)
            for k in confirmed:
                lyr = _layers.get(k)
                if isinstance(lyr, GeoJSON):    # fresh widget (new model id) re-materializes a
                    _set_layer(k, _clone_vector(lyr))   # dropped view — second-strike keys only
        if _miss_once:
            _schedule_verify(sorted(_miss_once), 1.75)  # confirm pass — heal on strike two

    # --- whole-channel wedge heal ---------------------------------------------------------
    # tree.js sniffs the embed manager's "Could not process update msg for model id" console
    # errors — updates aimed at widgets whose comm-open never materialized. When that wedge
    # hits (2026-07-15: every layer created after the reach stage was server-alive but
    # permanently invisible), no re-add of the SAME objects can help; every present layer is
    # re-materialized as a fresh widget instead. State-preserving by construction: check
    # state, parks, sliders and result reactives are untouched — only widget identities
    # change. "head" and "grad_pts" go through their owners so their internal wiring
    # (_head_img opacity target, pin handlers) stays live.
    _widget_heal_t = {"t": 0.0}             # rate limiter — an unhealable page must not loop

    @reactive.effect
    @reactive.event(input.hype_widget_dead)
    def _widget_heal():
        if not _HAS_MAP:
            return
        now = time.monotonic()
        if now - _widget_heal_t["t"] < 20.0:
            return
        _widget_heal_t["t"] = now
        print("[map-heal] client reported dead widget models - rebuilding map layers")
        healed: list = []
        for k in list(_layers):
            lyr = _layers.get(k)
            if lyr is None or k in ("head", "grad_pts", "obs_wells"):
                continue
            try:
                fresh = _clone_layer(lyr)
                if fresh is not lyr:
                    _set_layer(k, fresh)
                    healed.append(k)
            except Exception:  # noqa: BLE001 — one bad layer must not kill the heal
                pass
        for k in list(_layer_shadow):       # parked widgets are dead too — swap the parks
            try:
                fresh = _clone_layer(_layer_shadow[k])
                if fresh is not _layer_shadow[k]:
                    _layer_shadow[k] = fresh
            except Exception:  # noqa: BLE001
                pass
        if _layers.get("head") is not None or _layer_shadow.get("head") is not None:
            try:
                _render_head_layer(int(head_layer_v()))   # owner rebuild — keeps _head_img wired
                healed.append("head")
            except Exception:  # noqa: BLE001
                pass
        if _layers.get("grad_pts") is not None:
            grad_ver.set(grad_ver() + 1)    # overlay owner rebuilds pins with live wiring
        if _layers.get("obs_wells") is not None:
            wells_ver.set(wells_ver() + 1)  # same owner-rebuild rule for the well markers
        if healed:
            # The rebuild is itself a burst the client can drop from (observed: the DEM
            # overlay vanished on a second heal pass) — trickle every healed key through the
            # relayer, which re-adds them one at a time as fresh objects until they stick.
            _schedule_relayer(healed, 2.0)
            ui.notification_show(f"Map display recovered — {len(healed)} layer(s) rebuilt "
                                 f"after a browser sync glitch.", duration=8)

    # --- deferred hz reveal: materialize the default-visible Zone layers AFTER the burst -------
    # _hz_done parks every hz layer at creation (no original ever rides the completion burst —
    # see the orphan post-mortem there). This effect flips the checked keys live once the client
    # has digested the burst + 3-D payloads, via _apply_check_effective → the same park→clone-add
    # path a calm checkbox click uses. Re-deriving from _eff_checked at fire time means a key the
    # user unchecked while waiting simply stays parked; a re-run overwrites the single deadline,
    # so a stale reveal never fires over fresh state.
    _hz_reveal_due = reactive.value(0.0)

    def _schedule_hz_reveal(delay=2.75):
        with reactive.isolate():
            _hz_reveal_due.set(time.monotonic() + delay)

    @reactive.effect
    def _hz_reveal():
        due = _hz_reveal_due()
        if not due or not _HAS_MAP:
            return
        remaining = due - time.monotonic()
        if remaining > 0.05:
            reactive.invalidate_later(max(remaining, 0.1))
            return
        with reactive.isolate():
            _hz_reveal_due.set(0.0)
        _apply_check_effective("gw.res.hz")
        # the clones are live now — run the targeted dropped-add verify on just those keys
        _schedule_verify([k for k in _HZ_TREE_KEYS if _layers.get(k) is not None], 1.75)

    def _reset_res_layer_vis():
        """A fresh run replaces every groundwater result layer — re-tick the finishing
        producer's rows (their groups too) and drop stale shadow parks so nothing new
        arrives invisibly."""
        for nid in ("gw.res.head", "gw.mesh", "sw.wse"):
            for k in ui_tree.NODE_LAYERS.get(nid, ()):
                _layer_shadow.pop(k, None)  # before apply: never resurrect a dead parked widget
            _unhide_node_layers(nid)

    @reactive.effect
    def _head_contours_toggle():
        try:
            v = input.head_contours_chk()   # checkbox exists only on the Results step
        except Exception:  # noqa: BLE001
            return
        if v is None or bool(v) == hd_contours_v():
            return
        hd_contours_v.set(bool(v))
        with reactive.isolate():
            if head_tifs():
                try:
                    _render_head_layer(int(head_layer_v()))   # rebuild the head group ± contours
                except Exception:  # noqa: BLE001
                    pass

    @reactive.effect
    def _head_opacity():
        try:
            op = input.head_opacity()       # mutable ImageOverlay.opacity → live, no re-render
        except Exception:  # noqa: BLE001
            return
        if op is None:
            return
        head_opacity_v.set(float(op))       # persist so the slider survives pane re-renders
        lyr = _head_img.get("lyr")          # the ImageOverlay inside the head LayerGroup
        if lyr is not None:
            try:
                lyr.opacity = float(op)
            except Exception:  # noqa: BLE001
                pass

    # ---- flow-path particle animation (rendered client-side by www/path_anim.js) ----
    async def _send_fp_anim():
        # Tiny settings push; the animator reads geometry + residence times off the live
        # hz_paths_* layers itself (total_time_d rides every feature), so this message is
        # the server's ONLY involvement. Speed is the raw slider value: the client anchors
        # the median displayed path to a 36/speed-second loop, because residence times are
        # log-distributed and only the client sees the displayed population.
        with reactive.isolate():
            on = bool(fp_anim_on_v())
            speed = float(fp_anim_speed_v())
            color = fp_anim_color_v()
            style = fp_anim_style_v()
            mode = fp_anim_mode_v()
            lmode = fp_line_mode_v()
            lw = float(fp_line_weight_v())
            lop = float(fp_line_opacity_v()) if fp_line_show_v() else 0.0
        await session.send_custom_message("hype_fp_anim", {
            "on": on, "speed": max(speed, 0.1), "color": color, "style": style,
            "mode": mode, "lmode": lmode, "lw": lw, "lop": lop})

    @reactive.effect
    @reactive.event(input.fp_anim_on, ignore_init=True)
    async def _fp_anim_toggle():
        # Change-guarded so pane remounts (which re-register the input at its persisted
        # value) don't re-send — the grid_wireframe pattern.
        v = bool(input.fp_anim_on())
        if v == fp_anim_on_v():
            return
        fp_anim_on_v.set(v)
        await _send_fp_anim()

    @reactive.effect
    @reactive.event(input.fp_anim_speed, ignore_init=True)
    async def _fp_anim_speed():
        v = float(input.fp_anim_speed())
        if v == fp_anim_speed_v():
            return
        fp_anim_speed_v.set(v)
        await _send_fp_anim()

    @reactive.effect
    @reactive.event(input.fp_anim_color_evt)   # nonce event input: NEVER ignore_init — the
    async def _fp_anim_color():                # input first exists at the first click, so
        # ignore_init would eat that click (bit us live 2026-07-25)
        c = (input.fp_anim_color_evt() or {}).get("c")
        if c not in FP_ANIM_COLORS:
            return
        if c == fp_anim_color_v() and fp_anim_mode_v() == "solid":
            return
        fp_anim_color_v.set(c)     # the pane reads this un-isolated → the active ring moves
        fp_anim_mode_v.set("solid")   # picking a swatch is an implicit return to solid
        await _send_fp_anim()

    @reactive.effect
    @reactive.event(input.fp_anim_style_evt)   # nonce event input: no ignore_init (see above)
    async def _fp_anim_style():
        s = (input.fp_anim_style_evt() or {}).get("s")
        if s not in ("comet", "dots") or s == fp_anim_style_v():
            return
        fp_anim_style_v.set(s)     # un-isolated pane read → the active button swaps
        await _send_fp_anim()

    @reactive.effect
    @reactive.event(input.fp_anim_mode_evt)   # nonce event input: no ignore_init (see above)
    async def _fp_anim_mode():
        m = (input.fp_anim_mode_evt() or {}).get("m")
        if m not in FP_ANIM_MODES or m == fp_anim_mode_v():
            return
        fp_anim_mode_v.set(m)      # un-isolated pane read → the active button swaps
        await _send_fp_anim()

    @reactive.effect
    async def _fp_anim_repush():
        # A page reload keeps the server-side "on" but starts a silent client; the tree's
        # ready ping (fires on every (re)connect) re-arms the animator. Value reads are
        # isolated so toggling never double-sends through this effect.
        if _tree_ready() == 0:
            return
        with reactive.isolate():
            on = bool(fp_anim_on_v())
            lmode = fp_line_mode_v()
        if on or lmode in FP_LINE_RAINBOW:   # rainbow lines need the canvas legend too
            await _send_fp_anim()

    # ---- flow-path LINE styling (lines are the four hz_paths_* GeoJSON layers) ----
    def _fp_line_style(cls) -> dict:
        # Composes the class constant with the user's display prefs. className stays: the
        # click-routing whitelist and box select key on "hype-fp-line". Reads are isolated
        # because _show_hz_layers calls this from inside arbitrary effects.
        with reactive.isolate():
            show = bool(fp_line_show_v())
            weight = float(fp_line_weight_v())
            opacity = float(fp_line_opacity_v())
            mode = fp_line_mode_v()
        st = dict(HZ_PATH_STYLE[cls])
        st["weight"] = weight
        st["opacity"] = (opacity if show else 0.0)
        if mode in FP_LINE_RAINBOW:
            # ipyleaflet merges the layer style OVER per-feature properties.style per
            # key, so the baked residence-time colors only show when no color is here.
            st.pop("color", None)
        return st

    def _fp_hover_style() -> dict:
        # Invisible lines must not flash on hover, so hiding also blanks the hover style.
        with reactive.isolate():
            return dict(PATH_HOVER) if fp_line_show_v() else {}

    def _fp_time_range():
        """Rainbow scale range over the DISPLAYED population: total_time_d of every
        path in a visible class (the same set the client animator sees, so all the
        legends agree). None when nothing is displayed. Reads are isolated — shared
        helpers must never gift dependencies to their callers."""
        with reactive.isolate():
            g = hz_gdf()
        if g is None or "total_time_d" not in getattr(g, "columns", ()):
            return None
        vis = [cls for cls in HZ_CLASSES
               if _eff_checked(f"gw.res.paths.{ui_tree.HZ_CLASS_SUFFIX[cls]}")]
        if not vis:
            return None
        tds = [float(t) for t in g[g["hz_class"].isin(vis)]["total_time_d"].tolist()
               if t and float(t) > 0]
        return video_mod.time_range_days(tds) if tds else None

    def _bake_fp_line_colors(rng) -> None:
        """Per-feature residence-time colors baked INTO the layer data (live + parked
        shadow, so clones and reveals keep them). Leaving a rainbow mode needs no
        un-bake: the layer style's color merges back over these. Elapsed bakes the
        total-time color per line as the under-stroke and zoom-animation fallback;
        the animation canvas paints the true along-path gradient over it
        (path_anim.js buildLineCache), as do the 3-D view and the captures."""
        if rng is None:
            return
        for cls in HZ_CLASSES:
            key = f"hz_paths_{cls}"
            for obj in (_layers.get(key), _layer_shadow.get(key)):
                if obj is None or not getattr(obj, "data", None):
                    continue
                try:
                    data = dict(obj.data)
                    feats = list(data.get("features") or [])
                    tds = [float((f.get("properties") or {}).get("total_time_d")
                                 or 0.0) for f in feats]
                    cols = video_mod.time_hex_colors(tds, rng)
                    out = []
                    for f, c in zip(feats, cols):
                        f = dict(f)
                        props = dict(f.get("properties") or {})
                        pst = dict(props.get("style") or {})
                        pst["color"] = c
                        props["style"] = pst
                        f["properties"] = props
                        out.append(f)
                    data["features"] = out
                    obj.data = data
                except Exception:  # noqa: BLE001 — a dying widget must not kill the batch
                    pass

    _fp_rng_applied = {"rng": None}    # last rng _apply_fp_line_style baked (rescale guard)

    async def _apply_fp_line_style(bake: bool = True):
        # Live restyle of the four class layers (and their parked shadows, so a later
        # clone carries the style) without any widget churn — the _sync_map_layers idiom.
        # Rainbow modes additionally bake per-feature colors into the data; ORDER is
        # load-bearing: the color-less layer style must land BEFORE the data re-add,
        # because Leaflet's setStyle MERGES (a removed key never unsets — the rendered
        # color only changes at addData time, against the then-current layer style).
        # bake=False for weight/opacity/show tweaks: the data is already baked, and a
        # re-bake per slider tick would be pure client churn.
        hover = _fp_hover_style()
        with reactive.isolate():
            weight = float(fp_line_weight_v())
            show = bool(fp_line_show_v())
            opacity = float(fp_line_opacity_v()) if show else 0.0
            lmode = fp_line_mode_v()
        rng = _fp_time_range() if lmode in FP_LINE_RAINBOW else None
        for cls in HZ_CLASSES:
            key = f"hz_paths_{cls}"
            st = _fp_line_style(cls)
            for obj in (_layers.get(key), _layer_shadow.get(key)):
                if obj is None:
                    continue
                try:
                    obj.style = st
                    obj.hover_style = hover
                except Exception:  # noqa: BLE001 — a dying widget must not kill the batch
                    pass
        if bake and rng is not None:
            _bake_fp_line_colors(rng)
        _fp_rng_applied["rng"] = rng
        # 3-D parity rides the same state: a light style message per class actor, no
        # geometry re-send (rainbow scalars rebuild client-side from the retained
        # per-path times + trng). mesh3d ignores keys it has not built yet.
        await _send_fp_line_3d(rng)

    async def _send_fp_line_3d(rng=None):
        """The per-class hype3d_style push (color/width/opacity + line color-by mode).
        Split out so a 3-D scene rebuild can re-assert the line coloring without
        re-baking the 2-D layer data."""
        with reactive.isolate():
            weight = float(fp_line_weight_v())
            show = bool(fp_line_show_v())
            opacity = float(fp_line_opacity_v()) if show else 0.0
            lmode = fp_line_mode_v()
        if rng is None and lmode in FP_LINE_RAINBOW:
            rng = _fp_time_range()
        for cls in HZ_CLASSES:
            st = _fp_line_style(cls)
            try:
                msg = {"key": f"hz3d_paths_{cls}",
                       "color": st.get("color") or HZ_COLORS.get(cls, "#0d9488"),
                       "width": max(1.0, weight * 1.5), "opacity": opacity,
                       "lmode": lmode}
                if rng is not None and lmode in FP_LINE_RAINBOW:
                    msg["trng"] = [float(rng[0]), float(rng[1])]
                await session.send_custom_message("hype3d_style", msg)
            except Exception:  # noqa: BLE001
                pass

    @reactive.effect
    @reactive.event(input.fp_line_show, ignore_init=True)
    async def _fp_line_show():
        v = bool(input.fp_line_show())
        if v == fp_line_show_v():
            return
        fp_line_show_v.set(v)
        await _apply_fp_line_style(bake=False)
        if fp_line_mode_v() == "elapsed":   # the 2-D gradient canvas tracks the stroke style
            await _send_fp_anim()

    @reactive.effect
    @reactive.event(input.fp_line_weight, ignore_init=True)
    async def _fp_line_weight():
        v = float(input.fp_line_weight())
        if v == fp_line_weight_v():
            return
        fp_line_weight_v.set(v)
        await _apply_fp_line_style(bake=False)
        if fp_line_mode_v() == "elapsed":   # the 2-D gradient canvas tracks the stroke style
            await _send_fp_anim()

    @reactive.effect
    @reactive.event(input.fp_line_opacity, ignore_init=True)
    async def _fp_line_opacity():
        v = float(input.fp_line_opacity())
        if v == fp_line_opacity_v():
            return
        fp_line_opacity_v.set(v)
        await _apply_fp_line_style(bake=False)
        if fp_line_mode_v() == "elapsed":   # the 2-D gradient canvas tracks the stroke style
            await _send_fp_anim()

    @reactive.effect
    @reactive.event(input.fp_line_mode_evt)    # nonce event input: no ignore_init (the
    async def _fp_line_mode():                 # input first exists at the first click)
        m = (input.fp_line_mode_evt() or {}).get("m")
        if m not in FP_LINE_MODES or m == fp_line_mode_v():
            return
        fp_line_mode_v.set(m)      # un-isolated pane read moves the active button
        await _apply_fp_line_style()
        await _send_fp_anim()      # the 2-D canvas legend follows the line mode too

    @reactive.effect
    async def _fp_rainbow_rescale():
        # The rainbow scale spans the DISPLAYED population, so class checkbox toggles
        # re-stretch it: re-bake the line colors and re-push the 3-D scalars — but only
        # when the range actually moved (the bake is the one churny operation here).
        _vis_state()
        with reactive.isolate():
            lmode = fp_line_mode_v()
        if lmode not in FP_LINE_RAINBOW:
            return
        if _fp_time_range() == _fp_rng_applied["rng"]:
            return
        await _apply_fp_line_style()


    # ---- flow-path animation video export (rendered by hype_app/video.py) ----
    _video_build_id = reactive.value(0)
    _video_result = reactive.value(None)     # last completed build: video.py result dict
    # worker-thread progress bridged by a polled tick — the _ras_prog idiom
    _video_prog: dict = {"stage": "", "i": 0, "n": 0, "t0": 0.0}
    _video_cancel = threading.Event()
    video_tick = reactive.value(0)

    def _label_from_marker(mk):
        """(lat, lon, text, color) from a DivIcon label marker, or None.

        Only the simple pattern the app itself produces is parsed
        (<div ... style="...color:{c}...">TEXT</div>); anything else is skipped.
        """
        try:
            icon = getattr(mk, "icon", None)
            html = getattr(icon, "html", None) or ""
            loc = getattr(mk, "location", None)
            if not html or not loc:
                return None
            text = re.sub(r"<[^>]+>", " ", html).strip()
            if not text or len(text) > 40:
                return None
            m = re.search(r"color\s*:\s*(#[0-9a-fA-F]{3,8}|[a-zA-Z]+)", html)
            return {"lat": float(loc[0]), "lon": float(loc[1]), "text": text,
                    "color": (m.group(1) if m else "#1f3864")}
        except Exception:  # noqa: BLE001
            return None

    def _gather_map_scene() -> dict:
        """Freeze the visible 2-D map into plain data for hype_app/video.py.

        Walks the live layer registry (the same server-side truth the map runs
        on), classifies each widget, and emits draw-ordered buckets: pane
        rasters (terrain 320, ref 340), default-pane rasters, filled polygons,
        lines, flow paths, selection, points. DivIcon labels ride separately
        and draw last. Panes are the only z-order Leaflet guarantees, so this
        ordering is faithful; within buckets, registry insertion order holds.
        """
        import base64 as _b64

        buckets = {k: [] for k in ("terrain", "ref", "raster", "fill", "line",
                                   "paths", "sel", "point")}
        labels = []

        def _vector_bucket(key, gj):
            feats = (gj.data or {}).get("features") or []
            if not feats:
                return None
            if key.startswith("hz_paths_sel"):
                return "sel"
            if key.startswith("hz_paths_"):
                return "paths"
            gt = ((feats[0].get("geometry") or {}).get("type")) or ""
            if gt in ("Point", "MultiPoint"):
                return "point"
            if gt in ("Polygon", "MultiPolygon"):
                return "fill"
            return "line"

        def _classify(key, obj):
            if obj is None:
                return
            if isinstance(obj, GeoJSON):
                b = _vector_bucket(key, obj)
                if b:
                    buckets[b].append({"kind": "vector", "key": key,
                                       "data": obj.data,
                                       "style": dict(obj.style or {}),
                                       "point_style": dict(obj.point_style or {})})
                return
            if isinstance(obj, ImageOverlay):
                url = obj.url or ""
                if not url.startswith("data:image/"):
                    return
                try:
                    png = _b64.b64decode(url.split(",", 1)[1])
                except Exception:  # noqa: BLE001
                    return
                pane = (getattr(obj, "pane", "") or "")
                b = ("terrain" if pane == ml_mod.PANE_TERRAIN
                     else "ref" if pane == ml_mod.PANE_REF else "raster")
                buckets[b].append({"kind": "raster", "key": key, "png": png,
                                   "bounds": [[float(obj.bounds[0][0]), float(obj.bounds[0][1])],
                                              [float(obj.bounds[1][0]), float(obj.bounds[1][1])]],
                                   "opacity": float(getattr(obj, "opacity", 1.0) or 1.0)})
                return
            if isinstance(obj, Marker):
                lab = _label_from_marker(obj)
                if lab:
                    labels.append(lab)
                return
            if isinstance(obj, LayerGroup):
                for child in (obj.layers or ()):
                    _classify(key, child)

        for key, obj in list(_layers.items()):
            if obj is None or key in _hidden_keys:
                continue
            _classify(key, obj)
        items = (buckets["terrain"] + buckets["ref"] + buckets["raster"]
                 + buckets["fill"] + buckets["line"] + buckets["paths"]
                 + buckets["sel"] + buckets["point"])
        return {"items": items, "labels": labels}

    def _fp_video_settings(d: dict) -> tuple[float, int]:
        # Clip length and fps ride the export_evt payload now (the Capture dropdown's
        # Video settings group); clamp whatever the client sent.
        try:
            secs = float(d.get("secs"))
        except (TypeError, ValueError):
            secs = 8.0
        try:
            fps = int(d.get("fps"))
        except (TypeError, ValueError):
            fps = 30
        return max(2.0, min(secs, 30.0)), (15 if fps == 15 else 30)

    def _on_video_progress(stage, i, n):
        # Worker-thread writer; the poller mirrors it into video_tick. Plain dict
        # mutation only — no reactives off-loop (the _ras_prog idiom).
        _video_prog["stage"] = str(stage)
        _video_prog["i"] = int(i)
        _video_prog["n"] = int(n)

    @reactive.extended_task
    async def video_task(payload: dict) -> dict:
        # Worker thread like report_task. build_flowpath_video is pyplot-free (OO Figure +
        # Agg only), so it does not take _REPORT_MPL_LOCK and can overlap a report build.
        def _work():
            res = video_mod.build_flowpath_video(
                payload["spec"], payload["out"], log=lambda m: None,
                progress=_on_video_progress, cancel=_video_cancel)
            return {"res": res, "build_id": payload["build_id"]}
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    def _video_poll():
        # 0.5 s mirror of the worker's progress dict while the task runs (_ras_poll).
        # The tick value comes from the PLAIN dict, never from video_tick itself: a
        # self-read would make this effect depend on the value it sets and spin the
        # session in a synchronous invalidation loop for the whole build.
        if video_task.status() != "running":
            return
        reactive.invalidate_later(0.5)
        video_tick.set(int(_video_prog["i"]) * 100003 + int(time.monotonic() * 2))

    @reactive.effect
    @reactive.event(input.fp_video_cancel)
    def _fp_video_cancel():
        # The only mechanism that actually stops a worker thread: the cooperative
        # event, checked every frame in the encode loop (task.cancel alone cannot).
        _video_cancel.set()

    def _launch_video_build(b_override=None, w_override=None,
                            secs: float = 8.0, fps: int = 30) -> bool:
        # GATHER on-loop: every reactive read happens here; the worker gets plain data.
        # Fired from the header capture control (export_evt); returns True once the
        # task is actually launched so the caller can add start feedback. b_override
        # and w_override carry a Specified Window rubber-band rect (bounds + CSS px
        # width) so the render covers exactly the dragged area.
        hz = hz_view()
        if not hz or not hz.get("hz_dir"):
            ui.notification_show("Run the hyporheic calculations first.", type="warning")
            return False
        b = b_override or (input.map_bounds() if "map_bounds" in input else None)
        if not b or not all(k in (b or {}) for k in ("west", "south", "east", "north")):
            ui.notification_show("Move the map once so the view is known, then record.",
                                 type="warning")
            return False
        visible = [cls for cls in HZ_CLASSES
                   if _eff_checked(f"gw.res.paths.{ui_tree.HZ_CLASS_SUFFIX[cls]}")]
        if not visible:
            ui.notification_show("Every flow path class is unchecked. Show at least one "
                                 "class to record.", type="warning")
            return False
        with reactive.isolate():
            line = {"show": bool(fp_line_show_v()), "weight": float(fp_line_weight_v()),
                    "opacity": float(fp_line_opacity_v()), "mode": fp_line_mode_v()}
            anim = {"speed": float(fp_anim_speed_v()), "style": fp_anim_style_v(),
                    "color": fp_anim_color_v(), "mode": fp_anim_mode_v()}
        try:
            width_px = int(w_override or b.get("w") or 1280)
        except Exception:  # noqa: BLE001
            width_px = 1280
        basemap = ("imagery" if _eff_checked("base.imagery")
                   else "topo" if _eff_checked("base.topo") else "none")
        spec = {
            "hz_dir": hz["hz_dir"],
            "bounds4326": {k: float(b[k]) for k in ("west", "south", "east", "north")},
            "basemap": basemap,
            "visible_classes": visible,
            "line": line, "anim": anim,
            "class_colors": dict(HZ_COLORS),
            "scene": _gather_map_scene(),
            "duration_s": secs, "fps": fps, "width_px": width_px,
        }
        bid = _video_build_id() + 1
        _video_build_id.set(bid)
        _task_armed["video"] = True
        _video_cancel.clear()
        _video_prog.update({"stage": "starting", "i": 0, "n": 0,
                            "t0": time.monotonic()})
        video_task({"spec": spec, "out": str(work_dir / "report" / "flowpaths_animation"),
                    "build_id": bid})
        return True

    def _video_modal(res: dict):
        p = Path(res["path"])

        def _serve(request):
            from starlette.responses import FileResponse

            mt = {"mp4": "video/mp4", "webm": "video/webm",
                  "png": "image/png"}.get(res["format"], "image/webp")
            return FileResponse(p, media_type=mt,
                                headers={"Cache-Control": "no-store"})

        url = session.dynamic_route("fp_video", _serve)
        is_still = res["format"] == "png"
        if res["format"] in ("mp4", "webm"):
            body = ui.tags.video(src=url, controls=True, autoplay=True, loop=True,
                                 muted=True, class_="hype-capture-media")
            if res["format"] == "mp4":
                note = (f"{res['frames']} frames at {res['fps']} fps, MP4 (H.264)."
                        if res.get("frames") else
                        f"MP4 (H.264) at {res['fps']} fps." if res.get("fps") else
                        "MP4 (H.264).")
            else:
                note = "WebM video. Install imageio-ffmpeg for MP4 output."
        elif is_still:
            body = ui.tags.img(src=url, class_="hype-capture-media")
            kind = res.get("kind")
            note = ("PNG of the whole application window."
                    if kind == "app" else
                    "PNG of the 3D view." if kind == "view3d" else
                    "PNG of the selected area at twice the screen resolution."
                    if kind == "rect" else
                    "PNG of the current map view at twice the screen resolution.")
            if res.get("copied"):
                note += " Copied to the clipboard."
        else:
            body = ui.tags.img(src=url, class_="hype-capture-media")
            note = ("No MP4 encoder was available, so this is an animated WebP. "
                    "Install imageio-ffmpeg to get MP4 output.")
        footer = [ui.download_button("dl_fp_video",
                                     "Save image" if is_still else "Save video")]
        if is_still:
            # Clipboard writes need a client gesture, so the copy runs in
            # export_menu.js (delegated on this class) from the served PNG.
            footer.insert(0, ui.tags.button("Copy image", type="button",
                                            class_="btn btn-default hype-copy-still",
                                            **{"data-url": url}))
        if is_still:
            title = ("Window capture" if res.get("kind") == "app" else
                     "Area capture" if res.get("kind") == "rect" else "View capture")
        else:
            title = ("3D view recording" if res.get("kind") == "video3d"
                     else "Pathline animation")
        return ui.modal(
            body, ui.div(note, class_="hype-instr"),
            title=title,
            footer=ui.TagList(*footer, ui.modal_button("Close")),
            size="l", easy_close=True)

    @reactive.effect
    def _video_done():
        if video_task.status() in ("initial", "running", "cancelled"):
            return
        if not _task_armed["video"]:
            return
        _task_armed["video"] = False
        try:
            out = video_task.result()
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Video build failed: {e}", type="error", duration=8)
            return
        with reactive.isolate():
            if out.get("build_id") != _video_build_id():
                return          # superseded build lands nothing
        if (out.get("res") or {}).get("cancelled"):
            ui.notification_show("Video build canceled.", duration=4)
            return
        _video_result.set(out["res"])
        ui.modal_show(_video_modal(out["res"]))

    @render.download(filename=lambda: Path((_video_result() or {}).get("path", "video")).name)
    def dl_fp_video():
        res = _video_result()
        if res and Path(res["path"]).exists():
            yield Path(res["path"]).read_bytes()

    @render.ui
    def fp_video_status():
        if video_task.status() != "running":
            return None
        _ = video_tick()          # the only reactive dependency; the rest is the plain dict
        stage = _video_prog["stage"]
        i, n = _video_prog["i"], _video_prog["n"]
        t0 = _video_prog["t0"]
        if stage == "frames" and n:
            pct = int(round(100.0 * i / n))
            line = f"Rendering frame {i} of {n}"
            elapsed = max(time.monotonic() - t0, 0.001)
            if i >= 10:
                remaining = int(round((n - i) * elapsed / i))
                line += f", about {remaining} s left"
        elif stage == "basemap":
            pct, line = 2, "Fetching the basemap from the USGS service"
        elif stage == "scene":
            pct, line = 5, "Drawing the map layers"
        else:
            pct, line = 0, "Starting the video build"
        return ui.div(
            ui.div("Saving pathline animation", class_="hype-video-notifier-title"),
            ui.div(ui.div(class_="hype-spinner"), ui.span(line), class_="hype-busy"),
            ui.div(ui.div(class_="hype-prog-bar", style=f"width:{pct}%;"),
                   class_="hype-prog"),
            ui.input_action_button("fp_video_cancel", "Cancel", class_="hype-edit-btn"),
            class_="hype-video-notifier",
        )

    # ---- 3-D view recording (frame-stepped capture + server MP4 assembly; the live
    # MediaRecorder path stays as the fallback whose webm plays without ffmpeg) ----
    _video3d_pending: dict = {}
    _ffmpeg_probe: dict = {}

    def _ffmpeg_available() -> bool:
        if "ok" not in _ffmpeg_probe:
            _ffmpeg_probe["ok"] = bool(video_mod.resolve_ffmpeg(lambda *_: None))
        return _ffmpeg_probe["ok"]

    async def _launch_3d_record(crop=None, secs: float = 8.0, fps: int = 30):
        # Frames mode steps the animation on an ideal clock and the server
        # assembles a constant-rate MP4 (the 2D builder's smoothness, in 3D).
        mode = "frames" if _ffmpeg_available() else "recorder"
        _video3d_pending.update(fps=int(fps), frames=int(round(secs * fps)))
        msg = {"seconds": secs, "fps": fps, "input_id": "fp3d_webm", "mode": mode}
        if isinstance(crop, dict):
            msg["crop"] = crop      # canvas-pixel rect from the rubber band
        await session.send_custom_message("hype3d_record", msg)
        if mode == "frames":
            ui.notification_show("Building the 3D video, one frame at a time…",
                                 duration=5)
        else:
            ui.notification_show(f"Recording the 3D view for {secs:.0f} seconds…",
                                 duration=secs + 2)

    @reactive.extended_task
    async def video3d_task(payload: dict) -> dict:
        def _work():
            if payload.get("mjpeg"):
                res = video_mod.assemble_mjpeg_to_mp4(
                    payload["mjpeg"], payload["out"], payload.get("fps") or 30,
                    frames=payload.get("frames"), log=lambda m: None)
                if res.get("format") == "mp4":
                    try:                    # the frame dump is tens of MB, drop it
                        Path(payload["mjpeg"]).unlink()
                    except OSError:
                        pass
            else:
                res = video_mod.transcode_webm_to_mp4(
                    payload["webm"], payload["out"], log=lambda m: None,
                    fps=payload.get("fps"))
            return {"res": res}
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    @reactive.event(input.fp3d_webm)
    def _fp3d_uploaded():
        files = input.fp3d_webm() or []
        if not files:
            return
        src = Path(files[0]["datapath"])
        name = str(files[0].get("name") or "")
        out = work_dir / "report"
        out.mkdir(parents=True, exist_ok=True)
        pend = dict(_video3d_pending)
        _task_armed["video3d"] = True
        if name.lower().endswith(".mjpeg"):
            dst = out / "view3d.mjpeg"
            shutil.copy2(src, dst)
            video3d_task({"mjpeg": str(dst), "out": str(out / "view3d"),
                          "fps": pend.get("fps"), "frames": pend.get("frames")})
        else:
            dst = out / "view3d.webm"
            shutil.copy2(src, dst)
            video3d_task({"webm": str(dst), "out": str(out / "view3d"),
                          "fps": pend.get("fps")})

    @reactive.effect
    def _video3d_done():
        if video3d_task.status() in ("initial", "running", "cancelled"):
            return
        if not _task_armed["video3d"]:
            return
        _task_armed["video3d"] = False
        try:
            out = video3d_task.result()
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"3D video transcode failed: {e}", type="error", duration=8)
            return
        res = dict(out["res"])
        res.setdefault("frames", "")
        res.setdefault("fps", "")
        res["kind"] = "video3d"
        if res["format"] == "mjpeg":
            # frames mode with a failed assembly: the raw frame dump is unplayable
            ui.notification_show("The 3D video could not be assembled into an MP4. "
                                 "Try recording again.", type="error", duration=8)
            return
        _video_result.set(res)
        if res["format"] == "webm":
            ui.notification_show("No MP4 encoder was available. The recording stayed webm.",
                                 type="warning", duration=8)
        ui.modal_show(_video_modal(res))

    @reactive.effect
    @reactive.event(input.hype3d_record_done)
    def _fp3d_record_done():
        d = input.hype3d_record_done() or {}
        if not d.get("ok"):
            ui.notification_show("3D recording did not produce a video"
                                 + (f" ({d.get('err')})" if d.get("err") else "."),
                                 type="error", duration=8)
        elif d.get("mode") == "download":
            ui.notification_show("The recording downloaded directly as .webm (upload path "
                                 "unavailable in this browser).", duration=8)

    # ---- Export menu (header): view capture dispatch + server-rendered 2D still ----
    # export_evt is a nonce event posted by www/export_menu.js. The client already
    # resolved the local branches (browser + 3D acts on the vtk canvas directly), so
    # what arrives here is: desktop full-window capture, the browser 2D still, and
    # the two video builds.
    @reactive.extended_task
    async def still_task(payload: dict) -> dict:
        def _work():
            res = video_mod.build_flowpath_still(payload["spec"], payload["out"],
                                                 log=lambda m: None)
            return {"res": res}
        return await anyio.to_thread.run_sync(_work)

    def _launch_still_build(b_override=None, w_override=None) -> bool:
        # Unlike the video, a still must work on ANY project state: no flow paths,
        # nothing visible, animation off are all fine (the builder draws nothing).
        # b_override and w_override carry a Specified Window rubber-band rect.
        b = b_override or (input.map_bounds() if "map_bounds" in input else None)
        if not b or not all(k in (b or {}) for k in ("west", "south", "east", "north")):
            ui.notification_show("Move the map once so the view is known, then export.",
                                 type="warning")
            return False
        hz = hz_view() or {}
        visible = [cls for cls in HZ_CLASSES
                   if _eff_checked(f"gw.res.paths.{ui_tree.HZ_CLASS_SUFFIX[cls]}")]
        with reactive.isolate():
            anim = {"on": bool(fp_anim_on_v()), "speed": float(fp_anim_speed_v()),
                    "style": fp_anim_style_v(), "color": fp_anim_color_v(),
                    "mode": fp_anim_mode_v()}
            # The line dict rides for the rainbow modes: an elapsed gradient re-draws
            # over the scene's baked (total) colors, and a line-only rainbow still
            # gets its legend.
            line = {"show": bool(fp_line_show_v()), "weight": float(fp_line_weight_v()),
                    "opacity": float(fp_line_opacity_v()), "mode": fp_line_mode_v()}
        try:
            width_px = int(w_override or b.get("w") or 1280)
        except Exception:  # noqa: BLE001
            width_px = 1280
        basemap = ("imagery" if _eff_checked("base.imagery")
                   else "topo" if _eff_checked("base.topo") else "none")
        spec = {
            "hz_dir": hz.get("hz_dir") or "",
            "bounds4326": {k: float(b[k]) for k in ("west", "south", "east", "north")},
            "basemap": basemap,
            "visible_classes": visible,
            "anim": anim,
            "line": line,
            "class_colors": dict(HZ_COLORS),
            "scene": _gather_map_scene(),
            "width_px": width_px,
            "scale": 2,        # supersample: crisper than any screen grab
            "kind": "rect" if b_override else "view",
        }
        _task_armed["still"] = True
        still_task({"spec": spec, "out": str(work_dir / "report" / "map_view")})
        return True

    @reactive.effect
    def _still_done():
        if still_task.status() in ("initial", "running", "cancelled"):
            return
        if not _task_armed.get("still"):
            return
        _task_armed["still"] = False
        try:
            out = still_task.result()
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Image export failed: {e}", type="error", duration=8)
            return
        _video_result.set(out["res"])
        ui.modal_show(_video_modal(out["res"]))

    @reactive.effect
    @reactive.event(input.export_evt)
    async def _export_evt():
        # Nonce event input (setInputValue only), so NO ignore_init: it would eat
        # the first menu click of the session. export_menu.js already resolved the
        # local branches (browser 3D acts on the vtk canvas directly), so what
        # arrives here is: the whole-window shell capture, the server-rendered view
        # still, and the two video builds.
        d = input.export_evt() or {}
        act = d.get("a")
        # Optional Specified Window rubber-band rect: b = 4326 bounds of the dragged
        # area, w = its CSS pixel width (the render width). crop = canvas pixels for
        # the 3D recorder. Absent for Full View Extent captures.
        b_ov = d.get("b") if isinstance(d.get("b"), dict) else None
        w_ov = d.get("w")
        if act == "still_view":
            if _launch_still_build(b_ov, w_ov):
                ui.notification_show("Rendering the view…", duration=4)
        elif act == "save_anim":
            secs, fps = _fp_video_settings(d)
            _launch_video_build(b_ov, w_ov, secs, fps)
        elif act == "record_3d":
            secs, fps = _fp_video_settings(d)
            await _launch_3d_record(d.get("crop"), secs, fps)

    def _show_capture_preview(src: Path, kind: str, copied: bool = True):
        # Snipping-tool flow: every camera capture lands in the preview modal with
        # Copy image and Save image. The capture is copied into the project's report
        # folder so dl_fp_video and the dynamic route serve a stable file.
        dst = work_dir / "report" / "window_capture.png"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        res = {"path": str(dst), "format": "png", "kind": kind, "copied": copied}
        _video_result.set(res)
        ui.modal_show(_video_modal(res))

    @reactive.effect
    @reactive.event(input.capture_png)
    def _capture_png_uploaded():
        # Browser 3D still: the canvas PNG arrives through the chunked uploader
        # (same reasoning as fp3d_webm). export_menu.js tried the clipboard while
        # the click gesture was live and encoded the outcome in the file name.
        files = input.capture_png() or []
        if files:
            copied = "-nocopy" not in str(files[0].get("name") or "")
            _show_capture_preview(Path(files[0]["datapath"]), "view3d", copied)

    @reactive.effect
    @reactive.event(input.desktop_capture)
    def _desktop_capture_done():
        # Outcome relay from the shell (via desktop_bridge.js), plus the client-side
        # timeout export_menu.js posts when a pre-capture shell never answers.
        d = input.desktop_capture() or {}
        if d.get("ok") and d.get("path"):
            src = Path(str(d["path"]))
            if src.exists():
                _show_capture_preview(src, "app")
                try:
                    src.unlink()
                except OSError:
                    pass       # temp file cleanup only, never worth surfacing
            else:
                ui.notification_show("The shell capture file was not found.",
                                     type="error", duration=8)
        elif d.get("reason") == "timeout":
            ui.notification_show("The desktop shell did not respond. Update the desktop "
                                 "app to enable view capture.", type="warning", duration=8)
        elif not d.get("ok"):
            ui.notification_show("View capture failed"
                                 + (f" ({d.get('err')})" if d.get("err") else "."),
                                 type="error", duration=8)

    @reactive.effect
    @reactive.event(input.export_client_note)
    def _export_client_note():
        # Small outcomes of the client-local 3D capture branch, surfaced through the
        # app's own notification styling instead of an alien toast.
        d = input.export_client_note() or {}
        if d.get("msg"):
            ui.notification_show(str(d["msg"]),
                                 type="warning" if d.get("warn") else "message",
                                 duration=5)

    # ---- raster hover probe (value chip rendered client-side by www/raster_probe.js) ----
    # While a raster-valued tree node is selected AND effectively visible, ship the SAME
    # EPSG:4326 grid its overlay was colored from as raw float32 over HTTP (dynamic_route;
    # a multi-MB grid on the websocket would court the 1008 concurrent-drain kill), then a
    # tiny hype_probe settings message. The client samples on mousemove: zero hover lag,
    # and the chip always agrees with the displayed colors.
    _probe_cache: dict = {}   # (path, mtime, max_dim) -> results.probe_grid payload
    _probe_cur: dict = {}     # bytes the dynamic-route handler serves right now
    _probe_sig: dict = {}     # last-pushed signature + tree-ready generation

    def _probe_resolve(nid):
        """(source path, chip label, warp max_dim) for a probe-able node, else None.
        Reads the data reactives so _probe_push re-fires when sources appear/change;
        max_dim must match the overlay's own warp so values register with the pixels."""
        if nid in ("terrain", "terrain.dem"):
            # The Terrain GROUP is the final terrain (carved DEM when a channel mod is
            # applied) — it shares _pane_dem via PANE_FOR_NODE, so it probes the same.
            p = active_dem()
            return (p, "Terrain", 1024) if p else None
        if nid == "terrain.chanmod":
            p = (carve_meta() or {}).get("diff_path")
            return (p, "Channel cut", 1024) if p else None
        if nid == "sw.wse":
            p = (ras_result() or {}).get("wse_tif")
            if p and Path(p).exists():
                return (p, "Water surface elevation", 1400)   # ras_results.result_overlay
            cropped = work_dir / "model" / "cropped_water_surface_raster.tif"
            p = str(cropped) if cropped.exists() else _wse_used.get("path")
            return (p, "Water surface elevation", 1024) if p else None
        if nid == "sw.depth":
            p = (ras_result() or {}).get("depth_tif")
            return (p, "Water depth", 1400) if p else None
        if nid == "gw.res.head":
            tifs = head_tifs()
            if not tifs:
                return None
            k = min(max(int(head_layer_v()), 1), len(tifs))
            return (tifs[k - 1], f"Hydraulic head (layer {k})", 1024)
        return None

    def _serve_probe(_req):
        from starlette.responses import Response
        return Response(content=_probe_cur.get("bytes", b""),
                        media_type="application/octet-stream",
                        headers={"Cache-Control": "no-store"})

    async def _send_probe(payload, label=""):
        if payload is None:
            await session.send_custom_message("hype_probe", {"on": False})
            return
        _probe_cur["bytes"] = payload["bytes"]
        url = session.dynamic_route("probe_grid", _serve_probe)  # fresh-nonced URL per push
        await session.send_custom_message("hype_probe", {
            "on": True, "url": url, "w": payload["w"], "h": payload["h"],
            "bounds": payload["bounds"], "label": label, "units": "m", "decimals": 2})

    @reactive.effect
    async def _probe_push():
        nid = sel_node()
        _vis_state()                       # re-run on any checkbox flip
        ready = _tree_ready()              # re-push current state on (re)connect
        row = _probe_resolve(nid) if nid else None
        path = label = None
        max_dim = 0
        if row is not None:
            path, label, max_dim = row
            # The group selection ghosts to its DEM child for the visibility gate: no
            # chip while the DEM overlay itself is hidden (child gate ANDs the group).
            vis_nid = "terrain.dem" if nid == "terrain" else nid
            if not (path and Path(path).exists() and _eff_checked(vis_nid)):
                row = None
        sig = None
        if row is not None:
            try:
                mtime = Path(path).stat().st_mtime
            except OSError:                # deleted between exists() and stat()
                row = None
            else:
                # No nid in the signature: Terrain vs DEM select the same source, so
                # flipping between them skips a redundant re-push/refetch.
                sig = (str(path), mtime, max_dim, label)
        if sig == _probe_sig.get("sig") and ready == _probe_sig.get("ready"):
            return                          # no state change, no reconnect: stay quiet
        _probe_sig["sig"] = sig
        _probe_sig["ready"] = ready
        if sig is None:
            await _send_probe(None)
            return
        key = (str(path), sig[2], max_dim)
        payload = _probe_cache.get(key)
        if payload is None:
            try:
                payload = results.probe_grid(path, max_dim=max_dim)
            except Exception as e:  # noqa: BLE001 — unreadable raster: no chip, no crash
                print(f"[probe] grid build failed for {path}: {e}")
                await _send_probe(None)
                return
            if len(_probe_cache) > 5:       # a few grids at 1-8 MB each is plenty
                _probe_cache.pop(next(iter(_probe_cache)))
            _probe_cache[key] = payload
        await _send_probe(payload, label)

    # ---- classed flow-path selection (Results step): click one, or box-select several ----

    @reactive.effect
    @reactive.event(input.fp_select_box)
    def _fp_box_select():
        # www/flowpath_select.js posts {west, south, east, north} (EPSG:4326) on box release.
        # Flow paths are the classed display now, so the box always routes to hz_gdf.
        from shapely.geometry import box as _box
        gdf = hz_gdf()
        if gdf is None:
            return
        # A box drag ends on the map, so Leaflet fires a plain map click right after
        # this event — stamp the pick so the mapclear deselect skips it and the
        # properties pane stays open (same contract as _on_hz_path_click).
        _map_ui["map_sel_ts"] = time.monotonic()
        b = input.fp_select_box() or {}
        try:
            bx = _box(float(b["west"]), float(b["south"]), float(b["east"]), float(b["north"]))
        except (KeyError, TypeError, ValueError):
            return
        # Selection only offers what the user can see: classes whose checkbox is
        # effectively off are excluded (single click already can't hit them — the
        # hidden GeoJSON is off the map entirely).
        vis = [c for c in HZ_CLASSES
               if _eff_checked(f"gw.res.paths.{ui_tree.HZ_CLASS_SUFFIX[c]}")]
        sub = gdf[gdf["hz_class"].isin(vis)]
        sub = sub[sub.intersects(bx)]       # crossing window: anything the box touches
        hz_sel_pids.set(tuple(int(p) for p in sub["particleid"]))

    @reactive.effect
    @reactive.event(input.xsect_line)
    def _on_xsect_line():
        # www/xsection.js posts {latlngs: [{lat, lng}, ...], nonce} when a section line is
        # finished. Sample the ORIGINAL DEM (and the carved terrain, if a channel modification
        # is applied) along the line and pop the profile plot in a modal.
        evt = input.xsect_line() or {}
        lls = evt.get("latlngs") or []
        dem_p = dem_path()
        if dem_p is None or len(lls) < 2:
            return
        import base64
        import io

        import matplotlib
        matplotlib.use("Agg", force=True)
        import matplotlib.pyplot as plt
        import numpy as np
        import rasterio
        from pyproj import Transformer

        def _sample(path, px, py):
            with rasterio.open(path) as src:
                z = np.array([v[0] for v in src.sample(np.column_stack([px, py]))],
                             dtype="float64")
                if src.nodata is not None:
                    z[np.isclose(z, float(src.nodata))] = np.nan
            z[np.abs(z) > 1e10] = np.nan
            return z

        try:
            lats = np.array([float(q["lat"]) for q in lls], dtype="float64")
            lons = np.array([float(q["lng"]) for q in lls], dtype="float64")
            with rasterio.open(dem_p) as src:
                tr = Transformer.from_crs("EPSG:4326", src.crs, always_xy=True)
                res_m = float(abs(src.transform.a))
            xs, ys = (np.asarray(v, dtype="float64") for v in tr.transform(lons, lats))
            # drop consecutive duplicate vertices (leaflet's dblclick repeats the last click)
            keep = np.r_[True, (np.abs(np.diff(xs)) + np.abs(np.diff(ys))) > 1e-9]
            xs, ys = xs[keep], ys[keep]
            if xs.size < 2:
                return
            seg = np.hypot(np.diff(xs), np.diff(ys))
            total = float(seg.sum())
            if total <= 0:
                return
            vert_d = np.r_[0.0, np.cumsum(seg)]           # vertex distances (segment breaks)
            step = max(res_m / 2.0, total / 2000.0)
            d = np.arange(0.0, total + step / 2.0, step)
            px = np.interp(d, vert_d, xs)
            py = np.interp(d, vert_d, ys)
            z_dem = _sample(dem_p, px, py)
            if not np.isfinite(z_dem).any():
                ui.notification_show("The section line is outside the terrain raster.",
                                     type="warning", duration=6)
                return
            z_mod = None
            if carve_active():
                cp = (carve_meta() or {}).get("path")     # same grid/CRS as the DEM (carve.py)
                if cp and Path(cp).exists():
                    z_mod = _sample(cp, px, py)
                    if not np.isfinite(z_mod).any():
                        z_mod = None

            fig, ax = plt.subplots(figsize=(7.6, 3.1))
            ax.plot(d, z_dem, color="#2b7bff", lw=1.6, label="Terrain (DEM)")
            if z_mod is not None:
                ax.plot(d, z_mod, color="#e8590c", lw=1.4, ls="--", label="Modified terrain")
                both = np.isfinite(z_dem) & np.isfinite(z_mod)
                ax.fill_between(d[both], z_mod[both], z_dem[both],
                                where=(z_dem[both] - z_mod[both]) > 1e-6,
                                color="#e8590c", alpha=0.18, linewidth=0)
            for vd in vert_d[1:-1]:                       # tick the interior vertices
                ax.axvline(float(vd), color="#94a3b8", lw=0.7, ls=":")
            ax.set_xlabel("Distance along section (m)", fontsize=9)
            ax.set_ylabel("Elevation (m)", fontsize=9)
            ax.tick_params(labelsize=8)
            ax.grid(color="#e2e8f0", lw=0.5)
            for side in ("top", "right"):
                ax.spines[side].set_visible(False)
            ax.legend(fontsize=8, frameon=False, loc="best")
            buf = io.BytesIO()
            fig.savefig(buf, format="png", dpi=135, bbox_inches="tight")
            plt.close(fig)
            uri = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")

            cap = (f"Length {total:,.0f} m · terrain {np.nanmin(z_dem):.2f}–"
                   f"{np.nanmax(z_dem):.2f} m")
            if z_mod is not None:
                cut = float(np.nanmax(z_dem - z_mod))
                if np.isfinite(cut) and cut > 0.005:
                    cap += f" · max modification depth {cut:.2f} m"
            ui.modal_show(ui.modal(
                ui.img(src=uri, style="width:100%;height:auto;"),
                ui.div(cap, class_="hype-instr", style="margin-top:6px;"),
                title="Terrain cross-section", size="l", easy_close=True, footer=None))
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Cross-section failed: {e}", type="error", duration=8)

    @reactive.effect
    def _fp_clear():
        if not _clicked_dynamic("fp_clear"):
            return
        hz_sel_pids.set(())

    @reactive.effect
    def _cancel_run():
        if not _clicked_dynamic("cancel_run"):
            return
        _terminate_child()
        log_lines.append("[run cancelled by user]")
        log_tick.set(len(log_lines))
        try:
            run_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        stage.set("")
        _select("gw.mesh")
        ui.notification_show("Run cancelled.", type="warning", duration=4)

    # ---- layer tree (left panel): server state → hype_tree push; tree_event → dispatch ----
    _tree_ready = reactive.value(0)        # ++ on client (re)connect → re-push the full tree
    _vis_state = reactive.value(0)         # ++ on any visibility change → re-push checkboxes

    def _bump_vis():
        with reactive.isolate():
            _vis_state.set(_vis_state() + 1)

    # Raw checkbox intent per check-node (the user's ticks; missing = default). EFFECTIVE
    # visibility of a node's layers = its own box AND every checkbox ancestor's box — group
    # toggles (Terrain / Water surface / Groundwater / Basemaps) override their descendants
    # without erasing the children's own state.
    _check_state: dict = {}
    _CHECK_DEFAULTS = {"base.imagery": False,     # topo is the startup basemap
                       # base.hydro (NHD flowline vectors) defaults ON — absent key = True
                       # Results defaults (revision §8.1): after delineation only the HYPORHEIC
                       # paths + volume show; losing/gaining/throughflow are opt-in.
                       "gw.res.paths.los": False, "gw.res.paths.gain": False,
                       "gw.res.paths.thru": False,
                       "gw.res.hz.los": False, "gw.res.hz.gain": False,
                       "gw.res.hz.thru": False}

    def _node_checked(nid) -> bool:
        return bool(_check_state.get(nid, _CHECK_DEFAULTS.get(nid, True)))

    def _eff_checked(nid) -> bool:
        return _node_checked(nid) and all(_node_checked(a)
                                          for a in ui_tree.check_ancestors(nid))

    def _set_keys_visible(keys, on):
        for k in keys:
            if on:
                _hidden_keys.discard(k)
            else:
                _hidden_keys.add(k)
            # Decor layers (reach, boundary lines, domain, wetted extent): visibility is DATA-DRIVEN
            # — the `visible` trait was not clearing them at runtime (toggle-off left the line shown),
            # but `.data` changes apply reliably. Re-render via _decor_show, which honors the updated
            # _hidden_keys (empty data = hidden, feature data = shown). Cached feature, no style needed.
            if k in _decor_feat and isinstance(_layers.get(k), GeoJSON):
                _decor_show(k, _decor_feat.get(k), None)
                continue
            obj = _layers.get(k)
            # GeoJSON MUST dodge the LayerGroup branches: ipyleaflet's GeoJSON IS-A FeatureGroup
            # IS-A LayerGroup, but it draws from `.data` and its `.layers` tuple is EMPTY — the
            # group-children hide silently no-ops on it, leaving the layer stuck visible (THE
            # untoggleable-flow-paths root cause, proven live 2026-07-09). Vectors park instead.
            if on:
                if isinstance(obj, LayerGroup) and not isinstance(obj, GeoJSON):
                    _group_children_visible(obj, True)     # true groups: show children in place
                    continue
                if obj is not None and hasattr(obj, "visible"):
                    try:
                        obj.visible = True
                        continue
                    except Exception:  # noqa: BLE001
                        pass
                obj = _layer_shadow.pop(k, None)
                if obj is not None and _layers.get(k) is None:
                    _set_layer(k, _clone_vector(obj))   # fresh model id — see _clone_vector
                    if k in _decor_feat:
                        # Re-push the cached geometry through the decor channel: a widget
                        # parked while hidden may carry empty data (any past creator), and
                        # the clone above copies data verbatim. No-op when already correct.
                        _decor_show(k, _decor_feat.get(k), None)
            else:
                if obj is None:
                    continue
                if isinstance(obj, LayerGroup) and not isinstance(obj, GeoJSON):
                    _group_children_visible(obj, False)   # true groups: hide CHILDREN — NEVER
                    continue                              # _MAP.remove a group (rebuilds siblings)
                if _visible_hide_works(obj):   # raster/marker: trait toggle, zero widget churn
                    try:
                        obj.visible = False
                        continue
                    except Exception:  # noqa: BLE001
                        pass
                _layer_shadow[k] = obj         # GeoJSON (visible=False is a no-op) / no-trait: park it
                try:
                    _MAP.remove(obj)
                except Exception:  # noqa: BLE001
                    pass
                _layers[k] = None

    def _apply_check_effective(nid):
        """Re-apply EFFECTIVE visibility to nid + its checkbox descendants (2-D layer keys
        and basemap tile traits). Returns the touched node ids so async callers can push the
        matching hype3d_vis messages (sync callers skip 3-D; the next layer push re-syncs)."""
        out = []
        for mid in ui_tree.check_subtree(nid):
            on = _eff_checked(mid)
            if mid.startswith("base."):
                lyr = _base_layers.get(mid.split(".", 1)[1])
                if lyr is not None:
                    try:
                        lyr.visible = on
                    except Exception:  # noqa: BLE001
                        pass
            # base nodes fall through to the layers path too: base.hydro drives the
            # "NHD streams" vector — hiding routes it into _hidden_keys, so each
            # reach-step viewport refetch parks instead of re-adding (sticky OFF)
            _set_keys_visible(ui_tree.NODE_LAYERS.get(mid, ()), on)
            out.append(mid)
        _bump_vis()
        return out

    def _reapply_all_vis():
        """Idempotent re-assert of EVERY checkbox-driven layer key (cheap: set discards and
        same-value trait writes don't sync). Safety net against client/server vis drift —
        called when returning from the 3-D view."""
        for nid in ui_tree.NODE_LAYERS:
            on = _eff_checked(nid)
            if nid.startswith("base."):
                lyr = _base_layers.get(nid.split(".", 1)[1])
                if lyr is not None:
                    try:
                        lyr.visible = on
                    except Exception:  # noqa: BLE001
                        pass
            _set_keys_visible(ui_tree.NODE_LAYERS.get(nid, ()), on)
        _apply_ml_vis()                    # dynamic Map-layers keys sit outside NODE_LAYERS
        _bump_vis()

    def _hide_node_layers(nid):
        _check_state[nid] = False
        _apply_check_effective(nid)

    def _unhide_node_layers(nid):
        # Force-show (editing/creation paths): tick the node AND any unticked ancestor group,
        # then re-apply from the topmost change so siblings hidden only by the parent return.
        _check_state[nid] = True
        top = nid
        for a in ui_tree.check_ancestors(nid):
            if not _node_checked(a):
                _check_state[a] = True
                top = a
        _apply_check_effective(top)

    def _feat_bounds(feat):
        try:
            from shapely.geometry import shape as _shape
            g = _shape((feat or {}).get("geometry") or {})
            if g.is_empty:
                return None
            minx, miny, maxx, maxy = g.bounds
            return [[miny, minx], [maxy, maxx]]
        except Exception:  # noqa: BLE001
            return None

    def _node_bounds(nid):
        """[[south, west], [north, east]] extent for a tree node (None = nothing to fly to).
        All reads isolated — callers are event handlers, not subscribers."""
        with reactive.isolate():
            if nid == "reach":
                return _feat_bounds(reach_feat())
            if nid in ("terrain", "terrain.dem", "terrain.chanmod"):
                return _map_ui.get("dem_bounds") or _feat_bounds(reach_feat())
            if nid == "maplyr":
                return _ml_bounds_union()
            if nid.startswith("ml:"):
                return (_ml_cache.get(nid[3:]) or {}).get("bounds")
            if nid in ui_tree.NODE_SLOT and nid != "sw.wetted":
                sv = _slot_value(ui_tree.NODE_SLOT[nid])
                return _feat_bounds(sv() if sv else None) or _feat_bounds(domain_feat())
            if nid == "sw.wetted":
                return (_feat_bounds(wse_extent_feat())
                        or _feat_bounds((ras_result() or {}).get("extent_feat"))
                        or _feat_bounds(domain_feat()))
            if nid.startswith("gw.res"):
                res = run_result()
                if res is not None:
                    try:
                        b = results.bounds_latlon(res)
                        if b:
                            return b
                    except Exception:  # noqa: BLE001
                        pass
                return _feat_bounds(domain_feat())
            if nid.startswith(("bnd", "sw", "gw")):
                return _feat_bounds(domain_feat())
        return None

    # ---- 3-D scene (vtk.js canvas): shared frame + layer pushes ----
    _scene: dict = {}                      # origin [X,Y], z0, crs — anchored once per DEM

    def _scene_crs():
        crs = proj_crs()
        if crs is not None:
            return crs
        rf = reach_feat()                  # pre-boundaries: same UTM zone via the reach
        if rf is None:
            return None
        try:
            return geometry.single_feature_gdf(rf).estimate_utm_crs()
        except Exception:  # noqa: BLE001
            return None

    def _scene_frame():
        """(origin_xy, z0) of the 3-D scene — anchored ONCE per DEM (its SW corner + min
        elevation − 2 m) so later layers can't shift the frame under earlier ones."""
        if _scene.get("origin") is not None:
            return _scene["origin"], _scene["z0"]
        p = dem_path()
        if p is None:
            return None, None
        crs = _scene_crs()
        if crs is None:
            return None, None
        try:
            import rasterio
            from rasterio.warp import transform_bounds
            with rasterio.open(p) as src:
                b = transform_bounds(src.crs, crs, *src.bounds)
            s = dem.dem_summary(p)
        except Exception:  # noqa: BLE001
            return None, None
        _scene["origin"] = [float(b[0]), float(b[1])]
        _scene["z0"] = float(s["min"]) - 2.0
        _scene["crs"] = crs
        return _scene["origin"], _scene["z0"]

    async def _send_3d(payload):
        """Push one hype3d_layer payload + its current checkbox visibility."""
        if payload is None:
            return
        await session.send_custom_message("hype3d_layer", payload)
        nid = next((n for n, k in ui_tree.NODE_3D.items() if k == payload["key"]), None)
        if nid is not None:
            await session.send_custom_message(
                "hype3d_vis", {"key": payload["key"], "on": _eff_checked(nid)})

    @reactive.extended_task
    async def scene_terrain_task(dem_p: str, crs_wkt: str, origin: tuple, z0: float,
                                 domain_f: dict | None = None) -> dict:
        def _work():
            p = dem_p
            if domain_f is not None:
                # Clip the 3-D terrain surface to the model domain: nodata outside the
                # polygon renders as skipped quads client-side, so the surface ends at the
                # boundary instead of overhanging the whole DEM rectangle. Soft-fail to the
                # full DEM on any raster hiccup (the 2026-07-16 crash rule).
                try:
                    out = work_dir / "scene" / "terrain_domain.tif"
                    p = dem.clip_dem_to_polygon(dem_p, geometry.single_feature_gdf(domain_f),
                                                str(out))
                except Exception as e:  # noqa: BLE001
                    print(f"[scene] terrain clip failed (using the full DEM): {e}")
                    p = dem_p
            return scene.terrain_payload(str(p), crs_wkt, tuple(origin), float(z0))
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    def _push_terrain_3d():
        p = active_dem()                   # carved terrain re-pushes the 3-D surface too
        if p is None or not _HAS_MAP:
            return
        origin, z0 = _scene_frame()
        if origin is None or scene_terrain_task.status() == "running":
            return
        dom = domain_feat()                # domain exists -> clipped surface; regen re-clips
        sig = (p, origin[0], origin[1], id(dom) if dom else None)
        if _scene.get("terrain_sig") == sig:
            return
        _scene["terrain_sig"] = sig
        crs = _scene["crs"]
        scene_terrain_task(p, crs.to_wkt() if hasattr(crs, "to_wkt") else str(crs),
                           tuple(origin), float(z0), dom)

    @reactive.effect
    async def _terrain_3d_done():
        if scene_terrain_task.status() in ("initial", "running"):
            return
        try:
            payload = scene_terrain_task.result()
        except Exception:  # noqa: BLE001
            return
        await _send_3d(payload)
        # A rebuilt terrain actor starts at full opacity: re-assert the DEM slider so
        # the see-through setting survives fetches and restores.
        with reactive.isolate():
            op = float(dem_opacity_v())
        if op < 0.999:
            await session.send_custom_message(
                "hype3d_style", {"key": "terrain", "opacity": op})

    def _task_state(t):
        try:
            return t.status()
        except Exception:  # noqa: BLE001
            return "initial"

    def _tree_statuses():
        """Per-node status icons (subscribing reads on features/results/task statuses — all
        discrete transitions, never per-tick progress, so pushes stay rare)."""
        st = {}
        if reach_feat() is not None:
            st["reach"] = "done"
        elif _task_state(snap_task) == "running" or _task_state(reach_task) == "running":
            st["reach"] = "running"
        ds = _task_state(dem_task)
        if ds == "running":
            st["terrain.dem"] = "running"
        elif dem_path() is not None:
            st["terrain.dem"] = "done"
        elif ds == "error":
            st["terrain.dem"] = "error"
        if _task_state(carve_task) == "running":
            st["terrain.chanmod"] = "running"
        elif carve_active():
            st["terrain.chanmod"] = "done"
        for nid, f in (("bnd.up", up_feat()), ("bnd.left", left_feat()),
                       ("bnd.right", right_feat()), ("bnd.down", down_feat())):
            if f is not None:
                st[nid] = "done"
        if _task_state(delineate_task) == "running":
            st["bnd"] = "running"
        elif _domain_build() is not None:
            st["bnd"] = "done"
        marks = _stale_marks()
        rs = _task_state(ras_task)
        if rs == "running":
            st["sw"] = "running"
        elif "sw" in marks:
            st["sw"] = "stale"
        elif ras_result() is not None:
            st["sw"] = "done"
        elif rs == "error":
            st["sw"] = "error"
        if _task_state(mesh_prev_task) == "running":
            st["sw.mesh"] = "running"
        elif ras_mesh_prev() is not None:
            st["sw.mesh"] = "done"
        wet = (((ras_result() or {}).get("extent_feat")) if wse_mode_v() == "model"
               else wse_extent_feat())
        if _task_state(wse_task) == "running":
            st["sw.wetted"] = "running"
        elif wet is not None:
            st["sw.wetted"] = "done"
        if _task_state(mesh_task) == "running":
            st["gw.mesh"] = "running"
        elif mesh_geom() is not None:
            st["gw.mesh"] = "done"
        runs = _task_state(run_task)
        if runs == "running":
            st["gw.run"] = "running"
        elif runs == "error":
            st["gw.run"] = "error"
        elif "gw" in marks and run_result() is not None:
            st["gw.run"] = "stale"
        elif run_result() is not None:
            st["gw.run"] = "done"
        if run_result() is not None:
            st["gw.res"] = "stale" if "gw" in marks else "done"
        hzs = _task_state(hz_task)
        if hzs == "running":
            st["gw.res.hz"] = "running"
        elif hzs == "error":
            st["gw.res.hz"] = "error"
        elif hz_result() is not None:
            st["gw.res.hz"] = "stale" if ({"gw", "hz"} & marks) else "done"
        ar = alt_result()
        if _task_state(alt_task) == "running" or (ar or {}).get("running"):
            st["gw.alt"] = "running"
        elif ar and ar.get("manifest"):
            amf = ar["manifest"]
            cur_hash = (input_snapshot() or {}).get("input_hash")
            if (ar.get("halted_on")
                    or not any(s.get("status") == "completed"
                               for s in amf.get("scenarios") or [])):
                st["gw.alt"] = "error"
            elif amf.get("base_input_hash") and cur_hash \
                    and amf["base_input_hash"] != cur_hash:
                st["gw.alt"] = "stale"
            else:
                st["gw.alt"] = "done"
        if _task_state(report_task) == "running":
            st["report"] = "running"
        elif report_paths() is not None:
            st["report"] = "stale" if ({"gw", "hz"} & marks) else "done"
        return st

    @reactive.effect
    async def _push_tree_state():
        _tree_ready()                          # re-push whenever the client (re)connects
        _vis_state()                           # …and whenever a checkbox/visibility changed
        statuses = _tree_statuses()
        checks = {nid: _node_checked(nid) for nid in ui_tree.NODE_LAYERS}
        reach = _reachable()
        disabled = {nid for nid, stp in ui_tree.NODE_STEP.items()
                    if stp is not None and stp not in reach}
        if dem_path() is None:                 # carving needs the terrain, not the boundaries
            disabled.add("terrain.chanmod")
        else:
            disabled.discard("terrain.chanmod")
        hidden = set()
        if not runmode.IS_DESKTOP:
            hidden.add("report.cmp")           # cloud has no filesystem access to other projects
            hidden.add("maplyr")               # reference layers link LOCAL files — desktop only
        if _task_state(run_task) == "initial" and run_result() is None:
            hidden.add("gw.run")               # the run row appears once a run first starts
        if run_result() is None:
            hidden.add("gw.res.hz")            # the Zone group appears after a GW run
            if _task_state(alt_task) == "initial" and alt_result() is None:
                hidden.add("gw.alt")           # alternatives surface once a run/manifest exists
        if hz_result() is None:                # Flow-paths/Volumes/Flows populate on delineation
            hidden.update(("gw.res.paths", "gw.res.hz.vols",    # children drop with their parent
                           "gw.res.hz.flows"))
            hidden.add("fn")                   # function screening reads the delineation's RTD
        dimmed = {nid for nid in ui_tree.NODE_LAYERS
                  if checks.get(nid) and not _eff_checked(nid)}
        extras = []
        if runmode.IS_DESKTOP:
            # Dynamic per-layer rows under the Map layers group (ids "ml:<uid>", NOT in
            # ui_tree.NODES — the dispatch below routes them before the static-id guards).
            group_on = _node_checked("maplyr")
            for rec in map_layers():           # subscribing read: add/remove re-pushes the tree
                uid = rec.get("id")
                vis = bool(rec.get("visible", True))
                extras.append({"id": _ml_key(uid), "label": rec.get("name") or "layer",
                               "parent": "maplyr", "depth": 1, "group": False,
                               "status": {"missing": "warn",
                                          "error": "error"}.get(_ml_status.get(uid), "none"),
                               "check": vis, "disabled": False,
                               "dim": vis and not group_on})
        payload = ui_tree.build_tree_payload(
            selected=sel_node(), statuses=statuses, checks=checks,
            disabled=disabled, hidden=hidden, dimmed=dimmed, extra_rows=extras)
        payload["view"] = view_mode_v()        # header 2D/3D buttons sync from this
        await session.send_custom_message("hype_tree", payload)

    @reactive.effect
    @reactive.event(input.tree_event)
    async def _tree_event_dispatch():
        evt = input.tree_event() or {}
        kind = evt.get("type")
        if kind == "ready":
            _tree_ready.set(_tree_ready() + 1)
        elif kind == "select":
            nid = evt.get("id")
            if isinstance(nid, str) and nid.startswith("ml:"):
                # Dynamic layer rows get their own per-layer pane; stale ids no-op.
                with reactive.isolate():
                    known = any(_ml_key(r.get("id")) == nid for r in map_layers())
                if known:
                    sel_src.set("tree")
                    sel_node.set(nid)
                return
            if nid in ui_tree.NODE:            # selection never moves the view — the props
                sel_src.set("tree")            # header's Zoom-to-extent button does that
                sel_node.set(nid)
        elif kind == "zoom":
            with reactive.isolate():
                nid = sel_node()
            b = _node_bounds(nid) if nid else None
            if b:
                await session.send_custom_message("hype_fly", {"bounds": b})
        elif kind == "deselect":
            sel_node.set(None)
        elif kind == "clearresults":       # header "Clear results": wipe this stage + downstream
            with reactive.isolate():
                nid = sel_node()
            if nid in ("bnd", "sw", "gw"):
                await _cascade_clear(nid, include_self=True)
        elif kind == "mapclear":
            # Empty-map click → deselect (clears the props context). Skipped when a map-driven
            # selection consumed the same click (mirror/boundary/path picks and flow-path box
            # selects stamp map_sel_ts),
            # and while an "add gradient point" or "add well" click is armed — that click IS
            # the placement (tree.js can't see this state, and near-line clicks land on tiles,
            # not the line; message order vs the widget interaction is not guaranteed).
            with reactive.isolate():
                if grad_adding() is not None or wells_adding():
                    return
            ts = _map_ui.get("map_sel_ts")
            if ts is not None and (time.monotonic() - float(ts)) < 0.8:
                return
            with reactive.isolate():
                cur = sel_node()
            if cur is not None:
                sel_node.set(None)
        elif kind == "view":
            v = evt.get("view")
            if v in ("2d", "3d"):
                if v == "3d" and alt_view() is not None:
                    # The 3-D scene is Basecase-only. Auto-return so what is on screen is
                    # always what the banner and the runs table say (user decision D6).
                    await _set_displayed_run(None, quiet=True)
                    ui.notification_show("3D is available for the Basecase only. Returned "
                                         "to Basecase.", duration=6)
                view_mode_v.set(v)
                if v == "2d":                  # back from 3-D: re-assert every layer vis and
                    _reapply_all_vis()         # sweep hidden hz ghosts (client-drift safety net)
                    await _sweep_hz([k for k in _hidden_keys
                                     if k.startswith("hz_") and _layers.get(k) is None])
        elif kind == "check":
            nid = evt.get("id")
            if isinstance(nid, str) and nid.startswith("ml:"):
                # Dynamic Map-layers row: intent lives on the RECORD (persisted), not in
                # _check_state; the ver bump drives autosave and the tree re-push.
                with reactive.isolate():
                    rec = next((r for r in map_layers() if _ml_key(r.get("id")) == nid), None)
                if rec is not None:
                    rec["visible"] = bool(evt.get("on"))
                    _set_keys_visible((nid,), _ml_eff(rec))
                    with reactive.isolate():
                        map_layers_ver.set(map_layers_ver() + 1)
                    _bump_vis()
                return
            if nid not in ui_tree.NODE:
                return
            on = bool(evt.get("on"))
            _check_state[nid] = on             # raw intent; ancestors stay authoritative
            affected = _apply_check_effective(nid)
            if nid == "maplyr":                # group toggle: the static walk finds no children
                _apply_ml_vis()
            if on and nid in ("base.imagery", "base.topo"):    # base maps act as a radio
                other = "base.topo" if nid == "base.imagery" else "base.imagery"
                _check_state[other] = False
                # fold the sibling's nodes into the 3-D vis sync below, or the unticked
                # basemap's drape key (basemap/basemap_topo) keeps its stale visibility.
                # Unticking the ACTIVE leaf is allowed (both off = no basemap, per user);
                # the client picker's ready-texture fallback keeps SWITCHES from blanking.
                affected = list(affected) + list(_apply_check_effective(other))
            for mid in affected:               # the same checkboxes drive the 3-D scene
                key3d = ui_tree.NODE_3D.get(mid)
                if key3d:
                    await session.send_custom_message(
                        "hype3d_vis", {"key": key3d, "on": _eff_checked(mid)})
            await _sweep_hz([k for mid in affected if not _eff_checked(mid)
                             for k in ui_tree.NODE_LAYERS.get(mid, ())
                             if k.startswith("hz_") and _layers.get(k) is None])
            # Hiding a flow-path class drops its paths from the current selection —
            # the highlight layer and info pane must never report invisible paths.
            path_nodes = {f"gw.res.paths.{s}" for s in ui_tree.HZ_CLASS_SUFFIX.values()}
            if path_nodes & set(affected):
                with reactive.isolate():
                    pids, gdf = hz_sel_pids(), hz_gdf()
                if pids and gdf is not None:
                    vis = [c for c in HZ_CLASSES
                           if _eff_checked(f"gw.res.paths.{ui_tree.HZ_CLASS_SUFFIX[c]}")]
                    sub = gdf[gdf["particleid"].isin(pids) & gdf["hz_class"].isin(vis)]
                    keep = tuple(int(p) for p in sub["particleid"])
                    if len(keep) != len(pids):
                        hz_sel_pids.set(keep)

    @output(suspend_when_hidden=False)     # the CARD is display:none until this output fills —
    @render.ui                             # default suspension would deadlock (hidden ⇒ never
    def propspane():                       # rendered ⇒ never shown)
        # Right properties panel: dispatch the selected node to its pane builder (defined
        # below with the PANE_FOR_NODE table). None (nothing selected / unknown node) leaves
        # the output empty — tree.js hides the whole card. Soft gating: unmet prerequisites
        # render a hint + jump chip instead of the controls.
        nid = sel_node()
        if not nid:
            # Nothing selected: first-run shows the Get-started card; once work exists an
            # empty output keeps the card hidden (tree.js follows the output's content).
            return _pane_welcome() if not _has_workspace() else None
        if isinstance(nid, str) and nid.startswith("ml:"):
            # Dynamic map-layer rows: a per-layer pane. The record + _ml_paint reads make
            # this output repaint on add/remove/status transitions, never on slider drags
            # (the mirror mutates the record in place). A stale id (layer removed, cloud
            # session) falls back to the group pane.
            uid = nid[3:]
            rec = next((r for r in map_layers() if r.get("id") == uid), None)
            if runmode.IS_DESKTOP and rec is not None:
                return _props_shell(str(rec.get("name") or "Layer"), _pane_ml_layer(uid))
            return _props_shell(ui_tree.NODE["maplyr"]["label"], _pane_maplyr())
        fn = PANE_FOR_NODE.get(nid)
        if fn is None:
            return None
        title = ui_tree.NODE[nid]["label"]
        pre = PREREQS.get(nid)
        if pre is not None:
            ok_fn, msg, jump, jlabel = pre
            if not ok_fn():
                return _props_shell(title, ui.div(msg, class_="hype-instr"),
                                    _next_hint(jump, jlabel))
        return _props_shell(title, fn(), clear_btn=(nid in ("bnd", "sw", "gw")),
                            wide=(nid in ("gw.alt", "gw.wells")))

    # ---- navigation ----
    def _reachable():
        r = {STEP_REACH}
        if reach_feat() is not None:
            r.add(STEP_DEM)
        if dem_path() is not None:
            r.add(STEP_BOUNDARIES)
        if _domain_build() is not None:          # all four boundaries close into a valid domain
            r.update({STEP_SURFACE, STEP_K, STEP_MESH})
        if run_result() is not None:
            r.update({STEP_RUN, STEP_RESULTS})
        if hz_result() is not None:
            r.add(STEP_REPORT)
        return r

    def _stage_states():
        """Per-stage {n, label, node, state, active} for the header stage bar. Derives from the
        same sources as the tree icons (_tree_statuses + _reachable) — light reads only, never
        _wse_path() (it copies files as a side effect)."""
        st = _tree_statuses()
        reach = _reachable()
        active = ui_tree.STEP_STAGE.get(current_step(), 1)
        sw_done = (st.get("sw") == "done" or st.get("sw.wetted") == "done"
                   or (wse_mode_v() == "upload" and bool(_safe("wse_upload", None))))
        entry = {1: STEP_REACH, 2: STEP_DEM, 3: STEP_BOUNDARIES,
                 4: STEP_SURFACE, 5: STEP_K, 6: STEP_RESULTS, 7: STEP_REPORT}
        done = {1: reach_feat() is not None, 2: dem_path() is not None,
                3: _domain_build() is not None, 4: sw_done,
                5: run_result() is not None, 6: hz_result() is not None,
                7: report_paths() is not None}
        running = {1: st.get("reach") == "running",
                   2: "running" in (st.get("terrain.dem"), st.get("terrain.chanmod")),
                   3: st.get("bnd") == "running",
                   4: "running" in (st.get("sw"), st.get("sw.mesh"), st.get("sw.wetted")),
                   5: "running" in (st.get("gw.mesh"), st.get("gw.run")),
                   6: st.get("gw.res.hz") == "running",
                   7: st.get("report") == "running"}
        error = {2: st.get("terrain.dem") == "error", 4: st.get("sw") == "error",
                 5: st.get("gw.run") == "error", 6: st.get("gw.res.hz") == "error"}
        stale = {4: st.get("sw") == "stale", 5: st.get("gw.run") == "stale",
                 6: "stale" in (st.get("gw.res"), st.get("gw.res.hz"))}
        out = []
        for n, label, node in ui_tree.STAGES:
            state = ("running" if running.get(n) else
                     "error" if error.get(n) else
                     "stale" if stale.get(n) else
                     "done" if done.get(n) else
                     "todo" if entry[n] in reach else "locked")
            out.append({"n": n, "label": label, "node": node,
                        "state": state, "active": n == active})
        return out

    def _runmode_chip():
        """Right-justified run-mode pill: accent "Cloud Run" with the restriction summary on
        hover, or a grayed "Desktop Run" (no limits). A plain div with no data-jump, so
        tree.js's delegated click routing ignores it; tabindex gives keyboard users the
        :focus-within popover."""
        if runmode.IS_DESKTOP:
            name, cls = "Desktop Run", "hype-runmode desktop"
            body = [ui.div("Running locally — the cloud size limits are off.",
                           class_="hype-runmode-lead"),
                    ui.div("Mesh, grid, and particle counts are bounded only by this "
                           "computer's memory.", class_="hype-runmode-note")]
        else:
            name, cls = "Cloud Run", "hype-runmode"
            body = [ui.div("Running on the cloud server — size limits apply:",
                           class_="hype-runmode-lead"),
                    ui.tags.ul(*[ui.tags.li(ui.tags.b(f"{k}: "), v)
                                 for k, v in runmode.cloud_limits()])]
        return ui.div(ui.span(class_="hype-runmode-glyph"),
                      ui.span(name, class_="hype-runmode-name"),
                      ui.div(*body, class_="hype-runmode-pop"),
                      class_=cls, tabindex="0")

    @render.ui
    def stage_bar():
        parts = []
        for s in _stage_states():
            cls = f"hype-stage st-{s['state']}" + (" active" if s["active"] else "")
            parts.append(ui.tags.button(
                ui.span(str(s["n"]), class_="hype-stage-num"),
                ui.span(s["label"], class_="hype-stage-name"),
                type="button", class_=cls,
                title=("Complete the earlier stages first" if s["state"] == "locked" else None),
                **{"data-jump": s["node"]}))
            parts.append(ui.span(class_="hype-stage-sep"))
        return ui.div(ui.div(*parts[:-1], class_="hype-stagebar-scroll"),
                      _runmode_chip(), class_="hype-stagebar")

    _nav_seen: dict = {}

    # ---- tree selection ↔ boundary-slot sync (two-way, equality-guarded both directions) ----
    @reactive.effect
    def _slot_from_sel():
        # Selecting a boundary node (or the wetted extent) makes it the active edit slot;
        # selecting anything else on the boundaries step — or deselecting — commits/clears the
        # slot. Leaving the step entirely is handled by _sync_bnd_slot's own reset.
        nid = sel_node()
        slot = ui_tree.NODE_SLOT.get(nid)
        with reactive.isolate():
            cur = bnd_slot()
            if slot is not None:
                if cur != slot:
                    bnd_slot.set(slot)
                _unhide_node_layers(nid)       # editing a hidden line would be invisible
            elif cur is not None and (nid is None or ui_tree.node_step(nid) == STEP_BOUNDARIES):
                bnd_slot.set(None)

    @reactive.effect
    def _sel_from_slot():
        # A map pick (_bnd_pick_on_click) selects the slot's tree node so the properties panel
        # follows; converges in one hop against _slot_from_sel via the equality guards.
        slot = bnd_slot()
        if slot is None:
            return
        nid = ui_tree.SLOT_NODE.get(slot)
        with reactive.isolate():
            if nid and sel_node() != nid:
                sel_src.set("map")            # map-initiated: the view must not fly away
                sel_node.set(nid)

    @reactive.effect
    @reactive.event(last_click)
    def _bnd_pick_on_click():
        # Boundaries editing is map-driven: click on/near a boundary line to select + edit it.
        # Only when nothing is being edited (else clicks add vertices via Leaflet.draw). Picks the
        # nearest boundary within a zoom-scaled pixel tolerance (forgiving on thin lines).
        if current_step() != STEP_BOUNDARIES or bnd_slot() is not None:
            return
        c = last_click(); crs = proj_crs()
        if not c or crs is None:
            return
        cands = {"up": up_feat(), "left": left_feat(), "right": right_feat(),
                 "down": down_feat(), "wse": wse_extent_feat()}
        cands = {k: v for k, v in cands.items() if v}
        if not cands:
            return
        import math
        import geopandas as gpd
        from shapely.geometry import Point, shape as _shape
        try:
            pt = gpd.GeoSeries([Point(float(c[1]), float(c[0]))], crs=4326).to_crs(crs).iloc[0]
            best, best_d = None, None
            for slot, f in cands.items():
                g = gpd.GeoSeries([_shape(f["geometry"])], crs=4326).to_crs(crs).iloc[0]
                d = pt.distance(g.boundary if g.geom_type == "Polygon" else g)
                if best_d is None or d < best_d:
                    best, best_d = slot, d
            z = _view()[0] or 16
            mpp = 156543.03 * math.cos(math.radians(float(c[0]))) / (2 ** int(z))
            if best is not None and best_d <= 14 * mpp:           # ~14 px tolerance
                _map_ui["map_sel_ts"] = time.monotonic()   # consumed → mapclear skips it
                bnd_slot.set(best)
        except Exception:  # noqa: BLE001
            return

    @reactive.effect
    @reactive.event(last_click)
    def _reach_pick_on_click():
        # Reach editing is map-driven too: once the centerline exists and its layer is the active
        # selection, click on/near the magenta line to start editing it (auto OR manual). Not while
        # already editing (those clicks drag vertices via Leaflet.draw). Uses _scene_crs so it works
        # on the Reach step, before proj_crs exists (which needs the four boundaries).
        if current_step() != STEP_REACH or reach_edit():
            return
        rf = reach_feat()
        if rf is None or sel_node() != "reach":
            return
        c = last_click(); crs = _scene_crs()
        if not c or crs is None:
            return
        import math
        import geopandas as gpd
        from shapely.geometry import Point, shape as _shape
        try:
            pt = gpd.GeoSeries([Point(float(c[1]), float(c[0]))], crs=4326).to_crs(crs).iloc[0]
            g = gpd.GeoSeries([_shape(rf["geometry"])], crs=4326).to_crs(crs).iloc[0]
            z = _view()[0] or 16
            mpp = 156543.03 * math.cos(math.radians(float(c[0]))) / (2 ** int(z))
            if pt.distance(g) <= 14 * mpp:                     # ~14 px tolerance (matches boundaries)
                _map_ui["map_sel_ts"] = time.monotonic()       # consumed → mapclear skips it
                reach_edit.set(True)
        except Exception:  # noqa: BLE001
            return

    @reactive.effect
    @reactive.event(last_click)
    def _grad_add_on_click():
        # Gradient-point placement (Groundwater hub, points mode): an armed click on/near the
        # left/right boundary line becomes an intermediate gradient point at that station.
        side = grad_adding()
        if side is None or sel_node() != "gw":
            return
        c, crs, build = last_click(), proj_crs(), _domain_build()
        if not c or crs is None or not build:
            return
        import math
        import uuid
        import geopandas as gpd
        from shapely.geometry import Point, shape as _shape
        try:
            pt = gpd.GeoSeries([Point(float(c[1]), float(c[0]))], crs=4326).to_crs(crs).iloc[0]
            line = gpd.GeoSeries([_shape(build[side]["geometry"])], crs=4326).to_crs(crs).iloc[0]
            z = _view()[0] or 16
            mpp = 156543.03 * math.cos(math.radians(float(c[0]))) / (2 ** int(z))
            if pt.distance(line) > 14 * mpp:      # ~14 px, zoom-scaled — a miss keeps the arm alive
                return
            st = min(max(float(line.project(pt) / line.length), 0.02), 0.98)  # corners own 0 / 1
            pts = list(grad_pts())
            if any(p["side"] == side and abs(p["station"] - st) < 0.005 for p in pts):
                ui.notification_show("A gradient point already sits there.", duration=4)
                return
            k0, k1 = ("g_ul", "g_dl") if side == "left" else ("g_ur", "g_dr")
            g0, g1 = float(_safe(k0, 0.005)), float(_safe(k1, 0.005))
            pts.append({"id": uuid.uuid4().hex[:8], "side": side, "station": round(st, 4),
                        "gradient": round(g0 + (g1 - g0) * st, 4)})   # prefill: corner interp
            pts.sort(key=lambda p: (p["side"], p["station"]))
            _map_ui["map_sel_ts"] = time.monotonic()   # consumed — mapclear must not deselect
            grad_pts.set(pts)
            grad_adding.set(None)
        except Exception:  # noqa: BLE001
            return

    @reactive.effect
    @reactive.event(input.gpt_arm)
    def _gpt_arm():
        # Value-encoded nonce input ("left:<ts>" / "right:<ts>" / "off:<ts>") — remount-proof.
        side = str(input.gpt_arm() or "").split(":", 1)[0]
        grad_adding.set(side if side in ("left", "right") else None)
        if side in ("left", "right"):
            view_mode_v.set("2d")                  # the points live on the 2-D map

    @reactive.effect
    @reactive.event(input.gpt_rm)
    def _gpt_rm():
        uid = str(input.gpt_rm() or "").split(":", 1)[0]
        pts = [p for p in grad_pts() if p["id"] != uid]
        if len(pts) != len(grad_pts()):
            grad_pts.set(pts)

    _gpt_seen: dict = {}

    @reactive.effect
    def _gpt_mirror():
        # The _keep_inputs idiom for the per-point numerics: on change, write the gradient IN
        # PLACE and bump grad_ver — heads/params re-read without remounting the row being typed in.
        changed = False
        for p in grad_pts():                       # re-arms when rows are added/removed
            iid = f"gpt_g_{p['id']}"
            try:
                v = input[iid]()                   # subscribes; SilentException until mounted
            except Exception:  # noqa: BLE001
                continue
            if v is None or v == _gpt_seen.get(iid, _MISSING):
                continue
            _gpt_seen[iid] = v
            try:
                p["gradient"] = _g4(v)
                changed = True
            except (TypeError, ValueError):
                pass
        if changed:
            with reactive.isolate():
                grad_ver.set(grad_ver() + 1)

    @reactive.effect
    def _reset_grad_adding():
        # Disarm when the user navigates off the hub or leaves points mode (mirrors
        # _reset_kz_adding) — a stale arm would swallow the next unrelated map click.
        if grad_adding() is not None and (
                sel_node() != "gw" or str(_safe("bc_mode", BC_QUAL)) != BC_PROFILE):
            grad_adding.set(None)

    @reactive.effect
    @reactive.event(last_click)
    def _wells_add_on_click():
        # Observation-well placement: an armed click anywhere on the map becomes a well.
        # No snap tolerance (unlike gradient points) — a well outside the model grid is
        # legal to place and simply samples as "outside model grid".
        if not wells_adding() or sel_node() != "gw.wells":
            return
        c = last_click()
        if not c:
            return
        import uuid
        wls = list(obs_wells())
        wls.append({"id": uuid.uuid4().hex[:8],
                    "name": wells_mod.default_name([w.get("name") for w in wls]),
                    "lat": float(c[0]), "lon": float(c[1]),
                    "screen_elev": None, "obs_head": None})
        _map_ui["map_sel_ts"] = time.monotonic()   # consumed — mapclear must not deselect
        obs_wells.set(wls)
        wells_adding.set(False)

    @reactive.effect
    @reactive.event(input.wells_arm)
    def _wells_arm():
        # Value-encoded nonce input ("on:<ts>" / "off:<ts>") — remount-proof (gpt_arm idiom).
        on = str(input.wells_arm() or "").split(":", 1)[0] == "on"
        wells_adding.set(on)
        if on:
            view_mode_v.set("2d")                  # wells are placed on the 2-D map

    @reactive.effect
    @reactive.event(input.wells_rm)
    def _wells_rm():
        uid = str(input.wells_rm() or "").split(":", 1)[0]
        wls = [w for w in obs_wells() if w["id"] != uid]
        if len(wls) == len(obs_wells()):
            return
        obs_wells.set(wls)
        for k in [k for k in _wells_seen if k.endswith(uid)]:
            _wells_seen.pop(k, None)
        kept = [p for p in well_pairs() if uid not in (p["a"], p["b"])]
        if len(kept) != len(well_pairs()):
            well_pairs.set(kept)
            ui.notification_show("Removed a tracked pair that referenced the deleted well.",
                                 duration=4)

    @reactive.effect
    def _wells_mirror():
        # The _gpt_mirror idiom for the per-well inputs (name, screen elevation, observed
        # head): on change, write the record IN PLACE and bump wells_ver — samples re-read
        # without remounting the input being typed in. Unlike gradients, a cleared numeric
        # is a VALID value here (None = no observation), so the change guard compares
        # against the record, not a seen-cache of non-None values.
        changed = False
        for w in obs_wells():                      # re-arms when rows are added/removed
            uid = w["id"]
            for fld, iid in (("name", f"wl_nm_{uid}"), ("screen_elev", f"wl_se_{uid}"),
                             ("obs_head", f"wl_oh_{uid}")):
                try:
                    v = input[iid]()               # subscribes; SilentException until mounted
                except Exception:  # noqa: BLE001
                    continue
                if fld == "name":
                    v = str(v or "").strip()
                    if not v:                      # never blank a name — the report prints it
                        continue
                else:
                    try:
                        v = None if v is None else float(v)
                    except (TypeError, ValueError):
                        continue
                if v == w.get(fld):
                    continue
                w[fld] = v
                changed = True
        if changed:
            with reactive.isolate():
                wells_ver.set(wells_ver() + 1)

    @reactive.effect
    @reactive.event(input.wells_pair_add)
    def _wells_pair_add():
        import uuid
        try:
            a, b = str(input.wlp_a() or ""), str(input.wlp_b() or "")
        except Exception:  # noqa: BLE001
            return
        ids = {w["id"] for w in obs_wells()}
        if a not in ids or b not in ids:
            return
        if a == b:
            ui.notification_show("Choose two different wells.", duration=4)
            return
        if any({p["a"], p["b"]} == {a, b} for p in well_pairs()):
            ui.notification_show("That pair is already tracked.", duration=4)
            return
        well_pairs.set(list(well_pairs()) + [{"id": uuid.uuid4().hex[:8], "a": a, "b": b}])

    @reactive.effect
    @reactive.event(input.wells_pair_rm)
    def _wells_pair_rm():
        pid = str(input.wells_pair_rm() or "").split(":", 1)[0]
        kept = [p for p in well_pairs() if p["id"] != pid]
        if len(kept) != len(well_pairs()):
            well_pairs.set(kept)

    @reactive.effect
    def _reset_wells_adding():
        # Disarm when the user navigates off the wells pane (mirrors _reset_grad_adding) —
        # a stale arm would swallow the next unrelated map click.
        if wells_adding() and sel_node() != "gw.wells":
            wells_adding.set(False)

    # ---- Map layers pane events (reference layers; see _sync_map_layers) ----
    @reactive.effect
    @reactive.event(input.ml_add)
    async def _ml_add():
        await _pick_map_layers("maplayer_add")

    @reactive.effect
    @reactive.event(input.ml_warn)
    async def _ml_warn():
        # The missing-file warning button: locate the moved/renamed file (relink in place).
        uid = str(input.ml_warn() or "").split(":", 1)[0]
        if uid:
            await _pick_map_layers(f"maplayer_relink:{uid}")

    @reactive.effect
    @reactive.event(input.ml_rm)
    def _ml_rm():
        uid = str(input.ml_rm() or "").split(":", 1)[0]
        with reactive.isolate():
            recs = [dict(r) for r in map_layers()]
        keep = [r for r in recs if r.get("id") != uid]
        if len(keep) != len(recs):
            map_layers.set(keep)           # the owner effect tears down widget + caches
            with reactive.isolate():
                cur = sel_node()
            if cur == _ml_key(uid):        # the open pane's layer is gone: show the group
                _select("maplyr")

    @reactive.effect
    @reactive.event(input.ml_open)
    def _ml_open():
        # Group-pane layer rows: clicking one selects its tree row (per-layer pane).
        uid = str(input.ml_open() or "").split(":", 1)[0]
        with reactive.isolate():
            known = any(r.get("id") == uid for r in map_layers())
        if known:
            _select(_ml_key(uid))

    @reactive.effect
    @reactive.event(input.ml_color_evt)
    def _ml_color():
        evt = input.ml_color_evt() or {}
        uid = str(evt.get("u") or "")
        c = str(evt.get("c") or "")
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", c):
            return
        with reactive.isolate():
            rec = next((r for r in map_layers() if r.get("id") == uid), None)
        if rec is not None and rec.get("color") != c:
            rec["color"] = c               # in place; the owner effect applies the restyle
            with reactive.isolate():
                map_layers_ver.set(map_layers_ver() + 1)

    @reactive.effect
    def _ml_op_mirror():
        # The _wells_mirror idiom for the per-layer opacity sliders: write the record IN
        # PLACE and bump the ver — _sync_map_layers (the single widget writer) applies it,
        # so nothing here touches widgets and nothing remounts the slider being dragged.
        changed = False
        for rec in map_layers():                   # re-arms when rows are added/removed
            try:
                v = input[f"ml_op_{rec['id']}"]()  # subscribes; SilentException until mounted
            except Exception:  # noqa: BLE001
                continue
            try:
                v = min(1.0, max(0.0, float(v)))
            except (TypeError, ValueError):
                continue
            if abs(v - float(rec.get("opacity", 0.8))) < 1e-9:
                continue
            rec["opacity"] = v
            changed = True
        if changed:
            with reactive.isolate():
                map_layers_ver.set(map_layers_ver() + 1)

    @reactive.effect
    def _reach_edit_toggle():
        # Props-pane "Edit centerline" / "Done editing" button → flip reach_edit. Strict-increment
        # guard (reach_edit_ctl re-renders, resetting the click count) — like _bnd_edit_buttons.
        try:
            n = int(input.reach_edit_toggle() or 0)
        except Exception:  # noqa: BLE001
            n = 0
        if n != _nav_seen.get("reach_edit_toggle", 0):
            up = n > _nav_seen.get("reach_edit_toggle", 0)
            _nav_seen["reach_edit_toggle"] = n
            if up:
                with reactive.isolate():
                    reach_edit.set(not reach_edit())

    @reactive.effect
    def _reset_reach_edit():
        # Editing only makes sense while the reach exists, its layer is selected, and we're on the
        # Reach step (mirrors _sync_bnd_slot's slot reset). Leaving any of those ends the edit — the
        # push then commits any live Leaflet.draw edit (reconcile's save-on-leave), so nothing is lost.
        if reach_edit() and not (current_step() == STEP_REACH and sel_node() == "reach"
                                 and reach_feat() is not None):
            reach_edit.set(False)

    @reactive.effect
    @reactive.event(input.bnd_done)
    def _bnd_done():
        if current_step() == STEP_REACH:
            reach_edit.set(False)       # the floating-bar "Done" on the reach ends its edit
        else:
            bnd_slot.set(None)          # "Done" → deselect; boundaries become clickable statics again

    @reactive.effect
    @reactive.event(input.bnd_clear)
    def _bnd_clear():
        with reactive.isolate():
            sv = _slot_value(bnd_slot())
        if sv is not None:
            sv.set(None)                # "Clear & redraw" → empty the slot; _push then arms a draw
            dc = _draw_ctl.get("dc")    # drop the old shape now so the fresh draw starts from empty
            if dc is not None:          # (else it lingers and _reclassify picks it, not the new one)
                try:
                    dc.clear(); dc.data = []
                except Exception:  # noqa: BLE001
                    pass

    @reactive.effect
    def _bnd_draw_links():
        # The legend's "Draw" links (only shown on empty rows) select that slot → _push arms a draw.
        # Strict-increment guard (legend re-renders, resetting link counts) — like _continue_nav.
        for slot in ("up", "left", "right", "down", "wse"):
            bid = f"bnd_draw_{slot}"
            try:
                n = int(input[bid]() or 0)
            except Exception:  # noqa: BLE001
                n = 0
            if n != _nav_seen.get(bid, 0):
                up = n > _nav_seen.get(bid, 0)
                _nav_seen[bid] = n
                if up:
                    bnd_slot.set(slot)

    @reactive.effect
    def _bnd_edit_buttons():
        # Legend per-row Edit/Save links: "Edit" (row not active) → select that slot (enter edit,
        # same as clicking the line); "Save" (active row) → bump bnd_commit so the client clicks
        # Leaflet's Save (→ draw:edited → _reclassify_drawn saves + deselects, the floating-bar path).
        # Strict-increment guard (legend re-renders, resetting link counts) — like _bnd_draw_links.
        for slot in ("up", "left", "right", "down", "wse"):
            bid = f"bnd_edit_{slot}"
            try:
                n = int(input[bid]() or 0)
            except Exception:  # noqa: BLE001
                n = 0
            if n != _nav_seen.get(bid, 0):
                up = n > _nav_seen.get(bid, 0)
                _nav_seen[bid] = n
                if up:
                    with reactive.isolate():
                        active = bnd_slot()
                    if active == slot:
                        bnd_commit.set(bnd_commit() + 1)   # Save → client commits the active edit
                    else:
                        bnd_slot.set(slot)                 # Edit → enter edit for this boundary

    @reactive.effect
    def _snap_corners():
        # "Snap corners together" (open-domain warning) → write the assembled snapped sides back so the
        # four corners coincide and the domain closes. Strict-increment guard (button lives in the
        # re-rendered domain_warning, so a plain @reactive.event would re-fire on the count reset).
        try:
            n = int(input.snap_corners() or 0)
        except Exception:  # noqa: BLE001
            n = 0
        if n != _nav_seen.get("snap_corners", 0):
            up = n > _nav_seen.get("snap_corners", 0)
            _nav_seen["snap_corners"] = n
            if up:
                with reactive.isolate():
                    b = _domain_build()
                if b:
                    up_feat.set(b["up"]); left_feat.set(b["left"])
                    right_feat.set(b["right"]); down_feat.set(b["down"])

    def _kz_defaults():
        """Default KH/KV for zones that don't carry their own yet (fresh draws, legacy saves).
        The old global Zone KH/KV pair lives on in _kept so pre-revision projects keep their
        effective values."""
        try:
            kh = float(_kept.get("kzone_kh") or 50.0)
        except (TypeError, ValueError):
            kh = 50.0
        try:
            kv = float(_kept.get("kzone_kv") or 5.0)
        except (TypeError, ValueError):
            kv = 5.0
        return {"default_kh": kh, "default_kv": kv}

    _kz_seen: dict = {}

    @reactive.effect
    def _kz_mirror():
        # The _gpt_mirror idiom for the per-zone KH/KV numerics: on change, write the value
        # into the zone Feature's properties IN PLACE — the list never re-sets, so the row
        # being typed in never remounts and focus never drops.
        for f in kzone_feats():                    # re-arms when zones are added/removed
            p = f.get("properties") or {}
            uid = p.get("uid")
            if not uid:
                continue
            for key, iid in (("KH", f"kz_kh_{uid}"), ("KV", f"kz_kv_{uid}")):
                try:
                    v = input[iid]()               # subscribes; SilentException until mounted
                except Exception:  # noqa: BLE001
                    continue
                if v is None or v == _kz_seen.get(iid, _MISSING):
                    continue
                _kz_seen[iid] = v
                try:
                    fv = float(v)
                except (TypeError, ValueError):
                    continue
                if fv > 0:
                    p[key] = fv

    @reactive.effect
    def _kz_buttons():
        # K-zone list management (same strict-increment guard as _clicked_dynamic so props-pane
        # re-render resets don't fire): Add → arm a guided polygon draw; per-row ×; Clear all.
        def _clicked(bid):
            try:
                n = int(input[bid]() or 0)
            except Exception:  # noqa: BLE001
                n = 0
            last = _nav_seen.get(bid, 0)
            if n != last:
                _nav_seen[bid] = n
                return n > last
            return False
        if _clicked("kz_add"):
            kz_adding.set(True)
            _unhide_node_layers("gw.k")        # drawing into a hidden K-zone layer = invisible
        for f in list(kzone_feats()):          # per-row remove buttons (dynamic ids)
            uid = (f.get("properties") or {}).get("uid")
            if uid and _clicked(f"kz_rm_{uid}"):
                kz = [z for z in kzone_feats()
                      if (z.get("properties") or {}).get("uid") != uid]
                kzone_feats.set(kz)
                _load_into_drawcontrol(kz)
                kz_adding.set(False)
        if _clicked("kz_clear"):
            kzone_feats.set([])
            _load_into_drawcontrol([])
            kz_adding.set(False)

    @reactive.effect
    def _reset_kz_adding():
        if current_step() != STEP_K:           # disarm a pending Add when leaving the K step
            with reactive.isolate():
                if kz_adding():
                    kz_adding.set(False)

    def _clear_auto_picks():
        pick_pts.set([]); reach_feat.set(None); auto_meta.set(None); last_click.set(None)
        for nm in ("pick1", "pick2", "Upstream cap", "Downstream cap"):
            _set_layer(nm, None)
        _hide_key("Reach")                  # persistent decor widget: hide, don't remove
        _mirror_shown.pop("Reach", None)
        dc = _draw_ctl.get("dc")
        if dc is not None:
            try:
                dc.clear()          # actually removes drawn shapes (dc.data=[] alone doesn't)
                dc.data = []
            except Exception:  # noqa: BLE001
                pass

    async def _clear_reach_all():
        # One reset for BOTH reach modes — picks/linework, boundaries, K-zones, the DEM, and
        # every downstream result (all of it is sized from the reach, so it all goes together).
        for t in (dem_task, snap_task, reach_task, da_task, dir_task, delineate_task,
                  wse_task, carve_task, scene_terrain_task):
            try:                        # reach-step producers: their done-handlers are all
                t.cancel()              # cancel-silent, so nothing resurrects mid-clear
            except Exception:  # noqa: BLE001
                pass
        _da_snap["sig"] = None          # a redrawn identical line prefills afresh
        _reach_dir["sig"] = None        # ... and re-checks its direction
        _reach_dir["verdict"] = "pending"
        _clear_auto_picks()
        await _cascade_clear("bnd", include_self=True)   # boundaries + sw/gw/hz + report + GMS
        kzone_feats.set([]); bnd_slot.set(None)
        grad_pts.set([]); grad_adding.set(None); ref_slope_override.set(None)
        obs_wells.set([]); well_pairs.set([]); wells_adding.set(False)   # wells anchor to this
        _wells_seen.clear(); _wells_grid_cache.clear()                   # reach's domain
        dem_path.set(None); dem_meta.set(None)   # also drop the downloaded DEM + its overlay
        # dem_src deliberately SURVIVES a reach clear: the source choice belongs to the
        # project, and the auto-chain re-imports the linked raster for the redrawn reach.
        dem_stretch_v.set(None); dem_lohi_v.set(None); _dem_shade_sig.clear()
        _set_layer("dem", None)
        carve_active.set(False); carve_meta.set(None)    # active_dem() must not keep serving a
        _set_layer("dem_carve", None)                    # carve whose DEM is gone
        _wse_used.clear()
        input_snapshot.set(None)
        _stale_marks.set(frozenset())
        _sweep_alt_dir()
        _probe_cache.clear(); _probe_cur.clear(); _probe_sig.clear()
        await _send_probe(None)
        _scene.clear()                     # 3-D frame re-anchors on the next DEM
        await session.send_custom_message("hype3d_clear", {})
        _wire_state.set(False)             # hype3d_clear resets S.wireframe client-side
        _kept.pop("grid_wireframe", None)
        ui.update_checkbox("grid_wireframe", value=False)
        _kept.pop("manual_da", None)       # a fresh draw re-prefills from NHD
        # NOT update_numeric: None fields are dropped from update messages (a no-op), while a
        # raw input message delivers value=null and the binding empties the box.
        session.send_input_message("manual_da", {"value": None})
        _chain["dem"] = _chain["bnd"] = None       # a redrawn reach auto-chains afresh
        # Drop the painted statics NOW — the step-entry sync only re-mirrors on a step CHANGE,
        # and we're already on Reach. Hide first, then re-mirror with every feature None: the
        # re-mirror EMPTIES each persistent decor's data, which lands even where the visible
        # trait is a client no-op (GeoJSON vectors — see _visible_hide_works).
        _clear_mirror_layers()
        _mirror_features_as_layers()
        print("[clear] reach cleared (features, terrain, results)", flush=True)
        ui.notification_show("Reach cleared. Terrain, boundaries, and all results were reset.",
                             duration=4)

    @reactive.effect
    async def _clear_points():
        if _clicked_dynamic("clear_points"):
            await _clear_reach_all()

    @reactive.effect
    async def _clear_draw():
        if _clicked_dynamic("clear_draw"):
            await _clear_reach_all()

    async def _begin_new_project():
        """The New Project flow behind the start page's New project tile (the header's own New
        door was folded into the start page in v1.0.5).

        GATE-AWARE. Under the startup gate there is nothing to lose, so it goes straight to the
        pick/name dialog. With a project open it confirms first: the start page is reachable
        mid-session from the header's Projects link, and without the confirm its New tile would
        be a one-click session wipe in cloud mode, where the confirm is the only thing standing
        between a click and an unsaved reach."""
        if _gated():
            if runmode.IS_DESKTOP:
                await _pick_path("new_project", save=True)
            else:
                _show_new_project_dialog()
            return
        if runmode.IS_DESKTOP:
            # GMS-style: pick the project location up front so every run writes in place.
            # Hard gate — no unsaved desktop sessions, so the only action is Create.
            open_note = (ui.p(f"The current project ({Path(_ws['project_file']).name}) keeps "
                              "its folder. It is saved automatically when you leave it.",
                              class_="hype-dim")
                         if _ws["project_file"] else None)
            ui.modal_show(ui.modal(
                ui.p("Pick where the project's main .hype file lives. Terrain, model runs, "
                     "and results save into that folder as you work."),
                open_note,
                title="New project",
                footer=ui.TagList(
                    ui.modal_button("Cancel"),
                    ui.input_action_button("confirm_new_create", "Create project…",
                                           class_="btn-primary"),
                ),
                easy_close=True))
            return
        ui.modal_show(ui.modal(
            ui.p("This clears the reach, terrain, boundaries, model runs, and results in this "
                 "session. Save or download your project first if you want to keep it."),
            title="Start a new project?",
            footer=ui.TagList(
                ui.modal_button("Cancel"),
                ui.input_action_button("confirm_new_project", "Start new project",
                                       class_="btn-danger"),
            ),
            easy_close=True))

    @reactive.effect
    async def _new_create():
        if not _clicked_dynamic("confirm_new_create"):
            return
        ui.modal_remove()
        await _pick_path("new_project", save=True)

    async def _reset_memory_state():
        """Wipe the in-memory session back to first-run — reactives, map layers, 3-D scene —
        WITHOUT touching the disk. Callers that own a temp workspace wipe it separately
        (_reset_session_state); project-folder flows must never wipe the user's folder."""
        # Stop in-flight work first: a straggling done-handler must not repopulate the fresh
        # session, and Windows can't delete files a live child still holds open.
        _autosave["restoring"] = True
        session.on_flushed(lambda: _autosave.update(restoring=False), once=True)
        _task_armed.update(sw=False, gw=False, hz=False, alt=False, report=False,
                           gms=False, pick=False, cmp=False)
        _gms_epoch["n"] += 1               # detached GMS builds must never swap post-reset
        _gms_pending.clear()
        try:
            gms_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        gms_status_v.set(None)
        _terminate_child()
        _kill_ras_proc()
        for h, k in ((_mesh_proc, "proc"), (_mesh3d_proc, "p"), (_hz_proc, "p"),
                     (_alt_proc, "p"), (_soil_proc, "p"), (_usgs_proc, "p"),
                     (_gms_proc, "p"), (_pick_proc, "p")):
            p = h.get(k)
            if p is not None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        _chain["dem"] = _chain["bnd"] = None
        up_feat.set(None); left_feat.set(None); right_feat.set(None); down_feat.set(None)
        kzone_feats.set([]); wse_extent_feat.set(None); bnd_slot.set(None)
        grad_pts.set([]); grad_adding.set(None); ref_slope_override.set(None)
        _gpt_seen.clear()   # was skipped here for years: New project inherited the previous
        #                     project's gradient points until a restore/reach-clear overwrote them
        obs_wells.set([]); well_pairs.set([]); wells_adding.set(False)
        _wells_seen.clear(); _wells_grid_cache.clear()
        map_layers.set([])                 # reference-layer POINTERS are per-project too
        with reactive.isolate():
            map_layers_ver.set(map_layers_ver() + 1)
        _ml_cache.clear(); _ml_status.clear(); _ml_err.clear()
        dem_path.set(None); dem_meta.set(None)
        dem_src.set(dem.normalize_dem_source(None))   # back to the 3DEP default
        dem_stretch_v.set(None); dem_lohi_v.set(None); _dem_shade_sig.clear()
        dem_hs_v.set(8.0); dem_opacity_v.set(0.8)     # declaration defaults
        origin_override.set(None)          # user-set Model Origin is per-project
        proj_crs.set(None)                 # re-derived from the next project's reach
        _drop_gw_artifacts()               # run result + head/grid/WSE layers + the grid preview
        head_layer_v.set(1); head_opacity_v.set(0.85); hd_contours_v.set(True)
        grid_opacity3d_v.set(1.0); grid_color3d_v.set(None)
        _set_project_meta(None, None)      # create/open re-establish it (or the gate re-arms)
        stage.set("")
        log_lines.clear(); log_tick.set(0); step_v.set(0)
        hz_result.set(None); hz_gdf.set(None); hz_sel_pids.set(())
        hz_log_lines.clear(); hz_log_tick.set(0); hz_step_v.set(0)
        input_snapshot.set(None); flow_lookup.set(None); flow_source.set(None)
        usgs_pick_v.set(None); usgs_flow_err.set(None)
        soil_snapshot.set(None); soil_overrides.set([])
        soil_source.set(None); soil_sel_units.set(frozenset()); soil_fetch_err.set(None)
        soil_inspect.set(None)
        results_model.set(None); report_paths.set(None); _report_shown_for.set(None)
        _report_stamp.set(None)
        fp_anim_on_v.set(False); fp_anim_speed_v.set(3.0)
        fp_anim_color_v.set(FP_ANIM_COLORS[0]); fp_anim_style_v.set("comet")
        fp_anim_mode_v.set("solid")
        fp_line_show_v.set(True); fp_line_weight_v.set(1.0)
        fp_line_opacity_v.set(0.9); fp_line_mode_v.set("class")
        _fp_rng_applied["rng"] = None
        await _send_fp_anim()              # park the client animator with the dying layers
        _video_cancel.set()                # cooperative stop for an in-flight video build
        _video_result.set(None)
        _video_prog.update(stage="", i=0, n=0, t0=0.0)
        _probe_cache.clear(); _probe_cur.clear(); _probe_sig.clear()
        await _send_probe(None)            # drop the client's hover grid too
        alt_result.set(None); alt_view.set(None)
        alt_log_lines.clear(); alt_scen_recs.clear(); alt_log_tick.set(0)
        _alt_stats_cache.clear(); _alt_payload.clear()
        _drop_ras_artifacts(); ras_log_lines.clear(); ras_log_tick.set(0)
        _ras_inputs_sig["sig"] = None
        ras_opacity_v.set(0.7)             # declaration default
        wse_mode_v.set("model")
        _wse_used.clear()
        pick_pts.set([]); reach_feat.set(None); auto_meta.set(None); last_click.set(None)
        delineate_mode.set("auto"); reach_edit.set(False); kz_adding.set(False)
        reach_gen.set(0); dem_gen.set(0)
        _da_snap["sig"] = None          # the next project's draw prefills + direction-checks
        _reach_dir["sig"] = None; _reach_dir["verdict"] = "pending"
        _flow["gdf"] = None; _flow.pop("bbox", None)   # NHD snap cache is per-site; a stale
        nhd_status.set("")              # bbox would even suppress the refetch at the same view
        dc = _draw_ctl.get("dc")
        if dc is not None:
            try:
                dc.data = []
            except Exception:  # noqa: BLE001
                pass
        for k in list(_layers):
            _set_layer(k, None)
        _hidden_keys.clear()               # a new run starts with every layer checkbox checked
        _layer_shadow.clear()
        _clear_mirror_layers()             # decor identity guards must forget the old project
        _mirror_features_as_layers()       # …and the empty data push wipes any painted decor
        _check_state.clear()               # …including the group toggles (back to defaults)
        _apply_check_effective("base")     # re-sync tile traits (user may have been on Topo)
        _map_ui.pop("dem_bounds", None)
        carve_active.set(False)
        carve_meta.set(None)
        _stale_marks.set(frozenset())
        comparison_mode_v.set(False)       # an open comparison overlay closes with the session
        comparison_collection_v.set(None); comparison_file_v.set(None)
        comparison_dirty_v.set(False); comparison_selected_member_v.set(None)
        comparison_inspections_v.set({})   # _sync_comparison_client reacts and hides the client
        _scene.clear()                     # 3-D frame re-anchors on the next DEM
        await session.send_custom_message("hype3d_clear", {})
        _wire_state.set(False)             # hype3d_clear resets S.wireframe client-side
        _kept.clear()                      # typed inputs are project state: address, DA, site
        #                                    metadata, K values, every knob — panes re-read
        #                                    _keep() defaults when they rebuild
        ui.update_checkbox("grid_wireframe", value=False)   # not pane-mounted: explicit update
        view_mode_v.set("2d")
        await session.send_custom_message(  # a fresh project starts with the groups collapsed
            "hype_tree_collapse", {"groups": list(ui_tree.GROUP_IDS)})
        _bump_vis()

    async def _reset_session_state():
        """Memory reset + disk wipe for TEMP sessions. Shared by cloud New and cloud Open
        (which re-populates after). In project-folder mode the wipe is skipped — the folder
        is the user's data; project flows handle disk explicitly."""
        await _reset_memory_state()
        if _ws["project_file"] is None:
            # Leftovers from an opened/previous project must never leak into the next
            # project's Download (the bundler sweeps work_dir wholesale).
            for child in work_dir.iterdir():
                try:
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
                except OSError:
                    pass

    def _adopt_workspace(folder: Path, main_file: Path | None):
        """Rebind the session workspace to `folder` (a project folder, or a fresh temp dir
        when main_file is None). Only called while no task is running — every consumer reads
        the closure at call time, and engine payloads snapshot str(work_dir) per run. The
        old dir is deleted only when it was an unsaved temp session (nothing else will)."""
        nonlocal work_dir
        old, was_temp = work_dir, _ws["project_file"] is None
        work_dir = Path(folder)
        _ws["project_file"] = str(main_file) if main_file else None
        project_file.set(_ws["project_file"])
        if runmode.IS_DESKTOP and main_file:
            recents.touch(main_file)       # welcome-dialog list; swallows its own IO errors
        if was_temp and old != work_dir:
            shutil.rmtree(old, ignore_errors=True)

    @reactive.effect
    async def _reset():
        # Modal confirm button is a dynamic input — strict-increment guard, house pattern.
        if not _clicked_dynamic("confirm_new_project"):
            return
        ui.modal_remove()
        await _reset_session_state()
        _map_home()                        # the wiped session starts at the national view
        # The reset cleared the project name, so the session is logically gated again:
        # go straight to the name dialog (its Cancel lands on the welcome).
        _show_new_project_dialog()

    @reactive.effect
    @reactive.event(input.nav_help)
    def _help():
        _show_help()

    @reactive.effect
    @reactive.event(input.start_help)     # start-page rail link (nonce): never ignore_init
    def _help_from_start():
        _show_help()

    @reactive.effect
    def _help_close():
        # `_clicked_dynamic`: the Close button is rebuilt on every show. Help raised from the
        # start page REPLACES it (one modal at a time), so Close funnels back through
        # _ensure_welcome like every other project-dialog exit; a no-op once a project is open.
        if _clicked_dynamic("help_close"):
            ui.modal_remove()
            _ensure_welcome()

    def _show_help():
        ui.modal_show(ui.modal(
            ui.markdown(
                "**How to use**\n\n"
                "Follow the numbered stages across the top; each stage's settings open in the "
                "panel on the right. The **Layers** panel (left) shows/hides everything on the "
                "map; select any item there for its details.\n\n"
                "1. **Reach**: Auto (default): zoom in until the streams appear, then click the "
                "upstream and downstream points on one (≤ 1 mile apart). Or Manual: draw the "
                "centerline from upstream to downstream and enter the drainage area.\n"
                "2. **Terrain**: fetched automatically from USGS 3DEP once the reach is set. "
                "Re-fetch at another resolution, or carve a channel, under Terrain.\n"
                "3. **Boundaries**: generated automatically from the terrain (floodplain width "
                "× bankfull depth). Select a side, or click its line on the map, to edit it.\n"
                "4. **Water surface**: choose the source: run the **HEC-RAS 2025 2D** model, "
                "use the auto/drawn wetted extent, or upload a WSE raster.\n"
                "5. **Groundwater**: review the subsurface properties and model grid, set the "
                "boundary-condition gradients, then **Run groundwater model**.\n"
                "6. **Results**: explore hydraulic head, **delineate the hyporheic zone**, and "
                "click flow paths (or drag a box) for their statistics.\n\n"
                "The water-surface extent becomes the constant-head (CHD) top boundary: from "
                "the surface model's WSE when available, else the DEM elevations inside the "
                "drawn extent. Nothing is saved on the server; **Save** (top right) gives you "
                "a project file (.hype) to pick up later from **Projects**: complete with all "
                "computed data, or settings-only for a small file. A .hype file is a ZIP "
                "archive; rename it to .zip to browse the stage folders in GIS.\n\n"
                "**Projects** (top right) is the start page: new project, open project, "
                "example projects to download and explore, your recent projects, and what "
                "changed in each release."
                + ("\n\n**Desktop projects**: HYPE Desktop works in project folders. **New "
                   "project** asks where to put the project's main .hype file, and every "
                   "stage saves into that folder as you work (terrain, models, results sit "
                   "next to the main file, like a GMS project). **Save** rewrites the main "
                   "file in place; settings also autosave after each completed run. **Save "
                   "As** copies the whole project to a new name or location and switches to "
                   "it. The project folder also holds an Aquaveo GMS copy of the groundwater "
                   "model in GMS, refreshed after each run. Avoid working in two windows on "
                   "the same folder; the last writer wins."
                   if runmode.IS_DESKTOP else "")),
            title="Help",
            # Server-side Close (not modal_button): raised from the start page this dialog
            # replaces the gate, so Close must funnel back; easy_close stays off while gated.
            footer=ui.input_action_button("help_close", "Close", class_="btn-primary"),
            easy_close=not _gated()))

    @reactive.effect
    @reactive.event(input.nav_about)
    def _about():
        ui.modal_show(ui.modal(
            ui.markdown(
                f"**HYPE - Hyporheic Exchange Explorer**\n\nVersion {APP_VERSION}"
                f"{_desktop_build_line()}\n\n"
                "Build a reach-scale hyporheic-exchange model from a map: trace a reach, fetch "
                "terrain, model the water surface, run the groundwater model, and delineate "
                "the hyporheic zone.\n\n"
                "Terrain & streams: USGS 3DEP and NHD. Engines: HEC-RAS 2025 (2D surface), "
                "MODFLOW 6 + MODPATH 7 (groundwater and particle tracking)."),
            title="About", easy_close=True,
            footer=ui.TagList(
                ui.input_action_button("about_whatsnew", "What's new",
                                       class_="btn-outline-secondary"),
                ui.modal_button("Close", class_="btn-primary"))))

    @reactive.effect
    def _about_whatsnew():
        # `_clicked_dynamic`, not `@reactive.event`: About is rebuilt on every show, so the
        # button's counter resets to 0 each time and an event binding would miss the second
        # click. The dialogs swap (Shiny shows one modal at a time).
        if _clicked_dynamic("about_whatsnew"):
            _show_whatsnew()

    # ---- downloads ----
    # "Save" captures the session into one .hype archive (a zip organized by pipeline stage —
    # see hype_app/bundle.zip_workspace): drawn reach + boundaries (serialized from the
    # in-memory reactives) and config always; terrain, the HEC-RAS surface model, and the
    # MODFLOW 6 / MODPATH 7 groundwater model + results when the "Complete project" scope is
    # chosen in the save dialog.
    def _has_workspace():
        # Anything worth saving yet? (a reach, a DEM, a surface run, a GW run, or a HZ result)
        return bool(reach_feat() or dem_path() or ras_result() or run_result() or hz_result())

    def _run_config():
        # Reproducibility metadata -> config/run_config.json inside the archive.
        crs = proj_crs()
        if crs is not None:
            try:
                epsg = crs.to_epsg()
            except Exception:  # noqa: BLE001
                epsg = None
            crs_info = {"epsg": epsg, "name": getattr(crs, "name", None), "wkt": crs.to_wkt()}
        else:
            crs_info = {"epsg": None, "name": None, "wkt": None}
        dem_p = active_dem()
        return {
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "app": "HYPE - Hyporheic Exchange Explorer",
            "app_version": APP_VERSION,
            "working_crs": crs_info,             # UTM/metres — the run CRS
            "vector_crs": "EPSG:4326",           # everything under 1_/3_ + the reach
            "model_origin_elev_m": model_origin_effective(),
            "wse_mode": wse_mode_v(),
            "kzones": {"enabled": bool(_safe("use_kzones", False)),
                       "count": len(kzone_feats() or []),
                       "zones": [{"label": (f.get("properties") or {}).get("LABEL"),
                                  "kh": (f.get("properties") or {}).get("KH"),
                                  "kv": (f.get("properties") or {}).get("KV"),
                                  "src": (f.get("properties") or {}).get("src")}
                                 for f in (kzone_feats() or [])]},
            "active_dem": (Path(str(dem_p)).name if dem_p else None),
            "carve_applied": bool(carve_active()),
        }

    # Workspace paths inside the manifest are stored behind bundle.WS_TOKEN, work_dir-relative —
    # a saved session's absolute paths are meaningless in the session that reopens it. Thin
    # closures (not aliases) so both read the CURRENT work_dir after a project-folder rebind.
    def _tokenize_paths(obj):
        return bundle.tokenize_paths(obj, work_dir)

    def _detokenize_paths(obj):
        return bundle.detokenize_paths(obj, work_dir)

    def _project_state():
        """The session manifest (config/state.json) — everything Open needs that isn't already
        a file in the archive. The vectors travel separately as GeoJSON (bundle._VECTOR_ARCS);
        the two transient previews (RAS mesh, 3-D grid) are deliberately not saved — each
        regenerates with one click."""
        with reactive.isolate():
            return {
                "format_version": bundle.FORMAT_VERSION,
                "app_version": APP_VERSION,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "auto_meta": auto_meta(),
                "delineate_mode": delineate_mode(),
                "reach_gen": reach_gen(), "dem_gen": dem_gen(),
                "dem_meta": dem_meta(),
                "dem_source": _tokenize_paths(dict(dem_src() or {})),
                "carve_active": carve_active(),
                "carve_meta": _tokenize_paths(carve_meta()),
                "dem_hs": dem_hs_v(), "dem_opacity": dem_opacity_v(),
                "dem_stretch": dem_stretch_v(),
                "origin_override": origin_override(),
                "ref_slope_override": ref_slope_override(),
                "grad_pts": list(grad_pts()),
                "obs_wells": [dict(w) for w in obs_wells()],
                "well_pairs": [dict(p) for p in well_pairs()],
                # Path POINTERS only, never copies. tokenize_paths rewrites in-workspace
                # paths to $WORKSPACE$ tokens and leaves foreign absolute paths untouched,
                # so external reference files survive save/restore byte-identical.
                "map_layers": _tokenize_paths([dict(r) for r in map_layers()]),
                "wse_mode": wse_mode_v(),
                "ras_result": _tokenize_paths(ras_result()),
                "ras_opacity": ras_opacity_v(),
                "run_result": _tokenize_paths(run_result()),
                "input_snapshot": input_snapshot(),
                # flow/soil/results/alternatives carry absolute workspace paths
                # (raw_response_paths, hz_dir) — tokenize them like the other artifact
                # dicts or a moved/reopened project folder points at dead paths.
                "flow_lookup": _tokenize_paths(flow_lookup()), "flow_source": flow_source(),
                "soil_snapshot": _tokenize_paths(soil_snapshot()),
                "soil_overrides": soil_overrides(),
                "soil_source": soil_source(),
                "results_model": _tokenize_paths(results_model()),
                "alt_result": _tokenize_paths({k: v for k, v in (alt_result() or {}).items()
                                               if k != "running"} or None),
                "head_layer": head_layer_v(), "head_opacity": head_opacity_v(),
                "head_contours": hd_contours_v(),
                "fp_line_show": fp_line_show_v(), "fp_line_weight": fp_line_weight_v(),
                "fp_line_opacity": fp_line_opacity_v(), "fp_line_mode": fp_line_mode_v(),
                "hz_result": _tokenize_paths(hz_result()),
                "wse_used": _tokenize_paths(_wse_used.get("path")),
                "stale_marks": sorted(_stale_marks()),
                "project_name": _ws["project_name"],
                "project_units": (project_meta_v() or {}).get("units")
                                 or project_meta.UNITS_METRIC,
                "project_created": (project_meta_v() or {}).get("created"),
                "project_id": (project_meta_v() or {}).get("project_id"),
                "site_id": (project_meta_v() or {}).get("site_id"),
                "kept": dict(_kept),
                "check_state": dict(_check_state),
                "hidden_keys": sorted(_hidden_keys),
                "sel_node": sel_node(), "current_step": current_step(),
            }

    def _current_vectors():
        return {"reach": reach_feat(), "upstream": up_feat(), "left": left_feat(),
                "right": right_feat(), "downstream": down_feat(), "domain": domain_feat(),
                "wse_extent": wse_extent_feat(), "k_zones": kzone_feats()}

    def _save_project_file() -> bool:
        """Desktop in-place Save: write the settings-only bundle to the project's main .hype.
        The desktop_project marker is what classify_bundle keys on — it is written ONLY here,
        never by the download/export path, so portable copies stay portable."""
        pf = _ws["project_file"]
        if not pf:
            return False
        with reactive.isolate():
            bundle.save_bundle_to(work_dir, pf, vectors=_current_vectors(), params=params(),
                                  run_config=_run_config(),
                                  state=_project_state() | {"desktop_project": True},
                                  assessment_input=input_snapshot())
        return True

    def _gms_export_name() -> str:
        """Project name sanitized for the GMS file/folder names (Win32-safe)."""
        stem = _ws["project_name"] or ""
        if not stem:
            pf = project_file()
            stem = Path(pf).stem if pf else ""
        stem = re.sub(r"[^A-Za-z0-9 _-]+", "_", stem).rstrip(" .")
        return stem or "hype_project"

    def _build_gms_tree():
        """Generate the GMS project into a temp dir for bundling.

        Never sinks the save: any export failure becomes GMS/EXPORT_ERROR.txt inside
        the archive (a mid-download abort would corrupt the user's .hype instead).
        Returns (tmp_root_to_cleanup, extra_trees_tuple_for_zip_workspace).
        """
        tmp = Path(tempfile.mkdtemp(prefix="hype_gms_"))
        dest = tmp / "gms"
        dest.mkdir()
        name = _gms_export_name()
        porosity = 0.3
        try:
            stats = json.loads((work_dir / "summary" / "hz" / "hz_stats.json")
                               .read_text(encoding="utf-8"))
            porosity = float(stats["knobs"]["porosity"])
        except Exception:  # noqa: BLE001 — fall back to the pane input / default
            try:
                porosity = float(_safe("porosity", 0.3))
            except Exception:  # noqa: BLE001
                pass
        try:
            with reactive.isolate():
                crs = proj_crs()
                # Pathlines only when a CURRENT delineation exists: summary/hz survives
                # gw reruns on disk, so unconditional inclusion shipped stale particles.
                hz_ok = hz_result() is not None
            wkt = crs.to_wkt(version="WKT1_ESRI") if crs is not None else ""
            res = gms.export_gms_project(
                work_dir, dest, name=name, crs_wkt_esri=wkt, porosity=porosity,
                hz_dir=(work_dir / "summary" / "hz") if hz_ok else None,
                log=lambda m: print(f"[gms] {m}", flush=True))
            for w in res.get("warnings", []):
                print(f"[gms] {w}", flush=True)
        except Exception as e:  # noqa: BLE001 — the save must still complete
            print(f"[gms] export failed: {e!r}", flush=True)
            shutil.rmtree(dest, ignore_errors=True)
            dest.mkdir(exist_ok=True)
            (dest / "EXPORT_ERROR.txt").write_text(
                "The GMS project could not be generated for this save.\n"
                f"Reason: {e}\n"
                "The rest of the archive is complete; re-run the groundwater "
                "stage and save again to retry.\n", encoding="utf-8")
        return tmp, (("GMS", dest),)

    # ---- live GMS folder (work_dir/GMS, refreshed off-loop after gw/hz runs) -----------
    @reactive.extended_task
    async def gms_task(payload: dict) -> dict:
        # ALWAYS a spawn child, never an in-process thread: the export writes HDF5 via
        # h5py while the app process carries GDAL's own HDF5 runtime (rasterio), and on
        # Windows that pairing hard-crashed the server at the .h5 write with no traceback
        # (live 2026-07-26; the identical standalone run succeeded). hz_task protocol.
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=gms_run.child_run, args=(payload, q), daemon=True)
            _gms_proc["p"] = p
            p.start()
            result = error = None
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "log":
                        print(f"[gms] {data}", flush=True)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:
                try:
                    kind, data = q.get_nowait()
                    if kind == "log":
                        print(f"[gms] {data}", flush=True)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            _gms_proc["p"] = None
            if error is not None:
                raise RuntimeError(error.strip().splitlines()[-1] or "GMS export failed")
            if result is None:
                raise RuntimeError("the export was interrupted")
            return result
        return await anyio.to_thread.run_sync(_work)

    def _request_gms_build(include_hz: bool) -> None:
        """GATHER phase of a GMS folder refresh. Reactive reads are isolated (callers are
        done-handlers and restore code) and passed as plain args. Single-flight with
        newest-wins: ExtendedTask.invoke() while running QUEUES (FIFO) instead of
        coalescing, so a build in flight parks the payload and _gms_done chains it."""
        with reactive.isolate():
            gwf_ws = work_dir / "model" / "gwf_workspace"
            if next(gwf_ws.glob("*.dis.grb"), None) is None:
                return
            try:
                crs = proj_crs()
                payload = {
                    "work_dir": str(work_dir), "name": _gms_export_name(),
                    "wkt": crs.to_wkt(version="WKT1_ESRI") if crs is not None else "",
                    "porosity": float(_safe("porosity", 0.3)),
                    "include_hz": include_hz,
                }
            except Exception:  # noqa: BLE001 — a nicety must never break a done-handler
                return
        if _task_state(gms_task) == "running":
            _gms_pending["payload"] = payload      # newest wins; dupes collapse
            return
        _task_armed["gms"] = True
        _gms_flight.update(epoch=_gms_epoch["n"], work_dir=payload["work_dir"])
        gms_task(payload)

    @reactive.effect
    def _gms_done():
        if gms_task.status() in ("initial", "running", "cancelled"):
            return
        if not _task_armed["gms"]:      # already applied (or session reset)
            return
        _task_armed["gms"] = False
        try:
            res = gms_task.result()
        except Exception as e:  # noqa: BLE001 — child killed or crashed
            res = {"ok": False, "skipped": False, "error": str(e)}
        # Post-hoc epoch veto (the child cannot see app state): a sweep/reset while it
        # ran means whatever it swapped in reflects invalidated results — remove it
        # quietly; the sweeper owns the folder.
        if _gms_flight.get("epoch") != _gms_epoch["n"]:
            shutil.rmtree(Path(_gms_flight.get("work_dir", str(work_dir))) / "GMS",
                          ignore_errors=True)
        elif not res.get("skipped"):
            gms_status_v.set(res)
            if not res.get("ok"):
                reason = res.get("error") or "unknown error"
                ui.notification_show(
                    f"Aquaveo GMS export did not finish: {reason}. Your results are "
                    "not affected. If the project is open in GMS, close it there, "
                    "then re-run the groundwater stage to retry.",
                    type="warning", duration=8)
        nxt = _gms_pending.pop("payload", None)     # chain the parked newest build
        if nxt is not None:
            _task_armed["gms"] = True
            _gms_flight.update(epoch=_gms_epoch["n"], work_dir=nxt["work_dir"])
            gms_task(nxt)

    def _stream_bundle(include_computed=True, include_gms=False):
        """Build the archive and stream it in 1 MiB chunks — flat egress memory even at
        hundreds of MB."""
        gms_tmp = None
        extra: tuple = ()
        if include_gms:
            live = work_dir / "GMS"
            if live.is_dir() and _task_state(gms_task) != "running":
                extra = (("GMS", live),)   # pack the live folder; no rebuild
            elif next((work_dir / "model" / "gwf_workspace").glob("*.dis.grb"), None):
                # No folder yet (or a refresh is mid-swap): legacy temp build.
                gms_tmp, extra = _build_gms_tree()
        try:
            path = bundle.zip_workspace(work_dir, vectors=_current_vectors(), params=params(),
                                        run_config=_run_config(), state=_project_state(),
                                        assessment_input=input_snapshot(),
                                        include_computed=include_computed,
                                        extra_trees=extra)
        finally:
            if gms_tmp is not None:
                shutil.rmtree(gms_tmp, ignore_errors=True)
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):   # 1 MiB — flat egress memory
                    yield chunk
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    def _save_scope_full():
        return str(_safe("save_scope", "full")) == "full"

    @render.ui
    def project_badge():
        if runmode.IS_DESKTOP:
            pf = project_file()
            if not pf:
                return None
            p = Path(pf)
            return ui.span(p.stem, class_="hype-project-badge", title=str(p.parent))
        name = (project_meta_v() or {}).get("name")
        if not name:
            return None
        return ui.span(name, class_="hype-project-badge",
                       title="Cloud session. Use Save to download the project file.")

    @reactive.effect
    async def _push_doc_title():
        # Browser tab title mirrors the project name (the desktop shell titles its own
        # window via _post_title; a hidden WebView tab title is harmless there).
        name = (project_meta_v() or {}).get("name")
        await session.send_custom_message("hype_doc_title", {"title": name})

    @render.ui
    def save_project():
        pf = project_file()
        if pf:
            # Project open (desktop): Save = rewrite the main file in place; Save As =
            # copy the whole folder (content dirs + GMS export) and switch to it.
            return ui.TagList(
                ui.input_action_link("nav_save", "Save",
                                     title=f"Save to {Path(pf).name}"),
                ui.input_action_link("nav_save_as", "Save As…",
                                     title="Copy the whole project to a new name or "
                                           "location, then switch to it"))
        name = (project_meta_v() or {}).get("name")     # cloud: re-render when the name lands
        if name is None and not _has_workspace():
            return ui.span("Save", class_="hype-nav-dim", title="Nothing to save yet")
        return ui.input_action_link("nav_save", "Save",
                                    title="Download this project as a .hype file. Reopen "
                                          "it later with Open.")

    def _do_desktop_save():
        # Hard gate: a desktop session always has a project open, so Save = rewrite the
        # main .hype in place. No dialog, no download.
        if not _ws["project_file"]:
            _ensure_welcome()              # unreachable in practice; never strand the gate
            return
        try:
            _save_project_file()
            ui.notification_show(f"Saved {Path(_ws['project_file']).name}", duration=4)
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Save failed: {e}", type="error", duration=10)

    @reactive.effect
    @reactive.event(input.nav_save)
    def _save_dialog():
        if runmode.IS_DESKTOP:
            _do_desktop_save()
        else:
            _show_bundle_dialog()

    async def _begin_save_as():
        """Desktop Save As entry: guard, then route through the native/dev picker. The
        reply funnels into _on_project_path -> _save_as_project."""
        if not (runmode.IS_DESKTOP and _ws["project_file"]):
            return
        if _busy_tasks():
            ui.notification_show("A task is still running. Wait for it to finish (or "
                                 "cancel it) before using Save As.", type="warning",
                                 duration=6)
            return
        await _pick_path("save_as", save=True,
                         file_name=f"{Path(_ws['project_file']).stem}.hype")

    @reactive.effect
    @reactive.event(input.nav_save_as)
    async def _save_as_link():
        await _begin_save_as()

    def _show_bundle_dialog():
        """The scoped .hype download dialog (cloud Save; desktop saves into its project
        folder instead and has no portable-bundle action)."""
        title, btn = "Save project", "Save project"
        intro = ("Downloads a .hype file named after your project. Nothing stays on "
                 "the server, so save before you leave.")
        ui.modal_show(ui.modal(
            ui.p(intro, class_="hype-instr"),
            ui.input_radio_buttons(
                "save_scope", None,
                choices={
                    "full": ui.TagList(
                        ui.tags.b("Complete project"),
                        ui.div("Settings plus all computed data (terrain, water surface, "
                               "groundwater model, results). Includes an Aquaveo GMS "
                               "project when a groundwater run exists. Reopens exactly "
                               "where you left off.", class_="hype-dim")),
                    "light": ui.TagList(
                        ui.tags.b("Settings only"),
                        ui.div("Reach, boundaries, and all parameters (small file). Terrain "
                               "and model runs are re-run after opening.", class_="hype-dim")),
                },
                selected=("full" if _save_scope_full() else "light")),
            ui.div(".hype files are ZIP archives; rename one to .zip to browse its folders "
                   "in GIS.", class_="hype-instr hype-dim"),
            footer=ui.TagList(
                ui.modal_button("Cancel"),
                # The click starts the download client-side; the nonce just tells the server
                # to close the dialog (removing the modal doesn't cancel the transfer).
                ui.download_button("dl_save", btn, class_="btn-primary btn-sm",
                                   onclick="Shiny.setInputValue('save_dl_go', Date.now())")),
            title=title, easy_close=True))

    @reactive.effect
    @reactive.event(input.save_dl_go)
    def _save_dialog_close():
        ui.modal_remove()

    # suspend_when_hidden=False: the link lives inside the save dialog, which doesn't exist at
    # session start — with the default suspend, the href would only be computed after the
    # client reports the late-bound output visible, and the button would stay disabled until
    # then. Computing it eagerly lets shiny.js cache the value and apply it the moment the
    # modal's element binds. (The filename lambda + generator still run per download request,
    # so the scope radio is read at click time.)
    def _download_name() -> str:
        # Project name when set (sanitized), else the timestamp fallback of old.
        stem = project_meta.filename_stem(_ws["project_name"] or "",
                                          f"hype_project_{datetime.now():%Y%m%d_%H%M}")
        return f"{stem}{'' if _save_scope_full() else '_settings'}.hype"

    @output(suspend_when_hidden=False)
    @render.download(filename=_download_name)
    def dl_save():
        # GMS rides every Complete save with usable groundwater results (no toggle).
        # The live work_dir/GMS folder is the normal source; the reactive gate only
        # matters for the temp-rebuild fallback (e.g. right after importing a bundle).
        include_gms = _save_scope_full() and (
            (work_dir / "GMS").is_dir()
            or (run_result() is not None and "gw" not in _stale_marks()))
        yield from _stream_bundle(include_computed=_save_scope_full(),
                                  include_gms=include_gms)

    # ---- Open project (restore a saved .hype / downloaded project .zip) ----
    _open_seen: dict = {}      # last consumed upload datapath — the file input is re-created
    #                            per modal, so guard it the way _clicked_dynamic guards buttons

    def _busy_tasks():
        # gms_task belongs here too: it writes work_dir/GMS, and every work_dir rebind
        # (Open / Save As / New) requires no task holding a captured workspace path.
        return [t for t in (snap_task, reach_task, dem_task, delineate_task, carve_task,
                            wse_task, ras_task, mesh_prev_task, mesh_task, run_task, hz_task,
                            alt_task, soil_task, usgs_flow_task, gms_task)
                if _task_state(t) == "running"]

    # ---- Cross-project comparison workspace (desktop only) --------------------------------
    # A full-screen overlay over frozen, read-only snapshots of OTHER saved projects. It never
    # touches work_dir, the engines, or the map: sources are read through comparison.py, edits
    # live in a ComparisonCollectionV1, and persistence is a standalone .hypecompare file.
    # Keeping that boundary here makes it mechanically impossible for a comparison read to
    # switch or save the active model.
    def _comparison_path() -> Path | None:
        raw = comparison_file_v()
        return Path(str(raw)) if raw else None

    def _comparison_inspect(collection=None):
        collection = collection or comparison_collection_v()
        if collection is None:
            comparison_inspections_v.set({})
            return {}
        try:
            findings = comparison_mod.inspect_collection(
                collection, collection_path=_comparison_path())
        except Exception as exc:  # inspection never invalidates the frozen collection
            print(f"[comparison] source inspection failed: {exc}", flush=True)
            findings = {}
        comparison_inspections_v.set(findings)
        return findings

    def _comparison_set(collection, *, dirty: bool = True, autosave: bool = True):
        """Install a collection and atomically persist edits after its first Save."""
        path = _comparison_path()
        if dirty:
            collection = collection.model_copy(
                update={"updated_at": datetime.now(timezone.utc)})
        if dirty and autosave and path is not None:
            try:
                collection = comparison_mod.save_collection(collection, path)
                dirty = False
            except Exception as exc:  # retain dirty state so Back cannot lose the edit
                ui.notification_show(f"Comparison autosave failed: {exc}", type="warning",
                                     duration=8)
        comparison_collection_v.set(collection)
        comparison_dirty_v.set(bool(dirty))
        _comparison_inspect(collection)

    async def _comparison_title(collection=None):
        if not _shell_present():
            return
        collection = collection or comparison_collection_v()
        title = ((collection.name + " · Comparison · HYPE") if collection is not None
                 else "HYPE Desktop")
        await session.send_custom_message("hype_desktop", {"type": "setTitle", "title": title})

    async def _comparison_open_path(path: Path):
        if _busy_tasks():
            ui.notification_show("A model task is still running. Wait for it to finish before "
                                 "opening a comparison.", type="warning", duration=7)
            return
        try:
            collection = comparison_mod.load_collection(path)
        except Exception as exc:
            ui.notification_show(f"Couldn't open the comparison: {exc}", type="error",
                                 duration=10)
            recents.forget_comparison(path)
            _ensure_welcome()
            return
        ui.modal_remove()
        comparison_file_v.set(str(path.resolve()))
        comparison_collection_v.set(collection)
        comparison_dirty_v.set(False)
        comparison_selected_member_v.set(None)
        comparison_mode_v.set(True)
        recents.touch_comparison(path)
        cmp_recents_ver.set(cmp_recents_ver() + 1)
        _comparison_inspect(collection)
        await _comparison_title(collection)

    async def _comparison_start(*, add_current: bool):
        if _busy_tasks():
            ui.notification_show("A model task is still running. Wait for it to finish before "
                                 "comparing projects.", type="warning", duration=7)
            return
        collection = comparison_mod.new_collection()
        comparison_file_v.set(None)
        comparison_dirty_v.set(False)
        comparison_selected_member_v.set(None)
        comparison_mode_v.set(True)
        ui.modal_remove()
        if add_current:
            # The workspace compares SAVED projects: the current one seeds the roster only
            # when its main file carries current canonical results.
            why = None
            if not _ws["project_file"]:
                why = "it has not been saved yet"
            elif results_model() is None:
                why = "it has no hyporheic results yet"
            elif {"gw", "hz"} & set(_stale_marks()):
                why = "its results predate an input change"
            if why is None:
                try:
                    collection = comparison_mod.add_projects(
                        collection, [Path(_ws["project_file"])])
                    comparison_dirty_v.set(True)
                except Exception as exc:  # noqa: BLE001
                    ui.notification_show(f"The current project couldn't be added: {exc}",
                                         type="warning", duration=8)
            else:
                ui.notification_show(f"The open project wasn't added because {why}. "
                                     "Use Add projects to pick saved sites.",
                                     type="message", duration=8)
        comparison_collection_v.set(collection)
        _comparison_inspect(collection)
        await _comparison_title(collection)

    async def _comparison_save_to(path: Path, *, close_after: bool = False):
        collection = comparison_collection_v()
        if collection is None:
            return
        path = path.with_suffix(".hypecompare")
        try:
            saved = comparison_mod.save_collection(collection, path)
        except Exception as exc:
            ui.notification_show(f"Couldn't save the comparison: {exc}", type="error",
                                 duration=9)
            return
        comparison_file_v.set(str(path.resolve()))
        comparison_collection_v.set(saved)
        comparison_dirty_v.set(False)
        recents.touch_comparison(path)
        cmp_recents_ver.set(cmp_recents_ver() + 1)
        _comparison_inspect(saved)
        await _comparison_title(saved)
        if close_after:
            await _comparison_finish_back(discard=True)

    async def _comparison_finish_back(*, discard: bool = False):
        if not discard and comparison_dirty_v() and _comparison_path() is not None:
            await _comparison_save_to(_comparison_path())
            if comparison_dirty_v():
                return                     # the save failed and said so; stay in the workspace
        if not discard and comparison_dirty_v() and _comparison_path() is None:
            ui.modal_show(ui.modal(
                ui.p("Save this comparison collection before closing it?"),
                title="Unsaved comparison",
                footer=ui.TagList(
                    ui.input_action_button("comparison_back_cancel", "Cancel"),
                    ui.input_action_button("comparison_back_discard", "Discard"),
                    ui.input_action_button("comparison_back_save", "Save",
                                           class_="btn-primary")), easy_close=True))
            return
        comparison_mode_v.set(False)
        comparison_collection_v.set(None)
        comparison_file_v.set(None)
        comparison_dirty_v.set(False)
        comparison_selected_member_v.set(None)
        comparison_inspections_v.set({})
        await _comparison_title(None)
        await _post_title()
        if _gated():
            _show_welcome()

    async def _comparison_add_paths(paths):
        collection = comparison_collection_v()
        if collection is None:
            return
        incoming = [Path(str(path)) for path in paths if str(path)]
        remaining = max(0, 10 - len(collection.members))
        if not remaining:
            ui.notification_show("A comparison can contain up to 10 projects.",
                                 type="warning", duration=6)
            return
        if len(incoming) > remaining:
            incoming = incoming[:remaining]
            ui.notification_show(f"Only the first {remaining} selected projects were added "
                                 "(10-project limit).", type="warning", duration=7)
        try:
            updated = await anyio.to_thread.run_sync(
                lambda: comparison_mod.add_projects(
                    collection, incoming, comparison_path=_comparison_path()))
        except Exception as exc:
            ui.notification_show(f"Couldn't add projects: {exc}", type="error", duration=9)
            return
        _comparison_set(updated)

    @reactive.effect
    async def _sync_comparison_client():
        visible = bool(comparison_mode_v())
        collection = comparison_collection_v()
        dirty = bool(comparison_dirty_v())
        selected = comparison_selected_member_v()
        inspections = comparison_inspections_v()
        if not visible or collection is None:
            await session.send_custom_message("hype_comparison", {"visible": False})
            return
        payload = comparison_mod.comparison_ui_payload(collection, inspections=inspections)
        payload.update({"visible": True, "dirty": dirty,
                        "selected_member_id": selected})
        await session.send_custom_message("hype_comparison", payload)

    async def _pick_comparison(purpose: str, *, save: bool = False,
                               multiple: bool = False, directory: bool = False):
        """Pick comparison sources/collections/exports on the comparison-only route."""
        shell = _shell_present()
        mode = "native" if shell else ("tk" if runmode.picker_mode() == "auto" else "modal")
        if shell:
            _pending_pick["purpose"] = purpose
            if multiple:
                msg = {"type": "pickProjectsMultiple", "purpose": purpose}
            elif directory:
                msg = {"type": "pickComparisonExport", "purpose": purpose}
            else:
                collection = comparison_collection_v()
                stem = project_meta.filename_stem(
                    getattr(collection, "name", None), "Hydraulic comparison")
                msg = {"type": "pickComparisonSave" if save else "pickComparisonOpen",
                       "purpose": purpose, "fileName": f"{stem}.hypecompare"}
            await session.send_custom_message("hype_desktop", msg)
            return
        if mode == "tk":
            if _task_state(pick_task) == "running":
                ui.notification_show("A file picker window is already open. Look for it on "
                                     "your taskbar.", type="warning", duration=6)
                return
            collection = comparison_collection_v()
            stem = project_meta.filename_stem(
                getattr(collection, "name", None), "Hydraulic comparison")
            payload = {"mode": ("directory" if directory else
                                "open_multiple" if multiple else
                                "save" if save else "open"),
                       "kind": "comparison" if not multiple else "project",
                       "purpose": purpose,
                       "title": ("Choose comparison export folder" if directory else
                                 "Select HYPE projects to compare" if multiple else
                                 "Save HYPE comparison" if save else "Open HYPE comparison"),
                       "initial_file": f"{stem}.hypecompare" if save else "",
                       "initial_dir": _pick_initial_dir()}
            _pending_pick["purpose"] = purpose
            _pick_flight["purpose"] = purpose
            ui.notification_show("Opening a file window...", duration=4, id="pick_opening")
            _task_armed["pick"] = True
            pick_task(payload)
            return
        _show_comparison_typed_pick_modal(purpose)

    def _show_comparison_typed_pick_modal(purpose: str, *, value: str = "",
                                          error: str | None = None):
        _pending_pick["purpose"] = purpose
        multi = purpose == "comparison_add"
        directory = purpose == "comparison_export"
        instruction = ("Enter one full .hype path per line." if multi else
                       "Enter the export folder path." if directory else
                       "Enter the full .hypecompare file path.")
        body = [ui.p(instruction),
                ui.input_text_area("comparison_pick_path", None, value=value, width="100%",
                                   rows=4 if multi else 2)]
        if error:
            body.append(ui.div(error, class_="text-danger", style="font-size: 0.9em;"))
        ui.modal_show(ui.modal(
            *body, title="Comparison path",
            footer=ui.TagList(
                ui.input_action_button("comparison_pick_cancel", "Cancel"),
                ui.input_action_button("comparison_pick_go", "OK", class_="btn-primary")),
            easy_close=True))

    @reactive.effect
    def _comparison_pick_cancel():
        if _clicked_dynamic("comparison_pick_cancel"):
            ui.modal_remove()
            _pending_pick.pop("comparison_close_after_save", None)
            if not comparison_mode_v():
                _ensure_welcome()

    @reactive.effect
    async def _comparison_typed_pick():
        if not _clicked_dynamic("comparison_pick_go"):
            return
        purpose = str(_pending_pick.get("purpose") or "")
        raw = str(_safe("comparison_pick_path", "") or "").strip()
        if purpose == "comparison_add":
            paths = [Path(line.strip().strip('"')) for line in raw.splitlines()
                     if line.strip()]
            bad = [str(path) for path in paths if path.suffix.lower() != ".hype"
                   or not path.is_file()]
            if not paths or bad:
                _show_comparison_typed_pick_modal(
                    purpose, value=raw,
                    error="Every entry must be an existing .hype project file.")
                return
            ui.modal_remove()
            await _comparison_add_paths(paths)
            return
        path = Path(raw.strip('"')) if raw else None
        if path is None:
            _show_comparison_typed_pick_modal(purpose, value=raw, error="Enter a path.")
            return
        if purpose == "comparison_export":
            if not path.is_dir():
                _show_comparison_typed_pick_modal(
                    purpose, value=raw, error="Choose an existing folder.")
                return
        elif purpose == "comparison_open":
            if path.suffix.lower() != ".hypecompare" or not path.is_file():
                _show_comparison_typed_pick_modal(
                    purpose, value=raw, error="Choose an existing .hypecompare file.")
                return
        elif purpose.startswith("comparison_relink:"):
            if path.suffix.lower() != ".hype" or not path.is_file():
                _show_comparison_typed_pick_modal(
                    purpose, value=raw, error="Choose an existing .hype project file.")
                return
        ui.modal_remove()
        await _dispatch_picked_result({"purpose": purpose, "path": str(path),
                                       "cancelled": False})

    _comparison_recent: dict = {"items": []}

    async def _show_comparison_add_dialog():
        collection = comparison_collection_v()
        if collection is None:
            return
        present = set()
        for member in collection.members:
            try:
                present.add(str(Path(member.source_absolute).resolve()).lower())
            except OSError:
                present.add(str(member.source_absolute).lower())
        items = []
        for item in recents.load():
            try:
                key = str(Path(item["path"]).resolve()).lower()
            except OSError:
                key = str(item["path"]).lower()
            if key not in present and Path(item["path"]).is_file():
                items.append(item)
        _comparison_recent["items"] = items[:10]
        if not _comparison_recent["items"]:
            await _pick_comparison("comparison_add", multiple=True)
            return
        choices = {str(index): f"{item['name']} · {Path(item['path']).parent}"
                   for index, item in enumerate(_comparison_recent["items"])}
        ui.modal_show(ui.modal(
            ui.p("Add recent projects, or browse to projects stored elsewhere."),
            ui.input_checkbox_group("comparison_recent_projects", "Recent projects",
                                    choices, selected=[]),
            title="Add projects",
            footer=ui.TagList(
                ui.input_action_button("comparison_add_cancel", "Cancel"),
                ui.input_action_button("comparison_add_browse", "Browse files…"),
                ui.input_action_button("comparison_add_recent", "Add selected",
                                       class_="btn-primary")), easy_close=True))

    @reactive.effect
    def _comparison_add_cancel():
        if _clicked_dynamic("comparison_add_cancel"):
            ui.modal_remove()

    @reactive.effect
    async def _comparison_add_browse():
        if not _clicked_dynamic("comparison_add_browse"):
            return
        ui.modal_remove()
        await _pick_comparison("comparison_add", multiple=True)

    @reactive.effect
    async def _comparison_add_recent():
        if not _clicked_dynamic("comparison_add_recent"):
            return
        raw = _safe("comparison_recent_projects", []) or []
        selected = [raw] if isinstance(raw, str) else list(raw)
        paths = []
        for token in selected:
            try:
                paths.append(_comparison_recent["items"][int(token)]["path"])
            except (ValueError, TypeError, IndexError):
                continue
        if not paths:
            ui.notification_show("Select at least one recent project, or choose Browse files.",
                                 type="warning", duration=5)
            return
        ui.modal_remove()
        await _comparison_add_paths(paths)

    async def _comparison_export_to(folder: Path):
        collection = comparison_collection_v()
        if collection is None:
            return
        inspections = comparison_inspections_v()
        ui.notification_show("Building comparison export…", id="comparison_exporting",
                             duration=None)

        def _run_export():
            # The overview figure is matplotlib: pyplot is process-global, so exports share
            # the report builds' lock rather than racing them.
            with _REPORT_MPL_LOCK:
                return comparison_mod.generate_comparison_report(
                    collection, folder, include_pdf=True, inspections=inspections)

        try:
            paths = await anyio.to_thread.run_sync(_run_export)
        except Exception as exc:
            ui.notification_show(f"Couldn't export the comparison: {exc}", type="error",
                                 duration=10)
            return
        finally:
            ui.notification_remove("comparison_exporting")
        ui.notification_show(f"Exported {len(paths)} files to {folder}", duration=8)

    @reactive.effect
    @reactive.event(input.comparison_event)
    async def _comparison_event():
        msg = input.comparison_event() or {}
        kind = str(msg.get("type") or "")
        collection = comparison_collection_v()
        if not comparison_mode_v() or collection is None:
            return
        if kind == "add_projects":
            await _show_comparison_add_dialog()
            return
        if kind == "refresh":
            ui.notification_show("Refreshing captured hydraulic snapshots…",
                                 id="comparison_refreshing", duration=None)
            try:
                updated = await anyio.to_thread.run_sync(
                    lambda: comparison_mod.refresh_collection(
                        collection, collection_path=_comparison_path()))
                _comparison_set(updated)
                ui.notification_show("Comparison snapshots refreshed.", duration=5)
            except Exception as exc:
                ui.notification_show(f"Refresh failed: {exc}", type="error", duration=9)
            finally:
                ui.notification_remove("comparison_refreshing")
            return
        if kind == "save":
            if _comparison_path() is None:
                await _pick_comparison("comparison_save", save=True)
            else:
                await _comparison_save_to(_comparison_path())
            return
        if kind == "save_as":
            await _pick_comparison("comparison_save_as", save=True)
            return
        if kind == "export":
            await _pick_comparison("comparison_export", directory=True)
            return
        if kind == "back":
            await _comparison_finish_back()
            return
        if kind == "relink_member":
            member_id = str(msg.get("id") or "")
            if member_id:
                await _pick_comparison(f"comparison_relink:{member_id}")
            return
        if kind == "member_select":
            comparison_selected_member_v.set(str(msg.get("id") or "") or None)
            return

        dirty = False
        if kind in ("view", "axis_scale", "sort_order"):
            updates = {}
            if kind == "view" and msg.get("view") in ("overview", "metric", "relationships"):
                updates["view"] = msg["view"]
            elif kind == "axis_scale" and msg.get("axis_scale") in ("auto", "linear", "log"):
                updates["scale"] = msg["axis_scale"]
            elif kind == "sort_order" and msg.get("sort_order") in \
                    ("added", "ascending", "descending"):
                updates["order"] = msg["sort_order"]
            if updates:
                collection = collection.model_copy(update={
                    "view_settings": collection.view_settings.model_copy(update=updates)})
                dirty = True
        elif kind in ("metric_add", "metric_remove"):
            # The Metric tab holds a LIST of aligned panels; the client mirrors the 6-panel
            # cap, and a refused add still pokes the reactive so an optimistic render can
            # never drift from the collection.
            metric_id = str(msg.get("metric_id") or "")
            ids = list(collection.view_settings.metric_ids)
            if (kind == "metric_add" and metric_id in comparison_metrics.METRICS_BY_ID
                    and metric_id not in ids):
                if len(ids) >= 6:
                    ui.notification_show("Up to 6 metric panels can be shown at once. "
                                         "Remove one to add another.", type="warning",
                                         duration=6)
                    comparison_collection_v.set(collection.model_copy())
                    return
                ids.append(metric_id)
            elif kind == "metric_remove" and metric_id in ids:
                ids.remove(metric_id)
            if ids != list(collection.view_settings.metric_ids):
                collection = collection.model_copy(update={
                    "view_settings": collection.view_settings.model_copy(
                        update={"metric_ids": ids})})
                dirty = True
        elif kind in ("member_include", "remove_member", "member_alias"):
            member_id = str(msg.get("id") or "")
            members = []
            for member in collection.members:
                if str(member.member_id) != member_id:
                    members.append(member)
                    continue
                if kind == "remove_member":
                    continue
                if kind == "member_include":
                    member = member.model_copy(update={"included": bool(msg.get("included"))})
                else:
                    alias = str(msg.get("alias") or "").strip() or None
                    member = member.model_copy(update={"alias": alias})
                members.append(member)
            if members != collection.members:
                collection = collection.model_copy(update={"members": members})
                dirty = True
                if kind == "remove_member" and comparison_selected_member_v() == member_id:
                    comparison_selected_member_v.set(None)
        if dirty:
            _comparison_set(collection)

    @reactive.effect
    async def _comparison_back_cancel():
        if _clicked_dynamic("comparison_back_cancel"):
            ui.modal_remove()

    @reactive.effect
    async def _comparison_back_discard():
        if not _clicked_dynamic("comparison_back_discard"):
            return
        ui.modal_remove()
        await _comparison_finish_back(discard=True)

    @reactive.effect
    async def _comparison_back_save():
        if not _clicked_dynamic("comparison_back_save"):
            return
        ui.modal_remove()
        _pending_pick["comparison_close_after_save"] = True
        await _pick_comparison("comparison_save", save=True)

    def _dem_src_picked(path: Path):
        """A picked local-DEM raster: link it as THE terrain source and import right away
        when a reach exists (pick = import in one step); without a reach (or drainage
        area) the auto-chain imports the moment those land. First pick, re-pick, and
        locate all funnel through here."""
        if path.suffix.lower() not in dem.DEM_SUFFIXES:
            ui.notification_show("Use a GeoTIFF (.tif or .tiff).", type="warning",
                                 duration=6)
            return
        with reactive.isolate():
            rec = dict(dem_src() or {})
        rec.update(mode="local", path=str(path), src_mtime=None)
        dem_src.set(rec)
        rf = reach_feat()
        if rf is None:
            ui.notification_show("Raster linked. Draw the reach centerline to import "
                                 "the terrain.", duration=6)
            return
        if delineate_mode() == "manual" and not _manual_da_valid():
            ui.notification_show("Raster linked. Enter the drainage area (km²) to "
                                 "import the terrain.", duration=6)
            return
        if _task_state(dem_task) == "running":
            try:                # a queued import runs after the cancelled fetch unwinds
                dem_task.cancel()
            except Exception:  # noqa: BLE001
                pass
        _launch_dem_fetch(rf)

    @reactive.effect
    @reactive.event(input.dem_choose_evt)
    async def _dem_choose_click():
        await _pick_dem_raster()

    async def _add_map_layers(paths):
        """Append picked files to Map layers as path-pointer records (never a copy).
        Dedupes by resolved path; unsupported suffixes get a notification and are skipped."""
        with reactive.isolate():
            recs = [dict(r) for r in map_layers()]

        def _pkey(p):
            try:
                return os.path.normcase(str(Path(p).resolve()))
            except OSError:
                return os.path.normcase(str(p))
        seen = {_pkey(r["path"]) for r in recs}
        added, bad = [], False
        for p in paths:
            if ml_mod.classify_path(p) is None:
                bad = True
                continue
            k = _pkey(p)
            if k in seen:
                ui.notification_show(f"{Path(p).name} is already in Map layers.",
                                     type="warning", duration=6)
                continue
            seen.add(k)
            added.append(ml_mod.new_layer_record(p))
        if bad:
            ui.notification_show(pathpick.MSG_REF_KIND, type="warning", duration=8)
        if added:
            if not _node_checked("maplyr"):
                _check_state["maplyr"] = True     # adding always shows itself (dem precedent)
            map_layers.set(recs + added)

    async def _relink_map_layer(uid: str, path: Path):
        """Point an existing layer record at the file's new location (missing-file repair)."""
        with reactive.isolate():
            rec = next((r for r in map_layers() if r.get("id") == uid), None)
        if rec is None:
            return
        kind = ml_mod.classify_path(path)
        if kind is None:
            ui.notification_show(pathpick.MSG_REF_KIND, type="warning", duration=8)
            return
        if kind != rec.get("kind"):
            want = ("a raster file (.tif, .tiff, .vrt)" if rec.get("kind") == "raster"
                    else "a vector file (.shp, .geojson, .json)")
            ui.notification_show(f"This layer links {want}. Pick the same kind of file.",
                                 type="warning", duration=8)
            return
        rec["path"] = str(path)                   # in place; the owner effect reloads it
        _ml_cache.pop(uid, None)
        with reactive.isolate():
            map_layers_ver.set(map_layers_ver() + 1)
            _ml_paint.set(_ml_paint() + 1)

    async def _dispatch_picked_result(res: dict, fallback_purpose: str = ""):
        """Route native/Tk/typed picker replies without ever sending comparison sources
        through the normal project-open dispatcher."""
        purpose = str(res.get("purpose") or fallback_purpose or "")
        paths = [Path(str(path)) for path in (res.get("paths") or []) if str(path)]
        raw_path = res.get("path")
        if res.get("cancelled") or (not paths and not raw_path):
            _pending_pick.pop("comparison_close_after_save", None)
            if purpose == "example_target":
                # The Save-to chooser came off the start page's example detail: go back
                # there (gated or not; the page was open either way).
                _show_welcome("examples", example=_welcome.get("example"))
                return
            if not comparison_mode_v():
                _ensure_welcome()          # exactly the pre-comparison cancel contract
            return
        # Map-layer purposes MUST branch before the _on_project_path fallthrough below —
        # that dispatcher would silently eat any purpose it doesn't recognize.
        if purpose == "maplayer_add":
            await _add_map_layers(paths or ([Path(str(raw_path))] if raw_path else []))
            return
        if purpose.startswith("maplayer_relink:"):
            if raw_path:
                await _relink_map_layer(purpose.partition(":")[2], Path(str(raw_path)))
            return
        if purpose == "dem_src_pick":
            if raw_path:
                _dem_src_picked(Path(str(raw_path)))
            return
        if purpose == "comparison_add":
            await _comparison_add_paths(paths)
            return
        if purpose == "comparison_open":
            await _comparison_open_path(Path(str(raw_path)))
            return
        if purpose in ("comparison_save", "comparison_save_as"):
            close_after = bool(_pending_pick.pop("comparison_close_after_save", False))
            await _comparison_save_to(Path(str(raw_path)), close_after=close_after)
            return
        if purpose == "comparison_export":
            await _comparison_export_to(Path(str(raw_path)))
            return
        if purpose.startswith("comparison_relink:"):
            member_id = purpose.partition(":")[2]
            collection = comparison_collection_v()
            if collection is None:
                return
            member = next((m for m in collection.members
                           if str(m.member_id) == member_id), None)
            if member is None:
                return
            try:
                replacement = comparison_mod.relink_member(
                    member, Path(str(raw_path)), collection_path=_comparison_path(),
                    refresh=True)
                members = [replacement if str(m.member_id) == member_id else m
                           for m in collection.members]
                _comparison_set(collection.model_copy(update={"members": members}))
            except Exception as exc:
                ui.notification_show(f"Couldn't relink that source: {exc}", type="error",
                                     duration=9)
            return
        await _on_project_path(purpose, Path(str(raw_path)))

    @reactive.effect
    @reactive.event(input.nav_start)
    async def _start_dialog():
        """Header Projects: the START PAGE (New, Open, Example projects, recents, What's new),
        never a file dialog.

        The header used to carry New and Open as two doors, and Open once branched straight to
        the picker (desktop) or the upload modal (cloud), skipping the one screen that offers
        recents and New side by side. `_show_welcome` is deliberate over `_ensure_welcome`: that
        one no-ops when a project is open, which is what every cancel funnel relies on."""
        if _busy_tasks():
            ui.modal_show(ui.modal(
                ui.p("A task is still running. Wait for it to finish (or cancel it) before "
                     "opening a project."),
                title="Projects", easy_close=True))
            return
        _show_welcome()

    def _show_open_modal():
        """Cloud Open (upload). Under the startup gate it is not dismissable and carries
        a Cancel that funnels back to the welcome; once a project exists it behaves like
        any other dialog."""
        body = (
            ui.p("Open a saved HYPE project (.hype, or a project .zip saved by an older "
                 "version). This replaces everything in the current session."),
            ui.input_file("open_project", None, accept=[".hype", ".zip"], multiple=False,
                          button_label="Browse…", placeholder="No file selected",
                          width="100%"))
        if _gated():
            ui.modal_show(ui.modal(*body, title="Open project", easy_close=False,
                                   footer=ui.input_action_button("open_cancel", "Cancel")))
        else:
            ui.modal_show(ui.modal(*body, title="Open project", easy_close=True))

    @reactive.effect
    def _open_cancel():
        if _clicked_dynamic("open_cancel"):
            ui.modal_remove()
            _ensure_welcome()

    @reactive.effect
    @reactive.event(input.open_project)
    async def _open_project_upload():
        up = input.open_project()
        if not up:
            return
        dp = up[0].get("datapath")
        if not dp or _open_seen.get("dp") == dp:
            return
        _open_seen["dp"] = dp
        if _busy_tasks():                  # re-check — the modal may have sat open a while
            ui.notification_show("A task is still running. Wait for it to finish before "
                                 "opening a project.", type="warning", duration=6)
            return
        fb_name = Path(str(up[0].get("name") or "")).stem or None   # pre-metadata fallback
        ui.modal_remove()
        ui.notification_show("Opening project…", duration=None, id="open_prog")
        try:
            await _apply_project(dp, fallback_name=fb_name)
            ui.notification_show("Project opened. Pick up where you left off.", duration=6)
        except bundle.ProjectError as e:
            ui.notification_show(str(e), type="error", duration=10)
            _ensure_welcome()              # the wipe already ran: re-gate, never strand
        except Exception as e:  # noqa: BLE001 — a failed restore must never kill the session
            ui.notification_show(f"Couldn't open the project: {e}", type="error", duration=10)
            _ensure_welcome()
        finally:
            ui.notification_remove("open_prog")

    async def _apply_project(zip_path, *, fallback_name: str | None = None):
        """Restore a saved session: wipe, extract, set every reactive in ONE flush with the
        non-reactive guards stamped, rebuild the raster layers, re-apply saved visibility,
        land unselected with the tree collapsed. Everything else (tree, stage bar, decor
        vectors, panes, 3-D terrain) rehydrates itself from the restored values after the
        flush."""
        await _reset_session_state()
        payload = bundle.restore_workspace(zip_path, work_dir)
        await _rehydrate(payload, fallback_name=fallback_name)

    async def _rehydrate(payload: dict, *, fallback_name: str | None = None,
                         keep_selection: bool = False):
        """Set every reactive from a restore payload (restore_workspace / restore_in_place /
        the Save As snapshot). Callers reset memory state first; detokenization runs
        against the CURRENT work_dir, so this is what heals paths after a rebind.
        `fallback_name` seeds the project name for pre-metadata bundles (upload stem).
        Opening a saved project lands unselected with every tree group collapsed;
        `keep_selection` (Save As — same session, new folder) re-lands on the saved
        selection instead."""
        _autosave["restoring"] = True
        session.on_flushed(lambda: _autosave.update(restoring=False), once=True)
        st = _detokenize_paths(payload.get("state") or {})
        vec = payload.get("vectors") or {}

        # geometry + provenance
        reach = vec.get("reach")
        if reach is not None:
            # An opened project's direction is settled — pre-seed so only fresh draws and
            # edits re-run the NHD/terrain direction check (the restore pre-seed pattern).
            _reach_dir["sig"] = _dir_sig(reach)
            _reach_dir["verdict"] = "ok"
        reach_feat.set(reach)
        auto_meta.set(st.get("auto_meta"))
        if st.get("delineate_mode") in ("auto", "manual"):
            delineate_mode.set(st["delineate_mode"])
        b_up, b_left = vec.get("upstream"), vec.get("left")
        b_right, b_down = vec.get("right"), vec.get("downstream")
        up_feat.set(b_up); left_feat.set(b_left); right_feat.set(b_right); down_feat.set(b_down)
        # THE cascade guard: restored boundaries must not read as "edited" on the next flush,
        # or _ras_stale_on_edit discards every result being restored right now.
        _ras_inputs_sig["sig"] = tuple(id(f) for f in (b_up, b_left, b_right, b_down))
        wse_extent_feat.set(vec.get("wse_extent"))
        kzone_feats.set(vec.get("k_zones") or [])   # re-normalized below, after _kept restores
        gen_r = int(st.get("reach_gen") or (1 if reach else 0))
        gen_d = int(st.get("dem_gen") or 0)
        reach_gen.set(gen_r); dem_gen.set(gen_d)
        _chain["dem"] = gen_r; _chain["bnd"] = gen_d   # no auto re-fetch/regenerate on open

        # parameters — before anything reads _safe/_keep; registry values older than the
        # stamp are stale (previous session) and lose to these (see _safe)
        _kept.clear()
        _kept.update(st.get("kept") or {})
        _migrate_pollutant_keys()
        _restore_stamp["t"] = time.monotonic()
        # project identity: stored name wins, else fallback (upload filename stem). On
        # desktop the main file's stem IS the name — Save As lands here with the new stem
        # already adopted, renaming the project; created rides the restored state.
        _pm = project_meta.meta_from_state(st, fallback_name=fallback_name)
        if runmode.IS_DESKTOP and _ws["project_file"]:
            _pm["name"] = Path(_ws["project_file"]).stem
        _set_project_meta(_pm["name"], _pm["created"], _pm["units"],
                          project_id=_pm.get("project_id"), site_id=_pm.get("site_id"),
                          mint_missing=True)
        # gradient boundary conditions: migrate legacy kept modes (4-corner, structured text)
        # onto the points model in place; a saved grad_pts list always wins over legacy text
        from hype_app import gradients as _grad_mod
        grad_pts.set(_grad_mod.migrate_kept_gradients(_kept, st.get("grad_pts")))
        grad_adding.set(None)
        _ws_wells = wells_mod.normalize_wells(st.get("obs_wells"))   # older saves lack the keys
        obs_wells.set(_ws_wells)
        well_pairs.set(wells_mod.normalize_pairs(st.get("well_pairs"),
                                                 {w["id"] for w in _ws_wells}))
        wells_adding.set(False)
        # Reference layers: pointers restore as-is; a MISSING file stays in the list (the
        # owner effect marks the row "file is missing" and the warn button relinks it).
        map_layers.set(ml_mod.normalize_map_layers(st.get("map_layers")))
        with reactive.isolate():
            map_layers_ver.set(map_layers_ver() + 1)
        # K-zones from pre-per-zone-K saves are bare geometry: give them uids + the save's
        # effective global Zone KH/KV pair (now in _kept) so nothing changes value.
        with reactive.isolate():
            kzone_feats.set(geometry.normalize_kzone_features(kzone_feats(), **_kz_defaults()))
        _rs_ov = st.get("ref_slope_override")
        ref_slope_override.set(float(_rs_ov) if _rs_ov is not None else None)

        # terrain
        dem_p = work_dir / "inputs" / "dem.tif"
        dem_path.set(str(dem_p) if dem_p.is_file() else None)
        dem_meta.set(st.get("dem_meta"))
        # A missing source file never blocks the restored terrain: the working DEM is in
        # the project; the pointer only drives the pane's warn card + re-import.
        dem_src.set(dem.normalize_dem_source(st.get("dem_source")))
        cm = st.get("carve_meta")
        if cm and cm.get("path") and Path(cm["path"]).is_file():
            carve_meta.set(cm)
            carve_active.set(bool(st.get("carve_active")))
        dem_hs_v.set(float(st.get("dem_hs") or 8.0))
        dem_opacity_v.set(float(st.get("dem_opacity") or 0.8))
        if st.get("dem_stretch"):
            dem_stretch_v.set(tuple(st["dem_stretch"]))
        if st.get("origin_override") is not None:
            origin_override.set(float(st["origin_override"]))
        with reactive.isolate():
            ad = active_dem()
        if ad:
            _show_dem_overlay(ad)
            with reactive.isolate():
                if carve_active():
                    _show_carve_overlay(cm)

        # water surface — a saved wse_mode is ignored: the draw/upload paths were removed from
        # the UI and the reset above already pinned "model". Restored wse_extent/wse artifacts
        # are harmless (every consumer is mode-guarded); an old draw/upload project just needs
        # a surface-model run before its next groundwater run.
        ras_opacity_v.set(float(st.get("ras_opacity") or 0.7))
        rr = st.get("ras_result")
        if rr and rr.get("depth_tif") and Path(rr["depth_tif"]).is_file():
            _show_ras_overlays(rr)
            ras_result.set(rr)
            # The 2D mesh preview is session-only by design (never saved) — rebuild it from
            # the run's own geometry file so the tree's checked "2D mesh" row has something
            # to show on a reopened project. No mesher rerun: h5 read + rasterize, off-loop;
            # the task lands AFTER the restore flush (outside the bursty-add drop window)
            # and failure is log-only via the _mesh_preview_done auto path. cell_size_m -1.0
            # is a sentinel no real size matches, so the next run's auto-mesh rebuilds.
            _gh5 = Path(rr.get("project_dir") or "") / "Geometries" / "Geometry.h5"
            _crs = f"EPSG:{rr['epsg']}" if rr.get("epsg") else None
            if _crs is None:
                try:
                    import rasterio
                    with rasterio.open(rr["depth_tif"]) as _src:
                        _crs = _src.crs.to_wkt() if _src.crs else None
                except Exception:  # noqa: BLE001
                    _crs = None
            if _gh5.is_file() and _crs:
                try:
                    _mesh_auto["on"] = True
                    mesh_prev_task({"from_h5": str(_gh5), "crs": _crs,
                                    "cell_size_m": -1.0})
                except Exception:  # noqa: BLE001 — the mesh is a nicety; restore stands
                    _mesh_auto["on"] = False
        if st.get("wse_used"):
            _wse_used["path"] = st["wse_used"]

        # groundwater run + results (display prefs first — the builders read them)
        head_opacity_v.set(float(st.get("head_opacity") or 0.85))
        hd_contours_v.set(bool(st.get("head_contours", True)))
        fp_line_show_v.set(bool(st.get("fp_line_show", True)))
        fp_line_weight_v.set(float(st.get("fp_line_weight") or 1.0))
        fp_line_opacity_v.set(float(st.get("fp_line_opacity") or 0.9))
        _flm = st.get("fp_line_mode")
        if _flm in ("solid", "single"):
            # Retired vocabulary ("single" from v1.0.0 saves, "solid" from the short-lived
            # custom-color mode): custom line colors are gone, so both mean the class
            # identity colors. Any saved fp_line_color is simply ignored.
            _flm = "class"
        if _flm in FP_LINE_MODES:
            fp_line_mode_v.set(_flm)
        # frozen run snapshot: prefer config/assessment_input.json, fall back to the state copy
        # (None for v1 projects — the legacy adapter).
        _snap_in = payload.get("assessment_input") or st.get("input_snapshot")
        input_snapshot.set(_snap_in)
        # Repopulate the site-metadata inputs from the frozen snapshot so a reopened project can
        # regenerate an identical report — the accordion is collapsed at save time, so these
        # widgets otherwise come back empty even though the data is in the snapshot.
        _site = (_snap_in or {}).get("site") or {}
        for _iid, _key in (("site_name", "site_name"), ("site_analyst", "analyst"),
                           ("site_org", "organization"), ("site_notes", "notes")):
            if _site.get(_key):
                _kept[_iid] = _site[_key]
        if _site.get("assessment_date"):
            _kept["site_date"] = str(_site["assessment_date"])
        flow_lookup.set(st.get("flow_lookup"))
        flow_source.set(st.get("flow_source"))
        soil_snapshot.set(st.get("soil_snapshot"))
        soil_overrides.set(st.get("soil_overrides") or [])
        soil_source.set(st.get("soil_source"))
        soil_sel_units.set(frozenset((st.get("soil_source") or {}).get("units") or []))
        soil_inspect.set(None)                 # transient, never restored
        # migrate() upgrades older results payloads (e.g. popping the retired sensitivity
        # field) — HypeModel is extra="forbid", so every later model_validate would reject
        # an unmigrated dict.
        _rm = st.get("results_model")
        if _rm:
            try:
                from hype_app.contracts import migrate as _migrate
                _rm = _migrate("assessment-results", _rm)
            except Exception:  # noqa: BLE001
                pass
        results_model.set(_rm)
        # Settings-only archives carry no model/sensitivity files — those stages come back
        # not-done rather than "done" with nothing on disk. After an EXTRACTION, gate on what
        # restore_workspace actually wrote, NOT a disk probe: right after the session wipe,
        # Windows can keep just-deleted dirs visible (delete-pending) until their handles
        # drain. An in-place open (restored is None) deleted nothing, so there the sibling
        # folders on disk ARE the truth.
        _restored = payload.get("restored")

        def _present(rel: str) -> bool:
            if _restored is not None:
                return any(p.startswith(rel) for p in _restored)
            d = work_dir / rel.rstrip("/")
            try:
                return d.is_dir() and next(d.iterdir(), None) is not None
            except OSError:
                return False

        # Alternatives: gated on the folder AND the restored snapshot still matching the
        # manifest's Basecase (a mixed restore must not present stale ranges as current).
        # A halted or mid-run save can carry pending/running rows the reopened session can
        # never resume — normalize them to not_run.
        alt_view.set(None)
        _alt_stats_cache.clear()
        _ar = st.get("alt_result") if _present("alternatives/") else None
        _amf = (_ar or {}).get("manifest")
        if _amf:
            _normalize_alt_manifest(_amf)      # batches saved by earlier builds still load
            _cur_hash = (_snap_in or {}).get("input_hash")
            if _amf.get("base_input_hash") and _cur_hash \
                    and _amf["base_input_hash"] != _cur_hash:
                _ar = None
            else:
                for _row in _amf.get("scenarios") or []:
                    if _row.get("status") in ("pending", "running"):
                        _row["status"] = "not_run"
                _ar = {"manifest": _amf, "running": False}
        else:
            _ar = None
        alt_result.set(_ar)
        rn = st.get("run_result")
        if rn and _present("model/gwf_workspace/"):
            run_result.set(rn)
            if _HAS_MAP:
                try:
                    _show_run_layers(rn)
                    hl = int(st.get("head_layer") or 1)
                    if hl != 1:
                        head_layer_v.set(hl)
                        _render_head_layer(hl)
                except Exception as e:  # noqa: BLE001
                    ui.notification_show(f"Project opened; the groundwater layers couldn't "
                                         f"be drawn: {e}", type="warning", duration=8)

        # hyporheic zone
        hz = st.get("hz_result")
        if hz and hz.get("hz_dir") and Path(hz["hz_dir"]).is_dir():
            # Seed the auto-open marker with the restored run's hash BEFORE hz_result flips:
            # reopening a project must not regenerate + pop the report modal mid-restore (the
            # multi-MB modal send raced the raster widget pushes and could kill the websocket
            # via the legacy-protocol concurrent-drain assertion; the Site Report node is the
            # manual home). A NEW delineation still auto-opens — its hash differs.
            _report_shown_for.set((_snap_in or {}).get("input_hash"))
            hz_result.set(hz)
            try:
                await _show_hz_layers(hz["hz_dir"])
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"Project opened; the hyporheic-zone layers couldn't "
                                     f"be drawn: {e}", type="warning", duration=8)
        _stale_marks.set(frozenset(st.get("stale_marks") or ()))
        # (Older saves carried a per-project comparison roster; standalone .hypecompare
        # collections replaced it, so that state key is simply ignored here.)

        # Restore drops GMS arcs by design (one-way export) and Save As copies the folder
        # wholesale; heal the GMS export whenever usable groundwater artifacts arrived
        # without one, and seed the status line from disk when it did travel.
        with reactive.isolate():
            if (work_dir / "GMS").is_dir():
                gms_status_v.set(
                    {"ok": not (work_dir / "GMS" / "EXPORT_ERROR.txt").exists()})
            elif (next((work_dir / "model" / "gwf_workspace").glob("*.dis.grb"), None)
                    is not None
                    and run_result() is not None
                    and "gw" not in _stale_marks()):
                _request_gms_build(include_hz=hz_result() is not None)

        # visibility + selection — saved intent overrides the fresh-result defaults the
        # rebuild helpers just applied
        _check_state.clear()
        _check_state.update(st.get("check_state") or {})
        hid = [k for k in (st.get("hidden_keys") or []) if isinstance(k, str)]
        if hid:
            _set_keys_visible(hid, False)
        _bump_vis()
        sel_node.set(None)                 # sets dedupe by identity: force a pane remount
        if keep_selection:
            nid = st.get("sel_node")
            _select(ui_tree.resolve_node(nid) or "reach")
        else:
            # Opening a saved project lands with nothing selected (no props card — the saved
            # sel_node is ignored) and every tree group collapsed. The STEP is still restored:
            # it drives the map machinery, and pinning Reach here armed the NHD flowline fetch
            # at the fly-in zoom and skipped the static mirrors. Legacy saves without the key
            # fall back to the saved selection's step, then Reach.
            stp = st.get("current_step")
            if stp not in ui_tree.STEP_STAGE:
                stp = ui_tree.node_step(st.get("sel_node")) or STEP_REACH
            current_step.set(stp)
            # Re-mirror the statics imperatively: _sync_map_shapes fires only on step
            # CHANGES, and the restored step can equal the pre-open one (fresh page = Reach),
            # which left the reach/boundary lines undrawn. Boundaries-step saves skip it —
            # _refresh_boundary_display re-renders from the just-set features there.
            if stp != STEP_BOUNDARIES:
                _mirror_shown.clear()
                with reactive.isolate():
                    _mirror_features_as_layers()
            await session.send_custom_message(
                "hype_tree_collapse", {"groups": list(ui_tree.GROUP_IDS)})
        try:
            b = _node_bounds("bnd") or _node_bounds("reach")
            if b:
                await session.send_custom_message("hype_fly", {"bounds": b})
        except Exception:  # noqa: BLE001
            pass
        # Every 3-D content send lives in a compute-time completion effect, so without
        # this a restored project shows bare terrain in the 3-D view: no grid, no
        # basemap drape, no classed paths, no volumes. Runs AFTER check_state is
        # restored so the vis pushes reflect the saved checkboxes.
        try:
            await _rebuild_3d_scene()
        except Exception:  # noqa: BLE001 — 3-D is a view of the results, never a gate
            pass

    # ---- Desktop project folders (runmode.IS_DESKTOP) ----------------------------------
    # GMS-style: the folder holding the main .hype IS the workspace — sessions run in place,
    # Save rewrites the main file, nothing is deleted on close. Native pickers come from the
    # shell bridge (www/desktop_bridge.js ↔ MainForm); in a plain dev browser the bridge is
    # absent and a typed-path modal stands in. Every rebind requires _busy_tasks() empty —
    # ExtendedTasks hold a captured str(work_dir).
    _PROJECT_DIRS = bundle.PROJECT_DIRS    # folder-layout contract lives in bundle.py
    _pending_pick: dict = {}       # purpose of the in-flight picker / fallback modal
    _pending_import: dict = {}     # source .hype awaiting an import-target choice

    def _shell_present() -> bool:
        return bool(_safe("desktop_shell", None))

    # One log line when the shell bridge attaches — lands in the shell's hype.log, so
    # "did native-dialog detection work?" is answerable from logs alone.
    _bridge_seen: dict = {}

    @reactive.effect
    @reactive.event(input.desktop_shell)
    async def _bridge_attached():
        if input.desktop_shell() and not _bridge_seen.get("logged"):
            _bridge_seen["logged"] = True
            print("[desktop] shell bridge attached", flush=True)
        # Re-assert the window title on every (re)attach — after a WebView reload the shell
        # still shows the lost session's project title. Idempotent (empty title = app name).
        await _post_title()

    async def _post_title():
        """Window title follows the open project (shell builds show '<name> — HYPE Desktop')."""
        if _shell_present():
            pf = _ws["project_file"]
            await session.send_custom_message(
                "hype_desktop", {"type": "setTitle", "title": Path(pf).stem if pf else ""})

    def _warn_path_advisories(folder: Path):
        s = str(folder)
        if len(s) > 140:
            ui.notification_show("That path is quite long. The solvers can hit the Windows "
                                 "260-character limit inside deeply nested model folders; a "
                                 "shorter path is safer.", type="warning", duration=8)
        od = os.environ.get("OneDrive") or os.environ.get("OneDriveConsumer")
        if od:
            try:
                Path(s).resolve().relative_to(Path(od).resolve())
                ui.notification_show("That folder is inside OneDrive. Pause syncing during "
                                     "model runs (sync locks can break solver writes).",
                                     type="warning", duration=8)
            except (ValueError, OSError):
                pass

    # ---- Startup gate: RAS2025-style start page (both run modes) ----------------------
    # Hard gate by design: the user must create or open a project before entering the app.
    # Desktop: a project = a folder (model runs are heavy; everything saves in place), so
    # there is no unsaved desktop session. Cloud: a project = a name (nothing persists
    # server-side; the name titles the session and the Save download). Every cancel/error
    # exit from the project dialogs funnels back via _ensure_welcome, so a project-less
    # session is never left dialog-less.
    #
    # The start page is ONE modal with three columns (rail: New / Open / Example projects;
    # center: recent projects; right: what's new) and two views: "home" and "examples" (the
    # gallery + detail, same shell, Back returns home). It opens off the client's map-ready
    # ping (_welcome_gate) and from the header's Projects link.
    _welcome = {"recents": [], "view": "home", "example": None}  # snapshot behind the rows

    def _gated() -> bool:
        if runmode.IS_DESKTOP:
            return _ws["project_file"] is None
        return _ws["project_name"] is None

    def _ensure_welcome():
        if _gated():
            _show_welcome()

    def _welcome_when(iso: str) -> str:
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            secs = (datetime.now(timezone.utc) - dt).total_seconds()
        except ValueError:
            return ""
        if secs < 90:
            return "just now"
        if secs < 3600:
            return f"{int(secs // 60)} min ago"
        if secs < 172800:
            return f"{int(secs // 3600)} h ago"
        if secs < 86400 * 14:
            return f"{int(secs // 86400)} days ago"
        return dt.astimezone().strftime("%b %d, %Y")

    def _nonce_js(evt_id: str, **fields) -> str:
        """Inline onclick that posts a nonce'd event input (the _evt_btn pattern) with optional
        extra fields. Only indices ever ride in here, never paths (backslash-escape trap)."""
        extra = "".join(f"{k}: {v}, " for k, v in fields.items())
        return (f"Shiny.setInputValue('{evt_id}', {{{extra}n: Date.now() + Math.random()}}, "
                "{priority: 'event'})")

    def _start_tile(evt_id: str, title: str, sub: str, icon: str, *,
                    primary: bool = False, active: bool = False):
        cls = "hype-start-tile" + (" is-primary" if primary else "") + (" is-active" if active else "")
        return ui.tags.button(
            ui.span(class_=f"hype-start-tile-ic ic-{icon}"),
            ui.span(ui.span(title, class_="hype-start-tile-t"),
                    ui.span(sub, class_="hype-start-tile-s"),
                    class_="hype-start-tile-txt"),
            type="button", class_=cls, title=title, onclick=_nonce_js(evt_id))

    def _recent_groups(items: list[dict]) -> list[tuple[str, list[tuple[int, dict]]]]:
        """RAS2025's buckets for everything below the featured card: Today / Last 7 days /
        Older by last_opened, newest-first inside each, empty buckets dropped. Carries the
        item's index in the SNAPSHOT so the rows keep firing positional events."""
        now = datetime.now(timezone.utc)
        today = now.astimezone().date()
        buckets: dict[str, list[tuple[int, dict]]] = {"Today": [], "Last 7 days": [], "Older": []}
        for i, it in enumerate(items):
            if i == 0:
                continue                      # the featured card
            key = "Older"
            try:
                dt = datetime.fromisoformat(it["last_opened"])
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                if dt.astimezone().date() == today:
                    key = "Today"
                elif (now - dt).total_seconds() < 7 * 86400:
                    key = "Last 7 days"
            except (ValueError, TypeError):
                pass
            buckets[key].append((i, it))
        return [(k, v) for k, v in buckets.items() if v]

    def _start_recent_row(i: int, it: dict):
        return ui.tags.button(
            ui.span(it["name"], class_="hype-welcome-name"),
            ui.span(str(Path(it["path"]).parent), class_="hype-welcome-dir"),
            ui.span(_welcome_when(it["last_opened"]), class_="hype-welcome-when"),
            # a span, not a button — the row is a <button> and HTML forbids nesting
            # buttons; stopPropagation keeps the row's open-onclick out
            ui.span("×", class_="hype-welcome-rm", title="Remove from recent projects",
                    onclick="event.stopPropagation(); " + _nonce_js("welcome_recent_rm", i=i)),
            type="button", class_="hype-welcome-row hype-start-row", title=it["path"],
            # idx only — never a path — goes into inline JS (backslash-escape trap)
            onclick=_nonce_js("welcome_recent", i=i))

    def _start_home_columns(items: list[dict]):
        """Center (recent projects) + right (what's new) of the home view."""
        if runmode.IS_DESKTOP:
            if items:
                feat = items[0]
                featured = ui.div(
                    ui.div(ui.HTML(_START_GLYPH_SVG), class_="hype-start-thumb"),
                    ui.div(
                        ui.div(feat["name"], class_="hype-start-feat-name"),
                        ui.div(f"Last opened {_welcome_when(feat['last_opened'])}",
                               class_="hype-start-feat-when"),
                        ui.div(str(Path(feat["path"]).parent), class_="hype-start-feat-dir",
                               title=feat["path"]),
                        ui.div(
                            ui.tags.button("Open", type="button",
                                           class_="btn btn-primary btn-sm",
                                           onclick="event.stopPropagation(); "
                                                   + _nonce_js("welcome_recent", i=0)),
                            ui.tags.button("Show in folder", type="button",
                                           class_="btn btn-outline-secondary btn-sm",
                                           onclick="event.stopPropagation(); "
                                                   + _nonce_js("welcome_reveal", i=0)),
                            ui.tags.button("Remove", type="button",
                                           class_="btn btn-link btn-sm hype-start-feat-rm",
                                           title="Remove from recent projects",
                                           onclick="event.stopPropagation(); "
                                                   + _nonce_js("welcome_recent_rm", i=0)),
                            class_="hype-start-feat-actions"),
                        class_="hype-start-feat-body"),
                    class_="hype-start-feat", title=feat["path"],
                    onclick=_nonce_js("welcome_recent", i=0))
                groups = [
                    ui.div(ui.div(label, class_="hype-sec hype-start-group"),
                           ui.div(*[_start_recent_row(i, it) for i, it in rows],
                                  class_="hype-welcome-list hype-start-list"))
                    for label, rows in _recent_groups(items)]
                center_body = [featured, *groups]
            else:
                center_body = [ui.div(
                    ui.div(ui.HTML(_START_GLYPH_SVG), class_="hype-start-thumb is-empty"),
                    ui.p("No recent projects yet. Start with New project, or download an "
                         "example to explore a finished site."),
                    class_="hype-start-empty")]
        else:
            center_body = [ui.div(
                ui.p("Nothing is stored on the server: a project lives in your browser "
                     "session and in the .hype file you save.", class_="hype-start-lead"),
                ui.tags.ol(
                    ui.tags.li(ui.tags.b("New project"), " names a project, then you pick a "
                               "site on the map and build the model stage by stage."),
                    ui.tags.li(ui.tags.b("Open project"), " uploads a .hype file saved "
                               "earlier, with everything it holds."),
                    ui.tags.li(ui.tags.b("Example projects"), " downloads a finished site to "
                               "explore its results before you build your own."),
                    class_="hype-start-steps"),
                class_="hype-start-getstarted")]
        center = ui.div(ui.div("Recent projects" if runmode.IS_DESKTOP else "Get started",
                               class_="hype-start-h"),
                        *center_body, class_="hype-start-main")
        rels = changelog.load()
        if rels:
            rel_nodes = [
                ui.div(ui.div(ui.span(r.date_display, class_="hype-start-rel-date"),
                              ui.span(r.label, class_="hype-start-rel-ver"),
                              class_="hype-start-rel-head"),
                       ui.tags.ul(*[ui.tags.li(changelog.plain(b)) for b in r.bullets])
                       if r.bullets else ui.div("Maintenance release.",
                                                class_="hype-start-rel-none"),
                       class_="hype-start-rel")
                for r in rels]
        else:
            rel_nodes = [ui.div(f"HYPE {APP_VERSION_LABEL}", class_="hype-start-rel-none")]
        side = ui.div(ui.div(ui.span("What's new", class_="hype-start-h"),
                             ui.span(APP_VERSION_LABEL, class_="hype-start-verchip"),
                             class_="hype-start-side-head"),
                      ui.div(*rel_nodes, class_="hype-start-rels"),
                      class_="hype-start-side")
        return center, side

    def _show_welcome(view: str = "home", *, example: str | None = None):
        # The start page. Rail = New / Open / Example projects (+ Open a comparison on
        # desktop, Help and Report an issue at the foot); center + right depend on the view:
        # "home" = recent projects + what's new, "examples" = the gallery + the selected
        # example's detail (see _start_examples_columns). No Bootstrap title bar; the modal is
        # widened inline (_START_MODAL_CSS) so three columns fit and scroll independently.
        #
        # TWO MODES SINCE THE HEADER CAN RAISE IT. Under the startup gate it is a HARD gate with
        # no way out at all (see the footer/easy_close note below). Reached from Projects with a
        # project already open it is an ordinary dialog and must be dismissable, or it is a trap:
        # `_ensure_welcome` no-ops once a project exists, so no cancel funnel would bring it back.
        #
        # RECENTS ARE RE-READ ON EVERY CALL, never cached: `_adopt_workspace` touches the store on
        # each project open, and the handlers index `_welcome["recents"]` POSITIONALLY, so a stale
        # snapshot would open the wrong project.
        gated = _gated()
        items = recents.load() if runmode.IS_DESKTOP else []
        _welcome["recents"] = items
        catalog = _examples_catalog()
        if view == "examples" and not catalog:
            view = "home"
        _welcome["view"] = view
        _welcome["example"] = example
        rail = ui.div(
            ui.div(ui.span("HYPE", class_="hype-start-mark"),
                   ui.div("Hyporheic Exchange Explorer", class_="hype-start-sub"),
                   ui.div(ui.span(APP_VERSION_LABEL, class_="hype-start-ver"),
                          ui.span("Desktop" if runmode.IS_DESKTOP else "Cloud",
                                  class_="hype-start-mode"),
                          class_="hype-start-chips"),
                   class_="hype-start-brand"),
            ui.div(
                _start_tile("welcome_new", "New project", "Pick a site and build a model",
                            "new", primary=True),
                _start_tile("welcome_open", "Open project",
                            "Open a project folder or a .hype file" if runmode.IS_DESKTOP
                            else "Upload a .hype file", "open"),
                *([_start_tile("start_examples", "Example projects",
                               "Download a worked site and explore it", "examples",
                               active=(view == "examples"))] if catalog else []),
                class_="hype-start-tiles"),
            # Comparisons are project-independent, so opening one must not require opening
            # a project first (desktop only: file access). Kept as a quiet link, not a tile.
            *([ui.tags.button("Open a comparison", type="button", class_="hype-start-link",
                              onclick=_nonce_js("welcome_compare"))]
              if runmode.IS_DESKTOP else []),
            ui.div(ui.tags.button("Help", type="button", class_="hype-start-link",
                                  onclick=_nonce_js("start_help")),
                   ui.a("Report an issue", href=ISSUES_URL, target="_blank",
                        rel="noopener", class_="hype-start-link"),
                   class_="hype-start-foot"),
            class_="hype-start-rail")
        if view == "examples":
            center, side = _start_examples_columns(catalog, example)
        else:
            center, side = _start_home_columns(items)
        # `title=None` drops the modal header AND its x; `footer=None` drops Bootstrap's
        # default Dismiss. Together with easy_close=False that is a dialog with no exit,
        # which is the point under the gate and wrong the moment there is something to go
        # back to: off the gate a close button rides in the page's own top-right corner.
        close = ([] if gated
                 else [ui.input_action_button("welcome_cancel", "×", class_="hype-start-close",
                                              title="Close")])
        ui.modal_show(ui.modal(
            ui.tags.style(_START_MODAL_CSS),
            ui.div(rail, center, side, *close,
                   class_="hype-start" + (" is-examples" if view == "examples" else "")),
            title=None,
            footer=None,
            easy_close=not gated))

    @reactive.effect
    def _welcome_cancel():
        # `_clicked_dynamic`, not `@reactive.event`: the button is rebuilt on every modal show,
        # so its counter resets to 0 each time and an event binding would miss the second click.
        if _clicked_dynamic("welcome_cancel"):
            ui.modal_remove()

    @reactive.effect
    @reactive.event(input.start_examples)   # nonce: never ignore_init
    def _start_examples_open():
        _show_welcome("examples")

    @reactive.effect
    @reactive.event(input.start_back)       # nonce: never ignore_init
    def _start_back():
        _show_welcome("home")

    @reactive.effect
    @reactive.event(input.welcome_reveal)   # nonce: never ignore_init
    def _welcome_reveal():
        """Featured card's Show in folder (desktop): select the main .hype in Explorer. The
        server IS the user's machine in desktop mode, so a local process is the right tool."""
        msg = input.welcome_reveal() or {}
        try:
            it = _welcome["recents"][int(msg.get("i"))]
        except (TypeError, ValueError, IndexError):
            return
        p = Path(it["path"])
        if not p.exists():
            ui.notification_show("That project file is no longer there.", type="warning",
                                 duration=6)
            return
        try:
            if os.name == "nt":
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p.parent)])
        except OSError:
            ui.notification_show("Could not open the folder.", type="warning", duration=6)

    # ---- Example projects (the start page's second view) --------------------------------
    # The catalog + thumbnails ship inside the app (hype_app/data/examples.json, www/examples/);
    # the bundles are GitHub release assets fetched on demand into the per-user cache
    # (examples.cache_dir()). Opening a downloaded example IS the existing import path:
    # desktop -> _import_bundle_to (folder-clash gate, restore, adopt, rehydrate, main file,
    # recents); cloud -> _apply_project. Nothing here knows about the workspace beyond that.
    #
    # The two dynamic columns are OUTPUTS inside the modal (start_gallery / start_detail /
    # start_dl_status) rather than markup rebuilt by _show_welcome, so picking a tile or a
    # download tick re-renders a column, never the whole dialog. They register with
    # suspend_when_hidden=False like every other late-bound modal output (the USGS lesson).
    if not runmode.IS_DESKTOP:
        # A container's home may be read-only and nothing there should outlive the session.
        examples_mod.set_cache_dir(Path(tempfile.gettempdir()) / "hype_examples")
    _start_sel = reactive.value("")          # selected example id (examples view)
    _start_ver = reactive.value(0)           # bump: re-render gallery + detail
    _example_target: dict = {}               # id -> chosen Save-to main file (desktop)
    _example_prog: dict = {"id": "", "done": 0, "total": 0, "t0": 0.0}
    _example_cancel = threading.Event()
    example_tick = reactive.value(0)
    _example_err: dict = {}                  # id -> last error message (shown in the detail)

    def _examples_catalog() -> list:
        return examples_mod.load_catalog(app_version=APP_VERSION, desktop=runmode.IS_DESKTOP)

    def _example_by_id(ex_id: str):
        for ex in _examples_catalog():
            if ex.id == ex_id:
                return ex
        return None

    def _example_default_target(ex) -> Path:
        """<last project's parent>/<stem>/<stem>.hype: the picker's own initial folder, so the
        example lands next to the user's other projects."""
        return Path(_pick_initial_dir()) / ex.stem / f"{ex.stem}.hype"

    def _example_target_for(ex) -> Path:
        t = _example_target.get(ex.id)
        return Path(t) if t else _example_default_target(ex)

    def _start_examples_columns(catalog: list, example: str | None):
        """Center (gallery) + right (detail) of the examples view: two outputs."""
        sel = example if any(e.id == example for e in catalog) else (catalog[0].id if catalog else "")
        _start_sel.set(sel)
        center = ui.div(
            ui.div(ui.tags.button("‹ Back", type="button", class_="hype-start-link hype-start-back",
                                  onclick=_nonce_js("start_back")),
                   ui.span("Example projects", class_="hype-start-h"),
                   class_="hype-start-main-head"),
            ui.p("Finished sites you can download one at a time and open with their results, "
                 "to explore before you build your own.", class_="hype-start-lead"),
            ui.output_ui("start_gallery"),
            class_="hype-start-main hype-start-gallery-col")
        side = ui.div(ui.output_ui("start_detail"), class_="hype-start-side hype-start-detail-col")
        return center, side

    def _example_tile(i: int, ex, selected: bool):
        chips = [ui.span(t, class_="hype-gallery-tag") for t in ex.tags[:4]]
        cached = examples_mod.is_cached(ex)
        return ui.tags.button(
            ui.div(ui.tags.img(src=_asset(ex.thumbnail), alt="", loading="lazy"),
                   class_="hype-gallery-thumb"),
            ui.div(ui.div(ex.title, class_="hype-gallery-title"),
                   ui.div(ex.description, class_="hype-gallery-desc"),
                   ui.div(*chips,
                          ui.span("Downloaded" if cached else ex.size_display,
                                  class_="hype-gallery-size" + (" is-cached" if cached else "")),
                          class_="hype-gallery-tags"),
                   class_="hype-gallery-body"),
            type="button", class_="hype-gallery-tile" + (" is-sel" if selected else ""),
            title=ex.title, **{"data-id": ex.id},
            onclick=_nonce_js("start_pick", i=i))     # index into the catalog, never a path

    @output(suspend_when_hidden=False)
    @render.ui
    def start_gallery():
        _ = _start_ver()
        sel = _start_sel()
        catalog = _examples_catalog()
        if not catalog:
            return ui.p("No example projects are available in this build.",
                        class_="hype-start-empty")
        return ui.div(*[_example_tile(i, ex, ex.id == sel) for i, ex in enumerate(catalog)],
                      class_="hype-gallery-grid")

    @output(suspend_when_hidden=False)
    @render.ui
    def start_detail():
        _ = _start_ver()
        ex = _example_by_id(_start_sel())
        if ex is None:
            return None
        cached = examples_mod.is_cached(ex)
        partial = examples_mod.part_path(ex).exists()
        meta = [ui.span(ex.size_display, class_="hype-gallery-size"),
                *[ui.span(t, class_="hype-gallery-tag") for t in ex.tags]]
        rows = [
            ui.div(ui.tags.img(src=_asset(ex.thumbnail), alt=""), class_="hype-gallery-hero"),
            ui.div(ex.title, class_="hype-start-h hype-gallery-dtitle"),
            ui.p(ex.description, class_="hype-gallery-ddesc"),
            ui.div(*meta, class_="hype-gallery-tags"),
        ]
        if ex.credit:
            rows.append(ui.div(ex.credit, class_="hype-gallery-credit"))
        if ex.published:
            rows.append(ui.div(f"Published {ex.published}", class_="hype-gallery-credit"))
        if runmode.IS_DESKTOP:
            target = _example_target_for(ex)
            rows.append(ui.div(
                ui.div("Save to", class_="hype-gallery-k"),
                ui.div(str(target.parent), class_="hype-gallery-path", title=str(target)),
                ui.tags.button("Change…", type="button", class_="hype-start-link",
                               onclick=_nonce_js("start_change_target")),
                class_="hype-gallery-target"))
        else:
            rows.append(ui.div("Opens in this session. Save it as a .hype file to keep it.",
                               class_="hype-gallery-credit"))
        err = _example_err.get(ex.id)
        if err:
            rows.append(ui.div(err, class_="hype-gallery-err"))
        rows.append(ui.output_ui("start_dl_status"))
        if cached:
            primary = ui.tags.button("Open example", type="button", class_="btn btn-primary",
                                     onclick=_nonce_js("start_open"))
            secondary = [ui.tags.button("Remove download", type="button",
                                        class_="btn btn-outline-secondary",
                                        onclick=_nonce_js("start_remove"))]
        else:
            label = "Resume download and open" if partial else "Download and open"
            primary = ui.tags.button(label, type="button", class_="btn btn-primary",
                                     onclick=_nonce_js("start_open"))
            secondary = []
        rows.append(ui.div(primary, *secondary, class_="hype-gallery-actions"))
        return ui.div(*rows, class_="hype-gallery-detail")

    def _on_example_progress(done: int, total: int):
        # Worker-thread writer; the poller mirrors it into example_tick (plain dict only).
        _example_prog["done"] = int(done)
        _example_prog["total"] = int(total)

    @reactive.extended_task
    async def example_task(payload: dict) -> dict:
        ex = payload["example"]

        def _work():
            p = examples_mod.fetch(ex, progress=_on_example_progress, cancel=_example_cancel)
            return {"id": ex.id, "path": str(p)}
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    def _example_poll():
        if example_task.status() != "running":
            return
        reactive.invalidate_later(0.5)
        example_tick.set(int(_example_prog["done"]) // 4096 * 7 + int(time.monotonic() * 2))

    @output(suspend_when_hidden=False)
    @render.ui
    def start_dl_status():
        if example_task.status() != "running":
            return None
        _ = example_tick()
        if _example_prog["id"] != _start_sel():
            return ui.div("Another example is downloading.", class_="hype-gallery-credit")
        done, total = _example_prog["done"], _example_prog["total"]
        pct = int(round(100.0 * done / total)) if total else 0
        line = (f"{examples_mod.human_size(done)} of {examples_mod.human_size(total)}"
                if total else "Starting the download")
        return ui.div(
            ui.div(ui.div(class_="hype-spinner"), ui.span(line), class_="hype-busy"),
            ui.div(ui.div(class_="hype-prog-bar", style=f"width:{pct}%;"), class_="hype-prog"),
            ui.input_action_button("start_dl_cancel", "Cancel", class_="btn-sm"),
            class_="hype-gallery-progress")

    @reactive.effect
    def _start_dl_cancel():
        if _clicked_dynamic("start_dl_cancel"):
            _example_cancel.set()          # cooperative: checked per chunk; keeps the .part

    @reactive.effect
    @reactive.event(input.start_pick)      # nonce: never ignore_init
    def _start_pick():
        msg = input.start_pick() or {}
        try:
            ex = _examples_catalog()[int(msg.get("i"))]
        except (TypeError, ValueError, IndexError):
            return
        _welcome["example"] = ex.id
        _start_sel.set(ex.id)

    @reactive.effect
    @reactive.event(input.start_change_target)   # nonce: never ignore_init
    async def _start_change_target():
        ex = _example_by_id(_start_sel())
        if ex is None or not runmode.IS_DESKTOP:
            return
        _welcome["example"] = ex.id
        await _pick_path("example_target", save=True, file_name=f"{ex.stem}.hype")

    @reactive.effect
    @reactive.event(input.start_remove)    # nonce: never ignore_init
    def _start_remove():
        ex = _example_by_id(_start_sel())
        if ex is None:
            return
        if example_task.status() == "running" and _example_prog["id"] == ex.id:
            ui.notification_show("Cancel the download first.", type="warning", duration=5)
            return
        examples_mod.remove(ex)
        _example_err.pop(ex.id, None)
        _start_ver.set(_start_ver() + 1)

    @reactive.effect
    @reactive.event(input.start_open)      # nonce: never ignore_init
    async def _start_open():
        """Download and open / Open example. Cached -> open now; else start the download
        (the done handler opens it). Cloud with a project open confirms first: opening
        replaces the whole session there, exactly like Open."""
        ex = _example_by_id(_start_sel())
        if ex is None:
            return
        if _busy_tasks():
            ui.notification_show("A task is still running. Wait for it to finish (or cancel "
                                 "it) before opening an example.", type="warning", duration=6)
            return
        if example_task.status() == "running":
            ui.notification_show("A download is already in progress.", type="warning",
                                 duration=5)
            return
        if not runmode.IS_DESKTOP and not _gated():
            _pending_pick["example_confirm"] = ex.id
            ui.modal_show(ui.modal(
                ui.p(f"Opening {ex.title} replaces everything in the current session. Save or "
                     "download your project first if you want to keep it."),
                title="Open example project?",
                footer=ui.TagList(
                    ui.input_action_button("example_confirm_cancel", "Cancel"),
                    ui.input_action_button("example_confirm_go", "Open example",
                                           class_="btn-danger")),
                easy_close=True))
            return
        await _example_go(ex)

    @reactive.effect
    def _example_confirm_cancel():
        if _clicked_dynamic("example_confirm_cancel"):
            ui.modal_remove()
            _pending_pick.pop("example_confirm", None)
            _show_welcome("examples", example=_welcome.get("example"))

    @reactive.effect
    async def _example_confirm_go():
        if not _clicked_dynamic("example_confirm_go"):
            return
        ex = _example_by_id(str(_pending_pick.pop("example_confirm", "") or ""))
        ui.modal_remove()
        if ex is None:
            _ensure_welcome()
            return
        _show_welcome("examples", example=ex.id)   # back on the page: progress renders there
        await _example_go(ex)

    async def _example_go(ex):
        _example_err.pop(ex.id, None)
        if examples_mod.is_cached(ex):
            await _example_open(ex)
            return
        _example_cancel.clear()
        _example_prog.update({"id": ex.id, "done": 0, "total": ex.size_bytes,
                              "t0": time.monotonic()})
        _task_armed["example"] = True
        _start_ver.set(_start_ver() + 1)
        example_task({"example": ex})

    @reactive.effect
    async def _example_done():
        if example_task.status() in ("initial", "running", "cancelled"):
            return
        if not _task_armed["example"]:
            return
        _task_armed["example"] = False
        ex_id = _example_prog.get("id") or ""
        ex = _example_by_id(ex_id)
        try:
            out = example_task.result()
        except examples_mod.ExampleCancelled:
            ui.notification_show("Download cancelled. It resumes where it stopped.", duration=5)
            _start_ver.set(_start_ver() + 1)
            return
        except examples_mod.ExampleError as e:
            _example_err[ex_id] = str(e)
            _start_ver.set(_start_ver() + 1)
            return
        except Exception as e:  # noqa: BLE001 — a failed download must never kill the session
            _example_err[ex_id] = f"Download failed: {e}"
            _start_ver.set(_start_ver() + 1)
            return
        _start_ver.set(_start_ver() + 1)
        if ex is None or out.get("id") != ex.id:
            return
        await _example_open(ex)

    async def _example_open(ex):
        """Open the cached bundle: desktop imports it into the chosen project folder (the
        clash modal / Create subfolder / Use anyway re-enter _import_bundle_to with the src
        stashed, exactly like a portable-file import); cloud applies it to the session."""
        src = examples_mod.cached_path(ex)
        if not src.is_file():
            _example_err[ex.id] = "The downloaded file is missing. Download it again."
            _start_ver.set(_start_ver() + 1)
            return
        if _busy_tasks():
            ui.notification_show("A task is still running. Wait for it to finish (or cancel "
                                 "it) before opening an example.", type="warning", duration=6)
            return
        ui.modal_remove()
        try:
            if runmode.IS_DESKTOP:
                _pending_import["src"] = str(src)
                await _import_bundle_to(_example_target_for(ex))
            else:
                ui.notification_show("Opening example…", duration=None, id="open_prog")
                try:
                    await _apply_project(str(src), fallback_name=ex.title)
                    ui.notification_show(f"Opened {ex.title}.", duration=6)
                finally:
                    ui.notification_remove("open_prog")
        except bundle.ProjectError as e:
            ui.notification_show(str(e), type="error", duration=10)
            _ensure_welcome()
        except Exception as e:  # noqa: BLE001 — a failed open must never kill the session
            ui.notification_show(f"Couldn't open the example: {e}", type="error", duration=10)
            _ensure_welcome()

    # ---- What's new: the changelog behind every version number -------------------------
    # Opened by clicking the version wherever it appears (header chip, welcome splash,
    # About footer). Shiny shows one modal at a time, so raising this from the welcome
    # splash REPLACES the startup gate; Close funnels back through _ensure_welcome exactly
    # like the project dialogs' cancels, and easy_close stays off while gated so
    # Esc/backdrop can't leave a project-less session dialog-less.
    def _show_whatsnew():
        notes = _changelog_md()
        body = (ui.div(ui.markdown(notes), class_="hype-whatsnew") if notes
                else ui.p(f"HYPE {APP_VERSION_LABEL}"))
        ui.modal_show(ui.modal(
            body,
            title="What's new",
            footer=ui.input_action_button("whatsnew_close", "Close",
                                          class_="btn-primary"),
            easy_close=not _gated()))

    @reactive.effect
    @reactive.event(input.whatsnew_evt)   # nonce input, so never ignore_init
    def _whatsnew_open():
        _show_whatsnew()

    @reactive.effect
    def _whatsnew_close():
        # `_clicked_dynamic` again: the Close button is rebuilt on every modal show.
        if _clicked_dynamic("whatsnew_close"):
            ui.modal_remove()
            _ensure_welcome()

    def _show_new_project_dialog():
        """Cloud New Project: a name is all a browser session needs (there is no folder).
        Hard-gated like the welcome — Cancel funnels back there."""
        ui.modal_show(ui.modal(
            ui.input_text("new_project_name", "Project name", width="100%",
                          placeholder="e.g. Mink Creek"),
            ui.div(f"Units: {project_meta.UNIT_LABELS[project_meta.UNITS_METRIC]}. "
                   "Unit selection is locked in this version.",
                   class_="hype-instr hype-dim"),
            ui.p("Nothing is stored on the server. Use Save to download your project as "
                 "a .hype file when you finish.", class_="hype-instr hype-dim"),
            title="New project",
            footer=ui.TagList(
                ui.input_action_button("new_project_cancel", "Cancel"),
                ui.input_action_button("new_project_create", "Create project",
                                       class_="btn-primary")),
            easy_close=False))

    @reactive.effect
    def _new_project_cancel():
        if _clicked_dynamic("new_project_cancel"):
            ui.modal_remove()
            _ensure_welcome()

    @reactive.effect
    async def _new_project_create():
        if not _clicked_dynamic("new_project_create"):
            return
        name = str(_safe("new_project_name", "") or "").strip()
        if not name:
            ui.notification_show("Enter a project name.", type="warning", duration=4)
            return                         # modal stays up for another try
        ui.modal_remove()
        await _reset_session_state()       # no-op on a virgin session; makes the entry
        #                                    identical after the destructive-New confirm
        _map_home()                        # a blank project starts at the national view
        _set_project_meta(name, datetime.now().isoformat(timespec="seconds"),
                          mint_missing=True)
        _select("reach")
        ui.notification_show(f"Created {name}", duration=5)

    @reactive.effect
    @reactive.event(input.welcome_new)
    async def _welcome_new():
        # Same helper the header's New uses, so the confirm cannot be bypassed by coming in
        # through the menu. Gated, it still goes straight through.
        await _begin_new_project()

    @reactive.effect
    @reactive.event(input.welcome_open)
    async def _welcome_open():
        # The busy pre-check the header's Open carries. `_on_project_path` re-checks anyway,
        # but only after a native dialog has already opened, and rejecting a path the user
        # just picked reads as a bug rather than as a guard.
        if _busy_tasks():
            ui.notification_show("A task is still running. Wait for it to finish (or cancel "
                                 "it) before opening a project.", type="warning", duration=6)
            return
        if runmode.IS_DESKTOP:
            await _pick_path("open_project", save=False)
        else:
            _show_open_modal()

    @reactive.effect
    @reactive.event(input.welcome_compare)
    async def _welcome_compare():
        if _busy_tasks():
            ui.notification_show("A model task is still running. Wait for it to finish before "
                                 "opening a comparison.", type="warning", duration=7)
            return
        await _pick_comparison("comparison_open", save=False)

    @reactive.effect
    @reactive.event(input.comparison_new_evt)
    async def _comparison_new_from_reports():
        # The Site Reports hub row and the tree-node launcher share this one entry point.
        if not runmode.IS_DESKTOP:
            return
        await _comparison_start(add_current=True)

    @reactive.effect
    @reactive.event(input.comparison_open_evt)
    async def _comparison_open_from_launcher():
        if not runmode.IS_DESKTOP:
            return
        if _busy_tasks():
            ui.notification_show("A model task is still running. Wait for it to finish before "
                                 "opening a comparison.", type="warning", duration=7)
            return
        await _pick_comparison("comparison_open", save=False)

    @reactive.effect
    @reactive.event(input.comparison_recent_open)
    async def _comparison_recent_open():
        # Launcher-pane recent rows: value-encoded index into the snapshot taken at render.
        msg = input.comparison_recent_open() or {}
        try:
            it = _cmp_recents["items"][int(msg.get("i"))]
        except (TypeError, ValueError, IndexError):
            return
        p = Path(it["path"])
        if not p.is_file():
            ui.notification_show("That comparison file is no longer there.", type="warning",
                                 duration=6)
            recents.forget_comparison(p)
            _cmp_recents["tick"] += 1
            cmp_recents_ver.set(cmp_recents_ver() + 1)
            return
        if _busy_tasks():
            ui.notification_show("A model task is still running. Wait for it to finish before "
                                 "opening a comparison.", type="warning", duration=7)
            return
        await _comparison_open_path(p)

    @reactive.effect
    @reactive.event(input.welcome_recent)
    async def _welcome_recent():
        msg = input.welcome_recent() or {}
        try:
            it = _welcome["recents"][int(msg.get("i"))]
        except (TypeError, ValueError, IndexError):
            return
        p = Path(it["path"])
        if not p.is_file():
            ui.notification_show("That project file is no longer there.", type="warning",
                                 duration=6)
            _show_welcome()            # rebuilt list — the vanished entry prunes out
            return
        if _ws["project_file"] and Path(_ws["project_file"]) == p:
            # ALREADY HERE. The open project sits at the top of the list (`_adopt_workspace`
            # touches the store on every open), so this row is the easiest one to hit by
            # accident. Reopening would parting-save, wipe and rehydrate its way back to
            # exactly this state, which looks like the app restarting for no reason.
            ui.modal_remove()
            return
        await _on_project_path("open_project", p)

    @reactive.effect
    @reactive.event(input.welcome_recent_rm)   # nonce event input: NEVER ignore_init
    def _welcome_recent_rm():
        msg = input.welcome_recent_rm() or {}
        try:
            it = _welcome["recents"][int(msg.get("i"))]
        except (TypeError, ValueError, IndexError):
            return
        recents.forget(it["path"])
        _ensure_welcome()   # re-show rebuilds rows + the _welcome["recents"] index snapshot

    _welcome_shown: dict = {}

    @reactive.effect
    @reactive.event(input.hype_map_ready)   # nonce from map_bounds.js: never ignore_init
    def _welcome_gate():
        # One-shot per session, fired by the client's map-ready ping (www/map_bounds.js posts
        # it once the main Leaflet map is in the DOM, or 6 s after connect as a fallback), so
        # the start page lands over a painted map rather than in the first flush over a blank
        # one; the boot veil covers the gap, so nothing is clickable before it. A reconnect is
        # a fresh server session in Shiny, so every real page load re-gates (including a
        # mid-work reload that lost the project); it can never pop over a live project —
        # nothing returns the gate condition to True without immediately presenting the next
        # dialog itself (cloud New resets straight into the name dialog).
        if _welcome_shown.get("done"):
            return
        _welcome_shown["done"] = True
        with reactive.isolate():
            if _gated():
                _show_welcome()

    def _show_typed_pick_modal(purpose: str, *, save: bool, value: str = "",
                               error: str | None = None):
        """The typed-path fallback modal (no shell, picker forced to "modal", or the tk
        dialog failed to open). Re-shown in place on validation errors with the user's
        text preserved; same button ids are safe (_clicked_dynamic strict-increment)."""
        _pending_pick["purpose"] = purpose
        body = [
            ui.p("Type the full path for the project's main .hype file. Its folder "
                 "becomes the project folder. You can also paste the path of an empty "
                 "folder." if save else
                 "Type the full path of the project's main .hype file."),
            ui.input_text("dev_pick_path", None, width="100%", value=value,
                          placeholder=r"D:\Projects\SiteA\SiteA.hype"),
        ]
        if error:
            body.append(ui.div(error, class_="text-danger", style="font-size: 0.9em;"))
        ui.modal_show(ui.modal(
            *body,
            footer=ui.TagList(
                ui.input_action_button("dev_pick_cancel", "Cancel"),
                ui.input_action_button("dev_pick_go", "OK", class_="btn-primary")),
            title="Project path", easy_close=not _gated()))

    _pick_flight: dict = {}    # {"purpose"} of the tk pick in flight; survives
    #                            _pending_pick re-stamps by a blocked second request

    def _pick_initial_dir() -> str:
        try:
            if _ws["project_file"]:
                return str(Path(_ws["project_file"]).parent.parent)
            items = recents.load()
            if items:
                return str(Path(items[0]["path"]).parent.parent)
        except Exception:  # noqa: BLE001 — a nicety must never break the pick
            pass
        return str(Path.home())

    @reactive.extended_task
    async def pick_task(payload: dict) -> dict:
        # ALWAYS a spawned child, never in-process tkinter: Tk wants the process's main
        # thread, and a wedged dialog stays hard-killable when the session resets.
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=pick_run.child_run, args=(payload, q), daemon=True)
            _pick_proc["p"] = p
            p.start()
            result = error = None
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "log":
                        print(f"[pick] {data}", flush=True)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    if not p.is_alive():
                        break
            while True:                    # post-mortem drain
                try:
                    kind, data = q.get_nowait()
                    if kind == "log":
                        print(f"[pick] {data}", flush=True)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            _pick_proc["p"] = None
            if error is not None:
                raise RuntimeError(error.strip().splitlines()[-1] or "file dialog failed")
            if result is None:
                raise RuntimeError("the file dialog was interrupted")
            return result
        return await anyio.to_thread.run_sync(_work)

    @reactive.effect
    async def _pick_done():
        if pick_task.status() in ("initial", "running", "cancelled"):
            return
        if not _task_armed["pick"]:        # already applied (or session reset)
            return
        _task_armed["pick"] = False
        ui.notification_remove("pick_opening")
        purpose = str(_pick_flight.get("purpose") or "")
        try:
            res = pick_task.result()
        except Exception as e:  # noqa: BLE001 — child died / tk failed: never dead-end
            ui.notification_show(f"The file picker couldn't open ({e}). Type the path "
                                 "instead.", type="warning", duration=8)
            if purpose.startswith("comparison_"):
                _show_comparison_typed_pick_modal(purpose)
            elif purpose.startswith("maplayer"):
                _show_maplayer_typed_pick_modal(purpose)
            elif purpose == "dem_src_pick":
                _show_dem_typed_pick_modal(purpose)
            else:
                _show_typed_pick_modal(purpose, save=purpose not in pathpick.OPEN_PURPOSES)
            return
        await _dispatch_picked_result(res, purpose)

    async def _pick_path(purpose: str, *, save: bool, file_name: str | None = None):
        """Ask for a .hype path: native dialog via the shell bridge when present, else a
        server-spawned tk dialog, else a typed-path modal (HYPE_PICKER=modal, or the tk
        child failing). Replies funnel into _on_project_path."""
        shell = _shell_present()
        mode = "native" if shell else ("tk" if runmode.picker_mode() == "auto" else "modal")
        print(f"[desktop] picker: {mode} ({purpose})", flush=True)
        if shell:
            _pending_pick["purpose"] = purpose
            await session.send_custom_message("hype_desktop", {
                "type": "pickProjectSave" if save else "pickProjectOpen",
                "purpose": purpose, "fileName": file_name or "Project1.hype"})
            return
        if mode == "tk":
            if _task_state(pick_task) == "running":
                # NEVER invoke while running: ExtendedTask.invoke queues (FIFO), which
                # would pop a second dialog the moment the first closes.
                ui.notification_show("A file picker window is already open. Look for it "
                                     "on your taskbar.", type="warning", duration=6)
                return
            _pending_pick["purpose"] = purpose
            _pick_flight["purpose"] = purpose
            titles = {"new_project": "New HYPE Project", "import_target":
                      "Import Project To", "save_as": "Save Project As",
                      "example_target": "Save Example Project To"}
            payload = {"mode": "save" if save else "open", "purpose": purpose,
                       "title": titles.get(purpose, "Open HYPE Project"),
                       "initial_file": (file_name or "Project1.hype") if save else "",
                       "initial_dir": _pick_initial_dir()}
            ui.notification_show("Opening a file window...", duration=4, id="pick_opening")
            _task_armed["pick"] = True
            pick_task(payload)
            return
        _show_typed_pick_modal(purpose, save=save)

    async def _pick_map_layers(purpose: str):
        """Ask for reference-layer file(s) (Map layers pane). DELIBERATELY no shell
        branch: the shipped WinForms shell has no map-layer message type and silently
        drops unknown commands (the pick would hang forever), so desktop uses the spawned
        tk child — the native Win32 dialog on Windows — until a shell release adds one."""
        if runmode.picker_mode() != "auto":
            _show_maplayer_typed_pick_modal(purpose)
            return
        if _task_state(pick_task) == "running":
            ui.notification_show("A file picker window is already open. Look for it "
                                 "on your taskbar.", type="warning", duration=6)
            return
        relink = purpose.startswith("maplayer_relink:")
        _pending_pick["purpose"] = purpose
        _pick_flight["purpose"] = purpose
        payload = {"mode": "open" if relink else "open_multiple", "kind": "maplayer",
                   "purpose": purpose,
                   "title": "Locate the layer file" if relink else "Add map layers",
                   "initial_dir": _pick_initial_dir()}
        ui.notification_show("Opening a file window...", duration=4, id="pick_opening")
        _task_armed["pick"] = True
        pick_task(payload)

    def _show_maplayer_typed_pick_modal(purpose: str, *, value: str = "",
                                        error: str | None = None):
        """Typed-path fallback for Map layers (HYPE_PICKER=modal, or the tk child failed)."""
        _pending_pick["purpose"] = purpose
        body = [
            ui.p("Type the full path of the raster (.tif, .tiff, .vrt) or vector "
                 "(.shp, .geojson, .json) file to link."),
            ui.input_text("ml_pick_path", None, width="100%", value=value,
                          placeholder=r"D:\GIS\parcels.shp"),
        ]
        if error:
            body.append(ui.div(error, class_="text-danger", style="font-size: 0.9em;"))
        ui.modal_show(ui.modal(
            *body,
            footer=ui.TagList(
                ui.input_action_button("ml_pick_cancel", "Cancel"),
                ui.input_action_button("ml_pick_go", "OK", class_="btn-primary")),
            title="Map layer file", easy_close=True))

    @reactive.effect
    def _ml_pick_cancel():
        if _clicked_dynamic("ml_pick_cancel"):
            ui.modal_remove()

    @reactive.effect
    async def _ml_pick_go():
        if not _clicked_dynamic("ml_pick_go"):
            return
        purpose = str(_pending_pick.get("purpose") or "")
        if not purpose.startswith("maplayer"):
            return
        raw = str(_safe("ml_pick_path", "") or "")
        target, err = pathpick.interpret_reference_file(raw)
        ui.modal_remove()
        if err:
            _show_maplayer_typed_pick_modal(purpose, value=raw, error=err)
            return
        await _dispatch_picked_result({"purpose": purpose, "path": str(target)})

    async def _pick_dem_raster(purpose: str = "dem_src_pick"):
        """Ask for the local DEM GeoTIFF (DEM pane, Terrain source = Local raster). Same
        no-shell-branch posture as _pick_map_layers: the shipped WinForms shell silently
        drops unknown message types, so desktop uses the spawned tk child until a shell
        release adds a native message."""
        if runmode.picker_mode() != "auto":
            _show_dem_typed_pick_modal(purpose)
            return
        if _task_state(pick_task) == "running":
            ui.notification_show("A file picker window is already open. Look for it "
                                 "on your taskbar.", type="warning", duration=6)
            return
        _pending_pick["purpose"] = purpose
        _pick_flight["purpose"] = purpose
        payload = {"mode": "open", "kind": "demraster", "purpose": purpose,
                   "title": "Choose DEM raster", "initial_dir": _pick_initial_dir()}
        ui.notification_show("Opening a file window...", duration=4, id="pick_opening")
        _task_armed["pick"] = True
        pick_task(payload)

    def _show_dem_typed_pick_modal(purpose: str = "dem_src_pick", *, value: str = "",
                                   error: str | None = None):
        """Typed-path fallback for the local DEM (HYPE_PICKER=modal, or the tk child
        failed)."""
        _pending_pick["purpose"] = purpose
        body = [
            ui.p("Type the full path of the DEM GeoTIFF (.tif or .tiff) to link."),
            ui.input_text("dem_pick_path", None, width="100%", value=value,
                          placeholder=r"D:\GIS\site_dem.tif"),
        ]
        if error:
            body.append(ui.div(error, class_="text-danger", style="font-size: 0.9em;"))
        ui.modal_show(ui.modal(
            *body,
            footer=ui.TagList(
                ui.input_action_button("dem_pick_cancel", "Cancel"),
                ui.input_action_button("dem_pick_go", "OK", class_="btn-primary")),
            title="DEM raster file", easy_close=True))

    @reactive.effect
    def _dem_pick_cancel():
        if _clicked_dynamic("dem_pick_cancel"):
            ui.modal_remove()

    @reactive.effect
    def _dem_pick_go():
        if not _clicked_dynamic("dem_pick_go"):
            return
        purpose = str(_pending_pick.get("purpose") or "")
        if purpose != "dem_src_pick":
            return
        raw = str(_safe("dem_pick_path", "") or "")
        target, err = pathpick.interpret_dem_file(raw)
        ui.modal_remove()
        if err:
            _show_dem_typed_pick_modal(purpose, value=raw, error=err)
            return
        _dem_src_picked(target)

    @reactive.effect
    def _dev_pick_cancel():
        if _clicked_dynamic("dev_pick_cancel"):
            ui.modal_remove()
            if str(_pending_pick.get("purpose") or "") == "example_target":
                _show_welcome("examples", example=_welcome.get("example"))
                return
            _ensure_welcome()

    @reactive.effect
    async def _dev_pick():
        if not _clicked_dynamic("dev_pick_go"):
            return
        raw = str(_safe("dev_pick_path", "") or "")
        purpose = str(_pending_pick.get("purpose") or "")
        known_stem = None
        if purpose == "save_as" and _ws["project_file"]:
            known_stem = Path(_ws["project_file"]).stem
        elif purpose == "import_target" and _pending_import.get("src"):
            known_stem = Path(str(_pending_import["src"])).stem
        elif purpose == "example_target":
            ex = _example_by_id(str(_welcome.get("example") or ""))
            known_stem = ex.stem if ex else None
        target, err = pathpick.interpret_typed_target(raw, purpose=purpose,
                                                      known_stem=known_stem)
        ui.modal_remove()
        if err:
            _show_typed_pick_modal(purpose, save=purpose not in pathpick.OPEN_PURPOSES,
                                   value=raw, error=err)
            return
        if purpose not in pathpick.OPEN_PURPOSES:
            # Typed-modal-only overwrite gate: native and tk dialogs confirm replacement
            # in the dialog itself. Picking the CURRENT main file via save_as is plain
            # Save (the dispatcher short-circuits) and must not scare the user.
            is_current = False
            try:
                is_current = bool(purpose == "save_as" and _ws["project_file"]
                                  and target.resolve()
                                  == Path(_ws["project_file"]).resolve())
            except OSError:
                pass
            if target.is_file() and not is_current:
                _pending_pick["overwrite"] = {"purpose": purpose, "path": str(target),
                                              "raw": raw}
                ui.modal_show(ui.modal(
                    ui.p(f"{target.name} already exists in this folder:"),
                    ui.div(ui.tags.code(str(target.parent)),
                           style="word-break: break-all;"),
                    ui.p("Replace it?"),
                    title="Replace existing file?",
                    footer=ui.TagList(
                        ui.input_action_button("dev_pick_ow_back", "Back"),
                        ui.input_action_button("dev_pick_ow_go", "Replace",
                                               class_="btn-danger")),
                    easy_close=not _gated()))
                return
        await _on_project_path(purpose, target)

    @reactive.effect
    def _dev_pick_ow_back():
        if _clicked_dynamic("dev_pick_ow_back"):
            ui.modal_remove()
            st = _pending_pick.pop("overwrite", None)
            if st:
                _show_typed_pick_modal(st["purpose"], save=True, value=st["raw"])
            else:
                _ensure_welcome()

    @reactive.effect
    async def _dev_pick_ow_go():
        if not _clicked_dynamic("dev_pick_ow_go"):
            return
        ui.modal_remove()
        st = _pending_pick.pop("overwrite", None)
        if not st:
            _ensure_welcome()
            return
        await _on_project_path(st["purpose"], Path(st["path"]))

    @reactive.effect
    @reactive.event(input.desktop_pick)
    async def _desktop_picked():
        # Native dialog replies (single path, multi-path, or cancel) all route through the
        # shared dispatcher: comparison purposes peel off there, everything else lands on
        # _on_project_path exactly as before. On cancel under the startup gate the welcome
        # dialog is usually still on screen (a self-replace is harmless); the exception it
        # rescues is the import_target hop, whose flow removed the import modal first.
        await _dispatch_picked_result(input.desktop_pick() or {})

    async def _on_project_path(purpose: str, p: Path):
        if not runmode.IS_DESKTOP:
            return
        if _busy_tasks():                  # re-check — a native dialog can sit open a while
            ui.notification_show("A task is still running. Wait for it to finish (or cancel "
                                 "it) first.", type="warning", duration=6)
            return                         # any open modal (welcome included) stays up
        ui.modal_remove()                  # clear the welcome / fallback dialog for dispatch
        try:
            if purpose == "new_project":
                await _create_project(p)
            elif purpose == "open_project":
                await _open_project_path(p)
            elif purpose == "import_target":
                await _import_bundle_to(p)
            elif purpose == "save_as":
                await _save_as_project(p)
            elif purpose == "example_target":
                # Store-only: the example detail's Save-to. Nothing is written until the
                # user clicks Download and open / Open example.
                ex_id = str(_welcome.get("example") or "")
                if ex_id:
                    _example_target[ex_id] = str(pathpick.ensure_hype_suffix(p))
                _show_welcome("examples", example=ex_id or None)
        except bundle.ProjectError as e:
            ui.notification_show(str(e), type="error", duration=10)
            _ensure_welcome()
        except Exception as e:  # noqa: BLE001 — a failed project op must never kill the session
            ui.notification_show(f"Project operation failed: {e}", type="error", duration=10)
            _ensure_welcome()

    def _show_clash_modal(purpose: str, main_file: Path, names: list[str], foreign: bool,
                          others: list[str]):
        """The picked folder already holds content. RAS2025 refuses non-empty targets
        outright; our adaptation offers its fix (a fresh stem-named subfolder) as the
        primary action but keeps an explicit override. HYPE artifacts (`names`) get the
        strong data-loss copy; unrelated content (`others`) gets a milder ownership
        nudge — the Desktop-dump guard. `purpose` ("new_project", "import_target", or
        "save_as") picks which dispatcher the confirm buttons re-enter; Cancel funnels
        back to the welcome gate. Everything before confirmation is read-only."""
        sub = bundle.clash_subfolder(main_file)
        _pending_pick["clash"] = {"purpose": purpose, "path": str(main_file),
                                  "sub": str(sub)}
        listed_others = ", ".join(others[:5]) + (f" and {len(others) - 5} more"
                                                 if len(others) > 5 else "")
        if names:
            shown = sorted(names)
            listed = ", ".join(shown[:6]) + (f" and {len(shown) - 6} more"
                                             if len(shown) > 6 else "")
            head = (f"This folder already holds another HYPE project ({listed})."
                    if foreign else
                    f"This folder already contains HYPE project files ({listed}).")
            if others:
                head += f" It also holds other files ({listed_others})."
            if purpose == "import_target":
                risk = ("Importing here would extract this project's files over it right "
                        "away." if foreign else
                        "Importing here would overwrite matching files right away.")
            elif purpose == "save_as":
                risk = ("Saving the copy here would mix it with that project; model runs "
                        "from one would overwrite the other's files."
                        if foreign else
                        "Saving the copy here would overwrite matching files right away.")
            else:
                risk = ("Projects in one folder share the same content folders, so model "
                        "runs from one project would overwrite the other's files."
                        if foreign else
                        "Model runs in the new project can overwrite them.")
            anyway_cls = "btn-danger"
        else:
            head = f"This folder already contains other files ({listed_others})."
            risk = ("A HYPE project keeps its files in its own folder; continuing would "
                    "create the project's content folders next to those files.")
            anyway_cls = None      # plain button: messiness, not data loss
        ui.modal_show(ui.modal(
            ui.p(f"{head} {risk}"),
            ui.p("Create subfolder keeps this project separate. It will be created at:"),
            ui.div(ui.tags.code(str(sub)), style="word-break: break-all;"),
            title="Folder isn't empty",
            footer=ui.TagList(
                ui.input_action_button("create_cancel", "Cancel"),
                ui.input_action_button("confirm_create_anyway", "Use this folder anyway",
                                       class_=anyway_cls),
                ui.input_action_button("confirm_create_subfolder", "Create subfolder",
                                       class_="btn-primary")),
            easy_close=not _gated()))

    async def _create_project(main_file: Path, *, confirmed: bool = False):
        main_file = pathpick.ensure_hype_suffix(main_file)
        folder = main_file.parent
        names, foreign, others = bundle.folder_clash(folder, main_file)
        if (names or others) and not confirmed:
            _show_clash_modal("new_project", main_file, names, foreign, others)
            return
        _warn_path_advisories(folder)
        folder.mkdir(parents=True, exist_ok=True)
        # Every project folder carries an aerials/ drop spot (EXPORT_DIRS contract:
        # travels with Save As, never packed into archives).
        (folder / "aerials").mkdir(exist_ok=True)
        if _ws["project_file"]:
            try:
                _save_project_file()       # parting save of the project we're leaving
            except Exception:  # noqa: BLE001
                pass
        await _reset_memory_state()
        _map_home()                        # a blank project starts at the national view
        _adopt_workspace(folder, main_file)
        _set_project_meta(main_file.stem,  # stamped once, at creation; the stem IS the name
                          datetime.now().isoformat(timespec="seconds"), mint_missing=True)
        _save_project_file()               # the folder has its main file from minute one
        await _post_title()
        _select("reach")
        ui.notification_show(f"Created {main_file.name}", duration=5)

    @reactive.effect
    def _create_cancel():
        if _clicked_dynamic("create_cancel"):
            ui.modal_remove()
            _pending_pick.pop("clash", None)
            _pending_import.pop("src", None)   # a cancelled import clash ends the import
            _ensure_welcome()

    @reactive.effect
    async def _create_anyway():
        if not _clicked_dynamic("confirm_create_anyway"):
            return
        ui.modal_remove()
        st = _pending_pick.pop("clash", None)
        if not st:
            _ensure_welcome()
            return
        try:
            if st["purpose"] == "import_target":
                await _import_bundle_to(Path(st["path"]), confirmed=True)
            elif st["purpose"] == "save_as":
                await _save_as_project(Path(st["path"]), confirmed=True)
            else:
                await _create_project(Path(st["path"]), confirmed=True)
        except bundle.ProjectError as e:   # same net as _on_project_path — an unhandled
            ui.notification_show(str(e), type="error", duration=10)      # effect exception
            _ensure_welcome()                                            # kills the session
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Project operation failed: {e}", type="error", duration=10)
            _ensure_welcome()

    @reactive.effect
    async def _create_subfolder():
        if not _clicked_dynamic("confirm_create_subfolder"):
            return
        ui.modal_remove()
        st = _pending_pick.pop("clash", None)
        if not st:
            _ensure_welcome()
            return
        try:
            # Dispatched UNCONFIRMED on purpose: the clash check re-runs against the
            # subfolder, so the dialog simply reappears (deeper path shown) if that
            # one is occupied too. Zero disk effects until a clean or confirmed pass.
            if st["purpose"] == "import_target":
                await _import_bundle_to(Path(st["sub"]))
            elif st["purpose"] == "save_as":
                await _save_as_project(Path(st["sub"]))
            else:
                await _create_project(Path(st["sub"]))
        except bundle.ProjectError as e:
            ui.notification_show(str(e), type="error", duration=10)
            _ensure_welcome()
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Project operation failed: {e}", type="error", duration=10)
            _ensure_welcome()

    async def _open_project_path(p: Path):
        if not p.is_file():
            ui.notification_show("That file doesn't exist.", type="error", duration=6)
            _ensure_welcome()
            return
        if bundle.classify_bundle(p) == "project":
            await _open_in_place(p)
            return
        # A portable bundle (cloud save / export): its content lives inside the zip. Hard
        # gate — no "open temporarily"; it lands in a project folder or not at all.
        _pending_import["src"] = str(p)
        sibs = any((p.parent / d).is_dir() for d in _PROJECT_DIRS)
        ui.modal_show(ui.modal(
            ui.p(f"“{p.name}” is a portable project file. Import it into a project folder "
                 "to work on it here."),
            title="Open project file",
            footer=ui.TagList(
                ui.input_action_button("import_cancel", "Cancel"),
                *([ui.input_action_button("import_open_here", "Open in place here")]
                  if sibs else []),
                ui.input_action_button("import_to_folder", "Import into a project…",
                                       class_="btn-primary")),
            easy_close=not _gated()))

    async def _open_in_place(main_file: Path, *, stamp: bool = False):
        payload = bundle.restore_in_place(main_file)   # parse/validate BEFORE any reset —
        #                                  a corrupt file must never cost the live session
        source_state = payload.get("state") or {}
        # Pre-identity projects mint their UUIDs during _rehydrate; persist them right away
        # so a read-only comparison of this file sees stable identities from now on.
        needs_identity_upgrade = not (source_state.get("project_id")
                                      and source_state.get("site_id"))
        if _ws["project_file"] and _ws["project_file"] != str(main_file):
            try:
                _save_project_file()                   # parting save of the old project
            except Exception:  # noqa: BLE001
                pass
        await _reset_memory_state()
        _adopt_workspace(main_file.parent, main_file)
        await _rehydrate(payload)
        if stamp or needs_identity_upgrade:
            _save_project_file()   # unmarked file opened in place by choice: stamp the marker
        await _post_title()
        ui.notification_show(f"Opened {main_file.name}", duration=5)

    @reactive.effect
    def _import_cancel():
        if _clicked_dynamic("import_cancel"):
            ui.modal_remove()
            _pending_import.pop("src", None)
            _ensure_welcome()

    @reactive.effect
    async def _import_to_folder():
        if not _clicked_dynamic("import_to_folder"):
            return
        ui.modal_remove()
        await _pick_path("import_target", save=True)

    @reactive.effect
    async def _import_open_here():
        if not _clicked_dynamic("import_open_here"):
            return
        ui.modal_remove()
        src = _pending_import.pop("src", None)
        if not src:
            _ensure_welcome()
            return
        try:
            await _open_in_place(Path(src), stamp=True)
        except bundle.ProjectError as e:   # corrupt file must not kill the session (or gate)
            ui.notification_show(str(e), type="error", duration=10)
            _ensure_welcome()
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Couldn't open the project: {e}", type="error", duration=10)
            _ensure_welcome()

    async def _import_bundle_to(target_main: Path, *, confirmed: bool = False):
        target_main = pathpick.ensure_hype_suffix(target_main)
        folder = target_main.parent
        if not _pending_import.get("src"):     # stale dispatch: nothing left to import
            _ensure_welcome()
            return
        # Gate BEFORE extraction: restore_workspace writes content dirs immediately, so
        # an occupied folder must be confirmed first. src stays stashed across the modal
        # round-trip and is consumed exactly once, at the proceed point below.
        names, foreign, others = bundle.folder_clash(folder, target_main)
        if (names or others) and not confirmed:
            _show_clash_modal("import_target", target_main, names, foreign, others)
            return
        src = _pending_import.pop("src")
        _warn_path_advisories(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "aerials").mkdir(exist_ok=True)
        ui.notification_show("Importing project…", duration=None, id="open_prog")
        try:
            # Extract into the NEW folder first — non-destructive to the current session.
            payload = bundle.restore_workspace(src, folder)
            if _ws["project_file"]:
                try:
                    _save_project_file()
                except Exception:  # noqa: BLE001
                    pass
            await _reset_memory_state()
            _adopt_workspace(folder, target_main)
            await _rehydrate(payload)
            _save_project_file()
            await _post_title()
            ui.notification_show(f"Imported {target_main.name} into {folder.name}",
                                 duration=6)
        finally:
            ui.notification_remove("open_prog")

    async def _save_as_project(target_main: Path, *, confirmed: bool = False):
        """Desktop Save As: copy the WHOLE project (content dirs + a fresh main .hype) to a
        new name/location, then switch to it. Copy is two-phase (copy, then reset+rebind+
        rehydrate) so a failure leaves the current project fully intact; _rehydrate
        detokenizes the snapshot against the NEW work_dir, healing every cached absolute
        path, and its stem-sync renames the project to the new stem."""
        src_main = _ws["project_file"]
        if not src_main:
            _ensure_welcome()
            return
        target_main = pathpick.ensure_hype_suffix(target_main)
        folder = target_main.parent
        same_folder = False
        try:
            if target_main.resolve() == Path(src_main).resolve():
                _do_desktop_save()         # picked the current file: that IS a plain Save
                return
            same_folder = folder.resolve() == work_dir.resolve()
        except OSError:
            pass
        if same_folder:
            ui.notification_show("Pick a different folder. Two projects can't share one "
                                 "folder; their model runs would overwrite each other's "
                                 "files.", type="warning", duration=8)
            return
        names, foreign, others = bundle.folder_clash(folder, target_main)
        if (names or others) and not confirmed:
            _show_clash_modal("save_as", target_main, names, foreign, others)
            return
        if _busy_tasks():                  # the clash modal may have sat open a while
            ui.notification_show("A task is still running. Wait for it to finish (or "
                                 "cancel it) before using Save As.", type="warning",
                                 duration=6)
            return
        _warn_path_advisories(folder)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "aerials").mkdir(exist_ok=True)
        try:
            _save_project_file()           # parting save: the copy carries latest settings
        except Exception:  # noqa: BLE001
            pass
        with reactive.isolate():
            copied_state = _project_state()
            # Save As is a distinct project lineage at the same physical site.
            copied_state["project_id"] = project_meta.new_identity()
            copied_state["site_id"] = (copied_state.get("site_id")
                                       or project_meta.new_identity())
            payload = {"state": copied_state, "vectors": _current_vectors(),
                       "params": params(), "run_config": _run_config(),
                       "assessment_input": input_snapshot(), "scoring_profile": None,
                       "extracted": 0, "restored": None}
        ui.notification_show("Copying project files…", duration=None, id="saveas_prog")
        try:
            bundle.copy_project_tree(work_dir, folder)
        except OSError as e:
            # copy_project_tree already rolled back the dirs it created fresh; we are
            # still bound to the old project, fully intact.
            ui.notification_show(f"Couldn't copy the project: {e}", type="error",
                                 duration=10)
            return
        finally:
            ui.notification_remove("saveas_prog")
        await _reset_memory_state()
        _adopt_workspace(folder, target_main)      # old folder is a project — left intact
        await _rehydrate(payload, keep_selection=True)   # heals paths; stem-sync renames
        #                                                  project; the user never "left"
        _save_project_file()                       # the NEW main file, freshly stamped
        await _post_title()
        ui.notification_show(f"Saved as {target_main.name}. You are now working in "
                             f"{folder}.", duration=6)

    # Autosave (desktop, project open): each completed stage writes the small main file, so
    # closing without Save loses at most a few clicks. The subscriptions below are exactly
    # the reactives the done-handlers set; effects batch per flush, so one completion = one
    # save. Restores flip _autosave["restoring"] and clear it on_flushed → zero saves.
    @reactive.effect
    def _autosave_on_results():
        if not runmode.IS_DESKTOP:
            return
        _ = (reach_feat(), dem_path(), dem_src(), carve_meta(), ras_result(), run_result(),
             hz_result(), alt_result(), soil_snapshot(), flow_lookup(), results_model(),
             input_snapshot(), wse_extent_feat(), obs_wells(), well_pairs(), wells_ver(),
             map_layers(), map_layers_ver())
        # wells_ver: typed screen elevations / observed heads are FIELD DATA — they autosave
        # like structural changes (unlike gradient typing, they exist nowhere else).
        # map_layers_ver: opacity/color/visibility edits mutate records in place, so the
        # ver counter is what makes them reach the save.
        if _ws["project_file"] is None or _autosave["restoring"]:
            return
        try:
            _save_project_file()
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Autosave failed: {e}", type="warning", duration=6)

    # ---- properties panes (right panel): one builder per tree node; bodies are the former
    # left-pane step branches, verbatim (they close over the server-scope reactives) ----
    def _pane_reach():
            return ui.TagList(
                ui.input_text("address", "Address, place, or stream", value=_keep("address", ""),
                              placeholder="e.g. Atlanta, GA  ·  Utoy Creek"),
                ui.div(ui.input_action_button("find_address", "Find on map",
                                              class_="btn-sm btn-outline-secondary"),
                       class_="hype-actions"),
                ui.input_radio_buttons(
                    "delineate_mode", "Define the reach",
                    {"auto": "Auto — pick two points on a stream",
                     "manual": "Manual — draw the centerline"}, selected=delineate_mode()),
                ui.panel_conditional(
                    "input.delineate_mode === 'auto'",
                    ui.output_ui("nhd_status_ui"),
                    ui.output_ui("auto_readout"),
                    ui.div(ui.input_action_button("clear_points", "Clear reach",
                                                  class_="btn-sm btn-outline-secondary"),
                           class_="hype-actions")),
                ui.panel_conditional(
                    "input.delineate_mode === 'manual'",
                    ui.div("Draw the centerline ", ui.tags.b("from upstream to downstream"),
                           ". The direction sets which side is the left and right floodplain. "
                           "The drainage area fills in from NHD when the stream is mapped; "
                           "enter or adjust it if needed.", class_="hype-instr"),
                    ui.div(
                        ui.input_numeric("manual_da", "Drainage area (km²)",
                                         value=_keep("manual_da", None), min=0.01, step=0.5),
                        _info_tip("Estimates bankfull channel size from regional curves "
                                  "(Bieger et al.), which sizes the floodplain boundaries "
                                  "and the terrain download."),
                        class_="hype-field-inline"),
                    ui.output_ui("manual_reach_status"),
                    ui.div(ui.input_action_button("clear_draw", "Clear reach",
                                                  class_="btn-sm btn-outline-secondary"),
                           class_="hype-actions")),
                ui.output_ui("reach_edit_ctl"),   # Edit centerline / Done (both modes, once traced)
                _next_hint("terrain.dem", "Next: Terrain →"),
            )

    def _pane_dem():
            display_ctrls = []
            if dem_path() is not None:            # display controls only once terrain exists
                with reactive.isolate():          # persisted slider state; changes must not
                    hs0 = float(dem_hs_v())       # re-render this pane (remount footgun)
                    op0 = float(dem_opacity_v())
                display_ctrls = [ui.accordion(
                    ui.accordion_panel(
                        "Display options",
                        ui.input_slider("dem_hs", "Hillshade strength (0 = flat colors)",
                                        min=0.0, max=8.0, value=hs0, step=0.5),
                        ui.input_slider("dem_opacity", "DEM opacity", min=0.0, max=1.0,
                                        value=op0, step=0.05),
                        ui.div(ui.input_action_button(
                            "dem_stretch_btn", "Fit legend to view",
                            class_="btn-sm btn-outline-secondary"), class_="hype-actions"),
                        ui.output_ui("dem_legend"),
                    ),
                    open=False, id="dem_disp_acc",
                )]
            fetch_ctrls = [
                ui.input_select("dem_res", "DEM resolution",
                                {"auto": "Auto (finest available)", "1": "1 m",
                                 "3": "3 m", "5": "5 m", "10": "10 m"},
                                selected=str(_keep("dem_res", "auto"))),
                ui.div(ui.input_action_button(
                    "fetch_dem",
                    "Re-fetch terrain" if dem_path() is not None else "Fetch terrain",
                    class_="btn-primary"), class_="hype-actions"),
            ]
            if runmode.IS_DESKTOP:
                with reactive.isolate():   # mode is read-only here; a subscribing read would
                    mode0 = (dem_src() or {}).get("mode", "3dep")   # remount the radio mid-click
                source_ctrls = [
                    ui.div(ui.input_radio_buttons(
                        "dem_src_mode", "Terrain source",
                        {"3dep": "USGS 3DEP", "local": "Local raster"},
                        selected=mode0, inline=True), class_="hype-srcpick"),
                    # != 'local' keeps the 3DEP controls visible during the first-render tick
                    # before the radio registers (an undefined input hides BOTH sections).
                    ui.panel_conditional("input.dem_src_mode != 'local'", *fetch_ctrls),
                    ui.panel_conditional("input.dem_src_mode == 'local'",
                                         ui.output_ui("dem_local_src")),
                ]
            else:                          # cloud: the 3DEP download is the only source
                source_ctrls = fetch_ctrls
            return ui.TagList(
                *source_ctrls,
                ui.output_ui("busy"),
                ui.output_ui("dem_status"),
                *display_ctrls,
                _next_hint("bnd", "Next: Boundaries →"),
            )

    def _pane_boundaries():
            return ui.TagList(
                ui.input_select("fp_mult", "Floodplain width (× bankfull depth)",
                                {"2": "2×", "5": "5×", "10": "10× (default)"},
                                selected=str(_keep("fp_mult", "10"))),
                ui.div(ui.input_action_button(
                    "regen",
                    "Regenerate boundaries" if _domain_build() is not None
                    else "Generate boundaries",
                    class_="btn-primary"), class_="hype-actions"),
                ui.output_ui("draw_status"),
                ui.output_ui("domain_warning"),
                _next_hint("sw", "Next: Surface Water Modeling →"),
            )

    def _pane_bnd_side(slot, label, color):
        def _pane():
            sv = _slot_value(slot)
            present = sv() is not None if sv is not None else False
            with reactive.isolate():
                active = bnd_slot() == slot
            rows = [ui.div(ui.span(class_="hype-leg-swatch", style=f"background:{color};"),
                           ui.span(label, class_="hype-leg-name"),
                           ui.span(ui.span(class_="hype-st st-done" if present
                                           else "hype-st st-todo"),
                                   "drawn" if present else "not drawn",
                                   class_="hype-leg-mark ok" if present else "hype-leg-mark"),
                           class_="hype-leg-row")]
            if active and present:
                hint = ("Editing on the map — drag vertices; Save/Done in the map bar, or "
                        "select another item to commit.")
            elif active:
                hint = "Draw the line on the map (click to place vertices, double-click to finish)."
            else:
                hint = "Selecting this boundary starts an edit on the map."
            conflict = next((c for c in bnd_conflicts() if c["slot"] == slot), None)
            if conflict is not None:
                rows.append(ui.div(conflict["msg"] + " Model runs are blocked until this "
                                   "is fixed.", class_="hype-card err"))
            return ui.TagList(
                ui.div(hint, class_="hype-instr"), *rows,
                ui.div(ui.input_action_button("bnd_clear_side", "Clear & redraw",
                                              class_="btn-sm btn-outline-secondary"),
                       class_="hype-actions") if present else None,
            )
        return _pane

    def _pane_sw():
            with reactive.isolate():          # persisted prefill only; a live (subscribing) read
                slope0 = ras_slope_default()  # here would re-render this pane on every change
            return ui.TagList(
                # Canonical streamflow — always available (used by the RAS model AND, later, the
                # hyporheic connectivity metric) (§5.1). Get USGS Flow opens the review modal.
                ui.div(
                    ui.input_numeric("ras_flow", "Streamflow (cfs)", value=_keep("ras_flow", 100.0),
                                     min=0.1, step=10.0),
                    ui.input_action_button("get_usgs_flow", "Get USGS Flow",
                                           class_="btn-outline-primary btn-sm"),
                    class_="hype-flow-input"),
                ui.div("Water surface model (HEC-RAS 2D)", class_="hype-subhead"),
                ui.input_numeric("ras_slope", "Normal-depth friction slope",
                                 value=_keep("ras_slope",
                                             round(slope0, 5) if slope0 else 0.001),
                                 min=0.00001, step=0.0005),
                ui.input_numeric("ras_n", "Manning's n", value=_keep("ras_n", 0.06),
                                 min=0.01, max=0.2, step=0.005),
                ui.input_numeric("ras_cell", "Mesh cell size (m)",
                                 value=_keep("ras_cell", 10.0), min=1.0, step=1.0),
                ui.accordion(
                    ui.accordion_panel(
                        "Advanced",
                        # Fixed engine, shown as a note — SWE-explicit is the only RAS 2025
                        # engine that runs on Posit Connect Cloud (Diffusion Wave needs Intel
                        # MKL, the GPU solver needs CUDA), so there is nothing to select.
                        ui.div("Engine: 2D Shallow Water (CPU)", class_="hype-instr"),
                        ui.input_numeric("ras_hours", "Simulation duration (hr)",
                                         value=_keep("ras_hours", 6.0), min=0.5, step=0.5),
                        ui.input_numeric("ras_dt", "Compute timestep (s)",
                                         value=_keep("ras_dt", 10.0), min=0.1, step=1.0),
                        ui.input_numeric("ras_out_min", "Output interval (min)",
                                         value=_keep("ras_out_min", 15.0), min=1.0,
                                         step=5.0),
                    ),
                    open=False, id="ras_adv",
                ),
                ui.output_ui("ras_estimate"),
                ui.output_ui("ras_controls"),  # Run/Cancel + live log + summary
                _next_hint("gw", "Next: Groundwater Modeling →"),
            )

    def _pane_k():
            return ui.TagList(
                ui.input_numeric("kh", "Horizontal K (m/d)", value=_keep("kh", 10.0),
                                 min=0.0001, step=1.0),
                ui.input_numeric("kv", "Vertical K (m/d)", value=_keep("kv", 1.0),
                                 min=0.0001, step=0.5),
                ui.input_numeric("porosity", "Porosity", value=_keep("porosity", 0.3),
                                 min=0.01, max=0.6, step=0.05),
                ui.div("NRCS soils (SSURGO)", class_="hype-subhead"),
                ui.output_ui("soil_k_status"),
                ui.div(ui.input_action_button("get_nrcs_soils", "Get NRCS Soils K",
                                              class_="btn-sm btn-outline-primary"),
                       class_="hype-actions"),
                ui.div("K-zones", class_="hype-subhead"),
                ui.input_checkbox("use_kzones", "Use hydraulic-conductivity zones",
                                  value=bool(_keep("use_kzones", False))),
                ui.panel_conditional(
                    "input.use_kzones === true",
                    ui.div("Each zone has its own KH and KV; zones override the base K "
                           "where they cover.", class_="hype-instr"),
                    ui.div(
                        ui.input_action_button("kz_add", "Add K-zone", class_="btn-sm btn-primary"),
                        ui.input_action_button("kz_clear", "Clear all",
                                               class_="btn-sm btn-outline-secondary"),
                        class_="hype-bnd-row"),
                    ui.output_ui("kzone_list")),
                _next_hint("gw.mesh", "Next: Model grid →"),
            )

    def _pane_mesh():
            _se = streambed_elevs()
            _mo = model_origin_effective()
            _help = ("Vertical origin of the model grid: the upstream streambed (thalweg) elevation. "
                     "Layers step down from here at the thickness above; each cell's top follows the "
                     "terrain, so the top cell is one layer thick at the upstream streambed. Default "
                     "= the minimum terrain elevation along the upstream boundary (with the channel "
                     "carve applied). Edit to override — your value persists until New project. Downstream "
                     "cells that would sit above ground are switched off.")
            _up_txt = (f"Upstream streambed (default): {_se['up']:.2f} m"
                       if _se and _se.get("up") is not None else "Upstream streambed: —")
            _dn_txt = (f"Downstream streambed: {_se['down']:.2f} m"
                       if _se and _se.get("down") is not None else "Downstream streambed: —")
            _gcol = grid_color3d_v()      # un-isolated: a swatch click re-renders the ring
            return ui.TagList(
                ui.input_numeric("cell_size", "Cell size (m)",
                                 value=_keep("cell_size", 10.0), min=1.0, step=1.0),
                ui.div("Smaller cells make a finer grid.", class_="hype-help"),
                ui.input_numeric("gw_mod_depth", "Model depth below water surface (m)",
                                 value=_keep("gw_mod_depth", 6.0), min=1.0, step=0.5),
                ui.input_numeric("z", "Layer thickness (m)",
                                 value=_keep("z", 0.25), min=0.05, step=0.05),
                ui.div("Model depth ÷ thickness sets the layer count.", class_="hype-help"),
                ui.accordion(
                    ui.accordion_panel(
                        "Vertical origin",
                        ui.div(
                            ui.input_numeric("model_origin", "Model origin (m)",
                                             value=(round(float(_mo), 2)
                                                    if _mo is not None else None),
                                             step=0.1),
                            _info_tip(_help),
                            class_="hype-field-inline"),
                        ui.div(f"{_up_txt}  ·  {_dn_txt}", class_="hype-instr"),
                    ),
                    ui.accordion_panel(
                        "3D display",
                        ui.input_checkbox("grid_wireframe", "Wireframe grid",
                                          value=bool(_keep("grid_wireframe", False))),
                        ui.input_slider("grid_opacity3d", "Grid line opacity",
                                        min=0.05, max=1.0,
                                        value=float(_keep("grid_opacity3d", 1.0)), step=0.05),
                        ui.div(ui.span("Grid color", class_="hype-fpsel-lbl"),
                               ui.tags.button(
                                   "Default", type="button",
                                   class_="hype-anim-stylebtn"
                                   + (" active" if _gcol is None else ""),
                                   title="Stock edges with the elevation-colored top",
                                   onclick=("Shiny.setInputValue('grid_color3d_evt', "
                                            "{c: 'default', n: Date.now()}, "
                                            "{priority: 'event'})")),
                               *[ui.tags.button(
                                     type="button",
                                     class_="hype-anim-swatch"
                                     + (" active" if _gcol == c else ""),
                                     style=f"background:{c};", title=nm,
                                     onclick=("Shiny.setInputValue('grid_color3d_evt', "
                                              f"{{c: '{c}', n: Date.now()}}, "
                                              "{priority: 'event'})"))
                                 for c, nm in (("#ffffff", "White"), ("#808080", "Gray"),
                                               ("#d32f2f", "Red"), ("#2563eb", "Blue"))],
                               class_="hype-fpsel-row"),
                    ),
                    open=False, id="mesh_adv_acc",
                ),
                ui.output_ui("estimate_box"),
                ui.div(ui.input_action_button("compute_mesh", "Preview grid in 3D",
                                              class_="btn-sm btn-outline-secondary"),
                       class_="hype-actions"),
                ui.output_ui("mesh_status"),
                _next_hint("gw", "Next: run groundwater →"),
            )

    def _hub_row(ok, label, detail, jump):
        # One readiness row of the groundwater run hub: status mask + name + current values +
        # an Edit jump (data-jump — same delegation as every other chip).
        return ui.div(
            ui.span(class_="hype-st " + ("st-done" if ok else "st-todo")),
            ui.span(label, class_="hype-leg-name"),
            ui.span(detail, class_="hype-hub-detail"),
            ui.tags.button("Edit", type="button", class_="hype-hub-edit",
                           **{"data-jump": jump}),
            class_="hype-leg-row")

    def _pane_gw():
            # The single run hub: readiness checklist → boundary conditions → particle
            # accordion → the ONE "Run groundwater model" button. Light reads only.
            if ras_result() is not None:
                sw_ok, sw_detail = True, "modeled (HEC-RAS)"
            else:
                # model-only: a restored wetted extent / uploaded raster no longer satisfies
                # the run path (_wse_path accepts only the RAS result)
                sw_ok, sw_detail = False, "not set — run the surface model"
            kz_n = len(kzone_feats() or []) if bool(_safe("use_kzones", False)) else 0
            k_detail = (f"KH {float(_safe('kh', 10.0)):g} · KV {float(_safe('kv', 1.0)):g} m/d"
                        + (f" · {kz_n} zone{'' if kz_n == 1 else 's'}" if kz_n else ""))
            grid_detail = (f"{float(_safe('cell_size', 10.0)):g} m cells · "
                           f"{float(_safe('gw_mod_depth', 6.0)):g} m deep")
            _bc0 = str(_keep("bc_mode", BC_QUAL))
            if _bc0 == BC_CORNER:                    # legacy mode: same corner numerics as points
                _bc0 = BC_PROFILE
            with reactive.isolate():                 # prefill only — _track_ref_slope_auto keeps
                _ov, _auto = ref_slope_override(), ref_slope_auto()   # the numeric live after
                _rs0 = (_ov if _ov is not None else
                        (round(_auto.value, 6) if _auto is not None
                         else _keep("g_ref_slope", 0.005)))
            return ui.TagList(
                ui.div(
                    _hub_row(True, "Subsurface properties", k_detail, "gw.k"),
                    _hub_row(True, "Model grid", grid_detail, "gw.mesh"),
                    _hub_row(sw_ok, "Surface Water Modeling", sw_detail, "sw"),
                    class_="hype-legend"),
                ui.input_select("bc_mode", "Boundary condition",
                                {BC_QUAL: "Qualitative",
                                 BC_PROFILE: "Gradient points (spatially varying)"},
                                selected=_bc0),
                ui.panel_conditional(
                    f"input.bc_mode === '{BC_QUAL}'",
                    ui.div(
                        ui.input_select("g_qual_left", "Left floodplain", _QUAL_CHOICES,
                                        selected=str(_keep("g_qual_left", "slightly_gaining"))),
                        ui.input_select("g_qual_right", "Right floodplain", _QUAL_CHOICES,
                                        selected=str(_keep("g_qual_right", "slightly_gaining"))),
                        class_="hype-field-row"),
                    # Reference slope on its own full-width row so the label never wraps;
                    # the two multipliers pair up below it.
                    ui.input_numeric("g_ref_slope", "Reference slope (m/m)", value=_rs0,
                                     min=0.0, step=0.0005),
                    ui.div(
                        ui.input_numeric("g_mult_slight", "Slight ×",
                                         value=_keep("g_mult_slight", 0.5), min=0.0, step=0.1),
                        ui.input_numeric("g_mult_strong", "Strong ×",
                                         value=_keep("g_mult_strong", 1.0), min=0.0, step=0.1),
                        class_="hype-field-row"),
                    ui.output_ui("gradient_qual_preview")),
                ui.panel_conditional(
                    f"input.bc_mode === '{BC_PROFILE}'",
                    # One table is the whole mode: corner rows (mandatory anchors, no remove)
                    # + map-added points, gradients editable in place.
                    ui.output_ui("gradient_pts_table"),
                    ui.output_ui("gradient_pts_msgs")),
                ui.div(ui.input_action_button("run_model", "Run groundwater model",
                                              class_="btn-primary"), class_="hype-actions"),
                *_gw_delineate_section(),
            )

    def _gw_delineate_section():
            # Zone delineation lives with the run hub (user feedback: it was buried on the
            # Hyporheic Zone node). The run_hz launcher keeps its own gates; progress still
            # shows on the Hyporheic Zone pane because _start_hz auto-selects gw.res.hz.
            ppc = int(_safe("hz_ppc", 1))
            est = _hz_particle_estimate(ppc)
            parts = [
                ui.div("Hyporheic zone", class_="hype-subhead"),
                ui.div(
                    ui.input_select("hz_ppc", "Particles per cell",
                                    {"1": "1 (fastest)", "3": "3", "6": "6", "9": "9 (finest)"},
                                    selected=str(_safe("hz_ppc", "1"))),
                    # Says what it does NOT do, because that is the part users get wrong: this
                    # sizes the zone-EXTENT release only. Exchange flows and the residence-time
                    # distribution come from a separate flux pass seeded at the streambed.
                    _info_tip("How finely each cell is sampled when mapping the zone's extent "
                              "and volume. Exchange flows and residence times come from a "
                              "separate pass at the streambed and do not change with this."),
                    class_="hype-field-inline"),
                ui.div(f"≈ {est:,} particles" if est else
                       "Run the groundwater model to estimate the particle count.",
                       class_="hype-instr"),
                ui.div(
                    ui.input_numeric("hz_sample", "Displayed paths per class",
                                     value=int(_safe("hz_sample", 500)), min=50,
                                     max=(100_000 if runmode.IS_DESKTOP else 1000),
                                     step=50),
                    # "and flow accounting" used to be here and was wrong: the flow accounting is
                    # produced by the flux pass, which these particles play no part in.
                    _info_tip("Display cap: this many flow paths per class get full "
                              "trajectory geometry for the 2D and 3D views. All particles "
                              "still drive the classification and volumes. Applies on the "
                              "next run."),
                    class_="hype-field-inline"),
            ]
            if runmode.IS_DESKTOP:
                # Streambed flux-pass release density (engine default 4). Desktop only:
                # raising it refines the residence-time distribution behind the screening
                # numbers; cloud keeps the fixed engine value.
                parts.append(ui.div(
                    ui.input_numeric("hz_iface_ppc", "Flux particles per cell",
                                     value=int(_safe("hz_iface_ppc", 4)), min=1, step=1),
                    _info_tip("How many particles are released per streambed inflow cell "
                              "for the flux pass that produces exchange flows and "
                              "residence times. The default of 4 matches all existing "
                              "runs; higher values refine the residence-time "
                              "distribution and take longer. Applies on the next run."),
                    class_="hype-field-inline"))
            if hz_task.status() == "running":
                parts.append(ui.div("Calculations running; progress is on the ",
                                    ui.tags.b("Hyporheic Zone"), " node.",
                                    class_="hype-instr"))
                parts.append(_next_hint("gw.res.hz", "Go to Hyporheic Zone →"))
            else:
                ready = run_result() is not None
                parts.append(ui.div(ui.input_action_button(
                    "run_hz", "Run Hyporheic Zone calculations",
                    class_="btn-primary" if (ready and hz_result() is None)
                    else "btn-outline-primary"), class_="hype-actions"))
            return parts

    def _pane_run():
            return ui.TagList(
                ui.output_ui("run_status"),
                ui.tags.pre(ui.output_text("run_log"), class_="hype-log"),
                ui.div(ui.input_action_button("cancel_run", "Cancel run",
                                              class_="btn-sm btn-outline-danger"),
                       class_="hype-actions"),
            )

    def _pane_head():
            tifs = head_tifs()
            if not tifs:
                return ui.div("Head layers appear here after a run.", class_="hype-instr")
            with reactive.isolate():         # persisted slider state, re-read on each pane re-run
                _ly = max(1, min(int(head_layer_v()), len(tifs)))
                _op = float(head_opacity_v())
                _ct = bool(hd_contours_v())
            return ui.TagList(
                ui.input_slider("head_layer", "Head layer (1 = top)", min=1, max=len(tifs),
                                value=_ly, step=1),
                ui.input_slider("head_opacity", "Head opacity", min=0.0, max=1.0,
                                value=_op, step=0.05),
                ui.input_checkbox("head_contours_chk", "Show head contours", value=_ct),
                ui.output_ui("head_legend"),
            )

    def _fpsel_buttons():
            # Selection mode: plain buttons (no server round-trip) — www/flowpath_select.js
            # arms/disarms the crossing-window tool via document-level delegation and keeps
            # the active states in sync, so any pane can carry a copy of this row.
            return ui.div(
                ui.tags.button("Single", type="button",
                               class_="hype-fpsel-single active",
                               title="Click one flow path (or its entry/return dot) on the map"),
                ui.tags.button("Box select", type="button",
                               class_="hype-fpsel-multi",
                               title="Drag a crossing window; every flow path it touches is "
                                     "selected"),
                class_="hype-fpsel-row")

    def _pane_paths():
            if hz_view() is None:          # follows the displayed run
                return ui.div("Run the Hyporheic Zone calculations (Groundwater Modeling pane) "
                              "to map the flow paths, split into the four exchange classes.",
                              class_="hype-instr")
            with reactive.isolate():       # persisted control state, re-read on each pane re-run
                _aon = bool(fp_anim_on_v())
                _asp = float(fp_anim_speed_v())
                _lshow = bool(fp_line_show_v())
                _lw = float(fp_line_weight_v())
                _lop = float(fp_line_opacity_v())
            _acol = fp_anim_color_v()      # un-isolated on purpose: a swatch or style click
            _asty = fp_anim_style_v()      # re-renders the pane so the active state follows
            _amode = fp_anim_mode_v()      # un-isolated: the active color-by button follows
            _lmode = fp_line_mode_v()      # un-isolated: the active mode button follows
            return ui.TagList(
                ui.div("Click a path or dot for its properties, or drag a box to "
                       "select several.", class_="hype-instr"),
                _fpsel_buttons(),
                ui.div("Path lines",
                       _info_tip("Display styling for the flow path lines themselves. Hiding "
                                 "the lines keeps the particle animation and click selection "
                                 "working, so you can watch the particles alone. The rainbow "
                                 "color modes map residence time onto a log scale stretched "
                                 "between the fastest and slowest displayed paths; the legend "
                                 "below shows the values."),
                       class_="hype-props-title"),
                ui.input_checkbox("fp_line_show", "Show path lines", value=_lshow),
                ui.input_slider("fp_line_weight", "Line thickness (px)", min=0.5, max=8.0,
                                value=_lw, step=0.5),
                ui.input_slider("fp_line_opacity", "Line opacity", min=0.05, max=1.0,
                                value=_lop, step=0.05),
                ui.div(ui.span("Color by", class_="hype-fpsel-lbl"),
                       *[ui.tags.button(
                             lbl, type="button",
                             class_="hype-anim-stylebtn" + (" active" if key == _lmode else ""),
                             title=tip,
                             onclick=("Shiny.setInputValue('fp_line_mode_evt', "
                                      f"{{m: '{key}', n: Date.now()}}, {{priority: 'event'}})"))
                         for key, lbl, tip in (
                             ("class", "Class",
                              "Each exchange class keeps its identity color"),
                             ("total", "Total time",
                              "Rainbow by each path's total residence time: blue is "
                              "quick, red is slow. One fixed color per line."),
                             ("elapsed", "Elapsed",
                              "Rainbow along each path from blue at its start toward "
                              "its total time color at its end"))],
                       class_="hype-fpsel-row"),
                ui.div("Particle animation",
                       _info_tip("One particle travels each displayed flow path. Travel time is "
                                 "proportional to the path's residence time, so quick loops mean "
                                 "fast exchange."),
                       class_="hype-props-title"),
                ui.input_checkbox("fp_anim_on", "Animate flow paths", value=_aon),
                ui.input_slider("fp_anim_speed", "Speed (slow to fast)", min=0.5, max=10.0,
                                value=_asp, step=0.5),
                ui.div(ui.span("Style", class_="hype-fpsel-lbl"),
                       *[ui.tags.button(
                             lbl, type="button",
                             class_="hype-anim-stylebtn" + (" active" if key == _asty else ""),
                             title=tip,
                             onclick=("Shiny.setInputValue('fp_anim_style_evt', "
                                      f"{{s: '{key}', n: Date.now()}}, {{priority: 'event'}})"))
                         for key, lbl, tip in (
                             ("comet", "Comet streaks",
                              "Short fading tail; tail length shows each particle's speed"),
                             ("dots", "Dots", "One small dot per path"))],
                       class_="hype-fpsel-row"),
                ui.div(ui.span("Color by", class_="hype-fpsel-lbl"),
                       *[ui.tags.button(
                             lbl, type="button",
                             class_="hype-anim-stylebtn" + (" active" if key == _amode
                                                            else ""),
                             title=tip,
                             onclick=("Shiny.setInputValue('fp_anim_mode_evt', "
                                      f"{{m: '{key}', n: Date.now()}}, "
                                      "{priority: 'event'})"))
                         for key, lbl, tip in (
                             ("solid", "Solid",
                              "Every particle uses the picked color"),
                             ("total", "Total time",
                              "Rainbow by each path's total residence time: blue is "
                              "quick, red is slow. One fixed color per particle."),
                             ("elapsed", "Elapsed time",
                              "Rainbow by time in transit: each particle starts blue "
                              "and shifts toward red as it ages along its path."))],
                       class_="hype-fpsel-row"),
                ui.div(ui.span("Particle color", class_="hype-fpsel-lbl"),
                       *[ui.tags.button(
                             type="button",
                             class_=("hype-anim-swatch"
                                     + (" active" if c == _acol else "")
                                     + ("" if _amode == "solid" else " dim")),
                             style=f"background:{c};",
                             title=(n if _amode == "solid"
                                    else f"{n} (switches back to Solid)"),
                             onclick=("Shiny.setInputValue('fp_anim_color_evt', "
                                      f"{{c: '{c}', n: Date.now()}}, {{priority: 'event'}})"))
                         for c, n in zip(FP_ANIM_COLORS, FP_ANIM_COLOR_NAMES)],
                       class_="hype-fpsel-row"),
                ui.output_ui("fp_time_legend"),
                ui.output_ui("hz_sel_props"),
            )

    def _pane_wells():
        """Observation wells: field data vs the Basecase groundwater solution. The pane is a
        thin shell — the tables live in their own outputs so structural re-renders never
        remount an input mid-keystroke (the gradient-table discipline)."""
        return ui.TagList(
            _base_only_note("Observation well sampling"),
            ui.div("Place observation wells on the map to compare computed heads from the "
                   "groundwater model with field measurements. The screen elevation picks "
                   "the model layer that is sampled.", class_="hype-instr"),
            ui.output_ui("wells_table"),
            ui.output_ui("wells_pairs"),
            ui.output_ui("wells_msgs"),
        )

    def _pane_maplyr():
        """User reference layers (path links, never copies). Thin shell — the row list lives
        in its own output; click a row (or its tree entry) for that layer's settings."""
        return ui.TagList(
            ui.div("Link raster or vector files from this computer as reference layers. "
                   "The project stores links, not copies.",
                   class_="hype-instr"),
            ui.output_ui("maplyr_rows"),
        )

    def _pane_ml_layer(uid: str):
        """Per-layer settings pane, opened by selecting the layer's tree row.

        Reads the record list and _ml_paint like maplyr_rows — NEVER map_layers_ver,
        which bumps on every slider drag and would remount the slider mid-drag. The
        slider keeps its ml_op_<uid> id so _ml_op_mirror works unchanged."""
        recs = map_layers()
        _ml_paint()
        rec = next((r for r in recs if r.get("id") == uid), None)
        if rec is None:
            return ui.div("This layer was removed.", class_="hype-instr hype-dim")
        with reactive.isolate():
            st = _ml_status.get(uid)
            reason = _ml_err.get(uid)
        body = [ui.div(
            ui.span("Raster" if rec.get("kind") == "raster" else "Vector",
                    class_="hype-ml-kind"),
            ui.span(rec.get("path") or "", class_="hype-ml-path",
                    title=rec.get("path") or ""),
            class_="hype-ml-head")]
        if st == "missing":
            body.append(ui.div(
                ui.span(class_="hype-st st-warn"),
                ui.span("The linked file was not found.", class_="hype-ml-missingtxt"),
                ui.tags.button(
                    "Locate file...", type="button",
                    class_="btn btn-sm btn-outline-secondary",
                    onclick=("Shiny.setInputValue('ml_warn','" + uid
                             + ":'+Date.now(),{priority:'event'})")),
                class_="hype-ml-missing"))
        elif st == "error":
            body.append(ui.div(reason or "could not be displayed",
                               class_="hype-ml-err", title=reason or ""))
        if rec.get("kind") == "vector":
            body.append(ui.div(
                ui.span("Line color", class_="hype-ml-lbl"),
                ui.tags.input(
                    type="color", value=rec.get("color") or ml_mod.DEFAULT_COLOR,
                    class_="hype-ml-color", title="Line color",
                    onchange=("Shiny.setInputValue('ml_color_evt',{u:'" + uid
                              + "',c:this.value,n:Date.now()},{priority:'event'})")),
                class_="hype-ml-ctrl"))
        body.append(ui.div(
            ui.input_slider(f"ml_op_{uid}", "Opacity", min=0.0, max=1.0, step=0.05,
                            value=float(rec.get("opacity", 0.8)), ticks=False,
                            width="100%"),
            class_="hype-ml-op"))
        body.append(ui.div(
            ui.tags.button(
                "Remove layer", type="button", class_="btn btn-sm btn-outline-danger",
                title="The file stays on disk.",
                onclick=("Shiny.setInputValue('ml_rm','" + uid
                         + ":'+Date.now(),{priority:'event'})")),
            class_="hype-actions"))
        return ui.TagList(*body)

    def _pane_results():
            parts = [
                ui.div("Groundwater run complete. The tree checkboxes show/hide each result "
                       "layer; select a layer for its display controls.", class_="hype-card ok"),
                _base_only_note("The run summary below"),
                ui.output_ui("result_summary"),
            ]
            parts = [p for p in parts if p is not None]
            if hz_result() is None:        # the one obvious next action after a run
                parts.append(_next_hint("gw", "Run Hyporheic Zone calculations →",
                                        primary=True))
            else:                          # zone delineated -> the report node is the takeaway
                parts.append(_next_hint("report", "Next: Site Reports →", primary=True))
            parts.append(ui.div("Results are in temporary storage. Use ",
                                ui.tags.b("Save"),
                                " in the header before you leave.", class_="hype-warn"))
            return ui.TagList(*parts)

    #: Which calculator each Screening tree node draws, derived from the registry rather than
    #: hand-written, so a node and its pane cannot drift apart and adding a calculator never
    #: touches this file. ONE NODE PER CALCULATOR now: Pollutant Attenuation is a group whose two
    #: mechanisms carry their own nodes, because the radio that used to switch them was doing the
    #: tree's job.
    FN_NODE_PROCESS = {fn_reg.pane_node(pk): pk for pk in fn_reg.SECTION_ORDER}
    #: Group nodes: the four functions, minus any whose node a calculator already claims.
    FN_GROUP_NODES = tuple(f.node_id for f in fn_reg.FUNCTIONS.values()
                           if f.node_id not in FN_NODE_PROCESS)

    def _fn_tip(tip):
        """`tip` is a registry `Help` card or, for a one-liner, a plain string."""
        return _info_tip(help=tip) if isinstance(tip, fn_reg.Help) else _info_tip(tip)

    def _fn_head(title, tip=None, *, tag=None):
        """The titled head shared by the group tables and the detail sparkline.

        `tag` is the tier chip ("hydraulics" / "assumed rate"). Method notes go in `tip`, not in
        prose: _info_tip is wired into exactly two CSS slots and hype-props-title is one of them."""
        head = [title]
        if tag:
            head.append(ui.span(tag, class_="hype-tag" + (" assumed" if tag != "hydraulics"
                                                          else "")))
        if tip:
            head.append(_fn_tip(tip))
        return ui.div(*head, class_="hype-props-title")

    def _fn_tbl(title, rows, tip=None, *, tag=None):
        """Titled table, copying the _pane_flows idiom."""
        body = [ui.tags.tr(ui.tags.td(k), ui.tags.td(v)) for k, v in rows if v is not None]
        return ui.TagList(_fn_head(title, tip, tag=tag),
                          ui.tags.table(ui.tags.tbody(*body), class_="hype-props-table"))

    def _pol_head(pre, s, spec, lead):
        """The header of one endpoint's panel: its name, what kind of endpoint it is, and its
        lead number.

        THE HEADER IS THE LEAD CARD. Collapsed, a list of ticked chemicals reads as a name-to-mass
        table a reader can scan; expanded, the panel adds only what supports that number. Nothing
        here is a literal: the label and the endpoint type come from the `Preset`, and the value is
        the same `_fn_val` call the card would have made, so the header and the report cannot word
        one endpoint's result differently."""
        kpi = next((k for k in spec.kpis if k.key == lead), None) or (spec.kpis[0] if spec.kpis
                                                                     else None)
        val = _fn_val(s, kpi, with_unit=False) if kpi is not None else None
        unit = _fn_unit(s, kpi) if kpi is not None else ""
        return ui.TagList(
            ui.span(pre.label, class_="hype-pol-name"),
            ui.span(fn_pol.TERMS[pre.endpoint].kind_label, class_="hype-tag assumed"),
            ui.span(ui.span(val or "n/a", class_="hype-pol-num"),
                    ui.span(unit, class_="hype-pol-unit") if unit else None,
                    class_="hype-pol-val"))

    def _fn_field(inp, tip):
        """Numeric input with its explanation in a tooltip rather than a paragraph beneath it."""
        return ui.div(inp, _fn_tip(tip), class_="hype-field-inline")

    def _tau_choice(kept, spec):
        """Which scenario button a stored rate selects.

        Needed because the two ends do not speak the same type: `_keep` hands back the float 8.0
        while a radio's keys are strings, so `str(8.0)` would match nothing and the control would
        open unselected. The nearest-match arm is for projects saved while this was a free numeric,
        which could hold any number at all -- and a snap is never silent, because `_fn_assumption`
        prints the response time actually in force directly under the headline."""
        opts = [float(v) for v, _ in spec.rate_scenarios]
        if not opts:
            return None
        try:
            want = float(spec.rate_central if kept in (None, "") else kept)
        except (TypeError, ValueError):
            want = float(spec.rate_central)
        return f"{min(opts, key=lambda v: abs(v - want)):g}"

    def _fn_label(s, spec_row):
        """A row or headline's label, preferring the one the RESULT carries.

        Pollutant endpoints choose their own vocabulary: a metal must read attenuation and an
        organic transformation (screening reference §7). The words come from `pollutants.TERMS`,
        which is validated against the reference's banned-word table, so the branch is data."""
        return (s.get(spec_row.label_key) or spec_row.label) if spec_row.label_key \
            else spec_row.label

    def _fn_unit(s, spec_row):
        """Likewise for units, so a microgram-per-litre endpoint reads g/day where a
        milligram-per-litre one reads kg/day."""
        return (s.get(spec_row.unit_key) or spec_row.unit) if spec_row.unit_key else spec_row.unit

    def _fn_val(s, spec_row, *, with_unit=True):
        """One registry PaneRow/PaneKpi rendered from the flat screen result, or None to drop it.

        `digits` is SIGNIFICANT FIGURES here, not decimal places: these quantities span orders of
        magnitude across sites, and decimal rounding collapsed a real sensitivity spread into
        "0.068 to 0.068". A headline passes with_unit=False because the KPI paints the unit in
        its own muted span beside the number."""
        v = s.get(spec_row.key)
        if v is None:
            return None
        unit = _fn_unit(s, spec_row) if with_unit else ""
        if spec_row.kind == "pct":
            return f"{report_mod.fmt(v * 100.0, 1)}%"
        if spec_row.kind == "pct_sig":
            return f"{report_mod.fmt_sig(v * 100.0, spec_row.digits)}%"
        if spec_row.kind == "int":
            return f"{report_mod.fmt(int(v))}{unit}"
        return f"{report_mod.fmt_sig(v, spec_row.digits)}{unit}"

    def _fn_rows(s, group_or_rows):
        """(label, value) pairs for a PaneGroup or a bare PaneRow tuple. List-driven groups (the
        thermal response bands) read their rows out of the result instead of the registry."""
        if getattr(group_or_rows, "list_key", ""):
            return [(b.get("label"), f"{report_mod.fmt((b.get('flow_fraction') or 0) * 100.0, 1)}%")
                    for b in (s.get(group_or_rows.list_key) or [])
                    if b.get("flow_fraction") is not None]
        rows = getattr(group_or_rows, "rows", group_or_rows)
        # A row whose registry entry carries a Help card gets an info tip beside its LABEL. Used
        # sparingly: only where the name alone misleads, such as the returning-path count, which
        # readers compare against the zone pane's much larger particle count.
        return [(ui.TagList(_fn_label(s, r), _fn_tip(r.help)) if r.help is not None
                 else _fn_label(s, r), _fn_val(s, r)) for r in rows]

    def _fn_kpi(s, spec, *, lead="", drop_lead=False):
        """The headline block: one item per registry PaneKpi that resolves, with its sensitivity
        range as an ATTRIBUTE of the number rather than a peer table row.

        `lead` names the KPI a FunctionSpec headlines. It goes FIRST and large; the rest of the
        declared set render smaller beside it. Passing "" leaves the declared order alone, so the
        first card leads. `drop_lead` omits it, for a caller that has already drawn that number
        somewhere the reader can see -- which is how the pollutant expanders put each endpoint's
        mass in its own header.

        THE BUG THIS SHAPE FIXES. `lead` used to be `only`, and it did what it said: every call
        site passed the headline, so a spec declaring three KPIs painted one and the other two --
        each with its own help card, units and sensitivity bounds -- rendered NOWHERE. Three
        docstrings and a test comment claimed they had merely moved into More metrics; nothing put
        them there. Nutrient Cycling, Habitat Creation and Dissolved Pollutants all lost two cards
        apiece that way. How many cards a pane shows is registry data now: declare one KPI and one
        renders, which is what Microplastics and Temperature do.

        ALL OR NOTHING WHEN NOTHING RESOLVES. A card that cannot be computed is normally dropped,
        which keeps a partly-configured pane honest. But when NOT ONE of them resolves the block
        used to vanish whole, and that is the Pollutant section's opening state, because it ships
        no rate by design. Three costs, none of them obvious: the section never said what it
        computes, so a reader had no reason to go hunting for a rate; the card explaining that
        every number is flow weighted rather than a particle average rides the first RENDERED
        headline, so it was unreachable there; and the pane stopped looking like its sibling for
        a reason that is about defaults rather than about the science.

        So: nothing resolves and the section has hydraulics -> paint every declared card with its
        label, tooltip and unit, and "n/a" where the number goes. `unavailable_reason` follows
        immediately and says which input is missing. Nothing resolves and there are no paths ->
        None as before, since three empty cards over "run the calculations first" is just noise.

        Deliberately not a Pollutant branch: denitrification loses all three headlines when
        dissolved oxygen is cleared and thermal loses its only one when the response time is,
        and both collapsed the same silent way."""
        def _conc(v):
            """A concentration a reader can act on. Saturated removal drives the outlet to
            something like 7e-14 mg/L, and printing that reads as instrument noise rather than
            "effectively none left"."""
            if v is None:
                return None
            return "under 0.01" if 0 < v < 0.01 else report_mod.fmt_sig(v)

        # Stable sort on a bool, so the headline moves to the front and everything else keeps the
        # order the registry declared it in. A `lead` naming no declared KPI leaves the set alone.
        kpis = tuple(sorted(spec.kpis, key=lambda k: k.key != lead)) if lead else spec.kpis
        vals = [(k, _fn_val(s, k, with_unit=False)) for k in kpis]
        # "n/a" and not a dash: report_mod.fmt already settled on it for every missing value the
        # app prints, precisely so nothing user-facing renders an em dash.
        pending = bool(vals) and not any(v is not None for _, v in vals)
        if pending and not s.get("n_paths"):
            return None

        items = []
        for k, val in vals:
            if val is None and not pending:
                continue
            # The SECTION card rides the first headline's name line. It is what answers "is this
            # an average over paths?" (it is not, it is flow weighted), so it has to sit against
            # the numbers that question is asked about, not behind a disclosure.
            label, unit = _fn_label(s, k), _fn_unit(s, k)
            head = [label] if items else [label, _fn_tip(spec.help)]
            if val is None:
                items.append(ui.div(
                    ui.div(*head, class_="hype-kpi-name"),
                    ui.div(ui.span("n/a", class_="hype-kpi-num pending"),
                           ui.span(unit, class_="hype-kpi-unit") if unit else None,
                           _fn_tip(k.help) if k.help is not None else None,
                           class_="hype-kpi-val"),
                    class_="hype-kpi-item"))
                continue
            rng = None
            if k.low_key:
                scale = 100.0 if k.kind == "pct" else 1.0
                lo, hi = s.get(k.low_key), s.get(k.high_key)
                lo = None if lo is None else lo * scale
                hi = None if hi is None else hi * scale
                span = report_mod.fmt_range(lo, hi, k.digits)
                if span is not None:
                    suffix = "%" if k.kind == "pct" else ""
                    # Equal bounds print ONCE, never as "0.0213 to 0.0213", and say why: the
                    # sweep genuinely did not move the estimate, usually because removal has
                    # already run to completion on nearly every path. Without the trailing
                    # label a lone number under the headline just reads as a duplicate.
                    label = ("sensitivity range" if " to " in span
                             else "across the whole rate sweep")
                    rng = f"{span}{suffix} {label}"
            ctx = None
            if k.context_key and s.get(k.context_key) is not None:
                # `c_in` in the SAME unit as `v`. The display pair exists for endpoints reported
                # in µg/L; sections without one fall back to the mg/L value they already carry.
                ctx = k.context_fmt.format(
                    c_in=_conc(s.get("inlet_concentration_display",
                                     s.get("inlet_concentration_mg_l"))),
                    v=_conc(s.get(k.context_key)),
                    u=s.get("concentration_unit") or "mg/L")
            foot = " ".join(x for x in (rng, ctx) if x)
            items.append(ui.div(
                ui.div(*head, class_="hype-kpi-name"),
                ui.div(ui.span(val, class_="hype-kpi-num"),
                       ui.span(unit, class_="hype-kpi-unit") if unit else None,
                       _fn_tip(k.help) if k.help is not None else None,
                       class_="hype-kpi-val"),
                ui.div(foot, class_="hype-kpi-sub") if foot else None,
                class_="hype-kpi-item"))
        if not items:
            return None
        # THE LEAD IS ALREADY ON SCREEN, so draw only what supports it. Pollutant Attenuation puts
        # each endpoint behind its own disclosure and prints that endpoint's lead number in the
        # header, which is what makes a list of ticked chemicals scannable while collapsed. Drawing
        # the lead again inside would be the same figure twice, six inches apart.
        if drop_lead:
            rest = items[1:]
            return (ui.div(*[x.add_class("hype-kpi-small") for x in rest],
                           class_="hype-kpi-grid") if rest else None)
        # ONE card renders exactly as it always did. That is what keeps the single-KPI panes and
        # `_sig_kpi`'s three stacked cards byte-identical: `.hype-kpi` keeps the accent rule and
        # nothing about this block changes for them.
        if len(items) == 1:
            return ui.div(items[0], class_="hype-kpi")
        # Lead plus supporters. The rule moves onto the LEAD ITEM rather than staying on the
        # container, so it sits beside the number a reader came for instead of running down the
        # whole block including the smaller cards, which would weight all three the same.
        return ui.div(
            items[0].add_class("hype-kpi-lead"),
            ui.div(*[x.add_class("hype-kpi-small") for x in items[1:]], class_="hype-kpi-grid"),
            class_="hype-kpi hype-kpi-split")

    def _fn_curve(points, mark_h=None, w=300, h=34):
        """R(tau) as an inline SVG polyline: the rate-free view (functions plan §6).

        Built here rather than through figures.render_opportunity_curve because this pane
        re-renders on every keystroke in the fields below it, and matplotlib on the keystroke path
        would make the panel feel stuck (the report's renderer also serialises behind
        _REPORT_MPL_LOCK). Log x-axis; the dot marks the timescale this run actually assumed."""
        pts = [(float(p["tau_hours"]), float(p["opportunity"])) for p in (points or [])
               if p.get("tau_hours") and p.get("opportunity") is not None]
        if len(pts) < 3:
            return None
        lo, hi = math.log10(pts[0][0]), math.log10(pts[-1][0])
        if hi <= lo:
            return None

        def xy(tau, r):
            return ((math.log10(tau) - lo) / (hi - lo) * w,
                    h - max(0.0, min(1.0, r)) * h)

        d = " ".join(f"{x:.1f},{y:.1f}" for x, y in (xy(t, r) for t, r in pts))
        mark = ""
        if mark_h and lo <= math.log10(mark_h) <= hi:
            r_at = pts[0][1]
            for (t0, r0), (t1, r1) in zip(pts, pts[1:]):
                if t0 <= mark_h <= t1:                       # log-linear interpolation
                    span = math.log10(t1) - math.log10(t0)
                    fr = 0.0 if span <= 0 else (math.log10(mark_h) - math.log10(t0)) / span
                    r_at = r0 + fr * (r1 - r0)
                    break
            else:
                r_at = pts[-1][1]
            mx, my = xy(mark_h, r_at)
            mark = f'<circle class="spark-mark" cx="{mx:.1f}" cy="{my:.1f}" r="3"/>'
        end = pts[-1][0]
        end_lbl = f"{end / 24.0:g} d" if end >= 48.0 else f"{end:g} h"
        return ui.TagList(
            ui.HTML(f'<svg class="hype-props-spark" viewBox="0 0 {w} {h}" '
                    f'preserveAspectRatio="none" role="img" '
                    f'aria-label="Removal opportunity against the assumed reaction timescale">'
                    f'<polygon class="spark-fill" points="0,{h} {d} {w},{h}"/>'
                    f'<polyline class="spark-line" points="{d}"/>{mark}</svg>'),
            ui.div(ui.span(f"{pts[0][0]:g} h"), ui.span(end_lbl), class_="hype-spark-cap"))

    def _fn_toggle(process_key):
        """The one control at the top of every screening pane.

        Off means NOT SCREENED, not screened-and-hidden: `_fn_inputs` sends the flag through to
        `assess`, which skips the section, so the results model and the report agree that no
        estimate was made. That is the only reading of "include in report" a reader can check."""
        return ui.input_checkbox(f"fn_incl_{process_key}", "Include in report",
                                 value=_fn_included(process_key))

    def _fn_do_gate() -> bool:
        """Whether the denitrification pane applies its redox gate. Same live-input-then-`_keep`
        shape as `_fn_included`, and for the same reason: the pane re-renders itself, so the
        checkbox that decides whether three inputs below it exist has to answer before they do."""
        try:
            v = input["fn_do_gate"]()
        except Exception:      # noqa: BLE001 — never mounted this session
            v = _keep("fn_do_gate", True)
        return bool(True if v is None else v)

    def _fn_included(process_key) -> bool:
        """Whether a section is switched on. Live input first, `_keep` for an unmounted pane, on
        by default, mirroring `_fn_inputs` so the pane and the report never disagree."""
        try:
            v = input[f"fn_incl_{process_key}"]()
        except Exception:      # noqa: BLE001 — never mounted this session
            v = _keep(f"fn_incl_{process_key}", True)
        return bool(True if v is None else v)

    def _fn_assumption(s, spec, row):
        """The one line under the headline naming what the estimate rests on.

        Generated from the result and the spec, never a literal sentence: a literal here is how the
        prose walls come back, and it would go stale the moment an endpoint changed its units."""
        if row is None:
            return None
        val = _fn_val(s, row)
        if val is None:
            return None
        src = fn_reg.source_labels(spec.sources[:1]) if spec.sources else ""
        tail = f", from {src}" if src else ""
        return ui.div(f"Assumes {row.label.lower()} of {val}{tail}.", class_="hype-props-note")

    def _fn_limits(fspec, spec, sections):
        """The Considerations panel: what the estimate cannot tell you.

        Sources in order of how specific they are to this run: conditions the model cannot verify
        (computed per-run, and per ENDPOINT now, so the metals gate appears once however many
        metals are ticked), the guard and provenance notes, then the function's standing limits.
        Deliberately not a `hype-card warn`: warn means something is wrong, and these are
        prerequisites and absences a reader has to weigh for themselves.

        It lives behind the disclosure rather than under the headline. Everything here is standing
        context that does not change as the reader works, and leaving it on the card is what made
        the numbers hard to find in the first place."""
        parts, seen = [], set()
        for s in sections:
            conds = tuple(s.get("eligibility_conditions") or ())
            if conds and conds not in seen:
                seen.add(conds)
                parts.append(ui.div(
                    ui.div("Applies only where all of these hold", class_="hype-gate-head"),
                    ui.tags.ul(*[ui.tags.li(c) for c in conds]),
                    class_="hype-props-gate"))
            for note in ("calibration_note", "depth_note", "advisory_note", "preset_note"):
                text = s.get(note)
                if text and text not in seen:
                    seen.add(text)
                    parts.append(ui.div(text, class_="hype-props-note"))
        if fspec.limits:
            parts.append(ui.TagList(
                _fn_head("What this cannot tell you", spec.transferability_note),
                ui.tags.ul(*[ui.tags.li(b) for b in fspec.limits], class_="hype-props-limits")))
        return ui.accordion_panel("Limitations", *parts) if parts else None

    def _pane_fn(process_key):
        """One screening pane: the include toggle, the headline, its inputs, the disclosure.

        Five calculators share this factory, so no section hardcodes a label, citation or
        assumption: they all come from the registry. The returned closure takes the owning
        FunctionSpec, which narrows the headline to the one number the function leads with; called
        bare, every declared KPI paints as it always did.

        THE HEADLINE COMES FIRST, under the toggle and nothing else. Everything that used to sit
        above it -- the hydraulic signature table, the function's question, the group tables -- is
        either deleted or behind the disclosure, because a reader opening this pane is looking for
        the number and had to scroll past three blocks to reach it."""
        spec = fn_reg.get_process(process_key)

        def pane(fspec=None):
            headline = fspec.headline(fspec.mechanism_for_process(process_key)) if fspec else ""
            rests_on = fspec.rests_on(fspec.mechanism_for_process(process_key)) if fspec else None
            now = _screening_now() or {}
            s = now.get(process_key)
            if not s:
                return None
            # ONE RESULT PER TICKED ENDPOINT for the dissolved section, one for everything else.
            # The headline block below renders once per entry, so a reader comparing zinc against
            # acesulfame sees two headlines rather than a table they have to interpret.
            runs = (now.get("contaminant_endpoints") or []) if process_key == "contaminant" \
                else [("", s)]

            parts = [_fn_toggle(process_key)]
            # Scope, when the section's name reads wider than what it models. One muted line, from
            # the registry -- a literal string here is how the prose walls come back.
            if spec.scope_note:
                parts.append(ui.div(spec.scope_note, class_="hype-props-scope"))
            # OFF: the numbers stay visible, faint, and every control below them goes. Dimming a
            # live input would leave it keyboard-reachable and reading as broken; not drawing it
            # says plainly that nothing here is in play.
            if not _fn_included(process_key):
                return ui.TagList(
                    *parts,
                    ui.div(*[x for x in (_fn_kpi(r, spec, lead=headline) for _, r in runs)
                             if x is not None],
                           class_="hype-props-off"),
                    ui.div("Switched off. This section is left out of the screening report.",
                           class_="hype-props-note"))

            # THE PICKER LEADS, for the one section that screens several endpoints at once. It used
            # to sit under the results with every other input, and that was right when it was two
            # checkbox groups: ten checkboxes is 250 px, which put the first number below the fold.
            # A chip picker is 40 px, and it decides how many result blocks follow -- so it now
            # reads in the order it acts.
            multi = any(fn_pol.get_preset(k) is not None for k, _ in runs)
            if spec.key == "contaminant":
                parts.append(ui.div(
                    ui.div("Screening inputs", class_="hype-card-head"),
                    ui.input_selectize(
                        FN_POL_SELECT_ID, "Dissolved pollutants",
                        # A NESTED dict is what Shiny turns into <optgroup>s, so the dropdown still
                        # separates the two families the checklists used to head. That grouping was
                        # the one thing the checklists did better, and it survives.
                        choices={label: {k: fn_pol.PRESET_BY_KEY[k].label for k in keys}
                                 for _, label, keys in fn_pol.PRESET_GROUPS},
                        selected=[k for k, _ in runs], multiple=True, remove_button=True),
                    ui.div("Each pollutant is screened on its own. Results are not combined.",
                           class_="hype-props-note"),
                    class_="hype-input-card"))
                if not runs:
                    parts.append(ui.div("Add a pollutant above to screen it.",
                                        class_="hype-props-note"))

            # ONE PANEL PER ENDPOINT, its lead number IN THE HEADER. Collapsed, a list of ticked
            # chemicals reads as a scannable name-to-mass table; the alternative was 361 px of card
            # per endpoint, measured, which is 4000 px at ten of them.
            #
            # Worded to avoid the phrase the disclosure block below opens with: that comment is a
            # SOURCE ANCHOR two tests slice this function on, and a second copy of it silently
            # narrows their window to nothing while they keep passing.
            pol_panels = []
            for key, r in runs:
                pre = fn_pol.get_preset(key)
                block = []
                # The declared headline block: one lead card, the rest smaller beside it. Which one
                # leads is FunctionSpec data rather than a branch here, and how many there are is
                # ProcessSpec data, so a pane showing one number is a pane declaring one KPI. In a
                # per-endpoint panel the lead is dropped, because the panel header already carries
                # that number and printing it twice is what the header was meant to save.
                block += [x for x in (_fn_kpi(r, spec, lead=headline, drop_lead=multi),)
                          if x is not None]
                # What the number above rests on, right beneath it, generated from the spec.
                block += [x for x in (_fn_assumption(r, spec, rests_on),) if x is not None]
                # Whether the rate matters at all, on the card (screening reference rule 14). This
                # is the one diagnostic that changes how the number above should be READ -- at Da
                # over 100 the answer is the exchange flux restated and a better rate constant
                # would not move it -- so it rides the headline while its arithmetic sits in More
                # metrics.
                #
                # The chip comes off the spec where a section has its own: thermal's ratio is a
                # residence time over a HEAT response time, and the solute card's language about
                # rate constants would be describing a knob that section does not have.
                if r.get("damkohler_note"):
                    block.append(ui.div(r["damkohler_note"],
                                        _fn_tip(spec.regime_help
                                                or fn_reg.POLLUTANT_REGIME_HELP),
                                        class_="hype-props-note"))
                # Directly under the headline, because that is what it explains: which input is
                # missing and therefore why those cards read "n/a". The strings themselves name no
                # direction (see screen.py) so they read correctly here and in the report, where
                # the order differs again.
                if r.get("unavailable_reason"):
                    block.append(ui.div(r["unavailable_reason"], class_="hype-props-note"))
                # ON THE CARD, not behind the disclosure. Switching the oxygen gate off raises
                # every number above by roughly a factor of two, and the control that did it is
                # two accordions down; a reader who reopens this project next month needs the
                # reason sitting with the number it changed.
                if r.get("oxygen_gate_note"):
                    block.append(ui.div(r["oxygen_gate_note"], class_="hype-props-note"))
                # First-order has no ceiling, so say so when the entered concentration pushes the
                # fit past where its rate constant was measured. Only on the degraded path: a
                # standing caveat card would mean "warn" no longer reads as "something is wrong".
                if r.get("first_order_validity_note"):
                    block.append(ui.div(r["first_order_validity_note"], class_="hype-card warn"))
                # ONE FIELD PER ENDPOINT, inside its own panel. The id is minted from the preset key
                # at import (`FN_POL_CONC_IDS`), so `_KEEP_IDS` names every one of them and a value
                # survives removing and re-adding a chip.
                if pre is not None:
                    block.append(_fn_field(
                        ui.input_numeric(f"fn_pol_conc_{pre.key}",
                                         f"Stream concentration ({pre.concentration_unit})",
                                         value=_keep(f"fn_pol_conc_{pre.key}", pre.concentration),
                                         min=0.0, step=0.1, update_on="blur"),
                        spec.concentration_help))
                if not multi:
                    parts += block
                    continue
                # EVERYTHING THAT VARIES BY ENDPOINT, and nothing that does not. The rate-dependent
                # group and the rows the registry did not mark `shared` come in here; the hydraulics
                # are hoisted to More metrics below and shown once for the whole section.
                for g in spec.pane_groups:
                    if not g.assumed_rate:
                        continue
                    rws = [(k, v) for k, v in _fn_rows(r, g) if v is not None]
                    if rws:
                        block.append(_fn_tbl(g.title, rws, g.help, tag="assumed rate"))
                own = [(k, v) for k, v in _fn_rows(r, [x for x in spec.detail_rows if not x.shared])
                       if v is not None]
                if own:
                    block.append(ui.tags.table(
                        ui.tags.tbody(*[ui.tags.tr(ui.tags.td(k), ui.tags.td(v))
                                        for k, v in own]),
                        class_="hype-props-table"))
                pol_panels.append(ui.accordion_panel(_pol_head(pre, r, spec, headline), *block,
                                                     value=key))
            if pol_panels:
                # First one open: a pane that opens entirely collapsed reads as empty, and the
                # endpoint at the top is the one a reader is most likely to have come for.
                parts.append(ui.accordion(*pol_panels, id="fn_pol_acc", class_="hype-pol-acc",
                                          open=[runs[0][0]]))

            # Inputs. The only per-section branch left: Shiny input ids and their numeric bounds
            # must be literal because _KEEP_IDS and _fn_inputs name them. Every label, default,
            # tooltip and citation still comes off the spec.
            #
            # update_on="blur" on ALL of them: the results sit above the fields and recomputing
            # per keystroke made them flicker through half-typed values ("1", "1.", "1.5"). It
            # also stops the wholesale propspane re-render replacing the very box being typed in.
            # Fires on blur, Enter, and the spinner arrows.
            #
            # The dissolved section's own card is drawn ABOVE its results (see the picker), because
            # what it holds decides how many of them there are.
            fields = []
            if spec.key == "denitrification":
                # ONE FIELD. Stream nitrate is the only quantity a reader is expected to know
                # about their own site; dissolved oxygen, its consumption rate and the anoxic
                # threshold are all model parameters with defensible defaults, so they moved to
                # Advanced inputs under the switch that decides whether they apply at all.
                fields.append(_fn_field(ui.input_numeric(
                    "fn_no3", "Stream nitrate (mg/L as NO3-N)",
                    value=_keep("fn_no3", fn_reg.NITRATE_DEFAULT_MG_N_L),
                    min=0.0, max=50.0, step=0.5, update_on="blur"),
                    spec.concentration_help))
            # ONE BORDERED CARD, titled. What a reader has to supply is a different kind of thing
            # from what the model computed above it, and unboxed the two ran together as a single
            # column of controls hanging off the numbers. Habitat supplies nothing, so it gets no
            # card rather than an empty one.
            if fields:
                parts.append(ui.div(ui.div("Screening inputs", class_="hype-card-head"), *fields,
                                    class_="hype-input-card"))

            adv = []
            adv_title = "Advanced inputs"
            if spec.key == "denitrification":
                # THE GATE IS A CONTROL, not a wired-shut assumption. Off asks what the reach
                # could transform if oxygen never had to be consumed first, which is a defensible
                # screening posture and is labelled as an upper bound where the number is.
                #
                # Its three inputs are NOT DRAWN when it is off, rather than drawn disabled. Same
                # rule the switched-off section follows: a dimmed live input stays keyboard
                # reachable and reads as broken, while a control that appears and disappears
                # directly under its own switch reads as exactly what it is.
                adv = [_fn_field(ui.input_checkbox(
                           "fn_do_gate", "Limit denitrification by dissolved oxygen",
                           value=_fn_do_gate()),
                           fn_reg.OXYGEN_GATE_HELP)]
                # The maxima are TYPO GUARDS only: Shiny min/max are HTML attributes and the
                # server still receives whatever is typed. The sensitivity envelope brackets its
                # own headline by construction (screen.screen_reactive), which is the real
                # guarantee. 50 /day clears the rate card's published 0.6 to 36 /day range.
                if _fn_do_gate():
                    adv += [_fn_field(ui.input_numeric(
                                "fn_do", "Stream dissolved oxygen (mg/L)",
                                value=_keep("fn_do", fn_reg.DO_STREAM_DEFAULT_MG_L),
                                min=0.0, max=20.0, step=0.5, update_on="blur"),
                                fn_reg.OXYGEN_HELP),
                            _fn_field(ui.input_numeric(
                                "fn_o2_rate", "Oxygen consumption (mg/L/day)",
                                value=_keep("fn_o2_rate",
                                            fn_reg.OXYGEN_CONSUMPTION_MG_L_DAY[1]),
                                min=0.1, max=100.0, step=1.0, update_on="blur"),
                                fn_reg.OXYGEN_RATE_HELP),
                            _fn_field(ui.input_numeric(
                                "fn_do_thresh", "Denitrification stops above (mg/L)",
                                value=_keep("fn_do_thresh", fn_reg.DO_ANOXIC_THRESHOLD_MG_L),
                                min=0.0, max=2.0, step=0.05, update_on="blur"),
                                fn_reg.ANOXIC_THRESHOLD_HELP)]
                adv.append(_fn_field(ui.input_numeric(
                    "fn_denit_rate",
                    "Denitrification first-order rate constant (1/day)",
                    value=_keep("fn_denit_rate", spec.rate_central),
                    min=0.01, max=50.0, step=0.1, update_on="blur"),
                    spec.rate_help))
            elif spec.key == "thermal_regulation":
                # A SCENARIO, NOT A MEASUREMENT. Nobody measures a thermal response time at a
                # site: it is a literature parameter with exactly three published cases, and the
                # 4 and 16 h ones are the sensitivity corners the reported range already sweeps.
                # A free box invited a fourth number with nothing behind it and no range that
                # bracketed it. `screen_thermal` still accepts any float, which is where a
                # future site-calibrated value goes in (thermal plan §4.2).
                adv = [_fn_field(ui.input_radio_buttons(
                           "fn_tau", "Thermal response time",
                           choices={f"{h:g}": f"{lbl}, {h:g} h"
                                    for h, lbl in spec.rate_scenarios},
                           selected=_tau_choice(_keep("fn_tau", spec.rate_central), spec)),
                           spec.rate_help)]

            # One accordion, stacked closed panels, mirroring EASI's "Scoring method" /
            # "Scoring criteria" pair: secondary detail is reachable without occupying the pane.
            # Everything a reader does not need to work the headline lives in here.
            panels = []
            body = []
            # ONE PASS PER RUN normally; ONE PASS TOTAL when the section screens several endpoints,
            # because then this panel holds only what they share. Three ticked chemicals used to
            # print the same returning-path count, the same median residence time and the same
            # streambed area three times each -- eight identical values per endpoint, which is the
            # clutter the per-endpoint panels were meant to relieve and would instead have moved.
            # Which rows are shared is registry data (`PaneGroup.assumed_rate`, `PaneRow.shared`),
            # so nothing here decides what depends on a rate.
            for key, r in (runs[:1] if multi else runs):
                pre = fn_pol.get_preset(key)
                if pre is not None and not multi:
                    body.append(_fn_head(pre.label))
                # The group tables live under the disclosure. They are not deleted: every row the
                # registry declares still renders, it just no longer competes with the one number
                # the function leads with. This is the whole of the decluttering.
                for g in spec.pane_groups:
                    if multi and g.assumed_rate:
                        continue                     # drawn in that endpoint's own panel
                    rows = [(k, v) for k, v in _fn_rows(r, g) if v is not None]
                    if rows:
                        body.append(_fn_tbl(
                            g.title, rows, g.help,
                            tag=("assumed rate" if g.assumed_rate else "hydraulics")))
                want = [x for x in spec.detail_rows if x.shared] if multi else spec.detail_rows
                detail = [(k, v) for k, v in _fn_rows(r, want) if v is not None]
                if detail:
                    body.append(ui.tags.table(
                        ui.tags.tbody(*[ui.tags.tr(ui.tags.td(k), ui.tags.td(v))
                                        for k, v in detail]),
                        class_="hype-props-table"))
                # The R(tau) sparkline lives here rather than under a group table. It is the only
                # on-pane signal of how far the answer rests on an assumed rate, so it is worth
                # keeping -- but R is monotone in tau with the marker at 1/rate, so a run near
                # complete removal pins its whole left half at the ceiling and it reads as a blob
                # on the card. Behind the disclosure it gets a name and a tooltip, from the spec.
                if spec.detail_curve is not None:
                    mark = (24.0 / r["rate_value"]
                            if (spec.rate_unit == fn_reg.RATE_FIRST_ORDER_PER_DAY
                                and r.get("rate_value")) else None)
                    curve = _fn_curve(r.get(spec.detail_curve.key), mark)
                    if curve is not None:
                        body += [_fn_head(spec.detail_curve.label, spec.detail_curve.help), curve]
            if body:
                panels.append(ui.accordion_panel("More metrics", *body))
            # THE CITED RATES ARE READ ONLY. Every dissolved endpoint carries a rate traceable to
            # a paper, so the section shows what is in force rather than offering a box to
            # overwrite it with. The sections that DO have an assumable constant still edit it.
            if spec.key == "contaminant" and runs:
                adv = [_fn_tbl("Attenuation rates in force",
                               [(fn_pol.PRESET_BY_KEY[k].label, _fn_val(r, rests_on))
                                for k, r in runs if fn_pol.get_preset(k) is not None],
                               spec.rate_help, tag="assumed rate"),
                       # The panel is named "Advanced inputs" here like everywhere else, and this
                       # line is what stops that being a promise the section does not keep: every
                       # rate above is fixed by the paper it came from, so there is nothing to type
                       # in. Said once, in the panel, rather than left for a reader to infer from
                       # an absence.
                       ui.div("These rates are fixed by the citation for each endpoint and cannot "
                              "be edited. The stream concentration beside each result can.",
                              class_="hype-props-note")]
            # A SECTION THAT SUPPLIES NO INPUT still ran under settings, and those are what a
            # reader questioning the numbers reaches for. Habitat Creation has no rate and no
            # concentration, but its volume is the zone pass times porosity -- so porosity and the
            # particle density that resolved the zone ARE its inputs. They live on other panes
            # because changing either means re-running the model, which is exactly why they are
            # reported here rather than offered. No `spec.key` test: which sections work this way
            # is registry data, and the note saying where to change them comes with the rows.
            if spec.run_settings and runs:
                srows = [(k, v) for k, v in _fn_rows(runs[0][1], spec.run_settings)
                         if v is not None]
                if srows:
                    adv = [_fn_tbl("Run settings", srows, fn_reg.RUN_SETTINGS_HELP,
                                   tag="hydraulics"),
                           ui.div(spec.run_settings_note, class_="hype-props-note")]
            if adv:
                panels.append(ui.accordion_panel(
                    adv_title, *adv,
                    # Only when there is one. A section with no rate has no rate citation, and an
                    # empty note div is a blank line at the bottom of the panel.
                    *([ui.div(spec.rate_citation, class_="hype-props-note")]
                      if spec.rate_citation else [])))
            limits = _fn_limits(fspec, spec, [r for _, r in runs]) if fspec is not None else None
            if limits is not None:
                panels.append(limits)
            panels.append(_refs_panel(spec))
            parts.append(ui.accordion(*panels, open=False, id=f"fn_more_{spec.key}"))
            return ui.TagList(*parts)
        return pane

    def _pane_process(process_key):
        """One Screening tree node: the pane for exactly one calculator.

        A function is what a manager asks about; a process is what the app calculates, and the two
        stopped being one-to-one when microplastic retention arrived. Every calculator now has its
        own node, so this only has to find the owning function (for the headline choice and the
        limits) and hand off."""
        fspec = fn_reg.function_for_process(process_key)
        body = _pane_fn(process_key)

        def pane():
            if not (_screening_now() or {}).get(process_key):
                return ui.div("Run the Hyporheic Zone calculations first.", class_="hype-instr")
            note = _base_only_note("Function screening")
            return ui.TagList(note, body(fspec)) if note is not None else body(fspec)
        return pane

    def _pane_functions():
        """The Hyporheic Functions group node."""
        return ui.TagList(
            *([n] if (n := _base_only_note("Function screening")) is not None else []),
            ui.div("Screening estimates of what the modeled hydraulics may support.",
                   class_="hype-instr"),
            _next_hint("fn.scr.nut", "Go to Nutrient Cycling →"))

    def _pane_fn_group():
        """The Screening group node: a contents list, since every number lives on a child.

        The shared framing lives HERE, once, rather than being repeated on four panes: these are
        literature-parameterized interpretations of modeled hydraulics, reported as low-central-high
        ranges, and they are for comparison rather than site-specific prediction (spec §9.2)."""
        return ui.TagList(
            *([n] if (n := _base_only_note("Function screening")) is not None else []),
            ui.div("Opportunity under stated assumptions, never a measured outcome. Each function "
                   "reads the hyporheic hydraulic signature and applies published process rates, "
                   "reported as a range. Use them to compare sites and alternatives, not to "
                   "predict performance at one.", class_="hype-instr"),
            *[_next_hint(f.node_id, f"{f.display_label} →")
              for f in (fn_reg.get_function(k) for k in fn_reg.FUNCTION_ORDER)])

    def _pane_fn_mechanisms(fn_key):
        """A function node that hosts several calculators: a contents list, one chip per node."""
        fspec = fn_reg.get_function(fn_key)

        def pane():
            return ui.TagList(
                ui.div(fspec.help.definition, class_="hype-instr"),
                *[_next_hint(m.node_id, f"{m.label} →") for m in fspec.mechanisms])
        return pane

    #: What each report node is: (open-button id, which document, the title, the blurb).
    #: One table so a node cannot describe one document and open another.
    #:
    #: ONE SENTENCE EACH. These sit at the top of a pane whose only action is a single button, and
    #: a paragraph there was three lines of prose to skim past on the way to it. What the document
    #: contains at length belongs in the document.
    #:
    #: NO DOWNLOAD COLUMN. The panes used to carry their own PDF and HTML buttons beside the ones
    #: on the modal footer, which is what forced two id sets in the first place.
    REPORT_DOCS = {
        # FIRST, and the only one with no run behind it: it describes the framework rather than
        # this site, so it reads the same before and after an analysis.
        "report.concept": ("open_report_concept", "concept", "Conceptual Model",
                           "How the three hydraulic dimensions map to the four function "
                           "families. Not site-specific."),
        "report.hyd": ("open_report_hyd", "hydraulics", "Hydraulics Report",
                       "Turnover, residence time and extent, with site maps and the detailed "
                       "tables."),
        "report.fn": ("open_report_fn", "screening", "Functional Screening Report",
                      "Published reaction rates applied to your flow paths, one section per "
                      "screening switched on."),
        # Cross-Site Comparison is NOT a document here: it is the full-screen workspace
        # (comparison.js overlay), launched from its own tree node and a bespoke hub row.
    }

    def _report_status(nid) -> tuple[bool, str]:
        """(can this document be opened, what the hub row says about it).

        ONE ANSWER FOR BOTH SURFACES. The hub row and the document's own node ask the same
        question, and they used to answer it with separate conditions that could disagree."""
        doc = REPORT_DOCS[nid][1]
        if doc == "concept":
            # The one document with no prerequisite, which is the whole of what a reader needs
            # here. What it actually describes is the blurb's job, on its own pane.
            return True, "Always available."
        if hz_result() is None:
            return False, "Needs the hyporheic zone calculations."
        if doc == "screening":
            n = sum(1 for k in fn_reg.SECTION_ORDER if _fn_included(k))
            if not n:
                return False, "No screenings are switched on."
            # WHY THE ENVELOPE IS NOT ON OFFER goes here rather than in a fourth pane block: the
            # option only renders when it can be built, and an option that silently is not there
            # is the same problem as a ticked box that silently does nothing.
            _eok, _ewhy = _envelope_state()
            tail = "" if _eok else f" · {_ewhy}"
            return True, f"Ready · {n} included module{'' if n == 1 else 's'}{tail}"
        return True, "Ready"

    def _report_row(nid):
        """One hub row: the document, whether it can be opened, and the way in."""
        open_id, _doc, title, _blurb = REPORT_DOCS[nid]
        ok, status = _report_status(nid)
        return ui.div(
            ui.div(ui.div(title, class_="hype-rep-title"),
                   ui.div(status, class_="hype-rep-status"),
                   class_="hype-rep-text"),
            _evt_btn(open_id, "Open →", "btn-sm btn-outline-secondary", disabled=not ok),
            class_="hype-leg-row hype-rep-row")

    def _pane_report_group():
        """The Site Reports hub: every report product, its readiness, and one way into each.

        NOTHING IS PRODUCED FROM HERE BY ACCIDENT. Finishing the analysis never opens or writes a
        document, and every row is an explicit choice. A document that has gone stale rebuilds when
        its own Open is pressed, so there is no separate Generate step to remember."""
        return ui.TagList(
            *([n] if (n := _base_only_note("Every report")) is not None else []),
            ui.div("Choose an explicit report product. Completing the hydraulic analysis never "
                   "opens or generates a report automatically.", class_="hype-instr"),
            *([_report_busy()] if report_task.status() == "running" else []),
            ui.div(*[_report_row(nid) for nid in REPORT_DOCS],
                   _comparison_hub_row(), class_="hype-legend"),
            # ONLY HERE. The three document panes used to render this too, so the same five fields
            # appeared four times in one tree branch.
            _report_controls())

    def _pane_report_doc(nid):
        """One document's node: what it is, whether it can be opened, and the way in.

        THREE BLOCKS, NEVER MORE: the blurb, the status, and exactly one of a jump chip, a busy
        row or the Open button.

        NO DOWNLOAD ROW. The same PDF and HTML are on the modal footer, which is where a reader is
        standing when they decide they want the file, and a pane copy is the kind of duplication
        that has to be kept in sync forever. It also cost a second set of download ids: a download
        button is an OUTPUT, and this pane stays mounted behind the open modal, so the two
        surfaces could not share one."""
        def pane():
            open_id, doc, title, blurb = REPORT_DOCS[nid]
            ok, status = _report_status(nid)
            # The status renders either way. It used to appear only when something was blocking,
            # so a ready pane said nothing about what it was ready WITH -- and on the screening
            # report that line is the module count.
            parts = [p for p in (_base_only_note("This report"),) if p is not None]
            parts += [ui.div(blurb, class_="hype-instr"),
                      ui.div(status, class_="hype-props-note")]
            # THE ONE REPORT-LEVEL OPTION, on this document only. It belongs here rather than in
            # `_report_controls` (which the hub renders for all three) because it changes what
            # THIS document says, and rather than in the four screening panes because it is one
            # decision about the report, not four about the modules.
            #
            # Rendered only when a sweep can actually back it. `_envelope_on` re-checks the same
            # gate at build time, so a tick saved against a sweep that has since been wiped or
            # gone partial resolves to off instead of sitting checked and quietly ignored. The
            # reason it cannot be offered rides the status line above, not a fourth block.
            if doc == "screening" and ok:
                _eok, _ewhy = _envelope_state()
                if _eok:
                    parts.append(ui.div(
                        # THE LABEL NAMES WHAT THE REPORT CALLS IT. The document's rows read
                        # "Across hydraulic alternatives (process inputs held)", so an option
                        # offering an "envelope" sent the reader looking for a section that does
                        # not exist under that name. The input id keeps the old word: it is
                        # persisted in saved projects and carried by the versioned contract.
                        ui.input_checkbox("report_fn_envelope",
                                          "Include ranges across hydraulic alternatives",
                                          value=bool(_keep("report_fn_envelope", False))),
                        class_="hype-rep-opt"))
            if not ok:
                parts.append(_next_hint("fn.scr", "Go to Screening estimates →")
                             if doc == "screening" and hz_result() is not None
                             else _next_hint("gw", "Go to Groundwater Modeling →"))
            # The conceptual figure is a shipped asset, so it never waits on a build.
            elif doc != "concept" and report_task.status() == "running":
                parts.append(_report_busy())
            else:
                parts.append(ui.div(
                    _evt_btn(open_id, f"Open {title}", "btn-primary btn-sm"),
                    class_="hype-actions"))
            return ui.TagList(*parts)
        return pane

    def _report_busy():
        return ui.div(ui.div(class_="hype-spinner"),
                      ui.span("Building the report… figures, site maps, and PDF typically take "
                              "~10 s (longer if the USGS basemap service is slow)."),
                      class_="hype-busy")

    def _report_controls():
            # Shared by all three report panes. Only one is ever mounted, so the input ids do not
            # collide and `_keep` mirrors them across a pane switch as it does everywhere else.
            #
            # NO GENERATE BUTTON. A completed delineation builds both documents, and the Open
            # buttons rebuild whatever has gone stale since, so a separate build step was asking
            # the reader to track freshness the app can track itself.
            return ui.TagList(
                ui.accordion(
                    ui.accordion_panel(
                        "Site details (for the report)",
                        ui.input_text("site_name", "Site name",
                                      value=_keep("site_name", ""), placeholder="e.g. Mink Brook"),
                        ui.div(
                            ui.input_text("site_analyst", "Analyst",
                                          value=_keep("site_analyst", "")),
                            ui.input_text("site_org", "Organization",
                                          value=_keep("site_org", "")),
                            class_="hype-field-row"),
                        ui.input_text("site_date", "Assessment date (YYYY-MM-DD)",
                                      value=_keep("site_date", "")),
                        ui.input_text_area("site_notes", "Notes",
                                           value=_keep("site_notes", ""), rows=2),
                        ui.div("Outlet, endpoints and reach length are taken from the drawn "
                               "reach automatically.", class_="hype-instr")),
                    open=False, id="site_meta_acc"),
            )

    # ---- Cross-Site Comparison (desktop only) ------------------------------------------
    # The tree node is a LAUNCHER for the full-screen comparison workspace, not a manager:
    # collections are standalone .hypecompare files read/written by comparison.py, so the
    # pane only offers New / Open / recent collections. Cloud hides the node (no filesystem
    # access to other projects' folders) and this pane degrades to a note if reached anyway.
    _cmp_recents: dict = {"items": [], "tick": 0}   # launcher snapshot (onclick idx -> entry)
    cmp_recents_ver = reactive.value(0)

    def _comparison_hub_row():
        """The Site Reports hub row for the workspace (deliberately not a REPORT_DOCS entry:
        it opens an interactive overlay, not a built document)."""
        desktop = runmode.IS_DESKTOP
        status = ("Compare saved hydraulic results from 2 to 10 project files."
                  if desktop else "Available in HYPE Desktop.")
        return ui.div(
            ui.div(ui.div("Cross-Site Comparison", class_="hype-rep-title"),
                   ui.div(status, class_="hype-rep-status"), class_="hype-rep-text"),
            _evt_btn("comparison_new_evt", "Compare projects…",
                     "btn-sm btn-outline-secondary", disabled=not desktop),
            class_="hype-leg-row hype-rep-row")

    def _pane_report_cmp():
        """The Cross-Site Comparison launcher (the PANE_FOR_NODE override for report.cmp)."""
        if not runmode.IS_DESKTOP:
            return ui.TagList(ui.div(
                "Cross-site comparison is available in HYPE Desktop, where saved projects "
                "on this computer can be read side by side.", class_="hype-instr"))
        cmp_recents_ver()                       # re-render after open/save/forget
        items = recents.load_comparisons()[:8]
        _cmp_recents["items"] = items
        rows = [
            ui.div(
                ui.div(ui.div(it["name"], class_="hype-welcome-name"),
                       ui.div(str(Path(it["path"]).parent), class_="hype-welcome-dir"),
                       class_="hype-rep-text"),
                class_="hype-welcome-row",
                onclick=(f"Shiny.setInputValue('comparison_recent_open', "
                         f"{{i: {i}, n: Date.now()}}, {{priority: 'event'}})"))
            for i, it in enumerate(items)]
        return ui.TagList(
            ui.div("Compare this site's hydraulic signature with other saved HYPE projects "
                   "in a full-screen workspace. Comparisons save as their own files and "
                   "never modify the source projects.", class_="hype-instr"),
            ui.div(_evt_btn("comparison_new_evt", "New comparison", "btn-primary"),
                   _evt_btn("comparison_open_evt", "Open comparison…",
                            "btn-outline-secondary"),
                   class_="hype-actions"),
            *([ui.div("Recent comparisons", class_="hype-props-note"),
               ui.div(*rows, class_="hype-welcome-list")] if rows else []),
        )

    def _pane_chanmod():
            running = carve_task.status() == "running"
            active = carve_active()
            m = carve_meta() or {}
            parts = [
                ui.div("Carve a uniform trapezoidal channel into the terrain along the reach "
                       "centerline. The modified raster feeds every later step — wetted "
                       "extent, surface model, and groundwater run.", class_="hype-instr"),
                ui.input_numeric("carve_bw", "Bottom width (m)",
                                 value=_keep("carve_bw", 4.0), min=0.0, step=0.5),
                ui.input_numeric("carve_depth", "Carve depth below existing ground (m)",
                                 value=_keep("carve_depth", 1.5), min=0.1, step=0.25),
                ui.input_numeric("carve_slope", "Side slope (m horizontal per 1 m vertical)",
                                 value=_keep("carve_slope", 2.0), min=0.1, step=0.5),
            ]
            if running:
                parts.append(ui.div(ui.div(class_="hype-spinner"), ui.span("Carving…"),
                                    class_="hype-busy"))
            else:
                parts.append(ui.div(ui.input_action_button(
                    "carve_btn", "Re-carve channel" if active else "Carve channel",
                    class_="btn-primary"), class_="hype-actions"))
            if active:
                parts.append(ui.p(ui.span(class_="hype-st st-done"),
                                  f"Channel carved — max cut {m.get('max_cut_m', 0):.2f} m "
                                  f"over {m.get('cells_cut', 0):,} cells. The blue overlay "
                                  f"shows the cut depth.", class_="hype-chk ok"))
                parts.append(ui.div(ui.input_action_button(
                    "carve_revert", "Revert to original terrain",
                    class_="btn-sm btn-outline-secondary"), class_="hype-actions"))
            return ui.TagList(*parts)

    def _pane_wetted():
            with reactive.isolate():
                mode = wse_mode_v()
            if mode == "model":
                have = (ras_result() or {}).get("extent_feat") is not None
                if not have:
                    return ui.div("The wetted extent will come from the HEC-RAS surface "
                                  "model — run it under Water surface.", class_="hype-instr")
                filt = wetted_filter_res()
                note = None
                if filt and filt.get("failed"):
                    note = ui.div("Could not find an extent connected upstream to downstream "
                                  "— filter not applied; the full wetted extent feeds the "
                                  "groundwater model.", class_="hype-card warn")
                elif filt and filt.get("kept_feat") is not None:
                    n_rm = int(filt.get("n_removed") or 0)
                    if n_rm:
                        note = ui.div(f"{n_rm} isolated pool{'s' if n_rm != 1 else ''} "
                                      f"removed ({filt.get('removed_m2', 0):,.0f} m²) — "
                                      "excluded from the groundwater boundary condition.",
                                      class_="hype-instr")
                    else:
                        note = ui.div("No isolated pools — the wetted extent is fully "
                                      "connected.", class_="hype-instr")
                return ui.TagList(
                    ui.div("The modeled wetted extent from the HEC-RAS surface run. This "
                           "extent sets the groundwater model's stream boundary condition.",
                           class_="hype-instr"),
                    ui.input_checkbox("wetted_filter",
                                      "Remove isolated pools (keep only the extent connected "
                                      "upstream to downstream)",
                                      value=bool(_keep("wetted_filter", True))),
                    note,
                    ui.input_checkbox("show_removed_pools",
                                      "Show removed pools on the map",
                                      value=bool(_keep("show_removed_pools", False)))
                    if (filt and filt.get("removed_feat") is not None) else None,
                )
            present = wse_extent_feat() is not None
            with reactive.isolate():
                active = bnd_slot() == "wse"
            if active:
                hint = ("Editing the wetted-extent polygon on the map — drag vertices; Save/Done "
                        "in the map bar." if present else
                        "Draw the wetted-extent polygon on the map (click vertices, click the "
                        "first point to close).")
            elif present:
                hint = ("Wetted extent derived/drawn. Selecting this node starts an edit on "
                        "the map.")
            else:
                hint = "No wetted extent yet — it derives automatically from the DEM, or draw one."
            return ui.TagList(
                ui.div(hint, class_="hype-instr"),
                ui.div(ui.input_action_button("bnd_clear_side", "Clear & redraw",
                                              class_="btn-sm btn-outline-secondary"),
                       class_="hype-actions") if present else None,
            )

    def _pane_sw_raster(kind):
        def _pane():
            res = ras_result()
            ov = _ras_overlays.get(kind) if res else None
            if ov is None:
                if kind == "wse" and run_result() is not None:
                    return ui.div("Showing the water-surface raster the groundwater run "
                                  "consumed (tree checkbox toggles it).", class_="hype-instr")
                return ui.div("Appears after a HEC-RAS surface run.", class_="hype-instr")
            with reactive.isolate():
                op0 = float(ras_opacity_v())
            legend = None
            try:
                uri = results.colorbar_datauri(ov["vmin"], ov["vmax"], cmap=ov["cmap"],
                                               label=ov["label"])
                legend = ui.img(src=uri, style="max-width:100%;height:auto;")
            except Exception:  # noqa: BLE001
                pass
            return ui.TagList(
                ui.div("The tree checkbox shows/hides this raster.", class_="hype-instr"),
                ui.input_slider("ras_opacity", "Overlay opacity", min=0.0, max=1.0,
                                value=op0, step=0.05),
                legend,
            )
        return _pane

    def _pane_basemaps():
            return ui.div("Choose the basemap with the checkboxes — Imagery and Topo swap as a "
                          "pair. NHD Hydrography shows the flowlines the reach snaps to; "
                          "un-check it to clear the linework while modeling (picks still snap).",
                          class_="hype-instr")

    def _hz_swatch(cls):
        return ui.span(class_="hype-hz-swatch",
                       style=f"background:{HZ_COLORS[cls]};")

    def _sig_kpi(cards):
        """The three signature dimensions as KPI cards.

        Paints the SAME `hype-kpi` markup `_fn_kpi` does, so the hydraulic headline and a function
        headline read as siblings. It does not call `_fn_kpi`: that helper labels a low/high pair
        "sensitivity range" and pulls its context line from `inlet_concentration_mg_l`, and neither
        is true here. P10 to P90 is a distribution, not a parameter sweep, and there is no
        concentration on this pane. Reusing the CSS is what makes them siblings; reusing the
        function would import chemistry vocabulary into a hydraulics pane.

        Carries no string literals: every label, unit, sub-line and tooltip is already formatted by
        `signature.card_view`, so the pane and the report cannot word the same number differently."""
        items = []
        for c in cards:
            pending = "" if c["primary_value"] != "n/a" else " pending"
            items.append(ui.div(
                ui.div(c["primary_name"], class_="hype-kpi-name"),
                # The tip rides the VAL row, not the name: `.hype-kpi-val .hype-info-tip` is the
                # only kpi selector styles.css gives it (styles.css:408), and matching _fn_kpi's
                # placement is what makes the two blocks visually identical.
                ui.div(ui.span(c["primary_value"], class_=f"hype-kpi-num{pending}"),
                       ui.span(c["primary_unit"], class_="hype-kpi-unit"),
                       *([_info_tip(help=c["help"])] if c.get("help") else []),
                       class_="hype-kpi-val"),
                *([ui.div(c["sub"], class_="hype-kpi-sub")] if c.get("sub") else []),
                class_="hype-kpi-item"))
        return ui.div(*items, class_="hype-kpi")

    def _pane_hz():
            # Results-side pane: the hydraulic signature, then classed flow paths and volumes. The
            # run controls (particles per cell, displayed paths, Run button) live in the
            # Groundwater Modeling run hub; progress still lands here (_start_hz auto-selects this
            # node on launch).
            running = hz_task.status() == "running"
            res = hz_view()                # follows the displayed run (Basecase or alternative)
            parts = []
            if running:
                parts += [
                    ui.output_ui("hz_status"),
                    ui.tags.pre(ui.output_text("hz_log"), class_="hype-log"),
                    ui.div(ui.input_action_button("cancel_hz", "Cancel",
                                                  class_="btn-sm btn-outline-danger"),
                           class_="hype-actions"),
                ]
            elif res is None:
                parts.append(_next_hint("gw", "Run Hyporheic Zone calculations →",
                                        primary=True))
            else:
                # Counts first (what the map is showing), then what the run MEANS. The three
                # dimensions used to appear only in the generated report, so the pane a user
                # actually lands on after a run said nothing about frequency, duration or extent.
                parts += [ui.output_ui("hz_summary"), ui.output_ui("hz_signature")]
            return ui.TagList(*parts)

    def _pane_vols():
            res = hz_view()                # follows the displayed run
            if res is None:
                return ui.div("Run the Hyporheic Zone calculations (Groundwater Modeling pane) "
                              "to compute the class volumes.", class_="hype-instr")
            classes = (res.get("stats") or {}).get("classes", {})
            rows = [ui.div("Zone volumes by exchange class — select a class for its detail; the "
                           "tree checkbox shows its footprint (2D) and volume shell (3D).",
                           class_="hype-instr")]
            for cls in HZ_CLASSES:
                st = classes.get(cls, {})
                rows.append(ui.div(
                    _hz_swatch(cls), ui.span(HZ_LABEL[cls], class_="hype-hz-k"),
                    ui.span(f"{st.get('volume_m3', 0):,.0f} m³", class_="hype-hz-v"),
                    class_="hype-hz-row"))
            return ui.div(*rows)

    def _pane_hz_paths(cls):
        def _pane():
            res = hz_view()                # follows the displayed run
            if res is None:
                return ui.div("Run the Hyporheic Zone calculations (Groundwater Modeling pane) "
                              "to populate the flow classes.", class_="hype-instr")
            st = (res.get("stats") or {}).get("classes", {}).get(cls, {})
            counts = (res.get("stats") or {}).get("counts", {})
            rows = [
                ui.div(_hz_swatch(cls), ui.span(HZ_LABEL[cls], class_="hype-hz-title"),
                       class_="hype-hz-head"),
                _kv("Particles", f"{st.get('n_particles', 0):,} "
                    f"({st.get('pct_of_classified', 0)}% of classified)"),
                _kv("Displayed paths", f"{st.get('displayed_paths', 0):,}"),
            ]
            rt = st.get("residence_time_days")
            if rt:
                rows.append(_kv("Residence time",
                                f"mean {rt['mean']:g} d · median {rt['median']:g} d · "
                                f"max {rt['max']:g} d"))
            lm = st.get("length_m")
            if lm:
                rows.append(_kv("Path length", f"mean {lm['mean']:g} m · median {lm['median']:g} m"))
            # per-side origin/exit tallies for context
            osides = counts.get("origin_sides", {})
            esides = counts.get("exit_sides", {})
            rows.append(_fpsel_buttons())
            rows.append(ui.output_ui("hz_sel_props"))
            return ui.TagList(
                ui.div("Click a path on the map for its properties, or drag a box to select "
                       "several; the tree checkbox toggles the class.", class_="hype-instr"),
                *rows,
            )
        return _pane

    def _pane_hz_vol(cls):
        def _pane():
            res = hz_view()                # follows the displayed run
            if res is None:
                return ui.div("Run the Hyporheic Zone calculations to compute this volume.",
                              class_="hype-instr")
            st = (res.get("stats") or {}).get("classes", {}).get(cls, {})
            bbox = st.get("bbox_m", [0, 0, 0])
            return ui.TagList(
                ui.div(_hz_swatch(cls), ui.span(f"{HZ_LABEL[cls]} zone", class_="hype-hz-title"),
                       class_="hype-hz-head"),
                _kv("Volume", f"{st.get('volume_m3', 0):,.0f} m³ "
                    f"({st.get('pct_domain_volume', 0)}% of domain)"),
                _kv("Footprint", f"{st.get('footprint_m2', 0):,.0f} m²"),
                _kv("Bounding box", f"{bbox[0]:g} × {bbox[1]:g} × {bbox[2]:g} m (L×W×H)"),
                _kv("Thickness", f"mean {st.get('thickness_mean_m', 0):g} m · "
                    f"max {st.get('thickness_max_m', 0):g} m"),
                _kv("Cells", f"{st.get('n_cells', 0):,}"),
                ui.div("Toggle the tree checkbox to show/hide this volume in the 3D view "
                       "(footprint on the 2D map).", class_="hype-instr"),
            )
        return _pane

    def _kv(label, value):
        return ui.div(ui.span(label, class_="hype-hz-k"), ui.span(str(value), class_="hype-hz-v"),
                      class_="hype-hz-row")

    def _info_tip(text=None, *, help=None):
        """Small hover/focus info icon, rendered by www/info_tip.js.

        Two channels, as EASI has: a plain string for a one-liner, or a `Help` object for a
        structured card (built by `helptext.render_card`, which lives beside the model so it is
        testable without Shiny). `data-tip`/`data-tip-html` rather than `title`, because the
        native tooltip is unstyleable OS chrome on a roughly one-second delay and carrying both
        attributes would show two tooltips. `tabindex` makes the text reachable without a
        pointer; `aria_label` is the flat text, since a screen reader cannot use the layout."""
        if help is not None:
            return ui.tags.span(class_="hype-info-tip",
                                data_tip_html=fn_reg.render_card(help), tabindex="0",
                                aria_label=fn_reg.flat_text(help))
        return ui.tags.span(class_="hype-info-tip", data_tip=text, tabindex="0",
                            aria_label=text)

    def _refs_panel(spec):
        """The pane's Sources disclosure: a real reference list, one entry per line.

        Full citations live HERE and not in a tooltip, because the tip is pointer-events:none
        so a DOI in one could be neither selected nor followed. Collapsed by default: it is the
        longest block on the pane and rarely the first thing a reader wants."""
        body = ([ui.div(fn_reg.SOURCES[k].reference(), class_="hype-ref")
                 for k in spec.sources]
                or [ui.div(spec.citation, class_="hype-ref")])
        return ui.accordion_panel("Sources", *body)

    def _pane_flows():
        """Flux-weighted exchange-flow accounting (§8.3 four-way) + the exchange-map key."""
        res = hz_view()                    # follows the displayed run
        if res is None:
            return ui.div("Run the Hyporheic Zone calculations to compute the flow accounting.",
                          class_="hype-instr")
        stats = res.get("stats") or {}
        flux = stats.get("flux")
        if not flux:
            return ui.div("No boundary inflow was found in the model budget, so there is "
                          "no flow to account for.", class_="hype-instr")
        acct = flux.get("accounting") or {}

        def m3d(v):
            f = float(v or 0.0)
            return f"{f:,.0f} m³/d" if abs(f) >= 100 else f"{f:,.2f} m³/d"

        if "gaining" not in acct:      # artifact from before the four-way extension
            return ui.TagList(
                ui.div("This delineation predates the four-flow accounting. Re-run it from "
                       "the Groundwater Modeling pane to compute gaining and throughflow "
                       "and the exchange map.", class_="hype-card warn"),
                ui.div("Stream-exchange split from the saved run", class_="hype-instr"),
                _kv("Stream downwelling", m3d(acct.get("total_downwelling"))),
                _kv("Hyporheic (returning)", m3d(acct.get("returning"))),
                _kv("Losing", m3d(acct.get("losing"))),
                _kv("Unresolved", m3d(acct.get("unresolved"))),
            )

        down = float(acct.get("total_downwelling") or 0.0)
        upw = float(acct.get("total_upwelling") or 0.0)
        net = float(acct.get("net_stream_exchange") or 0.0)
        rows = [ui.div("Flow accounting", _info_tip(
            "Flux-weighted particle accounting: every parcel of boundary inflow in the "
            "model budget is tracked along the delineation trajectories to where it "
            "leaves the model, and its flux is credited to that exit. Values are m³/d."),
            class_="hype-props-title")]
        for cls, key in (("hyporheic", "returning"), ("gaining", "gaining"),
                         ("losing", "losing"), ("throughflow", "throughflow")):
            rows.append(ui.div(
                _hz_swatch(cls), ui.span(HZ_LABEL[cls], class_="hype-hz-k"),
                ui.span(m3d(acct.get(key)), class_="hype-hz-v"),
                class_="hype-hz-row"))

        def _tbl(title, trs, tip=None):
            head = [title, _info_tip(tip)] if tip else [title]
            return ui.TagList(
                ui.div(*head, class_="hype-props-title"),
                ui.tags.table(ui.tags.tbody(*trs), class_="hype-props-table"))

        def _tr(k, v):
            return ui.tags.tr(ui.tags.td(k), ui.tags.td(v))

        reach_tag = ("net gaining reach" if net > 0
                     else "net losing reach" if net < 0 else "balanced")
        rows.append(_tbl("Stream exchange", [
            _tr("Downwelling (stream → subsurface)", m3d(down)),
            _tr("Upwelling (subsurface → stream)", m3d(upw)),
            _tr("Net exchange", f"{m3d(net)} · {reach_tag}"),
            _tr("Unresolved (stream-origin)", m3d(acct.get("unresolved"))),
        ], tip=("Flow across the streambed. Downwelling is stream water entering the "
                "subsurface; upwelling is subsurface water returning to the stream. Net "
                "exchange is upwelling minus downwelling. Unresolved parcels were still "
                "traveling at the tracking time cap.")))
        rows.append(_tbl("Boundary underflow", [
            _tr("Inflow (sides)", m3d(acct.get("total_side_inflow"))),
            _tr("Outflow (sides)", m3d(acct.get("total_side_outflow"))),
            _tr("Unresolved (side-origin)", m3d(acct.get("side_unresolved"))),
        ], tip=("Groundwater that enters or leaves through the model's side boundaries "
                "without exchanging with the stream.")))

        ret = float(acct.get("returning") or 0.0)
        norm = []
        area = acct.get("streambed_area_m2")
        if area and ret:
            norm.append(_kv("Hyporheic flux", f"{ret / float(area):.4g} m/d over "
                            f"{float(area):,.0f} m² of streambed"))
        rl = _reach_length_m()
        if rl and ret:
            norm.append(_kv("Exchange per channel length", f"{ret / rl:.4g} m³/d per m"))
        porosity = float((stats.get("knobs") or {}).get("porosity") or 0.0)
        hyp_vol = float((stats.get("classes") or {}).get("hyporheic", {})
                        .get("volume_m3") or 0.0)
        if porosity > 0 and hyp_vol > 0 and ret > 0:
            norm.append(_kv("Hyporheic turnover", f"{porosity * hyp_vol / ret:,.1f} days "
                            "(pore volume ÷ hyporheic flow)"))
        if norm:
            rows.append(ui.div("Normalized", _info_tip(
                "Stream exchange scaled for comparison between sites: hyporheic flux per "
                "unit streambed area, exchange per meter of channel, and turnover time "
                "(hyporheic pore volume divided by hyporheic flow)."),
                class_="hype-props-title"))
            rows += norm

        rtd_c = flux.get("rtd_by_class") or {}
        tt = []
        for key, cls in (("returning", "hyporheic"), ("gaining", "gaining"),
                         ("losing", "losing"), ("throughflow", "throughflow")):
            r = rtd_c.get(key)
            if r:
                tt.append(_kv(HZ_LABEL[cls], f"mean {r['weighted_mean_days']:g} d · "
                              f"median {r['weighted_median_days']:g} d"))
        if tt:
            rows.append(ui.div("Flux-weighted residence time", _info_tip(
                "Time water spends in the subsurface, weighted by each parcel's flux so "
                "larger flows count more. Mean and median per flow class."),
                class_="hype-props-title"))
            rows += tt

        return ui.TagList(*rows)

    def _pane_flow_cells(direction):
        # Leaf panes for the streambed exchange-cell overlays, split out of the Flows node
        # so the red (downwelling) and blue (upwelling) cells toggle independently.
        down = direction == "down"
        cls = "losing" if down else "gaining"
        text = ("Red cells mark downwelling: streambed cells where stream water enters "
                "the subsurface." if down else
                "Blue cells mark upwelling: streambed cells where subsurface water "
                "returns to the stream.")

        def pane():
            return ui.TagList(
                ui.div(_hz_swatch(cls), " ", text, class_="hype-instr"),
                ui.div("This layer's checkbox shows or hides the cells.",
                       class_="hype-instr"))
        return pane

    def _pane_project():
        """Project identity pane: name, save location, locked units, created date, and the
        save actions (the same handlers the header links use)."""
        meta = project_meta_v() or {}
        if runmode.IS_DESKTOP and _ws["project_file"]:
            loc: object = ui.tags.code(str(Path(_ws["project_file"]).parent),
                                       style="word-break: break-all;")
        else:
            loc = "This browser session. Use Save to download a .hype project file."
        units_label = project_meta.UNIT_LABELS.get(
            meta.get("units"), project_meta.UNIT_LABELS[project_meta.UNITS_METRIC])
        if runmode.IS_DESKTOP:
            actions = (_evt_btn("proj_save", "Save", "btn-primary btn-sm"),
                       _evt_btn("proj_save_as", "Save As…", "btn-outline-secondary btn-sm"))
        else:
            actions = (_evt_btn("proj_save", "Save project…", "btn-primary btn-sm"),)
        gms_row = ()
        if runmode.IS_DESKTOP and _ws["project_file"]:
            st = gms_status_v()            # subscribes: the row refreshes when a build lands
            gdir = work_dir / "GMS"
            if (gdir / "EXPORT_ERROR.txt").exists() or (st and st.get("ok") is False):
                gms_line = "Last export failed. See GMS/EXPORT_ERROR.txt in the project folder."
            elif gdir.is_dir():
                gms_line = ("GMS folder, refreshed after each groundwater run. Exports "
                            "reflect the Basecase run.")
            else:
                gms_line = None
            if gms_line:
                gms_row = (ui.div(ui.div("Aquaveo GMS", class_="hype-proj-k"),
                                  ui.div(gms_line, class_="hype-proj-v"),
                                  class_="hype-proj-row"),)
        return ui.TagList(
            ui.div(ui.div("Name", class_="hype-proj-k"),
                   ui.div(meta.get("name") or "Untitled", class_="hype-proj-v"),
                   class_="hype-proj-row"),
            ui.div(ui.div("Location", class_="hype-proj-k"),
                   ui.div(loc, class_="hype-proj-v"), class_="hype-proj-row"),
            ui.div(ui.div("Units", class_="hype-proj-k"),
                   ui.div(units_label, ui.span(" Locked in this version.",
                                               class_="hype-dim"),
                          class_="hype-proj-v"),
                   class_="hype-proj-row"),
            ui.div(ui.div("Created", class_="hype-proj-k"),
                   ui.div(project_meta.created_display(meta.get("created")),
                          class_="hype-proj-v"),
                   class_="hype-proj-row"),
            *gms_row,
            ui.div(*actions, class_="hype-actions"))

    @reactive.effect
    @reactive.event(input.proj_save)
    def _proj_save():
        if runmode.IS_DESKTOP:
            _do_desktop_save()
        else:
            _show_bundle_dialog()

    @reactive.effect
    @reactive.event(input.proj_save_as)
    async def _proj_save_as():
        await _begin_save_as()

    def _next_hint(nid, label, primary=False):
        """A guidance chip advancing the selection — plain button, delegated via tree.js
        (data-jump), so it carries none of the dynamic-input remount hazards. `primary`
        renders it filled (the one obvious next action, e.g. run-complete → Delineate)."""
        return ui.div(ui.tags.button(label, type="button",
                                     class_="hype-jump primary" if primary else "hype-jump",
                                     **{"data-jump": nid}), class_="hype-actions")

    # node id -> pane builder (the dispatch table for the right properties panel)
    PANE_FOR_NODE = {
        "project": _pane_project,
        "reach": _pane_reach,
        "terrain": _pane_dem, "terrain.dem": _pane_dem, "terrain.chanmod": _pane_chanmod,
        "bnd": _pane_boundaries,
        "bnd.up": _pane_bnd_side("up", "Upstream", UP_STYLE["color"]),
        "bnd.left": _pane_bnd_side("left", "Left floodplain", LEFT_STYLE["color"]),
        "bnd.right": _pane_bnd_side("right", "Right floodplain", RIGHT_STYLE["color"]),
        "bnd.down": _pane_bnd_side("down", "Downstream", DOWN_STYLE["color"]),
        "sw": _pane_sw, "sw.mesh": _pane_sw, "sw.wetted": _pane_wetted,
        "sw.wse": _pane_sw_raster("wse"), "sw.depth": _pane_sw_raster("depth"),
        "gw": _pane_gw, "gw.k": _pane_k,
        "gw.mesh": _pane_mesh, "gw.run": _pane_run, "gw.alt": _pane_alt,
        "gw.wells": _pane_wells,
        "gw.res": _pane_results, "gw.res.head": _pane_head, "gw.res.paths": _pane_paths,
        "gw.res.hz": _pane_hz, "gw.res.hz.vols": _pane_vols,
        "gw.res.paths.hyp": _pane_hz_paths("hyporheic"),
        "gw.res.paths.los": _pane_hz_paths("losing"),
        "gw.res.paths.gain": _pane_hz_paths("gaining"),
        "gw.res.paths.thru": _pane_hz_paths("throughflow"),
        "gw.res.hz.hyp": _pane_hz_vol("hyporheic"),
        "gw.res.hz.los": _pane_hz_vol("losing"),
        "gw.res.hz.gain": _pane_hz_vol("gaining"),
        "gw.res.hz.thru": _pane_hz_vol("throughflow"),
        "gw.res.hz.flows": _pane_flows,
        "gw.res.hz.flows.down": _pane_flow_cells("down"),
        "gw.res.hz.flows.up": _pane_flow_cells("up"),
        "fn": _pane_functions, "fn.scr": _pane_fn_group,
        # One pane per CALCULATOR, plus a contents pane for any function that hosts more than one.
        **{nid: _pane_process(pk) for nid, pk in FN_NODE_PROCESS.items()},
        **{nid: _pane_fn_mechanisms(fn_reg.FUNCTIONS[k].key)
           for k in fn_reg.FUNCTION_ORDER
           for nid in (fn_reg.FUNCTIONS[k].node_id,) if nid in FN_GROUP_NODES},
        "report": _pane_report_group,
        **{nid: _pane_report_doc(nid) for nid in REPORT_DOCS},
        # OVERRIDES the expansion above (later duplicate keys win in a dict literal): the
        # comparison node is a MANAGER pane, not a blurb-status-button document pane.
        "report.cmp": _pane_report_cmp,
        "maplyr": _pane_maplyr,
        "base": _pane_basemaps, "base.imagery": _pane_basemaps, "base.topo": _pane_basemaps,
        "base.hydro": _pane_basemaps,
    }

    # Soft gating: node -> (ok_fn, hint, jump-target node, jump label). Unmet → the pane shows
    # the hint + a jump chip instead of controls; the tree row itself stays clickable.
    PREREQS = {
        "terrain": (lambda: reach_feat() is not None,
                    "Define the reach centerline first.", "reach", "Go to Reach centerline →"),
        "terrain.dem": (lambda: reach_feat() is not None,
                        "Define the reach centerline first.", "reach", "Go to Reach centerline →"),
        "terrain.chanmod": (lambda: reach_feat() is not None and dem_path() is not None,
                            "Define the reach and fetch terrain first — the channel is carved "
                            "into the DEM along the centerline.", "terrain.dem", "Go to DEM →"),
        "bnd": (lambda: dem_path() is not None,
                "Fetch the terrain first — boundaries are sized from it.", "terrain.dem",
                "Go to DEM →"),
        "sw": (lambda: _domain_build() is not None,
               "Generate the four boundaries first (they close into the model domain).",
               "bnd", "Go to Boundaries →"),
        "sw.mesh": (lambda: _domain_build() is not None,
                    "Generate the four boundaries first.", "bnd", "Go to Boundaries →"),
        "sw.wetted": (lambda: dem_path() is not None,
                      "Fetch the terrain first.", "terrain.dem", "Go to DEM →"),
        "gw": (lambda: _domain_build() is not None,
               "Generate the four boundaries first.", "bnd", "Go to Boundaries →"),
        "gw.k": (lambda: _domain_build() is not None,
                 "Generate the four boundaries first.", "bnd", "Go to Boundaries →"),
        "gw.mesh": (lambda: _domain_build() is not None,
                    "Generate the four boundaries first.", "bnd", "Go to Boundaries →"),
        "gw.wells": (lambda: _domain_build() is not None,
                     "Generate the four boundaries first.", "bnd", "Go to Boundaries →"),
        "gw.res": (lambda: run_result() is not None,
                   "Run the groundwater model first.", "gw", "Go to Groundwater Modeling →"),
        "gw.res.head": (lambda: run_result() is not None,
                        "Run the groundwater model first.", "gw",
                        "Go to Groundwater Modeling →"),
        "gw.res.hz": (lambda: run_result() is not None,
                      "Run the groundwater model first; the analysis reuses its solution.",
                      "gw", "Go to Groundwater Modeling →"),
        "gw.alt": (lambda: hz_result() is not None,
                   "Run the groundwater model and delineate the Hyporheic Zone first. "
                   "Alternatives reuse that completed run as the Basecase.",
                   "gw", "Go to Groundwater Modeling →"),
    }
    # THE GROUP NODE HAS NO PREREQUISITE, deliberately. It hosts the Conceptual Model, which is a
    # shipped figure and needs no run, so walling the hub off behind a delineation would make the
    # one always-available document unreachable until the analysis finished. The hub says per row
    # what each document still needs. The other two keep the wall.
    _report_prereq = (lambda: hz_result() is not None,
                      "Run the groundwater model, then delineate the hyporheic zone. This report "
                      "summarizes that analysis.", "gw", "Go to Groundwater Modeling →")
    for _rid in ("report.hyd", "report.fn"):
        PREREQS[_rid] = _report_prereq
    for _fnid in ("fn", "fn.scr", *FN_NODE_PROCESS, *FN_GROUP_NODES):
        PREREQS[_fnid] = (lambda: hz_result() is not None,
                          "Run the Hyporheic Zone calculations first. Function screening reads the "
                          "flux-weighted residence times that analysis produces.", "gw",
                          "Go to Groundwater Modeling →")
    for _bd in ("bnd.up", "bnd.left", "bnd.right", "bnd.down"):
        PREREQS[_bd] = PREREQS["bnd"]
    # the Flow-paths / Volumes groups and their class rows need the analysis to have run
    for _hzc in ("gw.res.paths", "gw.res.paths.hyp", "gw.res.paths.los", "gw.res.paths.gain",
                 "gw.res.paths.thru", "gw.res.hz.vols", "gw.res.hz.hyp", "gw.res.hz.los",
                 "gw.res.hz.gain", "gw.res.hz.thru", "gw.res.hz.flows",
                 "gw.res.hz.flows.down", "gw.res.hz.flows.up"):
        PREREQS[_hzc] = (lambda: hz_result() is not None,
                         "Run the Hyporheic Zone calculations first.", "gw",
                         "Go to Groundwater Modeling →")

    _ZOOM_ICON = ui.HTML(
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2.4" stroke-linecap="round" aria-hidden="true">'
        '<path d="M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9'
        'M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15"/>'
        '<circle cx="12" cy="12" r="3"/></svg>')

    def _props_shell(title, *body, clear_btn=False, chrome=True, wide=False):
        head = [ui.span(class_="hype-panel-caret"),
                ui.span(title, class_="hype-panel-title")]
        if chrome:
            if clear_btn:  # top-right "Clear results" for Boundaries / Surface Water / Groundwater
                head.append(ui.tags.button(
                    "Clear results", type="button", class_="hype-props-clear",
                    title="Clear this step's results and everything downstream"))
            head.append(ui.tags.button(_ZOOM_ICON, type="button", class_="hype-props-zoom",
                                       title="Zoom to extent"))
            head.append(ui.tags.button("×", type="button", class_="hype-props-close",
                                       title="Close (deselect)"))
        parts = [ui.div(*head, class_="hype-props-head"),
                 ui.div(*body, class_="hype-props-body")]
        if wide:
            # Scoped width override for content that needs the room (the alternatives runs
            # table). Lives INSIDE the pane content (map_edit_style precedent), so it vanishes
            # the moment the selection leaves the node. min() keeps it clear of the tree panel
            # on small windows.
            parts.append(ui.tags.style(
                "#hype-props-panel{width:min(520px, calc(100vw - 356px));}"))
        return ui.TagList(*parts)

    def _pane_welcome():
        # First-run "home" card — shown while nothing is selected and no work exists yet.
        return _props_shell(
            "Get started",
            ui.div("Build a hyporheic exchange model in seven steps:",
                   class_="hype-welcome-note"),
            ui.tags.ol(
                # One item per header stage, same numbers and labels (ui_tree.STAGES) — the
                # card is a legend for the bar, not a second numbering scheme.
                ui.tags.li(ui.span("1", class_="hype-welcome-num"),
                           ui.span(ui.tags.b("Reach"), ": pick two points on a stream, "
                                   "or draw the centerline.")),
                ui.tags.li(ui.span("2", class_="hype-welcome-num"),
                           ui.span(ui.tags.b("Terrain"), ": elevation data downloads "
                                   "automatically.")),
                ui.tags.li(ui.span("3", class_="hype-welcome-num"),
                           ui.span(ui.tags.b("Boundaries"), ": generated automatically. "
                                   "Edit the lines if needed.")),
                ui.tags.li(ui.span("4", class_="hype-welcome-num"),
                           ui.span(ui.tags.b("Surface Water"), ": set the streamflow and "
                                   "run the surface model.")),
                ui.tags.li(ui.span("5", class_="hype-welcome-num"),
                           ui.span(ui.tags.b("Groundwater"), ": run the groundwater model, "
                                   "then delineate the hyporheic zone.")),
                ui.tags.li(ui.span("6", class_="hype-welcome-num"),
                           ui.span(ui.tags.b("Results"), ": view flow paths, volumes, and "
                                   "exchange flows.")),
                ui.tags.li(ui.span("7", class_="hype-welcome-num"),
                           ui.span(ui.tags.b("Report"), ": generate the site summary "
                                   "report.")),
                class_="hype-welcome-steps"),
            ui.div("Use New (top right) to create a project folder — work saves there as "
                   "you go." if runmode.IS_DESKTOP else
                   "Work isn't saved on the server. Use Save (top right) before you leave.",
                   class_="hype-welcome-note"),
            _next_hint("reach", "Start with step 1: Reach →"),
            chrome=False)

    @reactive.effect
    def _bnd_clear_side():
        # "Clear & redraw" on a boundary-side / wetted-extent pane: empty the selected node's
        # slot (the slot sync then arms a fresh draw via _push_reach_state).
        if not _clicked_dynamic("bnd_clear_side"):
            return
        with reactive.isolate():
            slot = ui_tree.NODE_SLOT.get(sel_node())
        sv = _slot_value(slot) if slot else None
        if sv is not None:
            sv.set(None)
            dc = _draw_ctl.get("dc")
            if dc is not None:
                try:
                    dc.clear(); dc.data = []
                except Exception:  # noqa: BLE001
                    pass

    @render.ui
    def nhd_status_ui():
        s = nhd_status()
        return ui.div(s, class_="hype-instr") if s else None

    @render.ui
    def auto_readout():
        n = len(pick_pts()); m = auto_meta()
        st1 = "st-done" if n >= 1 else "st-active"
        st2 = "st-done" if n >= 2 else ("st-active" if n == 1 else "st-todo")
        rows = [ui.div(ui.span(class_=f"hype-st {st1}"), "Upstream point",
                       class_="hype-chk ok" if n >= 1 else "hype-chk"),
                ui.div(ui.span(class_=f"hype-st {st2}"), "Downstream point",
                       class_="hype-chk ok" if n >= 2 else "hype-chk")]
        if m:
            rows.append(ui.div(
                f"Reach {m['length_m'] / 1609.344:.2f} mi · drainage area {m['da_sqkm']:.1f} km²",
                class_="hype-estimate green"))
        return ui.div(*rows)

    @render.ui
    def manual_reach_status():
        drawn = delineate_mode() == "manual" and reach_feat() is not None
        da_ok = _manual_da_valid()
        return ui.div(
            ui.div(ui.span(class_="hype-st " + ("st-done" if drawn else "st-todo")),
                   "Reach centerline drawn" if drawn else "Draw the reach centerline",
                   class_="hype-chk ok" if drawn else "hype-chk"),
            ui.div(ui.span(class_="hype-st " + ("st-done" if da_ok else "st-todo")),
                   "Drainage area entered" if da_ok
                   else "Waiting for the drainage area (from NHD, or enter it)",
                   class_="hype-chk ok" if da_ok else "hype-chk"))

    @render.ui
    def reach_edit_ctl():
        # Edit the centerline no matter how it was created (auto pick or manual draw): its own output
        # so toggling never remounts the reach pane's text inputs (the documented remount footgun).
        if reach_feat() is None:
            return None
        editing = reach_edit()
        hint = ("Editing on the map — drag the vertices; Done here or in the map bar."
                if editing else
                "Click the centerline on the map to edit its vertices, or use Edit centerline.")
        return ui.TagList(
            ui.div(hint, class_="hype-instr"),
            ui.div(ui.input_action_button(
                "reach_edit_toggle", "Done editing" if editing else "Edit centerline",
                class_="btn-sm " + ("btn-primary" if editing else "btn-outline-secondary")),
                class_="hype-actions"))

    @render.ui
    def draw_status():
        # Compact color legend — the boundaries are edited by clicking their lines on the map, so
        # this is informational (swatch + name + ✓/○, active row highlighted); empty rows get a
        # small "Draw" link as the only entry point when there's no line on the map to click.
        active = bnd_slot()
        defs = [("up", "Upstream", UP_STYLE["color"], up_feat()),
                ("left", "Left floodplain", LEFT_STYLE["color"], left_feat()),
                ("right", "Right floodplain", RIGHT_STYLE["color"], right_feat()),
                ("down", "Downstream", DOWN_STYLE["color"], down_feat())]
        if wse_mode_v() == "draw":
            defs.append(("wse", "Water surface", WSE_STYLE["color"], wse_extent_feat()))
        rows = []
        for slot, label, color, feat in defs:
            present = feat is not None
            inner = [ui.span(class_="hype-leg-swatch", style=f"background:{color};"),
                     ui.span(label, class_="hype-leg-name"),
                     ui.span(class_="hype-st st-done" if present else "hype-st st-todo")]
            if present:
                inner.append(ui.input_action_link(f"bnd_edit_{slot}",
                             "Save" if slot == active else "Edit", class_="hype-leg-edit"))
            elif active is None:
                inner.append(ui.input_action_link(f"bnd_draw_{slot}", "Draw", class_="hype-leg-draw"))
            rows.append(ui.div(*inner, class_="hype-leg-row" + (" active" if slot == active else "")))
        if active:
            hint = "Editing on the map — drag vertices, or use the bar to Clear & redraw / Done."
        elif _domain_build() is not None:
            hint = "Click a boundary line on the map to edit it."
        elif any(f is not None for *_, f in defs):
            hint = "Click a boundary on the map to edit, or Generate boundaries."
        else:
            hint = "Click Generate boundaries to build the four sides."
        return ui.div(ui.div(hint, class_="hype-instr"), ui.div(*rows, class_="hype-legend"))

    @render.ui
    def domain_warning():
        # Boundary sanity warnings, stacked. Red cards first: a boundary line lying across the
        # reach centerline (bnd_conflicts) BLOCKS the surface run + mesh preview until fixed.
        # Then the soft amber guidance: (1) the four boundaries don't meet at a corner (the
        # derived domain still force-closes; snapping auto-connects near endpoints — this
        # catches the ones too far apart to snap); (2) the reach centerline doesn't meet the
        # up/down boundaries. Navigation itself is never gated.
        if not _HAS_MAP or current_step() != STEP_BOUNDARIES:
            return None
        children = [ui.div(c["msg"] + " Model runs are blocked until this is fixed.",
                           class_="hype-card err")
                    for c in bnd_conflicts()]
        gap = geometry.corner_gaps_m(up_feat(), left_feat(), right_feat(), down_feat())
        if gap is not None and gap > 25.0:
            children.append(ui.div(
                ui.div(f"Boundaries don't meet at a corner (gap ≈ {gap:.0f} m). Drag an endpoint "
                       "onto the neighbouring line to connect them, or:"),
                ui.input_action_button("snap_corners", "Snap corners together",
                                       class_="hype-warn-btn"),
                class_="hype-warn"))
        for msg in geometry.reach_boundary_issues(reach_feat(), up_feat(), left_feat(),
                                                  right_feat(), down_feat()):
            children.append(ui.div(msg, class_="hype-warn"))
        return ui.TagList(*children) if children else None

    @render.ui
    def kzone_list():
        # Per-zone editable KH/KV rows. Structural deps ONLY (the zone list + the drawing
        # flag): values are painted from each Feature's properties and edited in place by
        # _kz_mirror, so typing never re-renders (the gradient-table discipline).
        kz = kzone_feats()
        parts = []
        if kz_adding():
            parts.append(ui.p("Drawing a K-zone — click on the map to place vertices, "
                              "double-click to finish.", class_="hype-chk"))
        elif not kz:
            parts.append(ui.p("No K-zones yet — click Add K-zone.", class_="hype-chk"))
        if kz:
            rows = [ui.div(ui.span("Zone", class_="hype-kz-h"),
                           ui.span("KH (m/d)", class_="hype-kz-h"),
                           ui.span("KV (m/d)", class_="hype-kz-h"),
                           ui.span(""), class_="hype-kz-row hype-kz-head")]
            for f in kz:
                p = f.get("properties") or {}
                uid = p.get("uid") or ""
                name = ui.span(str(p.get("LABEL") or "Zone"),
                               (ui.span("NRCS", class_="hype-kz-src")
                                if p.get("src") == "nrcs" else None),
                               class_="hype-kz-name", title=str(p.get("LABEL") or ""))
                rows.append(ui.div(
                    name,
                    ui.input_numeric(f"kz_kh_{uid}", None, value=p.get("KH"),
                                     min=0.0001, step=1.0),
                    ui.input_numeric(f"kz_kv_{uid}", None, value=p.get("KV"),
                                     min=0.0001, step=0.5),
                    ui.tags.button("×", type="button", class_="hype-gpt-rm",
                                   onclick=(f"Shiny.setInputValue('kz_rm_{uid}', "
                                            f"(window._kzrm_{uid}=(window._kzrm_{uid}||0)+1))"),
                                   title="Remove this zone"),
                    class_="hype-kz-row"))
            parts.append(ui.div(*rows, class_="hype-kz-table"))
            parts.append(ui.p("Double-click a zone on the map to edit its shape.",
                              class_="hype-instr hype-dim"))
        return ui.TagList(*parts)

    @render.ui
    def dem_status():
        if dem_path() is None:
            return None
        m = dem_meta() or {}
        res, src = m.get("resolution_m"), m.get("source", "USGS 3DEP")
        verb = "imported" if src == "Local raster" else "fetched"
        tag = f"{res:g} m ({src})" if res else src
        try:
            s = dem.dem_summary(dem_path())
            return ui.p(ui.span(class_="hype-st st-done"),
                        f"Terrain {verb} · {tag} · {s['width']} × {s['height']} px · "
                        f"{s['min']:.1f}–{s['max']:.1f} m", class_="hype-chk ok")
        except Exception:  # noqa: BLE001
            return ui.p(ui.span(class_="hype-st st-done"), f"Terrain {verb} · {tag}",
                        class_="hype-chk ok")

    @render.ui
    def dem_local_src():
        """The Local-raster section of the DEM pane: source file card + actions. One compact
        card per state (none picked / linked / changed on disk / missing), nonce buttons only
        (this container re-renders on every dem_src change)."""
        _ = sel_node()      # re-probe the filesystem on every pane visit: render output is
        #                     cached, so without this a source renamed mid-session keeps
        #                     showing its last card until dem_src itself changes
        rec = dem_src() or {}
        p = rec.get("path")
        hint = ui.div("The project stores a link to the source file and imports a working "
                      "copy sized to the reach. Elevations must be in meters.",
                      class_="hype-instr")
        if not p:
            return ui.TagList(
                ui.div(ui.div("No raster selected.", class_="hype-dem-none"),
                       class_="hype-dem-src"),
                ui.div(_evt_btn("dem_choose_evt", "Choose raster...", "btn-primary"),
                       class_="hype-actions"),
                hint)
        pp = Path(p)
        name = ui.div(pp.name, class_="hype-dem-name", title=str(pp))
        folder = ui.div(str(pp.parent), class_="hype-dem-dir", title=str(pp.parent))
        if not pp.is_file():
            return ui.TagList(
                ui.div(name, folder,
                       ui.div(ui.span(class_="hype-st st-warn"),
                              "The linked file was not found. The imported terrain is "
                              "still in the project.", class_="hype-dem-note"),
                       class_="hype-dem-src warn"),
                ui.div(_evt_btn("dem_choose_evt", "Locate the file...", "btn-primary"),
                       class_="hype-actions"),
                hint)
        rows = [name, folder]
        try:
            changed = (rec.get("src_mtime") is not None
                       and abs(pp.stat().st_mtime - float(rec["src_mtime"])) > 1e-6)
        except (OSError, TypeError, ValueError):
            changed = False
        if changed:
            rows.append(ui.div(ui.span(class_="hype-st st-warn"),
                               "The source file has changed since it was imported.",
                               class_="hype-dem-note"))
        return ui.TagList(
            ui.div(*rows, class_="hype-dem-src"),
            ui.div(_evt_btn("dem_import_evt",
                            "Re-import terrain" if dem_path() is not None
                            else "Import terrain", "btn-primary"),
                   _evt_btn("dem_choose_evt", "Choose a different raster...",
                            "btn-sm btn-outline-secondary"),
                   class_="hype-actions"),
            hint)

    @render.ui
    def dem_legend():
        lohi = dem_lohi_v()
        if dem_path() is None or not lohi:
            return None
        uri = results.colorbar_datauri(lohi[0], lohi[1], cmap="terrain", label="Elevation (m)")
        return ui.img(src=uri, style="max-width:100%;height:auto;")

    @render.ui
    def estimate_box():
        est = grid_estimate()
        if not est:
            return None
        facts = (f"Domain ≈ {est['dom_w']:,.0f} × {est['dom_h']:,.0f} m · {est['nlay']} layers "
                 f"({est['ncol']}×{est['nrow']} cells/layer)")
        b = estimate.band(est["n_cells"])
        msg = estimate.band_message(est)
        if runmode.IS_DESKTOP and b == "red":
            msg = (f"Grid ≈ {est['ncol']}×{est['nrow']}×{est['nlay']} = {est['n_cells']:,} cells. "
                   "Very large — no limit in Desktop Run; expect a long, memory-hungry solve.")
        return ui.TagList(
            ui.div(facts, class_="hype-chk"),
            ui.div(msg, class_=f"hype-estimate {b}"))

    @render.ui
    def ras_estimate():
        g = _domain_gdf_4326()
        if g is None:
            return None
        try:
            cell = float(_safe("ras_cell", 10.0))
            prev = ras_mesh_prev()
            if prev and abs(float(prev.get("cell_size_m", -1)) - cell) < 1e-9:
                n, meshed = int(prev["cell_count"]), True    # real count from `ras mesh`
            else:
                n, meshed = ras_engine.estimate_cell_count(g, cell), False
        except Exception:  # noqa: BLE001
            return None
        green, cap = ras_engine.cell_budget()
        band = "green" if n <= green else ("amber" if n <= cap else "red")
        lead = f"{n:,} mesh cells (meshed)" if meshed else f"≈ {n:,} mesh cells"
        red_msg = ("very large — no limit in Desktop Run; expect a long solve."
                   if runmode.IS_DESKTOP else
                   f"over the {cap:,}-cell limit; increase the cell size.")
        msg = (f"{lead} at {cell:g} m — "
               + {"green": "quick run.",
                  "amber": ("will take a while on this computer." if runmode.IS_DESKTOP
                            else "will take a while on this server."),
                  "red": red_msg}[band])
        return ui.div(msg, class_=f"hype-estimate {band}")

    @render.ui
    def ras_controls():
        # Everything transient about the surface run lives here (NOT in the sw pane body) so
        # re-renders never remount the parameter inputs above (kept values notwithstanding).
        running = ras_task.status() == "running"
        meshing = mesh_prev_task.status() == "running"
        res = ras_result()
        if running:
            return ui.TagList(
                ui.output_ui("ras_run_head"),      # ticking progress bar — isolated re-render
                ui.tags.pre(ui.output_text("ras_log"), class_="hype-log"),
                ui.div(ui.input_action_button("cancel_surface", "Cancel",
                                              class_="btn-sm btn-outline-danger"),
                       class_="hype-actions"),
            )
        blocked = bool(bnd_conflicts())     # boundary line across the centerline — hard gate
        if meshing:
            mesh_row = ui.div(ui.div(class_="hype-spinner"),
                              ui.span("Meshing…", class_="hype-run-label"),
                              class_="hype-run-head")
        else:
            mesh_row = ui.div(ui.input_action_button(
                "ras_mesh_btn", "Preview mesh", class_="btn-sm btn-outline-secondary",
                disabled=blocked),
                class_="hype-actions")
        parts = [
            mesh_row,
            ui.div(ui.input_action_button("run_surface", "Run surface model",
                                          class_="btn-primary",
                                          disabled=meshing or blocked), class_="hype-actions"),
        ]
        prev_mesh = ras_mesh_prev()
        if prev_mesh and prev_mesh.get("too_big"):
            # The overlay cap (MESH_PREVIEW_MAX_FACES) is a display-quality limit; without
            # this line a too-big mesh reads as "checked but silently missing" on the map.
            parts.append(ui.div(
                f"Mesh has {prev_mesh.get('n_faces', 0):,} faces, too many to draw as a "
                "map overlay; the model itself is unaffected.", class_="hype-instr"))
        if blocked:
            parts = [
                ui.div("A boundary line crosses the stream centerline. Fix it in the "
                       "Boundaries step before meshing or running.", class_="hype-card err"),
                _next_hint("bnd", "Fix boundaries →"),
                *parts,
            ]
        if res:
            m = res.get("max_depth_m") or 0.0
            n_parts = int(res.get("n_parts") or 0)
            main_frac = float(res.get("main_frac") or 1.0)
            pools = f" · {n_parts} parts" if n_parts > 1 else ""
            parts.append(ui.div(
                ui.span(class_="hype-st st-done"),
                f"Surface model complete — {res.get('n_cells', 0):,} cells · "
                f"max depth {m:.2f} m · wetted area {res.get('wetted_area_m2', 0):,.0f} m²"
                f"{pools} · {res.get('runtime_s', 0):.0f} s. The modeled water surface feeds "
                f"the groundwater run.", class_="hype-chk ok"))
            filt = wetted_filter_res()
            if filt and filt.get("kept_feat") is not None and filt.get("n_removed"):
                n_rm = int(filt["n_removed"])
                parts.append(ui.div(
                    ui.span(class_="hype-st st-done"),
                    f"GW extent: {n_rm} isolated pool{'s' if n_rm != 1 else ''} removed "
                    f"({filt.get('removed_m2', 0):,.0f} m²); see Wetted extent.",
                    class_="hype-chk"))
            if main_frac < 0.9:
                terr_res = float(res.get("terrain_res_m") or 0.0)
                hint = (f"The {terr_res:.0f} m terrain is likely too coarse — re-fetch the DEM "
                        "at 1 m if available."
                        if terr_res > 2.0 else
                        "May be real braided flow — try a smaller cell size or higher flow for "
                        "a continuous surface.")
                parts.append(ui.div(
                    f"Fragmented water surface — the largest connected patch is only "
                    f"{main_frac:.0%} of the wetted area. {hint}",
                    class_="hype-estimate amber"))
            parts.append(ui.div("Toggle Depth / Water surface in the tree; select one for its "
                                "legend and opacity.", class_="hype-instr"))
        elif ras_log_tick():
            parts.append(ui.tags.pre(ui.output_text("ras_log"), class_="hype-log"))
        return ui.TagList(*parts)


    @render.ui
    def ras_run_head():
        secs = int(ras_elapsed()); mm, ss = secs // 60, secs % 60
        stage = ras_stage() or "Running"
        pct = ras_pct()
        row = [ui.div(class_="hype-spinner"),
               ui.span(stage, class_="hype-run-label"),
               ui.span(f"{mm}:{ss:02d}", class_="hype-elapsed")]
        if pct is None:                                # indeterminate stage (python steps)
            bar = ui.div(ui.div(class_="hype-prog-bar indet"), class_="hype-prog")
            label = None
        else:
            bar = ui.div(ui.div(class_="hype-prog-bar", style=f"width:{pct}%"),
                         class_="hype-prog")
            text = f"{pct}%"
            if stage == "Computing" and pct >= 5:      # ETA only where %-of-simulated-time is linear
                stage_elapsed = max(time.monotonic() - ras_stage_t0(), 0.1)
                remain = int(stage_elapsed / pct * (100 - pct))
                text += f" · about {remain // 60}:{remain % 60:02d} left"
            label = ui.div(text, class_="hype-prog-label")
        return ui.TagList(ui.div(*row, class_="hype-run-head"), bar, label)

    @render.text
    def ras_log():
        ras_log_tick()
        return "\n".join(ras_log_lines[-200:]) or "Starting…"

    @render.ui
    def mesh_status():
        if mesh_task.status() == "running":
            return ui.div(
                ui.div(ui.div(class_="hype-spinner"), ui.span("Building 3D mesh…"),
                       class_="hype-busy"),
                ui.div(ui.input_action_button("mesh3d_cancel", "Cancel",
                                              class_="btn-sm btn-outline-danger"),
                       class_="hype-actions"),
            )
        g = mesh_geom()
        if not g:
            return ui.p("Click Preview grid in 3D to inspect the grid before running.",
                    class_="hype-chk")
        f = g.get("decimation", 1)
        note = "" if f == 1 else f" · shown at 1/{f} resolution"
        return ui.p(ui.span(class_="hype-st st-done"),
                    f"{g.get('nActiveFull', 0):,} active cells{note}.", class_="hype-chk ok")

    @render.ui
    def mesh3d_style():
        # Reveal the vtk.js scene as the canvas whenever the header toggle says 3D.
        if view_mode_v() == "3d":
            return ui.tags.style(".hype-mesh3d{display:block;}")
        return None

    @render.ui
    def busy():
        s = stage()
        running = dem_task.status() == "running" or run_task.status() == "running"
        return ui.div(ui.div(class_="hype-spinner"), ui.span(s), class_="hype-busy") if (s and running) else None

    @render.ui
    def run_status():
        log_tick()
        if run_task.status() != "running":
            return ui.div(ui.div(class_="hype-spinner"), ui.span("Starting…"), class_="hype-busy")
        n = step_v()
        secs = int(elapsed_v()); mm, ss = secs // 60, secs % 60
        label = RUN_STEPS.get(n, RUN_STEPS[0])
        head = f"Step {n} of {RUN_TOTAL} — {label}" if n else label
        if n:
            pct = max(6, min(100, int(round(n / RUN_TOTAL * 100))))
            bar = ui.div(ui.div(class_="hype-prog-bar", style=f"width:{pct}%;"), class_="hype-prog")
        else:
            bar = ui.div(ui.div(class_="hype-prog-bar indet"), class_="hype-prog")
        return ui.div(
            ui.div(ui.div(class_="hype-spinner"), ui.span(head, class_="hype-run-label"),
                   ui.span(f"{mm}:{ss:02d}", class_="hype-elapsed"), class_="hype-run-head"),
            bar,
            class_="hype-run-status",
        )

    @render.text
    def run_log():
        log_tick()
        return "\n".join(log_lines[-200:]) or "Starting… preparing terrain and model inputs."

    @render.ui
    def result_summary():
        res = run_result()
        if not res:
            return None
        txt = results.summary_text(res, work_dir)
        m = dem_meta() or {}
        if m.get("resolution_m"):
            txt = f"{txt}\nDEM: {m['resolution_m']} m ({m.get('source', 'USGS 3DEP')})"
        return ui.tags.pre(txt, class_="hype-log")

    @render.ui
    def fp_time_legend():
        # In-pane rainbow legend, shown while lines or particles color by residence
        # time. One bar serves both (they share the scale); the _vis_state read makes
        # class checkbox toggles re-stretch the range live.
        _vis_state()
        lmode = fp_line_mode_v()
        amode = fp_anim_mode_v()
        line_on = lmode in FP_LINE_RAINBOW
        anim_on = amode in ("total", "elapsed") and fp_anim_on_v()
        if not (line_on or anim_on):
            return None
        rng = _fp_time_range()
        if rng is None:
            return None
        label = video_mod.legend_label(amode if anim_on else None,
                                       lmode if line_on else None)
        stops = video_mod.turbo_css_stops()
        grad = ("linear-gradient(90deg,"
                + ",".join(f"{c} {i * 100 // 12}%" for i, c in enumerate(stops)) + ")")
        parts = [ui.div(label, class_="hype-pane-legend-title"),
                 ui.div(class_="bar", style=f"background:{grad}"),
                 ui.div(ui.span(video_mod.fmt_days(rng[0])),
                        ui.span(video_mod.fmt_days(rng[1])), class_="ticks")]
        if line_on and anim_on and lmode != amode:
            parts.append(ui.div(f"Lines: {lmode} time. Particles: {amode} time.",
                                class_="hype-pane-legend-sub"))
        return ui.div(*parts, class_="hype-pane-legend")

    @render.ui
    def hz_status():
        hz_log_tick()
        if hz_task.status() != "running":
            return ui.div(ui.div(class_="hype-spinner"), ui.span("Starting…"), class_="hype-busy")
        n = hz_step_v()
        secs = int(hz_elapsed()); mm, ss = secs // 60, secs % 60
        label = HZ_STEPS.get(n, HZ_STEPS[0])
        head = f"Step {n} of {HZ_TOTAL} — {label}" if n else label
        if n:
            pct = max(6, min(100, int(round(n / HZ_TOTAL * 100))))
            bar = ui.div(ui.div(class_="hype-prog-bar", style=f"width:{pct}%;"), class_="hype-prog")
        else:
            bar = ui.div(ui.div(class_="hype-prog-bar indet"), class_="hype-prog")
        return ui.div(
            ui.div(ui.div(class_="hype-spinner"), ui.span(head, class_="hype-run-label"),
                   ui.span(f"{mm}:{ss:02d}", class_="hype-elapsed"), class_="hype-run-head"),
            bar, class_="hype-run-status")

    @render.text
    def hz_log():
        hz_log_tick()
        return "\n".join(hz_log_lines[-200:]) or "Starting the hyporheic-zone analysis…"

    @render.ui
    def hz_summary():
        res = hz_view()                    # follows the displayed run
        if not res:
            return None
        counts = (res.get("stats") or {}).get("counts", {})
        by = counts.get("by_class", {})
        rows = [ui.div(_hz_swatch(cls), ui.span(HZ_LABEL[cls], class_="hype-hz-k"),
                       ui.span(f"{by.get(cls, 0):,}", class_="hype-hz-v"), class_="hype-hz-row")
                for cls in HZ_CLASSES]
        rows.append(ui.div(ui.span("Unresolved", class_="hype-hz-k"),
                           ui.span(f"{by.get('unresolved', 0):,}", class_="hype-hz-v"),
                           class_="hype-hz-row"))
        return ui.div(ui.div("Particles by class", class_="hype-instr"), *rows)

    @render.ui
    def hz_signature():
        """The three hydraulic dimensions, on the pane a user actually lands on after a run.

        Same derivation as the report and the Screening panes, so the number here is the number
        there. Six lines of content and no accordion: the full signature, the governing equation
        and the sources belong to the site report, and the jump chip below says so."""
        res = hz_view()                    # follows the displayed run
        if not res:
            return None
        try:
            full_stats = res.get("stats") or {}
            sig = signature.derive(signature.SignatureInputs.from_hz_bundle(
                full_stats, _flux_metrics(full_stats, res.get("hz_dir")),
                streamflow_cms=_streamflow_cms(), reach_length_m=_reach_length_m(),
                snapshot_porosity=_safe("porosity", None)))
            cards = signature.card_view(sig)
            regime = None
            # The regime sentence reads the BASECASE results model; suppress it while an
            # alternative is displayed so the block never mixes two runs.
            if results_model() and alt_view() is None:
                # The regime sentence needs the threshold scenarios, which live on the results
                # model. Present only once a report has been built; the cards do not wait for it.
                from hype_app.contracts import AssessmentResultsV2
                regime = signature.regime_description(
                    AssessmentResultsV2.model_validate(results_model()))
        except Exception:  # noqa: BLE001 — a read-only summary must never break the pane
            return None
        return ui.div(
            ui.div(dims.SIGNATURE_TITLE, class_="hype-props-title"),
            ui.div(dims.SIGNATURE_SENTENCE, class_="hype-instr"),
            _sig_kpi(cards),
            *([ui.div(regime.contact_statement, class_="hype-props-note")] if regime else []),
            _next_hint("report.hyd", "Full hydraulic signature in the report →"))

    @render.ui
    def hz_sel_props():
        pids = hz_sel_pids()
        gdf = hz_gdf()
        if not pids or gdf is None:
            return None
        sub = gdf[gdf["particleid"].isin(pids)]
        if not len(sub):
            return None
        clear = ui.div(ui.input_action_button("fp_clear", "Clear selection",
                                              class_="btn-sm btn-outline-secondary"),
                       class_="hype-props-actions")
        if len(sub) == 1:
            r = sub.iloc[0]
            return ui.div(
                ui.div("Selected path", class_="hype-hz-head"),
                _kv("Particle", f"#{int(r['particleid'])}"),
                _kv("Class", HZ_LABEL.get(r.get("hz_class"), str(r.get("hz_class", "?")))),
                _kv("Origin → exit", f"{r.get('origin_side', '?')} → {r.get('exit_side', '?')}"),
                _kv("Length", f"{r.get('length_m', 0):g} m"),
                _kv("Residence time", f"{r.get('total_time_d', 0):g} d"),
                clear)
        by_cls = sub["hz_class"].value_counts().to_dict() if "hz_class" in sub else {}
        tally = " · ".join(f"{HZ_LABEL.get(c, c)}: {n}" for c, n in by_cls.items()) or str(len(sub))
        rows = [ui.div(f"{len(sub)} paths selected", class_="hype-hz-head"),
                _kv("By class", tally)]
        if "length_m" in sub:
            rows.append(_kv("Length", f"mean {sub['length_m'].mean():,.1f} m"))
        if "total_time_d" in sub:
            rows.append(_kv("Residence", f"mean {sub['total_time_d'].mean():,.1f} d"))
        rows.append(clear)
        return ui.div(*rows)

    @render.ui
    def head_legend():
        rng = head_rng()
        if rng is None:
            return None
        try:
            uri = results.colorbar_datauri(rng[0], rng[1], cmap="viridis",
                                           label="Hydraulic head (m)")
        except Exception:  # noqa: BLE001
            return None
        return ui.img(src=uri, style="width:100%; max-width:320px; margin:2px 0 6px;")

    @render.ui
    def readout():
        if not _HAS_MAP:
            return None
        z, c = _view()
        if not c:
            return ui.div("Search or zoom to a stream to begin", class_="hype-readout")
        crs = proj_crs()
        crs_txt = f" · CRS {crs.to_epsg()}" if crs is not None and crs.to_epsg() else ""
        return ui.div(f"Zoom {int(z)} · {float(c[0]):.4f}, {float(c[1]):.4f}{crs_txt}",
                      class_="hype-readout")

    @render.ui
    def flow_loading():
        # Bottom-center cue that the clickable NHD stream vectors are being fetched — only on the
        # Reach step, zoomed in enough for them to appear, while a fetch is actually in flight.
        if not _HAS_MAP or current_step() != STEP_REACH:
            return None
        z, _c = _view()
        if z is None or int(z) < 12 or flow_task.status() != "running":
            return None
        return ui.div(ui.div(class_="hype-spinner"), ui.span("Loading streams…"),
                      class_="hype-flow-loading")

    @render.ui
    def xsect_style():
        # Reveal the ⛰ cross-section tool (www/xsection.js; display:none in styles.css)
        # whenever terrain exists AND its tree layer is checked — the tool samples the DEM
        # the user is looking at. _vis_state re-runs this on any checkbox flip.
        _vis_state()
        if not _HAS_MAP or dem_path() is None or not _eff_checked("terrain.dem"):
            return None
        return ui.tags.style(".hype-map-wrap .hype-xsect{display:block !important;}")

    @render.ui
    def map_edit_style():
        # On the Reach + Boundaries steps the draw tool is auto-driven (www/reach_draw.js), so hide
        # the Leaflet.draw toolbar (the control stays in the DOM — we click its anchors; its mouse
        # tooltip lives in the popup pane, so it still shows). Add a crosshair only while a pick or a
        # fresh draw is actually possible. Mirrors EASI's cursor_style pattern.
        step = current_step()
        if not _HAS_MAP or step not in (STEP_REACH, STEP_BOUNDARIES, STEP_K, STEP_MESH,
                                        STEP_RESULTS):
            return None
        css = ".hype-map-wrap .leaflet-draw{display:none !important;}"
        if step == STEP_RESULTS:                    # nothing to draw here; show the box-select tool
            # Route clicks to the flow paths + their nodes ONLY. Two layers otherwise swallow
            # every real click over the domain: the wetted-extent polygon's fill (interactive
            # across its whole area), and the DivIcon label markers (marker pane sits ABOVE all
            # vector overlays). Verified with document.elementFromPoint — element-targeted
            # synthetic clicks bypass hit-testing and cannot catch this.
            return ui.tags.style(
                css + ".hype-map-wrap .hype-fpsel{display:block !important;}"
                ".hype-map-wrap path.leaflet-interactive:not(.hype-fp-line):not(.hype-fp-node)"
                "{pointer-events:none !important;}"
                ".hype-map-wrap .leaflet-marker-icon{pointer-events:none !important;}")
        if step == STEP_REACH:
            z, _c = _view()
            no_reach = reach_feat() is None
            armed = delineate_mode() == "manual" and no_reach
            picking = (delineate_mode() == "auto" and no_reach and z is not None
                       and int(z) >= 12 and len(pick_pts()) < 2)
            crosshair = armed or picking
        elif step == STEP_BOUNDARIES:               # crosshair while drawing a fresh side
            slot = bnd_slot()
            sv = _slot_value(slot) if slot else None
            crosshair = bool(slot) and (sv is None or sv() is None)
        elif step == STEP_MESH:                     # gw hub — crosshair while placing a grad
            crosshair = grad_adding() is not None or wells_adding()   # point or a well
            if crosshair:
                # The armed click is a MAP click, but with results displayed the domain is
                # carpeted with DivIcon labels (contour values, head pills, exchange dots)
                # and interactive vector fills that swallow it — the marker pane sits above
                # every vector overlay (the STEP_RESULTS routing lesson above). While armed,
                # nothing else needs to be clickable, so let everything pass through.
                css += (".hype-map-wrap .leaflet-marker-icon{pointer-events:none !important;}"
                        ".hype-map-wrap path.leaflet-interactive"
                        "{pointer-events:none !important;}")
        else:                                       # STEP_K — crosshair while adding a K-zone
            crosshair = kz_adding()
        if crosshair:
            css += (".hype-map-wrap .leaflet-grab{cursor:crosshair !important;}"
                    ".hype-map-wrap .leaflet-container.leaflet-dragging,"
                    ".hype-map-wrap .leaflet-container.leaflet-dragging .leaflet-grab"
                    "{cursor:grabbing !important;}")
        return ui.tags.style(css)

    @reactive.effect
    async def _push_reach_state():
        # Tell the client (www/reach_draw.js) how to guide the map: the follow-cursor pick tooltip
        # (Reach auto), auto-arm a fresh draw (`armShape` = line/polygon), and/or allow
        # double-click-to-edit. Covers the Reach centerline and the four Boundaries slots + WSE.
        if not _HAS_MAP:
            return
        step = current_step()
        z, _c = _view()
        picking = arm = can_edit = auto_edit = False
        arm_shape = "line"
        slot_id = None
        if step == STEP_REACH:
            mode = delineate_mode()
            no_reach = reach_feat() is None
            editing = reach_edit() and not no_reach               # centerline loaded for vertex editing
            picking = (mode == "auto" and no_reach and z is not None and int(z) >= 12
                       and len(pick_pts()) < 2)
            arm = mode == "manual" and no_reach
            auto_edit = editing            # engage Leaflet.draw edit now (auto OR manual reach)
            can_edit = editing             # + double-click save fallback while editing
            slot_id = "reach" if editing else None
        elif step == STEP_BOUNDARIES:
            slot = bnd_slot()
            slot_id = slot
            if slot:
                sv = _slot_value(slot)
                has = sv() is not None if sv is not None else False
                arm_shape = "polygon" if slot == "wse" else "line"
                arm = not has                                     # empty slot → draw it
                auto_edit = has                                   # selected existing line → edit now
                can_edit = has                                    # + double-click fallback
        elif step == STEP_K:
            slot_id = "kzone"
            arm_shape = "polygon"                 # K-zones are polygons; Add arms a fresh draw
            arm = bool(kz_adding())
            can_edit = (not kz_adding()) and len(kzone_feats()) > 0
        await session.send_custom_message("hype_reach", {
            "step": step, "slot": slot_id, "picking": bool(picking), "arm": bool(arm),
            "canEdit": bool(can_edit), "autoEdit": bool(auto_edit), "armShape": arm_shape,
            "commit": int(bnd_commit()),      # ++ from the legend "Save" link → client clicks Save
            "slotName": {"reach": "Reach centerline", "up": "Upstream",
                         "left": "Left floodplain", "right": "Right floodplain",
                         "down": "Downstream", "wse": "Water surface"}.get(slot_id, ""),
        })


app = App(app_ui, server, static_assets=Path(__file__).parent / "www")
