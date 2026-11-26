"""Pixel commands (api-design § Pixels): import_png.

Pixels go through ``kritamcp.pixels`` (D5: QImage, no numpy, 8-bit RGBA
only). The undo inverse of a pixel write keeps the old pixels of the
rectangles it changes (D6) and writes them back on the layer it looks up
in the document passed at call time.
"""

import dataclasses
import traceback

from .. import batch, nodes, paths, pixels, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from .layers import PAINT, _check_coord, _dry_run, _live

MODES = ("replace", "over")


def _rect(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def _bounds(node):
    """The node's bounds as ``(x, y, w, h)``, or None when it has none
    (a zeroed layer reports (0, 0, 0, 0) with ``hasExtents()`` True)."""
    if not node.hasExtents():
        return None
    r = node.bounds()
    if r.width() <= 0 or r.height() <= 0:
        return None
    return r.x(), r.y(), r.width(), r.height()


def _union(a, b):
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return x0, y0, x1 - x0, y1 - y0


def _area(r):
    return r[2] * r[3]


def _write(node, data, rect, what):
    if not node.setPixelData(data, *rect):
        raise CommandError("krita_error", f"Krita refused the pixels for {what}")


class RestorePixels:
    """The inverse of a pixel write: write back the old pixels of each
    ``(rect, bytes)`` patch (snapshots taken before the write, so overlaps
    agree) on the layer with ``node_id``, looked up in the document passed
    at call time (batch.py: never a captured wrapper). ``nbytes``: the
    snapshot size, for the journal's byte cap."""

    __slots__ = ("node_id", "patches", "nbytes")

    def __init__(self, nid, patches):
        self.node_id = nid
        self.patches = tuple((tuple(rect), data) for rect, data in patches)
        self.nbytes = sum(len(data) for _, data in self.patches)

    @property
    def node_ids(self):
        return (self.node_id,)

    def __call__(self, doc):
        node = _live(doc, self.node_id)
        for rect, data in self.patches:
            _write(node, data, rect, f"layer {self.node_id} (restoring old pixels)")

    def __repr__(self):
        rects = ", ".join(f"{r[2]}x{r[3]}@({r[0]},{r[1]})" for r, _ in self.patches)
        return f"RestorePixels({self.node_id}, {rects})"


@dataclasses.dataclass
class _Import:
    nid: str
    png: str
    size: tuple
    x: int
    y: int
    mode: str
    clear_outside: bool
    old_bounds: object  # (x, y, w, h) or None, read at validation

    @property
    def rect(self):
        return self.x, self.y, self.size[0], self.size[1]

    def snapshot_rects(self):
        """The rectangles whose old pixels the inverse keeps: the PNG's, plus
        (``clear_outside``) the layer's old bounds; one bounding rectangle
        when that is no bigger than the two apart."""
        if not self.clear_outside or self.old_bounds is None:
            return [self.rect]
        union = _union(self.rect, self.old_bounds)
        if _area(union) <= _area(self.rect) + _area(self.old_bounds):
            return [union]
        return [self.rect, self.old_bounds]

    def changed(self):
        """The bounding rectangle of everything the import may change."""
        if self.clear_outside and self.old_bounds is not None:
            return _union(self.rect, self.old_bounds)
        return self.rect


def _bool(params, key):
    value = params.get(key, False)
    if value is None:
        return False
    if type(value) is not bool:
        raise CommandError("bad_request", f"'{key}' must be true or false, got {value!r}",
                           {"param": key})
    return value


def _validate(doc, params):
    ref = params.get("id")
    if ref is None:
        raise CommandError("bad_request", "'id' is required (a layer ref)", {"param": "id"})
    node = nodes.resolve_node(doc, ref, field="id")
    kind = node.type()
    if kind != PAINT:
        raise CommandError("conflict",
                           f"Layer {ref!r} is a {kind}; import_png needs a paint layer",
                           {"id": nodes.node_id(node), "type": kind})
    pixels.require_rgba8(doc)

    given = params.get("png")
    if not isinstance(given, str) or given == "":
        raise CommandError("bad_request", "'png' is required (the path of a PNG file)",
                           {"param": "png"})
    path = paths.resolve_allowed_path(given, must_exist=True)
    size = pixels.png_size(path)
    pixels.check_png_limits(size, given)
    given_xy = {k: params[k] for k in ("x", "y") if params.get(k) is not None}
    x, y = _check_coord(given_xy, "x"), _check_coord(given_xy, "y")

    mode = params.get("mode", "replace")
    if mode is None:
        mode = "replace"
    if mode not in MODES:
        raise CommandError("bad_request", f"'mode' must be 'replace' or 'over', got {mode!r}",
                           {"param": "mode", "valid": list(MODES)})
    clear_outside = _bool(params, "clear_outside")
    if clear_outside and mode != "replace":
        raise CommandError("bad_request", "'clear_outside' is only valid with mode 'replace'",
                           {"param": "clear_outside"})
    return _Import(nid=nodes.node_id(node), png=path, size=size, x=x, y=y, mode=mode,
                   clear_outside=clear_outside, old_bounds=_bounds(node))


def _apply(doc, plan):
    """``apply_fn`` for ``batch.run``: decode, snapshot, write. On failure
    the old pixels are written back before re-raising (batch.py: the
    failing item cleans up after itself)."""
    node = _live(doc, plan.nid)
    data, w, h = pixels.load_png_bgra(plan.png)  # before any change: a bad file leaves nothing
    if (w, h) != tuple(plan.size):
        raise CommandError("conflict", f"{plan.png} changed since validation "
                           f"({plan.size[0]}x{plan.size[1]}, now {w}x{h})")
    inverse = RestorePixels(plan.nid, [(r, pixels.as_bytes(node.pixelData(*r)))
                                       for r in plan.snapshot_rects()])
    what = f"layer {plan.nid}"
    try:
        if plan.mode == "over":
            ((_, existing),) = inverse.patches  # "over" snapshots just the PNG's rect
            data = pixels.composite_over(existing, data, w, h)
        elif plan.clear_outside and plan.old_bounds is not None:
            _write(node, bytes(4 * _area(plan.old_bounds)), plan.old_bounds, what)
        _write(node, data, plan.rect, what)
    except Exception:
        try:
            inverse(doc)
        except Exception:
            print(f"[KritaMCP] import_png: could not restore layer {plan.nid}:")
            traceback.print_exc()
        raise
    return plan.changed(), inverse


@command("import_png", timeout=LONG_TIMEOUT, mutating=True)
def cmd_import_png(params):
    """Write a PNG into a paint layer at (x, y): ``replace`` its pixels
    there (``clear_outside``: and clear the rest of the layer) or composite
    ``over`` them. One journal entry; ``dry_run`` validates only."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    plan = _validate(doc, params)
    head = {"doc": handle, "layer": plan.nid, "png": plan.png, "mode": plan.mode,
            "clear_outside": plan.clear_outside}
    if dry_run:
        return {**head, "dry_run": True, "would_change": _rect(*plan.changed()), "warnings": []}

    outcome = batch.run([plan], _apply, doc, label="import_png", field="png")
    doc.refreshProjection()
    doc.waitForDone()
    warnings = []
    if not batch.JOURNALS.get(handle).push("import_png", outcome.inverses, outcome.nbytes):
        warnings.append({
            "code": "not_undoable",
            "message": f"The old pixels take {outcome.nbytes} bytes, over the undo journal's "
                       "byte cap: this import can't be undone, and the agent undo journal was "
                       "cleared",
            "nbytes": outcome.nbytes})
    changed = outcome.results[0]
    bounds = _bounds(_live(doc, plan.nid))
    return {**head, "dry_run": False, "changed": _rect(*changed),
            "bounds": None if bounds is None else _rect(*bounds), "warnings": warnings}
