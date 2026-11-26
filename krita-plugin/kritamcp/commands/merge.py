"""merge_layers (api-design § krita_merge_layers).

- **One group**: flattened into one paint layer at its place. The pixels
  are the group's projection (spike Q8: it includes the clipping, filters,
  masks and layer styles inside the group, and the group's own masks, but
  not the group's opacity), so the new layer takes the group's opacity,
  blend mode, visibility, clipping (inherit alpha) and lock as
  properties. Krita keeps a projection only of an isolated group that is
  visible with its ancestors (P3.2); any other group (pass-through,
  hidden, or inside a hidden group) is read from a ``clone()`` of the
  document with it made isolated and visible, as ``export_layers`` does.
- **Several layers** (siblings): in a ``clone()``, they are moved, in their
  stacking order, into a temporary isolated group directly above the
  topmost one (ancestors made visible), and that group's projection is
  read. The user's document never holds the temporary group: there, the
  new layer is inserted directly above the topmost layer and the merged
  layers are removed. Their opacity and blend modes are baked into the
  pixels; the new layer is normal, opacity 1, visible.

Applied as batch items in the user's document: create the new layer, then
remove each merged layer, top first (``_take_out``: the layer with its
subtree). One journal entry: ``RemoveNode`` of the new layer and a
``ReinsertNode`` per removed layer (P5.1 protocol: undo puts the original
nodes back with their ids, pixels and properties; redo re-inserts the same
merged layer). Clones are closed in ``finally`` and never touched after.
"""

import dataclasses
import traceback

from .. import batch, layerprops, nodes, pixels, refs, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from .layers import (GROUP, MASK_TYPES, PAINT, RemoveNode, _add, _bad, _detach, _dry_run,
                     _duplicate_warnings, _insert, _live, _parent_node, _position, _remove_node,
                     _strict_item, _take_out, _with_notes, subtree)
from .pixels import _bounds, _clone_node, _isolate, _write

KEYS = ("doc", "ids", "name", "dry_run")
TEMP_GROUP = "kmcp-merge-temp"


@dataclasses.dataclass
class _Merge:
    mode: str        # "group" or "layers"
    ids: list        # the merged layers, top first
    parent: str      # their parent (refs.ROOT or a group id)
    name: str
    removed: list    # ids and subtrees, top to bottom
    props: dict      # the new layer's (layerprops.apply form)
    style: str       # ASL to copy onto the new layer, or ""


def _ids(index, params):
    given = params.get("ids")
    if not isinstance(given, list) or not given:
        raise _bad("'ids' must be a non-empty list of layer refs: one group, or several "
                   "layers with the same parent", param="ids")
    batch.check_items(given, "ids")
    out = []
    for i, ref in enumerate(given):
        if ref == refs.ROOT:
            raise _bad(f"ids[{i}]: the root '/' can't be merged", param="ids", index=i)
        nid = refs.resolve(index, ref, field=f"ids[{i}]")
        if nid in out:
            raise _bad(f"ids[{i}]: {ref!r} is listed twice", param="ids", index=i)
        kind = index.get(nid)["type"]
        if kind in MASK_TYPES:
            raise CommandError("conflict", f"ids[{i}]: {index.path_of(nid)!r} is a {kind}; "
                               "merge_layers merges layers (a layer's masks are baked in with "
                               "it)", {"index": i, "id": nid, "type": kind})
        out.append(nid)
    return out


def _plan(doc, index, by_id, params):
    params = _strict_item(params, KEYS)
    if params.get("name") is None:
        raise _bad("'name' is required (the merged layer's name)", param="name")
    name = layerprops.check_name(params["name"])
    ids = sorted(_ids(index, params), key=index.order)
    if len(ids) == 1:
        nid = ids[0]
        if index.get(nid)["type"] != GROUP:
            raise _bad(f"{index.path_of(nid)!r} is a {index.get(nid)['type']}; give one group "
                       "(to flatten it) or several layers with the same parent", param="ids")
        group = by_id[nid]
        props = {"visible": bool(group.visible()), "opacity": group.opacity() / 255,
                 "blend_mode": group.blendingMode(), "inherit_alpha": bool(group.inheritAlpha()),
                 "locked": bool(group.locked())}
        return _Merge("group", ids, index.parent(nid), name, subtree(index, nid), props,
                      group.layerStyleToAsl() or "")
    parents = {index.parent(nid) for nid in ids}
    if len(parents) > 1:
        raise _bad("the layers to merge must have the same parent; they are in "
                   f"{len(parents)} different groups", param="ids",
                   parents=sorted(parents, key=lambda p: -1 if p == refs.ROOT else index.order(p)))
    removed = [i for nid in ids for i in subtree(index, nid)]
    return _Merge("layers", ids, parents.pop(), name, removed, {}, "")


