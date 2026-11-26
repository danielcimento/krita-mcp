"""Layer-tree commands (api-design § Layer tree): list_layers,
create_layers, update_layers, move_layers, delete_layers,
duplicate_layer, set_active_layer.

Node wrappers come from one ``nodes.tree`` walk (or live lookups) per
command and are never kept after it (they are only valid while the
document is open). The exception is a ``ReinsertNode`` (a delete's
inverse, or the redo of an undone create), which keeps the detached nodes
it re-inserts (D6, batch.py).

Every inverse follows batch.py's protocol (P5.1): applying it returns its
own reverse (``RemoveNode`` <-> ``ReinsertNode``, ``RestoreProps`` with the
values it overwrote, ``MoveBack`` with the forward move), and ``check(doc)``
names what is missing before it is applied.
"""

import dataclasses
import os
import traceback

from .. import batch, config, layerprops, manifest, nodes, paths, pixels, refs, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError


def _depth_param(params):
    """``depth``: None (unlimited) or an int >= 1."""
    value = params.get("depth")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise CommandError("bad_request",
                           f"'depth' must be a whole number of at least 1; got {value!r}")
    return value


def subtree(index, root, depth=None):
    """Ids under ``root`` (an id or ``refs.ROOT``), depth-first, top to
    bottom. A layer root comes first, at depth 0; its children (or the
    top-level nodes, for ``ROOT``) are depth 1. ``depth``: the deepest
    level listed, None for all."""
    out = [] if root == refs.ROOT else [root]
    stack = [(c, 1) for c in reversed(index.children(root))]
    while stack:
        nid, d = stack.pop()
        out.append(nid)
        if depth is None or d < depth:
            stack.extend((c, d + 1) for c in reversed(index.children(nid)))
    return out


@command("list_layers")
def cmd_list_layers(params):
    """The layers under ``root`` as a flat list of layer objects, with
    per-type counts."""
    handle, doc = registry.resolve_doc(params)
    depth = _depth_param(params)
    fields = params.get("fields")
    nodes.check_fields(fields)  # also when nothing is listed
    root_ref = params.get("root")
    if root_ref is None:
        root_ref = refs.ROOT

    index, by_id = nodes.tree(doc)
    root = refs.resolve(index, root_ref, allow_root=True, field="root")

    layers, counts = [], {}
    for nid in subtree(index, root, depth):
        layers.append(nodes.layer_object(by_id[nid], index, fields, nid=nid))
        kind = index.get(nid)["type"]
        counts[kind] = counts.get(kind, 0) + 1
    counts = dict(sorted(counts.items()))
    counts["total"] = len(layers)
    return {"doc": handle, "root": root, "layers": layers, "counts": counts}


# --- create_layers ---------------------------------------------------------

SPEC_KEYS = ("key", "name", "type", "parent", "above", "visible", "opacity", "blend_mode",
             "locked", "alpha_locked", "inherit_alpha", "pass_through", "png", "x", "y")
SPEC_TYPES = {"paint": "paintlayer", "group": "grouplayer"}
GROUP, PAINT = "grouplayer", "paintlayer"
MAX_KEY_LEN = 255


@dataclasses.dataclass
class _Plan:
    """One validated spec. ``parent`` is ``refs.ROOT``, an existing id, or
    the int position of a spec planned earlier in this call (a placeholder
    for a layer not created yet); ``above`` is None, an id or such an int."""

    index: int
    key: object
    name: str
    kind: str
    parent: object
    above: object
    props: dict
    png: object = None
    size: object = None
    x: int = 0
    y: int = 0


def _dry_run(params):
    dry_run = params.get("dry_run", False)
    if type(dry_run) is not bool:
        raise CommandError("bad_request", f"'dry_run' must be true or false, got {dry_run!r}",
                           {"param": "dry_run"})
    return dry_run


def _bad(message, **details):
    return CommandError("bad_request", message, details or None)


def _strict_item(item, valid):
    """``item`` (it must be a dict) without its null values: an explicit
    null means "not given". An unknown key is ``bad_request`` with
    ``details.unknown`` / ``details.valid``, so typos fail."""
    if not isinstance(item, dict):
        raise _bad(f"must be an object, got {type(item).__name__}")
    item = {k: v for k, v in item.items() if v is not None}
    unknown = sorted(str(k) for k in item if k not in valid)
    if unknown:
        raise _bad(f"unknown key(s) {', '.join(map(repr, unknown))}; valid: {', '.join(valid)}",
                   unknown=unknown, valid=list(valid))
    return item


def _check_key(value):
    if not isinstance(value, str) or value == "":
        raise _bad("'key' must be a non-empty string", param="key")
    if len(value) > MAX_KEY_LEN:
        raise _bad(f"'key' is longer than {MAX_KEY_LEN} characters", param="key")
    if "$" in value or "/" in value:
        raise _bad(f"'key' {value!r} must not contain '$' or '/' (refer to it as '$<key>')",
                   param="key")
    return value


def _check_coord(spec, key):
    v = spec.get(key, 0)
    if isinstance(v, bool) or not isinstance(v, int):
        raise _bad(f"'{key}' must be a whole number of canvas pixels, got {v!r}", param=key)
    if not -config.COORD <= v <= config.COORD:
        raise _bad(f"'{key}' must be between {-config.COORD} and {config.COORD}", param=key)
    return v


