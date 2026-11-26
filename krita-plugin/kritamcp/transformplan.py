"""What a ``transform_layer`` call does (api-design § krita_transform_layer).
Pure: no krita / PyQt5 imports.

- ``parse``: the ``translate`` / ``scale`` / ``rotate`` / ``flip`` /
  ``pivot`` / ``resample`` parameters, validated.
- The geometry of each step on a rectangle ``(x, y, w, h)`` in canvas
  pixels, as Krita does it (verified headless, krita-api-notes § Nodes):
  ``flipped``, ``scaled_size`` / ``scaled``, ``rotated``,
  ``rotation_shift``; ``predict`` chains them in the fixed order flip,
  scale, rotate, translate.
- Rectangle helpers: ``union``, ``minus``.

Rotation is clockwise on screen (y points down): a point ``(x, y)``
relative to the centre goes to ``(x cos a - y sin a, x sin a + y cos a)``.
"""

import dataclasses
import math

from . import config
from .errors import CommandError

# Krita's filter-strategy ids (KisFilterStrategyRegistry). Verified headless
# 2026-11-25: each scales differently; "Box" is NearestNeighbor under
# another id, and an unknown name silently falls back to Bicubic.
FILTERS = ("Bicubic", "Bilinear", "NearestNeighbor", "Lanczos3", "Mitchell", "BSpline", "Bell",
           "Hermite")
DEFAULT_FILTER = "Bicubic"
MIN_SCALE, MAX_SCALE = 0.01, 100.0
MAX_DEGREES = 3600.0
FLIPS = ("h", "v")
CENTER = "center"


def _bad(param, message, **details):
    return CommandError("bad_request", message, {"param": param, **details})


def _number(value):
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value))


def _pair(value, param, what):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise _bad(param, f"'{param}' must be {what}, got {value!r}")
    return value


@dataclasses.dataclass(frozen=True)
class Spec:
    """A validated transform. Steps that change nothing are None:
    ``translate`` ``(dx, dy)`` ints, ``scale`` ``(sx, sy)``, ``rotate``
    degrees clockwise (as given; ``turn`` is it modulo 360), ``flip`` "h"
    or "v". ``pivot``: ``CENTER`` or ``(x, y)``."""

    translate: object = None
    scale: object = None
    rotate: object = None
    flip: object = None
    pivot: object = CENTER
    resample: str = DEFAULT_FILTER

    @property
    def turn(self):
        return None if self.rotate is None else self.rotate % 360.0

    @property
    def pixel_steps(self):
        """True if a step resamples or moves pixels (all but translate)."""
        return any(v is not None for v in (self.flip, self.scale, self.rotate))


def parse(params):
    """The ``Spec`` for ``params`` (a dict; absent or null keys mean "not
    given"). Errors: ``bad_request`` with ``details.param``; nothing to do
    at all is ``bad_request`` too."""
    translate = params.get("translate")
    if translate is not None:
        dx, dy = _pair(translate, "translate", "[dx, dy], whole numbers of canvas pixels")
        for v in (dx, dy):
            if isinstance(v, bool) or not isinstance(v, int):
                raise _bad("translate", f"'translate' must be [dx, dy], whole numbers of canvas "
                           f"pixels, got {translate!r}")
            if not -config.COORD <= v <= config.COORD:
                raise _bad("translate", f"'translate' values must be between {-config.COORD} "
                           f"and {config.COORD}")
        translate = None if (dx, dy) == (0, 0) else (dx, dy)

    scale = params.get("scale")
    if scale is not None:
        sx, sy = (scale, scale) if _number(scale) else _pair(
            scale, "scale", "a number or [sx, sy]")
        for v in (sx, sy):
            if not _number(v) or not MIN_SCALE <= v <= MAX_SCALE:
                raise _bad("scale", f"'scale' factors must be numbers from {MIN_SCALE} to "
                           f"{MAX_SCALE} (use 'flip' to mirror), got {scale!r}")
        scale = None if (sx, sy) == (1, 1) else (float(sx), float(sy))

    rotate = params.get("rotate")
    if rotate is not None:
        if not _number(rotate) or abs(rotate) > MAX_DEGREES:
            raise _bad("rotate", f"'rotate' must be a number of degrees (clockwise) from "
                       f"{-MAX_DEGREES:g} to {MAX_DEGREES:g}, got {rotate!r}")
        rotate = None if rotate % 360.0 == 0 else float(rotate)

    flip = params.get("flip")
    if flip is not None and flip not in FLIPS:
        raise _bad("flip", f"'flip' must be 'h' (mirror left-right) or 'v' (top-bottom), "
                   f"got {flip!r}", valid=list(FLIPS))

    pivot = params.get("pivot", CENTER)
    if pivot is None:
        pivot = CENTER
    if pivot != CENTER:
        px, py = _pair(pivot, "pivot", "'center' or [x, y] in canvas pixels")
        for v in (px, py):
            if not _number(v) or not -config.COORD <= v <= config.COORD:
                raise _bad("pivot", f"'pivot' must be 'center' or [x, y], numbers of canvas "
                           f"pixels from {-config.COORD} to {config.COORD}, got {pivot!r}")
        pivot = (float(px), float(py))

    resample = params.get("resample", DEFAULT_FILTER)
    if resample is None:
        resample = DEFAULT_FILTER
    by_lower = {f.lower(): f for f in FILTERS}
    if not isinstance(resample, str) or resample.lower() not in by_lower:
        raise _bad("resample", f"'resample' must be one of {', '.join(FILTERS)}, got {resample!r}",
                   valid=list(FILTERS))
    resample = by_lower[resample.lower()]

    spec = Spec(translate, scale, rotate, flip, pivot, resample)
    given = [k for k in ("translate", "scale", "rotate", "flip") if params.get(k) is not None]
    if not given:
        raise CommandError("bad_request", "nothing to do: give at least one of translate, "
                           "scale, rotate and flip", {"param": "translate"})
    return spec


