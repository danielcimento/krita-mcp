"""History commands (D6): the agent journal's undo, redo and listing, and
Krita's own UI undo stack as ui_undo / ui_redo.

``undo`` / ``redo`` apply one entry of the document's journal
(``batch.Journal``): one agent tool call. They know nothing of edits made
by hand in the UI. ``ui_undo`` / ``ui_redo`` trigger Krita's Edit → Undo /
Redo on the document's view, which only partly covers agent edits
(krita-api-notes § Undo).
"""

from krita import Krita

from .. import batch, registry
from ..dispatch import LONG_TIMEOUT, command
from ..errors import CommandError

UI_WARNING = (
    "Krita's own undo only partly covers agent edits: property changes, pixel writes and moves "
    "made by agent tools are not on Krita's undo stack, so Edit > Undo may skip them or revert "
    "an unrelated earlier step. Use krita_undo to revert agent tool calls.")


def _step(params, verb):
    handle, doc = registry.resolve_doc(params)
    journal = batch.JOURNALS.get(handle)
    step = journal.undo if verb == "undo" else journal.redo
    source_len = len(journal) if verb == "undo" else journal.redo_count
    if not source_len:
        raise CommandError("conflict", f"nothing to {verb} in {handle}",
                           {"doc": handle, "undo": len(journal), "redo": journal.redo_count})
    try:
        entry, reverse, kept = step(doc, where=f" in {handle}")
    finally:
        doc.refreshProjection()
        doc.waitForDone()
    warnings = []
    if not kept:
        other = "redo" if verb == "undo" else "undo"
        warnings.append({
            "code": f"not_{other}able",
            "message": f"Reversing this {verb} needs about {reverse.nbytes} bytes, over the "
                       f"journal's memory and disk caps: it can't be {other}ne, and the "
                       f"{other} stack was cleared",
            "nbytes": reverse.nbytes})
    done = "undone" if verb == "undo" else "redone"
    return {"doc": handle, done: {"label": entry.label, "layers": entry.node_ids()},
            "remaining": {"undo": len(journal), "redo": journal.redo_count},
            "warnings": warnings}


@command("undo", timeout=LONG_TIMEOUT, mutating=True)
def cmd_undo(params):
    """Revert the newest agent tool call in the document's journal."""
    return _step(params, "undo")


@command("redo", timeout=LONG_TIMEOUT, mutating=True)
def cmd_redo(params):
    """Re-apply the newest undone agent tool call."""
    return _step(params, "redo")


@command("journal")
def cmd_journal(params):
    """The document's undo and redo stacks, the next entry to apply first."""
    handle, _ = registry.resolve_doc(params)
    return {"doc": handle, **batch.JOURNALS.get(handle).describe()}


# --- Krita's UI undo stack -------------------------------------------------

def _view_of(doc):
    """A view showing ``doc`` (a live wrapper), or None (always None
    headless)."""
    for view in Krita.instance().views():
        shown = view.document()
        if shown is not None and shown == doc:
            return view
    return None


def _ui_target(params):
    """``(handle, activated)`` once the target document's view is the
    active one: ``doc``'s (activated first when it isn't), else the active
    view's. No view: ``conflict``."""
    if params.get("doc") is None:
        live = registry.active_live()
        if live is None:
            raise CommandError("conflict", "No active document in a Krita window: Krita's own "
                               "undo needs a view (krita_undo works without one)")
        return registry.handle_for(live), False
    handle, doc = registry.resolve_doc(params)
    active = registry.active_live()
    if active is not None and active == doc:
        return handle, False
    view = _view_of(doc)
    if view is None:
        raise CommandError("conflict", f"{handle} is not shown in a Krita window: Krita's own "
                           "undo needs a view (krita_undo works without one)", {"doc": handle})
    window = view.window()
    if window is not None:
        window.showView(view)
    active = registry.active_live()
    if (active is None or not active == doc) and window is not None:
        window.activate()
        active = registry.active_live()
    if active is None or not active == doc:
        raise CommandError("conflict", f"Could not make {handle}'s view the active one",
                           {"doc": handle})
    return handle, True


def _trigger(params, action_name, what):
    handle, activated = _ui_target(params)
    action = Krita.instance().action(action_name)
    if not action:
        raise CommandError("krita_error", f"Could not trigger {what}")
    action.trigger()
    return {"doc": handle, "action": action_name, "activated_view": activated,
            "warnings": [{"code": "partial_ui_undo", "message": UI_WARNING}]}


@command("ui_undo", mutating=True)
def cmd_ui_undo(params):
    """Krita's Edit → Undo on the document's view."""
    return _trigger(params, "edit_undo", "undo")


@command("ui_redo", mutating=True)
def cmd_ui_redo(params):
    """Krita's Edit → Redo on the document's view."""
    return _trigger(params, "edit_redo", "redo")