class _Planner:
    """``validate_fn`` for ``batch.validate_all``: validates one spec
    against the document's index plus the specs planned before it."""

    def __init__(self, doc, index):
        self.doc, self.index = doc, index
        self.plans = []
        self.keys = {}  # key -> position of the spec that defined it
        self._rgba_checked = False

    def ref_text(self, value):
        """How a planned parent/above reads in results: "/", an id or "$key"."""
        if isinstance(value, int):
            return "$" + self.plans[value].key
        return value

    def __call__(self, i, spec):
        spec = _strict_item(spec, SPEC_KEYS)
        if "name" not in spec:
            raise _bad("'name' is required", param="name")
        name = layerprops.check_name(spec["name"])

        kind = spec.get("type", "paint")
        if not isinstance(kind, str) or kind not in SPEC_TYPES:
            raise _bad(f"'type' must be 'paint' or 'group', got {kind!r}", param="type")
        kind = SPEC_TYPES[kind]

        key = spec.get("key")
        if key is not None:
            _check_key(key)
            if key in self.keys:
                raise _bad(f"duplicate key {key!r} (first defined by specs[{self.keys[key]}])",
                           param="key", key=key, first=self.keys[key])

        parent_ref = spec.get("parent", refs.ROOT)
        parent = refs.resolve(self.index, parent_ref, keys=self.keys, allow_root=True,
                              field="parent")
        if parent != refs.ROOT:
            ptype = self.plans[parent].kind if isinstance(parent, int) else self.index.get(parent)["type"]
            if ptype != GROUP:
                raise _bad(f"parent {parent_ref!r} is a {ptype}; only a group or the root '/' "
                           "can hold layers", param="parent")

        above = None
        if "above" in spec:
            above_ref = spec["above"]
            above = refs.resolve(self.index, above_ref, keys=self.keys, field="above")
            above_parent = (self.plans[above].parent if isinstance(above, int)
                            else self.index.parent(above))
            if above_parent != parent:
                raise _bad(f"above {above_ref!r} is not a child of parent {parent_ref!r}",
                           param="above")

        props = layerprops.check(spec, kind)
        props.pop("name", None)
        plan = _Plan(index=i, key=key, name=name, kind=kind, parent=parent, above=above,
                     props=props)

        if "png" in spec:
            if kind != PAINT:
                raise _bad("'png' is only valid on paint layers", param="png")
            plan.png = paths.resolve_allowed_path(spec["png"], must_exist=True)
            plan.size = pixels.png_size(plan.png)
            pixels.check_png_limits(plan.size, spec["png"])
            plan.x, plan.y = _check_coord(spec, "x"), _check_coord(spec, "y")
            if not self._rgba_checked:
                pixels.require_rgba8(self.doc)
                self._rgba_checked = True
        else:
            for k in ("x", "y"):
                if k in spec:
                    raise _bad(f"'{k}' is only valid with 'png'", param=k)

        self.plans.append(plan)
        if key is not None:
            self.keys[key] = i
        return plan

    def describe(self, plan):
        out = {"key": plan.key, "name": plan.name, "type": plan.kind,
               "parent": self.ref_text(plan.parent),
               "above": None if plan.above is None else self.ref_text(plan.above)}
        if plan.png is not None:
            out.update(png=plan.png, x=plan.x, y=plan.y, w=plan.size[0], h=plan.size[1])
        return out


def _remove_node(doc, nid):
    """Remove the node with id ``nid`` from ``doc``, if it is there."""
    node = nodes.node_by_id(doc, nid)
    if node is None:
        return False
    if not node.remove():
        raise CommandError("krita_error", f"Krita refused to remove layer {nid}")
    return True


def _gone(doc, nid, what="Layer"):
    """A ``check`` reason when node ``nid`` is not in ``doc``, else None."""
    if nodes.node_by_id(doc, nid) is None:
        return f"{what} {nid} is not in the document (any more)"
    return None


def _parent_gone(doc, pid):
    return None if pid == refs.ROOT else _gone(doc, pid, "Parent layer")


class RemoveNode:
    """The inverse of creating a node: ``RemoveNode(id)(doc)`` removes it
    (with its subtree) from the live document passed at call time
    (batch.py: never a captured wrapper) and returns the ``ReinsertNode``
    that puts it back where it was. ``node_ids`` lets undo check the node
    still exists. A node that isn't there is left alone (None returned),
    for rollbacks; undo refuses that case first through ``check``."""

    __slots__ = ("node_id",)

    def __init__(self, nid):
        self.node_id = nid

    @property
    def node_ids(self):
        return (self.node_id,)

    def check(self, doc):
        return _gone(doc, self.node_id)

    def __call__(self, doc):
        node = nodes.node_by_id(doc, self.node_id)
        if node is None:
            return None
        return _take_out(node, self.node_id)

    def __repr__(self):
        return f"RemoveNode({self.node_id})"


def _applier(doc, by_id):
    """``apply_fn`` for ``batch.run``. ``by_id`` maps existing ids to their
    node wrappers (this command's walk); created nodes are added to it, so
    later specs find their placeholder parents. Discarded with the command."""
    created = {}  # plan position -> new id

    def node_for(target):
        if target == refs.ROOT:
            return doc.rootNode()
        nid = created[target] if isinstance(target, int) else target
        node = by_id.get(nid)
        if node is None:
            raise CommandError("krita_error", f"Layer {nid} disappeared during the batch")
        return node

    def apply_one(ctx_doc, plan):
        parent = node_for(plan.parent)
        above = None if plan.above is None else node_for(plan.above)
        data = None
        if plan.png is not None:
            # Decode before creating the node, so a bad file leaves nothing.
            data, w, h = pixels.load_png_bgra(plan.png)
            if (w, h) != tuple(plan.size):
                raise CommandError("conflict", f"{plan.png} changed since validation "
                                   f"({plan.size[0]}x{plan.size[1]}, now {w}x{h})")
        if plan.kind == GROUP:
            node = ctx_doc.createGroupLayer(plan.name)
        else:
            node = ctx_doc.createNode(plan.name, PAINT)
        if node is None:
            raise CommandError("krita_error", f"Krita could not create {plan.kind} {plan.name!r}")
        nid = nodes.node_id(node)
        try:
            if not parent.addChildNode(node, above):
                raise CommandError("krita_error", f"Krita refused to add {plan.name!r} to its parent")
            layerprops.apply(node, plan.props, locked=False)
            if data is not None and not node.setPixelData(data, plan.x, plan.y, w, h):
                raise CommandError("krita_error", f"Krita refused the pixels for {plan.name!r}")
            layerprops.apply_locked(node, plan.props)  # last: a locked layer may refuse writes
        except Exception:
            try:  # the failing item cleans up after itself (batch.py)
                _remove_node(ctx_doc, nid)
            except Exception:
                print(f"[KritaMCP] create_layers: could not remove half-made layer {nid}:")
                traceback.print_exc()
            raise
        created[plan.index] = nid
        by_id[nid] = node
        return {"key": plan.key, "id": nid, "name": plan.name, "type": plan.kind}, RemoveNode(nid)

    return apply_one


