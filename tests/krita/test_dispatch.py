"""dispatch.execute called directly, headless (no window, so no view)."""

import os
import shutil
import tempfile
import unittest

from krita import Krita

from helpers import add_paint_layer, scratch_document, settle, temp_config
from kritamcp import config, dispatch, nodes, registry
from kritamcp.commands import paint

# action -> (timeout, mutating), as declared with @command (D9).
EXPECTED_REGISTRY = {
    "clear": (300, True),
    "close_document": (30, True),
    "create_layers": (300, True),
    "delete_layers": (300, True),
    "duplicate_layer": (300, True),
    "draw_shape": (30, True),
    "export_layers": (300, False),
    "fill": (30, True),
    "get_canvas": (300, False),
    "get_color_at": (30, False),
    "get_document_info": (30, False),
    "import_png": (300, True),
    "journal": (30, False),
    "layer_stats": (300, False),
    "list_documents": (30, False),
    "lint": (300, False),
    "lint_snapshot": (300, False),
    "list_brushes": (30, False),
    "list_layers": (30, False),
    "move_layers": (300, True),
    "new_canvas": (300, True),
    "open_file": (300, True),
    "redo": (300, True),
    "render": (300, False),
    "save": (300, False),
    "select": (30, False),
    "selection_to_layer": (300, True),
    "set_brush": (30, False),
    "set_active_layer": (30, False),
    "set_color": (30, False),
    "stroke": (30, True),
    "trim_layers": (300, True),
    "ui_redo": (30, True),
    "ui_undo": (30, True),
    "undo": (300, True),
    "update_layers": (300, True),
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
                         "bad_request", "'width' must be at least 1")
        self.assertError(run("new_canvas", width="wide", height=48),
                         "bad_request", "'width' must be a number")

    def test_new_canvas_per_side_limit(self):
        # Acceptance test 3, at the default limits (the runner has no config file).
        err = self.assertError(run("new_canvas", width=16385, height=16), "too_large")
        self.assertEqual(err["details"], {"limit": "max_canvas_dim", "max": 16384, "width": 16385})
        self.assertIn("~/.krita-mcp-config.json", err["message"])

    def test_new_canvas_too_many_pixels(self):
        err = self.assertError(run("new_canvas", width=9000, height=9000), "too_large")
        self.assertEqual(err["details"],
                         {"limit": "max_canvas_pixels", "max": 67108864, "pixels": 81000000})
        self.assertIn("max_canvas_pixels", err["message"])

    def test_new_canvas_returns_ok(self):
        before = list(Krita.instance().documents())
        try:
            result = self.assertOk(run("new_canvas", width=64, height=48,
                                       name="kmcp-dispatch-test"))
            self.assertRegex(result.pop("doc"), r"^doc-[1-9][0-9]*$")
            self.assertRegex(result.pop("background_layer"), r"^\{[0-9a-f-]{36}\}$")
            self.assertEqual(result, {"width": 64, "height": 48, "name": "kmcp-dispatch-test",
                                      "color_profile": "sRGB-elle-V2-srgbtrc.icc",
                                      "resolution": 120, "background": "#1a1a2e"})
        finally:
            # Defensive: close anything new without touching it otherwise.
            for d in Krita.instance().documents():
                if not any(d == b for b in before):
                    try:
                        d.setModified(False)
                    finally:
                        d.close()

    def test_new_canvas_limits_come_from_config(self):
        # Small limits keep this quick; the full-size 5800x8400 canvas is
        # test_documents.NewCanvasTest.test_full_size_canvas.
        self.assertEqual(dict(config.current().limits), config.DEFAULT_LIMITS)
        with temp_config({"limits": {"max_canvas_dim": 64, "max_canvas_pixels": 2048}}):
            self.assertEqual(config.current().errors, ())
            err = self.assertError(run("new_canvas", width=65, height=10), "too_large")
            self.assertEqual(err["details"]["limit"], "max_canvas_dim")
            err = self.assertError(run("new_canvas", width=50, height=50), "too_large")
            self.assertEqual(err["details"]["limit"], "max_canvas_pixels")
            before = list(Krita.instance().documents())
            try:
                self.assertOk(run("new_canvas", width=32, height=32, name="kmcp-limit-test"))
            finally:
                for d in Krita.instance().documents():
                    if not any(d == b for b in before):
                        try:
                            d.setModified(False)
                        finally:
                            d.close()
        # Restored: the defaults apply again.
        self.assertEqual(dict(config.current().limits), config.DEFAULT_LIMITS)
        self.assertError(run("new_canvas", width=0, height=1), "bad_request")

    def test_get_canvas_writes_to_current_output_dir(self):
        # get_canvas reads config.current().output_dir per call (it follows
        # CANVAS_OUTPUT_DIR, which current() keys on). Headless there is no
        # active document, so the document is passed as a handle.
        out = tempfile.mkdtemp(prefix="kmcp-out-")
        saved = config.CANVAS_OUTPUT_DIR
        try:
            config.CANVAS_OUTPUT_DIR = out
            with scratch_document(8, 8) as doc:
                handle = registry.handle_for(doc)
                result = self.assertOk(run("get_canvas", filename="../kmcp-shot", doc=handle))
            expected = os.path.join(os.path.realpath(out), "kmcp-shot.png")
            self.assertEqual(result, {"path": expected, "doc": handle, "width": 8, "height": 8,
                                      "region": {"x": 0, "y": 0, "w": 8, "h": 8}, "scale": 1.0,
                                      "bytes": os.path.getsize(expected)})
            self.assertTrue(os.path.isfile(expected))
        finally:
            config.CANVAS_OUTPUT_DIR = saved
            config.reset_cache()
            shutil.rmtree(out, ignore_errors=True)

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

    def test_save_no_path_never_saved_is_conflict(self):
        # No path means "save in place" (P1.7), which needs a saved document.
        with scratch_document(8, 8) as doc:
            handle = registry.handle_for(doc)
            err = self.assertError(run("save", doc=handle), "conflict")
            self.assertEqual(err["details"], {"doc": handle})

    def test_open_file_bad_extension(self):
        err = run("open_file", path="notes.txt")
        self.assertIs(err["ok"], False)
        self.assertEqual(err["error"]["message"], "File type not allowed: notes.txt")

    def test_brush_preset_needs_a_view(self):
        self.assertError(run("set_brush", preset="Basic"), "conflict",
                         "No active view: a brush preset can only be set in a Krita window")

    def test_stroke_validation_before_view(self):
        self.assertError(run("stroke", points=[[0, 0]]),
                         "bad_request", "Need at least 2 points for a stroke")
        self.assertError(run("stroke", points=[[0, 0], [1]]),
                         "bad_request", "Each point must be an [x, y] pair of numbers")

    def test_no_document_is_not_found(self):
        if Krita.instance().activeDocument() is not None:
            self.skipTest("a document is active")
        msg = "No active document; pass 'doc' (a handle like 'doc-1')"
        for action, params in (("fill", {"x": 1, "y": 1}), ("clear", {}),
                               ("stroke", {"points": [[0, 0], [1, 1]]}),
                               ("draw_shape", {}), ("get_color_at", {"x": 0, "y": 0}),
                               ("get_canvas", {}), ("save", {"path": "kmcp-x.png"})):
            self.assertError(run(action, **params), "not_found", msg)


