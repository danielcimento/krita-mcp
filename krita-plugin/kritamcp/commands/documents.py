"""Document commands: new canvas, open, save/export."""

import os

from krita import InfoObject, Krita
from PyQt5.QtGui import QColor

from ..config import CANVAS_OUTPUT_DIR, MAX_CANVAS_DIM, MAX_CANVAS_PIXELS
from ..errors import CommandError
from ..params import int_param
from ..paths import resolve_allowed_path
from ._active import get_active_document


def cmd_new_canvas(params):
    """Create a new canvas."""
    width = int_param(params, "width", 800, 1, MAX_CANVAS_DIM)
    height = int_param(params, "height", 600, 1, MAX_CANVAS_DIM)
    if width * height > MAX_CANVAS_PIXELS:
        raise CommandError(f"Canvas too large (max {MAX_CANVAS_PIXELS} pixels)")
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
    pixel_data = bytes([b, g, r, 255] * (width * height))
    layer.setPixelData(pixel_data, 0, 0, width, height)

    doc.refreshProjection()

    return {"status": "ok", "width": width, "height": height, "name": name}


def cmd_get_canvas(params):
    """Export current canvas to file and return path."""
    filename = params.get("filename", "canvas.png")
    if not isinstance(filename, str) or not filename:
        return {"error": "Invalid filename"}

    doc = get_active_document()
    if not doc:
        return {"error": "No active document"}

    # Strip any directory components so the export stays in the output dir
    filename = os.path.basename(filename)
    if not filename.lower().endswith('.png'):
        filename += '.png'

    filepath = resolve_allowed_path(os.path.join(CANVAS_OUTPUT_DIR, filename))

    # Export image (batch mode suppresses export dialog)
    doc.setBatchmode(True)
    doc.exportImage(filepath, InfoObject())
    doc.setBatchmode(False)

    return {"status": "ok", "path": filepath}


def cmd_save(params):
    """Save to specific path."""
    filepath = resolve_allowed_path(params.get("path"))

    doc = get_active_document()
    if not doc:
        return {"error": "No active document"}

    # Batch mode suppresses export dialog
    doc.setBatchmode(True)
    doc.exportImage(filepath, InfoObject())
    doc.setBatchmode(False)

    return {"status": "ok", "path": filepath}


def cmd_open_file(params):
    """Open an existing file in Krita."""
    filepath = resolve_allowed_path(params.get("path"), must_exist=True)

    app = Krita.instance()

    # Open the document
    doc = app.openDocument(filepath)
    if not doc:
        return {"error": f"Failed to open: {filepath}"}

    # Add view to active window
    window = app.activeWindow()
    if window:
        window.addView(doc)

    return {"status": "ok", "path": filepath, "name": doc.name(), "width": doc.width(), "height": doc.height()}
