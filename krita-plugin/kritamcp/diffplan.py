"""Comparing two documents' layer trees and pixels, for ``diff_documents``
(api-design § krita_diff_documents). Pure: no krita / PyQt5 imports.

**Records.** Each document's nodes come in as plain dicts in stacking
order (top to bottom, depth-first, the Layers docker order; the root-level
selection mask left out): the layer object's ``id``, ``name``, ``path``,
``parent`` (None at the top level), ``type``, the ``PROPS`` and ``bounds``.
``with_indices`` adds ``index``, the position from the bottom among the
node's siblings (as in the layer object, but counted without the
selection mask).

**Matching** (``match``), in three passes, each only among the nodes the
passes before left unmatched:

1. ``uuid``: the same id in both documents (a ``.kra`` round trip and
   ``Document.clone()`` keep ids, D4);
2. ``path``: the same name path, when exactly one unmatched node in each
   document has it (a repeated path is left alone: there is no telling
   which copy is which);
3. ``name``: the same name, when the name is unique in each whole
   document (not only among the unmatched nodes).

**Pixels** (``diff_stats``): two layers' ``pixelData`` over one
rectangle, read in strips of rows. A row whose bytes are equal is skipped
(a C-level compare). For a row that differs, the two rows become big ints
and are XORed; ``x | x >> 8`` then ``| x >> 16`` folds each pixel's four
bytes into its lowest byte, and ``[0::4]`` keeps those, one byte per pixel,
nonzero where the pixels differ. Pixels that are fully transparent in both
rows count as equal whatever colour they hold (the colour under alpha 0
is invisible) and are counted apart, as ``alpha0_rgb_count``: the OR of
the two alpha rows says which pixels are visible in either. The largest
channel difference comes from 16-bit lanes (``max_abs_diff``). Both masks go through a
translate table to 0x00 / 0xFF and are ANDed as big ints; ``count`` gives
the changed pixels of the row and ``lstrip`` / ``rstrip`` of zero bytes
its first and last. So the work is O(rows) Python steps, each O(width)
in C.
"""

from .errors import CommandError

# Property deltas reported in ``changed``, in this order.
PROPS = ("type", "visible", "opacity", "blend_mode", "locked", "alpha_locked", "inherit_alpha",
         "pass_through", "has_layer_style")
PAINT = "paintlayer"

# Rows per strip: about 4 MP, so each side's buffer stays near 16 MB.
STRIP_PIXELS = 4 * 1024 * 1024

_TO_FF = bytes([0]) + bytes([0xFF]) * 255  # translate: 0 -> 0, anything else -> 0xFF


# --- records -------------------------------------------------------------------

def with_indices(records):
    """``records`` (stacking order) with ``index`` set on each: the count of
    later siblings, i.e. the position from the bottom within its parent."""
    below = {}
    out = []
    for r in reversed(records):
        key = r["parent"]
        n = below.get(key, 0)
        below[key] = n + 1
        out.append({**r, "index": n})
    out.reverse()
    return out


# --- matching ------------------------------------------------------------------

def _unmatched_groups(records, key, used):
    groups = {}
    for r in records:
        if r["id"] not in used:
            groups.setdefault(r[key], []).append(r["id"])
    return groups


def match(a, b):
    """``[(id_a, id_b, method)]`` in ``b``'s stacking order, ``method``
    one of ``uuid``, ``path``, ``name`` (see the module docstring)."""
    ids_a = {r["id"] for r in a}
    found = {}  # id_b -> (id_a, method)
    used_a = set()

    def pair(ia, ib, method):
        found[ib] = (ia, method)
        used_a.add(ia)

    for r in b:
        if r["id"] in ids_a:
            pair(r["id"], r["id"], "uuid")

    paths_a = _unmatched_groups(a, "path", used_a)
    for path, ids_b in _unmatched_groups(b, "path", found).items():
        ids = paths_a.get(path)
        if ids and len(ids) == 1 and len(ids_b) == 1:
            pair(ids[0], ids_b[0], "path")

    names_a, names_b = {}, {}
    for records, names in ((a, names_a), (b, names_b)):
        for r in records:
            names.setdefault(r["name"], []).append(r["id"])
    for r in b:
        ib = r["id"]
        if ib in found:
            continue
        ids = names_a.get(r["name"])
        if ids and len(ids) == 1 and len(names_b[r["name"]]) == 1 and ids[0] not in used_a:
            pair(ids[0], ib, "name")

    return [(found[r["id"]][0], r["id"], found[r["id"]][1]) for r in b if r["id"] in found]


# --- the structural diff ---------------------------------------------------------

def _brief(r):
    return {"id": r["id"], "name": r["name"], "path": r["path"], "type": r["type"]}