RED = bytes((0, 0, 255, 255))      # BGRA
GREEN = bytes((0, 255, 0, 255))
WHITE = bytes((255, 255, 255, 255))
CLEAR = bytes(4)


class PaintStateMixin:
    """Save and restore the plugin-side paint state (D15) around each test,
    and stand-in hooks the tests replace."""

    def setUp(self):
        self._paint_state = (paint.current_color, paint.current_brush_size,
                             paint.current_opacity)
        self._active_node = nodes.active_node

    def tearDown(self):
        (paint.current_color, paint.current_brush_size,
         paint.current_opacity) = self._paint_state
        nodes.active_node = self._active_node


class PaintTest(PaintStateMixin, EnvelopeAssertions, unittest.TestCase):
    """Painting with doc + layer, headless (no view)."""

    def test_fill_targets_doc_and_layer_only(self):
        with scratch_document(16, 16, name="kmcp-paint-a") as a, \
                scratch_document(16, 16, name="kmcp-paint-b") as b:
            ink_a, ink_b = add_paint_layer(a, "Ink"), add_paint_layer(b, "Ink")
            ha, hb = registry.handle_for(a), registry.handle_for(b)
            self.assertOk(run("set_color", color="#ff0000"))
            result = self.assertOk(run("fill", doc=ha, layer="Ink", x=8, y=8, radius=3))
            self.assertEqual(result, {"x": 8, "y": 8, "radius": 3, "doc": ha,
                                      "layer": nodes.node_id(ink_a)})
            settle(a)
            settle(b)
            self.assertEqual(bytes(ink_a.pixelData(8, 8, 1, 1)), RED)
            self.assertEqual(bytes(ink_a.pixelData(0, 0, 1, 1)), CLEAR)
            self.assertEqual(bytes(ink_b.pixelData(0, 0, 16, 16)), CLEAR * 256)
            self.assertEqual(bytes(b.rootNode().projectionPixelData(8, 8, 1, 1)), WHITE)

            # draw_shape and stroke, by id and by path.
            result = self.assertOk(run("draw_shape", doc=hb, layer=nodes.node_id(ink_b),
                                       shape="rectangle", x=0, y=0, width=4, height=4))
            self.assertEqual((result["doc"], result["layer"]), (hb, nodes.node_id(ink_b)))
            result = self.assertOk(run("stroke", doc=hb, layer="/Ink",
                                       points=[[2, 12], [12, 12]], size=3, hardness=1.0))
            self.assertEqual((result["doc"], result["layer"]), (hb, nodes.node_id(ink_b)))
            settle(b)
            self.assertEqual(bytes(ink_b.pixelData(1, 1, 1, 1)), RED)
            self.assertEqual(bytes(ink_b.pixelData(7, 12, 1, 1)), RED)
            self.assertEqual(bytes(ink_a.pixelData(1, 1, 1, 1)), CLEAR)

    def test_non_paint_layers_conflict(self):
        with scratch_document(16, 16, name="kmcp-paint-group") as doc:
            group = doc.createGroupLayer("G")
            doc.rootNode().addChildNode(group, None)
            handle = registry.handle_for(doc)
            gid = nodes.node_id(group)
            err = self.assertError(run("fill", doc=handle, layer="G", x=1, y=1), "conflict")
            self.assertIn("grouplayer", err["message"])
            self.assertEqual(err["details"], {"doc": handle, "layer": gid, "type": "grouplayer"})
            self.assertError(run("clear", doc=handle, layer=gid), "conflict")
            # Default layer: the active node, which headless is always None.
            self.assertError(run("fill", doc=handle, x=1, y=1), "not_found",
                             f"No active layer in {handle}; pass 'layer'")
            # An active group is a conflict too (plan finding 9).
            nodes.active_node = lambda d: d.nodeByUniqueID(group.uniqueId())
            err = self.assertError(run("draw_shape", doc=handle, shape="rectangle"), "conflict")
            self.assertEqual(err["details"]["layer"], gid)
            # And unknown / ambiguous layer refs.
            self.assertError(run("fill", doc=handle, layer="Nope", x=1, y=1), "not_found")
            add_paint_layer(doc, "Twin")
            add_paint_layer(doc, "Twin")
            err = self.assertError(run("fill", doc=handle, layer="Twin", x=1, y=1), "ambiguous")
            self.assertEqual(len(err["details"]["matches"]), 2)

    def test_default_layer_is_the_active_paint_layer(self):
        with scratch_document(16, 16, name="kmcp-paint-active") as doc:
            ink = add_paint_layer(doc, "Ink")
            handle = registry.handle_for(doc)
            nodes.active_node = lambda d: d.nodeByUniqueID(ink.uniqueId())
            paint.current_color = "#00ff00"
            result = self.assertOk(run("fill", doc=handle, x=2, y=2, radius=1))
            self.assertEqual(result["layer"], nodes.node_id(ink))
            settle(doc)
            self.assertEqual(bytes(ink.pixelData(2, 2, 1, 1)), GREEN)

    def test_unknown_or_bad_doc(self):
        self.assertError(run("fill", doc="doc-999999", layer="Ink", x=1, y=1), "not_found")
        self.assertError(run("get_color_at", doc="doc-999999", x=0, y=0), "not_found")
        self.assertError(run("get_canvas", doc="doc-999999"), "not_found")
        self.assertError(run("fill", doc=5, x=1, y=1), "bad_request")

    def test_set_color(self):
        self.assertError(run("set_color", color="#zzzzzz"), "bad_request")
        self.assertError(run("set_color", color=123), "bad_request")
        self.assertError(run("set_color"), "bad_request")
        result = self.assertOk(run("set_color", color="#00FF00"))
        self.assertEqual(result, {"color": "#00ff00", "view_updated": False})
        self.assertEqual(paint.current_color, "#00ff00")
        with scratch_document(8, 8, name="kmcp-paint-color") as doc:
            ink = add_paint_layer(doc, "Ink")
            handle = registry.handle_for(doc)
            self.assertOk(run("fill", doc=handle, layer="Ink", x=4, y=4, radius=2))
            settle(doc)
            self.assertEqual(bytes(ink.pixelData(4, 4, 1, 1)), GREEN)

    def test_color_never_set_paints_black(self):
        paint.current_color = None
        with scratch_document(8, 8, name="kmcp-paint-black") as doc:
            ink = add_paint_layer(doc, "Ink")
            handle = registry.handle_for(doc)
            self.assertOk(run("fill", doc=handle, layer="Ink", x=4, y=4, radius=2))
            settle(doc)
            self.assertEqual(bytes(ink.pixelData(4, 4, 1, 1)), bytes((0, 0, 0, 255)))

    def test_set_brush_without_view(self):
        result = self.assertOk(run("set_brush", size=7, opacity=0.5))
        self.assertEqual(result, {"preset": None, "size": 7, "opacity": 0.5,
                                  "view_updated": False})
        self.assertEqual((paint.current_brush_size, paint.current_opacity), (7, 0.5))
        self.assertError(run("set_brush", size=0), "bad_request")
        # A preset needs a view; nothing is stored when it fails.
        self.assertError(run("set_brush", preset="Basic", size=9), "conflict")
        self.assertEqual(paint.current_brush_size, 7)

    def test_clear(self):
        with scratch_document(16, 16, name="kmcp-clear") as doc:
            ink = add_paint_layer(doc, "Ink")
            handle = registry.handle_for(doc)
            # Pixels inside and past the canvas edges (bounds aren't clipped).
            ink.setPixelData(RED * 16, -5, -5, 4, 4)
            ink.setPixelData(RED * 4, 20, 20, 2, 2)
            ink.setPixelData(RED * 4, 3, 3, 2, 2)
            settle(doc)
            r = ink.bounds()
            self.assertEqual((r.x(), r.y(), r.width(), r.height()), (-5, -5, 27, 27))

            result = self.assertOk(run("clear", doc=handle, layer="Ink", color="#0000ff"))
            self.assertEqual(result, {"color": "#0000ff", "doc": handle,
                                      "layer": nodes.node_id(ink)})
            settle(doc)
            self.assertEqual(bytes(ink.pixelData(0, 0, 16, 16)),
                             bytes((255, 0, 0, 255)) * 256)
            self.assertEqual(bytes(ink.pixelData(-5, -5, 1, 1)), RED)  # outside: kept

            result = self.assertOk(run("clear", doc=handle, layer="Ink", color="Transparent"))
            self.assertEqual(result["color"], "transparent")
            settle(doc)
            # Bounds shrink to nothing, but hasExtents() stays True after
            # zeroing (pinned; krita-api-notes § Nodes), so the layer object
            # checks the size too.
            r = ink.bounds()
            self.assertEqual((r.x(), r.y(), r.width(), r.height()), (0, 0, 0, 0))
            self.assertTrue(ink.hasExtents())
            self.assertIsNone(nodes.layer_object(ink, nodes.build_index(doc))["bounds"])
            self.assertEqual(bytes(ink.pixelData(-5, -5, 27, 27)), CLEAR * 27 * 27)
            # Clearing an empty layer works too.
            self.assertOk(run("clear", doc=handle, layer="Ink", color="transparent"))

            self.assertError(run("clear", doc=handle, layer="Ink", color="#nothex"),
                             "bad_request")

    def test_get_color_at(self):
        with scratch_document(16, 10, name="kmcp-sample") as doc:
            ink = add_paint_layer(doc, "Ink")
            group = doc.createGroupLayer("G")
            doc.rootNode().addChildNode(group, None)
            inner = doc.createNode("Inner", "paintlayer")
            group.addChildNode(inner, None)
            ink.setPixelData(RED, 2, 3, 1, 1)
            inner.setPixelData(GREEN, 5, 5, 1, 1)
            mask = doc.createNode("Mask", "transparencymask")
            ink.addChildNode(mask, None)
            settle(doc)
            handle = registry.handle_for(doc)

            # Bounds come from the document (16x10).
            for x, y in ((16, 0), (0, 10), (-1, 0), (0, -1)):
                err = self.assertError(run("get_color_at", doc=handle, x=x, y=y), "bad_request")
                self.assertIn("16x10", err["message"])
            self.assertError(run("get_color_at", doc=handle, y=0), "bad_request")

            got = self.assertOk(run("get_color_at", doc=handle, x=15, y=9))
            self.assertEqual(got, {"color": "#ffffff", "r": 255, "g": 255, "b": 255, "a": 255,
                                   "doc": handle, "layer": None})
            got = self.assertOk(run("get_color_at", doc=handle, x=2, y=3))
            self.assertEqual(got["color"], "#ff0000")  # composite

            got = self.assertOk(run("get_color_at", doc=handle, x=2, y=3, layer="Ink"))
            self.assertEqual((got["color"], got["a"], got["layer"]),
                             ("#ff0000", 255, nodes.node_id(ink)))
            got = self.assertOk(run("get_color_at", doc=handle, x=5, y=5, layer="Ink"))
            self.assertEqual(got["a"], 0)  # the layer alone, not the composite
            got = self.assertOk(run("get_color_at", doc=handle, x=5, y=5, layer="G"))
            self.assertEqual((got["color"], got["a"], got["layer"]),
                             ("#00ff00", 255, nodes.node_id(group)))
            err = self.assertError(run("get_color_at", doc=handle, x=0, y=0, layer="Ink/Mask"),
                                   "conflict")
            self.assertEqual(err["details"]["type"], "transparencymask")


