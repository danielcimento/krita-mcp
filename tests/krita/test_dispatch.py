"""dispatch.execute called directly, headless (no window, so no view)."""

import os
import shutil
import tempfile
import unittest

from krita import Krita

from helpers import scratch_document
from kritamcp import dispatch
from kritamcp.commands import documents

# action -> (timeout, mutating), as declared with @command (D9).
EXPECTED_REGISTRY = {
    "clear": (300, True),
    "draw_shape": (30, True),
    "fill": (30, True),
    "get_canvas": (300, False),
    "get_color_at": (30, False),
    "list_brushes": (30, False),
    "new_canvas": (300, True),
    "open_file": (300, True),
    "redo": (30, True),
    "save": (300, False),
    "set_brush": (30, False),
    "set_color": (30, False),
    "stroke": (30, True),
    "undo": (30, True),
}


def run(action, **params):
    return dispatch.execute({"action": action, "params": params})


class EnvelopeAssertions:
    def assertOk(self, envelope):
        self.assertIs(envelope.get("ok"), True, envelope)
        self.assertEqual(set(envelope), {"ok", "result", "elapsed_ms"})
        self.assertIsInstance(envelope["elapsed_ms"], int)
        self.assertGreaterEqual(envelope["elapsed_ms"], 0)
        self.assertNotIn("status", envelope["result"])
        return envelope["result"]

    def assertError(self, envelope, code, message=None):
        self.assertIs(envelope.get("ok"), False, envelope)
        self.assertEqual(set(envelope), {"ok", "error", "elapsed_ms"})
        self.assertIsInstance(envelope["elapsed_ms"], int)
        self.assertEqual(envelope["error"]["code"], code, envelope)
        if message is not None:
            self.assertEqual(envelope["error"]["message"], message)
        return envelope["error"]


class RegistryTest(unittest.TestCase):
    def test_actions_and_flags(self):
        self.assertEqual(dispatch.actions(), sorted(EXPECTED_REGISTRY))
        for name, (timeout, mutating) in EXPECTED_REGISTRY.items():
            entry = dispatch.get(name)
            self.assertEqual((entry.name, entry.timeout, entry.mutating),
                             (name, timeout, mutating), name)
            self.assertTrue(callable(entry.fn))

    def test_get_unknown(self):
        self.assertIsNone(dispatch.get("nope"))

    def test_duplicate_registration_refused(self):
        with self.assertRaises(ValueError):
            dispatch.command("undo")(lambda params: {})


class DispatchTest(EnvelopeAssertions, unittest.TestCase):
    def test_unknown_action(self):
        self.assertError(run("nope"), "bad_request", "Unknown action: nope")
        self.assertError(dispatch.execute({}), "bad_request", "Unknown action: None")
        self.assertError(dispatch.execute("undo"), "bad_request")

    def test_params_must_be_an_object(self):
        self.assertError(dispatch.execute({"action": "new_canvas", "params": None}),
                         "bad_request", "'params' must be a JSON object")
        self.assertError(dispatch.execute({"action": "list_brushes", "params": [1]}),
                         "bad_request")

    def test_unexpected_exception_is_krita_error(self):
        name = "kmcp_test_raises"

        @dispatch.command(name)
        def boom(params):
            raise RuntimeError("kaboom")

        try:
            self.assertError(run(name), "krita_error", "kaboom")
        finally:
            del dispatch._registry[name]

    def test_new_canvas_width_zero(self):
        self.assertError(run("new_canvas", width=0, height=48),
                         "bad_request", "'width' must be between 1 and 8192")

    def test_new_canvas_too_many_pixels(self):
        self.assertError(run("new_canvas", width=8192, height=8192),
                         "too_large", "Canvas too large (max 16777216 pixels)")

    def test_new_canvas_returns_ok(self):
        before = list(Krita.instance().documents())
        try:
            result = self.assertOk(run("new_canvas", width=64, height=48,
                                       name="kmcp-dispatch-test"))
            self.assertEqual(result, {"width": 64, "height": 48, "name": "kmcp-dispatch-test"})
            # Headless there is no window, so no view is added. The document
            # is owned by the Python wrapper createDocument returned, and is
            # destroyed when cmd_new_canvas returns and drops it. So nothing
            # stays open (pinned; a handle registry must keep the wrapper).
            created = [d for d in Krita.instance().documents()
                       if not any(d == b for b in before)]
            self.assertEqual(created, [])
        finally:
            # Defensive: close anything new without touching it otherwise.
            for d in Krita.instance().documents():
                if not any(d == b for b in before):
                    try:
                        d.setModified(False)
                    finally:
                        d.close()

    def test_list_brushes(self):
        result = self.assertOk(run("list_brushes"))
        self.assertEqual(set(result), {"brushes", "count"})
        self.assertIsInstance(result["brushes"], list)
        self.assertEqual(result["count"], len(result["brushes"]))
        self.assertLessEqual(result["count"], 50)

    def test_list_brushes_bad_limit(self):
        self.assertError(run("list_brushes", limit=0),
                         "bad_request", "'limit' must be between 1 and 1000")

    def test_save_outside_allowed_dirs(self):
        # Only ok/message are pinned: the code becomes forbidden_path once
        # paths.py passes codes.
        envelope = run("save", path="/etc/kmcp-test.png")
        self.assertIs(envelope["ok"], False)
        err = envelope["error"]
        self.assertIn("allowed directories", err["message"])
        self.assertIn("/etc/kmcp-test.png", err["message"])

    def test_save_no_path(self):
        err = run("save")
        self.assertIs(err["ok"], False)
        self.assertEqual(err["error"]["message"], "No path specified")

    def test_open_file_bad_extension(self):
        err = run("open_file", path="notes.txt")
        self.assertIs(err["ok"], False)
        self.assertEqual(err["error"]["message"], "File type not allowed: notes.txt")

    def test_view_commands_need_a_view(self):
        self.assertError(run("set_color", color="#ff0000"), "conflict", "No active view")
        self.assertError(run("set_brush", size=5), "conflict", "No active view")

    def test_stroke_validation_before_view(self):
        self.assertError(run("stroke", points=[[0, 0]]),
                         "bad_request", "Need at least 2 points for a stroke")
        self.assertError(run("stroke", points=[[0, 0], [1]]),
                         "bad_request", "Each point must be an [x, y] pair of numbers")

    def test_no_document_is_not_found(self):
        if Krita.instance().activeDocument() is not None:
            self.skipTest("a document is active")
        self.assertError(run("fill", x=1, y=1), "not_found", "No active layer")
        self.assertError(run("clear"), "not_found", "No active layer")
        self.assertError(run("get_color_at", x=0, y=0), "not_found", "No active document")
        self.assertError(run("get_canvas"), "not_found", "No active document")


class ExportTest(unittest.TestCase):
    """documents._export: batch mode always reset; a failed export raises."""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="kmcp-export-")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_export_ok(self):
        path = os.path.join(self.dir, "out.png")
        with scratch_document(8, 8) as doc:
            documents._export(doc, path)
            self.assertFalse(doc.batchmode())
        self.assertTrue(os.path.isfile(path))

    def test_export_failure_is_krita_error(self):
        path = os.path.join(self.dir, "missing-dir", "out.png")
        with scratch_document(8, 8) as doc:
            with self.assertRaises(documents.CommandError) as cm:
                documents._export(doc, path)
            self.assertFalse(doc.batchmode())
        self.assertEqual(cm.exception.code, "krita_error")
        self.assertFalse(os.path.exists(path))
