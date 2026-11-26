"""Pixel commands (api-design § Pixels): export_layers, import_png.

Pixels go through ``kritamcp.pixels`` (D5: QImage, no numpy, 8-bit RGBA
only). The undo inverse of a pixel write keeps the old pixels of the
rectangles it changes (D6) and writes them back on the layer it looks up
in the document passed at call time.
"""

import dataclasses
import os
import traceback

from .. import batch, blendmodes, layerprops, manifest, nodes, paths, pixels, refs, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from .documents import doc_name
from .layers import GROUP, PAINT, _check_coord, _dry_run, _live

MODES = ("replace", "over")


def _rect(x, y, w, h):
    return {"x": x, "y": y, "w": w, "h": h}


def _bounds(node):
    """The node's bounds as ``(x, y, w, h)``, or None when it has none
    (a zeroed layer reports (0, 0, 0, 0) with ``hasExtents()`` True)."""
    if not node.hasExtents():
        return None
    r = node.bounds()
    if r.width() <= 0 or r.height() <= 0:
        return None
    return r.x(), r.y(), r.width(), r.height()


def _union(a, b):
    x0, y0 = min(a[0], b[0]), min(a[1], b[1])
    x1, y1 = max(a[0] + a[2], b[0] + b[2]), max(a[1] + a[3], b[1] + b[3])
    return x0, y0, x1 - x0, y1 - y0


def _area(r):
    return r[2] * r[3]


def _write(node, data, rect, what):
    if not node.setPixelData(data, *rect):
        raise CommandError("krita_error", f"Krita refused the pixels for {what}")


class RestorePixels:
    """The inverse of a pixel write: write back the old pixels of each
    ``(rect, bytes)`` patch (snapshots taken before the write, so overlaps
    agree) on the layer with ``node_id``, looked up in the document passed
    at call time (batch.py: never a captured wrapper). ``nbytes``: the
    snapshot size, for the journal's caps; the patches are ``batch.Blob``s
    (``blobs``), which the journal may spill to disk.

    Applying it first reads the layer's current pixels over the same
    rectangles and returns them as a new ``RestorePixels`` (the redo). If a
    write fails, the pixels read are written back before re-raising."""

    __slots__ = ("node_id", "patches", "nbytes")

    def __init__(self, nid, patches):
        self.node_id = nid
        self.patches = tuple((tuple(rect), data if isinstance(data, batch.Blob)
                              else batch.Blob(data)) for rect, data in patches)
        self.nbytes = sum(blob.nbytes for _, blob in self.patches)

    @property
    def node_ids(self):
        return (self.node_id,)

    @property
    def blobs(self):
        return tuple(blob for _, blob in self.patches)

    def check(self, doc):
        node = nodes.node_by_id(doc, self.node_id)
        if node is None:
            return f"Layer {self.node_id} is not in the document (any more)"
        if node.type() != PAINT:
            return f"Layer {self.node_id} is a {node.type()}, not a paint layer"
        return None

    def __call__(self, doc):
        node = _live(doc, self.node_id)
        datas = [blob.load() for _, blob in self.patches]  # a missing spill file changes nothing
        current = [(rect, pixels.as_bytes(node.pixelData(*rect))) for rect, _ in self.patches]
        what = f"layer {self.node_id} (restoring old pixels)"
        try:
            for (rect, _), data in zip(self.patches, datas):
                _write(node, data, rect, what)
        except Exception:
            try:
                for rect, data in current:
                    _write(node, data, rect, what)
            except Exception:
                print(f"[KritaMCP] could not put back the pixels of layer {self.node_id}:")
                traceback.print_exc()
            raise
        return RestorePixels(self.node_id, current)

    def __repr__(self):
        rects = ", ".join(f"{r[2]}x{r[3]}@({r[0]},{r[1]})" for r, _ in self.patches)
        return f"RestorePixels({self.node_id}, {rects})"


