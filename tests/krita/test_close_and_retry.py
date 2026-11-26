"""Regressions from the Phase 5 live run (2026-11-25).

- Saves and exports retry while Krita refuses to start them: in the GUI,
  exportDocumentSync returns False at once while the image is busy (seen
  right after a .kra save in live test_allowlist).
- close_document drops the agent journal *before* closing: its entries may
  hold detached nodes, and freeing those after the GUI document is gone is
  the suspected cause of a crash in the live run.
"""

import os
import tempfile
import unittest

from kritamcp import batch, dispatch, fileio
from kritamcp.errors import CommandError



class FakeDoc:
    """Just what fileio's export path calls."""

    def __init__(self, path, refusals):
        self.path, self.refusals = path, refusals
        self.waits = self.calls = 0
        self._batch = False

    def batchmode(self):
        return self._batch

    def setBatchmode(self, value):
        self._batch = value

    def waitForDone(self):
        self.waits += 1

    def exportImage(self, path, info):
        self.calls += 1
        if self.calls <= self.refusals:
            return False
        with open(path, "wb") as f:
            f.write(b"png")
        return True


class RetryTest(unittest.TestCase):
    def setUp(self):
        self.sleep = fileio.RETRY_SLEEP_S
        fileio.RETRY_SLEEP_S = 0
        self.dir = tempfile.mkdtemp(prefix="kmcp-retry-")

    def tearDown(self):
        fileio.RETRY_SLEEP_S = self.sleep
        for name in os.listdir(self.dir):
            os.unlink(os.path.join(self.dir, name))
        os.rmdir(self.dir)

    def test_export_retries_until_krita_accepts(self):
        path = os.path.join(self.dir, "x.png")
        doc = FakeDoc(path, refusals=2)
        fileio.export_image(doc, path)
        self.assertEqual(doc.calls, 3)
        self.assertGreaterEqual(doc.waits, 3)  # waits for the image before each try
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(doc.batchmode())  # restored

    def test_export_gives_up_after_the_attempts(self):
        path = os.path.join(self.dir, "y.png")
        doc = FakeDoc(path, refusals=fileio.ATTEMPTS)
        with self.assertRaises(CommandError) as cm:
            fileio.export_image(doc, path)
        self.assertEqual(cm.exception.code, "krita_error")
        self.assertEqual(doc.calls, fileio.ATTEMPTS)


def run(action, **params):
    return dispatch.execute({"action": action, "params": params})


class CloseDropsJournalFirstTest(unittest.TestCase):
    def test_close_document_forgets_the_journal_while_open(self):
        created = run("new_canvas", width=16, height=16, background="transparent")
        self.assertTrue(created["ok"], created)
        handle = created["result"]["doc"]
        try:
            made = run("create_layers", doc=handle, specs=[{"name": "Gone"}])
            self.assertTrue(made["ok"], made)
            deleted = run("delete_layers", doc=handle, ids=["Gone"])
            self.assertTrue(deleted["ok"], deleted)
            self.assertIn(handle, batch.JOURNALS)  # holds the detached node
            graveyard = len(batch._GRAVEYARD)
        finally:
            closed = run("close_document", doc=handle, discard=True)
        self.assertTrue(closed["ok"], closed)
        self.assertNotIn(handle, batch.JOURNALS)
        self.assertEqual(len(batch._GRAVEYARD), graveyard)  # freed, not parked
