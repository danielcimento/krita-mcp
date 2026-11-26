"""transform_layer (api-design § krita_transform_layer).

A paint layer, or a group of paint layers (nested groups allowed), is
flipped, scaled, rotated and translated, in that order, about a pivot.

Verified headless (krita-api-notes § Nodes, 2026-11-25), which decides
the path of each step:

- ``Node.scaleNode(origin, w, h, filter)`` and ``rotateNode(radians)``
  work headless (no view, no dialog) and on groups too: they transform
  every node of the subtree with one transform (children keep their
  places relative to each other). ``scaleNode`` keeps ``origin`` fixed,
  ``rotateNode`` turns about the centre of ``bounds()`` (positive radians
  clockwise on screen); a quarter turn is an exact pixel permutation. So
  scale and rotate are Krita's, with ``resample`` as the filter; a custom
  pivot for a turn is the turn, then a move by the pivot's displacement.
- ``Node.move`` on a paint layer shifts its pixels (relative to
  ``position()``), but on a group it changes nothing visible (the
  children stay put), so a group is translated by moving each paint layer
  in it.
- A flip is ours: ``QImage.mirrored`` over each paint layer's bounds,
  written back mirrored about the axis, so it is exact.
- A hidden pass-through group reports empty ``bounds()``, which
  ``scaleNode`` divides by; such a group is made isolated for the call.

Journal: one entry. The flip / scale / rotate part is a
``RestorePixels`` per paint layer, holding its pixels over its old bounds
plus transparent patches for the parts of its new bounds outside them
(Krita's bounds are exact for alpha > 0, so that restores every visible
pixel and the bounds exactly). The translate part is a ``MoveBy`` of the
paint layers (exact: it moves colour under alpha 0 too). Both follow the
P5.1 protocol: applying returns the reverse.
"""

import contextlib
import math
import traceback

from PyQt5.QtCore import QPointF

from .. import batch, nodes, pixels, refs, registry, transformplan
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from .layers import GROUP, PAINT, _dry_run, _live, _required_ref, _strict_item, subtree
from .pixels import RestorePixels, _bounds, _rect, _write

KEYS = ("doc", "id", "translate", "scale", "rotate", "flip", "pivot", "resample", "dry_run")


def _settle(doc):
    doc.waitForDone()
    doc.refreshProjection()
    doc.waitForDone()


def _rect_or_none(r):
    return None if r is None else _rect(*r)


class Composite:
    """Several independent inverses as one (batch.py's protocol): applied
    newest first, each returning its reverse; ``check`` is the first
    reason any part gives. If a part fails, the parts already applied are
    reversed again before re-raising."""

    __slots__ = ("parts",)

    def __init__(self, parts):
        self.parts = tuple(parts)

    @property
    def node_ids(self):
        return tuple(i for p in self.parts for i in getattr(p, "node_ids", ()))

    @property
    def nbytes(self):
        return sum(batch.nbytes_of(p) for p in self.parts)

    @property
    def blobs(self):
        return tuple(b for p in self.parts for b in batch.blobs_of(p))

    def check(self, doc):
        for p in self.parts:
            reason = batch.check_of(p, doc)
            if reason is not None:
                return reason
        return None

    def __call__(self, doc):
        produced = []
        try:
            for p in reversed(self.parts):
                produced.append(p(doc))
        except Exception:
            done = [p for p in produced if p is not None]
            _, errors = batch.rollback(done, doc)  # the last one applied first
            for e in errors:
                print(f"[KritaMCP] transform: could not put a step back: {e['error']}")
            raise
        return Composite(reversed([p for p in produced if p is not None]))

    def __repr__(self):
        return f"Composite({', '.join(map(repr, self.parts))})"


class MoveBy:
    """Translate the paint layers ``node_ids`` by ``(dx, dy)`` (``Node.move``
    relative to ``position()``), looked up in the document passed at call
    time. Returns the ``MoveBy`` back."""

    __slots__ = ("node_ids", "dx", "dy")

    def __init__(self, nids, dx, dy):
        self.node_ids, self.dx, self.dy = tuple(nids), dx, dy

    def check(self, doc):
        for nid in self.node_ids:
            node = nodes.node_by_id(doc, nid)
            if node is None:
                return f"Layer {nid} is not in the document (any more)"
            if node.type() != PAINT:
                return f"Layer {nid} is a {node.type()}, not a paint layer"
        return None

    def __call__(self, doc):
        layers = [_live(doc, nid) for nid in self.node_ids]
        for node in layers:
            _move(node, self.dx, self.dy)
        doc.waitForDone()
        return MoveBy(self.node_ids, -self.dx, -self.dy)

    def __repr__(self):
        return f"MoveBy({len(self.node_ids)} layers, {self.dx}, {self.dy})"


def _move(node, dx, dy):
    p = node.position()
    node.move(p.x() + dx, p.y() + dy)


