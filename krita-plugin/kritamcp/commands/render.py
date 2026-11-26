"""render (api-design § krita_render) and its get_canvas shortcut.

A render reads the document's projection (Krita's composited image) over
a region, optionally scales it and puts a background colour under it, and
writes a PNG. The user's document is never changed:

- With no ``only`` / ``hide`` (or when they change nothing), the
  projection is read directly.
- Otherwise the visibility changes are made in a ``clone()`` (D10 B:
  copy-on-write, ~10 ms for Mao, keeps node UUIDs), whose projection is
  refreshed and read, and the clone is closed in ``finally``. It is never
  touched after ``close()`` (crash hazard, krita-api-notes § Crash
  hazards), and its node wrappers live in a helper's frame only.

``Document.projection`` is Krita's ``convertToQImage`` with the default
profile: a ``Format_ARGB32`` (non-premultiplied) image in 8-bit sRGB. For
an RGBA/U8 document in an sRGB profile it is the composited bytes exactly
(``Document.pixelData``, colour under zero alpha included); other depths,
colour models and profiles are converted to 8-bit sRGB by Krita, so they
render too (verified 2026-11-25: U16, F16, F32, GRAYA, CMYKA, ClayRGB).
"""

import os

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QImage

from .. import config, nodes, paths, pixels, refs, registry, renderplan
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from .pixels import _png_level

PNG_EXT = ".png"


def _png_path(given, tool="render"):
    """The resolved output path: inside an allowed root, ending in ``.png``."""
    if not isinstance(given, str) or given == "":
        raise CommandError("bad_request", "'path' is required (the PNG file to write)",
                           {"param": "path"})
    real = paths.resolve_allowed_path(given)
    if os.path.splitext(real)[1].lower() != PNG_EXT:
        raise CommandError("bad_request",
                           f"{tool} writes PNG files only; 'path' must end in .png, got {given!r}",
                           {"param": "path"})
    return real


def _background(params):
    """``None`` (transparent) or ``(bgra bytes, "#rrggbb" / "#aarrggbb")``."""
    value = params.get("background")
    if value is None or (isinstance(value, str) and value.strip().lower() == "transparent"):
        return None
    color = QColor(value) if isinstance(value, str) else QColor()
    if not color.isValid():
        raise CommandError("bad_request",
                           f"'background' must be a colour such as '#ffffff' ('#rgb', "
                           f"'#rrggbb', '#aarrggbb' or an SVG colour name), or null for "
                           f"transparent; got {value!r}", {"param": "background"})
    bgra = bytes((color.blue(), color.green(), color.red(), color.alpha()))
    name = color.name(QColor.HexRgb if color.alpha() == 255 else QColor.HexArgb)
    return bgra, name


def _refs(params, key, index):
    """The resolved ids of the ``key`` list (``only`` / ``hide``), in the
    order given without repeats; None when absent."""
    value = params.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not value:
        raise CommandError("bad_request",
                           f"'{key}' must be a non-empty list of layer refs, or null",
                           {"param": key})
    out = []
    for nid in refs.resolve_many(index, value, field=key):
        if nid not in out:
            out.append(nid)
    return out


class Render:
    """One validated render: what to read, how to scale it, where to write."""

    __slots__ = ("path", "rect", "size", "scale", "only", "hide", "changes", "background",
                 "level")

    def __init__(self, path, rect, size, scale, only=None, hide=None, changes=None,
                 background=None, level=None):
        self.path, self.rect, self.size, self.scale = path, rect, size, scale
        self.only, self.hide, self.changes = only, hide, changes or {}
        self.background, self.level = background, level


def plan(doc, path, params):
    """Validate ``params`` against ``doc`` (a live wrapper); nothing is
    read or written yet. ``path``: already resolved by ``_png_path``."""
    rect = renderplan.clip_region(params.get("region"), doc.width(), doc.height())
    scale = renderplan.scale_param(params)
    size = renderplan.output_size(rect[2], rect[3], scale)
    background = _background(params)
    level = _png_level(params)
    only = hide = None
    changes = {}
    if params.get("only") is not None or params.get("hide") is not None:
        index, by_id = nodes.tree(doc)
        only, hide = _refs(params, "only", index), _refs(params, "hide", index)
        visible = {nid: bool(node.visible()) for nid, node in by_id.items()}
        changes = renderplan.visibility_changes(index, visible, only, hide)
    return Render(path, rect, size, scale, only, hide, changes, background, level)


def _set_visibility(clone, changes):
    """Apply ``changes`` (``{id: bool}``) to the clone's nodes, found by
    UUID (clones keep them). Its own frame, so no node wrapper outlives the
    clone."""
    for nid, visible in changes.items():
        node = nodes.node_by_id(clone, nid)
        if node is None:
            raise CommandError("krita_error", f"Layer {nid} is missing from the document's copy")
        node.setVisible(visible)


