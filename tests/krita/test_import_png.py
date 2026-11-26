"""commands/pixels.py: import_png, headless, through dispatch.execute.

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. PNGs live in a temp dir made an
allowed root through a temp config file. Undo is checked the way
``krita_undo`` (P5.1) will do it: pop the call's journal entry and run its
inverses through ``batch.rollback`` with the live document.
"""

import contextlib
import os
import shutil
import tempfile
import unittest

from krita import Krita, Selection

from helpers import add_paint_layer, scratch_document, settle, temp_config
from kritamcp import batch, nodes, pixels, registry
from kritamcp.commands import pixels as pixel_cmds
from test_dispatch import EnvelopeAssertions, run
from test_pixels import pattern, solid

# A window larger than the 32x24 test canvases, for reading pixels that lie
# past the canvas edge too.
WIN = (-8, -8, 56, 48)


def opaque_pattern(w, h, seed=0):
    """``pattern`` with every alpha at least 1: no transparent margin."""
    data = bytearray(pattern(w, h, seed))
    data[3::4] = bytes(max(1, a) for a in data[3::4])
    return bytes(data)


def splice(window, win, rect, data):
    """``window`` (pixels over ``win``) with ``rect`` overwritten by
    ``data``; rect must lie inside win."""
    wx, wy, ww, _ = win
    x, y, w, h = rect
    out = bytearray(window)
    for r in range(h):
        o = 4 * ((y - wy + r) * ww + (x - wx))
        out[o:o + 4 * w] = data[4 * w * r:4 * w * (r + 1)]
    return bytes(out)


def read(node, rect=WIN):
    return pixels.as_bytes(node.pixelData(*rect))


def bounds_of(node):
    r = node.bounds()
    if not node.hasExtents() or r.width() <= 0 or r.height() <= 0:
        return None
    return {"x": r.x(), "y": r.y(), "w": r.width(), "h": r.height()}