# --- validation ----------------------------------------------------------------

class _Plan:
    __slots__ = ("nid", "kind", "path", "spec", "paint", "box", "steps", "predicted")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def _check_target(index, nid):
    """The paint layers the transform moves: ``[nid]`` for a paint layer,
    every paint layer under a group. Masks and other layer types (or a
    paint layer with masks, a group holding anything but paint layers and
    groups) are ``conflict``: Krita's transforms would change them too, and
    the journal could not put them back."""
    kind = index.get(nid)["type"]
    path = index.path_of(nid)
    if kind not in (PAINT, GROUP):
        raise CommandError("conflict", f"Layer {path!r} is a {kind}; transform_layer transforms "
                           "paint layers and groups of paint layers", {"id": nid, "type": kind})
    inner = subtree(index, nid)[1:]
    other = sorted({index.get(i)["type"] for i in inner} - {PAINT, GROUP})
    if kind == PAINT and inner:
        raise CommandError("conflict", f"Layer {path!r} has masks ({', '.join(other)}); "
                           "transform_layer can't transform (or undo) masks: apply or delete "
                           "them first", {"id": nid, "types": other})
    if other:
        bad = [i for i in inner if index.get(i)["type"] not in (PAINT, GROUP)]
        raise CommandError("conflict", f"Group {path!r} holds {', '.join(other)} layers; "
                           "transform_layer transforms groups of paint layers (and groups) only",
                           {"id": nid, "types": other, "layers": bad})
    if kind == PAINT:
        return kind, path, [nid]
    return kind, path, [i for i in inner if index.get(i)["type"] == PAINT]


def _box(node, kind, paint_nodes):
    """The target's bounds as ``(x, y, w, h)``: ``bounds()``; for a group
    whose ``bounds()`` reads empty (hidden pass-through), the union of its
    paint layers'. None when there are no pixels."""
    own = _bounds(node)
    if own is not None or kind != GROUP:
        return own
    box = None
    for n in paint_nodes:
        box = transformplan.union(box, _bounds(n))
    return box


def _plan(doc, params):
    params = _strict_item(params, KEYS)
    spec = transformplan.parse(params)
    index, by_id = nodes.tree(doc)
    ref = _required_ref(params)
    if ref == refs.ROOT:
        raise CommandError("bad_request", "the root '/' can't be transformed; give a layer",
                           {"param": "id"})
    nid = refs.resolve(index, ref, field="id")
    kind, path, paint = _check_target(index, nid)
    if spec.pixel_steps:
        pixels.require_rgba8(doc)
    doc.waitForDone()
    box = _box(by_id[nid], kind, [by_id[i] for i in paint])
    if box is None:
        raise CommandError("conflict", f"Layer {path!r} has no pixels to transform",
                           {"id": nid})
    steps, predicted = transformplan.predict(box, spec)
    for step in steps:
        transformplan.check_size(step["bounds"])
    return _Plan(nid=nid, kind=kind, path=path, spec=spec, paint=paint, box=box, steps=steps,
                 predicted=predicted)


# --- apply ---------------------------------------------------------------------

@contextlib.contextmanager
def _krita_bounds(doc, node, kind):
    """``node``'s bounds as Krita's transforms read them. A group that
    reads empty while it has pixels (a hidden pass-through group) is made
    isolated for the ``with`` block, which gives it bounds again."""
    rect = _bounds(node)
    toggled = False
    if rect is None and kind == GROUP and node.passThroughMode():
        node.setPassThroughMode(False)
        toggled = True
        _settle(doc)
        rect = _bounds(node)
    try:
        if rect is None:
            raise CommandError("krita_error", "Krita reports no bounds for the layer to "
                               "transform")
        yield rect
    finally:
        if toggled:
            node.setPassThroughMode(True)
            _settle(doc)


def _mirror(data, w, h, axis):
    image = pixels.wrap(data, w, h).mirrored(axis == "h", axis == "v")
    return pixels.from_qimage(image)[0]


def _flip(doc, plan, pivot):
    """Mirror every paint layer about the axis through ``pivot`` (or the
    target's centre): its pixels over its bounds, mirrored, written where
    they land; what they leave is cleared."""
    axis = plan.spec.flip
    box = plan.box  # the flip is the first step
    axis2 = transformplan.flip_axis2(box, axis, None if plan.spec.pivot == "center" else pivot)
    for nid in plan.paint:
        node = _live(doc, nid)
        old = _bounds(node)
        if old is None:
            continue
        data = pixels.as_bytes(node.pixelData(*old))
        new = transformplan.flipped(old, axis, axis2)
        what = f"layer {nid}"
        for r in transformplan.minus(old, new):
            _write(node, bytes(4 * r[2] * r[3]), r, what)
        _write(node, _mirror(data, old[2], old[3], axis), new, what)
    new_box = transformplan.flipped(box, axis, axis2)
    return {"op": "flip", "axis": axis, "shift": [new_box[0] - box[0], new_box[1] - box[1]]}