# --- geometry ---------------------------------------------------------------

def center(rect):
    x, y, w, h = rect
    return x + w / 2, y + h / 2


def pivot_point(spec, rect):
    """The pivot in canvas pixels: the rectangle's centre for ``CENTER``."""
    return center(rect) if spec.pivot == CENTER else spec.pivot


def _round(v):
    """Round half up (Krita's placement of half-pixel results)."""
    return math.floor(v + 0.5)


def flip_axis2(rect, axis, pivot):
    """Twice the mirror axis's coordinate, an int (a flip is exact only
    about a whole or half pixel): ``2 * x`` of the vertical axis for "h",
    ``2 * y`` of the horizontal one for "v". The rectangle's own centre
    gives ``2 * x + w`` exactly; another pivot is rounded to the nearest
    half pixel."""
    x, y, w, h = rect
    if pivot is None:
        return 2 * x + w if axis == "h" else 2 * y + h
    return _round(2 * (pivot[0] if axis == "h" else pivot[1]))


def flipped(rect, axis, axis2):
    """Where ``rect`` lands mirrored about the axis at ``axis2 / 2``."""
    x, y, w, h = rect
    if axis == "h":
        return axis2 - x - w, y, w, h
    return x, axis2 - y - h, w, h


def scaled_size(w, h, sx, sy):
    """The ``scaleNode`` target size: the bounds times the factors, rounded,
    at least 1. Krita's real factor is then ``W / w`` (``H / h``)."""
    return max(1, _round(w * sx)), max(1, _round(h * sy))


def scaled(rect, pivot, size):
    """``rect`` scaled to ``size`` with ``pivot`` fixed (``scaleNode``)."""
    x, y, w, h = rect
    nw, nh = size
    px, py = pivot
    return _round(px + (x - px) * nw / w), _round(py + (y - py) * nh / h), nw, nh


def _rotate_vec(dx, dy, degrees):
    a = math.radians(degrees)
    c, s = math.cos(a), math.sin(a)
    return dx * c - dy * s, dx * s + dy * c


