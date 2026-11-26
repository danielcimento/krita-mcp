"""nodes.py: tree walk, index, ref resolution and the layer object, headless."""

import unittest

from krita import Selection

from helpers import scratch_document, settle
from kritamcp import nodes, refs
from kritamcp.errors import CommandError


def _build(doc):
    """Top to bottom:

        [Selection Mask]       (the global selection, skipped)
        Top                    paint, empty, 50 % opacity, hidden, multiply, locked
        G1                     group
          Eye                  paint (duplicate name), alpha locked
          G2                   group, pass-through
            a/b                paint, inherit alpha
            Eye                paint, pixels at (3, 4, 2, 2)
        Background             paint, from createDocument
    """
    root = doc.rootNode()
    g1 = doc.createGroupLayer("G1")
    root.addChildNode(g1, None)
    top = doc.createNode("Top", "paintlayer")
    root.addChildNode(top, None)
    g2 = doc.createGroupLayer("G2")
    g1.addChildNode(g2, None)
    eye_outer = doc.createNode("Eye", "paintlayer")
    g1.addChildNode(eye_outer, None)
    eye_inner = doc.createNode("Eye", "paintlayer")
    g2.addChildNode(eye_inner, None)
    slash = doc.createNode("a/b", "paintlayer")
    g2.addChildNode(slash, None)

    eye_inner.setPixelData(bytes((1, 2, 3, 255)) * 4, 3, 4, 2, 2)
    top.setOpacity(128)
    top.setVisible(False)
    top.setBlendingMode("multiply")
    top.setLocked(True)
    eye_outer.setAlphaLocked(True)
    slash.setInheritAlpha(True)
    g2.setPassThroughMode(True)

    sel = Selection()
    sel.select(0, 0, 5, 5, 255)
    doc.setSelection(sel)
    settle(doc)
    background = [c for c in root.childNodes() if c.name() == "Background"][0]
    return {"top": top, "g1": g1, "eye_outer": eye_outer, "g2": g2, "slash": slash,
            "eye_inner": eye_inner, "background": background}


ORDER = ["top", "g1", "eye_outer", "g2", "slash", "eye_inner", "background"]