def head(ra, rb, method):
    """The fields every pair entry starts with: both ids, the path in ``b``
    (``path_a`` too when it differs) and the match method."""
    out = {"id_a": ra["id"], "id_b": rb["id"], "path": rb["path"]}
    if ra["path"] != rb["path"]:
        out["path_a"] = ra["path"]
    out["match"] = method
    return out


def diff(a, b):
    """The structural diff of two record lists (``with_indices`` applied).

    Returns ``{"pairs": [(record_a, record_b, method)], "added", "removed",
    "renamed", "moved", "changed", "matched": {uuid, path, name, total}}``:

    - ``added`` / ``removed``: ``{id, name, path, type}`` of the nodes only
      in ``b`` / only in ``a``, each in its document's stacking order;
    - ``renamed``: pairs whose names differ, with ``name: [a, b]``;
    - ``moved``: pairs whose parent or index differs, with ``parent: [a, b]``
      (ids, null at the top level), ``index: [a, b]`` (from the bottom) and
      ``parent_changed``. Parents are the same when both are top level or
      the parent in ``a`` is matched to the parent in ``b``;
    - ``changed``: pairs with property deltas, ``fields: {prop: [a, b]}``
      over ``PROPS``.

    Pair entries start with ``head`` and come in ``b``'s stacking order.
    """
    by_a = {r["id"]: r for r in a}
    by_b = {r["id"]: r for r in b}
    matched = match(a, b)
    a_to_b = {ia: ib for ia, ib, _ in matched}
    matched_b = {ib for _, ib, _ in matched}
    pairs = [(by_a[ia], by_b[ib], m) for ia, ib, m in matched]

    renamed, moved, changed = [], [], []
    counts = {"uuid": 0, "path": 0, "name": 0}
    for ra, rb, method in pairs:
        counts[method] += 1
        if ra["name"] != rb["name"]:
            renamed.append({**head(ra, rb, method), "name": [ra["name"], rb["name"]]})
        pa, pb = ra["parent"], rb["parent"]
        same_parent = (pa is None and pb is None) or (pa is not None and a_to_b.get(pa) == pb
                                                      and pb is not None)
        if not same_parent or ra["index"] != rb["index"]:
            moved.append({**head(ra, rb, method), "parent": [pa, pb],
                          "index": [ra["index"], rb["index"]],
                          "parent_changed": not same_parent})
        fields = {k: [ra[k], rb[k]] for k in PROPS if ra.get(k) != rb.get(k)}
        if fields:
            changed.append({**head(ra, rb, method), "fields": fields})
    counts["total"] = len(pairs)
    return {
        "pairs": pairs,
        "added": [_brief(r) for r in b if r["id"] not in matched_b],
        "removed": [_brief(r) for r in a if r["id"] not in a_to_b],
        "renamed": renamed,
        "moved": moved,
        "changed": changed,
        "matched": counts,
    }


def pixel_pairs(pairs):
    """The pairs whose pixels are compared: paint layers in both documents
    with pixels in at least one (``bounds`` not None)."""
    return [(ra, rb, m) for ra, rb, m in pairs
            if ra["type"] == PAINT and rb["type"] == PAINT
            and (ra["bounds"] is not None or rb["bounds"] is not None)]


# --- pixels ----------------------------------------------------------------------

def union(r1, r2):
    """The union of two ``(x, y, w, h)`` rectangles; either may be None."""
    if r1 is None:
        return r2
    if r2 is None:
        return r1
    x0, y0 = min(r1[0], r2[0]), min(r1[1], r2[1])
    x1, y1 = max(r1[0] + r1[2], r2[0] + r2[2]), max(r1[1] + r1[3], r2[1] + r2[3])
    return x0, y0, x1 - x0, y1 - y0


# --- pixel statistics (F003 K4) -----------------------------------------------

def _spread(data):
    """Each byte of ``data`` in its own 16-bit little-endian lane (low byte
    the value, high byte 0)."""
    out = bytearray(2 * len(data))
    out[0::2] = data
    return out


_LANE_FF = bytes([0, 0xFF]) + bytes(254)  # translate: 1 -> 0xFF, 0 -> 0


def max_abs_diff(ra, rb):
    """``max |a - b|`` over the byte pairs of two equal-length byte strings
    (0 when equal), without a per-byte loop. Each byte goes into a 16-bit
    lane; ``(A | 0x100 per lane) - B`` leaves ``256 + a - b`` in every lane
    (1..511: no borrow crosses lanes), whose low byte is ``a - b`` where
    the high byte is 1 (``a >= b``). The same with A and B swapped gives
    ``b - a`` where ``b >= a``; the larger of the two maxima is the
    answer."""
    n = len(ra)
    if n == 0 or ra == rb:
        return 0
    a = int.from_bytes(_spread(ra), "little")
    b = int.from_bytes(_spread(rb), "little")
    high = int.from_bytes(bytes([0, 1]) * n, "little")
    best = 0
    for x, y in ((a, b), (b, a)):
        lanes = ((x | high) - y).to_bytes(2 * n, "little")
        keep = lanes[1::2].translate(_LANE_FF)
        low = int.from_bytes(lanes[0::2], "little") & int.from_bytes(keep, "little")
        best = max(best, max(low.to_bytes(n, "little")))
    return best


