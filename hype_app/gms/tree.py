"""The GMS project-explorer tree: /Tree/Tree token stream of a .gpr file.

GMS stores the project explorer (names, nesting, GUID links, expansion state) as ONE
flat HDF5 dataset of fixed-width byte strings, one token per array slot. Reverse
engineered from a GMS 10.7.4 project; grammar:

    BEGTREE
      BEGTRNODE                     <- the "Project" wrapper node
        TRTYPE Project
        TRNAME "Project"
        TRGUID <project uuid>
        TREXPANDED 1
        BEGTRMOD <module id>        <- modules nest INSIDE the project node
          BEGTRNODE                 <- exactly one root node per module
            TRTYPE <class code>     e.g. TI_ROOT, TIGRID3D, TIMODFLOW, TIPARTSET ...
            TRNAME "<display name>"
            TRGUID <uuid> | TRID -1 <- data-object link, or -1 for synthetic folders
            TRACTIVE <n>            <- -1 marks the active item among its siblings
            TREXPANDED 0|1
            TRSTATE 0|1
            BEGTRNODE ... ENDTRNODE <- children, nestable
          ENDTRNODE
        ENDTRMOD <module id>
      ENDTRNODE
    ENDTREE

Property tokens appear in the order above (TRTYPE, TRNAME, link, TRACTIVE,
TREXPANDED, TRSTATE) and every one after TRNAME is optional. Module ids observed:
1=TIN, 5=2D Grid, 6=2D Scatter, 8=3D Grid, 10=Map, 11=GIS. The stream must stay
balanced and its TRGUIDs consistent with the GUID datasets elsewhere in the .gpr,
or GMS corrupts/drops explorer items.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Emission order for properties of nodes we build ourselves (parsed nodes keep
# their original order verbatim).
_PROP_ORDER = ("TRTYPE", "TRNAME", "TRGUID", "TRID", "TRACTIVE", "TREXPANDED", "TRSTATE")
_PROP_KEYS = frozenset(_PROP_ORDER)

# Parity with the observed file: tokens are |S79. Kept as a floor; widened only if
# a generated token would not fit.
MIN_TOKEN_WIDTH = 79


@dataclass
class TreeNode:
    """One explorer item: ordered (key, raw-value) props + children."""
    props: list[tuple[str, str]] = field(default_factory=list)
    children: list["TreeNode"] = field(default_factory=list)

    def _get(self, key: str) -> str | None:
        for k, v in self.props:
            if k == key:
                return v
        return None

    @property
    def trtype(self) -> str:
        return self._get("TRTYPE") or ""

    @property
    def name(self) -> str:
        raw = self._get("TRNAME") or ""
        return raw[1:-1] if raw.startswith('"') and raw.endswith('"') else raw

    @property
    def guid(self) -> str | None:
        return self._get("TRGUID")

    def set_guid(self, guid: str):
        self.props = [(k, guid if k == "TRGUID" else v) for k, v in self.props]

    def set_name(self, name: str):
        self.props = [(k, f'"{name}"' if k == "TRNAME" else v) for k, v in self.props]


def node(trtype: str, name: str, *, guid: str | None = None, trid: int | None = None,
         active: int | None = None, expanded: int | None = None, state: int | None = None,
         children: list[TreeNode] | None = None) -> TreeNode:
    """Build a node with canonical property order. Pass guid OR trid (default trid=-1)."""
    props: list[tuple[str, str]] = [("TRTYPE", trtype), ("TRNAME", f'"{name}"')]
    if guid is not None:
        props.append(("TRGUID", guid))
    else:
        props.append(("TRID", str(-1 if trid is None else trid)))
    if active is not None:
        props.append(("TRACTIVE", str(active)))
    if expanded is not None:
        props.append(("TREXPANDED", str(expanded)))
    if state is not None:
        props.append(("TRSTATE", str(state)))
    return TreeNode(props=props, children=list(children or []))


@dataclass
class GmsTree:
    """The whole stream: the Project wrapper node + its (module id, root node) blocks."""
    project: TreeNode
    modules: list[tuple[int, TreeNode]] = field(default_factory=list)

    def module(self, mod_id: int) -> TreeNode | None:
        for mid, root in self.modules:
            if mid == mod_id:
                return root
        return None


# ---------------------------------------------------------------------------
# tokens <-> tree
# ---------------------------------------------------------------------------

def _emit_node(n: TreeNode, out: list[str]):
    out.append("BEGTRNODE")
    for k, v in n.props:
        out.append(f"{k} {v}")
    for c in n.children:
        _emit_node(c, out)
    out.append("ENDTRNODE")


def build_tokens(tree: GmsTree) -> list[str]:
    out: list[str] = ["BEGTREE", "BEGTRNODE"]
    for k, v in tree.project.props:
        out.append(f"{k} {v}")
    for mod_id, root in tree.modules:
        out.append(f"BEGTRMOD {mod_id}")
        _emit_node(root, out)
        out.append(f"ENDTRMOD {mod_id}")
    out.append("ENDTRNODE")
    out.append("ENDTREE")
    return out


class TreeFormatError(ValueError):
    pass


class _Cursor:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.i = 0

    def peek(self) -> str:
        if self.i >= len(self.tokens):
            raise TreeFormatError("unexpected end of token stream")
        return self.tokens[self.i]

    def take(self) -> str:
        t = self.peek()
        self.i += 1
        return t

    def expect(self, tok: str):
        t = self.take()
        if t != tok:
            raise TreeFormatError(f"expected {tok!r} at #{self.i - 1}, got {t!r}")


def _split_prop(tok: str) -> tuple[str, str] | None:
    key, _, rest = tok.partition(" ")
    return (key, rest) if key in _PROP_KEYS else None


def _parse_node(cur: _Cursor) -> TreeNode:
    cur.expect("BEGTRNODE")
    n = TreeNode()
    while True:
        t = cur.peek()
        if t == "BEGTRNODE":
            n.children.append(_parse_node(cur))
        elif t == "ENDTRNODE":
            cur.take()
            return n
        else:
            prop = _split_prop(cur.take())
            if prop is None:
                raise TreeFormatError(f"unknown token inside node: {t!r}")
            n.props.append(prop)


def parse_tokens(tokens: list[str]) -> GmsTree:
    """Parse and validate a full stream. Raises TreeFormatError on any imbalance."""
    cur = _Cursor(list(tokens))
    cur.expect("BEGTREE")
    cur.expect("BEGTRNODE")
    project = TreeNode()
    modules: list[tuple[int, TreeNode]] = []
    while True:
        t = cur.peek()
        if t.startswith("BEGTRMOD "):
            mod_id = int(cur.take().split()[1])
            root = _parse_node(cur)
            end = cur.take()
            if end != f"ENDTRMOD {mod_id}":
                raise TreeFormatError(f"module {mod_id} closed by {end!r}")
            modules.append((mod_id, root))
        elif t == "ENDTRNODE":
            cur.take()
            break
        elif t == "BEGTRNODE":
            # Never observed (plain children of the Project node), but keep them.
            project.children.append(_parse_node(cur))
        else:
            prop = _split_prop(cur.take())
            if prop is None:
                raise TreeFormatError(f"unknown token in project node: {t!r}")
            project.props.append(prop)
    cur.expect("ENDTREE")
    if cur.i != len(cur.tokens):
        raise TreeFormatError(f"{len(cur.tokens) - cur.i} trailing tokens after ENDTREE")
    return GmsTree(project=project, modules=modules)


def iter_nodes(tree: GmsTree):
    """Yield every TreeNode (module roots and descendants), depth-first in
    document order."""
    def walk(n: TreeNode):
        yield n
        for c in n.children:
            yield from walk(c)
    for _, root in tree.modules:
        yield from walk(root)
    for c in tree.project.children:
        yield from walk(c)


def guids_in_tree(tree: GmsTree) -> set[str]:
    out = set()
    if tree.project.guid:
        out.add(tree.project.guid)
    for n in iter_nodes(tree):
        if n.guid:
            out.add(n.guid)
    return out


# ---------------------------------------------------------------------------
# HDF5 dataset I/O
# ---------------------------------------------------------------------------

def read_tokens(h5file) -> list[str]:
    return [t.decode("ascii") for t in h5file["Tree/Tree"][:]]


def write_tokens(h5file, tokens: list[str]):
    """Recreate /Tree/Tree with example-parity storage (|S79+, gzip, one chunk)."""
    import numpy as np

    width = max(MIN_TOKEN_WIDTH, max((len(t) for t in tokens), default=0) + 1)
    for t in tokens:
        t.encode("ascii")               # any non-ascii name is a bug upstream
    tree_grp = h5file.require_group("Tree")
    if "Tree" in tree_grp:
        del tree_grp["Tree"]
    data = np.array([t.encode("ascii") for t in tokens], dtype=f"S{width}")
    tree_grp.create_dataset("Tree", data=data, chunks=(len(tokens),),
                            compression="gzip", maxshape=(len(tokens),))
