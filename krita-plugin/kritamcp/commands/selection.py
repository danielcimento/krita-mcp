"""Selection commands (api-design § Selections and part separation): select,
selection_to_layer.

The document's global selection is a ``Selection`` set with
``Document.setSelection``; Krita shows it as a root-level ``Selection Mask``
node, which ``nodes.walk`` (so ``list_layers``, refs and lint) skips (D4).
Verified headless (krita-api-notes § Selections, 2026-11-25):

- ``Document.selection()`` returns the LIVE global selection, and the
  ``Selection`` passed to ``setSelection`` stays shared with the document:
  changing either changes the document's selection behind Krita's back. So
  a current selection is only read, or ``duplicate()``-d (a deep copy)
  before it is combined, and a selection is never touched after it is set.
- ``select(x, y, w, h, v)`` writes ``v`` into the rectangle (it adds, it
  doesn't replace), and selections may lie past the canvas, on a standalone
  ``Selection`` and as the document's selection.
- ``add`` is the clamped sum ``min(a + b, 255)``, ``subtract`` the clamped
  difference ``max(a - b, 0)``, ``intersect`` the minimum; for 0/255
  selections that is union, difference and intersection.
- ``grow``, ``shrink(r, r, False)`` and ``feather`` aren't clipped to the
  canvas; ``shrink`` treats everything outside the selection as unselected,
  so a selection at the canvas edge shrinks from it too. ``invert`` on a
  standalone selection only inverts inside its own extent, so ``invert`` is
  computed here, over the canvas.
- An empty selection set on the document keeps the ``Selection Mask`` node
  (with 0x0 bounds), so an empty result is set as ``None`` instead.

Selections are not in the agent journal: ``krita_undo`` doesn't undo a
``select``. ``selection_to_layer`` never calls ``Selection.cut`` / ``copy``
/ ``paste``, which go through the system clipboard (spike Q9).
"""

import math
import traceback

from krita import Selection
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QImage, QPainter, QPolygonF

from .. import alphastats, batch, config, layerprops, nodes, pixels, refs, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from .layers import (MASK_TYPES, PAINT, RemoveNode, _add, _bad, _dry_run, _duplicate_warnings,
                     _live, _parent_node, _position, _remove_node, _required_ref, _strict_item,
                     _with_notes, placement)
from .pixels import RestorePixels, _bounds, _rect, _write

MODES = ("rect", "polygon", "alpha_of", "invert", "none", "all")
OPS = ("replace", "add", "subtract", "intersect")
COMMON_KEYS = ("doc", "mode", "op", "grow", "shrink", "feather")
MODE_KEYS = {"rect": ("x", "y", "w", "h"), "polygon": ("points", "antialias"),
             "alpha_of": ("layer", "threshold"), "invert": (), "none": (), "all": ()}
MODIFIERS = ("grow", "shrink", "feather")
MAX_RADIUS = 500
MIN_POINTS, MAX_POINTS = 3, 10000

_INVERT = bytes(255 - v for v in range(256))
_ZERO_1 = bytes([1] + [0] * 255)
_NONZERO_1 = bytes([0] + [1] * 255)


# --- reading selections ----------------------------------------------------

def sel_rect(sel):
    """A ``Selection``'s ``(x, y, w, h)``, or None (no selection, or empty)."""
    if sel is None:
        return None
    w, h = sel.width(), sel.height()
    if w <= 0 or h <= 0:
        return None
    return sel.x(), sel.y(), w, h


def sel_bytes(sel, rect):
    """The selection's values over ``rect``, one byte per pixel."""
    x, y, w, h = rect
    data = pixels.as_bytes(sel.pixelData(x, y, w, h))
    if len(data) != w * h:
        raise CommandError("krita_error",
                           f"Krita returned {len(data)} bytes for a {w}x{h} selection read")
    return data


def _count(sel):
    """``(rect, pixel_count)``: the selection's bounds and how many pixels
    in them are selected at all (value > 0); ``(None, 0)`` when empty."""
    rect = sel_rect(sel)
    if rect is None:
        return None, 0
    data = sel_bytes(sel, rect)
    n = len(data) - data.count(0)
    return (rect, n) if n else (None, 0)


