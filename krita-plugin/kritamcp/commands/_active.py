"""Helpers to find the active document, view and layer."""

from krita import Krita


def get_active_document():
    """Get active document or return None."""
    app = Krita.instance()
    return app.activeDocument()


def get_active_view():
    """Get active view or return None."""
    app = Krita.instance()
    window = app.activeWindow()
    if window:
        return window.activeView()
    return None


def get_active_layer():
    """Get active paint layer."""
    doc = get_active_document()
    if doc:
        return doc.activeNode()
    return None
