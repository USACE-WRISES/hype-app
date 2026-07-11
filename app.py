"""Hyporheic web app — a StreamStats-style Shiny app that builds and runs a MODFLOW 6 +
MODPATH 7 hyporheic model from a map-defined reach, and shows the pathlines/heads.

Flow (six stages, shown as the header stage bar): define the reach (auto NHD pick or manual
draw) → terrain auto-fetches from 3DEP → boundaries auto-generate from bankfull geometry →
choose the water surface (HEC-RAS 2025 2D run / wetted extent / uploaded raster) → review
subsurface properties + grid and run MODFLOW 6 + MODPATH 7 → delineate the hyporheic zone
and explore flow paths, volumes, and heads. Download the whole project as a zip.
"""
from __future__ import annotations

import multiprocessing as mp
import os
import queue as _queue
import re
import shutil
import tempfile
import threading
import time
from datetime import datetime
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

from hype_app import (assess, bieger, bundle, carve, delineate, dem, estimate, geocode,  # noqa: E402
                      geometry, hydro, hz_results, mesh, ras_results, report as report_mod,
                      results, scene, snapshot, ui_tree)
from hype_app import hz_run  # noqa: E402
from hype_app import sens_run  # noqa: E402
from hype_app import soil_run  # noqa: E402
from hype_app import usgs_run  # noqa: E402
from hype_app import ras as ras_engine  # noqa: E402
from hype_app import run as runner  # noqa: E402

try:
    from ipyleaflet import (DivIcon, DrawControl, GeoJSON, ImageOverlay, LayerGroup,
                            Map, Marker, ScaleControl, TileLayer, ZoomControl)
    from ipywidgets import Layout
    from shinywidgets import output_widget, reactive_read, render_widget
    _HAS_MAP = True
except Exception:  # pragma: no cover
    _HAS_MAP = False

USGS_IMAGERY = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
USGS_TOPO = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
USGS_HYDRO = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSHydroCached/MapServer/tile/{z}/{y}/{x}"
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
LEFT_STYLE = {"color": "#1f6feb", "weight": 3, "opacity": 0.95}      # Left FPL (blue)
RIGHT_STYLE = {"color": "#d83933", "weight": 3, "opacity": 0.95}     # Right FPL (red)
UP_STYLE = {"color": "#f08c00", "weight": 3, "opacity": 0.95}        # Upstream boundary (orange)
DOWN_STYLE = {"color": "#9b59b6", "weight": 3, "opacity": 0.95}      # Downstream boundary (purple)
KZONE_STYLE = {"color": "#7b3fa0", "weight": 2, "opacity": 0.95, "fill": False}
SOILS_STYLE = {"color": "#8a6d3b", "weight": 1, "opacity": 0.9,        # NRCS SSURGO polygons (tan)
               "fillColor": "#d2b48c", "fillOpacity": 0.22}
NHD_STYLE = {"color": "#00c2ff", "weight": 3.5, "opacity": 0.95}     # clickable NHD flowlines (bold)
REACH_STYLE = {"color": "#ff2d95", "weight": 5, "opacity": 0.95}     # the analysis reach (magenta — pops on USGS topo, distinct from cyan NHD)
CAP_STYLE = {"color": "#333333", "weight": 2, "opacity": 0.9, "dashArray": "6 5", "fill": False}

# An empty FeatureCollection — used by _decor_show to CLEAR a layer's rendered children before a
# visible False→True reveal, so the reveal's addData renders nothing and the following data-set is
# the only addData (guards against the bursty-flush double-add that duplicated the reach line).
_EMPTY_FC = {"type": "FeatureCollection", "features": []}

STEP_REACH, STEP_DEM, STEP_BOUNDARIES, STEP_SURFACE, STEP_K, STEP_MESH, STEP_RUN, STEP_RESULTS = (
    "reach", "dem", "boundaries", "surface", "k", "mesh", "run", "results")

RAS_UNAVAILABLE_MSG = (
    "The HEC-RAS 2025 engine isn't available here — on Windows set HYPE_RAS_BIN to a "
    "HEC-RAS 2025 install (the folder containing ras.exe); on Linux the bundled "
    "bin/ras2025 runtime is used.")

APP_VERSION = "2026.07"        # About dialog + run_config.json + the project-file manifest

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
HZ_TOTAL = 7
HZ_STEPS = {0: "Preparing…", 1: "Loading the flow solution", 2: "Seeding particles",
            3: "Tracking forward (endpoints)", 4: "Tracking backward (endpoints)",
            5: "Classifying + delineating volumes", 6: "Tracing display pathlines",
            7: "Writing artifacts"}
HZ_MAX_PARTICLES = int(os.environ.get("HYPE_HZ_MAX_PARTICLES", "2000000"))