@dataclasses.dataclass
class _Import:
    nid: str
    png: str
    size: tuple
    x: int
    y: int
    mode: str
    clear_outside: bool
    old_bounds: object  # (x, y, w, h) or None, read at validation

    @property
    def rect(self):
        return self.x, self.y, self.size[0], self.size[1]

    def snapshot_rects(self):
        """The rectangles whose old pixels the inverse keeps: the PNG's, plus
        (``clear_outside``) the layer's old bounds; one bounding rectangle
        when that is no bigger than the two apart."""
        if not self.clear_outside or self.old_bounds is None:
            return [self.rect]
        union = _union(self.rect, self.old_bounds)
        if _area(union) <= _area(self.rect) + _area(self.old_bounds):
            return [union]
        return [self.rect, self.old_bounds]

    def changed(self):
        """The bounding rectangle of everything the import may change."""
        if self.clear_outside and self.old_bounds is not None:
            return _union(self.rect, self.old_bounds)
        return self.rect


def _bool(params, key, default=False):
    value = params.get(key, default)
    if value is None:
        return default
    if type(value) is not bool:
        raise CommandError("bad_request", f"'{key}' must be true or false, got {value!r}",
                           {"param": key})
    return value


def _validate(doc, params):
    ref = params.get("id")
    if ref is None:
        raise CommandError("bad_request", "'id' is required (a layer ref)", {"param": "id"})
    node = nodes.resolve_node(doc, ref, field="id")
    kind = node.type()
    if kind != PAINT:
        raise CommandError("conflict",
                           f"Layer {ref!r} is a {kind}; import_png needs a paint layer",
                           {"id": nodes.node_id(node), "type": kind})
    pixels.require_rgba8(doc)

    given = params.get("png")
    if not isinstance(given, str) or given == "":
        raise CommandError("bad_request", "'png' is required (the path of a PNG file)",
                           {"param": "png"})
    path = paths.resolve_allowed_path(given, must_exist=True)
    size = pixels.png_size(path)
    pixels.check_png_limits(size, given)
    given_xy = {k: params[k] for k in ("x", "y") if params.get(k) is not None}
    x, y = _check_coord(given_xy, "x"), _check_coord(given_xy, "y")

    mode = params.get("mode", "replace")
    if mode is None:
        mode = "replace"
    if mode not in MODES:
        raise CommandError("bad_request", f"'mode' must be 'replace' or 'over', got {mode!r}",
                           {"param": "mode", "valid": list(MODES)})
    clear_outside = _bool(params, "clear_outside")
    if clear_outside and mode != "replace":
        raise CommandError("bad_request", "'clear_outside' is only valid with mode 'replace'",
                           {"param": "clear_outside"})
    return _Import(nid=nodes.node_id(node), png=path, size=size, x=x, y=y, mode=mode,
                   clear_outside=clear_outside, old_bounds=_bounds(node))


def _apply(doc, plan):
    """``apply_fn`` for ``batch.run``: decode, snapshot, write. On failure
    the old pixels are written back before re-raising (batch.py: the
    failing item cleans up after itself)."""
    node = _live(doc, plan.nid)
    data, w, h = pixels.load_png_bgra(plan.png)  # before any change: a bad file leaves nothing
    if (w, h) != tuple(plan.size):
        raise CommandError("conflict", f"{plan.png} changed since validation "
                           f"({plan.size[0]}x{plan.size[1]}, now {w}x{h})")
    snapshots = [(r, pixels.as_bytes(node.pixelData(*r))) for r in plan.snapshot_rects()]
    inverse = RestorePixels(plan.nid, snapshots)
    what = f"layer {plan.nid}"
    try:
        if plan.mode == "over":
            ((_, existing),) = snapshots  # "over" snapshots just the PNG's rect
            data = pixels.composite_over(existing, data, w, h)
        elif plan.clear_outside and plan.old_bounds is not None:
            _write(node, bytes(4 * _area(plan.old_bounds)), plan.old_bounds, what)
        _write(node, data, plan.rect, what)
    except Exception:
        try:
            inverse(doc)
        except Exception:
            print(f"[KritaMCP] import_png: could not restore layer {plan.nid}:")
            traceback.print_exc()
        raise
    return plan.changed(), inverse


