"""Analysis (api-design § Analysis): the lint snapshot collector,
``lint`` (D14) and ``layer_stats`` (P5.3).

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

Stray alpha (``stray_count`` and ``stray_specks``) is
``alphastats.stray_stats`` with the strong mask dilated by
``Selection.grow`` on a standalone ``Selection`` (never attached to the
document, so the user's selection and layer tree are untouched), or by
``alphastats.dilate_shift`` when ``grow`` is unavailable or fails a
self-check against it. Both use the same kernel, so they agree exactly.
"""

import time

from krita import Selection

from .. import alphastats, batch, config, lint, nodes, pixels, refs, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError

PAINT = "paintlayer"
STRAY_METHODS = ("auto", "grow", "shift")

# distance -> True when Selection.grow matched dilate_shift on the self-check.
_grow_checked = {}


def stray_settings():
    """The stray-alpha settings from ``config.current().lint``, over the
    defaults (``layer_stats``, and the alpha part of the lint snapshot)."""
    return alphastats.normalize_settings(getattr(config.current(), "lint", None))


def lint_settings(raw=None):
    """The lint snapshot's ``settings`` (F004): the stray-alpha settings,
    ``reference_pattern`` and ``disable`` from the config's ``lint``
    section, and the ``live2d`` section, over ``lint.default_settings()``.
    ``raw``: a settings dict to use instead of the config (tests); values
    are checked by ``lint.run``, not here."""
    if raw is None:
        cfg = config.current()
        raw = {**config.plain(getattr(cfg, "lint", {}) or {}),
               "live2d": config.plain(getattr(cfg, "live2d", {}) or {})}
    out = lint.default_settings()
    out.update(alphastats.normalize_settings(raw))
    for key in ("reference_pattern", "disable"):
        if key in raw:
            out[key] = list(raw[key]) if isinstance(raw[key], tuple) else raw[key]
    for name in lint.PACKS:
        if isinstance(raw.get(name), dict):
            out[name] = {**out[name], **config.plain(raw[name])}
    if "additive_mode" in raw and "additive_mode" not in (raw.get("live2d") or {}):
        out["live2d"]["additive_mode"] = raw["additive_mode"]  # an F003-shaped settings dict
    return out


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


def read_bounds(node, bounds):
    """The BGRA bytes of ``node`` over ``bounds`` (``{"x", "y", "w", "h"}``),
    checked for size."""
    x, y, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
    data = pixels.as_bytes(node.pixelData(x, y, w, h))
    if len(data) != 4 * w * h:
        raise CommandError("krita_error",
                           f"Krita returned {len(data)} bytes for a {w}x{h} layer read")
    return data


def dilated(strong01, w, h, r, method="auto"):
    """``strong01`` (0/1 bytes, ``w * h``) dilated by the radius-``r``
    ``Selection.grow`` kernel (nonzero bytes = covered): ``grow`` on a
    standalone Selection, or ``alphastats.dilate_shift`` (``method``
    ``"shift"``, or ``"auto"`` when grow fails its self-check or raises)."""
    dilate = _dilate_for(method, r)
    try:
        return dilate(strong01, w, h, r)
    except Exception as e:
        if method != "auto" or dilate is alphastats.dilate_shift:
            raise
        print(f"[KritaMCP] Selection.grow failed on a layer, using the shift fallback: {e!r}")
        return alphastats.dilate_shift(strong01, w, h, r)


def data_stats(data, bounds, settings, method="auto"):
    """``alpha_stats`` for BGRA ``data`` already read over ``bounds`` (not
    None)."""
    x, y, w, h = bounds["x"], bounds["y"], bounds["w"], bounds["h"]
    count = pixels.count_alpha(data, 1)
    ob = pixels.opaque_bounds(data, w, h, 1)
    if ob is not None:
        ob = (ob[0] + x, ob[1] + y, ob[2], ob[3])
    alpha = pixels.alpha(data)
    del data  # frees the BGRA buffer before the dilation when the caller passed a temporary
    below, distance = settings["stray_alpha_below"], settings["stray_alpha_distance"]
    speck_max = settings["stray_speck_max_px"]

    def dilate(strong01, dw, dh, r):
        return dilated(strong01, dw, dh, r, method)

    stray, specks, _ = alphastats.stray_stats(alpha, w, h, below, distance, speck_max,
                                              dilate, (x, y))
    return {"pixel_count": count, "opaque_bounds": _rect(ob), "stray_count": stray,
            "stray_specks": specks}


