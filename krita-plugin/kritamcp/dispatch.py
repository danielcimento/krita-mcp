"""Command dispatch: action name -> handler. Called on Krita's main thread.

(P1.3 replaces the if/elif chain with a registry.)
"""

from .commands import documents, history, paint


def execute(command):
    """Execute a paint command and return result."""
    try:
        action = command.get("action")
        params = command.get("params", {})

        if action == "new_canvas":
            return documents.cmd_new_canvas(params)
        elif action == "set_color":
            return paint.cmd_set_color(params)
        elif action == "set_brush":
            return paint.cmd_set_brush(params)
        elif action == "stroke":
            return paint.cmd_stroke(params)
        elif action == "fill":
            return paint.cmd_fill(params)
        elif action == "draw_shape":
            return paint.cmd_draw_shape(params)
        elif action == "get_canvas":
            return documents.cmd_get_canvas(params)
        elif action == "undo":
            return history.cmd_undo(params)
        elif action == "redo":
            return history.cmd_redo(params)
        elif action == "clear":
            return paint.cmd_clear(params)
        elif action == "save":
            return documents.cmd_save(params)
        elif action == "get_color_at":
            return paint.cmd_get_color_at(params)
        elif action == "list_brushes":
            return paint.cmd_list_brushes(params)
        elif action == "open_file":
            return documents.cmd_open_file(params)
        else:
            return {"error": f"Unknown action: {action}"}

    except Exception as e:
        return {"error": str(e)}
