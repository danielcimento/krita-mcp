"""occlusion_map (api-design § krita_occlusion_map): how much of a paint
layer the layers drawn after it hide.

``occlusionplan.plan`` picks the *units* that cover the target (paint
layers and isolated groups drawn after it, visible, in ``modes``) and the
adjustments the coverage render needs. The coverage is the alpha of a
render of only those units (the target hidden), read from a ``clone()``
of the document (D10 B) over the target's bounds clipped to the canvas,
so the user's document is never touched; the clone is closed in
``finally`` and never used after (krita-api-notes § Crash hazards), and
its node wrappers live in one helper's frame.

The map is ``round(target alpha x coverage / 255)`` per pixel, the exact
product (``pixels.scale_alpha``: QPainter DestinationIn on a 16-bit
premultiplied image, exact for all 65536 pairs), written as an 8-bit
grayscale PNG the size of the target's bounds. Counts use translate tables
and big-int AND / ``bit_count``, no per-pixel loop.
"""

from PyQt5.QtGui import QImage

from .. import nodes, occlusionplan, paths, pixels, refs, registry, renderplan
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from .pixels import _bounds, _png_level, _rect
from .render import _png_path

_NONZERO_1 = bytes([0] + [1] * 255)
_FULL_1 = bytes([0] * 255 + [1])


def _at_least_1(level):
    return bytes(1 if v >= level else 0 for v in range(256))


def _props(by_id):
    """``{id: {...}}``: what ``occlusionplan.plan`` reads of every node."""
    out = {}
    for nid, node in by_id.items():
        kind = node.type()
        out[nid] = {"type": kind, "visible": bool(node.visible()),
                    "blend_mode": node.blendingMode(),
                    "pass_through": (bool(node.passThroughMode())
                                     if kind == occlusionplan.GROUP
                                     and hasattr(node, "passThroughMode") else False),
                    "inherit_alpha": bool(node.inheritAlpha())}
    return out


def _adjust(clone, visibility, plan, props):
    """Apply the coverage render's changes to the clone's nodes, found by
    UUID (clones keep them). Its own frame, so no node wrapper outlives
    the clone."""

    def node(nid):
        found = nodes.node_by_id(clone, nid)
        if found is None:
            raise CommandError("krita_error",
                               f"Layer {nid} is missing from the document's copy")
        return found

    for nid, visible in visibility.items():
        node(nid).setVisible(visible)
    for nid in plan.unclip:
        node(nid).setInheritAlpha(False)
    for nid in plan.neutral:
        n = node(nid)
        if n.opacity() != 255:
            n.setOpacity(255)
        if props[nid]["inherit_alpha"]:
            n.setInheritAlpha(False)
        if not props[nid]["pass_through"] and n.blendingMode() != "normal":
            n.setBlendingMode("normal")


def _coverage_render(doc, visibility, plan, props, rect):
    """The alpha bytes (one per pixel) of the clone's projection over
    ``rect`` with the coverage adjustments applied."""
    clone = doc.clone()
    if clone is None:
        raise CommandError("krita_error", "Krita could not copy the document for the map")
    try:
        _adjust(clone, visibility, plan, props)
        clone.refreshProjection()
        clone.waitForDone()
        image = clone.projection(*rect)
    finally:
        try:
            clone.setModified(False)  # close() never prompts then
        finally:
            clone.close()
    del clone  # crash hazard: never touch a closed document's wrapper
    x, y, w, h = rect
    if image is None or image.isNull() or (image.width(), image.height()) != (w, h):
        raise CommandError("krita_error", f"Krita returned no image for the {w}x{h} region")
    return pixels.from_qimage(image)[0][3::4]


def _canvas_part(bounds, width, height):
    """``bounds`` clipped to the canvas, or None when it lies off it."""
    x, y, w, h = bounds
    x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, width), min(y + h, height)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def _embed(part, rect, bounds):
    """``part`` (one byte per pixel over ``rect``) placed in a zero buffer
    the size of ``bounds`` (``rect`` inside it)."""
    bx, by, bw, bh = bounds
    x, y, w, h = rect
    if (x, y, w, h) == (bx, by, bw, bh):
        return part
    left, right = bytes(x - bx), bytes(bx + bw - x - w)
    blank = bytes(bw)
    rows = [blank] * (y - by)
    rows += [left + part[r * w:(r + 1) * w] + right for r in range(h)]
    rows += [blank] * (by + bh - y - h)
    return b"".join(rows)


