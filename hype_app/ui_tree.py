"""Pure model for the layer tree (left panel) — NO Shiny imports, no reactives.

The tree is a FIXED hierarchy (hype-app has one reach, one DEM, four boundary sides, one
surface-water model, one groundwater model), so the model is a flat ordered list with parent
links. app.py gathers live state (statuses, checkbox values, reachability) and calls
build_tree_payload(); www/tree.js renders/reconciles the DOM. The ONE dynamic exception is
per-session feature rows (user Map layers) passed through build_tree_payload(extra_rows=...):
NODES itself is module-global and shared across sessions, so per-user rows must never be
appended here — they ride the payload only.

Node ids are dotted paths ("bnd.left", "gw.res.paths"). NODE_STEP maps each node onto the
wizard step whose machinery drives it — BEHAVIORAL, not hierarchical (e.g. "sw.wetted" maps
to the *boundaries* step because the wetted-extent polygon is edited through the boundary
slot machinery).
"""
from __future__ import annotations

# Step keys — must match the STEP_* constants in app.py (stable strings).
_REACH, _DEM, _BOUNDARIES, _SURFACE, _K, _MESH, _RUN, _RESULTS, _REPORT = (
    "reach", "dem", "boundaries", "surface", "k", "mesh", "run", "results", "report")