class DocumentHandleTest(EnvelopeAssertions, unittest.TestCase):
    """new_canvas / open_file / save return and take handles."""

    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="kmcp-docs-")
        self._saved_out = config.CANVAS_OUTPUT_DIR
        config.CANVAS_OUTPUT_DIR = self.out

    def tearDown(self):
        config.CANVAS_OUTPUT_DIR = self._saved_out
        config.reset_cache()
        shutil.rmtree(self.out, ignore_errors=True)

    def _close(self, handle):
        doc = registry.lookup(handle)
        doc.setModified(False)
        doc.close()

    def test_new_canvas_document_stays_open(self):
        result = self.assertOk(run("new_canvas", width=24, height=12, name="kmcp-handle"))
        handle = result["doc"]
        try:
            # The registry holds the wrapper, so the headless document lives.
            doc = registry.lookup(handle)
            self.assertEqual((doc.width(), doc.height(), doc.name()), (24, 12, "kmcp-handle"))
            del doc
            got = self.assertOk(run("get_color_at", doc=handle, x=0, y=0))
            self.assertEqual(got["color"], "#1a1a2e")
            self.assertEqual(got["doc"], handle)
        finally:
            self._close(handle)
        self.assertError(run("get_color_at", doc=handle, x=0, y=0), "not_found")

    def test_save_and_open_file(self):
        with scratch_document(8, 6, name="kmcp-save") as doc:
            handle = registry.handle_for(doc)
            result = self.assertOk(run("save", doc=handle, path="kmcp-saved.png"))
            expected = os.path.join(os.path.realpath(self.out), "kmcp-saved.png")
            self.assertGreater(result.pop("bytes"), 0)
            self.assertEqual(result, {"path": expected, "doc": handle, "format": "png",
                                      "doc_path_changed": False, "blend_conversions": []})
            self.assertTrue(os.path.isfile(expected))
        result = self.assertOk(run("open_file", path="kmcp-saved.png"))
        opened = result["doc"]
        try:
            self.assertNotEqual(opened, handle)
            self.assertEqual((result["width"], result["height"]), (8, 6))
            self.assertTrue(registry.lookup(opened) is not None)
        finally:
            self._close(opened)
