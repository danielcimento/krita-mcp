"""File I/O through Krita with batch mode on (D5). Main thread only.

**Crash hazard.** A modal dialog during a command blocks the main thread,
and dismissing one opened from a script has crashed Krita
(krita-api-notes § Crash hazards). So every save, export and open goes
through this module, which turns batch mode on around the one Krita call
and restores the previous value in ``finally``:

- ``doc.setBatchmode(True)``: the document's flag, which ``exportImage``,
  ``saveAs`` and ``save`` read (verified for the first two).
- ``Krita.instance().setBatchmode(True)``: the app-wide flag, for calls
  that read it instead. ``openDocument`` copies it into the new document
  while loading (Krita's source), so a recovery or import-options prompt
  can't appear; ``save`` gets it too, as a belt-and-braces measure. It is
  only on for the duration of the call: left on, it would also silence
  the user's own dialogs.

Krita's ``saveAs`` and ``save`` return True without writing anything when
the directory is missing (verified), so each save or export also checks
the file changed.

``Node.save`` is never used (D5: it ignores the document flag and bakes
opacity into the alpha).
"""

import contextlib
import os
import time

from krita import InfoObject, Krita

from .errors import CommandError


@contextlib.contextmanager
def batch_mode(doc=None, app_wide=False):
    """Batch mode on for ``doc`` (a live wrapper) and/or app-wide for the
    ``with`` block; both restored to their previous values afterwards.

    ``doc`` must still be open when the block ends: don't close it inside.
    """
    app = Krita.instance()
    prev_app = app.batchmode() if app_wide else None
    prev_doc = doc.batchmode() if doc is not None else None
    try:
        if app_wide:
            app.setBatchmode(True)
        if doc is not None:
            doc.setBatchmode(True)
        yield
    finally:
        try:
            if doc is not None:
                doc.setBatchmode(prev_doc)
        finally:
            if app_wide:
                app.setBatchmode(prev_app)


def _stat(path):
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_ino, st.st_size, st.st_mtime_ns)


def _written(path, before):
    """Whether ``path`` is a file that changed since ``before`` (``_stat``).
    ``saveAs`` and ``save`` return True without writing anything when the
    directory is missing (verified), so their bool isn't enough."""
    after = _stat(path)
    return after is not None and os.path.isfile(path) and after != before


# Krita's exportDocumentSync gives up at once (returns False) when it can't
# take the image's barrier lock or the saving mutex: while the image is still
# busy (e.g. just after a save) or an autosave runs. Seen in the GUI only:
# headless images are never busy. So wait for the image to be idle first and
# retry a few times. [live test_allowlist, 2026-11-25]
ATTEMPTS = 20
RETRY_SLEEP_S = 0.1


def _with_retries(doc, call):
    """Run ``call()`` (a Krita save/export returning a bool), waiting for
    the image to be idle first and retrying while Krita refuses. Callers
    still check that the file was written (Krita can claim success
    without writing into a missing directory; retrying won't fix that)."""
    for _ in range(ATTEMPTS):
        doc.waitForDone()
        if call():
            return True
        time.sleep(RETRY_SLEEP_S)
    return False


def export_image(doc, path):
    """``doc.exportImage(path)`` (format from the extension). The
    document's own ``fileName()`` and ``modified`` are unchanged. Failure:
    ``krita_error``."""
    before = _stat(path)
    with batch_mode(doc):
        ok = _with_retries(doc, lambda: doc.exportImage(path, InfoObject()))
    if not ok or not _written(path, before):
        raise CommandError("krita_error", f"Krita failed to export to {path}")


def save_as(doc, path):
    """``doc.saveAs(path)``: the document now lives at ``path`` (kept as
    given, so pass it resolved) and is no longer modified. Failure:
    ``krita_error``; if Krita claimed success without writing the file,
    the document is marked modified again, since it isn't saved anywhere."""
    before = _stat(path)
    with batch_mode(doc):
        ok = _with_retries(doc, lambda: doc.saveAs(path))
    if ok and not _written(path, before):
        doc.setModified(True)  # saveAs cleared it, but nothing was saved
        ok = False
    if not ok:
        raise CommandError("krita_error", f"Krita failed to save to {path}")


def save_in_place(doc):
    """``doc.save()`` to its own ``fileName()``. Failure: ``krita_error``."""
    path = doc.fileName()
    before = _stat(path)
    with batch_mode(doc, app_wide=True):
        ok = _with_retries(doc, doc.save)
    if not ok or not _written(path, before):
        raise CommandError("krita_error", f"Krita failed to save {path}")


def open_document(path):
    """``Krita.instance().openDocument(path)`` with app-wide batch mode.
    Returns the new live wrapper; Krita returning None is ``krita_error``."""
    app = Krita.instance()
    with batch_mode(app_wide=True):
        doc = app.openDocument(path)
    if doc is None:
        raise CommandError("krita_error", f"Krita could not open {path}")
    return doc
