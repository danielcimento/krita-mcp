"""set_canvas_size (api-design § krita_set_canvas_size).

Changes the canvas size without scaling anything: the art keeps its place
relative to itself, shifted by the offset the anchor gives
(``canvasplan``). Verified headless (krita-api-notes § Documents,
2026-11-25):

- ``Document.resizeImage(x, y, w, h)`` takes the new canvas rectangle in
  old canvas coordinates: ``x, y`` negative adds space on the left / top.
  Every node moves by ``(-x, -y)``: paint layers, groups' children, masks
  and the global selection alike.
- Layers are **not cropped**: pixels a shrink pushes past the canvas edge
  stay in the layers (``pixelData`` past the edge returns them), so
  resizing back with the opposite offset restores every layer's pixels and
  bounds exactly. The journal inverse is just that reverse resize.
- It does not set ``modified()`` (headless).

Journal: one ``ResizeCanvas`` (P5.1 protocol: applying it returns the
resize forward again). Its ``check`` refuses when the canvas is no longer
the size the call left it (resized by hand since), and older entries stay
consistent because the journal is a stack: their rectangles are in the
coordinates of the canvas they were recorded on, which is back by the
time they are applied.
"""

from .. import batch, canvasplan, config, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from ..params import canvas_size
from .layers import _dry_run, _strict_item

KEYS = ("doc", "width", "height", "anchor", "dry_run")

RIG_WARNING = {
    "code": "rig_breaking",
    "message": "Changing the canvas size breaks existing rigs: mesh coordinates are canvas "
               "pixels, so a rig made on this document no longer fits (CONVENTIONS § Canvas: "
               "fix the canvas size before rigging starts and never change it afterwards)",
}


def _settle(doc):
    doc.waitForDone()
    doc.refreshProjection()
    doc.waitForDone()


class ResizeCanvas:
    """Resize the canvas of the document passed at call time to
    ``width`` x ``height``, moving the content by ``offset`` (``(dx, dy)``,
    where the old origin lands). ``expect``: the ``(w, h)`` the canvas
    must have first (``check``). Applying it returns the reverse resize."""

    __slots__ = ("width", "height", "offset", "expect")
    node_ids = ()
    nbytes = 0

    def __init__(self, width, height, offset, expect):
        self.width, self.height = width, height
        self.offset = tuple(offset)
        self.expect = tuple(expect)

    def check(self, doc):
        size = (doc.width(), doc.height())
        if size != self.expect:
            return (f"the canvas is {size[0]}x{size[1]} now, not {self.expect[0]}x"
                    f"{self.expect[1]} as this call left it (resized since)")
        return None

    def __call__(self, doc):
        before = (doc.width(), doc.height())
        doc.resizeImage(*canvasplan.krita_rect(self.offset, (self.width, self.height)))
        _settle(doc)
        after = (doc.width(), doc.height())
        if after != (self.width, self.height):
            raise CommandError("krita_error",
                               f"Krita left the canvas {after[0]}x{after[1]}, not "
                               f"{self.width}x{self.height}",
                               {"width": after[0], "height": after[1]})
        return ResizeCanvas(before[0], before[1], (-self.offset[0], -self.offset[1]), after)

    def __repr__(self):
        return f"ResizeCanvas({self.width}x{self.height}, offset {self.offset})"


def _size_params(params):
    for key in ("width", "height"):
        if key not in params:
            raise CommandError("bad_request", f"'{key}' is required (canvas pixels)",
                               {"param": key})
    return canvas_size(params, config.current().limits)


@command("set_canvas_size", timeout=LONG_TIMEOUT, mutating=True)
def cmd_set_canvas_size(params):
    """Resize the canvas (no scaling) about ``anchor``. Always warns that
    it breaks existing rigs. One journal entry; ``dry_run`` reports the
    offset."""
    params = _strict_item(params, KEYS)
    width, height = _size_params(params)
    anchor = canvasplan.check_anchor(params.get("anchor", canvasplan.DEFAULT_ANCHOR))
    dry_run = _dry_run(params)
    handle, doc = registry.resolve_doc(params)

    old = (doc.width(), doc.height())
    dx, dy = canvasplan.offset(anchor, old, (width, height))
    result = {"doc": handle, "width_before": old[0], "height_before": old[1],
              "width": width, "height": height, "offset": {"x": dx, "y": dy},
              "anchor": anchor, "dry_run": dry_run, "changed": old != (width, height),
              "warnings": [dict(RIG_WARNING)]}
    if dry_run or not result["changed"]:
        return result

    doc.waitForDone()
    back = ResizeCanvas(width, height, (dx, dy), old)(doc)
    if not batch.JOURNALS.get(handle).push("set_canvas_size", [back], 0):
        result["warnings"].append({  # not expected: the entry holds no pixels
            "code": "not_undoable",
            "message": "The undo journal could not keep this resize, and was cleared",
            "nbytes": 0})
    return result
