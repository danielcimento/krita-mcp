"""Parameter validators. Pure: no krita / PyQt5 imports."""

import math

from .errors import CommandError

LIMITS_HINT = 'configurable under "limits" in ~/.krita-mcp-config.json'


def int_param(params, key, default, lo, hi):
    """Read an integer parameter and check it is within [lo, hi]."""
    value = params.get(key, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))):
        raise CommandError("bad_request", f"'{key}' must be a number")
    value = int(value)
    if not lo <= value <= hi:
        raise CommandError("bad_request", f"'{key}' must be between {lo} and {hi}")
    return value


def float_param(params, key, default, lo, hi):
    value = params.get(key, default)
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or (isinstance(value, float) and not math.isfinite(value))):
        raise CommandError("bad_request", f"'{key}' must be a number")
    value = float(value)
    if not lo <= value <= hi:
        raise CommandError("bad_request", f"'{key}' must be between {lo} and {hi}")
    return value


def canvas_size(params, limits, default=(800, 600)):
    """Read ``width`` and ``height`` and check them against ``limits``
    (``config.current().limits``: ``max_canvas_dim`` per side,
    ``max_canvas_pixels`` in total). Returns ``(width, height)``.

    Not a number, or under 1: ``bad_request``. Over a limit: ``too_large``,
    with ``details`` naming the limit.
    """
    max_dim, max_pixels = limits["max_canvas_dim"], limits["max_canvas_pixels"]
    size = []
    for key, dflt in zip(("width", "height"), default):
        value = params.get(key, dflt)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))):
            raise CommandError("bad_request", f"'{key}' must be a number")
        value = int(value)
        if value < 1:
            raise CommandError("bad_request", f"'{key}' must be at least 1")
        if value > max_dim:
            raise CommandError(
                "too_large",
                f"'{key}' {value} is over the limit of {max_dim} pixels per side "
                f"(max_canvas_dim, {LIMITS_HINT})",
                {"limit": "max_canvas_dim", "max": max_dim, key: value})
        size.append(value)
    width, height = size
    if width * height > max_pixels:
        raise CommandError(
            "too_large",
            f"Canvas too large: {width}x{height} = {width * height} pixels is over the limit "
            f"of {max_pixels} (max_canvas_pixels, {LIMITS_HINT})",
            {"limit": "max_canvas_pixels", "max": max_pixels, "pixels": width * height})
    return width, height
