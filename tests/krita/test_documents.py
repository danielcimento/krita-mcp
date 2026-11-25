"""Document tools (P1.7) through dispatch, headless.

Headless ``activeDocument()`` is always None, so every call passes ``doc``.
Every document a test creates or opens is closed in ``tearDown`` through
``close_document(discard=True)``; closed wrappers are never touched.
The allowed root and output dir is a temp dir.
"""

import os
import shutil
import tempfile
import unittest

from krita import Krita, Selection

from helpers import add_paint_layer, settle
from kritamcp import config, fileio, nodes, registry
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run

SRGB = "sRGB-elle-V2-srgbtrc.icc"
DOC_FIELDS = {"doc", "name", "path", "width", "height", "modified", "active"}
INFO_FIELDS = {"doc", "name", "path", "width", "height", "resolution", "color_model",
               "color_depth", "color_profile", "modified", "layer_counts", "max_depth",
               "selection"}


def top_level(doc):
    return [c for c in doc.rootNode().childNodes() if c.type() != "selectionmask"]


class DocumentsBase(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self.out = tempfile.mkdtemp(prefix="kmcp-p17-")
        self.real_out = os.path.realpath(self.out)
        self.outside = tempfile.mkdtemp(prefix="kmcp-p17-outside-")
        self._saved_out = config.CANVAS_OUTPUT_DIR
        config.CANVAS_OUTPUT_DIR = self.out
        config.reset_cache()
        self.handles = []
        self.count_before = len(Krita.instance().documents())

    def tearDown(self):
        try:
            for handle in self.handles:
                try:
                    registry.lookup(handle)
                except CommandError:
                    continue  # already closed
                self.assertOk(run("close_document", doc=handle, discard=True))
            self.assertEqual(len(Krita.instance().documents()), self.count_before)
        finally:
            config.CANVAS_OUTPUT_DIR = self._saved_out
            config.reset_cache()
            shutil.rmtree(self.out, ignore_errors=True)
            shutil.rmtree(self.outside, ignore_errors=True)

    def new(self, **params):
        params.setdefault("width", 8)
        params.setdefault("height", 6)
        result = self.assertOk(run("new_canvas", **params))
        self.handles.append(result["doc"])
        return result

    def open(self, path):
        result = self.assertOk(run("open_file", path=path))
        self.handles.append(result["doc"])
        return result

    def assertNothingCreated(self, code, action, **params):
        count = len(Krita.instance().documents())
        err = self.assertError(run(action, **params), code)
        self.assertEqual(len(Krita.instance().documents()), count)
        return err


class NewCanvasTest(DocumentsBase):
    def test_colored_background_reuses_the_layer(self):
        result = self.new(background="#FF8000", name="kmcp-orange")
        doc = registry.lookup(result["doc"])
        layers = top_level(doc)
        self.assertEqual([(n.name(), n.type()) for n in layers], [("Background", "paintlayer")])
        self.assertEqual(nodes.node_id(layers[0]), result["background_layer"])
        settle(doc)
        self.assertEqual(bytes(layers[0].pixelData(0, 0, 8, 6)), bytes((0, 128, 255, 255)) * 48)
        self.assertFalse(doc.modified())
        self.assertEqual(result["background"], "#ff8000")
        self.assertEqual(result["name"], "kmcp-orange")

    def test_white_background_is_left_as_created(self):
        result = self.new(background="white")
        doc = registry.lookup(result["doc"])
        layers = top_level(doc)
        self.assertEqual(len(layers), 1)
        self.assertEqual(nodes.node_id(layers[0]), result["background_layer"])
        self.assertEqual(bytes(layers[0].pixelData(0, 0, 8, 6)), bytes((255,) * 4) * 48)
        self.assertEqual(result["background"], "#ffffff")

    def test_transparent_background_is_an_empty_layer(self):
        result = self.new(background="Transparent")
        handle = result["doc"]
        doc = registry.lookup(handle)
        layers = top_level(doc)
        self.assertEqual([(n.name(), n.type()) for n in layers], [("Background", "paintlayer")])
        self.assertEqual(nodes.node_id(layers[0]), result["background_layer"])
        self.assertIsNone(nodes.layer_object(layers[0], nodes.build_index(doc))["bounds"])
        self.assertEqual(bytes(layers[0].pixelData(0, 0, 8, 6)), bytes(4) * 48)
        self.assertFalse(doc.modified())
        del doc, layers
        self.assertEqual(result["background"], "transparent")
        got = self.assertOk(run("get_color_at", doc=handle, x=3, y=3))
        self.assertEqual(got["a"], 0)

    def test_bad_background(self):
        for bad in ("#zzzzzz", 5, None, "", "#80ff0000"):
            self.assertNothingCreated("bad_request", "new_canvas", width=4, height=4,
                                      background=bad)

    def test_defaults(self):
        result = self.new()
        self.assertEqual((result["name"], result["color_profile"], result["resolution"],
                          result["background"]), ("New Canvas", SRGB, 120, "#1a1a2e"))
        info = self.assertOk(run("get_document_info", doc=result["doc"]))
        self.assertEqual(info["resolution"], 120)

    def test_color_profile(self):
        result = self.new(color_profile="sRGB built-in")
        self.assertEqual(result["color_profile"], "sRGB built-in")
        info = self.assertOk(run("get_document_info", doc=result["doc"]))
        self.assertEqual(info["color_profile"], "sRGB built-in")
        self.assertEqual(self.new(color_profile="")["color_profile"], SRGB)

        err = self.assertNothingCreated("bad_request", "new_canvas", width=4, height=4,
                                        color_profile="kmcp-no-such.icc")
        self.assertEqual(err["details"]["color_profile"], "kmcp-no-such.icc")
        available = err["details"]["available"]
        self.assertIn(SRGB, available)
        self.assertEqual(available, list(Krita.instance().profiles("RGBA", "U8")))
        self.assertNothingCreated("bad_request", "new_canvas", width=4, height=4, color_profile=7)

    def test_resolution(self):
        result = self.new(resolution=300)
        self.assertEqual(result["resolution"], 300)
        info = self.assertOk(run("get_document_info", doc=result["doc"]))
        self.assertEqual(info["resolution"], 300)
        for bad in (0, -1, 10001, "high", True, float("nan")):
            self.assertNothingCreated("bad_request", "new_canvas", width=4, height=4,
                                      resolution=bad)

    def test_name(self):
        for bad in ("", "  ", 5):
            self.assertNothingCreated("bad_request", "new_canvas", width=4, height=4, name=bad)
        self.assertEqual(len(self.new(name="n" * 300)["name"]), 200)

    def test_full_size_canvas(self):
        # Acceptance test 3, first bullet, headless (D8: Mao's size).
        result = self.new(width=5800, height=8400)
        self.assertEqual((result["width"], result["height"]), (5800, 8400))
        info = self.assertOk(run("get_document_info", doc=result["doc"]))
        self.assertEqual((info["width"], info["height"], info["color_model"],
                          info["color_depth"], info["color_profile"]),
                         (5800, 8400, "RGBA", "U8", SRGB))
        got = self.assertOk(run("get_color_at", doc=result["doc"], x=5799, y=8399))
        self.assertEqual(got["color"], "#1a1a2e")
        self.assertOk(run("close_document", doc=result["doc"], discard=True))


class ListAndInfoTest(DocumentsBase):
    def test_list_documents(self):
        a, b = self.new(name="kmcp-A"), self.new(name="kmcp-B", width=10, height=3)
        listed = self.assertOk(run("list_documents"))
        self.assertEqual(set(listed), {"documents"})
        by_handle = {d["doc"]: d for d in listed["documents"]}
        for entry in listed["documents"]:
            self.assertEqual(set(entry), DOC_FIELDS)
        self.assertEqual(by_handle[a["doc"]], {"doc": a["doc"], "name": "kmcp-A", "path": None,
                                               "width": 8, "height": 6, "modified": False,
                                               "active": False})  # headless: never active
        self.assertEqual(by_handle[b["doc"]]["width"], 10)
        # Order is documents() order; every open document gets a handle.
        self.assertEqual(len(listed["documents"]), len(Krita.instance().documents()))

        saved = self.assertOk(run("save", doc=a["doc"], path="kmcp-a.kra"))
        registry.lookup(b["doc"]).setModified(True)
        by_handle = {d["doc"]: d for d in self.assertOk(run("list_documents"))["documents"]}
        self.assertEqual(by_handle[a["doc"]]["path"], saved["path"])
        self.assertIs(by_handle[b["doc"]]["modified"], True)

    def test_list_marks_the_active_document(self):
        a, b = self.new(name="kmcp-A"), self.new(name="kmcp-B")
        live_b = registry.lookup(b["doc"])
        saved = registry._active_document
        registry._active_document = lambda: live_b
        try:
            listed = self.assertOk(run("list_documents"))["documents"]
            info = self.assertOk(run("get_document_info"))  # no doc: the active one
        finally:
            registry._active_document = saved
        del live_b
        active = {d["doc"]: d["active"] for d in listed}
        self.assertEqual((active[a["doc"]], active[b["doc"]]), (False, True))
        self.assertEqual(info["doc"], b["doc"])

    def test_get_document_info(self):
        result = self.new(name="kmcp-info", width=16, height=12)
        handle = result["doc"]
        doc = registry.lookup(handle)
        group = doc.createGroupLayer("G")
        doc.rootNode().addChildNode(group, None)
        group.addChildNode(doc.createNode("p", "paintlayer"), None)
        inner = doc.createGroupLayer("G2")
        group.addChildNode(inner, None)
        q = doc.createNode("q", "paintlayer")
        inner.addChildNode(q, None)
        q.addChildNode(doc.createNode("m", "transparencymask"), None)
        add_paint_layer(doc, "top")
        settle(doc)
        del doc, group, inner, q

        info = self.assertOk(run("get_document_info", doc=handle))
        self.assertEqual(set(info), INFO_FIELDS)
        self.assertEqual((info["doc"], info["name"], info["path"], info["width"],
                          info["height"], info["color_model"], info["color_depth"],
                          info["color_profile"]),
                         (handle, "kmcp-info", None, 16, 12, "RGBA", "U8", SRGB))
        self.assertEqual(info["layer_counts"],
                         {"grouplayer": 2, "paintlayer": 4, "transparencymask": 1})
        self.assertEqual(info["max_depth"], 4)  # G (1) / G2 (2) / q (3) / m (4)
        self.assertIsNone(info["selection"])
        self.assertIsInstance(info["modified"], bool)

        sel = Selection()
        sel.select(2, 3, 4, 5, 255)
        registry.lookup(handle).setSelection(sel)
        info = self.assertOk(run("get_document_info", doc=handle))
        self.assertEqual(info["selection"], {"x": 2, "y": 3, "w": 4, "h": 5})
        # The global selection's mask is not counted as a layer.
        self.assertEqual(info["layer_counts"],
                         {"grouplayer": 2, "paintlayer": 4, "transparencymask": 1})
        registry.lookup(handle).setSelection(None)
        self.assertIsNone(self.assertOk(run("get_document_info", doc=handle))["selection"])

    def test_info_of_an_empty_document(self):
        handle = self.new(background="transparent")["doc"]
        registry.lookup(handle).rootNode().childNodes()[0].remove()
        info = self.assertOk(run("get_document_info", doc=handle))
        self.assertEqual((info["layer_counts"], info["max_depth"]), ({}, 0))

    def test_unknown_or_missing_doc(self):
        self.assertError(run("get_document_info", doc="doc-999999"), "not_found")
        if Krita.instance().activeDocument() is None:
            self.assertError(run("get_document_info"), "not_found")


class SaveTest(DocumentsBase):
    def test_save_kra_moves_the_document(self):
        handle = self.new()["doc"]
        result = self.assertOk(run("save", doc=handle, path="sub/dir/a.kra"))
        expected = os.path.join(self.real_out, "sub", "dir", "a.kra")
        self.assertEqual(result["path"], expected)  # parents created
        self.assertEqual((result["doc"], result["format"], result["doc_path_changed"]),
                         (handle, "kra", True))
        self.assertEqual(result["bytes"], os.path.getsize(expected))
        self.assertGreater(result["bytes"], 0)
        info = self.assertOk(run("get_document_info", doc=handle))
        self.assertEqual(info["path"], expected)
        self.assertFalse(info["modified"])
        # Again to the same file: the path doesn't change.
        again = self.assertOk(run("save", doc=handle, path=expected, format="kra"))
        self.assertIs(again["doc_path_changed"], False)

    def test_export_png_keeps_the_document_path(self):
        handle = self.new()["doc"]
        result = self.assertOk(run("save", doc=handle, path="a.png"))
        self.assertEqual((result["path"], result["format"], result["doc_path_changed"]),
                         (os.path.join(self.real_out, "a.png"), "png", False))
        self.assertGreater(result["bytes"], 0)
        self.assertIsNone(self.assertOk(run("get_document_info", doc=handle))["path"])

        kra = self.assertOk(run("save", doc=handle, path="a.kra"))["path"]
        result = self.assertOk(run("save", doc=handle, path="b.jpeg", format="JPG"))
        self.assertEqual(result["format"], "jpg")
        self.assertIs(result["doc_path_changed"], False)
        self.assertEqual(self.assertOk(run("get_document_info", doc=handle))["path"], kra)

    def test_save_in_place(self):
        handle = self.new()["doc"]
        kra = self.assertOk(run("save", doc=handle, path="inplace.kra"))["path"]
        doc = registry.lookup(handle)
        doc.rootNode().childNodes()[0].setPixelData(bytes((0, 0, 255, 255)), 0, 0, 1, 1)
        doc.setModified(True)
        del doc
        mtime = os.stat(kra).st_mtime_ns
        result = self.assertOk(run("save", doc=handle))
        self.assertEqual((result["path"], result["format"], result["doc_path_changed"]),
                         (kra, "kra", False))
        self.assertEqual(result["bytes"], os.path.getsize(kra))
        self.assertGreaterEqual(os.stat(kra).st_mtime_ns, mtime)
        self.assertFalse(registry.lookup(handle).modified())
        self.assertOk(run("save", doc=handle, format="kra"))
        self.assertError(run("save", doc=handle, format="png"), "bad_request")

    def test_save_in_place_never_saved_is_conflict(self):
        handle = self.new()["doc"]
        err = self.assertError(run("save", doc=handle), "conflict")
        self.assertEqual(err["details"], {"doc": handle})

    def test_bad_formats_and_paths(self):
        handle = self.new()["doc"]
        err = self.assertError(run("save", doc=handle, path="x.png", format="kra"), "bad_request")
        self.assertEqual(err["details"], {"format": "kra", "extension": "png"})
        err = self.assertError(run("save", doc=handle, path="x.png", format="xcf"),
                               "bad_request")
        self.assertIn("png", err["details"]["available"])
        self.assertError(run("save", doc=handle, path="x.png", format=3), "bad_request")
        self.assertError(run("save", doc=handle, path=os.path.join(self.outside, "x.png")),
                         "forbidden_path")
        self.assertError(run("save", doc=handle, path="x.txt"), "forbidden_path")
        self.assertError(run("save", doc=handle, path="../x.png"), "forbidden_path")
        self.assertFalse(os.listdir(self.out))
        # A file where a parent directory should be.
        open(os.path.join(self.out, "file"), "w").close()
        self.assertError(run("save", doc=handle, path="file/x.png"), "conflict")

    def test_in_place_outside_the_roots_is_forbidden(self):
        handle = self.new()["doc"]
        doc = registry.lookup(handle)
        fileio.save_as(doc, os.path.join(self.outside, "elsewhere.kra"))  # as if by hand
        del doc
        self.assertError(run("save", doc=handle), "forbidden_path")


class OpenCloseTest(DocumentsBase):
    def test_open_already_open_and_reopen(self):
        handle = self.new(name="kmcp-open")["doc"]
        kra = self.assertOk(run("save", doc=handle, path="o.kra"))["path"]
        count = len(Krita.instance().documents())
        # The temp dir is under /var, a symlink to /private/var: both match.
        for path in (kra, "o.kra", os.path.join(self.out, "o.kra")):
            result = self.assertOk(run("open_file", path=path))
            self.assertEqual((result["doc"], result["already_open"], result["path"]),
                             (handle, True, kra))
        self.assertEqual(len(Krita.instance().documents()), count)
        self.assertEqual(result["layer_counts"], {"paintlayer": 1})

        self.assertOk(run("close_document", doc=handle))  # saved: no discard needed
        self.assertError(run("get_document_info", doc=handle), "not_found")
        result = self.open("o.kra")
        self.assertNotEqual(result["doc"], handle)
        self.assertEqual(set(result), {"doc", "path", "name", "width", "height",
                                       "layer_counts", "already_open"})
        self.assertEqual((result["path"], result["name"], result["width"], result["height"],
                          result["layer_counts"], result["already_open"]),
                         (kra, "kmcp-open", 8, 6, {"paintlayer": 1}, False))
        self.assertFalse(registry.lookup(result["doc"]).batchmode())

    def test_name_falls_back_to_the_file_name(self):
        handle = self.new()["doc"]
        self.assertOk(run("save", doc=handle, path="fallback.ora"))
        result = self.open("fallback.ora")
        self.assertEqual(result["name"], "fallback.ora")  # .ora has no title
        listed = {d["doc"]: d for d in self.assertOk(run("list_documents"))["documents"]}
        self.assertEqual(listed[result["doc"]]["name"], "fallback.ora")

    def test_open_errors(self):
        self.assertError(run("open_file", path="missing.png"), "not_found")
        self.assertError(run("open_file", path="notes.txt"), "forbidden_path")
        self.assertError(run("open_file", path=os.path.join(self.outside, "x.png")),
                         "forbidden_path")
        with open(os.path.join(self.out, "garbage.png"), "wb") as f:
            f.write(b"not a png")
        app_batch = Krita.instance().batchmode()
        self.assertNothingCreated("krita_error", "open_file", path="garbage.png")
        self.assertEqual(Krita.instance().batchmode(), app_batch)

    def test_close_document(self):
        handle = self.new(name="kmcp-close")["doc"]
        registry.lookup(handle).setModified(True)
        err = self.assertError(run("close_document", doc=handle), "conflict")
        self.assertEqual(err["details"], {"doc": handle, "name": "kmcp-close"})
        self.assertError(run("close_document", doc=handle, discard=False), "conflict")
        self.assertTrue(registry.lookup(handle).modified())  # untouched, still open

        self.assertError(run("close_document"), "bad_request")
        self.assertError(run("close_document", doc=handle, discard="yes"), "bad_request")

        count = len(Krita.instance().documents())
        result = self.assertOk(run("close_document", doc=handle, discard=True))
        self.assertEqual(result, {"doc": handle, "closed": True})
        self.assertEqual(len(Krita.instance().documents()), count - 1)
        self.assertError(run("get_document_info", doc=handle), "not_found")
        self.assertError(run("close_document", doc=handle, discard=True), "not_found")
        self.assertNotIn(handle, [d["doc"] for d in
                                  self.assertOk(run("list_documents"))["documents"]])


class FileioTest(DocumentsBase):
    """fileio: batch mode on during the call, previous values restored."""

    def test_batch_mode_restores_previous_values(self):
        app = Krita.instance()
        handle = self.new()["doc"]
        doc = registry.lookup(handle)
        app_before = app.batchmode()
        with fileio.batch_mode(doc, app_wide=True):
            self.assertTrue(doc.batchmode())
            self.assertTrue(app.batchmode())
        self.assertFalse(doc.batchmode())
        self.assertEqual(app.batchmode(), app_before)

        doc.setBatchmode(True)
        with self.assertRaises(ZeroDivisionError):
            with fileio.batch_mode(doc):
                1 / 0
        self.assertTrue(doc.batchmode())  # restored to the previous True
        doc.setBatchmode(False)

    def test_export_ok_and_failure(self):
        handle = self.new()["doc"]
        doc = registry.lookup(handle)
        path = os.path.join(self.out, "e.png")
        fileio.export_image(doc, path)
        self.assertTrue(os.path.isfile(path))
        self.assertFalse(doc.batchmode())
        missing = os.path.join(self.out, "missing-dir", "e.png")
        with self.assertRaises(CommandError) as cm:
            fileio.export_image(doc, missing)
        self.assertEqual(cm.exception.code, "krita_error")
        self.assertFalse(doc.batchmode())
        self.assertFalse(os.path.exists(missing))

    def test_save_as_that_writes_nothing_is_an_error(self):
        # saveAs into a missing directory returns True, writes nothing,
        # re-points fileName() and clears modified (verified, P1.7).
        handle = self.new()["doc"]
        doc = registry.lookup(handle)
        with self.assertRaises(CommandError) as cm:
            fileio.save_as(doc, os.path.join(self.out, "missing-dir", "e.kra"))
        self.assertEqual(cm.exception.code, "krita_error")
        self.assertFalse(doc.batchmode())
        self.assertTrue(doc.modified())  # nothing was saved

    def test_save_in_place_into_a_removed_directory_is_an_error(self):
        handle = self.new()["doc"]
        kra = self.assertOk(run("save", doc=handle, path="gone/x.kra"))["path"]
        shutil.rmtree(os.path.dirname(kra))
        registry.lookup(handle).setModified(True)
        self.assertError(run("save", doc=handle), "krita_error")
        self.assertTrue(registry.lookup(handle).modified())