class NodesTest(unittest.TestCase):
    def test_walk_index_layer_object_resolve(self):
        with scratch_document(20, 10, name="kmcp-nodes") as doc:
            n = _build(doc)
            ids = {k: nodes.node_id(v) for k, v in n.items()}
            for nid in ids.values():
                self.assertEqual(refs.normalize_uuid(nid), nid)  # braced lowercase

            # The selection mask is there, on top, but not walked.
            raw = [(c.name(), c.type()) for c in doc.rootNode().childNodes()]
            self.assertEqual(raw[-1], ("Selection Mask", "selectionmask"))
            self.assertEqual(len(raw), 4)

            walked = list(nodes.walk(doc))
            self.assertEqual([e["id"] for _, e in walked], [ids[k] for k in ORDER])
            for node, entry in walked:
                self.assertEqual(nodes.node_id(node), entry["id"])
                self.assertEqual(node.name(), entry["name"])
                self.assertEqual(node.type(), entry["type"])
            parents = {e["id"]: e["parent"] for _, e in walked}
            self.assertIsNone(parents[ids["top"]])
            self.assertIsNone(parents[ids["background"]])
            self.assertEqual(parents[ids["g2"]], ids["g1"])
            self.assertEqual(parents[ids["eye_inner"]], ids["g2"])

            index = nodes.build_index(doc)
            self.assertEqual(list(index), [ids[k] for k in ORDER])
            paths = {k: index.path_of(ids[k]) for k in ORDER}
            self.assertEqual(paths, {
                "top": "Top", "g1": "G1", "eye_outer": "G1/Eye", "g2": "G1/G2",
                "slash": "G1/G2/a\\/b", "eye_inner": "G1/G2/Eye", "background": "Background",
            })

            # Layer object, all fields.
            top = nodes.layer_object(n["top"], index)
            self.assertEqual(list(top), list(nodes.LAYER_FIELDS))
            self.assertEqual(top, {
                "id": ids["top"], "name": "Top", "path": "Top", "type": "paintlayer",
                "parent": None, "index": 2, "visible": False, "opacity": 0.502,
                "blend_mode": "multiply", "locked": True, "alpha_locked": False,
                "inherit_alpha": False, "pass_through": None, "has_layer_style": False,
                "bounds": None, "child_count": 0, "children": [],
            })
            g1 = nodes.layer_object(n["g1"], index)
            self.assertEqual(g1["type"], "grouplayer")
            self.assertIs(g1["pass_through"], False)
            self.assertEqual(g1["children"], [ids["eye_outer"], ids["g2"]])  # top to bottom
            self.assertEqual(g1["index"], 1)
            self.assertEqual(g1["opacity"], 1.0)
            self.assertEqual(g1["bounds"], {"x": 3, "y": 4, "w": 2, "h": 2})  # union of children
            g2 = nodes.layer_object(n["g2"], index)
            self.assertIs(g2["pass_through"], True)
            self.assertEqual(g2["parent"], ids["g1"])
            self.assertEqual(g2["children"], [ids["slash"], ids["eye_inner"]])
            self.assertEqual(g2["index"], 0)  # bottom of G1
            inner = nodes.layer_object(n["eye_inner"], index)
            self.assertEqual(inner["bounds"], {"x": 3, "y": 4, "w": 2, "h": 2})
            self.assertEqual(inner["path"], "G1/G2/Eye")
            self.assertEqual(inner["index"], 0)
            self.assertIs(nodes.layer_object(n["eye_outer"], index)["alpha_locked"], True)
            slash = nodes.layer_object(n["slash"], index)
            self.assertIs(slash["inherit_alpha"], True)
            self.assertEqual(slash["name"], "a/b")
            bg = nodes.layer_object(n["background"], index)
            self.assertEqual(bg["bounds"], {"x": 0, "y": 0, "w": 20, "h": 10})
            self.assertEqual((bg["index"], bg["parent"]), (0, None))

            # Field subsets: id, name, child_count always included (F005 K12).
            self.assertEqual(nodes.layer_object(n["g1"], index, ["opacity"]),
                             {"id": ids["g1"], "name": "G1", "opacity": 1.0, "child_count": 2})
            self.assertEqual(list(nodes.layer_object(n["top"], index, ["bounds", "id"])),
                             ["id", "name", "bounds", "child_count"])
            self.assertEqual(nodes.layer_object(n["g1"], index, ["children"])["children"],
                             [ids["eye_outer"], ids["g2"]])
            for bad in (["nope"], "opacity", [1]):
                with self.assertRaises(CommandError) as cm:
                    nodes.layer_object(n["top"], index, bad)
                self.assertEqual(cm.exception.code, "bad_request")

            # Resolve.
            def same(ref, key, **kw):
                got = nodes.resolve_node(doc, ref, index, **kw)
                self.assertTrue(got == n[key], ref)

            braced = ids["eye_inner"]
            same(braced, "eye_inner")
            same(braced[1:-1], "eye_inner")
            same(braced.upper(), "eye_inner")
            same(braced[1:-1].upper(), "eye_inner")
            same("G1/G2/Eye", "eye_inner")
            same("/G1/Eye", "eye_outer")
            same("G1/G2/a\\/b", "slash")
            same("a\\/b", "slash")
            same("Top", "top")
            same("/Top", "top")
            same("G2", "g2")
            self.assertTrue(nodes.resolve_node(doc, "/", allow_root=True) == doc.rootNode())
            same("Background", "background")  # index built on demand

            def code(ref, **kw):
                with self.assertRaises(CommandError) as cm:
                    nodes.resolve_node(doc, ref, index, **kw)
                return cm.exception

            err = code("Eye")
            self.assertEqual(err.code, "ambiguous")
            self.assertEqual(err.details["matches"], [ids["eye_outer"], ids["eye_inner"]])
            self.assertEqual(code("a/b").code, "not_found")  # a path; hint in the message
            self.assertIn("a\\/b", code("a/b").message)
            self.assertEqual(code("Selection Mask").code, "not_found")
            self.assertEqual(code("{00000000-0000-0000-0000-000000000000}").code, "not_found")
            self.assertEqual(code("/").code, "bad_request")
            self.assertEqual(code("G1//Eye").code, "bad_request")

            self.assertIsNone(nodes.node_by_id(doc, "{00000000-0000-0000-0000-000000000001}"))
            self.assertTrue(nodes.node_by_id(doc, braced[1:-1]) == n["eye_inner"])

    def test_empty_document_tree(self):
        with scratch_document(8, 8, name="kmcp-nodes-empty") as doc:
            index = nodes.build_index(doc)
            self.assertEqual(len(index), 1)  # Background
            (bg_id,) = list(index)
            bg = nodes.node_by_id(doc, bg_id)
            self.assertEqual(nodes.layer_object(bg, index, ["path"])["path"], "Background")
