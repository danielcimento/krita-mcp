"""Headless smoke tests: Krita's scripting API works under kritarunner."""

import sys
import unittest

from krita import Krita

from helpers import add_paint_layer, scratch_document, settle


class EnvironmentTest(unittest.TestCase):
    def test_python_is_3_13_or_later(self):
        self.assertGreaterEqual(sys.version_info[:2], (3, 13))

    def test_headless_has_no_windows(self):
        self.assertEqual(len(Krita.instance().windows()), 0)


class DocumentTest(unittest.TestCase):
    def test_pixel_roundtrip(self):
        data = bytes((1, 2, 3, 255)) * 4  # 2x2 BGRA
        with scratch_document(16, 16) as doc:
            node = add_paint_layer(doc, "p")
            node.setPixelData(data, 2, 2, 2, 2)
            settle(doc)
            self.assertEqual(bytes(node.pixelData(2, 2, 2, 2)), data)
            self.assertEqual(bytes(node.pixelData(0, 0, 1, 1)), bytes(4))

    def test_document_size(self):
        with scratch_document(32, 24) as doc:
            self.assertEqual((doc.width(), doc.height()), (32, 24))


class PluginImportTest(unittest.TestCase):
    """Importing kritamcp headless has no side effects (D16)."""

    def test_import_kritamcp(self):
        import kritamcp  # noqa: F401

    def test_import_registers_extension_but_starts_nothing(self):
        import kritamcp
        from kritamcp import bridge
        from kritamcp.extension import KritaMCPExtension

        self.assertIsNone(bridge.AUTH_TOKEN)
        self.assertEqual(bridge.command_queue.status(), {"busy": None, "queued": 0})
        ours = [e for e in Krita.instance().extensions() if isinstance(e, KritaMCPExtension)]
        self.assertEqual(len(ours), 1)  # registered exactly once
        # No window, so createActions never ran: no server thread, no timer.
        self.assertIsNone(ours[0].server_thread)
        self.assertIsNone(ours[0].timer)