def _counts(kinds):
    counts = {}
    for kind in kinds:
        counts[kind] = counts.get(kind, 0) + 1
    counts = dict(sorted(counts.items()))
    counts["total"] = len(kinds)
    return counts


def _load_manifest(given):
    """``(path, Specs, positions)`` for ``create_layers(manifest=given)``.

    ``positions[k]`` is the index in the manifest's ``layers`` of spec
    ``k`` (projection entries are skipped, so later entries shift).
    """
    path = paths.resolve_allowed_path(given, must_exist=True, allow_json=True)
    if os.path.splitext(path)[1].lower() != paths.MANIFEST_EXT:
        raise CommandError("bad_request", f"'manifest' must be a {paths.MANIFEST_EXT} file: {given}",
                           {"param": "manifest"})
    loaded = manifest.to_specs(manifest.read(path), os.path.dirname(path))
    skipped = {s["index"] for s in loaded.skipped}
    positions = [i for i in range(len(loaded.specs) + len(skipped)) if i not in skipped]
    return path, loaded, positions


def _manifest_warnings(document, doc):
    """Warnings (not errors) about a manifest's informative ``document``."""
    warnings = []
    w, h = document.get("width"), document.get("height")
    if all(type(v) is int for v in (w, h)) and (w, h) != (doc.width(), doc.height()):
        warnings.append({
            "code": "document_size",
            "message": f"The manifest was made for a {w}x{h} document; this one is "
                       f"{doc.width()}x{doc.height()}. Layers keep their manifest offsets.",
            "manifest_size": {"w": w, "h": h},
            "doc_size": {"w": doc.width(), "h": doc.height()}})
    return warnings


