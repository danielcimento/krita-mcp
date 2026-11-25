"""registry.py (D3): document handles, headless.

Closed documents are never touched: they are closed through their live
wrapper and every later check goes through the registry, which only uses
``==`` on stored wrappers.
"""

import os
import re
import shutil
import tempfile
import unittest

from krita import Krita

from helpers import scratch_document
from kritamcp import registry
from kritamcp.errors import CommandError

HANDLE = re.compile(r"doc-[1-9][0-9]*")


def _save_as(doc, path):
    doc.setBatchmode(True)
    try:
        ok = doc.saveAs(path)
    finally:
        doc.setBatchmode(False)
    if not ok:
        raise RuntimeError(f"saveAs {path} failed")


class HandleTest(unittest.TestCase):
    def assertCode(self, code, fn, *args):
        with self.assertRaises(CommandError) as cm:
            fn(*args)
        self.assertEqual(cm.exception.code, code, cm.exception.message)
        return cm.exception

    def test_distinct_and_stable(self):
        with scratch_document(name="kmcp-reg-a") as a, scratch_document(name="kmcp-reg-b") as b:
            ha, hb = registry.handle_for(a), registry.handle_for(b)
            self.assertRegex(ha, HANDLE)
            self.assertRegex(hb, HANDLE)
            self.assertNotEqual(ha, hb)
            self.assertEqual(registry.handle_for(a), ha)
            self.assertEqual(registry.handle_for(b), hb)
            # A fresh wrapper of the same document gets the same handle.
            fresh = [d for d in Krita.instance().documents() if d == a]
            self.assertEqual(len(fresh), 1)
            self.assertEqual(registry.handle_for(fresh[0]), ha)
            self.assertTrue(registry.lookup(ha) == a)
            self.assertTrue(registry.lookup(hb) == b)
            handle, live = registry.resolve_doc({"doc": ha})
            self.assertEqual(handle, ha)
            self.assertTrue(live == a)

    def test_lookup_after_close_is_not_found(self):
        doc = Krita.instance().createDocument(8, 8, "kmcp-reg-closed", "RGBA", "U8", "", 72.0)
        try:
            handle = registry.handle_for(doc)
            self.assertTrue(registry.lookup(handle) == doc)
        finally:
            doc.setModified(False)
            doc.close()
        del doc  # never touched again
        err = self.assertCode("not_found", registry.lookup, handle)
        self.assertEqual(err.details, {"doc": handle})
        self.assertNotIn(handle, registry._entries)  # pruned
        self.assertCode("not_found", registry.resolve_doc, {"doc": handle})
        # Handles are never reused.
        with scratch_document(name="kmcp-reg-next") as nxt:
            new = registry.handle_for(nxt)
            self.assertGreater(int(new.split("-")[1]), int(handle.split("-")[1]))

    def test_unknown_and_malformed(self):
        self.assertCode("not_found", registry.lookup, "doc-999999")
        self.assertCode("not_found", registry.resolve_doc, {"doc": "doc-999999"})
        for bad in (5, True, [], "", "doc-x", "doc-0", "relative/path.kra", "mydoc"):
            self.assertCode("bad_request", registry.resolve_doc, {"doc": bad})

    def test_handle_for_a_closed_document_refused(self):
        doc = Krita.instance().createDocument(8, 8, "kmcp-reg-unreg", "RGBA", "U8", "", 72.0)
        doc.setModified(False)
        doc.close()
        before = dict(registry._entries)
        self.assertCode("krita_error", registry.handle_for, doc)
        del doc
        self.assertLessEqual(set(registry._entries), set(before))  # nothing added


class PathTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kmcp-reg-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_path_lookup(self):
        path = os.path.join(self.dir, "saved.kra")
        with scratch_document(name="kmcp-reg-path") as a, scratch_document(name="kmcp-reg-other"):
            _save_as(a, path)
            # fileName() keeps the path as given; the temp dir is under
            # /var, a symlink to /private/var, so only realpath matches both.
            self.assertEqual(a.fileName(), path)
            ha = registry.handle_for(a)
            for form in (path, os.path.realpath(path)):
                handle, live = registry.resolve_doc({"doc": form})
                self.assertEqual(handle, ha)
                self.assertTrue(live == a)
            with self.assertRaises(CommandError) as cm:
                registry.resolve_doc({"doc": os.path.join(self.dir, "other.kra")})
            self.assertEqual(cm.exception.code, "not_found")

    def test_path_of_two_documents_is_ambiguous(self):
        path = os.path.join(self.dir, "twice.kra")
        with scratch_document(name="kmcp-reg-t1") as a, scratch_document(name="kmcp-reg-t2") as b:
            _save_as(a, path)
            _save_as(b, path)
            with self.assertRaises(CommandError) as cm:
                registry.resolve_doc({"doc": path})
            self.assertEqual(cm.exception.code, "ambiguous")
            self.assertEqual(sorted(cm.exception.details["matches"]),
                             sorted([registry.handle_for(a), registry.handle_for(b)]))


class ActiveDocumentTest(unittest.TestCase):
    def tearDown(self):
        registry._active_document = self._saved

    def setUp(self):
        self._saved = registry._active_document

    def test_headless_has_no_active_document(self):
        # Pinned (krita-api-notes § Documents): headless, activeDocument()
        # is None even with documents open and after setActiveDocument.
        with scratch_document(name="kmcp-reg-act") as a:
            Krita.instance().setActiveDocument(a)
            self.assertIsNone(Krita.instance().activeDocument())
            for params in ({}, {"doc": None}):
                with self.assertRaises(CommandError) as cm:
                    registry.resolve_doc(params)
                self.assertEqual(cm.exception.code, "not_found")
                self.assertIn("No active document", cm.exception.message)

    def test_active_document_is_matched_against_documents(self):
        with scratch_document(name="kmcp-reg-act2") as a:
            registry._active_document = lambda: [d for d in Krita.instance().documents()
                                                 if d == a][0]
            handle, live = registry.resolve_doc({})
            self.assertEqual(handle, registry.handle_for(a))
            self.assertTrue(live == a)

    def test_closed_active_document_is_not_found(self):
        doc = Krita.instance().createDocument(8, 8, "kmcp-reg-act3", "RGBA", "U8", "", 72.0)
        doc.setModified(False)
        doc.close()
        registry._active_document = lambda: doc  # stale: only compared with ==
        with self.assertRaises(CommandError) as cm:
            registry.resolve_doc({})
        self.assertEqual(cm.exception.code, "not_found")


class ListOpenTest(unittest.TestCase):
    def test_list_open_covers_all_documents(self):
        with scratch_document(name="kmcp-reg-l1") as a, scratch_document(name="kmcp-reg-l2") as b:
            ha = registry.handle_for(a)  # b has no handle yet
            listed = registry.list_open()
            docs = Krita.instance().documents()
            self.assertEqual(len(listed), len(docs))
            for (handle, live), d in zip(listed, docs):
                self.assertRegex(handle, HANDLE)
                self.assertTrue(live == d)
            handles = dict((h, live) for h, live in listed)
            self.assertEqual(len(handles), len(listed))  # no handle twice
            self.assertTrue(handles[ha] == a)
            hb = registry.handle_for(b)
            self.assertIn(hb, handles)
            self.assertTrue(handles[hb] == b)