# --- select: validation ----------------------------------------------------

def _int(params, key, lo, hi, required=False, default=None, what="a whole number"):
    value = params.get(key, default)
    if value is None:
        if required:
            raise _bad(f"'{key}' is required", param=key)
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise _bad(f"'{key}' must be {what} from {lo} to {hi}, got {value!r}", param=key)
    return value


def _check_area(w, h, what):
    limits = config.current().limits
    max_dim, max_pixels = limits["max_canvas_dim"], limits["max_canvas_pixels"]
    if max(w, h) > max_dim:
        raise CommandError("too_large", f"The {what} is {w}x{h}; the limit is {max_dim} pixels "
                           "per side (max_canvas_dim)",
                           {"limit": "max_canvas_dim", "max": max_dim, "w": w, "h": h})
    if w * h > max_pixels:
        raise CommandError("too_large", f"The {what} is {w}x{h} = {w * h} pixels; the limit is "
                           f"{max_pixels} (max_canvas_pixels)",
                           {"limit": "max_canvas_pixels", "max": max_pixels, "pixels": w * h})


def _points(params):
    pts = params.get("points")
    if pts is None:
        raise _bad("'points' is required for mode 'polygon'", param="points")
    if not isinstance(pts, list) or not MIN_POINTS <= len(pts) <= MAX_POINTS:
        raise _bad(f"'points' must be a list of {MIN_POINTS} to {MAX_POINTS} [x, y] pairs",
                   param="points")
    out = []
    for i, p in enumerate(pts):
        if (not isinstance(p, (list, tuple)) or len(p) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not math.isfinite(v) or abs(v) > config.COORD for v in p)):
            raise _bad(f"points[{i}] must be [x, y], two numbers of canvas pixels within "
                       f"±{config.COORD}, got {p!r}", param="points", index=i)
        out.append((float(p[0]), float(p[1])))
    return out


def _check_select(doc, params):
    """The validated request, before anything changes: ``(mode, op, spec,
    modifiers)``; ``spec`` holds what the mode needs."""
    mode = params.get("mode")
    if mode is None:
        raise _bad(f"'mode' is required: one of {', '.join(MODES)}", param="mode")
    if not isinstance(mode, str) or mode not in MODES:
        raise _bad(f"'mode' must be one of {', '.join(MODES)}, got {mode!r}", param="mode",
                   valid=list(MODES))
    params = _strict_item(params, COMMON_KEYS + MODE_KEYS[mode])
    op = params.get("op", "replace")
    if not isinstance(op, str) or op not in OPS:
        raise _bad(f"'op' must be one of {', '.join(OPS)}, got {op!r}", param="op",
                   valid=list(OPS))
    if mode in ("invert", "none") and op != "replace":
        raise _bad(f"mode {mode!r} takes no 'op' (it acts on the whole current selection); "
                   f"got {op!r}", param="op")
    modifiers = {k: _int(params, k, 1, MAX_RADIUS, what="a whole number of pixels")
                 for k in MODIFIERS}
    if mode == "none":
        given = [k for k, v in modifiers.items() if v is not None]
        if given:
            raise _bad(f"mode 'none' takes no {given[0]!r}", param=given[0])

    spec = {}
    if mode == "rect":
        for k in ("x", "y"):
            spec[k] = _int(params, k, -config.COORD, config.COORD, required=True)
        for k in ("w", "h"):
            spec[k] = _int(params, k, 1, config.COORD, required=True)
        _check_area(spec["w"], spec["h"], "rectangle")
    elif mode == "polygon":
        spec["points"] = _points(params)
        aa = params.get("antialias", True)
        if type(aa) is not bool:
            raise _bad(f"'antialias' must be true or false, got {aa!r}", param="antialias")
        spec["antialias"] = aa
        x0, y0, w, h = _polygon_box(spec["points"])
        if w >= 1 and h >= 1:
            _check_area(w, h, "polygon's bounding box")
    elif mode == "alpha_of":
        index = nodes.build_index(doc)
        nid = refs.resolve(index, _required_ref(params, "layer"), field="layer")
        kind = index.get(nid)["type"]
        if kind in MASK_TYPES:
            raise CommandError("conflict", f"Layer {index.path_of(nid)!r} is a {kind}; "
                               "alpha_of takes a layer (a paint layer's own pixels, or any "
                               "other layer's projection)", {"id": nid, "type": kind})
        pixels.require_rgba8(doc)
        spec.update(layer=nid, type=kind,
                    threshold=_int(params, "threshold", 1, 255, default=1))
    return mode, op, spec, modifiers