@command("create_layers", timeout=LONG_TIMEOUT, mutating=True)
def cmd_create_layers(params):
    """Create groups and paint layers from ``specs`` or a ``manifest`` file,
    in list order, all or nothing (D11). With ``dry_run`` only validate and
    describe."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    specs, given = params.get("specs"), params.get("manifest")
    if (specs is None) == (given is None):
        raise CommandError("bad_request", "pass exactly one of 'specs' and 'manifest'")

    extra = {}
    field, positions = "specs", None
    if given is not None:
        path, loaded, positions = _load_manifest(given)
        specs, field = loaded.specs, "layers"
        extra = {"manifest": path, "skipped": loaded.skipped,
                 "warnings": _manifest_warnings(loaded.document, doc)}
        if not specs:
            raise CommandError("bad_request", f"Manifest {path} has no layers to create "
                               f"(all {len(loaded.skipped)} entries are skipped)",
                               {"skipped": loaded.skipped})

    index, by_id = nodes.tree(doc)
    planner = _Planner(doc, index)
    plans = batch.validate_all(specs, planner, field=field, positions=positions)
    counts = _counts([p.kind for p in plans])
    if dry_run:
        return {"doc": handle, "dry_run": True,
                "would_create": [planner.describe(p) for p in plans], "counts": counts, **extra}

    outcome = batch.run(plans, _applier(doc, by_id), doc, label="create_layers", field=field,
                        positions=positions)
    doc.refreshProjection()
    doc.waitForDone()
    batch.JOURNALS.get(handle).push("create_layers", outcome.inverses, outcome.nbytes)

    fresh = nodes.build_index(doc)
    created = [{**r, "path": fresh.path_of(r["id"]) if r["id"] in fresh else None}
               for r in outcome.results]
    return {"doc": handle, "dry_run": False, "created": created, "counts": counts, **extra}


def _live(doc, nid):
    node = nodes.node_by_id(doc, nid)
    if node is None:
        raise CommandError("conflict", f"Layer {nid} is not in the document (any more)",
                           {"id": nid})
    return node


def _parent_node(doc, pid):
    return doc.rootNode() if pid == refs.ROOT else _live(doc, pid)


# --- update_layers ---------------------------------------------------------

CHANGE_KEYS = ("id",) + layerprops.PROPS

# The current value of each property, in update_layers' units (opacity 0-1,
# so layerprops' round(v * 255) gives the same 0-255 value back).
_READERS = {
    "name": lambda node: node.name(),
    "visible": lambda node: bool(node.visible()),
    "opacity": lambda node: node.opacity() / 255,
    "blend_mode": lambda node: node.blendingMode(),
    "locked": lambda node: bool(node.locked()),
    "alpha_locked": lambda node: bool(node.alphaLocked()),
    "inherit_alpha": lambda node: bool(node.inheritAlpha()),
    "pass_through": lambda node: bool(node.passThroughMode()),  # groups only (validated)
}


def _read_props(node, keys):
    return {k: _READERS[k](node) for k in keys}


def _set_props(node, props):
    """Set ``props`` (layerprops.check output). An unlock goes first and a
    lock last, so a locked layer never refuses the other changes."""
    if props.get("locked") is False:
        layerprops.apply_locked(node, props)
    layerprops.apply(node, props, locked=False)
    if props.get("locked") is True:
        layerprops.apply_locked(node, props)


@dataclasses.dataclass
class _Change:
    nid: str
    props: dict


class _UpdatePlanner:
    def __init__(self, index):
        self.index = index
        self.seen = {}  # id -> position of the change that names it

    def __call__(self, i, change):
        change = _strict_item(change, CHANGE_KEYS)
        if "id" not in change:
            raise _bad("'id' is required (the layer to change)", param="id")
        nid = refs.resolve(self.index, change["id"], field="id")
        if nid in self.seen:
            raise _bad(f"layer {change['id']!r} is already changed by changes[{self.seen[nid]}]; "
                       "put all of a layer's changes in one item", param="id",
                       id=nid, first=self.seen[nid])
        props = layerprops.check(change, self.index.get(nid)["type"])
        if not props:
            raise _bad(f"nothing to change: give at least one of {', '.join(layerprops.PROPS)}",
                       valid=list(layerprops.PROPS))
        self.seen[nid] = i
        return _Change(nid, props)


def _rename_warnings(index, plans):
    """A ``duplicate_name`` warning per rename whose new name another layer
    has after the call (D4: bare-name refs to it become ambiguous)."""
    final = {nid: index.get(nid)["name"] for nid in index}
    for p in plans:
        if "name" in p.props:
            final[p.nid] = p.props["name"]
    by_name = {}
    for nid, name in final.items():  # stacking order
        by_name.setdefault(name, []).append(nid)
    warnings = []
    for p in plans:
        name = p.props.get("name")
        if name is None or name == index.get(p.nid)["name"]:
            continue
        others = [o for o in by_name[name] if o != p.nid]
        if others:
            warnings.append({
                "code": "duplicate_name",
                "message": f"Layer {p.nid} is now named {name!r}, like {len(others)} other "
                           f"layer{'s' if len(others) != 1 else ''}; a bare-name ref to it is "
                           "ambiguous (use its id or a path)",
                "id": p.nid, "name": name, "others": others})
    return warnings


class RestoreProps:
    """The inverse of a property update: set the old values of exactly the
    fields the update set, on the node looked up in the document passed at
    call time. Returns the ``RestoreProps`` of the values it overwrote."""

    __slots__ = ("node_id", "props")

    def __init__(self, nid, props):
        self.node_id, self.props = nid, props

    @property
    def node_ids(self):
        return (self.node_id,)

    def check(self, doc):
        return _gone(doc, self.node_id)

    def __call__(self, doc):
        node = _live(doc, self.node_id)
        current = _read_props(node, self.props)
        _set_props_or_restore(node, self.props, current, self.node_id)
        return RestoreProps(self.node_id, current)

    def __repr__(self):
        return f"RestoreProps({self.node_id}, {self.props!r})"


def _set_props_or_restore(node, props, old, nid):
    """Set ``props``; if that fails, put ``old`` back and re-raise (the
    failing item cleans up after itself, batch.py)."""
    try:
        _set_props(node, props)
    except Exception:
        try:
            _set_props(node, old)
        except Exception:
            print(f"[KritaMCP] could not restore the properties of layer {nid}:")
            traceback.print_exc()
        raise


def _update_one(doc, plan):
    node = _live(doc, plan.nid)
    old = _read_props(node, plan.props)
    _set_props_or_restore(node, plan.props, old, plan.nid)
    return {"id": plan.nid, "changed": list(plan.props)}, RestoreProps(plan.nid, old)


@command("update_layers", timeout=LONG_TIMEOUT, mutating=True)
def cmd_update_layers(params):
    """Set layer properties: per change only the fields given, all or
    nothing (D11)."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    index = nodes.build_index(doc)
    plans = batch.validate_all(params.get("changes"), _UpdatePlanner(index), field="changes")
    warnings = _rename_warnings(index, plans)
    if dry_run:
        return {"doc": handle, "dry_run": True,
                "would_update": [{"id": p.nid, "path": index.path_of(p.nid), "set": p.props}
                                 for p in plans],
                "warnings": warnings}

    outcome = batch.run(plans, _update_one, doc, label="update_layers", field="changes")
    doc.refreshProjection()
    doc.waitForDone()
    batch.JOURNALS.get(handle).push("update_layers", outcome.inverses)  # no snapshots
    fresh = nodes.build_index(doc)
    updated = [{"id": r["id"], "path": fresh.path_of(r["id"]) if r["id"] in fresh else None,
                "changed": r["changed"]} for r in outcome.results]
    return {"doc": handle, "dry_run": False, "updated": updated, "warnings": warnings}


# --- tree surgery for move_layers and delete_layers -------------------------
#
# Verified headless (krita-api-notes § Nodes, 2026-11-25):
# - Removing a node (remove() or removeChildNode) detaches its whole subtree:
#   every descendant is taken off its parent too, so re-adding a group gives
#   an empty group. The child wrappers, if held, can be re-added with their
#   UUIDs, pixels and properties. So every removal here captures the
#   subtree first (_capture) and every insert puts it back (_restore).
# - addChildNode(child, above) returns True but does nothing when ``above``
#   is not a child of the parent, so the sibling is always looked up among
#   the parent's children first.
# - There is no bottom insert: addChildNode(child, None) goes on top. To put
#   a node at the bottom, insert it above the bottom child, then move that
#   child above it (_insert).
# Nothing calls a method on a detached node except re-inserting it.

BOTTOM = "<bottom>"  # a position: below every child (None: on top)


def _capture(node):
    """``node``'s subtree as ``[(child, capture(child)), ...]``, bottom to top."""
    return [(c, _capture(c)) for c in node.childNodes()]


def _children(parent):
    """``parent``'s children, bottom to top, without the root's global
    selection mask (D4)."""
    kids = parent.childNodes()
    if parent.parentNode() is None:
        kids = [k for k in kids if k.type() != "selectionmask"]
    return kids


def _add(parent, child, above):
    if not parent.addChildNode(child, above):
        raise CommandError("krita_error", "Krita refused to insert a layer")


def _restore(node, layout):
    """Put the captured subtree back under the (attached) ``node``, unless
    Krita kept it."""
    if not layout or node.childNodes():
        return
    for child, sub in layout:
        _add(node, child, None)  # bottom to top: each goes on top
        _restore(child, sub)