# --- pixels ---------------------------------------------------------------------

def _read(node):
    """``(rect, bytes)`` of ``node``'s projection over its bounds, or None."""
    rect = _bounds(node)
    if rect is None:
        return None
    data = pixels.as_bytes(node.projectionPixelData(*rect))
    if len(data) != 4 * rect[2] * rect[3]:
        raise CommandError("krita_error", f"Krita returned {len(data)} bytes for a "
                           f"{rect[2]}x{rect[3]} projection")
    return rect, data


def _as_is(index, by_id, gid):
    """Krita keeps an up-to-date projection of group ``gid`` (P3.2)."""
    if by_id[gid].passThroughMode():
        return False
    return all(by_id[n].visible() for n in [gid] + index.ancestors(gid))


def _temp_group_projection(clone, index, plan):
    """In ``clone``: the merged layers moved, bottom first, into a new
    isolated group directly above the topmost one (ancestors visible); its
    projection. Its own frame, so no wrapper outlives the clone."""
    parent = _parent_node(clone, plan.parent)
    if plan.parent != refs.ROOT:
        for a in [plan.parent] + index.ancestors(plan.parent):
            _clone_node(clone, a).setVisible(True)
    group = clone.createGroupLayer(TEMP_GROUP)
    if group is None:
        raise CommandError("krita_error", "Krita could not create the temporary group")
    _add(parent, group, _position(parent, plan.ids[0])[0])
    for nid in reversed(plan.ids):  # bottom first: each goes on top
        node = _clone_node(clone, nid)
        layout = _detach(node)
        _insert(group, node, layout)
    clone.refreshProjection()
    clone.waitForDone()
    return _read(group)


def _group_projection(clone, index, plan):
    _isolate(clone, index, plan.ids)
    clone.refreshProjection()
    clone.waitForDone()
    return _read(_clone_node(clone, plan.ids[0]))


def _from_clone(doc, index, plan):
    clone = doc.clone()
    if clone is None:
        raise CommandError("krita_error", "Krita could not copy the document to merge in")
    try:
        read = _group_projection if plan.mode == "group" else _temp_group_projection
        return read(clone, index, plan)
    finally:
        try:
            clone.setModified(False)  # close() never prompts then
        finally:
            clone.close()
        del clone  # crash hazard: never touch a closed document's wrapper


def merged_pixels(doc, index, by_id, plan):
    """``(rect, BGRA bytes)`` of the merged result, or None when nothing is
    visible. Reads only: the user's document is not changed."""
    doc.waitForDone()
    if plan.mode == "group" and _as_is(index, by_id, plan.ids[0]):
        doc.refreshProjection()
        doc.waitForDone()
        return _read(by_id[plan.ids[0]])
    return _from_clone(doc, index, plan)


# --- apply ----------------------------------------------------------------------

def _create(doc, plan, merged):
    node = doc.createNode(plan.name, PAINT)
    if node is None:
        raise CommandError("krita_error", f"Krita could not create layer {plan.name!r}")
    nid = nodes.node_id(node)
    parent = _parent_node(doc, plan.parent)
    _add(parent, node, _position(parent, plan.ids[0])[0])  # directly above the topmost
    try:
        if nodes.node_by_id(doc, nid) is None:  # addChildNode can report success and do nothing
            raise CommandError("krita_error", "Krita did not insert the merged layer")
        if merged is not None:
            _write(node, merged[1], merged[0], f"the merged layer {plan.name!r}")
        layerprops.apply(node, plan.props, locked=False)
        if plan.style:
            node.setLayerStyleFromAsl(plan.style)
        layerprops.apply_locked(node, plan.props)
    except Exception:
        try:
            _remove_node(doc, nid)
        except Exception:
            print(f"[KritaMCP] merge_layers: could not remove the half-made layer {nid}:")
            traceback.print_exc()
        raise
    return nid, RemoveNode(nid)


def _remove(doc, nid):
    inverse = _take_out(_live(doc, nid), nid)
    return list(inverse.node_ids), inverse


def _applier(plan, merged):
    def apply_one(doc, item):
        if item is None:
            return _create(doc, plan, merged)
        return _remove(doc, item)
    return apply_one