# --- select: building the new selection --------------------------------------

def _polygon_box(points):
    """The pixel box around the polygon: ``(x0, y0, w, h)`` with the corners
    rounded outwards (w or h 0 for a degenerate polygon)."""
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    x0, y0 = math.floor(min(xs)), math.floor(min(ys))
    return x0, y0, math.ceil(max(xs)) - x0, math.ceil(max(ys)) - y0


def polygon_mask(points, antialias):
    """``(mask, (x0, y0, w, h))``: the polygon rasterized by ``QPainter``
    into a ``Format_Alpha8`` image over its bounding box, ``Qt.WindingFill``
    (non-zero: overlapping loops stay filled, as SVG's default), with or
    without antialiasing; one byte per pixel, 255 inside. Points are pixel
    corners: ``[[2, 3], [6, 3], [6, 8], [2, 8]]`` is exactly the rectangle
    ``x=2, y=3, w=4, h=5``. None for a degenerate polygon."""
    x0, y0, w, h = _polygon_box(points)
    if w < 1 or h < 1:
        return None
    image = QImage(w, h, QImage.Format_Alpha8)
    image.fill(0)
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, antialias)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0, 255))
        painter.translate(-x0, -y0)
        painter.drawPolygon(QPolygonF([QPointF(x, y) for x, y in points]), Qt.WindingFill)
    finally:
        painter.end()
    return pixels.alpha8_bytes(image), (x0, y0, w, h)


def _from_mask(mask, rect):
    sel = Selection()
    if mask is not None and mask.count(0) != len(mask):
        sel.setPixelData(mask, *rect)
    return sel


def _alpha_selection(doc, spec):
    node = _live(doc, spec["layer"])
    doc.waitForDone()
    bounds = _bounds(node)
    if bounds is None:
        return Selection()
    x, y, w, h = bounds
    if spec["type"] == PAINT:
        data = node.pixelData(x, y, w, h)
    else:
        data = node.projectionPixelData(x, y, w, h)
    data = pixels.as_bytes(data)
    if len(data) != 4 * w * h:
        raise CommandError("krita_error",
                           f"Krita returned {len(data)} bytes for a {w}x{h} layer read")
    table = bytes(255 if v >= spec["threshold"] else 0 for v in range(256))
    return _from_mask(data[3::4].translate(table), bounds)


def _new_selection(doc, mode, spec):
    """The selection the mode describes (not combined yet); a fresh
    ``Selection`` the document doesn't hold."""
    sel = Selection()
    if mode == "rect":
        sel.select(spec["x"], spec["y"], spec["w"], spec["h"], 255)
    elif mode == "all":
        sel.select(0, 0, doc.width(), doc.height(), 255)
    elif mode == "polygon":
        made = polygon_mask(spec["points"], spec["antialias"])
        if made is not None:
            sel = _from_mask(*made)
    elif mode == "alpha_of":
        sel = _alpha_selection(doc, spec)
    elif mode == "invert":
        canvas = (0, 0, doc.width(), doc.height())
        current = doc.selection()
        if sel_rect(current) is None:
            sel.select(*canvas, 255)  # inverting nothing selects everything
        else:
            sel = _from_mask(sel_bytes(current, canvas).translate(_INVERT), canvas)
    return sel


def _combine(doc, new, op):
    """``new`` combined with the document's current selection (a deep copy
    of it, so the live selection is untouched until ``setSelection``)."""
    if op == "replace":
        return new
    current = doc.selection()
    if sel_rect(current) is None:
        return new if op == "add" else Selection()
    result = current.duplicate()
    getattr(result, op)(new)  # add / subtract / intersect
    return result


