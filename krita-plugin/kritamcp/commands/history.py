"""History commands: undo, redo (Krita's UI undo stack; D6 changes this)."""

from krita import Krita

from ..dispatch import command
from ..errors import CommandError


def _trigger(action_name, what):
    action = Krita.instance().action(action_name)
    if not action:
        raise CommandError("krita_error", f"Could not trigger {what}")
    action.trigger()
    return {}


@command("undo", mutating=True)
def cmd_undo(params):
    """Undo last action."""
    return _trigger("edit_undo", "undo")


@command("redo", mutating=True)
def cmd_redo(params):
    """Redo last undone action."""
    return _trigger("edit_redo", "redo")
