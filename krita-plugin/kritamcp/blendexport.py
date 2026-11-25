"""Additive blend conversion on export (D17). Main thread only.

Krita's ``add`` and ``linear_dodge`` render identically, but ``.psd``
export writes ``add`` as Normal (the effect is lost) and ``.ora`` export
writes ``linear_dodge`` as the Krita-only ``krita:linear_dodge``. So an
export to those formats writes each such layer in the other spelling:

- ``psd``: ``add`` → ``linear_dodge`` (written as ``lddg``; groups too).
- ``ora``: ``linear_dodge`` → ``add`` (written as ``svg:plus``).

The change is made on a ``clone()`` of the document (D10: copy-on-write,
~10 ms for Mao, keeps node UUIDs), never on the user's document. The
clone is not in ``Krita.instance().documents()`` (verified headless; from
libkis' source it isn't registered with KisPart or given a view in the
GUI either), so the registry can't hand out a handle for it, and it is
closed before the command returns. After ``close()`` it is never touched
(crash hazard, krita-api-notes § Crash hazards).

With nothing to convert, the original document is exported as is and no
clone is made. ``.kra`` and every other format are never converted.
"""

from . import fileio, nodes
from .errors import CommandError

# format -> {mode in the document: mode written to the file}
CONVERSIONS = {
    "psd": {"add": "linear_dodge"},
    "ora": {"linear_dodge": "add"},
}


def planned(doc, fmt):
    """``[{id, path, from, to}]``: the nodes of ``doc`` (a live wrapper)
    whose blend mode an export to ``fmt`` converts, in the Layers docker's
    order. Every node counts, groups and masks included; the root-level
    global selection mask is skipped (``nodes.walk``). Empty when ``fmt``
    has no table."""
    table = CONVERSIONS.get(fmt)
    if not table:
        return []
    index, by_id = nodes.tree(doc)
    out = []
    for nid, node in by_id.items():
        mode = node.blendingMode()
        if mode in table:
            out.append({"id": nid, "path": index.path_of(nid), "from": mode, "to": table[mode]})
    return out


def export(doc, path, fmt, conversions=None):
    """Export ``doc`` to ``path`` (``fileio.export_image``, batch mode),
    with the additive spelling ``fmt`` needs. Returns the conversions made
    (``planned(doc, fmt)``, or ``conversions`` when the caller already has
    it), ``[]`` when the original was exported unchanged. The original's
    tree, ``fileName()`` and ``modified`` are untouched. Failure:
    ``krita_error``."""
    if conversions is None:
        conversions = planned(doc, fmt)
    if not conversions:
        fileio.export_image(doc, path)
        return []
    clone = doc.clone()
    if clone is None:
        raise CommandError("krita_error", "Krita could not copy the document for the export")
    try:
        _convert(clone, conversions)
        clone.refreshProjection()
        clone.waitForDone()
        fileio.export_image(clone, path)
    finally:
        try:
            clone.setModified(False)  # close() never prompts then
        finally:
            clone.close()
    del clone  # crash hazard: never touch a closed document's wrapper
    return conversions


def _convert(clone, conversions):
    """Set the converted modes on the clone's nodes, found by UUID (clones
    keep them). Its own frame, so no node wrapper outlives the clone."""
    for c in conversions:
        node = nodes.node_by_id(clone, c["id"])
        if node is None:
            raise CommandError("krita_error",
                               f"Layer {c['path']} is missing from the document's copy")
        node.setBlendingMode(c["to"])