@command("select")
def cmd_select(params):
    """Set the document's global selection: a rectangle, a polygon, a
    layer's alpha, the inverse of the current selection, nothing or
    everything, combined with the current one by ``op``, then grown, shrunk
    and feathered. Not in the agent journal."""
    handle, doc = registry.resolve_doc(params)
    mode, op, spec, modifiers = _check_select(doc, params)
    warnings = []
    if mode == "none":
        doc.setSelection(None)
        rect, count = None, 0
    else:
        sel = _combine(doc, _new_selection(doc, mode, spec), op)
        if sel_rect(sel) is not None:
            if modifiers["grow"]:
                sel.grow(modifiers["grow"], modifiers["grow"])
            if modifiers["shrink"]:
                sel.shrink(modifiers["shrink"], modifiers["shrink"], False)
            if modifiers["feather"]:
                sel.feather(modifiers["feather"])
        rect, count = _count(sel)
        doc.setSelection(sel if count else None)  # an empty one would keep the mask node
        if not count:
            warnings.append({"code": "empty_selection",
                             "message": "The result selects nothing, so the document now has "
                                        "no selection"})
    return {"doc": handle, "mode": mode, "op": op,
            "bounds": None if rect is None else _rect(*rect), "pixel_count": count,
            "warnings": warnings}


# --- selection_to_layer ------------------------------------------------------

S2L_KEYS = ("doc", "source", "name", "parent", "above", "cut", "dry_run")


