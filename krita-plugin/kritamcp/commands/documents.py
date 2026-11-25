"""Document commands: new canvas, open, save/export (all long-timeout, D9)."""

import os

from krita import InfoObject, Krita
from PyQt5.QtGui import QColor

from .. import config
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError
from ..params import canvas_size
from ..paths import resolve_allowed_path
from ._active import get_active_document


def _export(doc, filepath):
    """exportImage with batch mode on (no dialogs), always reset afterwards."""
    doc.setBatchmode(True)
    try:
        ok = doc.exportImage(filepath, InfoObject())
    finally:
        doc.setBatchmode(False)
    if not ok:
        raise CommandError("krita_error", f"Krita failed to export to {filepath}")


def _active_document():
    doc = get_active_document()
    if not doc:
        raise CommandError("not_found", "No active document")
    return doc


# new_canvas writes a full-canvas buffer, so it gets the long timeout too.
@command("new_canvas", timeout=LONG_TIMEOUT, mutating=True)
def cmd_new_canvas(params):
    """Create a new canvas."""
    # Limits are read per call, so a config file change applies at once (D8).
    width, height = canvas_size(params, config.current().limits)
    name = str(params.get("name", "New Canvas"))[:200]
    bg_color = str(params.get("background", "#1a1a2e"))

    app = Krita.instance()

    # Create document with background color
    doc = app.createDocument(width, height, name, "RGBA", "U8", "", 120.0)

    window = app.activeWindow()
    if window:
        window.addView(doc)

    # Create a paint layer
    root = doc.rootNode()
    layer = doc.createNode("paint", "paintlayer")
    root.addChildNode(layer, None)

    # Fill background using pixel data
    color = QColor(bg_color)
    r, g, b = color.red(), color.green(), color.blue()

    # Create pixel data for entire canvas (BGRA format)
    pixel_data = bytes((b, g, r, 255)) * (width * height)
    layer.setPixelData(pixel_data, 0, 0, width, height)

    doc.refreshProjection()

    return {"width": width, "height": height, "name": name}


@command("get_canvas", timeout=LONG_TIMEOUT)
def cmd_get_canvas(params):
    """Export current canvas to file and return path."""
    filename = params.get("filename", "canvas.png")
    if not isinstance(filename, str) or not filename:
        raise CommandError("bad_request", "Invalid filename")

    doc = _active_document()

    # Strip any directory components so the export stays in the output dir
    filename = os.path.basename(filename)
    if not filename.lower().endswith('.png'):
        filename += '.png'

    filepath = resolve_allowed_path(os.path.join(config.current().output_dir, filename))

    _export(doc, filepath)

    return {"path": filepath}


@command("save", timeout=LONG_TIMEOUT)
def cmd_save(params):
    """Save to specific path."""
    filepath = resolve_allowed_path(params.get("path"))

    doc = _active_document()

    _export(doc, filepath)

    return {"path": filepath}


@command("open_file", timeout=LONG_TIMEOUT, mutating=True)
def cmd_open_file(params):
    """Open an existing file in Krita."""
    filepath = resolve_allowed_path(params.get("path"), must_exist=True)

    app = Krita.instance()

    # Open the document
    doc = app.openDocument(filepath)
    if not doc:
        raise CommandError("krita_error", f"Failed to open: {filepath}")

    # Add view to active window
    window = app.activeWindow()
    if window:
        window.addView(doc)

    return {"path": filepath, "name": doc.name(), "width": doc.width(), "height": doc.height()}
