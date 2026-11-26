"""What a ``render`` call draws (api-design § krita_render). Pure: no krita /
PyQt5 imports.

- ``clip_region``: the ``region`` parameter, clipped to the canvas.
- ``output_size``: the PNG's size for a region and ``scale``.
- ``visibility_changes``: the visibility to set (in a clone) for ``only`` /
  ``hide``, over a ``refs.NodeIndex``.
"""

import math

from . import config
from .errors import CommandError
from .params import LIMITS_HINT
from .refs import ROOT

MAX_SCALE = 4.0
RECT_KEYS = ("x", "y", "w", "h")


def _bad(message, **details):
    return CommandError("bad_request", message, details or None)


def clip_region(region, width, height):
    """``(x, y, w, h)``: ``region`` (``{x, y, w, h}`` in canvas pixels, or
    None for the whole canvas) clipped to the ``width`` x ``height``
    canvas. Errors: ``bad_request`` for a malformed region, one with
    ``w`` or ``h`` under 1, or one that lies entirely off the canvas."""
    if region is None:
        return 0, 0, width, height
    if not isinstance(region, dict) or set(region) != set(RECT_KEYS):
        raise _bad("'region' must be an object with exactly the keys x, y, w and h "
                   "(canvas pixels)", param="region")
    values = []
    for key in RECT_KEYS:
        v = region[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise _bad(f"'region.{key}' must be a whole number of canvas pixels, got {v!r}",
                       param="region")
        if not -config.COORD <= v <= config.COORD:
            raise _bad(f"'region.{key}' must be between {-config.COORD} and {config.COORD}",
                       param="region")
        values.append(v)
    x, y, w, h = values
    if w < 1 or h < 1:
        raise _bad(f"'region' is empty ({w}x{h}); w and h must be at least 1", param="region")
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, width), min(y + h, height)
    if x1 <= x0 or y1 <= y0:
        raise _bad(f"'region' {w}x{h} at ({x}, {y}) lies outside the {width}x{height} canvas",
                   param="region", canvas={"w": width, "h": height})
    return x0, y0, x1 - x0, y1 - y0


def scale_param(params):
    """``scale``: a number in (0, 4], default 1."""
    value = params.get("scale", 1.0)
    if value is None:
        return 1.0
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= MAX_SCALE):
        raise _bad(f"'scale' must be a number greater than 0 and at most {MAX_SCALE:g}, "
                   f"got {value!r}", param="scale")
    return float(value)


def _scaled(n, scale):
    """``n * scale`` rounded to the nearest whole number (halves up), at least 1."""
    return max(1, math.floor(n * scale + 0.5))


def output_size(w, h, scale, limits=None):
    """The PNG's ``(width, height)`` for a ``w`` x ``h`` region at ``scale``:
    each side rounded to the nearest pixel (halves up), at least 1. Over
    ``max_canvas_pixels`` in total: ``too_large``. ``limits`` defaults to
    ``config.current().limits``."""
    if limits is None:
        limits = config.current().limits
    ow, oh = _scaled(w, scale), _scaled(h, scale)
    max_pixels = limits["max_canvas_pixels"]
    if ow * oh > max_pixels:
        raise CommandError(
            "too_large",
            f"The render would be {ow}x{oh} = {ow * oh} pixels, over the limit of {max_pixels} "
            f"(max_canvas_pixels, {LIMITS_HINT}); use a smaller scale or region",
            {"limit": "max_canvas_pixels", "max": max_pixels, "pixels": ow * oh,
             "w": ow, "h": oh})
    return ow, oh


def _subtree(index, nid):
    """``nid``'s strict descendants."""
    out, stack = set(), index.children(nid)
    while stack:
        n = stack.pop()
        out.add(n)
        stack.extend(index.children(n))
    return out


def visibility_changes(index, visible, only=None, hide=None):
    """``{id: bool}``: the visibility to set so that the document renders
    ``only`` minus ``hide`` (lists of resolved ids, or None), changes only.

    ``visible``: ``{id: bool}``, every node's current visibility. With
    ``only``, each listed node and its ancestors are visible, every other
    node is hidden, and the descendants of a listed node keep their own
    visibility (a hidden child of an ``only`` group stays hidden). Nodes
    are hidden at the highest level that does it: the siblings of the
    listed nodes and of their ancestors, outside every listed subtree;
    nodes inside those keep their state, as the hidden ancestor hides them.
    Then every ``hide`` node is hidden (hide wins over only).
    """
    want = {}
    if only:
        keep = set(only)
        inside = set()
        for nid in keep:
            inside |= _subtree(index, nid)
        path = {a for nid in keep for a in index.ancestors(nid)}
        shown = keep | path
        for nid in shown:
            want[nid] = True
        for parent in [ROOT] + sorted(path - keep - inside, key=index.order):
            for child in index.children(parent):
                if child not in shown:
                    want[child] = False
    for nid in hide or ():
        want[nid] = False
    return {nid: v for nid, v in want.items() if bool(visible[nid]) != v}
