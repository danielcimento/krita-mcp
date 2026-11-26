"""Analysis (api-design § Analysis): the lint snapshot collector (D14).

``collect_snapshot(doc, settings)`` reads everything the lint rules need
in one pass, so the rules (``lint.py``) stay pure functions over a dict:
the document's size and colour space, every node's properties in global
stacking order, and per-paint-layer alpha statistics. ``krita_lint``
(P4.4) and ``krita_layer_stats`` (P5.3) reuse it; the ``lint_snapshot``
command returns it as is, for debugging and the headless suite.

Alpha statistics are read over the layer's own ``bounds()``, which is
exact and not clipped to the canvas (krita-api-notes § Nodes), so they
cover pixels past the canvas edge too. They need 8-bit RGBA pixels: on
any other document (or layer) ``alpha`` is null and ``document.pixel_stats``
is false; the ``color-space`` rule reports the document either way.

Stray alpha is ``alphastats.stray_count`` with the strong mask dilated by
``Selection.grow`` on a standalone ``Selection`` (never attached to the
document, so the user's selection and layer tree are untouched), or by
``alphastats.dilate_shift`` when ``grow`` is unavailable or fails a
self-check against it. Both use the same kernel, so they agree exactly.
"""

import time

from krita import Selection

from .. import alphastats, config, nodes, pixels, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError

PAINT = "paintlayer"
STRAY_METHODS = ("auto", "grow", "shift")

# distance -> True when Selection.grow matched dilate_shift on the self-check.
_grow_checked = {}


def lint_settings():
    """The lint settings from ``config.current().lint`` (when the config
    has that section), over the defaults."""
    return alphastats.normalize_settings(getattr(config.current(), "lint", None))


def grow_dilate(strong01, w, h, r):
    """``strong01`` (0/1 bytes, ``w * h``) dilated by ``Selection.grow(r,
    r)`` on a standalone Selection; the window ``(0, 0, w, h)`` read back
    (0/255 bytes). Grow is not clipped to the Selection's size (verified
    2026-11-25), so no padding is needed: every strong pixel is inside the
    window, and only the window is read."""
    sel = Selection()
    sel.resize(w, h)
    sel.setPixelData(alphastats.to_ff(strong01), 0, 0, w, h)
    sel.grow(r, r)
    return pixels.as_bytes(sel.pixelData(0, 0, w, h))