def _scale(doc, plan, pivot):
    target = _live(doc, plan.nid)
    with _krita_bounds(doc, target, plan.kind) as rect:
        size = transformplan.scaled_size(rect[2], rect[3], *plan.spec.scale)
        target.scaleNode(QPointF(*pivot), size[0], size[1], plan.spec.resample)
        doc.waitForDone()
    return transformplan.scale_step(rect, size, plan.spec.resample)


def _rotate(doc, plan, pivot):
    target = _live(doc, plan.nid)
    turn = plan.spec.turn
    with _krita_bounds(doc, target, plan.kind) as rect:
        target.rotateNode(math.radians(turn))
        doc.waitForDone()
    shift = transformplan.rotation_shift(rect, pivot, turn)
    if shift != (0, 0):
        for nid in plan.paint:
            _move(_live(doc, nid), *shift)
        doc.waitForDone()
    return {"op": "rotate", "degrees": plan.spec.rotate, "shift": list(shift)}


def _restore_op(doc, snapshots):
    """The inverse of the pixel steps: per paint layer, transparent patches
    over the parts of its bounds now outside its old bounds, then its old
    pixels over those (``RestorePixels`` writes patches in order)."""
    parts = []
    for nid, old, data in snapshots:
        now = _bounds(_live(doc, nid))
        patches = [(r, bytes(4 * r[2] * r[3])) for r in transformplan.minus(now, old)]
        if old is not None:
            patches.append((old, data))
        if patches:
            parts.append(RestorePixels(nid, patches))
    return Composite(parts) if parts else None


def _pixel_steps(doc, plan):
    """``apply_fn`` item for flip / scale / rotate: snapshots, then the
    steps; on failure the snapshots are written back (the failing item
    cleans up after itself, batch.py)."""
    spec = plan.spec
    pivot = transformplan.pivot_point(spec, plan.box)
    snapshots = []
    for nid in plan.paint:
        node = _live(doc, nid)
        old = _bounds(node)
        snapshots.append((nid, old, None if old is None else pixels.as_bytes(node.pixelData(*old))))
    applied = []
    try:
        if spec.flip is not None:
            applied.append(_flip(doc, plan, pivot))
            _settle(doc)
        if spec.scale is not None:
            applied.append(_scale(doc, plan, pivot))
            _settle(doc)
        if spec.rotate is not None:
            applied.append(_rotate(doc, plan, pivot))
            _settle(doc)
    except Exception:
        try:
            inverse = _restore_op(doc, snapshots)
            if inverse is not None:
                inverse(doc)
                _settle(doc)
        except Exception:
            print(f"[KritaMCP] transform_layer: could not restore layer {plan.nid}:")
            traceback.print_exc()
        raise
    return applied, _restore_op(doc, snapshots)


def _translate(doc, plan):
    dx, dy = plan.spec.translate
    op = MoveBy(plan.paint, dx, dy)
    back = op(doc)
    return [{"op": "translate", "by": [dx, dy]}], back


def _apply(doc, item):
    kind, plan = item
    return _pixel_steps(doc, plan) if kind == "pixels" else _translate(doc, plan)


@command("transform_layer", timeout=LONG_TIMEOUT, mutating=True)
def cmd_transform_layer(params):
    """Flip, scale, rotate and translate a paint layer or a group of paint
    layers, in that order, about ``pivot``. One journal entry; ``dry_run``
    predicts the bounds."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    plan = _plan(doc, params)
    head = {"doc": handle, "layer": plan.nid, "path": plan.path, "type": plan.kind,
            "bounds_before": _rect(*plan.box)}
    if dry_run:
        steps = [{**s, "bounds": _rect(*s["bounds"])} for s in plan.steps]
        return {**head, "dry_run": True, "would_apply": steps,
                "bounds_predicted": _rect(*plan.predicted), "warnings": []}

    items = []
    if plan.spec.pixel_steps:
        items.append(("pixels", plan))
    if plan.spec.translate is not None:
        items.append(("translate", plan))
    outcome = batch.run(items, _apply, doc, label="transform_layer", field="id")
    _settle(doc)
    warnings = []
    if not batch.JOURNALS.get(handle).push("transform_layer", outcome.inverses, outcome.nbytes):
        warnings.append({
            "code": "not_undoable",
            "message": f"The old pixels take {outcome.nbytes} bytes, over the undo journal's "
                       "memory and disk caps: this transform can't be undone, and the agent "
                       "undo journal was cleared",
            "nbytes": outcome.nbytes})
    applied = [s for r in outcome.results for s in r]
    target = _live(doc, plan.nid)
    after = _box(target, plan.kind, [_live(doc, i) for i in plan.paint])
    return {**head, "dry_run": False, "bounds_after": _rect_or_none(after), "applied": applied,
            "warnings": warnings}
