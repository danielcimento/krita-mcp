"""Layer-tree commands (api-design § Layer tree): list_layers so far.

Node wrappers come from one ``nodes.tree`` walk per command and are never
kept after it (they are only valid while the document is open).
"""

from .. import nodes, refs, registry
from ..dispatch import command
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
