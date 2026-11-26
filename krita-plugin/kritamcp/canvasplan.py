"""Where the art goes when the canvas changes size, for ``set_canvas_size``
(api-design § krita_set_canvas_size). Pure: no krita / PyQt5 imports.

The *offset* is where the old canvas's top-left corner lands on the new
canvas: every layer's content moves by it, so the art keeps its place
relative to itself. Per axis, with ``d = new - old`` (negative when
shrinking):

- left / top: 0 (the old edge stays the edge);
- right / bottom: ``d``;
- centre: ``d // 2``, floor division, so when ``d`` is odd the art sits
  half a pixel left of (or above) the exact centre, growing and shrinking
  alike: growing 10 -> 13 adds 1 column on the left and 2 on the right,
  shrinking 13 -> 10 removes 2 on the left and 1 on the right.

Krita's ``Document.resizeImage(x, y, w, h)`` takes the new canvas's
rectangle in old canvas coordinates, i.e. ``(-offset_x, -offset_y, w, h)``
(verified headless 2026-11-25, krita-api-notes § Documents).
"""

from .errors import CommandError

# anchor -> (horizontal, vertical) factor: 0 = keep the left/top edge,
# 1 = centre, 2 = keep the right/bottom edge.
ANCHORS = {
    "top-left": (0, 0), "top": (1, 0), "top-right": (2, 0),
    "left": (0, 1), "center": (1, 1), "right": (2, 1),
    "bottom-left": (0, 2), "bottom": (1, 2), "bottom-right": (2, 2),
}
DEFAULT_ANCHOR = "center"


def check_anchor(value):
    """``value`` if it is one of ``ANCHORS``, else ``bad_request``."""
    if not isinstance(value, str) or value not in ANCHORS:
        raise CommandError("bad_request",
                           f"'anchor' must be one of {', '.join(ANCHORS)}; got {value!r}",
                           {"param": "anchor", "valid": list(ANCHORS)})
    return value


def _axis(factor, old, new):
    d = new - old
    return 0 if factor == 0 else d // 2 if factor == 1 else d


def offset(anchor, old_size, new_size):
    """``(dx, dy)``: where the old canvas's origin lands on the new one."""
    fx, fy = ANCHORS[anchor]
    return _axis(fx, old_size[0], new_size[0]), _axis(fy, old_size[1], new_size[1])


def krita_rect(offset_xy, new_size):
    """The ``resizeImage(x, y, w, h)`` arguments for ``offset_xy``."""
    return -offset_xy[0], -offset_xy[1], new_size[0], new_size[1]