@command("import_png", timeout=LONG_TIMEOUT, mutating=True)
def cmd_import_png(params):
    """Write a PNG into a paint layer at (x, y): ``replace`` its pixels
    there (``clear_outside``: and clear the rest of the layer) or composite
    ``over`` them. One journal entry; ``dry_run`` validates only."""
    handle, doc = registry.resolve_doc(params)
    dry_run = _dry_run(params)
    plan = _validate(doc, params)
    head = {"doc": handle, "layer": plan.nid, "png": plan.png, "mode": plan.mode,
            "clear_outside": plan.clear_outside}
    if dry_run:
        return {**head, "dry_run": True, "would_change": _rect(*plan.changed()), "warnings": []}

    outcome = batch.run([plan], _apply, doc, label="import_png", field="png")
    doc.refreshProjection()
    doc.waitForDone()
    warnings = []
    if not batch.JOURNALS.get(handle).push("import_png", outcome.inverses, outcome.nbytes):
        warnings.append({
            "code": "not_undoable",
            "message": f"The old pixels take {outcome.nbytes} bytes, over the undo journal's "
                       "memory and disk caps: this import can't be undone, and the agent undo "
                       "journal was cleared",
            "nbytes": outcome.nbytes})
    changed = outcome.results[0]
    bounds = _bounds(_live(doc, plan.nid))
    return {**head, "dry_run": False, "changed": _rect(*changed),
            "bounds": None if bounds is None else _rect(*bounds), "warnings": warnings}


# --- export_layers ---------------------------------------------------------

MASKS = frozenset({"transparencymask", "filtermask", "transformmask", "colorizemask",
                   "selectionmask"})
MANIFEST_NAME = "manifest.json"


def _ids_param(params, index):
    """``ids``: None for every layer (``"all"``, the default, or a list
    naming ``"/"``), else the resolved ids."""
    ids = params.get("ids", "all")
    if ids is None or ids == "all":
        return None
    if not isinstance(ids, list) or not ids:
        raise CommandError("bad_request",
                           "'ids' must be 'all' or a non-empty list of layer refs",
                           {"param": "ids"})
    resolved = refs.resolve_many(index, ids, allow_root=True, field="ids")
    return None if refs.ROOT in resolved else resolved


def _png_level(params):
    level = params.get("png_level")
    if level is not None and (isinstance(level, bool) or not isinstance(level, int)
                              or not 0 <= level <= 9):
        raise CommandError("bad_request",
                           f"'png_level' must be a whole number from 0 to 9, or null; got {level!r}",
                           {"param": "png_level"})
    return level


def _selection(index, selected):
    """``(chosen, structural)`` id sets for a subset export: ``chosen`` is
    the selected nodes with their whole subtrees, ``structural`` the
    ancestor groups of those that are not chosen themselves (written as
    group entries so the manifest keeps its nesting; no projection)."""
    chosen = set()
    for nid in selected:
        if nid in chosen:
            continue
        stack = [nid]
        while stack:
            n = stack.pop()
            chosen.add(n)
            stack.extend(index.children(n))
    structural = {a for nid in chosen for a in index.ancestors(nid)} - chosen
    return chosen, structural


def _create_order(index):
    """``("node", id)`` items depth-first, siblings bottom to top, parents
    before their children (a layer's masks right after it), with a
    ``("projection", id)`` item after each group's subtree."""
    stack = [("node", nid) for nid in index.children(refs.ROOT)]  # top to bottom: pops bottom first
    while stack:
        item = stack.pop()
        yield item
        kind, nid = item
        if kind == "node":
            if index.get(nid)["type"] == GROUP:
                stack.append(("projection", nid))
            stack.extend(("node", c) for c in index.children(nid))


