"""The layer tree of one document: walk, index, ref resolution, layer object.

Node wrappers are only valid while their document is open, so none is
kept across commands: re-find nodes by id (``node_by_id``) each command.
"""

from PyQt5.QtCore import QUuid

from . import layerprops, refs
from .errors import CommandError

# Layer-object keys, in output order (api-design § Layer object).
LAYER_FIELDS = (
    "id", "name", "path", "type", "parent", "index", "visible", "opacity",
    "blend_mode", "locked", "alpha_locked", "inherit_alpha", "pass_through",
    "has_layer_style", "bounds", "child_count", "children",
)
# F005 K12: children (the child ids) is an ordinary field; child_count is
# always there instead, so a row says whether there is more below it.
ALWAYS_FIELDS = ("id", "name", "child_count")


def node_id(node):
    """The node's id, braced lowercase."""
    return refs.normalize_uuid(node.uniqueId().toString())


def children_top_down(node):
    """Child nodes, top to bottom (``childNodes()`` is bottom to top)."""
    return list(reversed(node.childNodes()))


def walk(doc):
    """Yield ``(node, entry)`` depth-first, top to bottom (the Layers
    docker order). ``entry`` is ``{"id", "name", "parent", "type"}`` with
    ``parent`` None at the top level. The document's global selection, a
    root-level ``selectionmask``, is skipped (D4)."""
    top = [c for c in children_top_down(doc.rootNode()) if c.type() != "selectionmask"]
    stack = [(c, None) for c in reversed(top)]
    while stack:
        node, parent = stack.pop()
        nid = node_id(node)
        yield node, {"id": nid, "name": node.name(), "parent": parent, "type": node.type()}
        stack.extend((c, nid) for c in reversed(children_top_down(node)))


def build_index(doc):
    """The document's ``refs.NodeIndex``."""
    return refs.NodeIndex([entry for _, entry in walk(doc)])


def tree(doc):
    """``(index, {id: node})`` from one walk, for commands that need both
    the index and every node (``list_layers``). The wrappers are valid for
    the current command only."""
    by_id, entries = {}, []
    for node, entry in walk(doc):
        by_id[entry["id"]] = node
        entries.append(entry)
    return refs.NodeIndex(entries), by_id


def node_by_id(doc, nid):
    """The node with id ``nid`` (braces optional), or None."""
    return doc.nodeByUniqueID(QUuid(nid))


def resolve_node(doc, ref, index=None, *, keys=None, allow_root=False, field="layer"):
    """Resolve a layer ref (D4) to a Node; ``"/"`` gives the root node where
    ``allow_root``. Errors as ``refs.resolve``."""
    if index is None:
        index = build_index(doc)
    nid = refs.resolve(index, ref, keys=keys, allow_root=allow_root, field=field)
    if nid == refs.ROOT:
        return doc.rootNode()
    node = node_by_id(doc, nid)
    if node is None:
        raise CommandError("not_found", f"No layer with id {nid}")
    return node


def active_node(doc):
    """``doc.activeNode()``, or None. Headless it is always None, even after
    ``setActiveNode`` (krita-api-notes § Nodes); tests stand in for it."""
    return doc.activeNode()


def check_fields(fields):
    """Validate a ``fields`` list; returns the set of keys to include."""
    if fields is None:
        return set(LAYER_FIELDS)
    if not isinstance(fields, (list, tuple)) or not all(isinstance(f, str) for f in fields):
        raise CommandError("bad_request", "'fields' must be a list of layer-object keys")
    unknown = [f for f in fields if f not in LAYER_FIELDS]
    if unknown:
        raise CommandError("bad_request",
                           f"Unknown field(s) {', '.join(unknown)}; valid: {', '.join(LAYER_FIELDS)}",
                           {"unknown": unknown, "valid": list(LAYER_FIELDS)})
    return set(fields) | set(ALWAYS_FIELDS)


def _bounds(node):
    if not node.hasExtents():
        return None
    r = node.bounds()
    if r.width() * r.height() == 0:
        return None
    return {"x": r.x(), "y": r.y(), "w": r.width(), "h": r.height()}


def _pass_through(node):
    if node.type() != "grouplayer":
        return None
    getter = getattr(node, "passThroughMode", None)  # GroupLayer only
    return bool(getter()) if getter is not None else None


_GETTERS = {
    "type": lambda node, nid, index: node.type(),
    "path": lambda node, nid, index: index.path_of(nid),
    "parent": lambda node, nid, index: (None if index.parent(nid) == refs.ROOT
                                        else index.parent(nid)),
    "index": lambda node, nid, index: node.index(),
    "visible": lambda node, nid, index: bool(node.visible()),
    "opacity": lambda node, nid, index: layerprops.from_krita_opacity(node.opacity()),
    "blend_mode": lambda node, nid, index: node.blendingMode(),
    "locked": lambda node, nid, index: bool(node.locked()),
    "alpha_locked": lambda node, nid, index: bool(node.alphaLocked()),
    "inherit_alpha": lambda node, nid, index: bool(node.inheritAlpha()),
    "pass_through": lambda node, nid, index: _pass_through(node),
    "has_layer_style": lambda node, nid, index: node.layerStyleToAsl() != "",
    "bounds": lambda node, nid, index: _bounds(node),
}


def layer_object(node, index, fields=None, *, nid=None):
    """The layer object (api-design § Layer object) for ``node``, which must
    be in ``index``. ``fields``: optional list of keys; ``id``, ``name`` and
    ``child_count`` are always included, and only the keys asked for are read
    from Krita. ``nid``: the node's id when the caller already has it."""
    want = check_fields(fields)
    if nid is None:
        nid = node_id(node)
    out = {}
    for key in LAYER_FIELDS:
        if key not in want:
            continue
        if key == "id":
            out[key] = nid
        elif key == "name":
            out[key] = node.name()
        elif key == "children":
            out[key] = index.children(nid)
        elif key == "child_count":
            out[key] = len(index.children(nid))
        else:
            out[key] = _GETTERS[key](node, nid, index)
    return out
