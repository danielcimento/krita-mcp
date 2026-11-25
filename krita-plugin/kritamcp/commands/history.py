"""History commands: undo, redo."""

from krita import Krita


def cmd_undo(params):
    """Undo last action."""
    app = Krita.instance()
    action = app.action('edit_undo')
    if action:
        action.trigger()
        return {"status": "ok"}
    return {"error": "Could not trigger undo"}


def cmd_redo(params):
    """Redo last undone action."""
    app = Krita.instance()
    action = app.action('edit_redo')
    if action:
        action.trigger()
        return {"status": "ok"}
    return {"error": "Could not trigger redo"}