# Display order = list order. "check" nodes carry a visibility checkbox controlling the
# _layers keys in "layers". Checkbox semantics CASCADE: a node's layers are visible only when
# its own box AND every checkbox ancestor's box are ticked (the parent toggle overrides the
# children without erasing their state) — see app.py `_eff_checked`/`_apply_check_effective`.
NODES: list[dict] = [
    # Project: identity/info node (name, location, units, save actions) — no checkbox,
    # no map layers, never gated. Sits above the workflow nodes in both run modes.
    {"id": "project", "label": "Project", "parent": None, "group": False,
     "check": False, "layers": ()},
    {"id": "reach", "label": "Reach centerline", "parent": None, "group": False,
     "check": True, "layers": ("Reach",)},
    {"id": "terrain", "label": "Terrain", "parent": None, "group": True,
     "check": True, "layers": ()},
    {"id": "terrain.dem", "label": "DEM", "parent": "terrain", "group": False,
     "check": True, "layers": ("dem",)},
    {"id": "terrain.chanmod", "label": "Channel modification", "parent": "terrain",
     "group": False, "check": True, "layers": ("dem_carve",)},
    {"id": "bnd", "label": "Boundaries", "parent": None, "group": True,
     "check": True, "layers": ("Domain", "lbl_up", "lbl_left", "lbl_right", "lbl_down",
                               "lbl_wse")},
    {"id": "bnd.up", "label": "Upstream", "parent": "bnd", "group": False,
     "check": True, "layers": ("Upstream boundary",)},
    {"id": "bnd.left", "label": "Left floodplain", "parent": "bnd", "group": False,
     "check": True, "layers": ("Left boundary",)},
    {"id": "bnd.right", "label": "Right floodplain", "parent": "bnd", "group": False,
     "check": True, "layers": ("Right boundary",)},
    {"id": "bnd.down", "label": "Downstream", "parent": "bnd", "group": False,
     "check": True, "layers": ("Downstream boundary",)},
    {"id": "sw", "label": "Surface Water Modeling", "parent": None, "group": True,
     "check": True, "layers": ()},
    {"id": "sw.mesh", "label": "2D mesh", "parent": "sw", "group": False,
     "check": True, "layers": ("RAS mesh",)},
    {"id": "sw.wse", "label": "Water surface (raster)", "parent": "sw", "group": False,
     "check": True, "layers": ("wse_raster", "sw_wse")},   # wse_raster = today's consumed-WSE
     #                                                        overlay; sw_wse arrives at step 7
    {"id": "sw.depth", "label": "Depth (raster)", "parent": "sw", "group": False,
     "check": True, "layers": ("sw_depth",)},
    {"id": "sw.wetted", "label": "Wetted extent", "parent": "sw", "group": False,
     "check": True, "layers": ("Water-surface extent", "Modeled extent", "Removed pools")},
    {"id": "gw", "label": "Groundwater Modeling", "parent": None, "group": True,
     "check": True, "layers": ()},
    {"id": "gw.k", "label": "Subsurface properties", "parent": "gw", "group": False,
     "check": True, "layers": ("K-zones",)},
    # NRCS soils (SSURGO) review lives in a modal off the Subsurface-properties pane since
    # 2026-07-16 — no tree node, no main-map layer.
    {"id": "gw.mesh", "label": "Model grid", "parent": "gw", "group": False,
     "check": True, "layers": ("grid",)},
    {"id": "gw.run", "label": "Model run", "parent": "gw", "group": False,
     "check": False, "layers": ()},                       # hidden until a run first starts
    {"id": "gw.alt", "label": "Hydraulic Alternatives", "parent": "gw", "group": False,
     "check": False, "layers": ()},   # order-of-magnitude K / gradient sweep vs the Basecase
    # Observation wells: field data compared against the Basecase run (calibration). Pure
    # observation — never a model input, never hashed. The checkbox drives the marker layer.
    {"id": "gw.wells", "label": "Observation wells", "parent": "gw", "group": False,
     "check": True, "layers": ("obs_wells",)},
    {"id": "gw.res", "label": "Results", "parent": "gw", "group": True,
     "check": True, "layers": ()},
    {"id": "gw.res.head", "label": "Hydraulic head", "parent": "gw.res", "group": False,
     "check": True, "layers": ("head",)},
    # Zone: a GROUP hosting the analysis knobs + Delineate button (props pane). Hidden until a GW
    # run exists; its Flow-paths and Volumes subgroups stay hidden until hz_result exists. Flow
    # paths are produced ONLY by delineation — there is no monolithic pre-delineation display.
    {"id": "gw.res.hz", "label": "Hyporheic Zone", "parent": "gw.res", "group": True,
     "check": True, "layers": ()},
    # Flow paths: a GROUP under Zone, one classed child per exchange class. Each child's layers
    # carry BOTH its pathlines and its entry/return dots, so one checkbox toggles both together.
    {"id": "gw.res.paths", "label": "Flow paths", "parent": "gw.res.hz", "group": True,
     "check": True, "layers": ()},
    {"id": "gw.res.paths.hyp", "label": "Hyporheic", "parent": "gw.res.paths", "group": False,
     "check": True, "layers": ("hz_paths_hyporheic", "hz_nodes_hyporheic_start",
                               "hz_nodes_hyporheic_end")},
    {"id": "gw.res.paths.los", "label": "Losing", "parent": "gw.res.paths", "group": False,
     "check": True, "layers": ("hz_paths_losing", "hz_nodes_losing_start",
                               "hz_nodes_losing_end")},
    {"id": "gw.res.paths.gain", "label": "Gaining", "parent": "gw.res.paths", "group": False,
     "check": True, "layers": ("hz_paths_gaining", "hz_nodes_gaining_start",
                               "hz_nodes_gaining_end")},
    {"id": "gw.res.paths.thru", "label": "Throughflow", "parent": "gw.res.paths", "group": False,
     "check": True, "layers": ("hz_paths_throughflow", "hz_nodes_throughflow_start",
                               "hz_nodes_throughflow_end")},
    # Volumes: a GROUP under Zone with a selectable volume object (plan footprint) per class.
    {"id": "gw.res.hz.vols", "label": "Volumes", "parent": "gw.res.hz", "group": True,
     "check": True, "layers": ()},
    {"id": "gw.res.hz.hyp", "label": "Hyporheic", "parent": "gw.res.hz.vols", "group": False,
     "check": True, "layers": ("hz_foot_hyporheic",)},
    {"id": "gw.res.hz.los", "label": "Losing", "parent": "gw.res.hz.vols", "group": False,
     "check": True, "layers": ("hz_foot_losing",)},
    {"id": "gw.res.hz.gain", "label": "Gaining", "parent": "gw.res.hz.vols", "group": False,
     "check": True, "layers": ("hz_foot_gaining",)},
    {"id": "gw.res.hz.thru", "label": "Throughflow", "parent": "gw.res.hz.vols", "group": False,
     "check": True, "layers": ("hz_foot_throughflow",)},
    # Flows: flux-weighted exchange-flow accounting (pane) + the streambed exchange map.
    # A GROUP since 2026-07-24: the downwelling/upwelling cell overlays are separate child
    # layers so red and blue cells toggle independently (parent checkbox still cascades).
    {"id": "gw.res.hz.flows", "label": "Flows", "parent": "gw.res.hz", "group": True,
     "check": True, "layers": ()},
    {"id": "gw.res.hz.flows.down", "label": "Downwelling cells", "parent": "gw.res.hz.flows",
     "group": False, "check": True, "layers": ("hz_flow_down",)},
    {"id": "gw.res.hz.flows.up", "label": "Upwelling cells", "parent": "gw.res.hz.flows",
     "group": False, "check": True, "layers": ("hz_flow_up",)},
    # Hyporheic Functions: the optional side-branch that turns the three hydraulic dimensions into
    # functional estimates. Top-level so it physically separates hydraulics from function, and so
    # the remaining functions have somewhere to land without a second reorganisation.
    # Screening is fast, needs no field data, and feeds the report. Detailed (a MODFLOW 6 transport
    # or heat run, uncalibrated and gated) joins as a sibling in a later phase.
    {"id": "fn", "label": "Hyporheic Functions", "parent": None, "group": True,
     "check": False, "layers": ()},                        # hidden until hz_result exists
    # Screening is a GROUP holding ONE SECTION PER HYPORHEIC FUNCTION, and there are exactly four
    # of them. It stays a group rather than flattening onto `fn` because a Detailed tier is planned
    # alongside it as `fn.det`, and flattening now would buy one indent level at the cost of every
    # `fn.scr.*` node id, which is saved-project surface.
    {"id": "fn.scr", "label": "Screening estimates", "parent": "fn", "group": True,
     "check": False, "layers": ()},
    {"id": "fn.scr.nut", "label": "Nutrient Cycling", "parent": "fn.scr", "group": False,
     "check": False, "layers": ()},
    # Attenuation, not removal: the metals endpoints are sorption to manganese oxides that
    # desorb as pH falls, and the organics transform. Nothing in this section is destruction.
    # The node ID stays `fn.scr.pol` -- it is saved-project surface, and the label is not.
    #
    # ONE NODE, one calculator. It held two mechanisms for a while -- dissolved-phase attenuation
    # (first order in time) and microplastic retention (empirical in distance) -- and the group was
    # what let a reader navigate between them. Microplastics is out for now (see registry.py's
    # `_MICROPLASTIC` for the three tuples that bring it back), so a group with one child is a
    # click that leads nowhere and the dissolved node folded back into its parent.
    #
    # The endpoints are a chip picker INSIDE this node rather than nodes of their own: ten of them
    # would bury the four functions they sit under.
    {"id": "fn.scr.pol", "label": "Pollutant Attenuation", "parent": "fn.scr", "group": False,
     "check": False, "layers": ()},
    {"id": "fn.scr.hab", "label": "Habitat Creation", "parent": "fn.scr", "group": False,
     "check": False, "layers": ()},
    {"id": "fn.scr.tmp", "label": "Temperature Regulation", "parent": "fn.scr", "group": False,
     "check": False, "layers": ()},
    # Site Reports: the workflow's takeaway. Top-level so it is never buried under Results; no
    # checkbox, no map layers. THREE documents, three nodes. The hydraulic signature is direct
    # model output and the screening estimates are inferred from it (spec §9.4), so those are
    # separate reports and the tree is where a reader picks one. The group id stays "report" so a
    # saved project last viewed there still reopens somewhere real.
    {"id": "report", "label": "Site Reports", "parent": None, "group": True,
     "check": False, "layers": ()},
    # First, and the only one that never depends on a run: it describes the framework rather than
    # this site, so it is readable before there is anything to report.
    {"id": "report.concept", "label": "Conceptual Model", "parent": "report", "group": False,
     "check": False, "layers": ()},
    {"id": "report.hyd", "label": "Hydraulics Report", "parent": "report", "group": False,
     "check": False, "layers": ()},
    {"id": "report.fn", "label": "Functional Screening Report", "parent": "report", "group": False,
     "check": False, "layers": ()},
    # Desktop only (app._push_tree_state hides it in cloud: comparing reads OTHER projects'
    # folders off the local filesystem). A MANAGER pane plus a built document, never a live view.
    {"id": "report.cmp", "label": "Cross-Site Comparison", "parent": "report", "group": False,
     "check": False, "layers": ()},
    # Desktop only (cloud has no local filesystem for the linked files). The group node is
    # static; the per-layer child rows are DYNAMIC (one per user reference file) and ride
    # build_tree_payload(extra_rows=...) with ids "ml:<uid>" — they are NOT in NODES, and
    # app.py's tree_event dispatch routes them before the static-id guards.
    {"id": "maplyr", "label": "Map layers", "parent": None, "group": True,
     "check": True, "layers": ()},
    {"id": "base", "label": "Basemaps", "parent": None, "group": True,
     "check": True, "layers": ()},
    {"id": "base.imagery", "label": "USGS Imagery", "parent": "base", "group": False,
     "check": True, "layers": ()},                        # check=True means "has a checkbox"; the
    {"id": "base.topo", "label": "USGS Topo", "parent": "base", "group": False,   # startup checked
     "check": True, "layers": ()},                        # state is set in _CHECK_DEFAULTS (topo default)
    {"id": "base.hydro", "label": "NHD Hydrography", "parent": "base", "group": False,
     "check": True, "layers": ("NHD streams",)},  # the flowline vectors reach picks snap to
]

