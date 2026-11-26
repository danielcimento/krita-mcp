"""What a pixel write changed, for the painting tools' reports (F005 K11).
Pure: no krita / PyQt5 imports.

Masks are ``bytes`` with one byte per pixel, 0 or 1, row by row. Like
``alphastats``, they are built with ``bytes.translate`` and big-int
operations, not per-pixel Python loops.
"""

NONZERO_1 = bytes([0] + [1] * 255)
STRIP_BYTES = 4 << 20  # changed_mask works through 1 Mpx at a time


def _at_least_1(level):
    return bytes(1 if v >= level else 0 for v in range(256))



def changed_mask(old, new):
    """The pixels (4 bytes each, any channel order) where ``old`` and
    ``new`` differ, as a 0/1 mask."""
    if len(old) != len(new) or len(old) % 4:
        raise ValueError(f"old is {len(old)} and new {len(new)} bytes; need equal multiples of 4")
    if old == new:
        return bytes(len(old) // 4)
    parts = []
    for start in range(0, len(old), STRIP_BYTES):  # bounded temporaries on a full-canvas write
        a, b = old[start:start + STRIP_BYTES], new[start:start + STRIP_BYTES]
        if a == b:
            parts.append(bytes(len(a) // 4))
            continue
        n = len(a)
        ones = (int.from_bytes(a, "little") ^ int.from_bytes(b, "little")).to_bytes(
            n, "little").translate(NONZERO_1)
        any_channel = 0
        for c in range(4):
            any_channel |= int.from_bytes(ones[c::4], "big")
        parts.append(any_channel.to_bytes(n // 4, "big"))
    return b"".join(parts)


def at_least(values, level):
    """``values`` (one byte per pixel) as a 0/1 mask of the bytes >= ``level``
    (1-255)."""
    return values.translate(_at_least_1(level))


def and_count(a01, b01):
    """How many positions are 1 in both 0/1 masks."""
    if not a01:
        return 0
    return (int.from_bytes(a01, "big") & int.from_bytes(b01, "big")).bit_count()


def mask_bounds(mask, w, h):
    """``(x, y, w, h)`` of the 1s in ``mask`` (``w * h``), or None."""
    top = bottom = None
    left, right = w, -1
    one = b"\1"
    for y in range(h):
        row = mask[y * w:(y + 1) * w]
        first = row.find(one)
        if first < 0:
            continue
        if top is None:
            top = y
        bottom = y
        left = min(left, first)
        right = max(right, row.rfind(one))
    if top is None:
        return None
    return left, top, right - left + 1, bottom - top + 1


def crop(mask, w, rect):
    """The part of ``mask`` (``w`` wide) inside ``rect`` (``(x, y, w, h)``
    in the mask's own coordinates)."""
    x, y, cw, ch = rect
    return b"".join(mask[(y + r) * w + x:(y + r) * w + x + cw] for r in range(ch))


def intersect(a, b):
    """The overlap of two ``(x, y, w, h)`` rects, or None."""
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1 - x0, y1 - y0


def hidden_text(hidden_by, layer_path):
    """Why the layer doesn't show: itself or an ancestor group hidden."""
    if hidden_by == layer_path:
        return "the layer is hidden"
    return f"the layer's group '{hidden_by}' is hidden"


def notes(report, layer_path):
    """The painting tools' ``notes`` (F005 K11) for a ``report`` with
    ``changed_px``, ``layer_visible``, ``hidden_by``, ``covered_pct``,
    ``fully_hidden`` and ``top_cover`` (``{id, path}`` or None)."""
    if not report["changed_px"]:
        return [{"code": "nothing_changed",
                 "message": "nothing changed: the layer already had these pixels there"}]
    if not report["layer_visible"]:
        return [{"code": "layer_hidden",
                 "message": "this change isn't visible in the composite: "
                            + hidden_text(report["hidden_by"], layer_path)}]
    if report.get("covered_pct") == 1:
        top = report.get("top_cover")
        cover = f" (top cover: '{top['path']}')" if top else ""
        if report.get("fully_hidden"):
            text = "this change isn't visible in the composite: 100% covered by layers above"
        else:
            text = ("this change hardly shows in the composite: 100% covered by layers above, "
                    "some pixels only partly")
        return [{"code": "change_covered", "message": text + cover}]
    return []
