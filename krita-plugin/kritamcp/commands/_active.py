"""The active view (UI state). Documents come from ``registry.resolve_doc``
and layers from ``nodes``."""

from krita import Krita


def get_active_view():
    """The active window's active view, or None (always None headless)."""
    window = Krita.instance().activeWindow()
    if window:
        return window.activeView()
    return None