def _detach(node):
    """Remove the attached ``node`` from its parent; return its subtree."""
    layout = _capture(node)
    parent = node.parentNode()
    if parent is None or not parent.removeChildNode(node):
        raise CommandError("krita_error", "Krita refused to remove a layer from its parent")
    return layout


def _insert(parent, node, layout, above=None, bottom=False):
    """Insert the detached ``node`` (with subtree ``layout``) into
    ``parent``: directly above the child ``above``, at the bottom with
    ``bottom``, else on top."""
    if bottom:
        kids = _children(parent)
        if kids:
            low = kids[0]
            _add(parent, node, low)
            _restore(node, layout)
            low_layout = _detach(low)
            try:
                _add(parent, low, node)
            except CommandError:
                _add(parent, low, None)  # keep it in the document, if out of place
                _restore(low, low_layout)
                raise
            _restore(low, low_layout)
            return
    _add(parent, node, above)
    _restore(node, layout)


def _position(parent, anchor, index=None):
    """``(above, bottom)`` for ``_insert``. ``anchor``: None (on top),
    ``BOTTOM``, or the id of the child to go directly above. When that child
    is not under ``parent`` (any more), ``index`` (from the bottom, clamped)
    is used instead; without one it is a ``conflict``."""
    if anchor is None:
        return None, False
    if anchor == BOTTOM:
        return None, True
    kids = _children(parent)
    for k in kids:
        if nodes.node_id(k) == anchor:
            return k, False
    if index is None:
        raise CommandError("conflict", f"Layer {anchor} is not where it was (the position "
                           "to restore is relative to it)", {"id": anchor})
    if index <= 0:
        return None, True
    if index >= len(kids):
        return None, False
    return kids[index - 1], False


def _below(parent, nid):
    """``(anchor, index)`` of child ``nid`` of ``parent``: the id of the
    child directly below it (``BOTTOM`` if none) and its index from the
    bottom."""
    ids = [nodes.node_id(k) for k in _children(parent)]
    pos = ids.index(nid)
    return (BOTTOM if pos == 0 else ids[pos - 1]), pos


def _relocate(doc, nid, parent_id, anchor, index=None):
    """Move node ``nid`` under ``parent_id`` at ``anchor`` / ``index``
    (``_position``). If the insert fails, the node goes back where it was
    (as far as possible) and the error is raised."""
    node = _live(doc, nid)
    parent = _parent_node(doc, parent_id)
    old_parent = node.parentNode()
    old_anchor, _ = _below(old_parent, nid)
    old_above = None if old_anchor == BOTTOM else _position(old_parent, old_anchor)[0]
    layout = _detach(node)
    try:
        above, bottom = _position(parent, anchor, index)
        _insert(parent, node, layout, above, bottom)
    except Exception:
        try:
            if nodes.node_by_id(doc, nid) is None:  # still detached: put it back
                _insert(old_parent, node, layout, old_above, old_anchor == BOTTOM)
        except Exception:
            print(f"[KritaMCP] could not put layer {nid} back after a failed move:")
            traceback.print_exc()
        raise


# --- move_layers -----------------------------------------------------------

MOVE_KEYS = ("id", "parent", "above", "below", "index")
POSITION_KEYS = ("above", "below", "index")


class MoveBack:
    """The inverse of a move: put node ``node_id`` back under ``parent_id``
    directly above the sibling ``anchor`` (an id, or ``BOTTOM``), or at
    ``index`` from the bottom if that sibling is gone. Everything is looked
    up by id in the document passed at call time. ``batch.rollback`` runs
    inverses newest first, so each finds the state its move left."""

    __slots__ = ("node_id", "parent_id", "anchor", "index")

    def __init__(self, nid, parent_id, anchor, index):
        self.node_id, self.parent_id, self.anchor, self.index = nid, parent_id, anchor, index

    @property
    def node_ids(self):
        return (self.node_id,)

    def check(self, doc):
        return _gone(doc, self.node_id) or _parent_gone(doc, self.parent_id)

    def __call__(self, doc):
        """Move it back; return the ``MoveBack`` to where it was."""
        node = _live(doc, self.node_id)
        parent = node.parentNode()
        anchor, index = _below(parent, self.node_id)
        back = MoveBack(self.node_id, _id_of_parent(parent), anchor, index)
        _relocate(doc, self.node_id, self.parent_id, self.anchor, self.index)
        return back

    def __repr__(self):
        return f"MoveBack({self.node_id}, {self.parent_id}, {self.anchor}, {self.index})"


@dataclasses.dataclass
class _Move:
    nid: str
    parent: str      # target parent id or refs.ROOT
    anchor: object   # None (top), BOTTOM, or the sibling id to go directly above
    old_parent: str
    old_anchor: object
    old_index: int
    noop: bool


class _SimTree:
    """The tree as parent / children ids, updated move by move during
    validation, so later moves are checked against the planned state."""

    def __init__(self, index):
        self.index = index
        self.parent = {nid: index.parent(nid) for nid in index}
        self.kids = {refs.ROOT: list(reversed(index.children(refs.ROOT)))}  # bottom to top
        for nid in index:
            self.kids[nid] = list(reversed(index.children(nid)))

    def inside(self, a, b):
        """True if ``a`` is ``b`` or in its subtree."""
        while a != refs.ROOT:
            if a == b:
                return True
            a = self.parent[a]
        return False

    def path_of(self, nid):
        names = []
        while nid != refs.ROOT:
            names.append(self.index.get(nid)["name"])
            nid = self.parent[nid]
        return refs.join_path(list(reversed(names)))

    def where(self, nid):
        """``{id, path, parent, index}`` of ``nid`` now."""
        parent = self.parent[nid]
        return {"id": nid, "path": self.path_of(nid), "parent": parent,
                "index": self.kids[parent].index(nid)}