def _grow_works(r):
    """Whether ``Selection.grow(r, r)`` gives ``dilate_shift``'s result on
    a small test mask (a lone pixel, one at a corner, and a short line);
    cached per radius."""
    if r not in _grow_checked:
        n = 4 * r + 5
        mask = bytearray(n * n)
        mask[(n // 2) * n + n // 2] = 1
        mask[0] = 1
        mask[(n - 1) * n + 1:(n - 1) * n + 4] = b"\1\1\1"
        mask = bytes(mask)
        try:
            got = grow_dilate(mask, n, n, r).translate(bytes([0] + [1] * 255))
            ok = got == alphastats.dilate_shift(mask, n, n, r)
            why = "disagrees with the kernel"
        except Exception as e:  # missing method, a changed API, ...
            ok, why = False, f"failed: {e!r}"
        if not ok:
            print(f"[KritaMCP] Selection.grow({r}, {r}) {why}; using the shift fallback")
        _grow_checked[r] = ok
    return _grow_checked[r]


def _dilate_for(method, r):
    if r <= 0:
        return alphastats.dilate_shift  # never called: nothing to dilate
    if method == "shift" or (method == "auto" and not _grow_works(r)):
        return alphastats.dilate_shift
    return grow_dilate


def _rect(r):
    return None if r is None else {"x": r[0], "y": r[1], "w": r[2], "h": r[3]}


def alpha_stats(node, bounds, settings, method="auto"):
    """``{"pixel_count", "opaque_bounds", "stray_count"}`` for one 8-bit
    RGBA paint layer, read over ``bounds`` (``{"x", "y", "w", "h"}`` or
    None for a layer without pixels)."""
    if bounds is None:
        return {"pixel_count": 0, "opaque_bounds": None, "stray_count": 0}
    x, y, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
    data = pixels.as_bytes(node.pixelData(x, y, w, h))
    if len(data) != 4 * w * h:
        raise CommandError("krita_error",
                           f"Krita returned {len(data)} bytes for a {w}x{h} layer read")
    count = pixels.count_alpha(data, 1)
    ob = pixels.opaque_bounds(data, w, h, 1)
    if ob is not None:
        ob = (ob[0] + x, ob[1] + y, ob[2], ob[3])
    alpha = pixels.alpha(data)
    del data
    below, distance = settings["stray_alpha_below"], settings["stray_alpha_distance"]
    dilate = _dilate_for(method, distance)
    try:
        stray, _ = alphastats.stray_count(alpha, w, h, below, distance, dilate)
    except Exception as e:
        if method != "auto" or dilate is alphastats.dilate_shift:
            raise
        print(f"[KritaMCP] Selection.grow failed on a layer, using the shift fallback: {e!r}")
        stray, _ = alphastats.stray_count(alpha, w, h, below, distance, alphastats.dilate_shift)
    return {"pixel_count": count, "opaque_bounds": _rect(ob), "stray_count": stray}


def _is_rgba8(thing):
    return (thing.colorModel(), thing.colorDepth()) == pixels.RGBA8


SNAPSHOT_FIELDS = ("visible", "opacity", "blend_mode", "inherit_alpha", "alpha_locked",
                   "locked", "pass_through", "has_layer_style", "bounds")


def collect_snapshot(doc, settings=None, *, method="auto", timings=None):
    """The lint snapshot of ``doc`` (a live wrapper; api-design § krita_lint,
    Snapshot). ``settings``: the lint settings (``lint_settings()`` when
    None). ``method``: how stray alpha is dilated, ``"auto"`` (grow, else
    the fallback), ``"grow"`` or ``"shift"``; tests pick one.
    ``timings``: a list to append ``(node id, seconds)`` to, per paint
    layer read."""
    if method not in STRAY_METHODS:
        raise ValueError(f"method must be one of {STRAY_METHODS}, got {method!r}")
    settings = lint_settings() if settings is None else alphastats.normalize_settings(settings)
    doc.waitForDone()  # pending strokes or updates land before pixels are read
    pixel_stats = _is_rgba8(doc)
    document = {
        "width": doc.width(), "height": doc.height(),
        "color_model": doc.colorModel(), "color_depth": doc.colorDepth(),
        "color_profile": doc.colorProfile(), "pixel_stats": pixel_stats,
    }
    index, by_id = nodes.tree(doc)
    depth, out = {}, []
    for nid, node in by_id.items():  # walk order: global stacking order, top to bottom
        props = nodes.layer_object(node, index, ("path", "type", "parent") + SNAPSHOT_FIELDS,
                                   nid=nid)
        parent = props["parent"]  # None at the top level
        depth[nid] = 1 if parent is None else depth[parent] + 1
        alpha = None
        if props["type"] == PAINT and pixel_stats and _is_rgba8(node):
            start = time.perf_counter()
            alpha = alpha_stats(node, props["bounds"], settings, method)
            if timings is not None:
                timings.append((nid, time.perf_counter() - start))
        record = {"id": nid, "name": props["name"], "path": props["path"], "type": props["type"],
                  "parent": props["parent"], "depth": depth[nid], "order": len(out)}
        record.update((key, props[key]) for key in SNAPSHOT_FIELDS)
        record["alpha"] = alpha
        out.append(record)
    return {"document": document, "settings": settings, "nodes": out}


@command("lint_snapshot", timeout=LONG_TIMEOUT)
def cmd_lint_snapshot(params):
    """The lint snapshot of one document (debugging; ``krita_lint`` runs
    the rules over the same snapshot). Reads only."""
    handle, doc = registry.resolve_doc(params)
    return {"doc": handle, "snapshot": collect_snapshot(doc)}
