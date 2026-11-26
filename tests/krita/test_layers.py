"""commands/layers.py: list_layers, headless, through dispatch.execute."""

import unittest

from krita import Selection

from helpers import scratch_document, settle
from kritamcp import nodes, registry
from test_dispatch import EnvelopeAssertions, run


def _build(doc):
    """Top to bottom, on a 20x10 canvas:

        [Selection Mask]       the global selection (never listed)
        Off                    paint, pixels at (-3,-2,4,4) and (18,8,4,4): past the canvas
        A                      group
          Dup                  paint, multiply, opacity 128
          B                    group, hidden
            C                  group
              Dup              paint, empty
              Deep             paint, pixels at (2,3,2,2)
            Mid                paint
        Background             paint, from createDocument
    """
    root = doc.rootNode()

    def add(parent, name, kind="paintlayer"):
        node = doc.createGroupLayer(name) if kind == "grouplayer" else doc.createNode(name, kind)
        parent.addChildNode(node, None)  # on top
        return node

    a = add(root, "A", "grouplayer")
    off = add(root, "Off")
    b = add(a, "B", "grouplayer")
    dup_a = add(a, "Dup")
    mid = add(b, "Mid")
    c = add(b, "C", "grouplayer")
    deep = add(c, "Deep")
    dup_c = add(c, "Dup")

    pixel = bytes((10, 20, 30, 255))
    off.setPixelData(pixel * 16, -3, -2, 4, 4)
    off.setPixelData(pixel * 16, 18, 8, 4, 4)
    deep.setPixelData(pixel * 4, 2, 3, 2, 2)
    dup_a.setBlendingMode("multiply")
    dup_a.setOpacity(128)
    b.setVisible(False)

    sel = Selection()
    sel.select(0, 0, 5, 5, 255)
    doc.setSelection(sel)
    settle(doc)
    background = [n for n in root.childNodes() if n.name() == "Background"][0]
    found = {"off": off, "a": a, "dup_a": dup_a, "b": b, "c": c, "dup_c": dup_c,
             "deep": deep, "mid": mid, "background": background}
    return {k: nodes.node_id(v) for k, v in found.items()}


ORDER = ["off", "a", "dup_a", "b", "c", "dup_c", "deep", "mid", "background"]


