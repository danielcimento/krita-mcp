"""Document commands (api-design § Documents): list, info, new canvas, open,
save/export and close. (get_canvas, a render shortcut, is in ``render.py``.)

Every save, export and open goes through ``fileio`` (batch mode, D5).
Documents come from ``registry`` and are live wrappers valid for this
command only; after ``close()`` the wrapper is never touched again.
"""

import os

from krita import Krita
from PyQt5.QtGui import QColor

from .. import blendexport, config, fileio, nodes, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from ..params import canvas_size, float_param
from ..paths import ensure_parent_dir, resolve_allowed_path

# new_canvas defaults, kept from before F001 for compatibility.
DEFAULT_NAME = "New Canvas"
DEFAULT_BACKGROUND = "#1a1a2e"
DEFAULT_RESOLUTION = 120.0
RESOLUTION_RANGE = (1.0, 10000.0)  # dpi
MAX_NAME = 200

COLOR_MODEL, COLOR_DEPTH = "RGBA", "U8"  # the only model new_canvas makes (D5)

# Save formats: the allowed extensions without the dot, "jpeg" as "jpg".
_FORMAT_ALIASES = {"jpeg": "jpg"}
SAVE_FORMATS = sorted({_FORMAT_ALIASES.get(e[1:], e[1:]) for e in config.ALLOWED_IMAGE_EXTS})


# --- shared -------------------------------------------------------------

def doc_name(doc):
    """``doc.name()``, or the basename of ``fileName()`` when the title is
    empty (documents opened from ``.ora``, for one)."""
    name = doc.name()
    if name:
        return name
    path = doc.fileName()
    return os.path.basename(path) if path else ""


def doc_path(doc):
    """The document's file as a realpath, or None if never saved. Krita
    keeps ``fileName()`` as it was given (krita-api-notes § Documents)."""
    path = doc.fileName()
    return os.path.realpath(path) if path else None


def tree_stats(doc):
    """``(layer_counts, max_depth)`` from ``nodes.walk`` (the global
    selection mask is skipped). Top-level nodes are depth 1; an empty
    document has depth 0."""
    counts, depth, deepest = {}, {}, 0
    for _, entry in nodes.walk(doc):
        counts[entry["type"]] = counts.get(entry["type"], 0) + 1
        d = 1 if entry["parent"] is None else depth[entry["parent"]] + 1
        depth[entry["id"]] = d
        deepest = max(deepest, d)
    return dict(sorted(counts.items())), deepest


def _add_view(doc):
    """Show ``doc`` in the active window, if there is one (never headless)."""
    window = Krita.instance().activeWindow()
    if window:
        window.addView(doc)


def _format_of(ext_or_format):
    value = ext_or_format.lower().lstrip(".")
    return _FORMAT_ALIASES.get(value, value)


# --- queries ------------------------------------------------------------

@command("list_documents")
def cmd_list_documents(params):
    """Every open document, with a handle each (D3)."""
    active = registry.active_live()
    documents = [{
        "doc": handle,
        "name": doc_name(d),
        "path": doc_path(d),
        "width": d.width(),
        "height": d.height(),
        "modified": bool(d.modified()),
        "active": active is not None and active == d,
    } for handle, d in registry.list_open()]
    return {"documents": documents}


def _selection_rect(doc):
    sel = doc.selection()
    if sel is None:
        return None
    w, h = sel.width(), sel.height()
    if w <= 0 or h <= 0:
        return None
    return {"x": sel.x(), "y": sel.y(), "w": w, "h": h}


@command("get_document_info")
def cmd_get_document_info(params):
    """Size, color space, resolution, layer counts and selection of one
    document."""
    handle, doc = registry.resolve_doc(params)
    counts, max_depth = tree_stats(doc)
    return {
        "doc": handle,
        "name": doc_name(doc),
        "path": doc_path(doc),
        "width": doc.width(),
        "height": doc.height(),
        "resolution": doc.resolution(),
        "color_model": doc.colorModel(),
        "color_depth": doc.colorDepth(),
        "color_profile": doc.colorProfile(),
        "modified": bool(doc.modified()),
        "layer_counts": counts,
        "max_depth": max_depth,
        "selection": _selection_rect(doc),
    }


# --- new_canvas ---------------------------------------------------------

def _background_param(params):
    """``None`` for "transparent", else ``(r, g, b)``."""
    value = params.get("background", DEFAULT_BACKGROUND)
    if isinstance(value, str) and value.strip().lower() == "transparent":
        return None
    color = QColor(value) if isinstance(value, str) else QColor()
    if not color.isValid() or color.alpha() != 255:
        raise CommandError("bad_request",
                           f"'background' must be an opaque color such as '#1a1a2e', "
                           f"or 'transparent'; got {value!r}")
    return color.red(), color.green(), color.blue()