class ImportPngBase(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-import-")
        self.outside = tempfile.mkdtemp(prefix="kmcp-outside-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.callback(shutil.rmtree, self.outside, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def png(self, name, data, w, h, where=None):
        path = os.path.join(where or self.root, name)
        pixels.save_png(data, path, w, h)
        return path

    def setup_doc(self, doc, fill=True):
        """A paint layer "P" filled over the canvas with partial and zero
        alpha (colours under it too); returns (handle, P's id)."""
        handle = registry.handle_for(doc)
        node = add_paint_layer(doc, "P")
        if fill:
            self.assertTrue(node.setPixelData(pattern(32, 24, 7), 0, 0, 32, 24))
        settle(doc)
        batch.JOURNALS.get(handle).clear()
        return handle, nodes.node_id(node)

    def node(self, doc, nid):
        return nodes.node_by_id(doc, nid)

    def undo_last(self, handle, doc):
        entry = batch.JOURNALS.get(handle).pop()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.label, "import_png")
        ok, errors = batch.rollback(list(entry.inverses), doc)
        self.assertEqual((ok, errors), (len(entry.inverses), []))
        settle(doc)
        return entry


class ImportPngTest(ImportPngBase):
    def test_replace_bit_exact_partly_off_canvas_and_undo(self):
        data = pattern(10, 8, 3)  # partial and zero alpha, colours under zero alpha
        png = self.png("a.png", data, 10, 8)
        with scratch_document(32, 24, name="kmcp-import-replace") as doc:
            handle, nid = self.setup_doc(doc)
            before = read(self.node(doc, nid))
            before_bounds = bounds_of(self.node(doc, nid))
            result = self.assertOk(run("import_png", doc=handle, id="P", png=png, x=27, y=20))
            node = self.node(doc, nid)
            self.assertEqual(result["changed"], {"x": 27, "y": 20, "w": 10, "h": 8})
            self.assertEqual((result["doc"], result["layer"], result["mode"], result["png"],
                              result["dry_run"], result["clear_outside"], result["warnings"]),
                             (handle, nid, "replace", os.path.realpath(png), False, False, []))
            self.assertEqual(read(node, (27, 20, 10, 8)), data)  # kept past the canvas edge
            self.assertEqual(read(node), splice(before, WIN, (27, 20, 10, 8), data))
            self.assertEqual(result["bounds"], bounds_of(node))
            b = result["bounds"]
            self.assertGreater(b["x"] + b["w"], 32)  # bounds extend past the canvas
            self.assertGreater(b["y"] + b["h"], 24)

            entry = self.undo_last(handle, doc)
            self.assertEqual(entry.nbytes, 10 * 8 * 4)
            node = self.node(doc, nid)
            self.assertEqual(read(node), before)
            self.assertEqual(bounds_of(node), before_bounds)

    def test_replace_by_path_and_negative_offset(self):
        data = opaque_pattern(5, 4)
        png = self.png("b.png", data, 5, 4)
        with scratch_document(32, 24, name="kmcp-import-neg") as doc:
            handle, nid = self.setup_doc(doc, fill=False)
            result = self.assertOk(run("import_png", doc=handle, id="/P", png=png, x=-3, y=-2))
            self.assertEqual(result["bounds"], {"x": -3, "y": -2, "w": 5, "h": 4})
            self.assertEqual(read(self.node(doc, nid), (-3, -2, 5, 4)), data)

    def test_clear_outside_leaves_only_the_png(self):
        data = opaque_pattern(4, 4, 1)
        png = self.png("c.png", data, 4, 4)
        with scratch_document(32, 24, name="kmcp-import-clear") as doc:
            handle, nid = self.setup_doc(doc, fill=False)
            node = self.node(doc, nid)
            self.assertTrue(node.setPixelData(solid(6, 6, (1, 2, 3, 255)), 2, 2, 6, 6))
            self.assertTrue(node.setPixelData(solid(5, 5, (4, 5, 6, 90)), 20, 15, 5, 5))
            settle(doc)
            before = read(node)
            result = self.assertOk(run("import_png", doc=handle, id=nid, png=png, x=10, y=10,
                                       clear_outside=True))
            node = self.node(doc, nid)
            self.assertEqual(result["changed"], {"x": 2, "y": 2, "w": 23, "h": 18})
            self.assertEqual(result["bounds"], {"x": 10, "y": 10, "w": 4, "h": 4})
            self.assertEqual(read(node), splice(bytes(len(before)), WIN, (10, 10, 4, 4), data))

            entry = self.undo_last(handle, doc)
            self.assertEqual(entry.nbytes, 23 * 18 * 4)  # one union rect: smaller than two
            self.assertEqual(read(self.node(doc, nid)), before)
            self.assertEqual(bounds_of(self.node(doc, nid)), {"x": 2, "y": 2, "w": 23, "h": 18})

    def test_clear_outside_far_apart_snapshots_two_rects(self):
        data = opaque_pattern(3, 3, 2)
        png = self.png("d.png", data, 3, 3)
        with scratch_document(32, 24, name="kmcp-import-far") as doc:
            handle, nid = self.setup_doc(doc, fill=False)
            node = self.node(doc, nid)
            self.assertTrue(node.setPixelData(solid(2, 2, (9, 9, 9, 255)), 0, 0, 2, 2))
            settle(doc)
            before = read(node)
            result = self.assertOk(run("import_png", doc=handle, id="P", png=png, x=29, y=21,
                                       clear_outside=True))
            self.assertEqual(result["changed"], {"x": 0, "y": 0, "w": 32, "h": 24})
            self.assertEqual(result["bounds"], {"x": 29, "y": 21, "w": 3, "h": 3})
            entry = self.undo_last(handle, doc)
            self.assertEqual(entry.nbytes, (2 * 2 + 3 * 3) * 4)
            self.assertEqual(read(self.node(doc, nid)), before)

    def test_clear_outside_on_empty_layer(self):
        data = opaque_pattern(3, 2)
        png = self.png("e.png", data, 3, 2)
        with scratch_document(32, 24, name="kmcp-import-empty") as doc:
            handle, nid = self.setup_doc(doc, fill=False)
            result = self.assertOk(run("import_png", doc=handle, id="P", png=png, x=1, y=1,
                                       clear_outside=True))
            self.assertEqual(result["changed"], {"x": 1, "y": 1, "w": 3, "h": 2})
            self.assertEqual(result["bounds"], {"x": 1, "y": 1, "w": 3, "h": 2})
            self.undo_last(handle, doc)
            self.assertIsNone(bounds_of(self.node(doc, nid)))

    def test_over_matches_composite_over(self):
        data = pattern(9, 6, 5)
        png = self.png("f.png", data, 9, 6)
        with scratch_document(32, 24, name="kmcp-import-over") as doc:
            handle, nid = self.setup_doc(doc)
            node = self.node(doc, nid)
            before = read(node)
            old_rect = read(node, (3, 4, 9, 6))
            result = self.assertOk(run("import_png", doc=handle, id="P", png=png, x=3, y=4,
                                       mode="over"))
            self.assertEqual(result["mode"], "over")
            self.assertEqual(result["changed"], {"x": 3, "y": 4, "w": 9, "h": 6})
            want = pixels.composite_over(old_rect, data, 9, 6)
            node = self.node(doc, nid)
            self.assertEqual(read(node, (3, 4, 9, 6)), want)
            self.assertEqual(read(node), splice(before, WIN, (3, 4, 9, 6), want))
            self.undo_last(handle, doc)
            self.assertEqual(read(self.node(doc, nid)), before)

    def test_dry_run_changes_nothing(self):
        png = self.png("g.png", pattern(4, 4), 4, 4)
        with scratch_document(32, 24, name="kmcp-import-dry") as doc:
            handle, nid = self.setup_doc(doc)
            before = read(self.node(doc, nid))
            result = self.assertOk(run("import_png", doc=handle, id="P", png=png, x=40, y=-3,
                                       clear_outside=True, dry_run=True))
            self.assertEqual(result["dry_run"], True)
            self.assertEqual(result["would_change"], {"x": 0, "y": -3, "w": 44, "h": 27})
            self.assertNotIn("changed", result)
            self.assertEqual(read(self.node(doc, nid)), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_over_journal_cap_warns_not_undoable(self):
        png = self.png("h.png", pattern(8, 8), 8, 8)
        with scratch_document(32, 24, name="kmcp-import-cap") as doc:
            with temp_config({"allowed_dirs": [self.root], "journal": {"max_bytes": 100}}):
                handle, nid = self.setup_doc(doc)
                batch.JOURNALS.forget(handle)  # a fresh journal with the small cap
                self.assertOk(run("update_layers", doc=handle,
                                  changes=[{"id": "P", "visible": True}]))
                self.assertEqual(len(batch.JOURNALS.get(handle)), 1)
                result = self.assertOk(run("import_png", doc=handle, id="P", png=png))
            self.assertEqual([w["code"] for w in result["warnings"]], ["not_undoable"])
            self.assertEqual(result["warnings"][0]["nbytes"], 8 * 8 * 4)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)  # cleared
            batch.JOURNALS.forget(handle)

    def test_mid_apply_failure_restores_the_layer(self):
        png = self.png("i.png", opaque_pattern(4, 4), 4, 4)
        with scratch_document(32, 24, name="kmcp-import-fail") as doc:
            handle, nid = self.setup_doc(doc)
            before = read(self.node(doc, nid))
            real = pixel_cmds._write
            calls = []

            def failing(node, data, rect, what):
                calls.append(rect)
                if len(calls) == 2:  # the PNG write, after clearing the old bounds
                    raise RuntimeError("boom")
                return real(node, data, rect, what)

            pixel_cmds._write = failing
            try:
                err = self.assertError(run("import_png", doc=handle, id="P", png=png, x=2, y=2,
                                           clear_outside=True), "krita_error")
            finally:
                pixel_cmds._write = real
            self.assertIn("boom", err["message"])
            self.assertEqual(err["details"]["applied"], 0)
            settle(doc)
            self.assertEqual(read(self.node(doc, nid)), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)


class ImportPngErrorsTest(ImportPngBase):
    def test_target_must_be_a_paint_layer(self):
        png = self.png("a.png", pattern(2, 2), 2, 2)
        with scratch_document(16, 16, name="kmcp-import-types") as doc:
            handle = registry.handle_for(doc)
            group = doc.createGroupLayer("G")
            doc.rootNode().addChildNode(group, None)
            paint = add_paint_layer(doc, "Q")
            mask = doc.createTransparencyMask("M")
            paint.addChildNode(mask, None)
            targets = {"G": "grouplayer", "Q/M": "transparencymask"}
            filt = Krita.instance().filter("invert")
            if filt is not None:
                layer = doc.createFilterLayer("F", filt, Selection())
                if layer is not None:
                    doc.rootNode().addChildNode(layer, None)
                    targets["F"] = "filterlayer"
            settle(doc)
            self.assertIn("F", targets)
            for ref, kind in targets.items():
                err = self.assertError(run("import_png", doc=handle, id=ref, png=png), "conflict")
                self.assertEqual(err["details"]["type"], kind, ref)
                self.assertIn(kind, err["message"])
            self.assertError(run("import_png", doc=handle, id="/", png=png), "bad_request")
            self.assertError(run("import_png", doc=handle, id="nope", png=png), "not_found")
            self.assertError(run("import_png", doc=handle, png=png), "bad_request")

    def test_non_rgba8_document(self):
        png = self.png("a.png", pattern(2, 2), 2, 2)
        with scratch_document(16, 16, name="kmcp-import-u16", color_depth="U16") as doc:
            handle = registry.handle_for(doc)
            add_paint_layer(doc, "P")
            err = self.assertError(run("import_png", doc=handle, id="P", png=png), "conflict")
            self.assertEqual((err["details"]["color_model"], err["details"]["color_depth"]),
                             ("RGBA", "U16"))

    def test_bad_paths_and_params(self):
        png = self.png("a.png", pattern(2, 2), 2, 2)
        outside = self.png("o.png", pattern(2, 2), 2, 2, where=self.outside)
        not_png = os.path.join(self.root, "fake.png")
        with open(not_png, "wb") as f:
            f.write(b"not a png at all, just text padding it out" * 2)
        with scratch_document(16, 16, name="kmcp-import-bad") as doc:
            handle, nid = self.setup_doc(doc)
            before = read(self.node(doc, nid))

            def err(code, **params):
                return self.assertError(run("import_png", doc=handle, **{"id": "P", **params}),
                                        code)

            err("forbidden_path", png=outside)
            err("forbidden_path", png=os.path.join(self.root, "a.txt"))
            err("not_found", png=os.path.join(self.root, "missing.png"))
            err("bad_request", png=not_png)
            err("bad_request")  # no png
            err("bad_request", png=png, mode="under")
            err("bad_request", png=png, mode="over", clear_outside=True)
            err("bad_request", png=png, clear_outside="yes")
            err("bad_request", png=png, x=1.5)
            err("bad_request", png=png, y=10 ** 7)
            err("bad_request", png=png, dry_run="no")
            with temp_config({"allowed_dirs": [self.root],
                              "limits": {"max_canvas_dim": 1, "max_canvas_pixels": 1}}):
                self.assertEqual(err("too_large", png=png)["details"]["limit"], "max_canvas_dim")
            self.assertEqual(read(self.node(doc, nid)), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)


if __name__ == "__main__":
    unittest.main()