BC_CORNER = "4 Corner Gradients"
BC_PROFILE = "Spatially Varying Gradient"
BC_QUAL = "Qualitative"          # app-side only: category × reference slope -> profile strings
# Locked qualitative categories (revision §3.4): multiplier × reference slope.
_QUAL_CHOICES = {"strongly_gaining": "Strongly gaining (+1.0 × slope)",
                 "slightly_gaining": "Slightly gaining (+0.5 × slope)",
                 "neutral": "Neutral (0)",
                 "slightly_losing": "Slightly losing (−0.5 × slope)",
                 "strongly_losing": "Strongly losing (−1.0 × slope)"}

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
        ui.tags.script(src=_asset("measure2d.js")),  # map length ruler (client-only, always on)
        ui.tags.script(src=_asset("xsection.js")),   # terrain cross-section (shown while DEM on)
        ui.tags.script(src=_asset("mesh3d.js")),     # lazy-loads vtk.js from a CDN on first Compute
        ui.tags.script(src=_asset("tree.js")),       # layer tree (left panel) + panel chrome
    ),
    ui.div(
        ui.div(
            ui.span("HYPE", ui.tags.small("Hyporheic Exchange Explorer"), class_="hype-brand"),
            # 2D/3D canvas toggle — plain buttons, delegated via www/tree.js (data-view),
            # active states synced from the hype_tree payload's `view` field.
            ui.div(ui.tags.button("2D map", type="button", class_="hype-view-btn active",
                                  **{"data-view": "2d"}),
                   ui.tags.button("3D view", type="button", class_="hype-view-btn",
                                  **{"data-view": "3d"}),
                   class_="hype-view-toggle"),
            ui.div(ui.output_ui("dl_project"),
                   ui.input_action_link("nav_new", "New project"),
                   ui.input_action_link("nav_open", "Open"),
                   ui.output_ui("save_project"),
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
        ui.output_ui("readout"),
        ui.output_ui("flow_loading"),
        ui.output_ui("map_edit_style"),
        ui.output_ui("xsect_style"),
        ui.div(id="hype-mesh3d", class_="hype-mesh3d"),     # 3D mesh viewer overlay (vtk.js)
        ui.output_ui("mesh3d_style"),
        class_="hype-shell",
    ),
    title="HYPE — Hyporheic Exchange Explorer",
    padding=0,
    fillable=True,
)


def server(input, output, session):
    work_dir = Path(tempfile.mkdtemp(prefix="hype_session_"))

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

    @reactive.effect
    def _auto_view_mode():
        # Preserve the old Mesh-step UX: entering the Model-grid context flips to 3D, leaving
        # flips back — the header toggle overrides either way until the next crossing.
        step = current_step()
        prev = _map_ui.get("view_step")
        _map_ui["view_step"] = step
        if prev == step:
            return
        if step == STEP_MESH:
            view_mode_v.set("3d")
        elif prev == STEP_MESH:
            view_mode_v.set("2d")

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
    dem_meta = reactive.value(None)        # {"resolution_m", "source"} of the fetched 3DEP DEM
    carve_active = reactive.value(False)   # a carved channel is applied to the terrain
    carve_meta = reactive.value(None)      # {path, diff_path, cells_cut, max_cut_m}
    _stale_marks = reactive.value(frozenset())  # {"sw","gw"} whose results predate a carve/revert
    origin_override = reactive.value(None)  # user-set Model Origin (streambed elev, m); None = computed default

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
    fp_stats = reactive.value(None)         # per-particle flow-path metrics DataFrame (Results)
    fp_gdf = reactive.value(None)           # 4326 pathlines gdf — the drawn = selectable set
    sel_pids = reactive.value(())           # selected flow-path particleids (tuple)
    head_tifs = reactive.value([])          # per-layer head GeoTIFF paths (index 0 = top layer)
    head_rng = reactive.value(None)         # global (vmin, vmax) for consistent head coloring
    head_layer_v = reactive.value(1)        # persisted slider state (survives pane re-renders)
    head_opacity_v = reactive.value(0.85)   # persisted slider state (survives pane re-renders)
    hd_contours_v = reactive.value(True)    # show contour lines/labels in the head display
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
    _mesh3d_proc: dict = {"p": None}       # 3-D grid-preview child process (Mesh step; cancellable)
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
        p = _mesh3d_proc.get("p")
        if p is not None:
            try:
                p.kill()
            except Exception:  # noqa: BLE001
                pass
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
            cbs = getattr(getattr(lyr, "_click_callbacks", None), "callbacks", [])
            for cb in list(cbs):
                fresh.on_click(cb)
            return fresh
        except Exception:  # noqa: BLE001
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
    def streambed_elevs():
        """Min (thalweg) elevation of the CARVED terrain along the upstream and downstream boundary
        caps → {"up", "down"} in metres, or None until the boundaries + terrain exist. The upstream
        value is the default Model Origin (streambed elevation); the downstream value is surfaced so
        the user can confirm the model reaches below it."""
        build = _domain_build()
        dem_p = active_dem()
        if not build or not dem_p:
            return None
        up = delineate.min_elevation_along_line(build["up"], dem_p)
        down = delineate.min_elevation_along_line(build["down"], dem_p)
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
        # New run / clear / redraw all null the reach → drop any stale streambed override so the
        # default recomputes for the new reach. (An in-place boundary edit keeps the reach, and the
        # override, per "persist until the app is closed or they select a new run".)
        if reach_feat() is None:
            origin_override.set(None)

    def _domain_gdf_4326():
        f = domain_feat()
        return geometry.single_feature_gdf(f) if f else None

    @reactive.effect
    def _set_proj_crs():
        g = _domain_gdf_4326()
        proj_crs.set(g.estimate_utm_crs() if g is not None else None)

    @reactive.effect
    def _sync_wse_mode():
        # Mirror the WSE-mode radio into a reactive.value so the (non-reactive) draw callback and
        # the run handler can read it. Ignore unset/None reads: while the radio remounts (props-
        # pane re-render or selection change) the input transiently reads None — writing that
        # through would clobber the persisted mode back to the default and snap the radio back.
        try:
            v = input.wse_mode()
        except Exception:  # noqa: BLE001
            return
        if v:
            wse_mode_v.set(v)

    @reactive.effect
    def _push_wse_mode_to_radio():
        # Reverse sync: when the SERVER changes the mode (a completed surface run switches to
        # "model"; regen / stale-invalidation falls back to "draw"), patch the mounted radio in
        # place. update_radio_buttons does not remount the input, so this cannot re-enter the
        # clobber loop the pane-re-render approach had (the pane reads the mode isolated).
        v = wse_mode_v()
        with reactive.isolate():
            try:
                cur = input.wse_mode()
            except Exception:  # noqa: BLE001
                return
        if v and cur and v != cur:
            ui.update_radio_buttons("wse_mode", selected=v)

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
            with reactive.isolate():        # widget callback: never while a draw/edit is live
                if bnd_slot() is not None or kz_adding():
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
        lyr = GeoJSON(data=want, style=style, name=nm, visible=True)
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
            with reactive.isolate():               # keep the SSURGO review layer on the K/soils step
                _show_soils_layer(soil_snapshot())
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
    def _dem_overlay_opacity():
        # The hillshade's visibility belongs to its "DEM (hillshade)" entry in the layers control
        # (a client-side checkbox: checked = shown, on every step, state persists because the
        # layer object is created once at fetch and only trait-mutated afterwards). This effect
        # just keeps its opacity in step with the DEM-step slider.
        opacity = float(dem_opacity_v())
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
            kzone_feats.set([f for f in data_feats if (f.get("geometry") or {}).get("type") == "Polygon"])
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
    _base_layers: dict = {}                  # "imagery"/"topo"/"hydro" -> TileLayer handles

    if _HAS_MAP:
        def _build_map():
            m = Map(center=(39.5, -98.35), zoom=4, scroll_wheel_zoom=True,
                    zoom_control=False, max_zoom=19, layout=Layout(height="100%"))
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
            _base_layers["hydro"] = TileLayer(
                url=USGS_HYDRO, name="NHD Hydrography", base=False, opacity=0.85,
                attribution=USGS_ATTR, max_native_zoom=16, max_zoom=19, visible=False)
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

    # ---- DEM fetch ----
    @reactive.extended_task
    async def dem_task(domain_geojson: dict, out_path: str, resolution) -> dict:
        def _work():
            g = geometry.single_feature_gdf(domain_geojson)
            info = dem.fetch_dem(g, out_path, resolution=resolution)
            return {"path": info["path"], "resolution_m": info["resolution_m"],
                    "source": info["source"], "summary": dem.dem_summary(info["path"])}
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

    def _launch_dem_fetch(rf):
        # Shared by the pane button and the reach→terrain auto-chain.
        import geopandas as gpd
        from shapely.geometry import mapping, shape as _shape
        meta = _reach_meta() or {}
        half = min(max(8.0 * max(meta.get("width_m", 1.0), 1.0), 250.0), 800.0)
        buf = (gpd.GeoSeries([_shape(rf["geometry"])], crs=4326).to_crs(5070)
               .buffer(half + 60.0).to_crs(4326).iloc[0])
        stage.set("Downloading 3DEP terrain for the reach…")
        dem_task({"type": "Feature", "properties": {}, "geometry": mapping(buf)},
                 str(work_dir / "inputs" / "dem.tif"), _safe("dem_res", "auto"))

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
            ui.notification_show("Enter the drainage area (km²) first — it sizes the terrain "
                                 "download and the boundaries.", type="warning", duration=6)
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
                                           name="DEM (hillshade)", opacity=op))
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
        if dem_task.status() == "error":
            ui.notification_show("DEM fetch failed at all 3DEP resolutions — try a smaller area.",
                                 type="error", duration=8)
            return
        try:
            res = dem_task.result()
        except Exception:
            return
        dem_path.set(res["path"])
        with reactive.isolate():
            dem_gen.set(dem_gen() + 1)     # fetch completed → terrain→boundaries chain marker
            # Manual draws can run downhill→uphill (auto NHD traces are upstream-first by
            # construction) — flip a backwards line NOW, in the same flush, so the boundary
            # auto-chain always consumes the corrected reach.
            if delineate_mode() == "manual" and reach_feat() is not None:
                try:
                    fixed, was_flipped = delineate.orient_reach_downstream(
                        reach_feat(), res["path"])
                except Exception:  # noqa: BLE001
                    fixed, was_flipped = None, False
                if was_flipped and fixed is not None:
                    reach_feat.set(fixed)
                    _decor_show("Reach", fixed, REACH_STYLE)
                    ui.notification_show("Centerline direction corrected to upstream → "
                                         "downstream (from the terrain).", duration=6)
        dem_meta.set({"resolution_m": res.get("resolution_m"), "source": res.get("source")})
        dem_stretch_v.set(None)            # a fresh DEM starts at the full-raster stretch
        _unhide_node_layers("terrain.dem")  # a fresh fetch always shows itself
        _show_dem_overlay(res["path"])      # hillshade backdrop
        # A fresh original DEM invalidates any carve built on the previous one.
        with reactive.isolate():
            had_carve = carve_active()
        if had_carve:
            carve_active.set(False)
            carve_meta.set(None)
            _set_layer("dem_carve", None)
        with reactive.isolate():           # chain-aware wording (light, isolated reads)
            will_chain = (_domain_build() is None
                          and not any(f is not None for f in
                                      (up_feat(), left_feat(), right_feat(), down_feat())))
        ui.notification_show("Terrain ready — generating boundaries…" if will_chain
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
            ui.notification_show("Terrain changed — existing surface/groundwater results were "
                                 "computed on the previous terrain; re-run to update them.",
                                 type="warning", duration=8)

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
                                                     name="Channel modification", opacity=0.85))
                _unhide_node_layers("terrain.chanmod")
            except Exception:  # noqa: BLE001
                pass

    @reactive.effect
    def _carve_done():
        if carve_task.status() in ("initial", "running"):
            return
        stage.set("")
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
        if not c or z is None or int(z) < 12:
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
        _MAP.zoom = 15

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
            _MAP.zoom = 15
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
            particles_per_cell=int(_safe("pt_per_cell", 1)),
            min_path_mult=float(_safe("pt_min_mult", 3.0)),
            length_units="meters", time_units="days",
            # steady hyporheic screening defaults — no stress-period fields in the UI
            nper=1, nstp=1, perlen=1.0, tsmult=1.0, sim_name="hyporheic",
            boundary_condition_mode=bc,
        )
        if bc in (BC_QUAL, BC_PROFILE):
            # Structured/qualitative controls serialize losslessly onto the engine's
            # spatially-varying path (§7.5 head-anchor method). A parse error here falls
            # back to a flat default; _start_run re-validates and blocks with the message.
            from hype_app import gradients as grad_mod
            base["boundary_condition_mode"] = BC_PROFILE     # the engine's mode name
            try:
                cfg = _gradient_config()
                base["left_boundary_gradient_profile"] = grad_mod.serialize_profile(cfg.left_controls)
                base["right_boundary_gradient_profile"] = grad_mod.serialize_profile(cfg.right_controls)
            except Exception:  # noqa: BLE001
                base["left_boundary_gradient_profile"] = "0,0.005 1,0.005"
                base["right_boundary_gradient_profile"] = "0,0.005 1,0.005"
        else:
            base.update(
                upstream_left_fpl_gw_gradient=float(_safe("g_ul", 0.005)),
                upstream_right_fpl_gw_gradient=float(_safe("g_ur", 0.005)),
                downstream_left_fpl_gw_gradient=float(_safe("g_dl", 0.005)),
                downstream_right_fpl_gw_gradient=float(_safe("g_dr", 0.005)),
            )
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
        est = grid_estimate()                 # same red band that blocks Run — refuse up front
        if est and estimate.band(est["n_cells"]) == "red":
            ui.notification_show(estimate.band_message(est), type="error", duration=10)
            return
        stage.set("Building the 3D mesh…")
        _origin, _z0 = _scene_frame()          # shared z datum → vexag-safe layer alignment
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
        stage.set("")
        if mesh_task.status() == "error":
            try:
                mesh_task.result()
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"Mesh build failed: {e}", type="error", duration=8)
            return
        try:
            g = mesh_task.result()
        except Exception:  # noqa: BLE001
            return
        if g.get("cancelled"):
            return
        if g.get("error"):
            ui.notification_show(f"Mesh build failed: {g['error']}", type="error", duration=10)
            return
        mesh_geom.set(g)
        view_mode_v.set("3d")            # show the 3-D preview (the client defers the build until visible)
        await session.send_custom_message("hype_mesh", g)
        # Sync the aerial drape to the Basemaps → USGS Imagery checkbox (its own 3-D key), so a fresh
        # mesh honors the current basemap choice instead of always showing the imagery.
        await session.send_custom_message(
            "hype3d_vis", {"key": "basemap", "on": _eff_checked("base.imagery")})

    def _wse_path():
        """Resolve the WSE raster the engine will use: the surface-model result, the uploaded
        raster, or the DEM clipped to the drawn wetted-extent polygon. None if unavailable."""
        if wse_mode_v() == "model":
            res = ras_result()
            p = (res or {}).get("wse_for_gw")
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
                 "g_ul", "g_ur", "g_dl", "g_dr", "g_left_profile", "g_right_profile",
                 "g_qual_left", "g_qual_right", "g_ref_slope", "g_left_ctl", "g_right_ctl",
                 "usgs_region", "usgs_lat", "usgs_lon", "usgs_national",
                 "soil_policy", "use_soil_k", "sens_design",
                 "site_name", "site_analyst", "site_org", "site_date", "site_notes",
                 "ras_flow", "ras_slope", "ras_n", "ras_cell", "ras_hours", "ras_dt",
                 "ras_out_min", "kh", "kv", "porosity", "use_kzones", "kzone_kh", "kzone_kv",
                 "cell_size", "gw_mod_depth", "z", "pt_per_cell", "pt_min_mult",
                 "grid_wireframe",
                 "carve_bw", "carve_depth", "carve_slope", "hz_ppc", "hz_sample")
    _KEEP_SET = frozenset(_KEEP_IDS)

    @reactive.effect
    def _keep_inputs():
        for _iid in _KEEP_IDS:
            try:
                v = input[_iid]()
            except Exception:  # noqa: BLE001
                continue
            if v is None or v == _kept_seen.get(_iid, _MISSING):
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

    def _evt_btn(evt_id, label, cls):
        """Event-nonce button for panes that re-render themselves. input_action_button counters
        RESET to 0 on every pane rebind, and a rebind landing in the same server batch as the
        click's increment swallows the click (observed live on the flow panel). A nonce'd
        setInputValue with priority:'event' survives rebinds — the tree.js pattern."""
        return ui.tags.button(
            label, type="button", class_=f"btn {cls}",
            onclick=f"Shiny.setInputValue('{evt_id}', Date.now() + Math.random(), "
                    "{priority: 'event'})")

    # The lookup UI renders INLINE in the Water-surface pane (never in a modal): outputs
    # inside ui.modal wedge this app's session flush — panes are the proven dynamic surface.
    usgs_panel_open = reactive.value(False)

    def _usgs_section():
        """The flow-lookup section of the Water-surface pane (§5.1). Plain TagList — the pane
        itself re-renders on task-status / flow_lookup changes, so no nested outputs."""
        if not usgs_panel_open():
            return None
        lat0, lon0 = (_usgs_outlet_latlon() or (43.686, -72.237))
        head = ui.TagList(
            ui.tags.hr(),
            ui.p("USGS StreamStats — regional peak-flow statistics for the reach outlet. "
                 "National estimates are queried only when no regional discharge is usable, "
                 "or on request.", class_="hype-instr"),
            ui.div(
                ui.input_numeric("usgs_lat", "Outlet latitude",
                                 value=_keep("usgs_lat", round(lat0, 6)), step=0.0001),
                ui.input_numeric("usgs_lon", "Outlet longitude",
                                 value=_keep("usgs_lon", round(lon0, 6)), step=0.0001),
                ui.input_text("usgs_region", "Region (blank = auto)",
                              value=_keep("usgs_region", "")),
                class_="hype-field-row"),
            ui.input_checkbox("usgs_national",
                              "Also request national estimates for comparison",
                              value=bool(_keep("usgs_national", False))))
        if usgs_flow_task.status() == "running":
            return ui.TagList(head,
                              ui.div(ui.div(class_="hype-spinner"),
                                     ui.span("Contacting USGS StreamStats… watershed "
                                             "delineation can take ~30 s."), class_="hype-busy"),
                              ui.div(_evt_btn("usgs_cancel_evt", "Cancel fetch",
                                              "btn-sm btn-outline-danger"),
                                     class_="hype-actions"))
        body = [ui.div(_evt_btn("usgs_fetch_evt", "Fetch statistics", "btn-primary btn-sm"),
                       _evt_btn("usgs_close_evt", "Hide", "btn-sm btn-outline-secondary"),
                       class_="hype-actions")]
        fl = flow_lookup()
        if fl:
            bits = []
            if fl.get("selected_region"):
                bits.append(f"Region {fl['selected_region']}")
            if fl.get("basin_characteristics", {}).get("DRNAREA") is not None:
                bits.append(f"drainage area {fl['basin_characteristics']['DRNAREA']:g} mi²")
            if bits:
                body.append(ui.div(" · ".join(bits), class_="hype-instr"))
            warns = [w.get("message", "") for w in (fl.get("warnings") or [])]
            for w in warns[:4]:
                body.append(ui.div(w, class_="hype-warn"))
            cands = fl.get("candidates") or []
            choices = {}
            rows = []
            for c in cands:
                cfs, cms = c.get("value_cfs"), c.get("value_cms")
                recur = c.get("recurrence_years")
                flags = [f for f, on in (("national", c.get("is_national")),
                                         ("extrapolated", c.get("is_extrapolated")),
                                         ("not insertable", not c.get("insertable"))) if on]
                if c.get("insertable"):
                    label = f"{c.get('description') or c['id']} — {cfs:.1f} cfs"
                    if flags:
                        label += f"  [{', '.join(flags)}]"
                    choices[c["id"]] = label
                rows.append(ui.tags.tr(
                    ui.tags.td(c.get("description") or c.get("statistic_code") or c["id"]),
                    ui.tags.td(f"{cfs:.1f}" if isinstance(cfs, (int, float)) else "—"),
                    ui.tags.td(f"{cms:.3f}" if isinstance(cms, (int, float)) else "—"),
                    ui.tags.td(f"{recur:g}-yr" if isinstance(recur, (int, float)) else "—"),
                    ui.tags.td(", ".join(flags) or "ok")))
            if not cands:
                body.append(ui.div("No flow statistics returned for this point.",
                                   class_="hype-instr"))
            if choices:
                body.append(ui.input_radio_buttons("usgs_pick", "Select a statistic to insert",
                                                   choices))
                body.append(ui.div(_evt_btn("usgs_insert_evt", "Insert selected flow",
                                            "btn-success btn-sm"),
                                   class_="hype-actions"))
            elif cands:
                body.append(ui.div("No insertable discharge — see the warnings above.",
                                   class_="hype-instr"))
            if rows:
                body.append(ui.tags.table(
                    ui.tags.thead(ui.tags.tr(
                        ui.tags.th("Statistic"), ui.tags.th("cfs"), ui.tags.th("m³/s"),
                        ui.tags.th("Recurrence"), ui.tags.th("Status"))),
                    ui.tags.tbody(*rows), class_="table table-sm hype-flow-table"))
        return ui.TagList(head, *body)

    @reactive.effect
    def _usgs_open():
        if _clicked_dynamic("get_usgs_flow"):
            usgs_panel_open.set(True)

    @reactive.effect
    @reactive.event(input.usgs_close_evt)
    def _usgs_close():
        usgs_panel_open.set(False)

    @reactive.effect
    @reactive.event(input.usgs_fetch_evt)
    def _usgs_fetch():
        print("[usgs] fetch clicked")
        try:
            lat, lon = float(input.usgs_lat()), float(input.usgs_lon())
            region = (input.usgs_region() or "").strip().upper()
        except Exception:  # noqa: BLE001
            ui.notification_show("Enter a valid latitude and longitude.", type="warning")
            return
        if region and (not region.isalpha() or len(region) != 2):
            ui.notification_show("Enter the 2-letter state/region code (e.g. NH), or leave it "
                                 "blank to auto-detect.", type="warning")
            return
        if region:
            _kept["usgs_region"] = region
        try:
            want_nat = bool(input.usgs_national())
        except Exception:  # noqa: BLE001
            want_nat = False
        flow_lookup.set(None)                 # clear prior result while the new lookup runs
        print("[usgs] invoking lookup task")
        usgs_flow_task({"region": region, "lat": lat, "lon": lon, "want_national": want_nat,
                        "cache_dir": str(work_dir / "data_sources" / "usgs")})
        print("[usgs] task invoked (non-blocking)")

    @reactive.effect
    @reactive.event(input.usgs_cancel_evt)
    def _usgs_cancel_click():
        p = _usgs_proc.get("p")
        if p is not None and p.is_alive():
            _usgs_proc["cancelled"] = True
            p.kill()

    @reactive.effect
    def _usgs_done():
        if usgs_flow_task.status() in ("initial", "running"):
            return
        if usgs_flow_task.status() == "error":
            ui.notification_show("USGS lookup failed — check the point/region and try again.",
                                 type="error", duration=8)
            return
        try:
            res = usgs_flow_task.result()
        except Exception:  # noqa: BLE001
            return
        if isinstance(res, dict) and res.get("cancelled"):
            return
        if isinstance(res, dict) and res.get("error"):
            ui.notification_show(f"USGS lookup failed: {res['error']}", type="error", duration=8)
            return
        flow_lookup.set(res)

    @reactive.effect
    @reactive.event(input.usgs_insert_evt)
    def _usgs_insert():
        fl = flow_lookup()
        if not fl:
            ui.notification_show("Fetch flow statistics first.", type="warning")
            return
        try:
            pick = input.usgs_pick()
        except Exception:  # noqa: BLE001
            pick = None
        cand = next((c for c in (fl.get("candidates") or []) if c.get("id") == pick), None)
        if not cand or not isinstance(cand.get("value_cfs"), (int, float)):
            ui.notification_show("Select an insertable discharge statistic.", type="warning")
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
        usgs_panel_open.set(False)
        ui.notification_show(f"Inserted {cfs:g} cfs from USGS StreamStats.", duration=6)

    @render.ui
    def flow_source_note():
        fs = flow_source()
        if not fs:
            return None
        try:
            cur = float(input.ras_flow())
        except Exception:  # noqa: BLE001
            cur = None
        edited = cur is not None and abs(cur - float(fs.get("inserted_cfs", cur))) > 1e-6
        return ui.div(f"Source: {fs['source']}" + (" (edited since insert)" if edited else ""),
                      class_="hype-instr hype-flow-source")

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
            _soil_proc["p"] = None
            if error is not None:
                print(f"[soil] fetch failed:\n{error}")
                return {"error": error}
            return result or {"error": "The soils fetch stopped unexpectedly."}
        return await anyio.to_thread.run_sync(_work)

    def _show_soils_layer(snap):
        """Draw the clipped SSURGO polygons as the 'soils' decor layer (§6.3)."""
        try:
            polys = (snap or {}).get("polygons") or []
            feats = [{"type": "Feature", "geometry": p["geometry"],
                      "properties": {"mukey": p.get("mukey")}}
                     for p in polys if p.get("geometry")]
            _decor_show("soils", {"type": "FeatureCollection", "features": feats} if feats else None,
                        SOILS_STYLE)
        except Exception as e:  # noqa: BLE001
            print(f"[soils] layer draw failed: {e}")

    @reactive.effect
    @reactive.event(input.fetch_soils_evt)
    def _start_soil():
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
        soil_snapshot.set(None)
        soil_task(payload)

    @reactive.effect
    def _soil_done():
        if soil_task.status() in ("initial", "running"):
            return
        if soil_task.status() == "error":
            ui.notification_show("NRCS soils fetch failed.", type="error", duration=8)
            return
        try:
            res = soil_task.result()
        except Exception:  # noqa: BLE001
            return
        if isinstance(res, dict) and res.get("error"):
            tail = str(res["error"]).strip().splitlines()[-1][:160]
            ui.notification_show(f"NRCS soils fetch failed: {tail}", type="error", duration=10)
            return
        soil_snapshot.set(res)
        _show_soils_layer(res)
        ui.notification_show(f"Fetched {len(res.get('polygons') or [])} NRCS soil polygons.",
                             duration=5)

    def _pane_soils():
        snap = soil_snapshot()                    # subscribing read: re-render when a fetch lands
        header = ui.TagList(
            ui.p("Fetch SSURGO soils from NRCS Soil Data Access for the model domain, then review "
                 "map units, components, horizons and representative Ksat. Reviewing here does not "
                 "change the model K — that happens in the conductivity derivation.",
                 class_="hype-instr"),
            _evt_btn("fetch_soils_evt", "Fetch NRCS soils", "btn-outline-primary btn-sm"))
        if soil_task.status() == "running":
            return ui.TagList(header, ui.div("Querying NRCS Soil Data Access…",
                                             class_="hype-instr"))
        if not snap:
            return ui.TagList(header, ui.div("No soils fetched yet.", class_="hype-instr"))
        polys, mus = snap.get("polygons") or [], snap.get("map_units") or []
        cols = snap.get("source_columns_used") or {}
        cov = ui.div(ui.tags.strong(f"{len(polys)} polygons · {len(mus)} map units"), ui.br(),
                     "Columns used: " + (", ".join(f"{k}={v}" for k, v in cols.items()) or "—"),
                     class_="hype-instr")
        kv0 = float(_safe("kv", 1.0)) or 1.0
        aniso = float(_safe("kh", 10.0)) / kv0
        use_k = ui.TagList(
            ui.input_select("soil_policy", "Aggregation method (confirm before use)",
                            {"dominant": "Dominant component (largest %, its horizons)",
                             "weighted": "Weighted (component % · arithmetic KH / harmonic KV)"},
                            selected=str(_keep("soil_policy", "dominant"))),
            ui.input_checkbox("use_soil_k",
                              f"Use NRCS soils for model K (KV = Ksat × 0.0864; KH = {aniso:g} × KV; "
                              "manual K-zones still override)",
                              value=bool(_keep("use_soil_k", False))),
            ui.output_ui("soil_k_coverage_note"))
        choices = {mu["mukey"]: f"{mu.get('musym') or mu['mukey']} — {mu.get('name') or ''}"
                   for mu in mus}
        selector = ui.input_select("soil_mukey", "Map unit", choices) if choices else None
        return ui.TagList(header, cov, use_k, selector, ui.output_ui("soil_detail"))

    @render.ui
    def soil_k_coverage_note():
        _ = run_result()                          # re-render after each run
        p = work_dir / "summary" / "soil_k_coverage.json"
        if not p.is_file():
            return None
        try:
            import json as _json
            rep = _json.loads(p.read_text())
            pct = rep.get("volume_pct_by_origin") or {}
            return ui.div(f"Last run: soils covered {rep.get('domain_area_covered_pct', 0)}% of "
                          f"the domain — K volume {pct.get('derived', 0)}% derived, "
                          f"{pct.get('fallback', 0)}% global fallback.", class_="hype-instr")
        except Exception:  # noqa: BLE001
            return None

    @render.ui
    def soil_detail():
        snap = soil_snapshot()
        if not snap:
            return None
        mus = {mu["mukey"]: mu for mu in (snap.get("map_units") or [])}
        try:
            mukey = input.soil_mukey()
        except Exception:  # noqa: BLE001
            mukey = None
        mu = mus.get(mukey) or next(iter(mus.values()), None)
        if not mu:
            return None
        blocks = []
        for c in mu.get("components", []):
            hrows = []
            for h in c.get("horizons", []):
                ksat = h.get("ksat_um_s")
                kv = f"{ksat * 0.0864:.3f}" if isinstance(ksat, (int, float)) else "—"  # um/s→m/day
                hrows.append(ui.tags.tr(
                    ui.tags.td(h.get("name") or "—"),
                    ui.tags.td(f"{h.get('top_cm')}–{h.get('bottom_cm')} cm"),
                    ui.tags.td(f"{ksat:.2f}" if isinstance(ksat, (int, float)) else "—"),
                    ui.tags.td(kv), ui.tags.td(h.get("texture") or "—")))
            table = ui.tags.table(
                ui.tags.thead(ui.tags.tr(
                    ui.tags.th("Horizon"), ui.tags.th("Depth"), ui.tags.th("Ksat µm/s"),
                    ui.tags.th("KV m/day"), ui.tags.th("Texture"))),
                ui.tags.tbody(*hrows), class_="table table-sm hype-soil-table")
            bedrock = next((r for r in c.get("restrictions", []) if r.get("is_bedrock")), None)
            blocks.append(ui.div(
                ui.tags.strong(f"{c.get('name') or c.get('cokey')} — "
                               f"{c.get('comppct_r') or '?'}%"
                               + (" · major" if c.get("major") else "")),
                table,
                ui.div(f"Bedrock: {bedrock.get('kind')} at {bedrock.get('top_cm')} cm",
                       class_="hype-instr") if bedrock else None,
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
            site_name=(str(_safe("site_name", "")).strip() or None),
            analyst=(str(_safe("site_analyst", "")).strip() or None),
            organization=(str(_safe("site_org", "")).strip() or None),
            notes=(str(_safe("site_notes", "")).strip() or None),
            assessment_date=adate,
            upstream_point=up, downstream_point=dn, outlet=dn,
            reach_length_m=_reach_length_m())

    def _report_modal(res, paths):
        html = report_mod.render_html(res, app_version=APP_VERSION)
        return ui.modal(
            ui.div(
                ui.download_button("dl_report_html", "HTML", class_="btn-sm btn-outline-primary"),
                ui.download_button("dl_report_pdf", "PDF", class_="btn-sm btn-outline-primary"),
                ui.download_button("dl_report_csv", "Metrics CSV", class_="btn-sm btn-outline-primary"),
                ui.download_button("dl_report_json", "JSON", class_="btn-sm btn-outline-primary"),
                class_="hype-actions"),
            ui.tags.iframe(srcdoc=html, style="width:100%;height:60vh;border:1px solid #ccc"),
            title="Site Summary Report", size="xl", easy_close=True,
            footer=ui.modal_button("Close"))

    def _flux_metrics(hz_stats: dict, hz_dir):
        """(ExchangeAccounting in m3/s, transit_times, transit_weights, censored, transit_rows)
        from the §8.3 interface-pass artifacts. The model budget is m3/day; the canonical
        results + streamflow are m3/s, hence the /86400."""
        from hype_app.metrics import ExchangeAccounting
        DAY = 86400.0
        exchange = transit_t = transit_w = censored = None
        transit_rows = []
        acct = ((hz_stats or {}).get("flux") or {}).get("accounting") \
            if isinstance((hz_stats or {}).get("flux"), dict) else None
        if acct:
            exchange = ExchangeAccounting(
                total_downwelling=acct["total_downwelling"] / DAY,
                returning_hyporheic=acct["returning"] / DAY,
                losing_to_sides=acct["losing"] / DAY,
                unresolved=acct["unresolved"] / DAY)
            if acct.get("total_downwelling"):
                censored = acct["unresolved"] / acct["total_downwelling"]
        fx = hz_results.flux_arrays(hz_dir) if hz_dir else None
        if fx is not None:
            ret = fx["cls"] == 1
            if ret.any():
                transit_t = fx["time_days"][ret]
                transit_w = fx["weight"][ret]
            cls_names = {0: "unresolved", 1: "returning", 2: "losing"}
            transit_rows = [
                {"particle_id": int(i), "source_cell": int(fx["source_node"][i]),
                 "flow_weight": float(fx["weight"][i] / DAY),
                 "endpoint_class": cls_names.get(int(fx["cls"][i]), "unresolved"),
                 "transit_time_days": float(fx["time_days"][i]),
                 "termination": int(fx["status"][i])}
                for i in range(len(fx["cls"]))]
        return exchange, transit_t, transit_w, censored, transit_rows

    @reactive.effect
    @reactive.event(input.gen_report_evt)
    def _gen_report():
        hz, snap_dict = hz_result(), input_snapshot()
        if not hz or not snap_dict:
            ui.notification_show("Delineate the hyporheic zone first (its stats feed the report).",
                                 type="warning")
            return
        try:
            from hype_app.contracts import AssessmentInputSnapshot
            snap = AssessmentInputSnapshot.model_validate(snap_dict)
            # Overlay the CURRENT site metadata (name/analyst/date…) so fields filled after the
            # run still appear — descriptive metadata, never physics, so this doesn't reopen the
            # frozen inputs.
            try:
                snap = snap.model_copy(update={"site": _site_metadata()})
            except Exception:  # noqa: BLE001
                pass
            stats = (hz.get("stats") or {}).get("classes") or hz.get("stats") or {}
            hyp = stats.get("hyporheic") or {}
            vol, porosity = hyp.get("volume_m3"), snap.k.porosity
            exchange, transit_t, transit_w, censored, transit_rows = _flux_metrics(
                hz.get("stats") or {}, hz.get("hz_dir"))

            res = assess.build_results(
                snap, hz_stats=stats, streamflow_cms=snap.streamflow.value_cms,
                reach_length_m=_reach_length_m(), exchange=exchange,
                transit_times_days=transit_t, transit_weights=transit_w,
                mobile_pore_storage_m3=(float(vol) * float(porosity) if vol is not None else None),
                reference_area_m2=hyp.get("footprint_m2"),
                footprint_weighted_m2=hyp.get("footprint_m2"), porosity=porosity,
                censored_fraction=censored,
                app_version=APP_VERSION)
            results_model.set(res.model_dump(mode="json"))
            paths = report_mod.generate_report(res, work_dir / "report",
                                               transit_rows=transit_rows,
                                               app_version=APP_VERSION,
                                               model_version="MODFLOW 6 / MODPATH 7")
            report_paths.set(paths)
            ui.modal_show(_report_modal(res, paths))
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Report generation failed: {type(e).__name__}: {e}",
                                 type="error", duration=8)

    def _report_bytes(fmt):
        p = (report_paths() or {}).get(fmt)
        return Path(p).read_bytes() if p and Path(p).is_file() else b""

    @render.download(filename="site_report.html")
    def dl_report_html():
        yield _report_bytes("html")

    @render.download(filename="site_report.pdf")
    def dl_report_pdf():
        yield _report_bytes("pdf")

    @render.download(filename="site_metrics.csv")
    def dl_report_csv():
        yield _report_bytes("csv_metrics")

    @render.download(filename="assessment_results.json")
    def dl_report_json():
        yield _report_bytes("json")

    # ------------------------------------------------------------------
    # Gradient sensitivity (revision §10): sequential scenario execution + aggregation
    # ------------------------------------------------------------------
    sens_result = reactive.value(None)      # {"manifest": dict, "generator": str} after a run
    _sens_proc: dict = {}
    sens_log_lines: list = []
    sens_log_tick = reactive.value(0)

    @reactive.extended_task
    async def sens_task(payload: dict) -> dict:
        def _work():
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            p = ctx.Process(target=sens_run.child_run, args=(payload, q), daemon=True)
            _sens_proc["p"] = p
            p.start()
            result = error = None
            scen_recs = []
            while True:
                try:
                    kind, data = q.get(timeout=0.3)
                    if kind == "log":
                        sens_log_lines.append(data)
                    elif kind == "scenario":
                        scen_recs.append(data)
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
                    if kind == "scenario":
                        scen_recs.append(data)
                    elif kind == "result":
                        result = data
                    elif kind == "error":
                        error = data
                except _queue.Empty:
                    break
            p.join(timeout=5)
            cancelled = _sens_proc.pop("cancelled", False)
            _sens_proc["p"] = None
            if cancelled:
                return {"cancelled": True, "scenarios": scen_recs}
            if error is not None:
                return {"error": error, "scenarios": scen_recs}
            return result or {"scenarios": scen_recs}
        return await anyio.to_thread.run_sync(_work)

    def _scenario_metrics(stats: dict, hz_dir) -> dict:
        """Complete metric/HFCI dict for ONE scenario (§10.4 alternatives keep metrics only)."""
        from hype_app import hfci as hfci_mod
        from hype_app import metrics as metrics_mod
        classes = (stats or {}).get("classes") or {}
        hyp = classes.get("hyporheic") or {}
        porosity = float(_safe("porosity", 0.3))
        out: dict = {"volume_m3": hyp.get("volume_m3"), "footprint_m2": hyp.get("footprint_m2")}
        if hyp.get("volume_m3") is not None:
            out["pore_storage_m3"] = float(hyp["volume_m3"]) * porosity
        exchange, tt, tw, censored, _rows = _flux_metrics(stats, hz_dir)
        snap_d = input_snapshot() or {}
        q_cms = (snap_d.get("streamflow") or {}).get("value_cms")
        if q_cms is None:
            f = _safe("ras_flow", None)
            q_cms = float(f) * CFS_TO_CMS if f else None
        reach_len = _reach_length_m()
        exc_raw = None
        if exchange is not None and q_cms:
            conn = metrics_mod.connectivity(
                streamflow=q_cms, returning_hyporheic=exchange.returning_hyporheic,
                total_downwelling=exchange.total_downwelling,
                losing=exchange.losing_to_sides, unresolved=exchange.unresolved,
                reach_length_m=reach_len)
            if conn is not None:
                exc_raw = conn.excursions_per_mile
                out["excursions_per_mile"] = round(conn.excursions_per_mile, 6)
                out["returning_cms"] = round(exchange.returning_hyporheic, 8)
        storage_raw = None
        if out.get("pore_storage_m3") is not None and hyp.get("footprint_m2"):
            storage_raw = out["pore_storage_m3"] / float(hyp["footprint_m2"])
        proc_raw = None
        if tt is not None and tw is not None and len(tt):
            proc_raw = hfci_mod.processing_driver(tt, tw)
            out["rtd_median_days"] = round(metrics_mod.weighted_quantile(tt, tw, 0.5), 4)
        h = hfci_mod.compute_hfci(exchange_raw=exc_raw, storage_raw=storage_raw,
                                  processing_raw=proc_raw)
        out["hfci"] = h.hfci
        out["exchange_score"] = h.exchange.score
        out["storage_score"] = h.storage.score
        out["processing_score"] = h.processing.score
        return {k: v for k, v in out.items() if v is not None}

    def _sens_manifest_objects():
        """Rebuild the manifest object from the stored sens_result dict (None when absent)."""
        from hype_app.contracts import SensitivityScenarioManifest
        sr = sens_result()
        if not sr or not sr.get("manifest"):
            return None
        try:
            return SensitivityScenarioManifest.model_validate(sr["manifest"])
        except Exception:  # noqa: BLE001
            return None

    @reactive.effect
    @reactive.event(input.run_sens_evt)
    def _start_sens():
        build = _domain_build()
        if not (build and dem_path() and _wse_path()):
            ui.notification_show("Sensitivity needs the domain, terrain, and a water surface — "
                                 "same inputs as the groundwater run.", type="warning", duration=6)
            return
        try:
            gcfg = _gradient_config()
        except ValueError as ge:
            ui.notification_show(f"Fix the boundary gradients first: {ge}", type="warning",
                                 duration=8)
            return
        if gcfg is None:
            ui.notification_show("Sensitivity uses the structured/qualitative gradient modes — "
                                 "switch the boundary condition off the legacy corner mode.",
                                 type="warning", duration=8)
            return
        from hype_app import gradients as grad_mod
        from hype_app import sensitivity as sens_mod
        from hype_app.contracts import GeneratorType
        gen = GeneratorType(str(_safe("sens_design", "linked")))
        manifest = sens_mod.build_manifest(gcfg, gen)
        scen_payloads = [{
            "id": s.id, "label": s.label, "is_preferred": s.is_preferred,
            "left_profile": grad_mod.serialize_profile(s.gradients.left_controls),
            "right_profile": grad_mod.serialize_profile(s.gradients.right_controls)}
            for s in manifest.scenarios]
        try:
            crs = proj_crs()
            crs_id = crs.to_epsg() or crs.to_wkt()
            use_kz = bool(_safe("use_kzones", False))
            payload = {
                "crs": crs_id, "domain": build["domain"], "left": build["left"],
                "right": build["right"], "up": build["up"], "down": build["down"],
                "dem": active_dem(), "params": params(), "work_dir": str(work_dir),
                "wse_mode": "dem", "wse_path": _wse_path(),
                "wse_relief_thresh": float(_safe("wse_relief", 0.2)),
                "kzones": (kzone_feats() if use_kz else []),
                "kzone_kh": float(_safe("kzone_kh", 50.0)),
                "kzone_kv": float(_safe("kzone_kv", 5.0)),
                "scenarios": scen_payloads,
            }
            if bool(_safe("use_soil_k", False)) and soil_snapshot():
                from hype_app.soil_k import prepare_soil_k_payload
                kv0 = float(_safe("kv", 1.0)) or 1.0
                payload["soil_k"] = prepare_soil_k_payload(
                    soil_snapshot(), policy=str(_safe("soil_policy", "dominant")),
                    anisotropy_ratio=float(_safe("kh", 10.0)) / kv0,
                    fallback_kh=float(_safe("kh", 10.0)), fallback_kv=kv0)
            # resume (§10.3): skip scenarios already completed with an unchanged hash
            prior = _sens_manifest_objects()
            if prior is not None:
                done = {s.canonical_hash: s.id for s in prior.scenarios
                        if s.status.value == "completed"}
                payload["skip_ids"] = [s.id for s in manifest.scenarios
                                       if done.get(s.canonical_hash) == s.id]
                by_hash = {s.canonical_hash: s for s in prior.scenarios
                           if s.status.value == "completed"}
                for s in manifest.scenarios:      # carry completed results into the new manifest
                    old = by_hash.get(s.canonical_hash)
                    if old is not None and old.id == s.id:
                        s.status, s.metrics = old.status, old.metrics
                        s.artifact_paths = old.artifact_paths
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Couldn't start sensitivity: {e}", type="error", duration=8)
            return
        sens_log_lines.clear()
        sens_log_tick.set(0)
        sens_result.set({"manifest": manifest.model_dump(mode="json"),
                         "generator": manifest.generator.value, "running": True})
        sens_task(payload)

    @reactive.effect
    @reactive.event(input.cancel_sens_evt)
    def _sens_cancel():
        p = _sens_proc.get("p")
        if p is not None and p.is_alive():
            _sens_proc["cancelled"] = True
            p.kill()

    @reactive.effect
    def _sens_poll():
        if sens_task.status() != "running":
            return
        reactive.invalidate_later(0.5)
        sens_log_tick.set(len(sens_log_lines))

    @reactive.effect
    def _sens_done():
        if sens_task.status() in ("initial", "running"):
            return
        try:
            res = sens_task.result()
        except Exception:  # noqa: BLE001
            return
        sr = sens_result()
        if not sr or not sr.get("running"):
            return
        from hype_app.contracts import ScenarioStatus, SensitivityScenarioManifest
        manifest = SensitivityScenarioManifest.model_validate(sr["manifest"])
        recs = {r["id"]: r for r in (res.get("scenarios") or [])}
        for s in manifest.scenarios:
            rec = recs.get(s.id)
            if rec is None:
                continue
            if rec.get("ok"):
                s.status = ScenarioStatus.completed
                try:
                    s.metrics = _scenario_metrics(rec.get("stats") or {}, rec.get("hz_dir"))
                except Exception as me:  # noqa: BLE001
                    s.metrics = {}
                    print(f"[sens] metrics failed for {s.id}: {me}")
                s.artifact_paths = {"dir": rec.get("dir", "")}
            else:
                s.status = ScenarioStatus.failed
                s.error = (rec.get("error") or "")[-800:]
        manifest.cancelled = bool(res.get("cancelled"))
        out = {"manifest": manifest.model_dump(mode="json"),
               "generator": manifest.generator.value, "running": False}
        sens_result.set(out)
        try:
            import json as _json
            d = work_dir / "sensitivity"
            d.mkdir(parents=True, exist_ok=True)
            (d / "manifest.json").write_text(_json.dumps(out["manifest"], indent=2))
        except Exception:  # noqa: BLE001
            pass
        if res.get("error"):
            ui.notification_show("Sensitivity run failed — see the scenario log.",
                                 type="error", duration=8)
        elif res.get("cancelled"):
            ui.notification_show("Sensitivity cancelled — completed scenarios are kept.",
                                 duration=6)
        else:
            n_ok = sum(1 for s in manifest.scenarios if s.status.value == "completed")
            ui.notification_show(f"Sensitivity complete — {n_ok}/{len(manifest.scenarios)} "
                                 f"scenarios succeeded.", duration=6)

    def _pane_sens():
        running = sens_task.status() == "running"
        parts = [
            ui.div("Run the groundwater model over a set of gradient scenarios (lower / "
                   "preferred / upper at each control). Alternatives keep complete metrics; "
                   "only the main run keeps full display artifacts.", class_="hype-instr"),
            ui.input_select("sens_design", "Scenario design",
                            {"linked": "Linked lower / preferred / upper (3 scenarios)",
                             "crossed": "Left × right crossed (9 scenarios)",
                             "one_at_a_time": "One control at a time"},
                            selected=str(_keep("sens_design", "linked"))),
        ]
        if running:
            _ = sens_log_tick()
            tail = "\n".join(sens_log_lines[-14:])
            parts += [ui.div(ui.div(class_="hype-spinner"), ui.span("Running scenarios…"),
                             class_="hype-busy"),
                      ui.tags.pre(tail, class_="hype-log"),
                      ui.div(_evt_btn("cancel_sens_evt", "Cancel", "btn-sm btn-outline-danger"),
                             class_="hype-actions")]
            return ui.TagList(*parts)
        parts.append(ui.div(_evt_btn("run_sens_evt", "Run sensitivity scenarios", "btn-primary"),
                            class_="hype-actions"))
        manifest = _sens_manifest_objects()
        if manifest is not None and not (sens_result() or {}).get("running"):
            from hype_app import sensitivity as sens_mod
            done, failed = manifest.successful(), manifest.failed()
            parts.append(ui.div(
                ui.tags.strong(f"{len(done)} succeeded · {len(failed)} failed · "
                               f"design: {manifest.generator.value}"), class_="hype-instr"))
            rows = []
            for key, label in (("hfci", "HFCI (0–1)"),
                               ("excursions_per_mile", "Excursions / mile"),
                               ("volume_m3", "Hyporheic volume (m³)"),
                               ("pore_storage_m3", "Pore storage (m³)"),
                               ("rtd_median_days", "RTD median (days)")):
                agg = sens_mod.aggregate_metric(manifest.scenarios, key, manifest.preferred_id)
                if not agg:
                    continue
                rows.append(ui.tags.tr(
                    ui.tags.td(label),
                    ui.tags.td(report_mod.fmt(agg.get("preferred"))),
                    ui.tags.td(f"{report_mod.fmt(agg['min'])} ({agg['min_scenario']})"),
                    ui.tags.td(f"{report_mod.fmt(agg['max'])} ({agg['max_scenario']})"),
                    ui.tags.td(report_mod.fmt(agg["range"]))))
            if rows:
                parts.append(ui.tags.table(
                    ui.tags.thead(ui.tags.tr(ui.tags.th("Metric"), ui.tags.th("Preferred"),
                                             ui.tags.th("Min"), ui.tags.th("Max"),
                                             ui.tags.th("Range"))),
                    ui.tags.tbody(*rows), class_="table table-sm hype-sens-table"))
                dom = sens_mod.dominant_capacity_contributor(manifest.scenarios)
                if dom:
                    parts.append(ui.div(f"Dominant capacity contributor across scenarios: {dom}.",
                                        class_="hype-instr"))
            parts.append(ui.div("Ranges show sensitivity to the tested gradient assumptions — "
                                "not confidence intervals. Untested: K/soils, streamflow, "
                                "geometry, grid resolution, porosity.", class_="hype-warn"))
            for s in failed:
                parts.append(ui.div(f"{s.label}: failed", class_="hype-warn"))
        return ui.TagList(*parts)

    # ------------------------------------------------------------------
    # Structured / qualitative gradients (revision §7): config + reference slope
    # ------------------------------------------------------------------
    def _reference_slope():
        """ReferenceSlope with the §7.4 priority: modeled WSE raster → DEM drop → manual.
        Returns None when nothing usable (flat/adverse) — the UI then requires manual input."""
        from hype_app import gradients as grad_mod
        build = _domain_build()
        rr = ras_result()
        if build and rr and rr.get("wse_tif") and Path(rr["wse_tif"]).is_file():
            try:
                s = ras_engine.default_friction_slope(rr["wse_tif"], build["up"], build["down"])
                if s and s > 0:
                    return grad_mod.ReferenceSlope(value=float(s), source="wse_raster",
                                                   method="cap-line sample over reach distance")
            except Exception:  # noqa: BLE001
                pass
        s = ras_slope_default()
        if s and s > 0:
            return grad_mod.ReferenceSlope(value=float(s), source="dem_drop",
                                           method="cap-line sample over reach distance")
        manual = _safe("g_ref_slope", None)
        if manual and float(manual) > 0:
            return grad_mod.ReferenceSlope(value=float(manual), source="manual")
        return None

    def _gradient_config():
        """GradientBoundaryConfigV2 from the current UI (qualitative or structured modes).
        Returns None in legacy corner mode; raises ValueError with a user-facing message."""
        from hype_app import gradients as grad_mod
        from hype_app.contracts import (GradientBoundaryConfigV2, GradientQualitative, Side)
        bc = _safe("bc_mode", BC_QUAL)
        if bc == BC_QUAL:
            rs = _reference_slope()
            if rs is None:
                raise ValueError("No usable reference slope (flat or adverse reach) — enter a "
                                 "manual reference slope or use structured controls.")
            left = GradientQualitative(_safe("g_qual_left", "neutral"))
            right = GradientQualitative(_safe("g_qual_right", "neutral"))
            cfg = GradientBoundaryConfigV2.from_qualitative(
                left=left, right=right, reference_slope=rs)
            # sensitivity bounds = one category step either way (§10.1 qualitative default)
            def _with_bounds(controls, cat):
                from hype_app.contracts import QUALITATIVE_MULTIPLIER as QM
                lo_cat, hi_cat = grad_mod.qualitative_neighbors(cat)
                lo, hi = sorted((QM[lo_cat] * rs.value, QM[hi_cat] * rs.value))
                return [c.model_copy(update={"lower": lo, "upper": hi}) for c in controls]
            return cfg.model_copy(update={
                "left_controls": _with_bounds(cfg.left_controls, left),
                "right_controls": _with_bounds(cfg.right_controls, right)})
        if bc == BC_PROFILE:
            left = grad_mod.parse_control_lines(
                _safe("g_left_ctl", "0, 0.005\n1, 0.005"), Side.left)
            right = grad_mod.parse_control_lines(
                _safe("g_right_ctl", "0, 0.005\n1, 0.005"), Side.right)
            return GradientBoundaryConfigV2(mode="quantitative",
                                            left_controls=left, right_controls=right)
        return None                                   # legacy corner mode

    @render.ui
    def gradient_qual_preview():
        try:
            _ = input.g_qual_left(), input.g_qual_right()   # subscribe
        except Exception:  # noqa: BLE001
            pass
        rs = _reference_slope()
        if rs is None:
            return ui.TagList(
                ui.div("No usable reference slope from the water surface or DEM — enter one:",
                       class_="hype-warn"),
                ui.input_numeric("g_ref_slope", "Reference slope (m/m)",
                                 value=_keep("g_ref_slope", 0.005), min=0.0, step=0.001))
        try:
            cfg = _gradient_config()
            gl = cfg.left_controls[0].preferred
            gr = cfg.right_controls[0].preferred
            return ui.div(f"Reference slope {rs.value:.5f} m/m ({rs.source}) → left gradient "
                          f"{gl:+.5f}, right {gr:+.5f} m/m.", class_="hype-instr")
        except Exception as e:  # noqa: BLE001
            return ui.div(str(e), class_="hype-warn")

    @render.ui
    def gradient_ctl_check():
        from hype_app import gradients as grad_mod
        try:
            _ = input.g_left_ctl(), input.g_right_ctl()     # subscribe
        except Exception:  # noqa: BLE001
            pass
        try:
            cfg = _gradient_config()
        except ValueError as e:
            return ui.div(str(e), class_="hype-warn")
        if cfg is None:
            return None
        warns = grad_mod.validate_config(cfg)
        n = len(cfg.left_controls) + len(cfg.right_controls)
        bits = [ui.div(f"{n} controls parsed — heads anchor at each control "
                       "(head = WSE + gradient × distance) and interpolate between.",
                       class_="hype-instr")]
        bits += [ui.div(w.message, class_="hype-warn") for w in warns]
        return ui.TagList(*bits)

    @reactive.effect
    async def _start_surface():
        if not _clicked_dynamic("run_surface"):
            return
        build = _domain_build()
        if not (build and dem_path()):
            ui.notification_show("Need all four boundaries (closing into a domain) plus terrain "
                                 "before running the surface model.", type="warning", duration=6)
            return
        if not ras_engine.ras_available():
            ui.notification_show(RAS_UNAVAILABLE_MSG, type="error", duration=8)
            return
        cell = float(_safe("ras_cell", 10.0))
        est = ras_engine.estimate_cell_count(_domain_gdf_4326(), cell)
        _green, cap = ras_engine.cell_budget()
        if est > cap:
            need = cell * (est / cap) ** 0.5
            ui.notification_show(f"~{est:,} cells at {cell:g} m — over the {cap:,} limit. "
                                 f"Increase the cell size to ~{need:.0f} m.",
                                 type="error", duration=10)
            return
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
        await _cascade_clear("sw")      # re-running the water surface invalidates groundwater + results
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
    def _ras_extent_sync():
        # Owns the "Modeled extent" polygon (persists while a result exists — it's the water
        # surface the groundwater run consumes). Pure upsert: transitions no longer churn the
        # layer list, so the old force-fresh-per-step workaround is gone.
        if not _HAS_MAP:
            return
        ext = (ras_result() or {}).get("extent_feat")
        _upsert_geojson("Modeled extent", ext, WSE_STYLE)

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
        # Owns the "RAS mesh" overlay (Surface step only). Rasterized PNG, not vector —
        # thousands of face edges as SVG paths make Leaflet unusably slow. Also re-asserts
        # after a run completes (ras_result read) — the completion flush is exactly when the
        # client historically lost this layer.
        if not _HAS_MAP:
            return
        prev = ras_mesh_prev()
        ras_result()                               # re-run on run completion (see docstring)
        ov = (prev or {}).get("overlay")
        show = current_step() == STEP_SURFACE and prev and not prev.get("too_big") and ov
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
        if not ras_engine.ras_available():
            ui.notification_show(RAS_UNAVAILABLE_MSG, type="error", duration=8)
            return
        try:
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
        ras_log_tick.set(len(ras_log_lines))
        if "error" in res:
            ui.notification_show("Meshing failed: " + str(res["error"])[:300],
                                 type="error", duration=8)
            return
        ras_mesh_prev.set(res)                      # _ras_mesh_sync draws it
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
        _ras_overlays.clear()
        for nm in ("Modeled extent", "sw_depth", "sw_wse", "RAS mesh"):
            _set_layer(nm, None)

    def _drop_gw_artifacts():
        """Clear every groundwater product (grid preview, run, head results + their 2-D layers).
        The missing counterpart to _drop_ras_artifacts / _clear_hz_outputs."""
        mesh_geom.set(None)
        run_result.set(None)
        head_tifs.set([])
        head_rng.set(None)
        fp_stats.set(None)
        sel_pids.set(())
        _head_cache.clear()
        _contour_cache.clear()
        for nm in ("head", "grid", "wse_raster"):
            _set_layer(nm, None)

    def _drop_bnd_artifacts():
        """Clear the generated boundary geometry (the four lines + drawn wetted extent). The domain
        calc goes None automatically and the boundary map layers drop via their owner effects."""
        for sv in (up_feat, left_feat, right_feat, down_feat, wse_extent_feat):
            sv.set(None)
        origin_override.set(None)      # streambed origin derives from the (now-gone) upstream line

    async def _clear_3d(*keys):
        """Remove named 3-D layers (grid mesh / drapes) WITHOUT a full-scene reset: an empty lines3d
        payload routes to removeLayer3d(key), which is type-agnostic (mesh3d.js). Keeps terrain +
        basemap (unlike hype3d_clear, which _reset uses)."""
        for k in keys:
            await session.send_custom_message(
                "hype3d_layer", {"key": k, "kind": "lines3d", "polylines": []})

    async def _cascade_clear(stage, *, include_self=False):
        """Clear a pipeline stage's outputs and everything downstream. Order: bnd -> sw -> gw -> hz
        (hz = the head + hyporheic-zone results subtree). include_self=False clears STRICTLY
        downstream — used when the stage is being re-run and replaces its own output."""
        order = ["bnd", "sw", "gw", "hz"]
        todo = set(order[order.index(stage) + (0 if include_self else 1):])
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
        if est and estimate.band(est["n_cells"]) == "red":
            ui.notification_show(estimate.band_message(est), type="error", duration=10)
            return
        wse = _wse_path()
        if wse is None:
            ui.notification_show("No water surface yet — draw the wetted extent, run the Surface "
                                 "model, or upload a WSE raster.", type="warning", duration=6)
            return
        try:
            gradients_cfg = _gradient_config()   # None in legacy corner mode
        except ValueError as ge:
            ui.notification_show(f"Fix the boundary gradients first: {ge}",
                                 type="warning", duration=8)
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
        await _cascade_clear("gw")      # re-running groundwater invalidates the hyporheic results
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

    def _show_run_layers(res):
        """Head layers + model-grid + consumed-WSE raster from the on-disk run artifacts —
        shared by the run-completion handler and project restore. Returns (tifs, wov) for
        the caller's 3-D drapes."""
        tifs = results.head_rasters(work_dir, res)   # per-layer head color map + grid
        _head_cache.clear(); _contour_cache.clear()
        head_tifs.set(tifs)
        if tifs:
            head_rng.set(results.head_value_range(tifs))
            grid = results.grid_overlay(tifs)            # active cells only (≈ idomain)
            _set_layer("grid", ImageOverlay(url=grid["url"], bounds=grid["bounds"],
                                            name="Model grid", opacity=0.7) if grid else None)
            _render_head_layer(1)
        # Water-surface raster the model consumed, as its own toggleable layer. Prefer the
        # engine's domain-cropped copy; fall back to the raster resolved at launch.
        wse_tif = work_dir / "model" / "cropped_water_surface_raster.tif"
        wse_src = str(wse_tif) if wse_tif.exists() else _wse_used.get("path")
        wov = None
        if wse_src and Path(wse_src).exists():
            wrng = results.head_value_range([wse_src])
            wov = results.raster_overlay(wse_src, vmin=wrng[0], vmax=wrng[1], cmap="Blues")
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
        sel_pids.set(())                               # new run → clear any stale selection
        with reactive.isolate():                       # fresh GW result → its stale badge clears
            _stale_marks.set(frozenset(_stale_marks() - {"gw"}))
        _reset_res_layer_vis()                         # fresh layers → all visible, shadows dropped
        try:
            fp_stats.set(results.flowpath_stats(res, work_dir))
        except Exception:  # noqa: BLE001 — stats pane degrades to "n/a"; never block the map
            fp_stats.set(None)
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
                            mesh_task({
                                "run_ws": str(gwf_ws), "crs": proj_crs().to_wkt(),
                                "sides": ({k: build[k] for k in ("up", "left", "right", "down")}
                                          if build else None),
                                "scene_z0": z0,
                            })
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"Results computed; map render issue: {e}", duration=6)
        await _clear_hz_outputs()      # a new GW run rewrites the workspaces HZ reads
        _select("gw.res")
        ui.notification_show("Run complete.", duration=4)

    # ---- hyporheic-zone delineation task family (post-run; spawned child) ----
    _ALL_HZ_KEYS = tuple([f"hz_paths_{c}" for c in HZ_CLASSES]
                         + [f"hz_nodes_{c}_start" for c in HZ_CLASSES]
                         + [f"hz_nodes_{c}_end" for c in HZ_CLASSES]
                         + [f"hz_foot_{c}" for c in HZ_CLASSES]
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
        for cls in HZ_CLASSES:
            _set_layer(f"hz_paths_{cls}", None)
            _set_layer(f"hz_foot_{cls}", None)
            _set_layer(f"hz_nodes_{cls}_start", None)
            _set_layer(f"hz_nodes_{cls}_end", None)
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
    def _start_hz():
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
        if est > HZ_MAX_PARTICLES:
            ui.notification_show(
                f"~{est:,} particles at {ppc}/cell is over the {HZ_MAX_PARTICLES:,} limit — "
                f"use fewer particles per cell.", type="error", duration=10)
            return
        if est > 500_000:
            ui.notification_show(f"~{est:,} particles — this may take several minutes.",
                                 duration=7)
        try:
            crs = proj_crs()
            crs_id = crs.to_epsg() or crs.to_wkt()
            payload = {
                "work_dir": str(work_dir), "crs": crs_id,
                "left": build["left"], "right": build["right"],
                "up": build["up"], "down": build["down"],
                "params": {
                    "particles_per_cell": ppc,
                    "sample_per_class": int(_safe("hz_sample", 300)),
                    "porosity": float(_safe("porosity", 0.3)),
                    "modflow_bin_dir": runner.modflow_bin_dir(),
                    "hard_cap_particles": HZ_MAX_PARTICLES,
                },
            }
        except Exception as e:  # noqa: BLE001
            ui.notification_show(f"Could not start the analysis: {type(e).__name__}: {e}",
                                 type="error", duration=8)
            return
        hz_log_lines.clear()
        hz_log_tick.set(0)
        hz_step_v.set(0)
        hz_t0.set(time.monotonic())
        hz_elapsed.set(0)
        _select("gw.res.hz")
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

    async def _show_hz_layers(hz_dir):
        """Classed flow paths, entry/return dots, and footprints re-read from `hz_dir` — the
        map half of delineation completion, shared with project restore. Parks everything at
        creation and schedules the reveal (choreography notes inline). Returns the classed
        4326 gdf for the caller's 3-D payloads."""
        combined_4326 = None
        # the classed paths replace the monolithic forward set (2-D + 3-D)
        _set_layer("paths", None)
        _set_layer("paths_sel", None)
        sel_pids.set(())
        # visibility FIRST — classed paths on; only the hyporheic volume on by default (four
        # overlapping translucent shells would be unreadable). Fresh results also re-arm the
        # GROUP checks — an earlier group-uncheck must not leave the new delineation invisible
        # while the tree shows its leaves ticked.
        # Results defaults (revision §8.1): fresh delineation shows ONLY the hyporheic paths
        # + volume; losing/gaining/throughflow are opt-in via the tree.
        for cls in HZ_CLASSES:
            suf = ui_tree.HZ_CLASS_SUFFIX[cls]
            _check_state[f"gw.res.paths.{suf}"] = (cls == "hyporheic")
            _check_state[f"gw.res.hz.{suf}"] = (cls == "hyporheic")
        for gid in ("gw.res.hz", "gw.res.paths", "gw.res.hz.vols"):
            _check_state[gid] = True
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
                    lyr = GeoJSON(data=gj, style=HZ_PATH_STYLE[cls],
                                  hover_style=PATH_HOVER, name=f"{HZ_LABEL[cls]} paths")
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
            # everything above went into _layer_shadow (creation park) — reveal the checked
            # keys once the burst + 3-D churn settle; the reveal schedules its own verify.
            _schedule_hz_reveal()
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
        # 3-D per class: classed lines + translucent zone volumes
        origin, z0 = _scene_frame()
        if origin is not None:
            crs_s = _scene.get("crs")
            for cls in HZ_CLASSES:
                try:
                    sub = combined_4326
                    if sub is not None:
                        sub = sub[sub["hz_class"] == cls]
                    p3 = (scene.flowpaths_payload(sub, crs_s, origin, z0,
                                                  key=f"hz3d_paths_{cls}", color=HZ_COLORS[cls],
                                                  width=3) if sub is not None and len(sub) else None)
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
        st = (res.get("stats") or {}).get("classes", {}).get("hyporheic", {})
        vol = st.get("volume_m3", 0.0)
        counts = (res.get("stats") or {}).get("counts", {})
        pct = (100.0 * counts.get("n_classified", 0) / max(counts.get("n_seeds", 1), 1))
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
        origin, z0 = _scene_frame()         # the 3-D head drape follows the slider
        if origin is not None:
            try:
                ov = _head_cache.get(int(idx) - 1)
                await _send_3d(scene.drape_payload("head", ov, _scene.get("crs"), origin,
                                                   lift=0.6, opacity=0.8))
            except Exception:  # noqa: BLE001
                pass

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
        b = input.fp_select_box() or {}
        try:
            bx = _box(float(b["west"]), float(b["south"]), float(b["east"]), float(b["north"]))
        except (KeyError, TypeError, ValueError):
            return
        sub = gdf[gdf.intersects(bx)]       # crossing window: anything the box touches
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
                       "base.hydro": False,       # NHD hydrography overlay: opt-in, off by default
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
            else:
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
            else:
                _set_keys_visible(ui_tree.NODE_LAYERS.get(nid, ()), on)
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
    async def scene_terrain_task(dem_p: str, crs_wkt: str, origin: tuple, z0: float) -> dict:
        return await anyio.to_thread.run_sync(
            lambda: scene.terrain_payload(dem_p, crs_wkt, tuple(origin), float(z0)))

    @reactive.effect
    def _push_terrain_3d():
        p = active_dem()                   # carved terrain re-pushes the 3-D surface too
        if p is None or not _HAS_MAP:
            return
        origin, z0 = _scene_frame()
        if origin is None or scene_terrain_task.status() == "running":
            return
        sig = (p, origin[0], origin[1])
        if _scene.get("terrain_sig") == sig:
            return
        _scene["terrain_sig"] = sig
        crs = _scene["crs"]
        scene_terrain_task(p, crs.to_wkt() if hasattr(crs, "to_wkt") else str(crs),
                           tuple(origin), float(z0))

    @reactive.effect
    async def _terrain_3d_done():
        if scene_terrain_task.status() in ("initial", "running"):
            return
        try:
            payload = scene_terrain_task.result()
        except Exception:  # noqa: BLE001
            return
        await _send_3d(payload)

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
        if _task_state(run_task) == "initial" and run_result() is None:
            hidden.add("gw.run")               # the run row appears once a run first starts
        if run_result() is None:
            hidden.add("gw.res.hz")            # the Zone group appears after a GW run
            if _task_state(sens_task) == "initial" and sens_result() is None:
                hidden.add("gw.sens")          # sensitivity surfaces once a run/manifest exists
        if hz_result() is None:                # Flow-paths + Volumes populate on delineation
            hidden.update(("gw.res.paths", "gw.res.hz.vols"))   # children drop with their parent
        dimmed = {nid for nid in ui_tree.NODE_LAYERS
                  if checks.get(nid) and not _eff_checked(nid)}
        payload = ui_tree.build_tree_payload(
            selected=sel_node(), statuses=statuses, checks=checks,
            disabled=disabled, hidden=hidden, dimmed=dimmed)
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
            # selection consumed the same click (mirror/boundary/path picks stamp map_sel_ts).
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
                view_mode_v.set(v)
                if v == "2d":                  # back from 3-D: re-assert every layer vis and
                    _reapply_all_vis()         # sweep hidden hz ghosts (client-drift safety net)
                    await _sweep_hz([k for k in _hidden_keys
                                     if k.startswith("hz_") and _layers.get(k) is None])
        elif kind == "check":
            nid = evt.get("id")
            if nid not in ui_tree.NODE:
                return
            on = bool(evt.get("on"))
            _check_state[nid] = on             # raw intent; ancestors stay authoritative
            affected = _apply_check_effective(nid)
            if on and nid in ("base.imagery", "base.topo"):    # base maps act as a radio
                other = "base.topo" if nid == "base.imagery" else "base.imagery"
                _check_state[other] = False
                _apply_check_effective(other)
            for mid in affected:               # the same checkboxes drive the 3-D scene
                key3d = ui_tree.NODE_3D.get(mid)
                if key3d:
                    await session.send_custom_message(
                        "hype3d_vis", {"key": key3d, "on": _eff_checked(mid)})
            await _sweep_hz([k for mid in affected if not _eff_checked(mid)
                             for k in ui_tree.NODE_LAYERS.get(mid, ())
                             if k.startswith("hz_") and _layers.get(k) is None])

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
        return _props_shell(title, fn(), clear_btn=(nid in ("bnd", "sw", "gw")))

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
                 4: STEP_SURFACE, 5: STEP_K, 6: STEP_RESULTS}
        done = {1: reach_feat() is not None, 2: dem_path() is not None,
                3: _domain_build() is not None, 4: sw_done,
                5: run_result() is not None, 6: hz_result() is not None}
        running = {1: st.get("reach") == "running",
                   2: "running" in (st.get("terrain.dem"), st.get("terrain.chanmod")),
                   3: st.get("bnd") == "running",
                   4: "running" in (st.get("sw"), st.get("sw.mesh"), st.get("sw.wetted")),
                   5: "running" in (st.get("gw.mesh"), st.get("gw.run")),
                   6: st.get("gw.res.hz") == "running"}
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
        return ui.div(*parts[:-1], class_="hype-stagebar")

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

    @reactive.effect
    def _kz_buttons():
        # K-zone list management (same strict-increment guard as _clicked_dynamic so props-pane
        # re-render resets don't fire): Add → arm a guided polygon draw; Remove last / Clear all.
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
        if _clicked("kz_rmlast"):
            kz = list(kzone_feats())
            if kz:
                kz.pop()
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

    def _clear_reach_all():
        # One reset for BOTH reach modes — picks/linework, boundaries, K-zones, and the DEM
        # (everything downstream is sized from the reach, so it all goes together).
        _clear_auto_picks()
        up_feat.set(None); left_feat.set(None); right_feat.set(None); down_feat.set(None)
        kzone_feats.set([]); wse_extent_feat.set(None); bnd_slot.set(None)
        dem_path.set(None); dem_meta.set(None)   # also drop the downloaded DEM + its overlay
        dem_stretch_v.set(None); dem_lohi_v.set(None); _dem_shade_sig.clear()
        _set_layer("dem", None)
        _chain["dem"] = _chain["bnd"] = None       # a redrawn reach auto-chains afresh
        ui.notification_show("Reach cleared — terrain and boundaries were reset.", duration=4)

    @reactive.effect
    def _clear_points():
        if _clicked_dynamic("clear_points"):
            _clear_reach_all()

    @reactive.effect
    def _clear_draw():
        if _clicked_dynamic("clear_draw"):
            _clear_reach_all()

    @reactive.effect
    @reactive.event(input.nav_new)
    def _confirm_new_project():
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

    async def _reset_session_state():
        """Wipe the session back to first-run — memory, map layers, 3-D scene, AND the
        workspace dir. Shared by New project and Open project (which re-populates after)."""
        # Stop in-flight work first: a straggling done-handler must not repopulate the fresh
        # session, and Windows can't delete files a live child still holds open.
        _terminate_child()
        _kill_ras_proc()
        for h, k in ((_mesh_proc, "proc"), (_mesh3d_proc, "p"), (_hz_proc, "p"),
                     (_sens_proc, "p"), (_soil_proc, "p"), (_usgs_proc, "p")):
            p = h.get(k)
            if p is not None:
                try:
                    p.kill()
                except Exception:  # noqa: BLE001
                    pass
        _chain["dem"] = _chain["bnd"] = None
        up_feat.set(None); left_feat.set(None); right_feat.set(None); down_feat.set(None)
        kzone_feats.set([]); wse_extent_feat.set(None); bnd_slot.set(None)
        dem_path.set(None); dem_meta.set(None)
        dem_stretch_v.set(None); dem_lohi_v.set(None); _dem_shade_sig.clear()
        _drop_gw_artifacts()               # run result + head/grid/WSE layers + the grid preview
        stage.set("")
        log_lines.clear(); log_tick.set(0); step_v.set(0)
        hz_result.set(None); hz_gdf.set(None); hz_sel_pids.set(())
        hz_log_lines.clear(); hz_log_tick.set(0); hz_step_v.set(0)
        input_snapshot.set(None); flow_lookup.set(None); flow_source.set(None)
        soil_snapshot.set(None); soil_overrides.set([])
        results_model.set(None); report_paths.set(None)
        sens_result.set(None); sens_log_lines.clear(); sens_log_tick.set(0)
        _drop_ras_artifacts(); ras_log_lines.clear(); ras_log_tick.set(0)
        wse_mode_v.set("model")
        _wse_used.clear()
        pick_pts.set([]); reach_feat.set(None); auto_meta.set(None); last_click.set(None)
        reach_gen.set(0); dem_gen.set(0)
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
        _check_state.clear()               # …including the group toggles (back to defaults)
        _apply_check_effective("base")     # re-sync tile traits (user may have been on Topo)
        _map_ui.pop("dem_bounds", None)
        carve_active.set(False)
        carve_meta.set(None)
        _stale_marks.set(frozenset())
        _scene.clear()                     # 3-D frame re-anchors on the next DEM
        await session.send_custom_message("hype3d_clear", {})
        _wire_state.set(False)             # hype3d_clear resets S.wireframe client-side
        _kept.pop("grid_wireframe", None)
        ui.update_checkbox("grid_wireframe", value=False)
        view_mode_v.set("2d")
        # Disk too: leftovers from an opened/previous project must never leak into the next
        # project's Download (the bundler sweeps work_dir wholesale).
        for child in work_dir.iterdir():
            try:
                shutil.rmtree(child) if child.is_dir() else child.unlink()
            except OSError:
                pass
        _bump_vis()

    @reactive.effect
    async def _reset():
        # Modal confirm button is a dynamic input — strict-increment guard, house pattern.
        if not _clicked_dynamic("confirm_new_project"):
            return
        ui.modal_remove()
        await _reset_session_state()
        _select("reach")

    @reactive.effect
    @reactive.event(input.nav_help)
    def _help():
        ui.modal_show(ui.modal(
            ui.markdown(
                "**How to use**\n\n"
                "Follow the numbered stages across the top — each stage's settings open in the "
                "panel on the right. The **Layers** panel (left) shows/hides everything on the "
                "map; select any item there for its details.\n\n"
                "1. **Reach** — Auto (default): zoom in until the streams appear, then click the "
                "upstream and downstream points on one (≤ 1 mile apart). Or Manual: draw the "
                "centerline from upstream to downstream and enter the drainage area.\n"
                "2. **Terrain** — fetched automatically from USGS 3DEP once the reach is set. "
                "Re-fetch at another resolution, or carve a channel, under Terrain.\n"
                "3. **Boundaries** — generated automatically from the terrain (floodplain width "
                "× bankfull depth). Select a side — or click its line on the map — to edit it.\n"
                "4. **Water surface** — choose the source: run the **HEC-RAS 2025 2D** model, "
                "use the auto/drawn wetted extent, or upload a WSE raster.\n"
                "5. **Groundwater** — review the subsurface properties and model grid, set the "
                "boundary-condition gradients, then **Run groundwater model**.\n"
                "6. **Results** — explore hydraulic head, **delineate the hyporheic zone**, and "
                "click flow paths (or drag a box) for their statistics.\n\n"
                "The water-surface extent becomes the constant-head (CHD) top boundary — from "
                "the surface model's WSE when available, else the DEM elevations inside the "
                "drawn extent. Nothing is saved on the server — **Save** (top right) gives you "
                "a project file you can pick up later with **Open**; **Download project** is "
                "the full archive for GIS (it reopens the same way)."),
            title="Help", easy_close=True))

    @reactive.effect
    @reactive.event(input.nav_about)
    def _about():
        ui.modal_show(ui.modal(
            ui.markdown(
                f"**HYPE — Hyporheic Exchange Explorer**\n\nVersion {APP_VERSION}\n\n"
                "Build a reach-scale hyporheic-exchange model from a map: trace a reach, fetch "
                "terrain, model the water surface, run the groundwater model, and delineate "
                "the hyporheic zone.\n\n"
                "Terrain & streams: USGS 3DEP and NHD. Engines: HEC-RAS 2025 (2D surface), "
                "MODFLOW 6 + MODPATH 7 (groundwater and particle tracking)."),
            title="About", easy_close=True))

    # ---- downloads ----
    # A single "Download project" action (labeled header button) that captures the WHOLE
    # session — drawn reach + boundaries (serialized from the in-memory reactives), terrain,
    # the HEC-RAS surface model, and the MODFLOW 6 / MODPATH 7 groundwater model + results — into
    # one zip organized by pipeline stage (see hype_app/bundle.zip_workspace).
    def _has_workspace():
        # Anything worth downloading yet? (a reach, a DEM, a surface run, a GW run, or a HZ result)
        return bool(reach_feat() or dem_path() or ras_result() or run_result() or hz_result())

    @render.ui
    def dl_project():
        label = ui.TagList(ui.span(class_="hype-ic"), "Download project")
        if not _has_workspace():
            return ui.span(label, class_="hype-header-btn dim",
                           title="Nothing to download yet")
        return ui.download_link("dl_workspace", label, class_="hype-header-btn",
                                title="Download the project (.zip)")

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
                       "kh": float(_safe("kzone_kh", 50.0)),
                       "kv": float(_safe("kzone_kv", 5.0)),
                       "count": len(kzone_feats() or [])},
            "active_dem": (Path(str(dem_p)).name if dem_p else None),
            "carve_applied": bool(carve_active()),
        }

    # Workspace paths inside the manifest are stored behind this token, work_dir-relative —
    # a saved session's absolute temp paths are meaningless in the session that reopens it.
    _WS_TOKEN = "$WORKSPACE$"

    def _tokenize_paths(obj):
        """Deep-copy `obj` with every absolute path under work_dir rewritten to $WORKSPACE$/rel."""
        base = work_dir.resolve()

        def walk(v):
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [walk(x) for x in v]
            if isinstance(v, (str, Path)):
                s = str(v)
                try:
                    rp = Path(s)
                    if rp.is_absolute():
                        return _WS_TOKEN + "/" + rp.resolve().relative_to(base).as_posix()
                except (OSError, ValueError):
                    pass                    # not under the workspace — leave it alone
                return s
            return v
        return walk(obj)

    def _detokenize_paths(obj):
        """Inverse of _tokenize_paths, against THIS session's work_dir."""
        def walk(v):
            if isinstance(v, dict):
                return {k: walk(x) for k, x in v.items()}
            if isinstance(v, list):
                return [walk(x) for x in v]
            if isinstance(v, str) and v.startswith(_WS_TOKEN + "/"):
                return str(work_dir / v[len(_WS_TOKEN) + 1:])
            return v
        return walk(obj)

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
                "carve_active": carve_active(),
                "carve_meta": _tokenize_paths(carve_meta()),
                "dem_hs": dem_hs_v(), "dem_opacity": dem_opacity_v(),
                "dem_stretch": dem_stretch_v(),
                "origin_override": origin_override(),
                "wse_mode": wse_mode_v(),
                "ras_result": _tokenize_paths(ras_result()),
                "ras_opacity": ras_opacity_v(),
                "run_result": _tokenize_paths(run_result()),
                "input_snapshot": input_snapshot(),
                "flow_lookup": flow_lookup(), "flow_source": flow_source(),
                "soil_snapshot": soil_snapshot(), "soil_overrides": soil_overrides(),
                "results_model": results_model(),
                "sens_result": ({k: v for k, v in (sens_result() or {}).items()
                                 if k != "running"} or None),
                "head_layer": head_layer_v(), "head_opacity": head_opacity_v(),
                "head_contours": hd_contours_v(),
                "hz_result": _tokenize_paths(hz_result()),
                "wse_used": _tokenize_paths(_wse_used.get("path")),
                "stale_marks": sorted(_stale_marks()),
                "kept": dict(_kept),
                "check_state": dict(_check_state),
                "hidden_keys": sorted(_hidden_keys),
                "sel_node": sel_node(), "current_step": current_step(),
            }

    def _stream_bundle():
        """Build the archive (identical for Download and Save) and stream it in 1 MiB chunks —
        flat egress memory even at hundreds of MB."""
        vectors = {"reach": reach_feat(), "upstream": up_feat(), "left": left_feat(),
                   "right": right_feat(), "downstream": down_feat(), "domain": domain_feat(),
                   "wse_extent": wse_extent_feat(), "k_zones": kzone_feats()}
        path = bundle.zip_workspace(work_dir, vectors=vectors, params=params(),
                                    run_config=_run_config(), state=_project_state(),
                                    assessment_input=input_snapshot())
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):   # 1 MiB — flat egress memory
                    yield chunk
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    @render.download(filename=lambda: f"hype_project_{datetime.now():%Y%m%d_%H%M}.zip")
    def dl_workspace():
        yield from _stream_bundle()

    @render.ui
    def save_project():
        if not _has_workspace():
            return ui.span("Save", class_="hype-nav-dim", title="Nothing to save yet")
        return ui.download_link("dl_save", "Save",
                                title="Save a project file (.hype) — reopen it with Open to "
                                      "pick up where you left off")

    @render.download(filename=lambda: f"hype_project_{datetime.now():%Y%m%d_%H%M}.hype")
    def dl_save():
        yield from _stream_bundle()

    # ---- Open project (restore a saved .hype / downloaded project .zip) ----
    _open_seen: dict = {}      # last consumed upload datapath — the file input is re-created
    #                            per modal, so guard it the way _clicked_dynamic guards buttons

    def _busy_tasks():
        return [t for t in (snap_task, reach_task, dem_task, delineate_task, carve_task,
                            wse_task, ras_task, mesh_prev_task, mesh_task, run_task, hz_task,
                            sens_task, soil_task, usgs_flow_task)
                if _task_state(t) == "running"]

    @reactive.effect
    @reactive.event(input.nav_open)
    def _open_dialog():
        if _busy_tasks():
            ui.modal_show(ui.modal(
                ui.p("A task is still running — wait for it to finish (or cancel it) before "
                     "opening a project."),
                title="Open project", easy_close=True))
            return
        ui.modal_show(ui.modal(
            ui.p("Open a saved HYPE project — a .hype file from Save, or a project .zip from "
                 "Download project. This replaces everything in the current session."),
            ui.input_file("open_project", None, accept=[".hype", ".zip"], multiple=False,
                          button_label="Browse…", placeholder="No file selected", width="100%"),
            title="Open project", easy_close=True))

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
            ui.notification_show("A task is still running — wait for it to finish before "
                                 "opening a project.", type="warning", duration=6)
            return
        ui.modal_remove()
        ui.notification_show("Opening project…", duration=None, id="open_prog")
        try:
            await _apply_project(dp)
            ui.notification_show("Project opened — pick up where you left off.", duration=6)
        except bundle.ProjectError as e:
            ui.notification_show(str(e), type="error", duration=10)
        except Exception as e:  # noqa: BLE001 — a failed restore must never kill the session
            ui.notification_show(f"Couldn't open the project: {e}", type="error", duration=10)
        finally:
            ui.notification_remove("open_prog")

    async def _apply_project(zip_path):
        """Restore a saved session: wipe, extract, set every reactive in ONE flush with the
        non-reactive guards stamped, rebuild the raster layers, re-apply saved visibility,
        land on the saved selection. Everything else (tree, stage bar, decor vectors, panes,
        3-D terrain) rehydrates itself from the restored values after the flush."""
        await _reset_session_state()
        payload = bundle.restore_workspace(zip_path, work_dir)
        st = _detokenize_paths(payload.get("state") or {})
        vec = payload.get("vectors") or {}

        # geometry + provenance
        reach = vec.get("reach")
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
        kzone_feats.set(vec.get("k_zones") or [])
        gen_r = int(st.get("reach_gen") or (1 if reach else 0))
        gen_d = int(st.get("dem_gen") or 0)
        reach_gen.set(gen_r); dem_gen.set(gen_d)
        _chain["dem"] = gen_r; _chain["bnd"] = gen_d   # no auto re-fetch/regenerate on open

        # parameters — before anything reads _safe/_keep; registry values older than the
        # stamp are stale (previous session) and lose to these (see _safe)
        _kept.clear()
        _kept.update(st.get("kept") or {})
        _restore_stamp["t"] = time.monotonic()

        # terrain
        dem_p = work_dir / "inputs" / "dem.tif"
        dem_path.set(str(dem_p) if dem_p.is_file() else None)
        dem_meta.set(st.get("dem_meta"))
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

        # water surface
        if st.get("wse_mode") in ("model", "draw", "upload"):
            wse_mode_v.set(st["wse_mode"])
        ras_opacity_v.set(float(st.get("ras_opacity") or 0.7))
        rr = st.get("ras_result")
        if rr and rr.get("depth_tif") and Path(rr["depth_tif"]).is_file():
            _show_ras_overlays(rr)
            ras_result.set(rr)
        if st.get("wse_used"):
            _wse_used["path"] = st["wse_used"]

        # groundwater run + results (display prefs first — the builders read them)
        head_opacity_v.set(float(st.get("head_opacity") or 0.85))
        hd_contours_v.set(bool(st.get("head_contours", True)))
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
        _soil = st.get("soil_snapshot")
        soil_snapshot.set(_soil)
        soil_overrides.set(st.get("soil_overrides") or [])
        results_model.set(st.get("results_model"))
        sens_result.set(st.get("sens_result"))
        if _soil and _HAS_MAP:
            _show_soils_layer(_soil)
        rn = st.get("run_result")
        if rn:
            run_result.set(rn)
            try:
                fp_stats.set(results.flowpath_stats(rn, work_dir))
            except Exception:  # noqa: BLE001
                fp_stats.set(None)
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
            hz_result.set(hz)
            try:
                await _show_hz_layers(hz["hz_dir"])
            except Exception as e:  # noqa: BLE001
                ui.notification_show(f"Project opened; the hyporheic-zone layers couldn't "
                                     f"be drawn: {e}", type="warning", duration=8)
        _stale_marks.set(frozenset(st.get("stale_marks") or ()))

        # visibility + selection — saved intent overrides the fresh-result defaults the
        # rebuild helpers just applied
        _check_state.clear()
        _check_state.update(st.get("check_state") or {})
        hid = [k for k in (st.get("hidden_keys") or []) if isinstance(k, str)]
        if hid:
            _set_keys_visible(hid, False)
        _bump_vis()
        sel_node.set(None)                 # sets dedupe by identity: force a pane remount
        nid = st.get("sel_node")
        _select(nid if nid in ui_tree.NODE else "reach")
        try:
            b = _node_bounds("bnd") or _node_bounds("reach")
            if b:
                await session.send_custom_message("hype_fly", {"bounds": b})
        except Exception:  # noqa: BLE001
            pass

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
                           " — the direction sets which side is the left and right floodplain. "
                           "Then enter the drainage area.", class_="hype-instr"),
                    ui.input_numeric("manual_da", "Drainage area (km²)",
                                     value=_keep("manual_da", None), min=0.01, step=0.5),
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
            return ui.TagList(
                ui.input_select("dem_res", "DEM resolution",
                                {"auto": "Auto — finest (1 m where available)", "1": "1 m",
                                 "3": "3 m", "5": "5 m", "10": "10 m"},
                                selected=str(_keep("dem_res", "auto"))),
                ui.div(ui.input_action_button(
                    "fetch_dem",
                    "Re-fetch terrain" if dem_path() is not None else "Fetch terrain",
                    class_="btn-primary"), class_="hype-actions"),
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
                _next_hint("sw", "Next: Water surface →"),
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
            return ui.TagList(
                ui.div(hint, class_="hype-instr"), *rows,
                ui.div(ui.input_action_button("bnd_clear_side", "Clear & redraw",
                                              class_="btn-sm btn-outline-secondary"),
                       class_="hype-actions") if present else None,
            )
        return _pane

    def _pane_sw():
            with reactive.isolate():          # persisted prefill only; a live (subscribing) read
                wse_mode0 = wse_mode_v()      # here would re-render this pane on every radio change
                slope0 = ras_slope_default()
            return ui.TagList(
                # Canonical streamflow — always available (used by the RAS model AND, later, the
                # hyporheic connectivity metric) regardless of how the water surface is set (§5.1).
                ui.div(
                    ui.input_numeric("ras_flow", "Streamflow (cfs)", value=_keep("ras_flow", 100.0),
                                     min=0.1, step=10.0),
                    ui.input_action_button("get_usgs_flow", "Get USGS Flow",
                                           class_="btn-outline-primary btn-sm"),
                    ui.output_ui("flow_source_note"),
                    class_="hype-flow-input"),
                _usgs_section(),
                ui.input_radio_buttons(
                    "wse_mode", "Water surface (top boundary)",
                    {"model": "Modeled — HEC-RAS 2D (below)",
                     "draw": "Wetted extent (auto / drawn)",
                     "upload": "Upload a WSE raster"},
                    selected=(wse_mode0 or "model")),
                ui.panel_conditional(
                    "input.wse_mode === 'upload'",
                    ui.input_file("wse_upload", "WSE GeoTIFF", accept=[".tif", ".tiff"],
                                  multiple=False)),
                ui.panel_conditional(
                    "input.wse_mode === 'draw'",
                    ui.div("The wetted extent derives from the DEM automatically; select "
                           "Wetted extent in the tree to review or redraw it.",
                           class_="hype-instr")),
                # RAS setup renders ONLY in "model" mode — the parameters mean nothing for a
                # drawn/uploaded water surface and previously sat there as clutter.
                ui.panel_conditional(
                    "input.wse_mode === 'model'",
                    ui.div("Runs a HEC-RAS 2D model over the domain (steady inflow → "
                           "normal-depth outflow) using the streamflow above; its water surface "
                           "becomes the top boundary.", class_="hype-instr"),
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
                            ui.input_select(
                                "ras_engine_sel", "Engine",
                                {"swe": "HEC-RAS 2025 — 2D Shallow Water (explicit, CPU)"},
                                selected="swe"),
                            ui.div("The only RAS 2025 engine that runs on Posit Connect Cloud "
                                   "(Linux): Diffusion Wave needs Intel MKL (Windows-only) and "
                                   "the GPU solver needs CUDA.", class_="hype-instr"),
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
                ),
                _next_hint("gw", "Next: Groundwater →"),
            )

    def _pane_k():
            return ui.TagList(
                ui.div("Hydraulic conductivity. Optionally draw K-zone polygons.",
                       class_="hype-instr"),
                ui.input_numeric("kh", "Horizontal K (m/d)", value=_keep("kh", 10.0),
                                 min=0.0001, step=1.0),
                ui.input_numeric("kv", "Vertical K (m/d)", value=_keep("kv", 1.0),
                                 min=0.0001, step=0.5),
                ui.input_numeric("porosity", "Porosity", value=_keep("porosity", 0.3),
                                 min=0.01, max=0.6, step=0.05),
                ui.input_checkbox("use_kzones", "Use hydraulic-conductivity zones",
                                  value=bool(_keep("use_kzones", False))),
                ui.panel_conditional(
                    "input.use_kzones === true",
                    ui.div("Add one or more K-zone polygons (each uses these values); "
                           "double-click a zone to edit it.", class_="hype-instr"),
                    ui.input_numeric("kzone_kh", "Zone KH (m/d)", value=_keep("kzone_kh", 50.0),
                                     min=0.0001, step=1.0),
                    ui.input_numeric("kzone_kv", "Zone KV (m/d)", value=_keep("kzone_kv", 5.0),
                                     min=0.0001, step=0.5),
                    ui.div(
                        ui.input_action_button("kz_add", "Add K-zone", class_="btn-sm btn-primary"),
                        ui.input_action_button("kz_rmlast", "Remove last",
                                               class_="btn-sm btn-outline-secondary"),
                        ui.input_action_button("kz_clear", "Clear all",
                                               class_="btn-sm btn-outline-secondary"),
                        class_="hype-bnd-row"),
                    ui.output_ui("kzone_status")),
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
            return ui.TagList(
                ui.div("Model grid — the live estimate below keeps the run in bounds.",
                       class_="hype-instr"),
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
                            ui.tags.span(class_="hype-info-tip", title=_help),
                            class_="hype-field-inline"),
                        ui.div(f"{_up_txt}  ·  {_dn_txt}", class_="hype-instr"),
                    ),
                    ui.accordion_panel(
                        "3D display",
                        ui.input_checkbox("grid_wireframe",
                                          "Wireframe grid — see zone volumes inside the cells",
                                          value=bool(_keep("grid_wireframe", False))),
                        ui.div("Large grids render decimated in 3D; zone volumes and flow "
                               "paths stay full-resolution, so shell edges may not match "
                               "preview blocks.", class_="hype-instr"),
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
            elif wse_extent_feat() is not None:
                sw_ok, sw_detail = True, "wetted extent"
            elif wse_mode_v() == "upload" and bool(_safe("wse_upload", None)):
                sw_ok, sw_detail = True, "uploaded raster"
            else:
                sw_ok, sw_detail = False, "not set"
            kz_n = len(kzone_feats() or []) if bool(_safe("use_kzones", False)) else 0
            k_detail = (f"KH {float(_safe('kh', 10.0)):g} · KV {float(_safe('kv', 1.0)):g} m/d"
                        + (f" · {kz_n} zone{'' if kz_n == 1 else 's'}" if kz_n else ""))
            grid_detail = (f"{float(_safe('cell_size', 10.0)):g} m cells · "
                           f"{float(_safe('gw_mod_depth', 6.0)):g} m deep")
            return ui.TagList(
                ui.div("Everything the groundwater run needs, in one place — check the inputs, "
                       "set the boundary gradients, then run.", class_="hype-instr"),
                ui.div(
                    _hub_row(True, "Subsurface properties", k_detail, "gw.k"),
                    _hub_row(True, "Model grid", grid_detail, "gw.mesh"),
                    _hub_row(sw_ok, "Water surface", sw_detail, "sw"),
                    class_="hype-legend"),
                ui.input_select("bc_mode", "Boundary condition",
                                {BC_QUAL: "Qualitative (per side)",
                                 BC_PROFILE: "Structured controls (spatially varying)",
                                 BC_CORNER: "4 corner gradients (legacy)"},
                                selected=str(_keep("bc_mode", BC_QUAL))),
                ui.div("Sign convention: positive = floodplain head above the stream water "
                       "surface (gaining tendency); negative = below (losing). Units m/m.",
                       class_="hype-instr"),
                ui.panel_conditional(
                    f"input.bc_mode === '{BC_QUAL}'",
                    ui.div(
                        ui.input_select("g_qual_left", "Left floodplain", _QUAL_CHOICES,
                                        selected=str(_keep("g_qual_left", "neutral"))),
                        ui.input_select("g_qual_right", "Right floodplain", _QUAL_CHOICES,
                                        selected=str(_keep("g_qual_right", "neutral"))),
                        class_="hype-field-row"),
                    ui.output_ui("gradient_qual_preview")),
                ui.panel_conditional(
                    f"input.bc_mode === '{BC_PROFILE}'",
                    ui.input_text_area(
                        "g_left_ctl", "Left controls",
                        value=_keep("g_left_ctl", "0, 0.005\n1, 0.005"), rows=3),
                    ui.input_text_area(
                        "g_right_ctl", "Right controls",
                        value=_keep("g_right_ctl", "0, 0.005\n1, 0.005"), rows=3),
                    ui.div("One control per line: 'station, gradient[, lower, upper]'. Station "
                           "runs 0 (upstream) to 1 (downstream) and must include 0 and 1; the "
                           "optional lower/upper bounds feed the sensitivity scenarios.",
                           class_="hype-instr"),
                    ui.output_ui("gradient_ctl_check")),
                ui.panel_conditional(
                    f"input.bc_mode === '{BC_CORNER}'",
                    ui.div(
                        ui.input_numeric("g_ul", "Upstream-left gradient",
                                         value=_keep("g_ul", 0.005), step=0.001),
                        ui.input_numeric("g_ur", "Upstream-right gradient",
                                         value=_keep("g_ur", 0.005), step=0.001),
                        ui.input_numeric("g_dl", "Downstream-left gradient",
                                         value=_keep("g_dl", 0.005), step=0.001),
                        ui.input_numeric("g_dr", "Downstream-right gradient",
                                         value=_keep("g_dr", 0.005), step=0.001),
                        class_="hype-field-row"),
                    ui.div("Legacy method: heads interpolate linearly between the two corner "
                           "anchors of each side.", class_="hype-instr")),
                ui.accordion(
                    ui.accordion_panel(
                        "Particle tracking",
                        ui.input_select("pt_per_cell", "Particles per stream cell",
                                        {"1": "1 (default)", "4": "4 (2×2)", "9": "9 (3×3)"},
                                        selected=str(_keep("pt_per_cell", "1"))),
                        ui.input_numeric("pt_min_mult", "Min. flow-path length (× cell size)",
                                         value=_keep("pt_min_mult", 3.0), min=0.0, step=0.5),
                        ui.div(
                            ui.tags.ul(
                                ui.tags.li("Seeds a particle in every wetted stream cell, "
                                           "tracked through the bed."),
                                ui.tags.li("Paths shorter than the min length above aren't "
                                           "counted hyporheic (0 = keep all)."),
                            ),
                            class_="hype-instr"),
                    ),
                    open=False, id="gw_pt_acc",
                ),
                ui.div(ui.input_action_button("run_model", "Run groundwater model",
                                              class_="btn-primary"), class_="hype-actions"),
            )

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

    def _pane_paths():
            if hz_result() is None:
                return ui.div("Delineate the hyporheic zone (on the Zone node) to map the flow "
                              "paths, split into the four exchange classes.", class_="hype-instr")
            return ui.TagList(
                ui.div("Flow paths by exchange class. Each class's tree checkbox shows/hides its "
                       "paths and their entry (blue) / return (red) dots together. Click a path "
                       "or dot for its properties, or drag a box to select several.",
                       class_="hype-instr"),
                # Selection mode: plain buttons (no server round-trip) — www/flowpath_select.js
                # arms/disarms the crossing-window tool and keeps the active states in sync.
                ui.div(
                    ui.tags.button("Single", type="button",
                                   class_="hype-fpsel-single active",
                                   title="Click one flow path (or its entry/return dot) on the map"),
                    ui.tags.button("Box select", type="button",
                                   class_="hype-fpsel-multi",
                                   title="Drag a crossing window; every flow path it touches is "
                                         "selected"),
                    class_="hype-fpsel-row"),
                ui.output_ui("hz_sel_props"),
            )

    def _pane_results():
            parts = [
                ui.div("Groundwater run complete — the tree checkboxes show/hide each result "
                       "layer; select a layer for its display controls.", class_="hype-card ok"),
                ui.output_ui("result_summary"),
            ]
            if hz_result() is None:        # the one obvious next action after a run
                parts.append(_next_hint("gw.res.hz", "Delineate the hyporheic zone →",
                                        primary=True))
            else:                          # hyporheic zone delineated -> the site report is available
                parts.append(ui.accordion(
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
                    open=False, id="site_meta_acc"))
                parts.append(ui.div(_evt_btn("gen_report_evt", "Generate site report",
                                             "btn-primary btn-sm"),
                                    class_="hype-actions"))
            parts.append(ui.div("Results are in temporary storage — use ",
                                ui.tags.b("Download project"),
                                " in the header before you leave.", class_="hype-warn"))
            return ui.TagList(*parts)

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
                msg = ("The modeled wetted extent from the HEC-RAS surface run." if have else
                       "The wetted extent will come from the HEC-RAS surface model — run it "
                       "under Water surface.")
                return ui.div(msg, class_="hype-instr")
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
                          "pair; NHD Hydrography overlays the streams used for reach picking.",
                          class_="hype-instr")

    def _hz_swatch(cls):
        return ui.span(class_="hype-hz-swatch",
                       style=f"background:{HZ_COLORS[cls]};")

    def _pane_hz():
            running = hz_task.status() == "running"
            res = hz_result()
            ppc = int(_safe("hz_ppc", 1))
            est = _hz_particle_estimate(ppc)
            parts = [
                ui.div("Seed particles in every active cell and track them forward and "
                       "backward to classify hyporheic exchange, then delineate the zone "
                       "volumes. Runs on the existing groundwater solution — no re-solve.",
                       class_="hype-instr"),
                ui.input_select("hz_ppc", "Particles per cell",
                                {"1": "1 (fastest)", "3": "3", "6": "6", "9": "9 (finest)"},
                                selected=str(_safe("hz_ppc", "1"))),
                ui.div(f"≈ {est:,} particles" if est else
                       "Run the groundwater model to estimate the particle count.",
                       class_="hype-instr"),
                ui.accordion(
                    ui.accordion_panel(
                        "Advanced",
                        ui.input_numeric("hz_sample", "Displayed paths per class",
                                         value=int(_safe("hz_sample", 300)), min=50, max=1000,
                                         step=50),
                    ),
                    open=False, id="hz_adv_acc",
                ),
            ]
            if running:
                parts += [
                    ui.output_ui("hz_status"),
                    ui.tags.pre(ui.output_text("hz_log"), class_="hype-log"),
                    ui.div(ui.input_action_button("cancel_hz", "Cancel",
                                                  class_="btn-sm btn-outline-danger"),
                           class_="hype-actions"),
                ]
            else:
                parts.append(ui.div(ui.input_action_button(
                    "run_hz", "Re-delineate" if res else "Delineate hyporheic zone",
                    class_="btn-primary"), class_="hype-actions"))
            if res and not running:
                parts.append(ui.output_ui("hz_summary"))
            return ui.TagList(*parts)

    def _pane_vols():
            res = hz_result()
            if res is None:
                return ui.div("Delineate the hyporheic zone (on the Zone node) to compute the "
                              "class volumes.", class_="hype-instr")
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
            res = hz_result()
            if res is None:
                return ui.div("Delineate the hyporheic zone (on the Zone node) to populate the "
                              "flow classes.", class_="hype-instr")
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
            rows.append(ui.output_ui("hz_sel_props"))
            return ui.TagList(
                ui.div("Click a path on the map for its properties; the tree checkbox toggles "
                       "the class.", class_="hype-instr"),
                *rows,
            )
        return _pane

    def _pane_hz_vol(cls):
        def _pane():
            res = hz_result()
            if res is None:
                return ui.div("Delineate the hyporheic zone to compute this volume.",
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

    def _next_hint(nid, label, primary=False):
        """A guidance chip advancing the selection — plain button, delegated via tree.js
        (data-jump), so it carries none of the dynamic-input remount hazards. `primary`
        renders it filled (the one obvious next action, e.g. run-complete → Delineate)."""
        return ui.div(ui.tags.button(label, type="button",
                                     class_="hype-jump primary" if primary else "hype-jump",
                                     **{"data-jump": nid}), class_="hype-actions")

    # node id -> pane builder (the dispatch table for the right properties panel)
    PANE_FOR_NODE = {
        "reach": _pane_reach,
        "terrain": _pane_dem, "terrain.dem": _pane_dem, "terrain.chanmod": _pane_chanmod,
        "bnd": _pane_boundaries,
        "bnd.up": _pane_bnd_side("up", "Upstream", UP_STYLE["color"]),
        "bnd.left": _pane_bnd_side("left", "Left floodplain", LEFT_STYLE["color"]),
        "bnd.right": _pane_bnd_side("right", "Right floodplain", RIGHT_STYLE["color"]),
        "bnd.down": _pane_bnd_side("down", "Downstream", DOWN_STYLE["color"]),
        "sw": _pane_sw, "sw.mesh": _pane_sw, "sw.wetted": _pane_wetted,
        "sw.wse": _pane_sw_raster("wse"), "sw.depth": _pane_sw_raster("depth"),
        "gw": _pane_gw, "gw.k": _pane_k, "gw.soils": _pane_soils,
        "gw.mesh": _pane_mesh, "gw.run": _pane_run, "gw.sens": _pane_sens,
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
        "gw.soils": (lambda: _domain_build() is not None,
                     "Generate the four boundaries first.", "bnd", "Go to Boundaries →"),
        "gw.mesh": (lambda: _domain_build() is not None,
                    "Generate the four boundaries first.", "bnd", "Go to Boundaries →"),
        "gw.res": (lambda: run_result() is not None,
                   "Run the groundwater model first.", "gw", "Go to Groundwater →"),
        "gw.res.head": (lambda: run_result() is not None,
                        "Run the groundwater model first.", "gw", "Go to Groundwater →"),
        "gw.res.hz": (lambda: run_result() is not None,
                      "Run the groundwater model first — the analysis reuses its solution.",
                      "gw", "Go to Groundwater →"),
    }
    for _bd in ("bnd.up", "bnd.left", "bnd.right", "bnd.down"):
        PREREQS[_bd] = PREREQS["bnd"]
    # the Flow-paths / Volumes groups and their class rows need the analysis to have run
    for _hzc in ("gw.res.paths", "gw.res.paths.hyp", "gw.res.paths.los", "gw.res.paths.gain",
                 "gw.res.paths.thru", "gw.res.hz.vols", "gw.res.hz.hyp", "gw.res.hz.los",
                 "gw.res.hz.gain", "gw.res.hz.thru"):
        PREREQS[_hzc] = (lambda: hz_result() is not None,
                         "Delineate the hyporheic zone first.", "gw.res.hz",
                         "Go to Zone →")

    _ZOOM_ICON = ui.HTML(
        '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="2.4" stroke-linecap="round" aria-hidden="true">'
        '<path d="M4 9V5.5A1.5 1.5 0 0 1 5.5 4H9M15 4h3.5A1.5 1.5 0 0 1 20 5.5V9'
        'M20 15v3.5a1.5 1.5 0 0 1-1.5 1.5H15M9 20H5.5A1.5 1.5 0 0 1 4 18.5V15"/>'
        '<circle cx="12" cy="12" r="3"/></svg>')

    def _props_shell(title, *body, clear_btn=False, chrome=True):
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
        return ui.TagList(
            ui.div(*head, class_="hype-props-head"),
            ui.div(*body, class_="hype-props-body"),
        )

    def _pane_welcome():
        # First-run "home" card — shown while nothing is selected and no work exists yet.
        return _props_shell(
            "Get started",
            ui.div("Build a hyporheic-exchange model in three moves:",
                   class_="hype-welcome-note"),
            ui.tags.ol(
                ui.tags.li(ui.span("1", class_="hype-welcome-num"),
                           ui.span("Define your reach — search a place, then pick two points "
                                   "on a stream (or draw the centerline).")),
                ui.tags.li(ui.span("2", class_="hype-welcome-num"),
                           ui.span("Terrain and boundaries generate automatically — review "
                                   "and edit them as needed.")),
                ui.tags.li(ui.span("3", class_="hype-welcome-num"),
                           ui.span("Run the surface and groundwater models, then delineate "
                                   "the hyporheic zone.")),
                class_="hype-welcome-steps"),
            ui.div("Work isn't saved on the server — use Download project (top right) before "
                   "you leave.", class_="hype-welcome-note"),
            _next_hint("reach", "Start — define your reach →"),
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
                   "Drainage area entered" if da_ok else "Enter the drainage area",
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
        # Warn when the four boundaries don't meet at a corner. The derived domain still force-closes
        # for the model run, but a big gap means the user's lines are disconnected — guide them to fix
        # it. (Snapping auto-connects near endpoints; this catches the ones too far apart to snap.)
        if not _HAS_MAP or current_step() != STEP_BOUNDARIES:
            return None
        gap = geometry.corner_gaps_m(up_feat(), left_feat(), right_feat(), down_feat())
        if gap is None or gap <= 25.0:
            return None
        return ui.div(
            ui.div(f"Boundaries don't meet at a corner (gap ≈ {gap:.0f} m). Drag an endpoint onto "
                   "the neighbouring line to connect them, or:"),
            ui.input_action_button("snap_corners", "Snap corners together", class_="hype-warn-btn"),
            class_="hype-warn")

    @render.ui
    def kzone_status():
        kn = len(kzone_feats())
        if kz_adding():
            return ui.p("Drawing a K-zone — click on the map to place vertices.",
                        class_="hype-chk")
        if kn:
            return ui.p(ui.span(class_="hype-st st-done"),
                        f"{kn} K-zone{'' if kn == 1 else 's'} drawn — double-click one to edit.",
                        class_="hype-chk ok")
        return ui.p("No K-zones yet — click Add K-zone.", class_="hype-chk")

    @render.ui
    def dem_status():
        if dem_path() is None:
            return None
        m = dem_meta() or {}
        res, src = m.get("resolution_m"), m.get("source", "USGS 3DEP")
        tag = f"{res} m ({src})" if res else src
        try:
            s = dem.dem_summary(dem_path())
            return ui.p(ui.span(class_="hype-st st-done"),
                        f"Terrain fetched — {tag} · {s['width']} × {s['height']} px · "
                        f"{s['min']:.1f}–{s['max']:.1f} m", class_="hype-chk ok")
        except Exception:  # noqa: BLE001
            return ui.p(ui.span(class_="hype-st st-done"), f"Terrain fetched — {tag}",
                        class_="hype-chk ok")

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
        return ui.TagList(
            ui.div(facts, class_="hype-chk"),
            ui.div(estimate.band_message(est),
                   class_=f"hype-estimate {estimate.band(est['n_cells'])}"))

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
        msg = (f"{lead} at {cell:g} m — "
               + {"green": "quick run.", "amber": "will take a while on this server.",
                  "red": f"over the {cap:,}-cell limit; increase the cell size."}[band])
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
        if meshing:
            mesh_row = ui.div(ui.div(class_="hype-spinner"),
                              ui.span("Meshing…", class_="hype-run-label"),
                              class_="hype-run-head")
        else:
            mesh_row = ui.div(ui.input_action_button(
                "ras_mesh_btn", "Preview mesh", class_="btn-sm btn-outline-secondary"),
                class_="hype-actions")
        parts = [
            mesh_row,
            ui.div(ui.input_action_button("run_surface", "Run surface model",
                                          class_="btn-primary",
                                          disabled=meshing), class_="hype-actions"),
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
        extras = []
        if g.get("boundaries"):
            extras.append("boundary lines labeled")
        if g.get("basemap"):
            extras.append("aerial drape on top (toggle USGS Imagery; opacity slider in the 3D toolbar)")
        tail = (" · " + ", ".join(extras)) if extras else ""
        return ui.p(ui.span(class_="hype-st st-done"),
                    f"{g.get('nActiveFull', 0):,} active cells{note} — drag to orbit, "
                    f"middle/right-drag to pan, slider to slice{tail}.", class_="hype-chk ok")

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
        res = hz_result()
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
        return ui.div(ui.div("Particles by class", class_="hype-instr"), *rows,
                      ui.div("Select a class or volume node for its statistics.",
                             class_="hype-instr"))

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

    # Flow-path metric rows: (stats column, single-path label, short distribution label).
    # Units are the pipeline's — metric (the engine's "_ft" column names are labels only; the
    # head legend already says m).
    _FP_ROWS = [("length", "Path length (m)", "Length (m)"),
                ("horiz", "Horizontal length (m)", "Horiz. (m)"),
                ("depth", "Max depth below start (m)", "Depth (m)"),
                ("rtime_d", "Residence time (days)", "Res. time (d)"),
                ("vel", "Mean velocity (m/day)", "Vel. (m/d)")]
    _FP_EXTRA = [("head_start", "Starting hydraulic head (m)"),
                 ("head_end", "Ending hydraulic head (m)"),
                 ("hyd_grad", "Hydraulic gradient (–)")]

    def _fp_fmt(v, key: str = "") -> str:
        import math
        try:
            f = float(v)
        except (TypeError, ValueError):
            return "n/a"
        if not math.isfinite(f):
            return "n/a"
        return f"{f:,.4f}" if key == "hyd_grad" else f"{f:,.2f}"

    @render.ui
    def fp_props():
        pids = sel_pids()
        stats = fp_stats()
        if not pids or stats is None:
            return None
        have = [p for p in pids if p in stats.index]
        if not have:
            return None
        if len(have) == 1:
            pid = have[0]
            row = stats.loc[pid]
            trs = [ui.tags.tr(ui.tags.td(lbl), ui.tags.td(_fp_fmt(row.get(k), k)))
                   for k, lbl, _s in _FP_ROWS]
            trs += [ui.tags.tr(ui.tags.td(lbl), ui.tags.td(_fp_fmt(row.get(k), k)))
                    for k, lbl in _FP_EXTRA]
            body = [ui.div(f"Flow path #{pid}", class_="hype-props-title"),
                    ui.tags.table(ui.tags.tbody(*trs), class_="hype-props-table")]
        else:
            sub = stats.loc[have]
            trs = [ui.tags.tr(ui.tags.th(""), *(ui.tags.th(h) for h in
                                                ("Min", "Mean", "Median", "Max")))]
            for k, _lbl, short in _FP_ROWS:
                col = sub[k]
                trs.append(ui.tags.tr(ui.tags.td(short),
                                      ui.tags.td(_fp_fmt(col.min(), k)),
                                      ui.tags.td(_fp_fmt(col.mean(), k)),
                                      ui.tags.td(_fp_fmt(col.median(), k)),
                                      ui.tags.td(_fp_fmt(col.max(), k))))
            body = [ui.div(f"{len(have)} flow paths selected", class_="hype-props-title"),
                    ui.tags.table(ui.tags.tbody(*trs), class_="hype-props-table")]
            for k, lbl, unit in (("rtime_d", "Residence time", "days"),
                                 ("length", "Path length", "m")):
                try:
                    uri = results.hist_datauri(sub[k].tolist(), label=lbl, unit=unit)
                except Exception:  # noqa: BLE001
                    uri = None
                if uri:
                    body.append(ui.img(src=uri, class_="hype-props-hist"))
            with reactive.isolate():
                shown = fp_gdf()
                if results.flowpath_downsampled(run_result() or {}, shown):
                    body.append(ui.div(f"Selection covers the {len(shown):,} paths shown "
                                       "(display down-sampled).", class_="hype-props-note"))
        return ui.div(*body,
                      ui.div(ui.input_action_button("fp_clear", "Clear selection",
                                                    class_="btn-sm btn-outline-secondary"),
                             class_="hype-props-actions"),
                      class_="hype-props")

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
        if not _HAS_MAP or step not in (STEP_REACH, STEP_BOUNDARIES, STEP_K, STEP_RESULTS):
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