def _write_pixels(read, bounds, crop, canvas, path, level, written):
    """Write the pixels ``read(x, y, w, h)`` gives within ``bounds`` as a
    PNG at ``path``. Returns ``(rect, off_canvas, nbytes)``, or None when
    no pixel has alpha > 0 (nothing written).

    With ``crop`` the PNG is the opaque bounds (alpha >= 1) of the
    ``bounds`` window, ``rect`` where it goes on the canvas; without, the
    whole canvas at (0, 0). ``off_canvas``: the opaque bounds reach past
    the canvas edge."""
    if bounds is None:
        return None
    bx, by, bw, bh = bounds
    data = pixels.as_bytes(read(bx, by, bw, bh))
    opaque = pixels.opaque_bounds(data, bw, bh)
    if opaque is None:
        return None
    x, y, w, h = bx + opaque[0], by + opaque[1], opaque[2], opaque[3]
    cw, ch = canvas
    off_canvas = x < 0 or y < 0 or x + w > cw or y + h > ch
    if crop:
        if (w, h) != (bw, bh):
            data = pixels.crop(data, bw, bh, opaque)
    else:
        x, y, w, h = 0, 0, cw, ch
        data = pixels.as_bytes(read(0, 0, cw, ch))
    existed = os.path.lexists(path)
    nbytes = pixels.save_png(data, path, w, h, level=level)
    if not existed:
        written.append(path)
    return _rect(x, y, w, h), off_canvas, nbytes


def _recreatable(node):
    """Why ``create_layers`` would refuse this node's entry, or None."""
    try:
        layerprops.check_name(node.name())
    except CommandError as e:
        return f"name: {e.message}"
    if not blendmodes.is_valid(node.blendingMode()):
        return f"blend_mode {node.blendingMode()!r} is not a Krita blend-mode id"
    return None


