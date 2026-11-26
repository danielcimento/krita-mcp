"""Alpha statistics for lint (D14) and ``layer_stats``. Pure: no krita /
PyQt5 imports.

Buffers are alpha masks, one byte per pixel, ``w * h`` bytes, rows top
to bottom. NO PER-PIXEL PYTHON LOOPS: masks come from ``bytes.translate``,
counts from ``bytes.count`` or ``int.bit_count``, dilation from big-int
shifts; loops are over rows or over the kernel radius at most.

**Stray alpha** (api-design § Lint rules, ``stray-alpha``): a *weak* pixel
has ``0 < alpha < below``; a *strong* one has ``alpha >= below``. A weak
pixel is stray when no strong pixel lies within ``distance`` of it, i.e.
outside the strong mask dilated by ``distance``. "Within" is the kernel
of Krita's ``Selection.grow(r, r)`` (``kernel_half_widths``), which the
plugin uses for the dilation; ``dilate_shift`` computes the same kernel
here, as the fallback when ``grow`` is unavailable or misbehaves.
"""

import math

DEFAULT_SETTINGS = {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4}
ADDITIVE_MODES = ("either", "add", "linear_dodge")
MAX_DISTANCE = 256

# Translate tables over alpha bytes / mask bytes.
_NONZERO_1 = bytes([0] + [1] * 255)
_ONE_FF = bytes([0, 0xFF] + [0] * 254)


def strong_table(below):
    """Translate table: 1 for alpha >= ``below``, else 0."""
    return bytes(1 if v >= below else 0 for v in range(256))


def weak_table(below):
    """Translate table: 1 for ``0 < alpha < below``, else 0."""
    return bytes(1 if 0 < v < below else 0 for v in range(256))


def to_ff(mask01):
    """A 0/1 mask as 0x00/0xFF bytes (a Selection's "fully selected")."""
    return mask01.translate(_ONE_FF)


def normalize_settings(raw):
    """The lint settings as a plain dict: ``raw`` (a mapping, an object with
    attributes, or None, e.g. ``config.current().lint``) over
    ``DEFAULT_SETTINGS``. Missing or invalid values fall back to the
    default; config validation and its error messages are config.py's job.
    """
    out = dict(DEFAULT_SETTINGS)
    if raw is None:
        return out
    for key in DEFAULT_SETTINGS:
        if hasattr(raw, "get") and callable(raw.get):
            value = raw.get(key)
        else:
            value = getattr(raw, key, None)
        if value is not None and _valid(key, value):
            out[key] = value
    return out


def _valid(key, value):
    if key == "additive_mode":
        return value in ADDITIVE_MODES
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if key == "stray_alpha_below":
        return 1 <= value <= 255
    return 0 <= value <= MAX_DISTANCE  # stray_alpha_distance


def kernel_half_widths(r):
    """The dilation kernel of radius ``r`` as half-widths per row: entry
    ``|dy|`` (0..r) is the largest ``|dx|`` covered on row ``dy``.

    This is Krita's ``Selection.grow(r, r)`` kernel (verified 2026-11-25,
    one pixel grown by r = 1, 2, 3, 4, 5, 8 covered 9, 21, 45, 69, 101,
    241 pixels with exactly this shape): ``KisSelectionFilter::computeBorder``
    gives column ``i`` (``0 < |i| <= r``) the half-height
    ``round(sqrt(r^2 - (|i| - 0.5)^2))`` and column 0 the half-height
    ``r``. So the axis points at distance ``r`` are covered, and the shape
    is a little fuller than the Euclidean disc of radius ``r``: at r = 4
    it covers (3, 3) (distance 4.24) and (4, 2) (4.47), but not (4, 3).
    """
    if r <= 0:
        return [0]
    heights = [r] + [math.floor(math.sqrt(r * r - (i - 0.5) ** 2) + 0.5) for i in range(1, r + 1)]
    return [max(i for i in range(r + 1) if heights[i] >= dy) for dy in range(r + 1)]


def kernel_size(r):
    """The number of pixels in the radius-``r`` kernel."""
    return sum((2 * g + 1) * (1 if dy == 0 else 2) for dy, g in enumerate(kernel_half_widths(r)))


def dilate_shift(mask01, w, h, r):
    """The 0/1 ``mask01`` (``w * h``) dilated with the radius-``r`` kernel,
    as 0/1 bytes of the same size: the fallback for ``Selection.grow``.

    The rows are padded with ``r`` zero columns on each side (one ``join``
    over rows) and the whole buffer becomes one big int, one byte per
    pixel. Horizontal dilation by ``g`` ORs it with copies shifted by up
    to ``g`` bytes; the padding keeps a row's pixels from spilling into the
    next row. The vertical pass ORs each row offset ``dy`` of the
    horizontal dilation by ``g(dy)``, shifted by ``dy`` whole rows. That is
    about ``4r + 1`` big-int shifts and ORs, each O(buffer) in C; the
    pixels beyond the buffer's top or bottom fall away when it is turned
    back into bytes.
    """
    if r <= 0:
        return bytes(mask01)
    half = kernel_half_widths(r)
    pw = w + 2 * r
    pad = bytes(r)
    padded = b"".join(pad + mask01[y * w:(y + 1) * w] + pad for y in range(h))
    m = int.from_bytes(padded, "big")
    across = [m]
    for k in range(1, max(half) + 1):
        across.append(across[-1] | (m << (8 * k)) | (m >> (8 * k)))
    grown = across[half[0]]
    for dy in range(1, r + 1):
        row = across[half[dy]]
        shift = 8 * pw * dy
        grown |= (row << shift) | (row >> shift)
    n = pw * h
    grown &= (1 << (8 * n)) - 1
    out = grown.to_bytes(n, "big")
    return b"".join(out[y * pw + r:y * pw + r + w] for y in range(h))


def stray_count(alpha, w, h, below, distance, dilate=dilate_shift):
    """The number of weak pixels (``0 < alpha < below``) further than
    ``distance`` from every strong one (``alpha >= below``), for the
    ``w * h`` alpha bytes ``alpha``.

    ``dilate(strong01, w, h, distance)`` returns the strong mask dilated
    (bytes, nonzero = covered); it is only called when there are both weak
    and strong pixels and ``distance > 0``. Returns ``(count, dilated)``,
    ``dilated`` True when ``dilate`` was called.
    """
    weak = alpha.translate(weak_table(below))
    n_weak = weak.count(1)
    if n_weak == 0:
        return 0, False
    strong = alpha.translate(strong_table(below))
    if strong.count(1) == 0:
        return n_weak, False
    if distance <= 0:
        return n_weak, False  # a weak pixel is never strong itself
    grown = dilate(strong, w, h, distance)
    if len(grown) != w * h:
        raise ValueError(f"dilation gave {len(grown)} bytes for {w}x{h}")
    covered = int.from_bytes(grown.translate(_NONZERO_1), "big")
    return (int.from_bytes(weak, "big") & ~covered).bit_count(), True
