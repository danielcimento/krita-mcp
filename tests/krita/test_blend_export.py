"""Additive blend conversion on export (P2.7, D17), headless, through dispatch.

``save`` to ``.psd`` writes ``add`` as ``linear_dodge``, to ``.ora``
``linear_dodge`` as ``add``, on a clone; ``.kra`` and other formats are
never converted. Every call passes ``doc``; every document is closed in
``tearDown`` (``DocumentsBase``) or in ``finally``, and never touched after.
"""

import os
import shutil
import zipfile

from krita import Document, Krita

from helpers import settle
from kritamcp import fileio, nodes, registry
from test_documents import DocumentsBase
from test_dispatch import run

W, H = 16, 12

# The tree ``build`` makes, top to bottom, with each layer's blend mode:
#
#   G   group, add
#     GA  add
#     GN  normal
#   LD  linear_dodge
#   AD  add
#   MU  multiply
#   NO  normal
#   Background (new_canvas, #1a1a2e)
SPECS = [
    {"name": "NO"},
    {"name": "MU", "blend_mode": "multiply"},
    {"name": "AD", "blend_mode": "add"},
    {"name": "LD", "blend_mode": "linear_dodge"},
    {"key": "g", "name": "G", "type": "group", "blend_mode": "add"},
    {"name": "GN", "parent": "$g"},
    {"name": "GA", "parent": "$g", "blend_mode": "add"},
]
MODES = {"G": "add", "GA": "add", "GN": "normal", "LD": "linear_dodge", "AD": "add",
         "MU": "multiply", "NO": "normal", "Background": "normal"}