NODE: dict[str, dict] = {n["id"]: n for n in NODES}
NODE_LAYERS: dict[str, tuple] = {n["id"]: tuple(n["layers"]) for n in NODES if n["check"]}
# Every group id, hidden or not — the "collapse the whole tree" set (project open sends it
# to www/tree.js, whose client-owned expansion state can't know about not-yet-visible groups).
GROUP_IDS: tuple[str, ...] = tuple(n["id"] for n in NODES if n["group"])

# node id -> wizard step whose machinery drives it (None = leave the step alone: basemaps).
# Behavioral mapping — see module docstring for why sw.wetted lives on the boundaries step.
NODE_STEP: dict[str, str | None] = {
    "project": None,
    "reach": _REACH,
    "terrain": _DEM, "terrain.dem": _DEM, "terrain.chanmod": _DEM,
    "bnd": _BOUNDARIES, "bnd.up": _BOUNDARIES, "bnd.left": _BOUNDARIES,
    "bnd.right": _BOUNDARIES, "bnd.down": _BOUNDARIES,
    "sw": _SURFACE, "sw.mesh": _SURFACE,
    "sw.wetted": _BOUNDARIES,
    "sw.wse": _SURFACE, "sw.depth": _SURFACE,
    "gw": _MESH, "gw.k": _K, "gw.mesh": _MESH, "gw.run": _RUN,
    "gw.alt": _RUN,
    "gw.wells": _MESH,          # armed-click placement rides the mesh step's crosshair branch
    "gw.res": _RESULTS, "gw.res.head": _RESULTS, "gw.res.paths": _RESULTS,
    "gw.res.paths.hyp": _RESULTS, "gw.res.paths.los": _RESULTS,
    "gw.res.paths.gain": _RESULTS, "gw.res.paths.thru": _RESULTS,
    "gw.res.hz": _RESULTS, "gw.res.hz.vols": _RESULTS,
    "gw.res.hz.hyp": _RESULTS, "gw.res.hz.los": _RESULTS,
    "gw.res.hz.gain": _RESULTS, "gw.res.hz.thru": _RESULTS,
    "gw.res.hz.flows": _RESULTS,
    "gw.res.hz.flows.down": _RESULTS, "gw.res.hz.flows.up": _RESULTS,
    # Function screening rides the existing results step: it is an optional side-branch, so it
    # adds no STAGES entry and needs no _stage_states()/_reachable() edits (Sensitivity precedent).
    "fn": _RESULTS, "fn.scr": _RESULTS, "fn.scr.nut": _RESULTS, "fn.scr.pol": _RESULTS,
    "fn.scr.hab": _RESULTS, "fn.scr.tmp": _RESULTS,
    "report": _REPORT, "report.hyd": _REPORT, "report.fn": _REPORT,
    # None, so it is NEVER greyed: `app._push_tree_state` builds its disabled set from the
    # non-None entries here, and the Conceptual Model is openable with no run behind it.
    "report.concept": None,
    # None for the same reason: the comparison manager must be reachable with no local run
    # (foreign sites supply results), so only its Open gate asks for anything.
    "report.cmp": None,
    # None: reference layers are usable at any point in the workflow (basemaps precedent).
    "maplyr": None,
    "base": None, "base.imagery": None, "base.topo": None, "base.hydro": None,
}