class _Exporter:
    """One export_layers call: walks the tree once, writes the PNGs and
    collects manifest entries, ``skipped`` and the warning lists."""

    def __init__(self, doc, index, by_id, out_dir, opts):
        self.doc, self.index, self.by_id, self.dir = doc, index, by_id, out_dir
        self.crop, self.include_hidden, self.include_groups, self.level = opts
        self.canvas = (doc.width(), doc.height())
        self.entries, self.skipped, self.written = [], [], []
        self.keys = {}       # node id -> key of its (non-projection) entry
        self.omitted = {}    # node id -> skip reason
        self.nbytes = 0
        self.count = 0
        self.masked, self.off_canvas, self.unsupported, self.not_recreatable = [], [], [], []
        self.pending = []    # (position, group id, entry) of projection entries

    def _skip(self, nid, reason):
        self.omitted[nid] = reason
        self.skipped.append({"id": nid, "path": self.index.path_of(nid),
                             "type": self.index.get(nid)["type"], "reason": reason})

    def _reason(self, nid, node):
        parent = self.index.parent(nid)
        if self.omitted.get(parent) in ("hidden", "inside_hidden"):
            return "inside_hidden"
        if not self.include_hidden and not node.visible():
            return "hidden"
        kind = self.index.get(nid)["type"]
        if kind in MASKS:
            return "mask"
        if kind not in (PAINT, GROUP):
            return "unsupported_type"
        return None  # a parent omitted for another reason holds only masks

    def _entry(self, nid, node, kind):
        position = len(self.entries) + 1
        parent = self.index.parent(nid)
        entry = {
            "key": f"L{position:04d}",
            "id": nid,
            "name": node.name(),
            "path": self.index.path_of(nid),
            "type": "group" if kind == GROUP else "paint",
            "parent": refs.ROOT if parent == refs.ROOT else "$" + self.keys[parent],
            "visible": bool(node.visible()),
            "opacity": layerprops.from_krita_opacity(node.opacity()),
            "blend_mode": node.blendingMode(),
        }
        return position, entry

    def _pixels(self, position, name, read, node, entry):
        path = os.path.join(self.dir, manifest.png_name(position, name))
        out = _write_pixels(read, _bounds(node), self.crop, self.canvas, path, self.level,
                            self.written)
        if out is None:
            entry["png"] = None
            return
        rect, off_canvas, nbytes = out
        entry["png"] = os.path.basename(path)
        entry.update(rect)
        self.count += 1
        self.nbytes += nbytes
        if off_canvas and not self.crop:
            self.off_canvas.append(entry["path"])

    def node(self, nid):
        node = self.by_id[nid]
        reason = self._reason(nid, node)
        if reason is not None:
            self._skip(nid, reason)
            owner = self.index.parent(nid)
            if reason == "mask" and node.visible() and owner in self.keys:
                path = self.index.path_of(owner)
                if path not in self.masked:
                    self.masked.append(path)
            elif reason == "unsupported_type":
                self.unsupported.append(self.index.path_of(nid))
            return
        kind = self.index.get(nid)["type"]
        position, entry = self._entry(nid, node, kind)
        self.keys[nid] = entry["key"]
        problem = _recreatable(node)
        if problem is not None:
            self.not_recreatable.append({"path": entry["path"], "problem": problem})
        if kind == GROUP:
            entry.update(inherit_alpha=bool(node.inheritAlpha()), locked=bool(node.locked()),
                         pass_through=bool(node.passThroughMode()))
        else:
            entry.update(inherit_alpha=bool(node.inheritAlpha()),
                         alpha_locked=bool(node.alphaLocked()), locked=bool(node.locked()))
            self._pixels(position, node.name(), node.pixelData, node, entry)
        entry["stack_index"] = position - 1
        self.entries.append(entry)

    def projection(self, nid):
        """A projection entry for group ``nid``; its PNG is written later,
        by ``write_projections``."""
        node = self.by_id[nid]
        position, entry = self._entry(nid, node, PAINT)
        entry["projection"] = True
        entry["stack_index"] = position - 1
        self.entries.append(entry)
        self.pending.append((position, nid, entry))

    def run(self, chosen, structural):
        for kind, nid in _create_order(self.index):
            if chosen is not None and nid not in chosen and nid not in structural:
                continue
            if kind == "node":
                self.node(nid)
            elif (self.include_groups and nid in self.keys
                  and (chosen is None or nid in chosen)):
                self.projection(nid)
        self.write_projections()

    def _as_is(self, nid):
        """Krita keeps an up-to-date projection of group ``nid``: it is
        isolated (not pass-through) and it and its ancestors are visible.
        A pass-through group has no projection of its own, and a hidden
        group (or one inside a hidden group) an empty or stale one."""
        if self.by_id[nid].passThroughMode():
            return False
        return all(self.by_id[n].visible() for n in [nid] + self.index.ancestors(nid))

    def write_projections(self):
        """Write the pending projection PNGs. A group Krita keeps a
        projection of is read as is. The others are read from a ``clone()``
        of the document (D10; the user's document is never changed) in
        which they are made isolated and visible (their ancestors visible
        too), one clone per nesting depth: groups at one depth are disjoint
        subtrees, so changing them together changes none of their
        projections but their own."""
        by_depth = {}
        for item in self.pending:
            nid = item[1]
            if self._as_is(nid):
                self._projection_pixels(item, self.by_id[nid])
            else:
                by_depth.setdefault(len(self.index.ancestors(nid)), []).append(item)
        for depth in sorted(by_depth):
            self._from_clone(by_depth[depth])

    def _projection_pixels(self, item, node):
        position, _, entry = item
        self._pixels(position, entry["name"], node.projectionPixelData, node, entry)

    def _from_clone(self, items):
        clone = self.doc.clone()
        if clone is None:
            raise CommandError("krita_error", "Krita could not copy the document to compute "
                               "group projections")
        try:
            _isolate(clone, self.index, [nid for _, nid, _ in items])
            clone.refreshProjection()
            clone.waitForDone()
            for item in items:
                self._projection_pixels(item, _clone_node(clone, item[1]))
        finally:
            try:
                clone.setModified(False)  # close() never prompts then
            finally:
                clone.close()
        del clone  # crash hazard: never touch a closed document's wrapper

    def warnings(self):
        out = []
        if self.masked:
            out.append({
                "code": "masks_not_applied",
                "message": f"{len(self.masked)} exported layer(s) have visible masks, which "
                           "are not exported: the PNGs hold the layers' own pixels without the "
                           "masks applied, so a rebuild will not render the same",
                "paths": self.masked})
        if self.unsupported:
            out.append({
                "code": "not_exported",
                "message": f"{len(self.unsupported)} layer(s) of other types (filter, fill, "
                           "clone, vector or file layers) are not exported; a rebuild from "
                           "this manifest will not render the same",
                "paths": self.unsupported})
        if self.off_canvas:
            out.append({
                "code": "off_canvas",
                "message": f"{len(self.off_canvas)} layer(s) extend past the canvas; with "
                           "crop=false their pixels outside the canvas are not in the PNGs",
                "paths": self.off_canvas})
        if self.not_recreatable:
            out.append({
                "code": "not_recreatable",
                "message": f"{len(self.not_recreatable)} entr(ies) would be refused by "
                           "create_layers(manifest=...) as they are (fix the layers or edit "
                           "the manifest): " + "; ".join(f"{p['path']}: {p['problem']}"
                                                         for p in self.not_recreatable),
                "layers": self.not_recreatable})
        creatable = sum(1 for e in self.entries if not e.get("projection"))
        if creatable > batch.MAX_ITEMS:
            out.append({
                "code": "too_many_layers",
                "message": f"The manifest has {creatable} layers; create_layers takes at most "
                           f"{batch.MAX_ITEMS} per call",
                "count": creatable, "max": batch.MAX_ITEMS})
        return out