def _expand4(mask):
    out = bytearray(4 * len(mask))
    for c in range(4):
        out[c::4] = mask
    return bytes(out)


def _span(mask, w):
    """``(count, first, last)`` of the nonzero bytes of ``mask`` (``w``
    bytes), or None when there are none."""
    count = w - mask.count(0)
    if count == 0:
        return None
    return count, w - len(mask.lstrip(b"\0")), len(mask.rstrip(b"\0")) - 1


def diff_row_stats(ra, rb, w):
    """Where two BGRA rows of ``w`` pixels differ, or None when they are
    equal: ``(visible, alpha0, max_diff)``. ``visible``: the pixels that
    differ with alpha > 0 in either row; ``alpha0``: those with alpha 0 in
    both whose colour bytes differ (invisible); each ``(count, first,
    last)`` (x offsets in the row) or None. ``max_diff``: the largest
    channel difference over the visible ones."""
    if ra == rb:
        return None
    x = int.from_bytes(ra, "little") ^ int.from_bytes(rb, "little")
    x |= x >> 8
    x |= x >> 16
    differs = int.from_bytes(x.to_bytes(4 * w, "little")[0::4].translate(_TO_FF), "little")
    seen = (int.from_bytes(ra[3::4], "little") | int.from_bytes(rb[3::4], "little"))
    visible = int.from_bytes(seen.to_bytes(w, "little").translate(_TO_FF), "little")
    hits = (differs & visible).to_bytes(w, "little")
    hidden = (differs & ~visible & ((1 << (8 * w)) - 1)).to_bytes(w, "little")
    v, z = _span(hits, w), _span(hidden, w)
    max_diff = 0
    if v is not None:
        m = int.from_bytes(_expand4(hits), "little")
        max_diff = max_abs_diff((int.from_bytes(ra, "little") & m).to_bytes(4 * w, "little"),
                                (int.from_bytes(rb, "little") & m).to_bytes(4 * w, "little"))
    return v, z, max_diff


class Region:
    """Accumulates changed pixels: ``count`` and the bounding box."""

    __slots__ = ("count", "top", "bottom", "left", "right")

    def __init__(self):
        self.count, self.top, self.bottom, self.left, self.right = 0, None, None, None, None

    def add(self, row, got):
        count, first, last = got
        self.count += count
        self.top = row if self.top is None else self.top
        self.bottom = row
        self.left = first if self.left is None else min(self.left, first)
        self.right = last if self.right is None else max(self.right, last)

    def bounds(self, x0):
        if self.count == 0:
            return None
        return (x0 + self.left, self.top, self.right - self.left + 1, self.bottom - self.top + 1)


def diff_stats(read_a, read_b, rect, strip_pixels=STRIP_PIXELS):
    """Compare two images over ``rect`` (``(x, y, w, h)``); ``read_a`` /
    ``read_b`` are ``read(x, y, w, h) -> BGRA bytes``. Returns
    ``{"changed_count", "bounds", "max_channel_diff", "alpha0_rgb_count",
    "alpha0_rgb_bounds"}`` (F003 K4): the visible changes (alpha > 0 in
    either image), their box and largest channel difference (0-255), and
    the pixels that are transparent in both but hold different colour (no
    visible effect: a PNG round trip or Krita's compositing can leave them)
    with their box. Boxes are ``(x, y, w, h)`` or None."""
    x, y, w, h = rect
    visible, alpha0, max_diff = Region(), Region(), 0
    if w >= 1 and h >= 1:
        rows = max(1, min(h, strip_pixels // w))
        stride = 4 * w
        for y0 in range(y, y + h, rows):
            n = min(rows, y + h - y0)
            da, db = read_a(x, y0, w, n), read_b(x, y0, w, n)
            if len(da) != stride * n or len(db) != stride * n:
                raise CommandError("krita_error",
                                   f"Read {len(da)} and {len(db)} bytes for {w}x{n} pixels")
            if da == db:
                continue
            for r in range(n):
                s = r * stride
                got = diff_row_stats(da[s:s + stride], db[s:s + stride], w)
                if got is None:
                    continue
                v, z, m = got
                if v is not None:
                    visible.add(y0 + r, v)
                    max_diff = max(max_diff, m)
                if z is not None:
                    alpha0.add(y0 + r, z)
    return {"changed_count": visible.count, "bounds": visible.bounds(x),
            "max_channel_diff": max_diff if visible.count else 0,
            "alpha0_rgb_count": alpha0.count, "alpha0_rgb_bounds": alpha0.bounds(x)}