def pattern(w, h, seed):
    """BGRA bytes with varied color and alpha (some fully transparent)."""
    return bytes((i * 37 + seed * 59 + (i // 4) * 11) % 256 for i in range(4 * w * h))


def modes(doc):
    return {e["name"]: n.blendingMode() for n, e in nodes.walk(doc)}


def projection(doc):
    settle(doc)
    img = doc.projection(0, 0, doc.width(), doc.height())
    return bytes(img.constBits().asstring(img.sizeInBytes()))


class _CloneSpy:
    """Counts ``Document.clone`` calls for the ``with`` block."""

    def __enter__(self):
        self.calls = 0
        # The class's own entry: restoring ``Document.clone`` as read
        # through the class leaves an unbound method that no longer binds.
        self._orig = Document.__dict__["clone"]
        call = Document.clone
        spy = self

        def clone(doc):
            spy.calls += 1
            return call(doc)

        Document.clone = clone
        return self

    def __exit__(self, *exc):
        Document.clone = self._orig
        return False


class BlendExportTest(DocumentsBase):
    def build(self, specs=SPECS):
        """A new canvas with ``specs`` (pixels in every paint layer); returns
        ``(handle, {name: id})``."""
        handle = self.new(width=W, height=H)["doc"]
        self.assertOk(run("create_layers", doc=handle, specs=specs))
        doc = registry.lookup(handle)
        ids = {}
        for seed, (node, entry) in enumerate(nodes.walk(doc)):
            ids[entry["name"]] = entry["id"]
            if entry["type"] == "paintlayer" and entry["name"] != "Background":
                node.setPixelData(pattern(W - 4, H - 4, seed), seed % 4, seed % 3, W - 4, H - 4)
        settle(doc)
        doc.setModified(False)
        return handle, ids

    def reopen(self, path):
        """The modes of a copy of ``path``, opened and closed again (the copy
        so a document that has ``path`` open doesn't matter), and its
        projection."""
        copy = os.path.join(self.out, "reopen-" + os.path.basename(path))
        shutil.copyfile(path, copy)
        doc = fileio.open_document(copy)
        try:
            return modes(doc), projection(doc)
        finally:
            doc.setModified(False)
            doc.close()
            del doc

    def test_psd_writes_add_as_linear_dodge(self):
        handle, ids = self.build()
        doc = registry.lookup(handle)
        expected_px = projection(doc)
        # A saved, then modified, document: the export leaves both alone.
        kra = self.assertOk(run("save", doc=handle, path="orig.kra"))["path"]
        doc.setModified(True)
        file_name = doc.fileName()
        count = len(Krita.instance().documents())
        del doc

        with _CloneSpy() as spy:
            result = self.assertOk(run("save", doc=handle, path="a.psd"))
        self.assertEqual(spy.calls, 1)
        self.assertEqual(result["blend_conversions"], [
            {"id": ids["G"], "path": "G", "from": "add", "to": "linear_dodge"},
            {"id": ids["GA"], "path": "G/GA", "from": "add", "to": "linear_dodge"},
            {"id": ids["AD"], "path": "AD", "from": "add", "to": "linear_dodge"},
        ])
        self.assertFalse(result["doc_path_changed"])
        self.assertEqual(len(Krita.instance().documents()), count)  # the clone is closed

        doc = registry.lookup(handle)
        self.assertEqual(modes(doc), MODES)
        self.assertEqual(doc.fileName(), file_name)
        self.assertEqual(os.path.realpath(file_name), kra)
        self.assertTrue(doc.modified())
        # The premise: Krita's own export of these modes loses Add.
        raw = os.path.join(self.out, "raw.psd")
        fileio.export_image(doc, raw)
        del doc
        raw_modes, raw_px = self.reopen(raw)
        self.assertEqual({n: raw_modes[n] for n in ("G", "GA", "AD", "LD")},
                         {"G": "normal", "GA": "normal", "AD": "normal", "LD": "linear_dodge"})
        self.assertNotEqual(raw_px, expected_px)

        got, px = self.reopen(result["path"])
        self.assertEqual(got, {**MODES, "G": "linear_dodge", "GA": "linear_dodge",
                               "AD": "linear_dodge"})
        self.assertEqual(px, expected_px)  # identical rendering (D17)

    def test_ora_writes_linear_dodge_as_add(self):
        handle, ids = self.build()
        doc = registry.lookup(handle)
        expected_px = projection(doc)
        del doc
        count = len(Krita.instance().documents())

        result = self.assertOk(run("save", doc=handle, path="a.ora"))
        self.assertEqual(result["blend_conversions"], [
            {"id": ids["LD"], "path": "LD", "from": "linear_dodge", "to": "add"}])
        self.assertEqual(len(Krita.instance().documents()), count)
        doc = registry.lookup(handle)
        self.assertEqual(modes(doc), MODES)
        self.assertEqual(doc.fileName(), "")
        self.assertFalse(doc.modified())
        del doc

        with zipfile.ZipFile(result["path"]) as z:
            stack = z.read("stack.xml").decode()
        self.assertNotIn("krita:linear_dodge", stack)
        self.assertEqual(stack.count('composite-op="svg:plus"'), 4)  # G, GA, LD, AD

        got, px = self.reopen(result["path"])
        self.assertEqual(got, {**MODES, "LD": "add"})
        self.assertEqual(px, expected_px)

    def test_kra_and_png_are_never_converted(self):
        handle, _ = self.build()
        with _CloneSpy() as spy:
            png = self.assertOk(run("save", doc=handle, path="a.png"))
            kra = self.assertOk(run("save", doc=handle, path="a.kra"))
            in_place = self.assertOk(run("save", doc=handle))
        self.assertEqual(spy.calls, 0)
        for result in (png, kra, in_place):
            self.assertEqual(result["blend_conversions"], [])
        self.assertEqual(modes(registry.lookup(handle)), MODES)
        got, _ = self.reopen(kra["path"])
        self.assertEqual(got, MODES)

    def test_nothing_to_convert_makes_no_clone(self):
        handle, _ = self.build([{"name": "MU", "blend_mode": "multiply"},
                                {"name": "LD", "blend_mode": "linear_dodge"}])
        with _CloneSpy() as spy:
            result = self.assertOk(run("save", doc=handle, path="plain.psd"))
        self.assertEqual(spy.calls, 0)
        self.assertEqual(result["blend_conversions"], [])
        got, _ = self.reopen(result["path"])
        self.assertEqual(got, {"MU": "multiply", "LD": "linear_dodge", "Background": "normal"})

        handle, _ = self.build([{"name": "AD", "blend_mode": "add"}])
        with _CloneSpy() as spy:
            result = self.assertOk(run("save", doc=handle, path="plain.ora"))
        self.assertEqual(spy.calls, 0)
        self.assertEqual(result["blend_conversions"], [])

    def test_in_place_psd_save_converts(self):
        handle, _ = self.build([{"name": "A"}, {"name": "B"}])
        psd = self.assertOk(run("save", doc=handle, path="inplace.psd"))["path"]
        opened = self.open(psd)["doc"]
        self.assertOk(run("update_layers", doc=opened, changes=[{"id": "A", "blend_mode": "add"}]))
        doc = registry.lookup(opened)
        doc.setModified(True)  # as after an edit by hand
        file_name = doc.fileName()
        a_id = nodes.node_id(nodes.resolve_node(doc, "A"))
        expected_px = projection(doc)
        del doc

        with _CloneSpy() as spy:
            result = self.assertOk(run("save", doc=opened))
        self.assertEqual(spy.calls, 1)
        self.assertEqual(result["path"], psd)
        self.assertFalse(result["doc_path_changed"])
        self.assertEqual(result["blend_conversions"],
                         [{"id": a_id, "path": "A", "from": "add", "to": "linear_dodge"}])
        doc = registry.lookup(opened)
        self.assertEqual(modes(doc)["A"], "add")  # the open document keeps its spelling
        self.assertFalse(doc.modified())
        self.assertEqual(doc.fileName(), file_name)
        del doc

        got, px = self.reopen(psd)
        self.assertEqual(got["A"], "linear_dodge")
        self.assertEqual(px, expected_px)