class _MovePlanner:
    def __init__(self, index):
        self.index = index
        self.sim = _SimTree(index)

    def _sibling(self, move, key, nid, parent):
        sid = refs.resolve(self.index, move[key], field=key)
        if sid == nid:
            raise _bad(f"'{key}' names the layer being moved", param=key)
        if self.sim.parent[sid] != parent:
            where = "the root '/'" if parent == refs.ROOT else f"group {self.sim.path_of(parent)!r}"
            raise _bad(f"'{key}' {move[key]!r} is not a child of the target parent, {where} "
                       "(pass 'parent' to move into another group)", param=key)
        return sid

    def __call__(self, i, move):
        move = _strict_item(move, MOVE_KEYS)
        if "id" not in move:
            raise _bad("'id' is required (the layer to move)", param="id")
        given = [k for k in POSITION_KEYS if k in move]
        if len(given) > 1:
            raise _bad(f"give at most one of 'above', 'below' and 'index', not {' and '.join(given)}",
                       param=given[1], conflicting=given)
        sim = self.sim
        nid = refs.resolve(self.index, move["id"], field="id")
        old_parent = sim.parent[nid]
        parent = old_parent
        if "parent" in move:
            parent = refs.resolve(self.index, move["parent"], allow_root=True, field="parent")
            if parent != refs.ROOT:
                ptype = self.index.get(parent)["type"]
                if ptype != GROUP:
                    raise _bad(f"parent {move['parent']!r} is a {ptype}; only a group or the "
                               "root '/' can hold layers", param="parent")
                if sim.inside(parent, nid):
                    raise _bad(f"can't move {move['id']!r} into itself or its own subtree "
                               f"(parent {move['parent']!r})", param="parent")
        siblings = [c for c in sim.kids[parent] if c != nid]  # bottom to top

        if "above" in move:
            anchor = self._sibling(move, "above", nid, parent)
        elif "below" in move:
            pos = siblings.index(self._sibling(move, "below", nid, parent))
            anchor = BOTTOM if pos == 0 else siblings[pos - 1]
        elif "index" in move:
            k = move["index"]
            if isinstance(k, bool) or not isinstance(k, int) or not 0 <= k <= len(siblings):
                raise _bad(f"'index' must be a whole number from 0 (bottom) to {len(siblings)} "
                           f"(top), counted without the moving layer; got {k!r}",
                           param="index", max=len(siblings))
            anchor = None if k == len(siblings) else BOTTOM if k == 0 else siblings[k - 1]
        else:
            anchor = None
        if anchor == BOTTOM and not siblings:
            anchor = None

        old_kids = sim.kids[old_parent]
        old_index = old_kids.index(nid)
        old_anchor = BOTTOM if old_index == 0 else old_kids[old_index - 1]
        new_kids = list(siblings)
        if anchor is None:
            new_kids.append(nid)
        elif anchor == BOTTOM:
            new_kids.insert(0, nid)
        else:
            new_kids.insert(new_kids.index(anchor) + 1, nid)
        noop = parent == old_parent and new_kids == old_kids

        sim.kids[old_parent] = [c for c in old_kids if c != nid]
        sim.kids[parent] = new_kids
        sim.parent[nid] = parent
        return _Move(nid=nid, parent=parent, anchor=anchor, old_parent=old_parent,
                     old_anchor=old_anchor, old_index=old_index, noop=noop)


def _move_one(doc, plan):
    if plan.noop:
        return plan.nid, None
    _relocate(doc, plan.nid, plan.parent, plan.anchor)
    return plan.nid, MoveBack(plan.nid, plan.old_parent, plan.old_anchor, plan.old_index)


def _where(index, nid):
    """``{id, path, parent, index}`` from a fresh index (index from the bottom)."""
    parent = index.parent(nid)
    kids = index.children(parent)
    return {"id": nid, "path": index.path_of(nid), "parent": parent,
            "index": len(kids) - 1 - kids.index(nid)}


@command("move_layers", timeout=LONG_TIMEOUT, mutating=True)
def cmd_move_layers(params):
    """Reorder and reparent layers, in list order, all or nothing (D11)."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    index = nodes.build_index(doc)
    planner = _MovePlanner(index)
    plans = batch.validate_all(params.get("moves"), planner, field="moves")
    if dry_run:
        return {"doc": handle, "dry_run": True,
                "would_move": [planner.sim.where(p.nid) for p in plans]}

    outcome = batch.run(plans, _move_one, doc, label="move_layers", field="moves")
    doc.refreshProjection()
    doc.waitForDone()
    batch.JOURNALS.get(handle).push("move_layers", outcome.inverses)  # no snapshots
    fresh = nodes.build_index(doc)
    moved = [_where(fresh, nid) for nid in outcome.results if nid in fresh]
    return {"doc": handle, "dry_run": False, "moved": moved}


# --- delete_layers ---------------------------------------------------------

class ReinsertNode:
    """The inverse of a delete: re-insert the detached ``node`` (kept alive
    here, D6 / Q18) and its captured subtree under ``parent_id``, directly
    above the sibling ``anchor`` (an id, or ``BOTTOM``), or at ``index``
    from the bottom if that sibling is gone. The parent is looked up in the
    document passed at call time. ``node_ids``: the subtree's ids, top to
    bottom; ``nbytes``: about the pixel memory it keeps alive."""

    __slots__ = ("node", "layout", "parent_id", "anchor", "index", "node_ids", "nbytes")

    def __init__(self, node, layout, parent_id, anchor, index, node_ids, nbytes):
        self.node, self.layout = node, layout
        self.parent_id, self.anchor, self.index = parent_id, anchor, index
        self.node_ids, self.nbytes = tuple(node_ids), nbytes

    def check(self, doc):
        if nodes.node_by_id(doc, self.node_ids[0]) is not None:
            return f"Layer {self.node_ids[0]} is already in the document"
        return _parent_gone(doc, self.parent_id)

    def __call__(self, doc):
        """Re-insert; return the ``RemoveNode`` that takes it out again."""
        if nodes.node_by_id(doc, self.node_ids[0]) is not None:
            raise CommandError("conflict", f"Layer {self.node_ids[0]} is already in the document",
                               {"id": self.node_ids[0]})
        parent = _parent_node(doc, self.parent_id)
        above, bottom = _position(parent, self.anchor, self.index)
        _insert(parent, self.node, self.layout, above, bottom)
        return RemoveNode(self.node_ids[0])

    def __repr__(self):
        return f"ReinsertNode({self.node_ids[0]}, {len(self.node_ids)} nodes, {self.parent_id})"


def _paint_bytes(node, layout):
    """About ``w * h * 4`` per paint layer in the subtree (from bounds)."""
    total = 0
    stack = [(node, layout)]
    while stack:
        n, sub = stack.pop()
        if n.type() == PAINT:
            r = n.bounds()
            total += max(r.width(), 0) * max(r.height(), 0) * 4
        stack.extend(sub)
    return total


@dataclasses.dataclass
class _Delete:
    nid: str
    parent: str
    ids: list  # the subtree, top to bottom


def _check_delete_ref(index):
    def check(i, ref):
        if ref == refs.ROOT:
            raise _bad("the root '/' can't be deleted", param="ids")
        return refs.resolve(index, ref, field="id")
    return check


def _fold(index, nids):
    """The listed ids minus duplicates and ids inside another listed id's
    subtree, in stacking order (top first)."""
    listed = set(nids)
    tops = {nid for nid in listed if not any(a in listed for a in index.ancestors(nid))}
    return sorted(tops, key=index.order)


def _id_of_parent(parent):
    """The id of an attached node's ``parentNode()``, or ``refs.ROOT``."""
    return refs.ROOT if parent.parentNode() is None else nodes.node_id(parent)