def rotated(rect, degrees):
    """``rect`` rotated clockwise by ``degrees`` about its centre
    (``rotateNode``): a quarter turn swaps the sides exactly, the corner
    rounded half up; other angles give the bounding box of the turned
    corners."""
    x, y, w, h = rect
    cx, cy = center(rect)
    turn = degrees % 360.0
    if turn % 90.0 == 0:
        nw, nh = (h, w) if int(turn // 90) % 2 else (w, h)
        return _round(cx - nw / 2), _round(cy - nh / 2), nw, nh
    xs, ys = [], []
    for px, py in ((x, y), (x + w, y), (x, y + h), (x + w, y + h)):
        rx, ry = _rotate_vec(px - cx, py - cy, degrees)
        xs.append(cx + rx)
        ys.append(cy + ry)
    eps = 1e-6
    x0, y0 = math.floor(min(xs) + eps), math.floor(min(ys) + eps)
    x1, y1 = math.ceil(max(xs) - eps), math.ceil(max(ys) - eps)
    return x0, y0, x1 - x0, y1 - y0


def rotation_shift(rect, pivot, degrees):
    """``(dx, dy)``, whole pixels: the move after a turn about ``rect``'s
    centre that makes it a turn about ``pivot`` instead:
    ``(p - c) - R(p - c)``."""
    cx, cy = center(rect)
    vx, vy = pivot[0] - cx, pivot[1] - cy
    rx, ry = _rotate_vec(vx, vy, degrees)
    return _round(vx - rx), _round(vy - ry)


def moved(rect, d):
    x, y, w, h = rect
    return x + d[0], y + d[1], w, h


def predict(rect, spec):
    """``(steps, rect)``: each step as reported (``{"op", ...}``, with
    ``bounds`` the rectangle after it) and the final rectangle, for a
    layer or group whose bounds are ``rect``."""
    pivot = pivot_point(spec, rect)
    steps = []
    if spec.flip is not None:
        axis2 = flip_axis2(rect, spec.flip, None if spec.pivot == CENTER else pivot)
        new = flipped(rect, spec.flip, axis2)
        steps.append({"op": "flip", "axis": spec.flip, "shift": [new[0] - rect[0],
                                                                 new[1] - rect[1]]})
        rect = new
        steps[-1]["bounds"] = rect
    if spec.scale is not None:
        size = scaled_size(rect[2], rect[3], *spec.scale)
        steps.append(scale_step(rect, size, spec.resample))
        rect = scaled(rect, pivot, size)
        steps[-1]["bounds"] = rect
    if spec.rotate is not None:
        shift = rotation_shift(rect, pivot, spec.turn)
        rect = moved(rotated(rect, spec.turn), shift)
        steps.append({"op": "rotate", "degrees": spec.rotate, "shift": list(shift),
                      "bounds": rect})
    if spec.translate is not None:
        rect = moved(rect, spec.translate)
        steps.append({"op": "translate", "by": list(spec.translate), "bounds": rect})
    return steps, rect


def scale_step(rect, size, resample):
    return {"op": "scale", "factor": [round(size[0] / rect[2], 6), round(size[1] / rect[3], 6)],
            "size": {"w": size[0], "h": size[1]}, "resample": resample}


def check_size(rect, what="the transformed layer"):
    """Raise ``too_large`` if ``rect`` is over the canvas limits
    (``config.current().limits``)."""
    limits = config.current().limits
    w, h = rect[2], rect[3]
    max_dim, max_pixels = limits["max_canvas_dim"], limits["max_canvas_pixels"]
    if max(w, h) > max_dim or w * h > max_pixels:
        raise CommandError("too_large", f"{what} would be {w}x{h}; the limits are {max_dim} "
                           f"pixels per side and {max_pixels} pixels (max_canvas_dim, "
                           "max_canvas_pixels)",
                           {"w": w, "h": h, "max_canvas_dim": max_dim,
                            "max_canvas_pixels": max_pixels})


# --- rectangles -------------------------------------------------------------

def union(a, b):
    """The bounding rectangle of ``a`` and ``b``; either may be None."""
    if a is None:
        return b
    if b is None:
        return a
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return x0, y0, x1 - x0, y1 - y0


def minus(a, b):
    """The parts of ``a`` outside ``b`` as at most 4 disjoint rectangles
    (bands above and below ``b``, then left and right of it). ``b`` None:
    ``[a]``; ``a`` None: ``[]``."""
    if a is None:
        return []
    if b is None:
        return [tuple(a)]
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    ix0, iy0, ix1, iy1 = max(ax0, bx0), max(ay0, by0), min(ax1, bx1), min(ay1, by1)
    if ix0 >= ix1 or iy0 >= iy1:
        return [tuple(a)]
    out = []
    if ay0 < iy0:
        out.append((ax0, ay0, ax1 - ax0, iy0 - ay0))
    if iy1 < ay1:
        out.append((ax0, iy1, ax1 - ax0, ay1 - iy1))
    if ax0 < ix0:
        out.append((ax0, iy0, ix0 - ax0, iy1 - iy0))
    if ix1 < ax1:
        out.append((ix1, iy0, ax1 - ix1, iy1 - iy0))
    return out