class ListLayersTest(EnvelopeAssertions, unittest.TestCase):
    def ids_of(self, result):
        return [layer["id"] for layer in result["layers"]]

    def test_list_layers(self):
        with scratch_document(20, 10, name="kmcp-layers") as doc:
            ids = _build(doc)
            handle = registry.handle_for(doc)
            key = {v: k for k, v in ids.items()}

            def ls(**params):
                return self.assertOk(run("list_layers", doc=handle, **params))

            # Whole document: flat, depth-first, top to bottom; no selection mask.
            full = ls()
            self.assertEqual(set(full), {"doc", "root", "layers", "counts"})
            self.assertEqual((full["doc"], full["root"]), (handle, "/"))
            self.assertEqual(self.ids_of(full), [ids[k] for k in ORDER])
            self.assertNotIn("Selection Mask", [layer["name"] for layer in full["layers"]])
            self.assertEqual(full["counts"], {"grouplayer": 3, "paintlayer": 6, "total": 9})
            by = {key[layer["id"]]: layer for layer in full["layers"]}
            for layer in full["layers"]:
                self.assertEqual(list(layer), list(nodes.LAYER_FIELDS))

            self.assertEqual(by["a"]["children"], [ids["dup_a"], ids["b"]])  # top to bottom
            self.assertEqual(by["b"]["children"], [ids["c"], ids["mid"]])
            self.assertEqual(by["c"]["children"], [ids["dup_c"], ids["deep"]])
            self.assertEqual(by["deep"]["children"], [])
            self.assertEqual({k: by[k]["parent"] for k in ORDER}, {
                "off": None, "a": None, "dup_a": ids["a"], "b": ids["a"], "c": ids["b"],
                "dup_c": ids["c"], "deep": ids["c"], "mid": ids["b"], "background": None})
            # index counts from the bottom within the parent.
            self.assertEqual({k: by[k]["index"] for k in ORDER}, {
                "off": 2, "a": 1, "dup_a": 1, "b": 0, "c": 1, "dup_c": 1, "deep": 0,
                "mid": 0, "background": 0})
            self.assertEqual(by["deep"]["path"], "A/B/C/Deep")
            self.assertEqual(by["dup_c"]["path"], "A/B/C/Dup")

            self.assertEqual((by["dup_a"]["blend_mode"], by["dup_a"]["opacity"]),
                             ("multiply", 0.502))
            self.assertIs(by["b"]["visible"], False)
            self.assertIs(by["deep"]["visible"], True)  # its own flag, not the effective one
            self.assertIs(by["a"]["pass_through"], False)
            self.assertIsNone(by["dup_a"]["pass_through"])
            self.assertIsNone(by["dup_c"]["bounds"])  # empty
            self.assertEqual(by["deep"]["bounds"], {"x": 2, "y": 3, "w": 2, "h": 2})
            # Exact and not clipped to the 20x10 canvas.
            self.assertEqual(by["off"]["bounds"], {"x": -3, "y": -2, "w": 25, "h": 14})
            self.assertEqual(by["background"]["bounds"], {"x": 0, "y": 0, "w": 20, "h": 10})

            # depth: 1 = the root's direct children; cut-off nodes keep their children ids.
            top = ls(depth=1)
            self.assertEqual(self.ids_of(top), [ids[k] for k in ("off", "a", "background")])
            self.assertEqual(top["layers"][1]["children"], [ids["dup_a"], ids["b"]])
            self.assertEqual(top["counts"], {"grouplayer": 1, "paintlayer": 2, "total": 3})
            self.assertEqual(self.ids_of(ls(depth=2)),
                             [ids[k] for k in ("off", "a", "dup_a", "b", "background")])
            self.assertEqual(self.ids_of(ls(depth=100)), self.ids_of(full))
            self.assertEqual(self.ids_of(ls(root="/")), self.ids_of(full))

            # A layer root lists itself first, then its subtree.
            want_b = [ids[k] for k in ("b", "c", "dup_c", "deep", "mid")]
            for ref in (ids["b"], ids["b"][1:-1].upper(), "A/B", "/A/B", "B"):
                sub = ls(root=ref)
                self.assertEqual(sub["root"], ids["b"], ref)
                self.assertEqual(self.ids_of(sub), want_b, ref)
            sub = ls(root="B")
            self.assertEqual(sub["counts"], {"grouplayer": 2, "paintlayer": 3, "total": 5})
            self.assertEqual(sub["layers"][0], by["b"])  # same object as in the full listing
            self.assertEqual(self.ids_of(ls(root="B", depth=1)),
                             [ids[k] for k in ("b", "c", "mid")])
            leaf = ls(root="Deep")
            self.assertEqual(self.ids_of(leaf), [ids["deep"]])
            self.assertEqual(leaf["counts"], {"paintlayer": 1, "total": 1})
            self.assertEqual(self.ids_of(ls(root="A/B/C/Dup")), [ids["dup_c"]])

            # fields: id, name and child_count always included, in layer-object
            # order; children only when asked for (F005 K12).
            slim = ls(fields=["bounds", "type"])
            for layer in slim["layers"]:
                self.assertEqual(list(layer), ["id", "name", "type", "bounds", "child_count"])
            self.assertEqual(slim["layers"][0]["bounds"], by["off"]["bounds"])
            bare = ls(fields=[], depth=1)
            self.assertEqual(bare["layers"][1], {"id": ids["a"], "name": "A", "child_count": 2})
            kids = ls(fields=["children"], depth=1)
            self.assertEqual(kids["layers"][1], {"id": ids["a"], "name": "A", "child_count": 2,
                                                 "children": [ids["dup_a"], ids["b"]]})

            # Errors.
            err = self.assertError(run("list_layers", doc=handle, root="Dup"), "ambiguous")
            self.assertEqual(err["details"]["matches"], [ids["dup_a"], ids["dup_c"]])
            self.assertError(run("list_layers", doc=handle, root="Nope"), "not_found")
            self.assertError(run("list_layers", doc=handle, root="Selection Mask"), "not_found")
            self.assertError(run("list_layers", doc=handle,
                                 root="{00000000-0000-0000-0000-000000000000}"), "not_found")
            for bad in ("", 3, "A//B", "$key"):
                self.assertError(run("list_layers", doc=handle, root=bad), "bad_request")
            for bad in (0, -1, 1.5, 2.0, "2", True):
                self.assertError(run("list_layers", doc=handle, depth=bad), "bad_request")
            for bad in (["nope"], "bounds", [1], {"bounds": 1}):
                self.assertError(run("list_layers", doc=handle, fields=bad), "bad_request")
            self.assertError(run("list_layers", doc="doc-999999"), "not_found")
            if registry.active_live() is None:
                self.assertError(run("list_layers"), "not_found")  # headless: no active doc

    def test_empty_document(self):
        with scratch_document(8, 8, name="kmcp-layers-empty") as doc:
            doc.rootNode().childNodes()[0].remove()  # Background
            settle(doc)
            handle = registry.handle_for(doc)
            result = self.assertOk(run("list_layers", doc=handle))
            self.assertEqual((result["layers"], result["counts"]), ([], {"total": 0}))
            # fields are still checked when nothing is listed.
            self.assertError(run("list_layers", doc=handle, fields=["nope"]), "bad_request")


if __name__ == "__main__":
    unittest.main()