def _subtree_ids(node, layout):
    """Ids of ``node`` and its captured subtree, depth-first, top to bottom
    (like ``subtree``). The nodes must still be attached."""
    out, stack = [], [(node, layout)]
    while stack:
        n, sub = stack.pop()
        out.append(nodes.node_id(n))
        stack.extend(sub)  # bottom to top: the top child pops first
    return out


def _take_out(node, nid):
    """Remove the attached ``node`` (and with it its subtree); return the
    ``ReinsertNode`` that puts it back where it was."""
    parent = node.parentNode()
    anchor, old_index = _below(parent, nid)
    layout = _capture(node)
    ids = _subtree_ids(node, layout)
    nbytes = _paint_bytes(node, layout)
    parent_id = _id_of_parent(parent)
    if not node.remove():
        raise CommandError("krita_error", f"Krita refused to remove layer {nid}")
    return ReinsertNode(node, layout, parent_id, anchor, old_index, ids, nbytes)


def _delete_one(doc, plan):
    return plan.ids, _take_out(_live(doc, plan.nid), plan.nid)


@command("delete_layers", timeout=LONG_TIMEOUT, mutating=True)
def cmd_delete_layers(params):
    """Delete layers; a group goes with its subtree. All or nothing (D11)."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    index = nodes.build_index(doc)
    nids = batch.validate_all(params.get("ids"), _check_delete_ref(index), field="ids")
    plans = [_Delete(nid, index.parent(nid), subtree(index, nid)) for nid in _fold(index, nids)]
    ids = [i for p in plans for i in p.ids]
    if dry_run:
        return {"doc": handle, "dry_run": True, "would_delete": ids, "count": len(ids)}

    # Applied top first; a mid-apply error names the layer's place in ``ids``.
    positions = [nids.index(p.nid) for p in plans]
    outcome = batch.run(plans, _delete_one, doc, label="delete_layers", field="ids",
                        positions=positions)
    doc.refreshProjection()
    doc.waitForDone()
    warnings = []
    if not batch.JOURNALS.get(handle).push("delete_layers", outcome.inverses, outcome.nbytes):
        warnings.append({
            "code": "not_undoable",
            "message": f"The deleted layers take about {outcome.nbytes} bytes, over the undo "
                       "journal's memory cap (deleted layers can't be spilled to disk): this "
                       "delete can't be undone, and the agent undo journal was cleared",
            "nbytes": outcome.nbytes})
    deleted = [i for r in outcome.results for i in r]
    return {"doc": handle, "dry_run": False, "deleted": deleted, "count": len(deleted),
            "warnings": warnings}


# --- set_active_layer -------------------------------------------------------

def _required_ref(params, key="id"):
    ref = params.get(key)
    if ref is None:
        raise _bad(f"'{key}' is required (a layer ref)", param=key)
    return ref


@command("set_active_layer")
def cmd_set_active_layer(params):
    """Make a layer (any type, masks included) the document's active node:
    UI state, so no journal entry. Painting tools called without ``layer``
    paint on it, but without ``doc`` they use the active document."""
    handle, doc = registry.resolve_doc(params)
    index = nodes.build_index(doc)
    nid = refs.resolve(index, _required_ref(params), field="id")
    doc.setActiveNode(_live(doc, nid))
    warnings = []
    active = registry.active_live()
    if active is None or not active == doc:
        warnings.append({
            "code": "not_active_document",
            "message": f"{handle} is not the active document: painting tools called without "
                       f"'doc' still paint on the active document's active layer; pass "
                       f"doc={handle!r} (and 'layer') to paint here"})
    return {"doc": handle, "layer": nid, "path": index.path_of(nid),
            "type": index.get(nid)["type"], "warnings": warnings}


# --- duplicate_layer --------------------------------------------------------
#
# Verified headless (krita-api-notes § Nodes, 2026-11-25): Node.duplicate()
# returns a detached deep copy: the same name (no " (copy)" suffix), the
# properties, the pixels (past the canvas edge too) and the whole subtree
# (nested groups, masks), every node with a new UUID. addChildNode inserts
# it with its subtree.

MASK_TYPES = frozenset({"transparencymask", "filtermask", "transformmask", "colorizemask",
                        "selectionmask"})


@dataclasses.dataclass
class _Duplicate:
    source: str
    name: object    # None: Krita's (the source's name)
    parent: str     # refs.ROOT or a group id
    above: object   # None (on top) or the sibling id to go directly above


def _plan_duplicate(index, params):
    source = refs.resolve(index, _required_ref(params), field="id")
    kind = index.get(source)["type"]
    if kind in MASK_TYPES:
        raise CommandError("conflict", f"Layer {index.path_of(source)!r} is a {kind}; "
                           "duplicate_layer copies layers (duplicating a layer copies its masks)",
                           {"id": source, "type": kind})
    name = params.get("name")
    if name is not None:
        name = layerprops.check_name(name)
    parent, above = placement(index, params, source)
    return _Duplicate(source, name, parent, above)


def placement(index, params, source, what="the copy"):
    """``(parent, above)`` for a new layer made from ``source`` (the
    ``duplicate_layer`` / ``selection_to_layer`` rule): neither ``parent``
    nor ``above`` in ``params``: directly above the source, in its parent;
    ``parent`` (``/`` or a group): on top of its children; ``above``:
    directly above that sibling, a child of ``parent`` or, without it, of
    the source's parent. ``above`` None means on top."""
    if params.get("parent") is not None:
        parent_ref = params["parent"]
        parent = refs.resolve(index, parent_ref, allow_root=True, field="parent")
        if parent != refs.ROOT and index.get(parent)["type"] != GROUP:
            raise _bad(f"parent {parent_ref!r} is a {index.get(parent)['type']}; only a group "
                       "or the root '/' can hold layers", param="parent")
        above = None
    else:
        parent, above = index.parent(source), source
    if params.get("above") is not None:
        above_ref = params["above"]
        above = refs.resolve(index, above_ref, field="above")
        if index.parent(above) != parent:
            where = "the root '/'" if parent == refs.ROOT else f"group {index.path_of(parent)!r}"
            raise _bad(f"above {above_ref!r} is not a child of the target parent, {where} "
                       f"(pass 'parent' to put {what} in another group)", param="above")
    return parent, above