def alpha_stats(node, bounds, settings, method="auto"):
    """``{"pixel_count", "opaque_bounds", "stray_count", "stray_specks"}``
    for one 8-bit RGBA paint layer, read over ``bounds`` (``{"x", "y",
    "w", "h"}`` or None for a layer without pixels)."""
    if bounds is None:
        return {"pixel_count": 0, "opaque_bounds": None, "stray_count": 0,
                "stray_specks": alphastats.empty_specks()}
    return data_stats(read_bounds(node, bounds), bounds, settings, method)


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
    settings = lint_settings(settings)
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


@command("lint", timeout=LONG_TIMEOUT)
def cmd_lint(params):
    """``krita_lint`` (api-design § krita_lint): the lint rules over a fresh
    snapshot of one document. ``rules`` (rule ids and/or set names, default
    ``["default"]``) and ``severity_min`` are checked before the snapshot
    is collected; bad values are ``bad_request``. The settings come from
    the config's ``lint`` and ``live2d`` sections (F004: the Live2D rules
    are off unless ``live2d.enabled``; ``rules_off`` lists what the
    requested sets left out). Reads only."""
    rules = params.get("rules")
    severity_min = params.get("severity_min")
    lint.check_args(rules, severity_min)
    group_by = params.get("group_by") or "none"
    if group_by not in GROUP_BY:
        raise CommandError("bad_request", f"'group_by' must be one of {', '.join(GROUP_BY)}, "
                           f"got {group_by!r}", {"param": "group_by"})
    limit, offset = _whole(params, "limit", None, 1, 10_000), _whole(params, "offset", 0, 0, 10**7)
    handle, doc = registry.resolve_doc(params)
    snapshot = collect_snapshot(doc)
    result = lint.run(snapshot, rules, severity_min)
    out = {"doc": handle, "counts": result["counts"], "total": len(result["findings"]),
           "rules_run": result["rules_run"], "rules_off": result["rules_off"],
           "settings": snapshot["settings"],
           "pixel_stats": snapshot["document"]["pixel_stats"]}
    if group_by == "rule":
        out["group_by"] = "rule"
        out["by_rule"] = group_findings(result["findings"], limit, offset)
    else:
        out["findings"], paging = _page(result["findings"], limit, offset)
        out.update(paging)
    return out


GROUP_BY = ("none", "rule")


def _whole(params, key, default, low, high):
    value = params.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise CommandError("bad_request", f"'{key}' must be a whole number from {low} to {high}, "
                           f"got {value!r}", {"param": key})
    return value


def _page(items, limit, offset):
    """``(page, paging)``: ``items[offset:offset + limit]`` (all from
    ``offset`` without a limit) and, when paging was asked for, ``{offset,
    limit, next_offset}`` (``next_offset`` None at the end)."""
    page = items[offset:] if limit is None else items[offset:offset + limit]
    if limit is None and not offset:
        return page, {}
    end = offset + len(page)
    return page, {"offset": offset, "limit": limit,
                  "next_offset": end if end < len(items) else None}


def group_findings(findings, limit, offset):
    """``{rule: {severity, count, items, next_offset}}`` in the order rules
    first appear in ``findings`` (F003 K3): each rule's findings counted,
    ``items`` the page ``[offset:offset + limit]`` of them. Items leave out
    ``rule`` and ``severity``, which their group already gives (F004 G11:
    the smoke test's ``lint(all)`` stays under 8 KB)."""
    grouped = {}
    for f in findings:
        grouped.setdefault(f["rule"], []).append(f)
    out = {}
    for rule_id, items in grouped.items():
        page, paging = _page(items, limit, offset)
        end = offset + len(page)
        out[rule_id] = {"severity": items[0]["severity"], "count": len(items),
                        "items": [{k: v for k, v in f.items() if k not in GROUP_KEYS} for f in page],
                        "next_offset": paging.get("next_offset",
                                                  end if end < len(items) else None)}
    return out


