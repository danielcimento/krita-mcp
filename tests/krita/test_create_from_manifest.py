"""create_layers(manifest=…) (P2.4), headless, through dispatch.execute.

Manifests and PNGs are written into a temp dir that a temp config file
makes an allowed root (``CreateLayersBase``); a second temp dir stays
outside every root.
"""

import os

from helpers import scratch_document
from kritamcp import batch, manifest, nodes, registry
from test_create_layers import CreateLayersBase, _alpha_bounds, node_ids, write_png
from test_dispatch import run


def group(key, parent="/", **kw):
    return {"key": key, "name": kw.pop("name", key), "type": "group", "parent": parent, **kw}


def paint(key, parent="/", **kw):
    return {"key": key, "name": kw.pop("name", key), "type": "paint", "parent": parent, **kw}


class CreateFromManifestTest(CreateLayersBase):
    def setUp(self):
        super().setUp()
        self.dir = os.path.join(self.root, "export")
        os.mkdir(self.dir)

    def write_manifest(self, layers, document=None, name="manifest.json", where=None):
        path = os.path.join(where or self.dir, name)
        manifest.write(path, manifest.build(
            {"width": 16, "height": 16} if document is None else document, layers))
        return path

    def from_manifest(self, handle, path, **params):
        return run("create_layers", doc=handle, manifest=path, **params)

    def test_structure_props_and_pixels(self):
        a = write_png(os.path.join(self.dir, "0003_a.png"), 5, 4, seed=1)
        os.mkdir(os.path.join(self.root, "shared"))
        c = write_png(os.path.join(self.root, "shared", "c.png"), 3, 3, seed=2)
        write_png(os.path.join(self.dir, "0001_g.png"), 16, 16, seed=3)  # a projection
        path = self.write_manifest([
            group("g", pass_through=True, id="{old}", path="G", stack_index=0, name="G"),
            group("p", projection=True, png="0001_g.png", x=0, y=0, name="G"),
            paint("a", "$g", png="0003_a.png", x=3, y=-1, w=5, h=4, blend_mode="multiply",
                  opacity=0.5, name="A"),
            paint("e", "$g", png=None, x=0, y=0, name="Empty"),
            group("h", visible=False, name="Hidden"),
            # A relative path with "..": fine while it stays inside a root.
            paint("c", "$h", png="../shared/c.png", x=10, y=10, alpha_locked=True, name="C"),
        ])
        with scratch_document(16, 16, name="kmcp-manifest") as doc:
            handle = registry.handle_for(doc)
            result = self.assertOk(self.from_manifest(handle, path))
            self.assertEqual(set(result), {"doc", "dry_run", "created", "counts", "manifest",
                                           "skipped", "warnings"})
            self.assertEqual(result["manifest"], os.path.realpath(path))
            self.assertEqual(result["skipped"], [{"index": 1, "key": "p",
                                                  "reason": "group projection (export_layers "
                                                            "include_groups)"}])
            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["counts"], {"grouplayer": 2, "paintlayer": 3, "total": 5})
            self.assertEqual([(c["key"], c["path"]) for c in result["created"]],
                             [("g", "G"), ("a", "G/A"), ("e", "G/Empty"), ("h", "Hidden"),
                              ("c", "Hidden/C")])
            ids = {c["key"]: c["id"] for c in result["created"]}
            self.assertNotIn("{old}", ids.values())

            by_id, flat = self.listing(handle)
            self.assertEqual([layer["path"] for layer in flat],
                             ["Hidden", "Hidden/C", "G", "G/Empty", "G/A", "Background"])
            g, la, e, h, lc = (by_id[ids[k]] for k in "gaehc")
            self.assertIs(g["pass_through"], True)
            self.assertEqual((la["blend_mode"], la["opacity"], la["bounds"]),
                             ("multiply", 0.502, _alpha_bounds(a, 5, 4, 3, -1)))
            self.assertIsNone(e["bounds"])
            self.assertIs(h["visible"], False)
            self.assertIs(lc["alpha_locked"], True)
            self.assertEqual(bytes(nodes.node_by_id(doc, ids["a"]).pixelData(3, -1, 5, 4)), a)
            self.assertEqual(bytes(nodes.node_by_id(doc, ids["c"]).pixelData(10, 10, 3, 3)), c)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 1)

    def test_document_size_warning(self):
        write_png(os.path.join(self.dir, "a.png"), 2, 2)
        path = self.write_manifest([paint("a", png="a.png")],
                                   document={"name": "big", "width": 32, "height": 16})
        with scratch_document(16, 16, name="kmcp-manifest-size") as doc:
            handle = registry.handle_for(doc)
            result = self.assertOk(self.from_manifest(handle, path))
            self.assertEqual(result["warnings"], [{
                "code": "document_size",
                "message": "The manifest was made for a 32x16 document; this one is 16x16. "
                           "Layers keep their manifest offsets.",
                "manifest_size": {"w": 32, "h": 16}, "doc_size": {"w": 16, "h": 16}}])
            # No (or a non-numeric) size: no warning.
            for document in ({}, {"width": "32", "height": 16}):
                path = self.write_manifest([paint("a", png="a.png")], document=document)
                self.assertEqual(self.assertOk(self.from_manifest(handle, path))["warnings"], [])

    def test_dry_run(self):
        write_png(os.path.join(self.dir, "a.png"), 3, 2)
        path = self.write_manifest([group("p", projection=True), group("g"),
                                    paint("a", "$g", png="a.png", x=1, y=2)],
                                   document={"width": 8, "height": 8})
        with scratch_document(16, 16, name="kmcp-manifest-dry") as doc:
            handle = registry.handle_for(doc)
            before = node_ids(doc)
            result = self.assertOk(self.from_manifest(handle, path, dry_run=True))
            self.assertEqual(result, {
                "doc": handle, "dry_run": True,
                "would_create": [
                    {"key": "g", "name": "g", "type": "grouplayer", "parent": "/",
                     "above": None},
                    {"key": "a", "name": "a", "type": "paintlayer", "parent": "$g",
                     "above": None, "png": os.path.realpath(os.path.join(self.dir, "a.png")),
                     "x": 1, "y": 2, "w": 3, "h": 2}],
                "counts": {"grouplayer": 1, "paintlayer": 1, "total": 2},
                "manifest": os.path.realpath(path),
                "skipped": [{"index": 0, "key": "p",
                             "reason": "group projection (export_layers include_groups)"}],
                "warnings": [{"code": "document_size",
                              "message": "The manifest was made for a 8x8 document; this one "
                                         "is 16x16. Layers keep their manifest offsets.",
                              "manifest_size": {"w": 8, "h": 8},
                              "doc_size": {"w": 16, "h": 16}}]})
            self.assertEqual(node_ids(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_errors_point_at_the_manifest_entry(self):
        outside_png = os.path.join(self.outside, "x.png")
        write_png(outside_png, 2, 2)
        write_png(os.path.join(self.dir, "ok.png"), 2, 2)
        escape = os.path.relpath(outside_png, self.dir)
        self.assertTrue(escape.startswith(".."), escape)
        cases = [
            # (label, entry at manifest index 3, code)
            ("blend mode", paint("b", "$g", blend_mode="nope"), "bad_request"),
            ("png escapes the roots", paint("b", png=escape), "forbidden_path"),
            ("png missing", paint("b", png="missing.png"), "not_found"),
            ("png on a group", group("b", png="ok.png"), "bad_request"),
            ("bad key for create_layers", paint("b$", png="ok.png"), "bad_request"),
        ]
        with scratch_document(16, 16, name="kmcp-manifest-errors") as doc:
            handle = registry.handle_for(doc)
            before = node_ids(doc)
            for label, entry, code in cases:
                with self.subTest(label):
                    # Index 1 is a skipped projection: entry 3 is spec 2.
                    path = self.write_manifest([group("g"), group("p", projection=True),
                                                paint("a", "$g"), entry])
                    err = self.assertError(self.from_manifest(handle, path), code)
                    self.assertEqual(err["details"]["index"], 3, err)
                    self.assertEqual(err["details"]["field"], "layers", err)
                    self.assertTrue(err["message"].startswith("layers[3]: "), err)
                    self.assertEqual(node_ids(doc), before)
            # A structure error (manifest.to_specs) has the same shape.
            path = self.write_manifest([group("g"), paint("a", parent="$nope")])
            err = self.assertError(self.from_manifest(handle, path), "bad_request")
            self.assertEqual((err["details"]["index"], err["details"]["field"],
                              err["details"]["param"]), (1, "layers", "parent"))
            self.assertTrue(err["message"].startswith("layers[1]: "), err)

    def test_apply_failure_points_at_the_manifest_entry(self):
        # The header check passes; decoding entry 2 (spec 1) fails mid-apply.
        cut = os.path.join(self.dir, "cut.png")
        write_png(cut, 8, 8)
        with open(cut, "r+b") as f:
            f.truncate(40)
        path = self.write_manifest([paint("a"), group("p", projection=True),
                                    paint("b", png="cut.png")])
        with scratch_document(16, 16, name="kmcp-manifest-apply") as doc:
            handle = registry.handle_for(doc)
            before = node_ids(doc)
            err = self.assertError(self.from_manifest(handle, path), "bad_request")
            self.assertEqual((err["details"]["index"], err["details"]["field"],
                              err["details"]["applied"], err["details"]["rolled_back"]),
                             (2, "layers", 1, 1))
            self.assertIn("layers[2] failed", err["message"])
            self.assertEqual(node_ids(doc), before)

    def test_manifest_path_rules(self):
        inside = self.write_manifest([paint("a")])
        outside = self.write_manifest([paint("a")], where=self.outside)
        txt = os.path.join(self.dir, "manifest.txt")
        os.rename(self.write_manifest([paint("a")], name="m2.json"), txt)
        png = os.path.join(self.dir, "a.png")
        write_png(png, 2, 2)
        only_skipped = self.write_manifest([group("p", projection=True)], name="skipped.json")
        with scratch_document(16, 16, name="kmcp-manifest-paths") as doc:
            handle = registry.handle_for(doc)
            before = node_ids(doc)
            self.assertError(self.from_manifest(handle, outside), "forbidden_path")
            self.assertError(self.from_manifest(handle, txt), "forbidden_path")
            self.assertError(self.from_manifest(handle, os.path.join(self.dir, "nope.json")),
                             "not_found")
            err = self.assertError(self.from_manifest(handle, png), "bad_request")
            self.assertIn(".json", err["message"])
            err = self.assertError(self.from_manifest(handle, only_skipped), "bad_request")
            self.assertIn("no layers to create", err["message"])
            with open(inside, "w") as f:
                f.write('{"version": 1, "layers": [')
            err = self.assertError(self.from_manifest(handle, inside), "bad_request")
            self.assertIn("not valid JSON", err["message"])
            # Exactly one of specs and manifest.
            for params in ({}, {"specs": [{"name": "x"}], "manifest": inside}):
                err = self.assertError(run("create_layers", doc=handle, **params), "bad_request")
                self.assertIn("exactly one", err["message"])
            self.assertEqual(node_ids(doc), before)