#: Node ids that shipped in saved projects and no longer exist, mapped to their successor.
#:
#: Without an entry here, `app.py`'s open handler falls through `nid if nid in NODE else "reach"`,
#: and a project last viewed on a retired node reopens on Reach centerline with the stepper rewound
#: to stage 1 -- which reads as data loss rather than as a renamed node.
#:
#: All three of these are the Pollutant Attenuation subtree collapsing back to one node. Microplastic
#: Retention was a top-level Screening section (`fn.scr.mp`), then a mechanism node under Pollutant
#: Attenuation (`fn.scr.pol.mp`); Dissolved Pollutants (`fn.scr.pol.dis`) was its sibling. Every one
#: of them now lands on the merged node. NOTE an alias may not also be a live node, which is why
#: `fn.scr.mp` could never simply be revived -- `validate_tree` rejects the ambiguity.
NODE_ALIAS: dict[str, str] = {"fn.scr.mp": "fn.scr.pol",
                              "fn.scr.pol.mp": "fn.scr.pol",
                              "fn.scr.pol.dis": "fn.scr.pol",
                              # Sensitivity became Hydraulic Alternatives (2026-08-02)
                              "gw.sens": "gw.alt"}


def resolve_node(node_id: str | None) -> str | None:
    """A live node id for a possibly-retired one, or None when it names nothing at all."""
    nid = NODE_ALIAS.get(node_id, node_id)
    return nid if nid in NODE else None


