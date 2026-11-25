"""Layer-tree commands (api-design § Layer tree): list_layers, create_layers.

Node wrappers come from one ``nodes.tree`` walk per command and are never
kept after it (they are only valid while the document is open).
"""

import dataclasses
import traceback

from .. import batch, config, layerprops, nodes, paths, pixels, refs, registry
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


def _bad(message, **details):
    return CommandError("bad_request", message, details or None)


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
        if not isinstance(spec, dict):
            raise _bad(f"must be an object, got {type(spec).__name__}")
        # An explicit null means "not given" (the default applies).
        spec = {k: v for k, v in spec.items() if v is not None}
        unknown = sorted(k for k in spec if k not in SPEC_KEYS)
        if unknown:
            raise _bad(f"unknown key(s) {', '.join(map(repr, unknown))}; valid: {', '.join(SPEC_KEYS)}",
                       unknown=unknown, valid=list(SPEC_KEYS))
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
            _check_png_size(plan.size, spec["png"])
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


def _check_png_size(size, given):
    limits = config.current().limits
    w, h = size
    max_dim, max_pixels = limits["max_canvas_dim"], limits["max_canvas_pixels"]
    if max(w, h) > max_dim:
        raise CommandError("too_large",
                           f"PNG {given} is {w}x{h}; the limit is {max_dim} pixels per side "
                           "(max_canvas_dim)",
                           {"limit": "max_canvas_dim", "max": max_dim, "w": w, "h": h})
    if w * h > max_pixels:
        raise CommandError("too_large",
                           f"PNG {given} is {w}x{h} = {w * h} pixels; the limit is {max_pixels} "
                           "(max_canvas_pixels)",
                           {"limit": "max_canvas_pixels", "max": max_pixels, "pixels": w * h})


def _remove_node(doc, nid):
    """Remove the node with id ``nid`` from ``doc``, if it is there."""
    node = nodes.node_by_id(doc, nid)
    if node is None:
        return False
    if not node.remove():
        raise CommandError("krita_error", f"Krita refused to remove layer {nid}")
    return True


class RemoveNode:
    """The inverse of creating a node: ``RemoveNode(id)(doc)`` removes it
    from the live document passed at call time (batch.py: never a captured
    wrapper). ``node_ids`` lets undo check the node still exists."""

    __slots__ = ("node_id",)

    def __init__(self, nid):
        self.node_id = nid

    @property
    def node_ids(self):
        return (self.node_id,)

    def __call__(self, doc):
        _remove_node(doc, self.node_id)

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


@command("create_layers", timeout=LONG_TIMEOUT, mutating=True)
def cmd_create_layers(params):
    """Create groups and paint layers from ``specs``, in list order, all or
    nothing (D11). With ``dry_run`` only validate and describe."""
    handle, doc = registry.resolve_doc(params)
    dry_run = params.get("dry_run", False)
    if type(dry_run) is not bool:
        raise CommandError("bad_request", f"'dry_run' must be true or false, got {dry_run!r}")

    index, by_id = nodes.tree(doc)
    planner = _Planner(doc, index)
    plans = batch.validate_all(params.get("specs"), planner, field="specs")
    counts = _counts([p.kind for p in plans])
    if dry_run:
        return {"doc": handle, "dry_run": True,
                "would_create": [planner.describe(p) for p in plans], "counts": counts}

    outcome = batch.run(plans, _applier(doc, by_id), doc, label="create_layers", field="specs")
    doc.refreshProjection()
    doc.waitForDone()
    batch.JOURNALS.get(handle).push("create_layers", outcome.inverses, outcome.nbytes)

    fresh = nodes.build_index(doc)
    created = [{**r, "path": fresh.path_of(r["id"]) if r["id"] in fresh else None}
               for r in outcome.results]
    return {"doc": handle, "dry_run": False, "created": created, "counts": counts}
