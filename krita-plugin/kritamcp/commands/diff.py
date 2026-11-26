"""diff_documents (api-design § krita_diff_documents).

Matches the nodes of two open documents (by id, then name path, then
unique name: ``diffplan``) and reports what was added, removed, renamed,
moved and changed; with ``pixels``, which matched paint layers' pixels
differ (over the union of both layers' bounds) and whether the
projections differ. Reads only.

Pixels are read with ``pixelData`` over the union rectangle; outside a
layer's own data that returns its default pixel (transparent for a layer
made by ``createNode``, the fill colour for the ``Background`` a new
document gets), which is what the layer shows there.
"""

from .. import diffplan, nodes, pixels, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from .documents import doc_name
from .layers import _strict_item
from .pixels import _bounds, tile_window

KEYS = ("doc_a", "doc_b", "pixels", "ignore_alpha0_rgb")
_FIELDS = ["name", "path", "type", "parent", "visible", "opacity", "blend_mode", "locked",
           "alpha_locked", "inherit_alpha", "pass_through", "has_layer_style"]


def _doc(params, key):
    if params.get(key) is None:
        raise CommandError("bad_request",
                           f"'{key}' is required (a handle like 'doc-1', or the absolute path "
                           "of an open document)", {"param": key})
    return registry.resolve_doc(params, field=key)


def _records(doc):
    """``(records, {id: node})`` in stacking order (``diffplan``)."""
    index, by_id = nodes.tree(doc)
    records = []
    for nid in index:
        node = by_id[nid]
        obj = nodes.layer_object(node, index, _FIELDS, nid=nid)
        obj.pop("children", None)
        obj.pop("child_count", None)
        obj["bounds"] = _bounds(node)
        records.append(obj)
    return diffplan.with_indices(records), by_id


def _pixel_skip_reason(doc_a, doc_b):
    """Why the pixels can't be compared, as a warning, or None."""
    size_a, size_b = (doc_a.width(), doc_a.height()), (doc_b.width(), doc_b.height())
    if size_a != size_b:
        return {"code": "size_mismatch",
                "message": f"The documents differ in size ({size_a[0]}x{size_a[1]} and "
                           f"{size_b[0]}x{size_b[1]}): pixels were not compared",
                "size_a": {"w": size_a[0], "h": size_a[1]},
                "size_b": {"w": size_b[0], "h": size_b[1]}}
    spaces = [(d.colorModel(), d.colorDepth()) for d in (doc_a, doc_b)]
    if any(s != pixels.RGBA8 for s in spaces):
        return {"code": "not_rgba8",
                "message": "Pixels are compared for 8-bit RGBA documents only; these are "
                           f"{spaces[0][0]}/{spaces[0][1]} and {spaces[1][0]}/{spaces[1][1]}: "
                           "pixels were not compared",
                "color_spaces": [f"{m}/{d}" for m, d in spaces]}
    return None


def _reader(source):
    return lambda x, y, w, h: pixels.as_bytes(source.pixelData(x, y, w, h))


def _rect(r):
    return None if r is None else {"x": r[0], "y": r[1], "w": r[2], "h": r[3]}


def _stats(raw):
    """``diffplan.diff_stats`` with its boxes as ``{x, y, w, h}``."""
    return {**raw, "bounds": _rect(raw["bounds"]),
            "alpha0_rgb_bounds": _rect(raw["alpha0_rgb_bounds"])}


def _changed(stats, ignore_alpha0_rgb):
    return stats["changed_count"] > 0 or (not ignore_alpha0_rgb and stats["alpha0_rgb_count"] > 0)


def _window(node, bounds, canvas):
    """Where a layer's pixels can differ: its bounds, plus the part of its
    paint device's tiles inside the canvas, which is where colour under
    alpha 0 next to it can live (F003 E2). Not past the canvas: there a
    layer reads as its default pixel, which for the Background layer
    ``createDocument`` makes is opaque white."""
    if bounds is None:
        return None
    p = node.position()
    tiles = tile_window(bounds, (p.x(), p.y()))
    x0, y0 = max(tiles[0], 0), max(tiles[1], 0)
    x1, y1 = min(tiles[0] + tiles[2], canvas[0]), min(tiles[1] + tiles[3], canvas[1])
    if x1 <= x0 or y1 <= y0:
        return bounds
    return diffplan.union(bounds, (x0, y0, x1 - x0, y1 - y0))