def _count_inside(alpha, bounds, rect):
    """The pixels with alpha > 0 of ``alpha`` (over ``bounds``) inside ``rect``."""
    bx, by, bw, _ = bounds
    x, y, w, h = rect
    total, start = 0, x - bx
    for r in range(y - by, y - by + h):
        row = alpha[r * bw + start:r * bw + start + w]
        total += w - row.count(0)
    return total


def _and_count(a01, b01):
    """The number of positions where both 0/1 masks are 1."""
    return (int.from_bytes(a01, "big") & int.from_bytes(b01, "big")).bit_count()


def _write_map(values, w, h, path, level):
    """``values`` (``w * h`` bytes) as an 8-bit grayscale PNG."""
    image = QImage(values, w, h, w, QImage.Format_Grayscale8)  # ``values`` outlives it
    return pixels.save_png(image, path, level=level)


@command("occlusion_map", timeout=LONG_TIMEOUT)
def cmd_occlusion_map(params):
    """Map how much of a paint layer the layers drawn after it hide, as a
    grayscale PNG the size of its bounds (target alpha x coverage), with
    the share of its pixels covered. Reads only: the document is not
    changed."""
    path = _png_path(params.get("path"), tool="occlusion_map")
    handle, doc = registry.resolve_doc(params)
    modes = occlusionplan.check_modes(params.get("modes"))
    threshold = occlusionplan.check_threshold(params.get("threshold"))
    level = _png_level(params)
    pixels.require_rgba8(doc)
    index, by_id = nodes.tree(doc)
    ref = params.get("id")
    nid = refs.resolve(index, ref, field="id")
    node = by_id[nid]
    if node.type() != occlusionplan.PAINT:
        raise CommandError("conflict",
                           f"occlusion_map needs a paint layer; {index.path_of(nid)!r} is a "
                           f"{node.type()}", {"id": nid, "type": node.type()})
    props = _props(by_id)
    plan = occlusionplan.plan(index, props, nid, occlusionplan.mode_set(modes))
    out = {"doc": handle, "layer": nid, "name": node.name(), "modes": modes,
           "threshold": threshold, "above": occlusionplan.summary_ids(plan.units)}
    bounds = _bounds(node)
    alpha = pixels.as_bytes(node.pixelData(*bounds))[3::4] if bounds else b""
    total = len(alpha) - alpha.count(0)
    if total == 0:
        out.update({"path": None, "bounds": None, "pixels": 0, "covered_px": 0,
                    "off_canvas_px": 0, "covered_pct": None, "fully_hidden": False,
                    "bytes": 0})
        return out
    bx, by, bw, bh = bounds
    rect = _canvas_part(bounds, doc.width(), doc.height())
    if plan.units and rect is not None:
        visible = {i: p["visible"] for i, p in props.items()}
        visibility = renderplan.visibility_changes(index, visible, only=plan.units, hide=[nid])
        coverage = _embed(_coverage_render(doc, visibility, plan, props, rect), rect, bounds)
    else:
        coverage = bytes(bw * bh)
    values = pixels.scale_alpha(alpha, coverage, bw, bh)
    painted = alpha.translate(_NONZERO_1)
    covered = _and_count(painted, coverage.translate(
        _at_least_1(occlusionplan.threshold_level(threshold))))
    full = _and_count(painted, coverage.translate(_FULL_1))
    inside = _count_inside(alpha, bounds, rect) if rect is not None else 0
    path = paths.ensure_parent_dir(path)
    nbytes = _write_map(values, bw, bh, path, level)
    out.update({"path": path, "bounds": _rect(bx, by, bw, bh), "pixels": total,
                "covered_px": covered, "off_canvas_px": total - inside,
                "covered_pct": covered / total, "fully_hidden": full == total, "bytes": nbytes})
    if level is not None:
        out["png_level"] = level
    return out
