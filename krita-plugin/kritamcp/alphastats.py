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

**Specks** (P5.10): the stray pixels split into 8-connected components;
those of at most ``stray_speck_max_px`` pixels are specks, the larger
ones *regions* (soft glows and auras that fade out over more than
``distance`` px: intended art). Only specks are lint findings.
``components`` labels them with run-length union-find over rows: O(runs)
Python iterations, the runs themselves found by ``re`` in C.
"""

import math
import re

DEFAULT_SETTINGS = {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4,
                    "stray_speck_max_px": 64}
ADDITIVE_MODES = ("either", "add", "linear_dodge")
MAX_DISTANCE = 256
MAX_SPECK_PX = 1_000_000
MAX_RUNS = 2_000_000  # components() stops past this many runs (a noise field)
MAX_SAMPLES = 5

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
    if key == "stray_speck_max_px":
        return 1 <= value <= MAX_SPECK_PX
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
    count, _, dilated = _stray(alpha, w, h, below, distance, dilate, want_mask=False)
    return count, dilated


def _stray(alpha, w, h, below, distance, dilate, want_mask):
    """``stray_count``'s work: ``(count, mask, dilated)``, ``mask`` the
    stray pixels as 0/1 bytes when ``want_mask``, else None."""
    weak = alpha.translate(weak_table(below))
    n_weak = weak.count(1)
    if n_weak == 0:
        return 0, (bytes(len(alpha)) if want_mask else None), False
    strong = alpha.translate(strong_table(below))
    if strong.count(1) == 0 or distance <= 0:  # a weak pixel is never strong itself
        return n_weak, (weak if want_mask else None), False
    grown = dilate(strong, w, h, distance)
    if len(grown) != w * h:
        raise ValueError(f"dilation gave {len(grown)} bytes for {w}x{h}")
    bits = _and_not(weak, grown)
    return bits.bit_count(), (bits.to_bytes(len(weak), "big") if want_mask else None), True


def _and_not(weak01, grown):
    covered = int.from_bytes(grown.translate(_NONZERO_1), "big")
    return int.from_bytes(weak01, "big") & ~covered


def stray_mask(weak01, grown):
    """The stray pixels, ``weak01 AND NOT grown``, as 0/1 bytes.

    ``weak01`` is a 0/1 mask (``weak_table``), ``grown`` the dilated strong
    mask (any nonzero byte is covered, so a Selection's 0/255 works), both
    the same length. A byte-wise AND over big ints: no carries, so every
    byte stays 0 or 1."""
    if len(weak01) != len(grown):
        raise ValueError(f"masks differ in size: {len(weak01)} and {len(grown)} bytes")
    return _and_not(weak01, grown).to_bytes(len(weak01), "big")


def empty_specks():
    """``stray_specks`` for a layer with no stray pixel."""
    return {"count": 0, "pixels": 0, "regions": 0, "samples": [], "bounds": None,
            "truncated": False}


def stray_stats(alpha, w, h, below, distance, speck_max_px, dilate=dilate_shift, origin=(0, 0)):
    """One layer's stray-alpha snapshot fields: ``(stray_count,
    stray_specks, dilated)``, for the ``w * h`` alpha bytes ``alpha``.

    ``stray_count`` and ``dilate`` are as in ``stray_count``.
    ``stray_specks`` is ``{"count", "pixels", "regions", "samples",
    "truncated"}`` from ``components(stray, w, h, speck_max_px)``, with
    the sample rectangles moved by ``origin`` (the layer bounds' top-left,
    so they are in canvas coordinates).
    """
    count, mask, dilated = _stray(alpha, w, h, below, distance, dilate, want_mask=True)
    if count == 0:
        return 0, empty_specks(), dilated
    c = components(mask, w, h, speck_max_px)
    ox, oy = origin
    samples = [{"x": r["x"] + ox, "y": r["y"] + oy, "w": r["w"], "h": r["h"]} for r in c["samples"]]
    b = c["bounds"]
    bounds = None if b is None else {"x": b["x"] + ox, "y": b["y"] + oy, "w": b["w"], "h": b["h"]}
    return count, {"count": c["specks"], "pixels": c["speck_pixels"], "regions": c["regions"],
                   "samples": samples, "bounds": bounds, "truncated": c["truncated"]}, dilated


_RUN = re.compile(rb"\x01+")


def components(mask01, w, h, max_size, max_runs=MAX_RUNS):
    """The 8-connected components of the 1s in the 0/1 mask ``mask01``
    (``w * h`` bytes): ``{"specks", "speck_pixels", "regions", "samples",
    "truncated"}``. A *speck* has at most ``max_size`` pixels; ``regions``
    counts the larger components. ``samples`` are the bounding boxes
    (``{x, y, w, h}``, mask coordinates) of up to ``MAX_SAMPLES`` specks,
    top-most first, then left-most; ``bounds`` the box around every speck
    (None without specks), so a caller can judge a trim without rendering.

    Run-length union-find over rows. Each row's runs of 1s come from one
    ``re.finditer`` (in C). A run ``[a, b)`` touches a run ``[c, d)`` of
    the row above iff ``c <= b and a <= d`` (the ranges overlap once
    widened by one pixel: 8-connectivity). A two-pointer sweep pairs the
    runs of neighbouring rows, so the Python work is O(runs + rows), never
    O(pixels). A run joins the component of the first run it touches;
    touching more merges them (union by size, path compression). Each
    root keeps its size and bounding box.

    After more than ``max_runs`` runs (a noise field) the scan stops at
    the end of that row: ``truncated`` is True and the counts cover the
    rows scanned (components cut off there may be counted as specks).
    """
    parent, size = [], []
    x0s, y0s, x1s, y1s = [], [], [], []  # per label; inclusive x1, y1

    def find(i):
        root = i
        while parent[root] != root:
            root = parent[root]
        while parent[i] != root:
            parent[i], i = root, parent[i]
        return root

    prev = []  # the previous row's runs: (start, end, label)
    runs = 0
    truncated = False
    finditer = _RUN.finditer
    for y in range(h):
        row = mask01[y * w:(y + 1) * w]
        if 1 not in row:
            prev = []
            continue
        cur = []
        j, n_prev = 0, len(prev)
        for m in finditer(row):
            a, b = m.span()
            while j < n_prev and prev[j][1] < a:
                j += 1  # ends left of this run's widened range, so of every later run's too
            root = -1
            k = j
            while k < n_prev and prev[k][0] <= b:
                r = find(prev[k][2])
                if root < 0:
                    root = r
                elif r != root:
                    if size[r] > size[root]:
                        r, root = root, r
                    parent[r] = root
                    size[root] += size[r]
                    x0s[root] = min(x0s[root], x0s[r])
                    y0s[root] = min(y0s[root], y0s[r])
                    x1s[root] = max(x1s[root], x1s[r])
                k += 1
            if root < 0:
                root = len(parent)
                parent.append(root)
                size.append(b - a)
                x0s.append(a)
                y0s.append(y)
                x1s.append(b - 1)
                y1s.append(y)
            else:
                size[root] += b - a
                if a < x0s[root]:
                    x0s[root] = a
                if b - 1 > x1s[root]:
                    x1s[root] = b - 1
                y1s[root] = y
            cur.append((a, b, root))
        runs += len(cur)
        prev = cur
        if runs > max_runs:
            truncated = True
            break

    specks, speck_pixels, regions = [], 0, 0
    for i, p in enumerate(parent):
        if p != i:
            continue
        if size[i] <= max_size:
            specks.append(i)
            speck_pixels += size[i]
        else:
            regions += 1
    specks.sort(key=lambda i: (y0s[i], x0s[i]))
    samples = [{"x": x0s[i], "y": y0s[i], "w": x1s[i] - x0s[i] + 1, "h": y1s[i] - y0s[i] + 1}
               for i in specks[:MAX_SAMPLES]]
    bounds = None
    if specks:
        bx0, by0 = min(x0s[i] for i in specks), min(y0s[i] for i in specks)
        bx1, by1 = max(x1s[i] for i in specks), max(y1s[i] for i in specks)
        bounds = {"x": bx0, "y": by0, "w": bx1 - bx0 + 1, "h": by1 - by0 + 1}
    return {"specks": len(specks), "speck_pixels": speck_pixels, "regions": regions,
            "samples": samples, "bounds": bounds, "truncated": truncated}


# --- layer_stats and trim_layers (P5.3) -----------------------------------

DEFAULT_BUCKETS = (0, 1, 8, 32, 128, 255)
MIN_BUCKETS, MAX_BUCKETS = 2, 16


def check_buckets(buckets):
    """``buckets`` as a list of histogram edges: 2 to 16 whole numbers,
    strictly increasing, each 0-255 (None: ``DEFAULT_BUCKETS``). Raises
    ``ValueError`` with the reason otherwise (the command turns it into
    ``bad_request``)."""
    if buckets is None:
        return list(DEFAULT_BUCKETS)
    if not isinstance(buckets, (list, tuple)):
        raise ValueError(f"must be a list of alpha values, got {type(buckets).__name__}")
    if not MIN_BUCKETS <= len(buckets) <= MAX_BUCKETS:
        raise ValueError(f"must have {MIN_BUCKETS} to {MAX_BUCKETS} edges, got {len(buckets)}")
    for v in buckets:
        if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v <= 255:
            raise ValueError(f"edges must be whole numbers from 0 to 255, got {v!r}")
    if any(b <= a for a, b in zip(buckets, buckets[1:])):
        raise ValueError(f"edges must be strictly increasing, got {list(buckets)}")
    return list(buckets)


def bucket_table(edges):
    """Translate table: alpha -> bucket index (``histogram``), and
    ``len(edges)`` for alpha below ``edges[0]`` (in no bucket)."""
    table = bytearray([len(edges)] * 256)
    bounds = list(edges[1:]) + [256]
    for i, (lo, hi) in enumerate(zip(edges, bounds)):
        table[lo:hi] = bytes([i]) * (hi - lo)
    return bytes(table)


def histogram(alpha, edges):
    """``{"edges", "counts"}``: ``counts[i]`` is the number of alpha bytes
    with ``edges[i] <= alpha < edges[i + 1]``; the last bucket is
    ``edges[-1] <= alpha <= 255``. So ``len(counts) == len(edges)``, and
    alpha values below ``edges[0]`` are in no bucket. One ``translate`` to
    bucket indexes, then one ``count`` per bucket."""
    idx = alpha.translate(bucket_table(edges))
    return {"edges": list(edges), "counts": [idx.count(i) for i in range(len(edges))]}


def beyond_canvas(opaque_bounds, canvas_w, canvas_h):
    """How far the rectangle ``opaque_bounds`` (``{x, y, w, h}``, canvas
    coordinates) reaches past each canvas edge: ``{"left", "top", "right",
    "bottom"}`` in pixels (0 on a side it doesn't cross), or None when it
    is None or inside the canvas. ``opaque_bounds`` is tight, so a side's
    overhang means a pixel lies that far out."""
    if opaque_bounds is None:
        return None
    r = opaque_bounds
    over = {"left": max(0, -r["x"]), "top": max(0, -r["y"]),
            "right": max(0, r["x"] + r["w"] - canvas_w),
            "bottom": max(0, r["y"] + r["h"] - canvas_h)}
    return over if any(over.values()) else None


def count_inside(alpha, w, h, origin, canvas_w, canvas_h):
    """The number of nonzero alpha bytes of a ``w * h`` mask placed with
    its top-left at ``origin`` (canvas coordinates) that lie on the
    ``canvas_w * canvas_h`` canvas. One ``count`` per row of the overlap."""
    ox, oy = origin
    x0, x1 = max(0, -ox), min(w, canvas_w - ox)
    y0, y1 = max(0, -oy), min(h, canvas_h - oy)
    if x0 >= x1 or y0 >= y1:
        return 0
    n = 0
    for y in range(y0, y1):
        row = y * w
        n += (x1 - x0) - alpha.count(0, row + x0, row + x1)
    return n


def trim_mask(alpha, w, h, threshold, near_px=None, dilate=dilate_shift):
    """The pixels ``trim_layers`` clears, as 0/1 bytes (``w * h``), and
    their count: ``0 < alpha < threshold`` (pixels already at alpha 0 are
    never counted), and with ``near_px`` only those outside the
    ``alpha >= threshold`` mask dilated by ``near_px`` (``dilate``, the
    ``Selection.grow`` kernel; with no such pixel every faint one is
    cleared). This is the stray-alpha test with ``below = threshold`` and
    ``distance = near_px``."""
    count, mask, _ = _stray(alpha, w, h, threshold, near_px or 0, dilate, want_mask=True)
    return mask, count


_ONE_00 = bytes([0xFF, 0] + [0xFF] * 254)  # 0/1 mask -> keep (0xFF) / clear (0x00)


def zero_pixels(data, mask01, w):
    """BGRA ``data`` (bytes, rows of ``w`` pixels) with every byte of the
    pixels where ``mask01`` is 1 set to 0 (colour too, so nothing hidden
    stays under the cleared alpha); other bytes unchanged. Rows without a
    hit are copied as they are, fully cleared rows become zeros, and mixed
    rows are ANDed as big ints with a keep mask (0xFF per kept byte, the
    pixel mask widened 4x by slice assignment): O(rows) Python steps."""
    keep = mask01.translate(_ONE_00)
    out = bytearray(data)
    stride = 4 * w
    rows = len(mask01) // w
    for y in range(rows):
        p = y * w
        hits = mask01.count(1, p, p + w)
        if hits == 0:
            continue
        b = y * stride
        if hits == w:
            out[b:b + stride] = bytes(stride)
            continue
        wide = bytearray(stride)
        for c in range(4):
            wide[c::4] = keep[p:p + w]
        row = int.from_bytes(out[b:b + stride], "little") & int.from_bytes(wide, "little")
        out[b:b + stride] = row.to_bytes(stride, "little")
    return bytes(out)