GROUP_KEYS = ("rule", "severity")  # given once per group, not per item


# --- layer_stats (P5.3) ----------------------------------------------------

def check_layer_refs(index, params, action):
    """``params["ids"]`` resolved against ``index``: a list of 1-2000 layer
    refs, ``/`` refused, duplicates dropped (first kept). Errors carry
    ``details.index`` / ``field`` (``batch.validate_all``)."""
    def check(i, ref):
        if ref == refs.ROOT:
            raise CommandError("bad_request", f"the root '/' is not a layer ({action} takes "
                               "layer refs)", {"param": "ids"})
        return refs.resolve(index, ref, field="id")
    nids = batch.validate_all(params.get("ids"), check, field="ids")
    seen, out = set(), []
    for i, nid in enumerate(nids):
        if nid not in seen:
            seen.add(nid)
            out.append((i, nid))
    return out


def _buckets(params):
    try:
        return alphastats.check_buckets(params.get("buckets"))
    except ValueError as e:
        raise CommandError("bad_request", f"'buckets' {e}", {"param": "buckets"}) from None


def _layer_stats(node, nid, index, settings, edges, canvas):
    bounds = nodes.layer_object(node, index, ("bounds",), nid=nid)["bounds"]
    out = {"id": nid, "path": index.path_of(nid), "bounds": bounds}
    if bounds is None:
        out.update(opaque_bounds=None, pixel_count=0, opaque_count=0,
                   alpha_histogram={"edges": list(edges), "counts": [0] * len(edges)},
                   empty=True, beyond_canvas=None, stray_count=0,
                   stray_specks=alphastats.empty_specks())
        return out
    data = read_bounds(node, bounds)
    stats = data_stats(data, bounds, settings)
    alpha = pixels.alpha(data)
    del data
    beyond = alphastats.beyond_canvas(stats["opaque_bounds"], *canvas)
    if beyond is not None:
        inside = alphastats.count_inside(alpha, bounds["w"], bounds["h"],
                                         (bounds["x"], bounds["y"]), *canvas)
        beyond["pixels"] = stats["pixel_count"] - inside
    out.update(opaque_bounds=stats["opaque_bounds"], pixel_count=stats["pixel_count"],
               opaque_count=alpha.count(255),
               alpha_histogram=alphastats.histogram(alpha, edges),
               empty=stats["pixel_count"] == 0, beyond_canvas=beyond,
               stray_count=stats["stray_count"], stray_specks=stats["stray_specks"])
    return out


@command("layer_stats", timeout=LONG_TIMEOUT)
def cmd_layer_stats(params):
    """``krita_layer_stats`` (api-design § krita_layer_stats): per paint
    layer, bounds, opaque bounds, pixel counts, an alpha histogram, the
    overhang past the canvas and the stray-alpha numbers (lint's settings).
    Other layer types are listed in ``skipped``. Reads only."""
    handle, doc = registry.resolve_doc(params)
    edges = _buckets(params)
    pixels.require_rgba8(doc)
    doc.waitForDone()  # pending strokes or updates land before pixels are read
    index, by_id = nodes.tree(doc)
    targets = check_layer_refs(index, params, "layer_stats")
    settings = stray_settings()
    canvas = (doc.width(), doc.height())
    layers, skipped = [], []
    for _, nid in targets:
        node, kind = by_id[nid], index.get(nid)["type"]
        if kind != PAINT:
            skipped.append({"id": nid, "path": index.path_of(nid), "type": kind,
                            "reason": "not a paint layer (only paint layers have pixels of "
                                      "their own; render a group with krita_render)"})
        elif not _is_rgba8(node):
            skipped.append({"id": nid, "path": index.path_of(nid), "type": kind,
                            "reason": f"the layer is {node.colorModel()}/{node.colorDepth()}, "
                                      "not 8-bit RGBA"})
        else:
            layers.append(_layer_stats(node, nid, index, settings, edges, canvas))
    return {"doc": handle, "canvas": {"w": canvas[0], "h": canvas[1]}, "buckets": edges,
            "settings": settings, "layers": layers, "skipped": skipped}