def validate_tree() -> None:
    """Structural invariants, run at import so a malformed table cannot ship.

    THE BUG THIS EXISTS FOR: `fn.scr.mp` shipped without a `NODE_STEP` entry. `app._push_tree_state`
    builds its `disabled` set by iterating `NODE_STEP`, so a node missing from it is simply never
    gated: its four siblings greyed out when the results step was unreachable and it did not.
    Nothing else noticed, and the test that should have caught it iterated a hand-written id list
    with the same omission."""
    missing = [n["id"] for n in NODES if n["id"] not in NODE_STEP]
    if missing:
        raise ValueError(f"nodes with no NODE_STEP entry: {missing}. Every node needs one or it "
                         f"is never gated.")
    stray = [nid for nid in NODE_STEP if nid not in NODE]
    if stray:
        raise ValueError(f"NODE_STEP names nodes that do not exist: {stray}")
    for nid, target in NODE_ALIAS.items():
        if nid in NODE:
            raise ValueError(f"alias {nid!r} is also a live node; remove one")
        if target not in NODE:
            raise ValueError(f"alias {nid!r} points at missing node {target!r}")


validate_tree()

# HZ class-name <-> tree-node-suffix mapping (used by app.py to build layer keys and panes).
HZ_CLASS_SUFFIX = {"hyporheic": "hyp", "losing": "los", "gaining": "gain",
                   "throughflow": "thru"}
HZ_SUFFIX_CLASS = {v: k for k, v in HZ_CLASS_SUFFIX.items()}

# step -> the node the tree highlights when navigation arrives by step (stepper clicks during
# the migration, run-flow jumps): the step's "primary" node.
PRIMARY_NODE: dict[str, str] = {
    _REACH: "reach", _DEM: "terrain.dem", _BOUNDARIES: "bnd", _SURFACE: "sw",
    _K: "gw.k", _MESH: "gw.mesh", _RUN: "gw.run", _RESULTS: "gw.res",
    _REPORT: "report",
}

# The user-facing workflow: seven numbered stages rendered as the header stage bar
# (number, label, the node a chip click selects). The nine machinery steps collapse onto
# them via STEP_STAGE (K/mesh/run are all part of the Groundwater stage). Chip labels are
# short forms of the node labels (precedent: "Reach" for "Reach centerline").
STAGES: list[tuple[int, str, str]] = [
    (1, "Reach", "reach"),
    (2, "Terrain", "terrain.dem"),
    (3, "Boundaries", "bnd"),
    (4, "Surface Water", "sw"),
    (5, "Groundwater", "gw"),
    (6, "Results", "gw.res"),
    (7, "Report", "report"),
]
STEP_STAGE: dict[str, int] = {
    _REACH: 1, _DEM: 2, _BOUNDARIES: 3, _SURFACE: 4,
    _K: 5, _MESH: 5, _RUN: 5, _RESULTS: 6, _REPORT: 7,
}

# boundary slot ("up"/"left"/"right"/"down"/"wse") <-> node id
SLOT_NODE = {"up": "bnd.up", "left": "bnd.left", "right": "bnd.right", "down": "bnd.down",
             "wse": "sw.wetted"}
NODE_SLOT = {v: k for k, v in SLOT_NODE.items()}