def _clone_node(clone, nid):
    node = nodes.node_by_id(clone, nid)
    if node is None:
        raise CommandError("krita_error", f"Layer {nid} is missing from the document's copy")
    return node


def _isolate(clone, index, gids):
    """In ``clone``: groups ``gids`` isolated and visible, their ancestors
    visible. Its own frame, so no node wrapper outlives the clone."""
    for gid in gids:
        group = _clone_node(clone, gid)
        group.setPassThroughMode(False)
        group.setVisible(True)
        for a in index.ancestors(gid):
            _clone_node(clone, a).setVisible(True)


def _document_info(doc):
    return {"name": doc_name(doc), "width": doc.width(), "height": doc.height(),
            "resolution": doc.resolution(), "color_model": doc.colorModel(),
            "color_depth": doc.colorDepth(), "color_profile": doc.colorProfile()}


@command("export_layers", timeout=LONG_TIMEOUT)
def cmd_export_layers(params):
    """Write one PNG per paint layer (and, with ``include_groups``, per
    group projection) plus ``manifest.json`` into ``dir``; the manifest
    feeds ``create_layers(manifest=...)`` (D13). Reads only: the document
    is not changed."""
    handle, doc = registry.resolve_doc(params)
    opts = (_bool(params, "crop", True), _bool(params, "include_hidden", True),
            _bool(params, "include_groups"), _png_level(params))
    overwrite = _bool(params, "overwrite")
    given = params.get("dir")
    if not isinstance(given, str) or given == "":
        raise CommandError("bad_request", "'dir' is required (the output directory)",
                           {"param": "dir"})
    pixels.require_rgba8(doc)
    index, by_id = nodes.tree(doc)
    selected = _ids_param(params, index)
    chosen, structural = (None, None) if selected is None else _selection(index, selected)
    out_dir = paths.resolve_output_dir(given, overwrite=overwrite)

    exporter = _Exporter(doc, index, by_id, out_dir, opts)
    manifest_path = os.path.join(out_dir, MANIFEST_NAME)
    try:
        exporter.run(chosen, structural)
        manifest.write(manifest_path, manifest.build(_document_info(doc), exporter.entries))
    except BaseException:
        for path in exporter.written:  # PNGs this call created; replaced ones can't come back
            try:
                os.remove(path)
            except OSError:
                pass
        raise
    total = exporter.nbytes + os.path.getsize(manifest_path)
    return {"doc": handle, "dir": out_dir, "manifest": manifest_path, "count": exporter.count,
            "layers": exporter.entries, "skipped": exporter.skipped,
            "warnings": exporter.warnings(), "bytes": total}