def _clone_projection(doc, changes, rect):
    """The projection over ``rect`` of a clone of ``doc`` with ``changes``
    applied (D10 B). The QImage is Krita's own copy, valid after the clone
    is closed."""
    clone = doc.clone()
    if clone is None:
        raise CommandError("krita_error", "Krita could not copy the document for the render")
    try:
        _set_visibility(clone, changes)
        clone.refreshProjection()
        clone.waitForDone()
        image = clone.projection(*rect)
    finally:
        try:
            clone.setModified(False)  # close() never prompts then
        finally:
            clone.close()
    del clone  # crash hazard: never touch a closed document's wrapper
    return image


def _scaled(image, w, h):
    """``image`` resampled to ``w`` x ``h`` with ``Qt.SmoothTransformation``,
    as ``Format_ARGB32``.

    Qt smooth-scales ``Format_ARGB32`` by converting it to 8-bit
    premultiplied first, which loses colour as alpha drops: on a test image
    halved, colours were off from the exact average by up to 7.4 levels at
    alpha >= 32 and 171 at alpha 1-31. Scaling a ``Format_RGBA64_Premultiplied``
    copy instead keeps them within 0.5 and 1.3 levels (alpha within 0.5),
    for about 5-10 ms more at Mao size (tests/krita/test_render.py).
    Premultiplied scaling never bleeds the colour of alpha-0 pixels into
    visible ones; the output has (0, 0, 0, 0) wherever alpha is 0.
    """
    work = image.convertToFormat(QImage.Format_RGBA64_Premultiplied)
    work = work.scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    return work.convertToFormat(QImage.Format_ARGB32)


def execute(doc, job):
    """Render ``job`` (from ``plan``) from ``doc`` and write the PNG.
    Returns ``(path, bytes)``: the path again after its missing parent
    directories were created inside the allowed root, and the file size."""
    path = paths.ensure_parent_dir(job.path)
    x, y, w, h = job.rect
    if job.changes:
        image = _clone_projection(doc, job.changes, job.rect)
    else:
        doc.waitForDone()  # pending updates; a plain read is otherwise current
        image = doc.projection(x, y, w, h)
    if image is None or image.isNull() or (image.width(), image.height()) != (w, h):
        raise CommandError("krita_error", f"Krita returned no image for the {w}x{h} region")
    ow, oh = job.size
    if (ow, oh) != (w, h):
        image = _scaled(image, ow, oh)
    if job.background is not None:
        data = pixels.from_qimage(image)[0]
        under = job.background[0] * (ow * oh)
        data = pixels.composite_over(under, data, ow, oh)
        del image, under
        return path, pixels.save_png(data, path, ow, oh, level=job.level)
    return path, pixels.save_png(image, path, level=job.level)


def result(handle, path, job, nbytes):
    x, y, w, h = job.rect
    out = {"doc": handle, "path": path, "width": job.size[0], "height": job.size[1],
           "region": {"x": x, "y": y, "w": w, "h": h}, "scale": job.scale, "bytes": nbytes}
    if job.only is not None:
        out["only"] = job.only
    if job.hide is not None:
        out["hide"] = job.hide
    if job.background is not None:
        out["background"] = job.background[1]
    if job.level is not None:
        out["png_level"] = job.level
    return out


@command("render", timeout=LONG_TIMEOUT)
def cmd_render(params):
    """Write the document's composited image (or a region of it, scaled,
    with only some layers shown, over a background) to a PNG. Reads only:
    the document is not changed."""
    path = _png_path(params.get("path"))
    handle, doc = registry.resolve_doc(params)
    job = plan(doc, path, params)
    path, nbytes = execute(doc, job)
    return result(handle, path, job, nbytes)


@command("get_canvas", timeout=LONG_TIMEOUT)
def cmd_get_canvas(params):
    """A full-canvas ``render`` into the output dir: ``filename``'s base
    name, with ``.png`` appended unless it has it."""
    filename = params.get("filename", "canvas.png")
    if not isinstance(filename, str) or not filename:
        raise CommandError("bad_request", "Invalid filename")
    handle, doc = registry.resolve_doc(params)
    filename = os.path.basename(filename)  # stays in the output dir
    if not filename.lower().endswith(PNG_EXT):
        filename += PNG_EXT
    path = paths.resolve_allowed_path(os.path.join(config.current().output_dir, filename))
    job = plan(doc, path, {})
    path, nbytes = execute(doc, job)
    return result(handle, path, job, nbytes)