def _warnings(index, by_id, plan):
    out = []
    if plan.mode == "group":
        group = by_id[plan.ids[0]]
        if group.passThroughMode():
            out.append({
                "code": "pass_through_flattened",
                "message": "The group was pass-through: its flattened pixels are its isolated "
                           "composite, so layers inside it with blend modes (or filters) no "
                           "longer blend with the layers below the group, and it may render "
                           "differently", "id": plan.ids[0]})
        return out
    kids = index.children(plan.parent)  # top to bottom
    pos = sorted(kids.index(nid) for nid in plan.ids)
    between = [kids[i] for i in range(pos[0], pos[-1] + 1) if kids[i] not in plan.ids]
    if between:
        out.append({
            "code": "not_adjacent",
            "message": f"{len(between)} layer(s) between the merged ones are not merged; they "
                       "end up below the merged layer", "ids": between})
    isolated = [nid for nid in plan.ids
                if by_id[nid].blendingMode() != "normal" or index.get(nid)["type"] == "filterlayer"]
    if isolated:
        out.append({
            "code": "isolated_compositing",
            "message": f"{len(isolated)} merged layer(s) have a blend mode other than normal "
                       "(or are filter layers): inside the merge they only blend with the merged "
                       "layers below them, so the result renders differently wherever those are "
                       "not opaque", "ids": isolated})
    bottom = plan.ids[-1]
    if by_id[bottom].inheritAlpha():
        out.append({
            "code": "clipping_base_not_merged",
            "message": "The lowest merged layer is clipped (inherit alpha) to a layer that is "
                       "not merged; inside the merge it has nothing to clip to and drops out",
            "id": bottom})
    hidden = [nid for nid in plan.ids if not by_id[nid].visible()]
    if hidden:
        out.append({
            "code": "hidden_layers_dropped",
            "message": f"{len(hidden)} merged layer(s) are hidden: they don't show in the "
                       "merged pixels and are removed", "ids": hidden})
    return out


@command("merge_layers", timeout=LONG_TIMEOUT, mutating=True)
def cmd_merge_layers(params):
    """Flatten one group, or merge several sibling layers, into one paint
    layer at the (topmost) merged layer's place. One journal entry;
    ``dry_run`` validates only."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    index, by_id = nodes.tree(doc)
    plan = _plan(doc, index, by_id, params)
    warnings = _warnings(index, by_id, plan)
    others = [o for o in index.by_name(plan.name) if o not in plan.removed]
    head = {"doc": handle, "mode": plan.mode, "merged": plan.ids}
    if dry_run:
        clash, notes = _duplicate_warnings(index, plan.name, None, "layer", others, [])
        return _with_notes({**head, "dry_run": True,
                            "would_create": {"name": plan.name, "type": PAINT, "parent": plan.parent,
                                             "above": plan.ids[0]},
                            "would_remove": plan.removed, "warnings": warnings + clash}, notes)

    merged = merged_pixels(doc, index, by_id, plan)
    del by_id  # this walk's wrappers; the apply looks nodes up again
    # Steps: create the merged layer, then remove each merged layer, top first.
    outcome = batch.run([None] + plan.ids, _applier(plan, merged), doc, label="merge_layers",
                        field="steps")
    doc.refreshProjection()
    doc.waitForDone()
    nid = outcome.results[0]
    removed = [i for r in outcome.results[1:] for i in r]
    if merged is None:
        warnings.append({"code": "nothing_visible",
                         "message": "Nothing of the merged layers is visible: the new layer is "
                                    "empty", "id": nid})
    if not batch.JOURNALS.get(handle).push("merge_layers", outcome.inverses, outcome.nbytes):
        warnings.append({
            "code": "not_undoable",
            "message": f"The removed layers take about {outcome.nbytes} bytes, over the undo "
                       "journal's memory cap (removed layers can't be spilled to disk): this "
                       "merge can't be undone, and the agent undo journal was cleared",
            "nbytes": outcome.nbytes})
    fresh, fresh_by_id = nodes.tree(doc)
    clash, notes = _duplicate_warnings(fresh, plan.name, nid, "layer",
                                       [o for o in fresh.by_name(plan.name) if o != nid], [])
    return _with_notes({**head, "dry_run": False,
                        "layer": nodes.layer_object(fresh_by_id[nid], fresh, nid=nid),
                        "removed": removed, "warnings": warnings + clash}, notes)
