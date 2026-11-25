"""Document handles (D3). Main thread only.

Handles are ``doc-<n>``, ``n`` counting up from 1 and never reused in a
process. The registry maps each handle to a stored ``Document`` wrapper.

**Crash hazard.** Calling any method on the wrapper of a closed document
crashes Krita, and ``try`` can't catch it. A stored wrapper is therefore
only ever used in ``==`` against the wrappers ``Krita.instance().documents()``
returns (``==`` is safe on a closed wrapper: spike Q17). Every method call
goes to the live wrapper from ``documents()`` that matched. Callers get
live wrappers only, and must not keep them across commands.

Holding the wrapper also keeps a document alive: headless, a document from
``createDocument`` with no view is destroyed when its last Python reference
goes (krita-api-notes § Documents). So a headless ``new_canvas`` leaves a
usable document, and whoever made it must close it.
"""

import os
import re

from krita import Krita

from .errors import CommandError

_HANDLE_RE = re.compile(r"doc-[1-9][0-9]*")

_entries = {}  # handle -> stored Document wrapper, in issue order
_next = 1


def _live():
    return list(Krita.instance().documents())


def _prune(live):
    for handle, stored in list(_entries.items()):
        if not any(stored == d for d in live):
            del _entries[handle]


def _find(doc):
    for handle, stored in _entries.items():
        if stored == doc:
            return handle
    return None


def handle_for(doc):
    """The handle of the open document ``doc`` (a live wrapper), issuing one
    if it has none. One document never gets two handles."""
    live = _live()
    _prune(live)
    return _handle_in(doc, live)


def _handle_in(doc, live):
    global _next
    handle = _find(doc)
    if handle is not None:
        return handle
    if not any(doc == d for d in live):
        raise CommandError("krita_error", "The document is not open in Krita")
    handle = f"doc-{_next}"
    _next += 1
    _entries[handle] = doc
    return handle


def lookup(handle):
    """The live wrapper for ``handle``. Unknown or closed: ``not_found``."""
    live = _live()
    _prune(live)
    stored = _entries.get(handle)
    if stored is not None:
        for d in live:
            if stored == d:
                return d
    raise CommandError("not_found",
                       f"No open document with handle '{handle}' (closed, or never issued)",
                       {"doc": handle})


def _active_document():
    """``Krita.instance().activeDocument()``; a separate function so tests
    can stand in for it (headless it is always None)."""
    return Krita.instance().activeDocument()


def _by_path(path):
    want = os.path.realpath(os.path.expanduser(path))
    matches = []
    for d in _live():
        name = d.fileName()
        if name and os.path.realpath(name) == want:
            matches.append(d)
    if not matches:
        raise CommandError("not_found", f"No open document has the file {want}", {"doc": path})
    if len(matches) > 1:
        live = _live()
        _prune(live)
        handles = [_handle_in(d, live) for d in matches]
        raise CommandError("ambiguous",
                           f"{len(matches)} open documents have the file {want}: use a handle",
                           {"doc": path, "matches": handles})
    return matches[0]


def resolve_doc(params, field="doc"):
    """``(handle, live Document)`` for ``params[field]``.

    The value is a handle (``doc-3``); or the absolute (or ``~``) path of
    an open document, compared after ``realpath`` with each ``fileName()``
    (several: ``ambiguous``; none: ``not_found``); or absent / None, the
    active document. The path form only identifies a document that is
    already open, so it is not checked against the allowed roots.
    """
    ref = params.get(field)
    if ref is None:
        active = _active_document()
        live = None
        if active is not None:
            live = next((d for d in _live() if active == d), None)
        if live is None:
            raise CommandError("not_found",
                               f"No active document; pass '{field}' (a handle like 'doc-1')")
        return handle_for(live), live
    if not isinstance(ref, str) or ref == "":
        raise CommandError("bad_request",
                           f"'{field}' must be a document handle like 'doc-1' "
                           "or the absolute path of an open document")
    if _HANDLE_RE.fullmatch(ref):
        return ref, lookup(ref)
    if os.path.isabs(os.path.expanduser(ref)):
        live = _by_path(ref)
        return handle_for(live), live
    raise CommandError("bad_request",
                       f"'{field}' {ref!r} is neither a handle like 'doc-1' "
                       "nor an absolute path")


def list_open():
    """``[(handle, live Document)]`` for every open document, in
    ``documents()`` order. Documents without a handle (opened by the user)
    get one now."""
    live = _live()
    _prune(live)
    return [(_handle_in(d, live), d) for d in live]