# node id -> 3D scene layer key (the same tree checkbox drives both canvases; nodes absent
# here simply have no 3D representation).
NODE_3D = {
    "terrain.dem": "terrain",
    "base.imagery": "basemap",       # aerial drape on the 3-D mesh top (USGS Imagery)
    "base.topo": "basemap_topo",     # topo drape (same actor, second texture; radio picks one)
    "gw.mesh": "gw_mesh",
    "gw.res.head": "head",
    "sw.wse": "wse",
    "sw.depth": "depth",
    # classed flow paths (3D lines) + zone volumes (3D translucent shells)
    "gw.res.paths.hyp": "hz3d_paths_hyporheic",
    "gw.res.paths.los": "hz3d_paths_losing",
    "gw.res.paths.gain": "hz3d_paths_gaining",
    "gw.res.paths.thru": "hz3d_paths_throughflow",
    "gw.res.hz.hyp": "hz3d_vol_hyporheic",
    "gw.res.hz.los": "hz3d_vol_losing",
    "gw.res.hz.gain": "hz3d_vol_gaining",
    "gw.res.hz.thru": "hz3d_vol_throughflow",
}


def node_step(node_id):
    """The wizard step for a node id (None for unknown ids / basemaps — leave the step be).

    Resolves retired ids through NODE_ALIAS first, so restoring a project saved on a node that no
    longer exists lands on its successor's step instead of rewinding the stepper to stage 1."""
    if not node_id:
        return None
    return NODE_STEP.get(NODE_ALIAS.get(node_id, node_id))


def depth(node_id: str) -> int:
    d, n = 0, NODE.get(node_id)
    while n and n["parent"]:
        d += 1
        n = NODE.get(n["parent"])
    return d


def is_ancestor(anc: str, node_id: str) -> bool:
    n = NODE.get(node_id)
    while n and n["parent"]:
        if n["parent"] == anc:
            return True
        n = NODE.get(n["parent"])
    return False


def check_ancestors(node_id: str) -> list[str]:
    """Checkbox-carrying ancestors of a node, nearest first (the visibility cascade chain)."""
    out, n = [], NODE.get(node_id)
    while n and n["parent"]:
        p = NODE.get(n["parent"])
        if p is None:
            break
        if p.get("check"):
            out.append(p["id"])
        n = p
    return out


def check_subtree(node_id: str) -> list[str]:
    """node_id + every checkbox-carrying descendant, in display order — the set whose
    EFFECTIVE visibility must be re-applied when node_id's checkbox flips."""
    return [m["id"] for m in NODES
            if m.get("check") and (m["id"] == node_id or is_ancestor(node_id, m["id"]))]


def build_tree_payload(*, selected=None, statuses=None, checks=None, disabled=(),
                       hidden=(), dimmed=(), fly=None, extra_rows=()) -> dict:
    """The hype_tree custom-message payload: a flat, ordered node list for www/tree.js.

    statuses: {id: "idle"|"running"|"done"|"error"} (missing -> "none" = no icon)
    checks:   {id: bool} for checkbox rows (missing -> True); RAW user intent
    disabled: ids rendered grayed (soft gating: still clickable — the props pane explains)
    hidden:   ids not rendered at all (children of hidden groups are dropped too)
    dimmed:   checked ids whose layers are nevertheless hidden by an unchecked ancestor
              group — the row dims so the parent override stays visible
    fly:      optional [[south, west], [north, east]] bounds for a tree-initiated zoom
    extra_rows: FULLY-FORMED dynamic rows (per-session feature lists, e.g. Map layers):
              each dict carries the same keys as a static row (id/label/parent/depth/
              group/status/check/disabled/dim) and is inserted directly AFTER its
              parent's row (tree.js renders strict array order, so appending at the end
              would draw them at the bottom of the tree). Rows whose parent is hidden
              are dropped with it.
    """
    statuses = statuses or {}
    checks = checks or {}
    hidden = set(hidden)
    disabled = set(disabled)
    dimmed = set(dimmed)
    out = []
    for n in NODES:
        nid = n["id"]
        if nid in hidden:
            continue
        p = n["parent"]
        anc_hidden = False
        while p:
            if p in hidden:
                anc_hidden = True
                break
            p = NODE[p]["parent"]
        if anc_hidden:
            continue
        out.append({
            "id": nid,
            "label": n["label"],
            "parent": n["parent"],
            "depth": depth(nid),
            "group": bool(n["group"]),
            "status": statuses.get(nid, "none"),
            "check": bool(checks.get(nid, True)) if n["check"] else None,
            "disabled": nid in disabled,
            "dim": nid in dimmed,
        })
    if extra_rows:
        by_parent: dict = {}
        for r in extra_rows:
            by_parent.setdefault(r.get("parent"), []).append(dict(r))
        merged = []
        for row in out:
            merged.append(row)
            merged.extend(by_parent.pop(row["id"], ()))
        out = merged                       # extras whose parent didn't render drop with it
    payload = {"selected": selected, "nodes": out}
    if fly:
        payload["fly"] = fly
    return payload