def _intersect(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def _and01(a, b):
    """The AND of two 0/1 masks of the same length (big ints, in C)."""
    n = len(a)
    return (int.from_bytes(a, "big") & int.from_bytes(b, "big")).to_bytes(n, "big")


def split_pixels(old, mask, w, h):
    """``(copy, cut)`` for a source's BGRA ``old`` under the selection
    values ``mask`` (``w * h``): ``copy`` has alpha ``round(a * s / 255)``,
    ``cut`` (what the source keeps) ``round(a * (255 - s) / 255)``, colours
    unchanged (``pixels.mask_alpha``, exact). Where a copy pixel's alpha is 0
    all its bytes are 0 (no colour under alpha 0 in the new layer); a cut
    pixel the selection touches (s > 0) whose alpha becomes 0 is zeroed too.
    Pixels with s = 0 keep their bytes in the source exactly."""
    copy, copy_alpha = pixels.mask_alpha(old, mask, w, h, keep=True)
    copy = alphastats.zero_pixels(copy, copy_alpha.translate(_ZERO_1), w)
    cut, cut_alpha = pixels.mask_alpha(old, mask, w, h, keep=False)
    zero = _and01(cut_alpha.translate(_ZERO_1), mask.translate(_NONZERO_1))
    cut = alphastats.zero_pixels(cut, zero, w)
    return copy, cut


def _check_s2l(doc, params):
    params = _strict_item(params, S2L_KEYS)
    cut = params.get("cut", True)
    if type(cut) is not bool:
        raise _bad(f"'cut' must be true or false, got {cut!r}", param="cut")
    if params.get("name") is None:
        raise _bad("'name' is required (the new layer's name)", param="name")
    name = layerprops.check_name(params["name"])
    index = nodes.build_index(doc)
    source = refs.resolve(index, _required_ref(params, "source"), field="source")
    kind = index.get(source)["type"]
    parent, above = placement(index, params, source, "the new layer")
    if kind != PAINT:
        raise CommandError("conflict", f"Layer {index.path_of(source)!r} is a {kind}; "
                           "selection_to_layer copies from a paint layer",
                           {"id": source, "type": kind})
    pixels.require_rgba8(doc)
    sel = doc.selection()
    srect = sel_rect(sel)
    if srect is None:
        raise CommandError("conflict", "No selection: make one with krita_select first")
    doc.waitForDone()
    lrect = _bounds(_live(doc, source))
    region = None if lrect is None else _intersect(srect, lrect)
    mask = None if region is None else sel_bytes(sel, region)
    if mask is None or mask.count(0) == len(mask):
        raise CommandError("conflict", "The selection doesn't overlap the layer",
                           {"selection": _rect(*srect), "layer_bounds":
                            None if lrect is None else _rect(*lrect)})
    return index, {"source": source, "name": name, "parent": parent, "above": above,
                   "cut": cut, "region": region, "mask": mask}


def _create_step(doc, plan, copy):
    node = doc.createNode(plan["name"], PAINT)
    if node is None:
        raise CommandError("krita_error", f"Krita could not create layer {plan['name']!r}")
    nid = nodes.node_id(node)
    parent = _parent_node(doc, plan["parent"])
    above = None if plan["above"] is None else _position(parent, plan["above"])[0]
    _add(parent, node, above)
    try:
        if nodes.node_by_id(doc, nid) is None:  # addChildNode can report success and do nothing
            raise CommandError("krita_error", "Krita did not insert the new layer")
        _write(node, copy, plan["region"], f"the new layer {plan['name']!r}")
    except Exception:
        try:
            _remove_node(doc, nid)
        except Exception:
            print(f"[KritaMCP] selection_to_layer: could not remove the half-made layer {nid}:")
            traceback.print_exc()
        raise
    return nid, RemoveNode(nid)


def _cut_step(doc, plan, old, cut):
    node = _live(doc, plan["source"])
    inverse = RestorePixels(plan["source"], [(plan["region"], old)])
    try:
        _write(node, cut, plan["region"], f"layer {plan['source']}")
    except Exception:
        try:
            inverse(doc)
        except Exception:
            print(f"[KritaMCP] selection_to_layer: could not restore layer {plan['source']}:")
            traceback.print_exc()
        raise
    return None, inverse


@command("selection_to_layer", timeout=LONG_TIMEOUT, mutating=True)
def cmd_selection_to_layer(params):
    """Copy (``cut``: move) a paint layer's pixels inside the selection to a
    new paint layer at the same canvas position, alpha scaled by the
    selection's values. One journal entry; ``dry_run`` validates only."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    index, plan = _check_s2l(doc, params)
    name = plan["name"]
    head = {"doc": handle, "source": plan["source"], "cut_rect": _rect(*plan["region"]),
            "cut": plan["cut"]}
    if dry_run:
        warnings, notes = _duplicate_warnings(index, name, None, "layer", index.by_name(name), [])
        return _with_notes({**head, "dry_run": True,
                            "would_create": {"name": name, "type": PAINT, "parent": plan["parent"],
                                             "above": plan["above"]},
                            "warnings": warnings}, notes)

    x, y, w, h = plan["region"]
    old = pixels.as_bytes(_live(doc, plan["source"]).pixelData(x, y, w, h))
    if len(old) != 4 * w * h:
        raise CommandError("krita_error", f"Krita returned {len(old)} bytes for a {w}x{h} read")
    copy, cut = split_pixels(old, plan["mask"], w, h)
    steps = ["create"] + (["cut"] if plan["cut"] and cut != old else [])

    def apply_one(ctx, step):
        if step == "create":
            return _create_step(ctx, plan, copy)
        return _cut_step(ctx, plan, old, cut)

    outcome = batch.run(steps, apply_one, doc, label="selection_to_layer", field="source")
    doc.refreshProjection()
    doc.waitForDone()
    nid = outcome.results[0]
    fresh, by_id = nodes.tree(doc)
    warnings, notes = _duplicate_warnings(fresh, name, nid, "layer",
                                          [o for o in fresh.by_name(name) if o != nid], [])
    copied = pixels.count_alpha(copy)
    if not copied:
        warnings.append({"code": "nothing_copied",
                         "message": "The source has no visible pixels (alpha > 0) inside the "
                                    "selection: the new layer is empty", "id": nid})
    if not batch.JOURNALS.get(handle).push("selection_to_layer", outcome.inverses,
                                           outcome.nbytes):
        warnings.append({
            "code": "not_undoable",
            "message": f"The old pixels take {outcome.nbytes} bytes, over the undo journal's "
                       "memory and disk caps: this call can't be undone, and the agent undo "
                       "journal was cleared",
            "nbytes": outcome.nbytes})
    return _with_notes({**head, "dry_run": False,
                        "layer": nodes.layer_object(by_id[nid], fresh, nid=nid), "copied": copied,
                        "warnings": warnings}, notes)