def _pixel_changes(pairs, nodes_a, nodes_b, ignore_alpha0_rgb, canvas):
    """``(changes, alpha0_only)``: the matched paint layers whose pixels
    differ, with ``diffplan.diff_stats`` (K4); and how many differ only in
    colour under alpha 0 (left out of ``changes`` when ignored)."""
    out, alpha0_only = [], 0
    for ra, rb, method in diffplan.pixel_pairs(pairs):
        na, nb = nodes_a[ra["id"]], nodes_b[rb["id"]]
        rect = diffplan.union(_window(na, ra["bounds"], canvas),
                              _window(nb, rb["bounds"], canvas))
        stats = _stats(diffplan.diff_stats(_reader(na), _reader(nb), rect))
        if stats["changed_count"] == 0 and stats["alpha0_rgb_count"] > 0:
            alpha0_only += 1
        if _changed(stats, ignore_alpha0_rgb):
            out.append({**diffplan.head(ra, rb, method), **stats})
    return out, alpha0_only


def _document_changes(doc_a, doc_b):
    """The document properties that differ, ``{key: [a, b]}``."""
    out = {}
    for key, get in (("width", lambda d: d.width()), ("height", lambda d: d.height()),
                     ("color_model", lambda d: d.colorModel()),
                     ("color_depth", lambda d: d.colorDepth()),
                     ("color_profile", lambda d: d.colorProfile()),
                     ("resolution", lambda d: d.resolution())):
        a, b = get(doc_a), get(doc_b)
        if a != b:
            out[key] = [a, b]
    return out


@command("diff_documents", timeout=LONG_TIMEOUT)
def cmd_diff_documents(params):
    """Compare two open documents' trees, properties and (``pixels``)
    paint-layer pixels and projections. Reads only."""
    params = _strict_item(params, KEYS)
    with_pixels = params.get("pixels", True)
    if type(with_pixels) is not bool:
        raise CommandError("bad_request", f"'pixels' must be true or false, got {with_pixels!r}",
                           {"param": "pixels"})
    ignore_alpha0_rgb = params.get("ignore_alpha0_rgb", True)
    if type(ignore_alpha0_rgb) is not bool:
        raise CommandError("bad_request", "'ignore_alpha0_rgb' must be true or false, got "
                           f"{ignore_alpha0_rgb!r}", {"param": "ignore_alpha0_rgb"})
    handle_a, doc_a = _doc(params, "doc_a")
    handle_b, doc_b = _doc(params, "doc_b")
    if handle_a == handle_b:
        raise CommandError("bad_request",
                           f"'doc_a' and 'doc_b' are the same document ({handle_a}); "
                           "compare two different documents",
                           {"param": "doc_b", "doc": handle_a})
    doc_a.waitForDone()
    doc_b.waitForDone()  # pending strokes or updates land before anything is read

    records_a, nodes_a = _records(doc_a)
    records_b, nodes_b = _records(doc_b)
    found = diffplan.diff(records_a, records_b)

    warnings = []
    pixel_changes = projection_changed = projection = alpha0_only = None
    if with_pixels:
        skip = _pixel_skip_reason(doc_a, doc_b)
        if skip is not None:
            warnings.append(skip)
        else:
            pixel_changes, alpha0_only = _pixel_changes(found["pairs"], nodes_a, nodes_b,
                                                        ignore_alpha0_rgb,
                                                        (doc_a.width(), doc_a.height()))
            projection = _stats(diffplan.diff_stats(_reader(doc_a), _reader(doc_b),
                                                    (0, 0, doc_a.width(), doc_a.height())))
            projection_changed = _changed(projection, ignore_alpha0_rgb)

    counts = {key: len(found[key]) for key in ("added", "removed", "renamed", "moved", "changed")}
    counts["pixel_changes"] = None if pixel_changes is None else len(pixel_changes)
    counts["alpha0_rgb_only_layers"] = alpha0_only
    counts["matched"] = found["matched"]
    return {
        "doc_a": handle_a, "doc_b": handle_b,
        "name_a": doc_name(doc_a), "name_b": doc_name(doc_b),
        "added": found["added"], "removed": found["removed"], "renamed": found["renamed"],
        "moved": found["moved"], "changed": found["changed"],
        "pixel_changes": pixel_changes, "projection_changed": projection_changed,
        "projection": projection, "ignore_alpha0_rgb": ignore_alpha0_rgb,
        "document_changes": _document_changes(doc_a, doc_b),
        "counts": counts, "warnings": warnings,
    }