def _duplicate_one(doc, plan):
    """``apply_fn``: copy, rename, insert. A copy that fails after it was
    inserted is removed again (the failing item cleans up after itself)."""
    copy = _live(doc, plan.source).duplicate()
    if copy is None:
        raise CommandError("krita_error", f"Krita could not duplicate layer {plan.source}")
    nid = nodes.node_id(copy)
    if plan.name is not None:
        copy.setName(plan.name)  # while detached
    parent = _parent_node(doc, plan.parent)
    above = None if plan.above is None else _position(parent, plan.above)[0]
    _add(parent, copy, above)
    try:
        if nodes.node_by_id(doc, nid) is None:  # addChildNode can report success and do nothing
            raise CommandError("krita_error", "Krita did not insert the copy")
        if plan.name is not None and copy.name() != plan.name:
            raise CommandError("krita_error", f"Krita did not rename the copy to {plan.name!r}")
    except Exception:
        try:
            _remove_node(doc, nid)
        except Exception:
            print(f"[KritaMCP] duplicate_layer: could not remove the half-made copy {nid}:")
            traceback.print_exc()
        raise
    return nid, RemoveNode(nid)


def _duplicate_warnings(name, nid, others, copied_names):
    """``duplicate_name`` for the copy itself when another layer has its
    name (``others``: their ids), and ``duplicate_names_in_copy`` for the
    copied subtree, whose layers keep their names (D4: bare-name refs to
    them become ambiguous)."""
    warnings = []
    if others:
        warnings.append({
            "code": "duplicate_name",
            "message": f"The copy is named {name!r}, like {len(others)} other "
                       f"layer{'s' if len(others) != 1 else ''}; a bare-name ref to it is "
                       "ambiguous (use its id or a path, or pass 'name')",
            "id": nid, "name": name, "others": others})
    if copied_names:
        warnings.append({
            "code": "duplicate_names_in_copy",
            "message": f"The {len(copied_names)} layer(s) inside the copy keep their names, "
                       "which the original's layers have too; refer to them by id or path",
            "count": len(copied_names), "names": sorted(set(copied_names))})
    return warnings


@command("duplicate_layer", timeout=LONG_TIMEOUT, mutating=True)
def cmd_duplicate_layer(params):
    """Copy a layer (a group with its subtree, a layer with its masks):
    directly above the source by default, or under ``parent`` / above
    ``above``. One journal entry; ``dry_run`` validates only."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    index = nodes.build_index(doc)
    plan = _plan_duplicate(index, params)
    name = plan.name if plan.name is not None else index.get(plan.source)["name"]
    inner = subtree(index, plan.source)[1:]
    if dry_run:
        warnings = _duplicate_warnings(name, None, index.by_name(name),
                                       [index.get(i)["name"] for i in inner])
        return {"doc": handle, "dry_run": True, "source": plan.source,
                "would_create": {"name": name, "type": index.get(plan.source)["type"],
                                 "parent": plan.parent, "above": plan.above,
                                 "count": 1 + len(inner)},
                "warnings": warnings}

    outcome = batch.run([plan], _duplicate_one, doc, label="duplicate_layer", field="id")
    doc.refreshProjection()
    doc.waitForDone()
    batch.JOURNALS.get(handle).push("duplicate_layer", outcome.inverses)  # no snapshots
    nid = outcome.results[0]
    fresh, by_id = nodes.tree(doc)
    ids = subtree(fresh, nid)
    warnings = _duplicate_warnings(name, nid, [o for o in fresh.by_name(name) if o != nid],
                                   [fresh.get(i)["name"] for i in ids[1:]])
    return {"doc": handle, "dry_run": False, "source": plan.source,
            "layer": nodes.layer_object(by_id[nid], fresh, nid=nid), "ids": ids,
            "warnings": warnings}