def _profile_param(params):
    value = params.get("color_profile", "")
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise CommandError("bad_request", "'color_profile' must be a profile name (a string)")
    if value:
        available = list(Krita.instance().profiles(COLOR_MODEL, COLOR_DEPTH))
        if value not in available:
            raise CommandError(
                "bad_request",
                f"Color profile {value!r} is not installed for {COLOR_MODEL}/{COLOR_DEPTH}; "
                f"see details.available",
                {"color_profile": value, "available": available})
    return value


def _name_param(params):
    value = params.get("name", DEFAULT_NAME)
    if not isinstance(value, str) or not value.strip():
        raise CommandError("bad_request", "'name' must be a non-empty string")
    return value[:MAX_NAME]


def _background_node(doc):
    """The ``Background`` paint layer ``createDocument`` makes, or None."""
    for node in doc.rootNode().childNodes():
        if node.type() == "paintlayer" and node.name() == "Background":
            return node
    return None


# new_canvas writes a full-canvas buffer, so it gets the long timeout too.
@command("new_canvas", timeout=LONG_TIMEOUT, mutating=True)
def cmd_new_canvas(params):
    """Create an RGBA/U8 document. Every parameter is checked before
    anything is created. The ``Background`` layer ``createDocument`` makes
    is reused: filled with the color (skipped for opaque white, which it
    already is), or, for "transparent", replaced by an empty paint layer of
    the same name. The new document is not marked modified."""
    # Limits are read per call, so a config file change applies at once (D8).
    width, height = canvas_size(params, config.current().limits)
    name = _name_param(params)
    rgb = _background_param(params)
    profile = _profile_param(params)
    resolution = float_param(params, "resolution", DEFAULT_RESOLUTION, *RESOLUTION_RANGE)

    app = Krita.instance()
    doc = app.createDocument(width, height, name, COLOR_MODEL, COLOR_DEPTH, profile, resolution)
    if doc is None:
        raise CommandError("krita_error", "Krita could not create the document")
    # The registry keeps the wrapper, and with it the document: headless
    # (no view) it would be destroyed when ``doc`` goes out of scope.
    handle = registry.handle_for(doc)

    try:
        background = _fill_background(doc, rgb, width, height)
        doc.refreshProjection()
        doc.waitForDone()
        doc.setModified(False)
    except Exception:
        doc.setModified(False)  # don't leave a half-made document open
        doc.close()
        del doc
        raise
    _add_view(doc)

    return {
        "doc": handle,
        "name": doc.name(),
        "width": doc.width(),
        "height": doc.height(),
        "color_profile": doc.colorProfile(),
        "resolution": doc.resolution(),
        "background": "transparent" if rgb is None else "#%02x%02x%02x" % rgb,
        "background_layer": nodes.node_id(background),
    }


def _fill_background(doc, rgb, width, height):
    """Make the ``Background`` layer match ``rgb`` (None: transparent) and
    return it."""
    root = doc.rootNode()
    background = _background_node(doc)
    if rgb is None:
        fresh = doc.createNode("Background", "paintlayer")
        root.addChildNode(fresh, background)  # above the old one, then drop it
        if background is not None:
            background.remove()
        background = fresh
    elif background is None:  # not expected (spike Q4); make one
        background = doc.createNode("Background", "paintlayer")
        root.addChildNode(background, None)
        background.setPixelData(bytes((rgb[2], rgb[1], rgb[0], 255)) * (width * height),
                                0, 0, width, height)
    elif rgb != (255, 255, 255):
        background.setPixelData(bytes((rgb[2], rgb[1], rgb[0], 255)) * (width * height),
                                0, 0, width, height)
    return background


# --- open / save / close -----------------------------------------------

@command("open_file", timeout=LONG_TIMEOUT, mutating=True)
def cmd_open_file(params):
    """Open a file inside the allowed roots. If an open document already
    has that file, its handle comes back with ``already_open: true``."""
    filepath = resolve_allowed_path(params.get("path"), must_exist=True)

    for handle, d in registry.list_open():
        if doc_path(d) == filepath:
            return _opened(handle, d, filepath, already_open=True)

    doc = fileio.open_document(filepath)
    handle = registry.handle_for(doc)
    _add_view(doc)
    return _opened(handle, doc, filepath, already_open=False)


