"""Pure model for the layer tree (left panel) — NO Shiny imports, no reactives.

The tree is a FIXED hierarchy (hype-app has one reach, one DEM, four boundary sides, one
surface-water model, one groundwater model — no dynamic feature lists), so the model is a
flat ordered list with parent links. app.py gathers live state (statuses, checkbox values,
reachability) and calls build_tree_payload(); www/tree.js renders/reconciles the DOM.

Node ids are dotted paths ("bnd.left", "gw.res.paths"). NODE_STEP maps each node onto the
wizard step whose machinery drives it — BEHAVIORAL, not hierarchical (e.g. "sw.wetted" maps
to the *boundaries* step because the wetted-extent polygon is edited through the boundary
slot machinery).
"""
from __future__ import annotations

# Step keys — must match the STEP_* constants in app.py (stable strings).
_REACH, _DEM, _BOUNDARIES, _SURFACE, _K, _MESH, _RUN, _RESULTS = (
    "reach", "dem", "boundaries", "surface", "k", "mesh", "run", "results")

# Display order = list order. "check" nodes carry a visibility checkbox controlling the
# _layers keys in "layers". Checkbox semantics CASCADE: a node's layers are visible only when
# its own box AND every checkbox ancestor's box are ticked (the parent toggle overrides the
# children without erasing their state) — see app.py `_eff_checked`/`_apply_check_effective`.
NODES: list[dict] = [
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
    {"id": "sw", "label": "Water surface", "parent": None, "group": True,
     "check": True, "layers": ()},
    {"id": "sw.mesh", "label": "2D mesh", "parent": "sw", "group": False,
     "check": True, "layers": ("RAS mesh",)},
    {"id": "sw.wetted", "label": "Wetted extent", "parent": "sw", "group": False,
     "check": True, "layers": ("Water-surface extent", "Modeled extent")},
    {"id": "sw.wse", "label": "Water surface (raster)", "parent": "sw", "group": False,
     "check": True, "layers": ("wse_raster", "sw_wse")},   # wse_raster = today's consumed-WSE
     #                                                        overlay; sw_wse arrives at step 7
    {"id": "sw.depth", "label": "Depth (raster)", "parent": "sw", "group": False,
     "check": True, "layers": ("sw_depth",)},
    {"id": "gw", "label": "Groundwater", "parent": None, "group": True,
     "check": True, "layers": ()},
    {"id": "gw.k", "label": "Subsurface properties", "parent": "gw", "group": False,
     "check": True, "layers": ("K-zones",)},
    {"id": "gw.soils", "label": "NRCS soils", "parent": "gw", "group": False,
     "check": True, "layers": ("soils",)},   # SSURGO review layer (revision §6.3)
    {"id": "gw.mesh", "label": "Model grid", "parent": "gw", "group": False,
     "check": True, "layers": ("grid",)},
    {"id": "gw.run", "label": "Model run", "parent": "gw", "group": False,
     "check": False, "layers": ()},                       # hidden until a run first starts
    {"id": "gw.sens", "label": "Sensitivity", "parent": "gw", "group": False,
     "check": False, "layers": ()},   # gradient-sensitivity scenarios (revision §10)
    {"id": "gw.res", "label": "Results", "parent": "gw", "group": True,
     "check": True, "layers": ()},
    {"id": "gw.res.head", "label": "Hydraulic head", "parent": "gw.res", "group": False,
     "check": True, "layers": ("head",)},
    # Zone: a GROUP hosting the analysis knobs + Delineate button (props pane). Hidden until a GW
    # run exists; its Flow-paths and Volumes subgroups stay hidden until hz_result exists. Flow
    # paths are produced ONLY by delineation — there is no monolithic pre-delineation display.
    {"id": "gw.res.hz", "label": "Zone", "parent": "gw.res", "group": True,
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
    {"id": "base", "label": "Basemaps", "parent": None, "group": True,
     "check": True, "layers": ()},
    {"id": "base.imagery", "label": "USGS Imagery", "parent": "base", "group": False,
     "check": True, "layers": ()},                        # check=True means "has a checkbox"; the
    {"id": "base.topo", "label": "USGS Topo", "parent": "base", "group": False,   # startup checked
     "check": True, "layers": ()},                        # state is set in _CHECK_DEFAULTS (topo default)
    {"id": "base.hydro", "label": "NHD Hydrography", "parent": "base", "group": False,
     "check": True, "layers": ()},
]

NODE: dict[str, dict] = {n["id"]: n for n in NODES}
NODE_LAYERS: dict[str, tuple] = {n["id"]: tuple(n["layers"]) for n in NODES if n["check"]}

# node id -> wizard step whose machinery drives it (None = leave the step alone: basemaps).
# Behavioral mapping — see module docstring for why sw.wetted lives on the boundaries step.
NODE_STEP: dict[str, str | None] = {
    "reach": _REACH,
    "terrain": _DEM, "terrain.dem": _DEM, "terrain.chanmod": _DEM,
    "bnd": _BOUNDARIES, "bnd.up": _BOUNDARIES, "bnd.left": _BOUNDARIES,
    "bnd.right": _BOUNDARIES, "bnd.down": _BOUNDARIES,
    "sw": _SURFACE, "sw.mesh": _SURFACE,
    "sw.wetted": _BOUNDARIES,
    "sw.wse": _SURFACE, "sw.depth": _SURFACE,
    "gw": _MESH, "gw.k": _K, "gw.soils": _K, "gw.mesh": _MESH, "gw.run": _RUN,
    "gw.sens": _RUN,
    "gw.res": _RESULTS, "gw.res.head": _RESULTS, "gw.res.paths": _RESULTS,
    "gw.res.paths.hyp": _RESULTS, "gw.res.paths.los": _RESULTS,
    "gw.res.paths.gain": _RESULTS, "gw.res.paths.thru": _RESULTS,
    "gw.res.hz": _RESULTS, "gw.res.hz.vols": _RESULTS,
    "gw.res.hz.hyp": _RESULTS, "gw.res.hz.los": _RESULTS,
    "gw.res.hz.gain": _RESULTS, "gw.res.hz.thru": _RESULTS,
    "base": None, "base.imagery": None, "base.topo": None, "base.hydro": None,
}

# HZ class-name <-> tree-node-suffix mapping (used by app.py to build layer keys and panes).
HZ_CLASS_SUFFIX = {"hyporheic": "hyp", "losing": "los", "gaining": "gain",
                   "throughflow": "thru"}
HZ_SUFFIX_CLASS = {v: k for k, v in HZ_CLASS_SUFFIX.items()}

# step -> the node the tree highlights when navigation arrives by step (stepper clicks during
# the migration, run-flow jumps): the step's "primary" node.
PRIMARY_NODE: dict[str, str] = {
    _REACH: "reach", _DEM: "terrain.dem", _BOUNDARIES: "bnd", _SURFACE: "sw",
    _K: "gw.k", _MESH: "gw.mesh", _RUN: "gw.run", _RESULTS: "gw.res",
}

# The user-facing workflow: six numbered stages rendered as the header stage bar
# (number, label, the node a chip click selects). The eight machinery steps collapse onto
# them via STEP_STAGE (K/mesh/run are all part of the Groundwater stage).
STAGES: list[tuple[int, str, str]] = [
    (1, "Reach", "reach"),
    (2, "Terrain", "terrain.dem"),
    (3, "Boundaries", "bnd"),
    (4, "Water surface", "sw"),
    (5, "Groundwater", "gw"),
    (6, "Results", "gw.res"),
]
STEP_STAGE: dict[str, int] = {
    _REACH: 1, _DEM: 2, _BOUNDARIES: 3, _SURFACE: 4,
    _K: 5, _MESH: 5, _RUN: 5, _RESULTS: 6,
}

# boundary slot ("up"/"left"/"right"/"down"/"wse") <-> node id
SLOT_NODE = {"up": "bnd.up", "left": "bnd.left", "right": "bnd.right", "down": "bnd.down",
             "wse": "sw.wetted"}
NODE_SLOT = {v: k for k, v in SLOT_NODE.items()}

# node id -> 3D scene layer key (the same tree checkbox drives both canvases; nodes absent
# here simply have no 3D representation).
NODE_3D = {
    "terrain.dem": "terrain",
    "base.imagery": "basemap",     # aerial drape on the 3-D mesh top (USGS Imagery)
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
    """The wizard step for a node id (None for unknown ids / basemaps — leave the step be)."""
    if not node_id:
        return None
    return NODE_STEP.get(node_id)


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
                       hidden=(), dimmed=(), fly=None) -> dict:
    """The hype_tree custom-message payload: a flat, ordered node list for www/tree.js.

    statuses: {id: "idle"|"running"|"done"|"error"} (missing -> "none" = no icon)
    checks:   {id: bool} for checkbox rows (missing -> True); RAW user intent
    disabled: ids rendered grayed (soft gating: still clickable — the props pane explains)
    hidden:   ids not rendered at all (children of hidden groups are dropped too)
    dimmed:   checked ids whose layers are nevertheless hidden by an unchecked ancestor
              group — the row dims so the parent override stays visible
    fly:      optional [[south, west], [north, east]] bounds for a tree-initiated zoom
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
    payload = {"selected": selected, "nodes": out}
    if fly:
        payload["fly"] = fly
    return payload