def _opened(handle, doc, filepath, already_open):
    counts, _ = tree_stats(doc)
    return {"doc": handle, "path": filepath, "name": doc_name(doc),
            "width": doc.width(), "height": doc.height(),
            "layer_counts": counts, "already_open": already_open}


def _format_param(params):
    value = params.get("format")
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise CommandError("bad_request", "'format' must be a file format such as 'png'")
    fmt = _format_of(value)
    if fmt not in SAVE_FORMATS:
        raise CommandError("bad_request",
                           f"Unknown format {value!r}; one of {', '.join(SAVE_FORMATS)}",
                           {"format": value, "available": SAVE_FORMATS})
    return fmt


@command("save", timeout=LONG_TIMEOUT)
def cmd_save(params):
    """Save in place (no ``path``), as ``.kra`` (``saveAs``: the document's
    path moves there), or export to another format (``exportImage``: the
    document's path is unchanged). Missing parent directories inside the
    allowed root are created.

    ``.psd`` and ``.ora`` get the additive blend conversion (D17,
    ``blendexport``): exports, and in-place saves of a document whose own
    file is one of those, which then export the converted copy over that
    file instead of ``Document.save()`` and clear ``modified``. The result's
    ``blend_conversions`` lists the converted layers."""
    fmt = _format_param(params)
    path = params.get("path")
    if path is not None:  # checked before the document is looked up
        target = resolve_allowed_path(path)
    handle, doc = registry.resolve_doc(params)

    if path is None:
        current = doc.fileName()
        if not current:
            raise CommandError("conflict",
                               f"{handle} has never been saved; pass 'path' to save it",
                               {"doc": handle})
        # The document's own file is written, so it must be allowed too.
        target = resolve_allowed_path(current)
        ext = _format_of(os.path.splitext(target)[1])
        if fmt is not None and fmt != ext:
            raise CommandError("bad_request",
                               f"'format' {fmt!r} doesn't match the document's file {target}; "
                               "pass 'path' to save in another format",
                               {"format": fmt, "extension": ext})
        conversions = blendexport.planned(doc, ext)
        if conversions:
            # Document.save() would write the user's spelling (D17), so the
            # converted clone is exported over the document's own file.
            blendexport.export(doc, target, ext, conversions)
            doc.setModified(False)  # the content is saved
        else:
            fileio.save_in_place(doc)
        return _saved(handle, target, ext, changed=False, conversions=conversions)

    ext = _format_of(os.path.splitext(target)[1])
    if fmt is not None and fmt != ext:
        raise CommandError("bad_request",
                           f"'format' {fmt!r} doesn't match the extension of {target}",
                           {"format": fmt, "extension": ext})
    target = ensure_parent_dir(target)
    before = doc_path(doc)
    conversions = []
    if ext == "kra":
        fileio.save_as(doc, target)
    else:
        conversions = blendexport.export(doc, target, ext)
    return _saved(handle, target, ext, changed=doc_path(doc) != before,
                  conversions=conversions)


def _saved(handle, target, fmt, changed, conversions):
    try:
        size = os.path.getsize(target)
    except OSError as e:
        raise CommandError("krita_error",
                           f"Krita reported success but {target} can't be read: "
                           f"{e.strerror or e}") from e
    return {"doc": handle, "path": target, "format": fmt, "bytes": size,
            "doc_path_changed": changed, "blend_conversions": conversions}


@command("close_document", mutating=True)
def cmd_close_document(params):
    """Close one document. ``doc`` is required: defaulting to the active
    document could close the wrong one. Modified and not ``discard``:
    ``conflict``, with nothing touched."""
    if params.get("doc") is None:
        raise CommandError("bad_request",
                           "'doc' is required (a handle like 'doc-1'): close_document "
                           "never defaults to the active document")
    discard = params.get("discard", False)
    if not isinstance(discard, bool):
        raise CommandError("bad_request", "'discard' must be true or false")
    handle, doc = registry.resolve_doc(params)
    if doc.modified() and not discard:
        raise CommandError("conflict",
                           f"{handle} has unsaved changes; save it first, or pass discard=true",
                           {"doc": handle, "name": doc_name(doc)})
    doc.setModified(False)  # close() never prompts then (verified)
    ok = doc.close()
    del doc  # crash hazard: never touch a closed document's wrapper
    if not ok:
        raise CommandError("krita_error", f"Krita did not close {handle}", {"doc": handle})
    # The registry drops the handle at its next lookup (it is no longer
    # in documents()); later calls with it are not_found.
    return {"doc": handle, "closed": True}
