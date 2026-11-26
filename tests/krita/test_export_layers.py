"""commands/pixels.py: export_layers, headless, through dispatch.execute.

Every call passes ``doc`` and every document is closed in ``finally``
(``scratch_document``, or ``close_document`` for new_canvas documents).
Exports go into a temp dir made an allowed root through a temp config
file; a second temp dir stays outside every root.

The round trip (acceptance § 2, small variant, headless): export a
synthetic document, rebuild it with create_layers(manifest=…) on a new
transparent canvas, and compare the two documents' projections byte for
byte and their layer trees.
"""

import contextlib
import os
import shutil
import tempfile
import unittest

from krita import Krita, Selection

from helpers import scratch_document, settle, temp_config
from kritamcp import manifest, nodes, pixels, registry
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run
from test_pixels import pattern

W, H = 40, 30
# A window larger than the canvas, for reading pixels past its edge too.
WIN = (-16, -16, W + 32, H + 32)
TREE_FIELDS = ("path", "type", "visible", "opacity", "blend_mode", "locked", "alpha_locked",
               "inherit_alpha", "pass_through", "bounds")


def paint(doc, parent, name, rect=None, seed=0, **props):
    """A paint layer on top of ``parent``'s children, with ``pattern``
    pixels (alpha 0, partial and 255, colours under zero alpha) in
    ``rect``; ``props``: opacity (0-255), blend, inherit_alpha,
    alpha_locked, locked, visible."""
    node = doc.createNode(name, "paintlayer")
    parent.addChildNode(node, None)
    if rect is not None:
        x, y, w, h = rect
        assert node.setPixelData(pattern(w, h, seed), x, y, w, h)
    _props(node, props)
    return node


def group(doc, parent, name, **props):
    node = doc.createGroupLayer(name)
    parent.addChildNode(node, None)
    if props.pop("pass_through", False):
        node.setPassThroughMode(True)
    _props(node, props)
    return node


def _props(node, props):
    if "opacity" in props:
        node.setOpacity(props["opacity"])
    if "blend" in props:
        node.setBlendingMode(props["blend"])
    for key, setter in (("inherit_alpha", node.setInheritAlpha),
                        ("alpha_locked", node.setAlphaLocked),
                        ("visible", node.setVisible)):
        if key in props:
            setter(props[key])
    if props.get("locked"):
        node.setLocked(True)


def build(doc, extras=True):
    """The synthetic document (createDocument's white Background stays at
    the bottom). ``extras``: also a transparency mask and a filter layer,
    which export_layers can't carry over."""
    root = doc.rootNode()
    body = group(doc, root, "Body")
    paint(doc, body, "Base", (2, 3, 20, 15), seed=1)
    paint(doc, body, "Shade", (6, 5, 14, 12), seed=2, blend="multiply", inherit_alpha=True,
          opacity=128)
    paint(doc, body, "Glow", (10, 8, 9, 7), seed=3, blend="add")
    paint(doc, body, "Glow", (12, 2, 6, 6), seed=4, blend="linear_dodge")
    head = group(doc, body, "Head", pass_through=True)
    eyes = group(doc, head, "Eyes", opacity=200)
    paint(doc, eyes, "Eye/L", (25, 18, 5, 4), seed=5)  # off origin; a slash in the name
    paint(doc, head, "Empty")
    hidden = group(doc, root, "Hidden", visible=False)
    paint(doc, hidden, "Secret", (0, 0, 8, 8), seed=6)
    paint(doc, root, "Edge", (-5, 22, 12, 10), seed=7, blend="screen")  # past left and bottom
    paint(doc, root, "Locked", (30, 1, 6, 6), seed=8, locked=True, alpha_locked=True)
    if extras:
        masked = paint(doc, root, "Masked", (14, 14, 10, 10), seed=9)
        mask = doc.createTransparencyMask("M")
        masked.addChildNode(mask, None)
        filt = Krita.instance().filter("invert")
        layer = doc.createFilterLayer("Filter", filt, Selection())
        assert layer is not None
        root.addChildNode(layer, None)
    settle(doc)


# Create order: depth-first, siblings bottom to top, parents first.
ORDER = ["Background", "Body", "Body/Base", "Body/Shade", "Body/Glow", "Body/Glow",
         "Body/Head", "Body/Head/Eyes", "Body/Head/Eyes/Eye\\/L", "Body/Head/Empty",
         "Hidden", "Hidden/Secret", "Edge", "Locked"]


def read_bytes(read, rect):
    return pixels.as_bytes(read(*rect))


class ExportLayersBase(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-export-")
        self.outside = tempfile.mkdtemp(prefix="kmcp-outside-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.callback(shutil.rmtree, self.outside, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def export(self, handle, name="out", **params):
        d = os.path.join(self.root, name)
        return self.assertOk(run("export_layers", doc=handle, dir=d, **params))

    def check_manifest(self, result):
        """The written manifest equals the result's, and to_specs takes it."""
        m = manifest.read(result["manifest"])
        self.assertEqual((m["version"], m["generator"]), (1, "krita-mcp"))
        self.assertEqual(m["layers"], result["layers"])
        specs, skipped, _ = manifest.to_specs(m, result["dir"])
        projections = [e for e in m["layers"] if e.get("projection")]
        self.assertEqual(len(specs) + len(skipped), len(m["layers"]))
        self.assertEqual([s["key"] for s in skipped], [e["key"] for e in projections])
        return m

    def check_pixels(self, doc, result):
        """Each PNG is bit-exact against the layer's pixelData at its
        rectangle; a group projection's against ``reference_projection``."""
        for e in result["layers"]:
            if e.get("png") is None:
                continue
            data, w, h = pixels.load_png_bgra(os.path.join(result["dir"], e["png"]))
            self.assertEqual((w, h), (e["w"], e["h"]), e["path"])
            rect = (e["x"], e["y"], w, h)
            if e.get("projection"):
                want = reference_projection(doc, e["id"], rect)
            else:
                want = read_bytes(nodes.node_by_id(doc, e["id"]).pixelData, rect)
            self.assertEqual(data, want, e["path"])


def reference_projection(doc, gid, rect):
    """Group ``gid``'s projection over ``rect``: read as is when Krita
    keeps one (isolated, it and its ancestors visible), else from a clone
    in which only this group is made isolated and visible (and its
    ancestors visible). export_layers batches the clones per depth; this
    does one group at a time."""
    index = nodes.build_index(doc)
    chain = [gid] + index.ancestors(gid)
    group = nodes.node_by_id(doc, gid)
    if not group.passThroughMode() and all(nodes.node_by_id(doc, n).visible() for n in chain):
        return read_bytes(group.projectionPixelData, rect)
    clone = doc.clone()
    try:
        return _clone_projection(clone, gid, chain, rect)
    finally:
        clone.setModified(False)
        clone.close()


def _clone_projection(clone, gid, chain, rect):
    for n in chain:
        nodes.node_by_id(clone, n).setVisible(True)
    nodes.node_by_id(clone, gid).setPassThroughMode(False)
    settle(clone)
    return read_bytes(nodes.node_by_id(clone, gid).projectionPixelData, rect)


class ExportLayersTest(ExportLayersBase):
    def test_structure_files_crop_and_pixels(self):
        with scratch_document(W, H, name="kmcp-export") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            result = self.export(handle)
            self.assertEqual(set(result), {"doc", "dir", "manifest", "count", "layers",
                                           "skipped", "warnings", "bytes"})
            self.assertEqual(result["doc"], handle)
            self.assertEqual(result["dir"], os.path.realpath(os.path.join(self.root, "out")))
            self.assertEqual(result["manifest"], os.path.join(result["dir"], "manifest.json"))
            layers = result["layers"]
            order = ORDER + ["Masked"]
            self.assertEqual([e["path"] for e in layers], order)
            self.assertEqual([e["key"] for e in layers],
                             [f"L{i:04d}" for i in range(1, len(order) + 1)])
            self.assertEqual([e["stack_index"] for e in layers], list(range(len(order))))
            by_path = {}
            for e in layers:
                by_path.setdefault(e["path"], e)
            index = nodes.build_index(doc)
            key = {e["id"]: e["key"] for e in layers if e["type"] == "group"}
            for e in layers:
                parent = index.parent(e["id"])
                self.assertEqual(e["parent"], "/" if parent == "/" else "$" + key[parent],
                                 e["path"])

            # Files: <position>_<slug>.png, unique with duplicate names.
            pngs = [e["png"] for e in layers if e.get("png")]
            self.assertEqual(len(set(pngs)), len(pngs))
            glows = [e for e in layers if e["name"] == "Glow"]
            self.assertEqual([g["png"] for g in glows], ["0005_glow.png", "0006_glow.png"])
            self.assertEqual(by_path["Body/Head/Eyes/Eye\\/L"]["png"], "0009_eye-l.png")
            self.assertEqual(sorted(os.listdir(result["dir"])), sorted(pngs + ["manifest.json"]))
            self.assertEqual(result["count"], len(pngs))
            self.assertEqual(result["bytes"], sum(os.path.getsize(os.path.join(result["dir"], f))
                                                  for f in os.listdir(result["dir"])))

            # Properties as stored; groups vs paint layers.
            self.assertEqual(
                {k: v for k, v in by_path["Body/Shade"].items()
                 if k not in ("id", "png", "x", "y", "w", "h")},
                {"key": "L0004", "name": "Shade", "path": "Body/Shade", "type": "paint",
                 "parent": "$L0002", "visible": True, "opacity": 0.502,
                 "blend_mode": "multiply", "inherit_alpha": True, "alpha_locked": False,
                 "locked": False, "stack_index": 3})
            self.assertEqual(round(by_path["Body/Shade"]["opacity"] * 255), 128)
            self.assertEqual([g["blend_mode"] for g in glows], ["add", "linear_dodge"])
            head = by_path["Body/Head"]
            self.assertEqual(set(head), {"key", "id", "name", "path", "type", "parent",
                                         "visible", "opacity", "blend_mode", "inherit_alpha",
                                         "locked", "pass_through", "stack_index"})
            self.assertIs(head["pass_through"], True)
            self.assertIs(by_path["Body"]["pass_through"], False)
            self.assertEqual(round(by_path["Body/Head/Eyes"]["opacity"] * 255), 200)
            self.assertIs(by_path["Hidden"]["visible"], False)
            self.assertIs(by_path["Hidden/Secret"]["visible"], True)
            self.assertEqual((by_path["Locked"]["locked"], by_path["Locked"]["alpha_locked"]),
                             (True, True))
            self.assertEqual(by_path["Edge"]["blend_mode"], "screen")
            for e in layers:
                self.assertEqual(e["id"], nodes.node_id(nodes.node_by_id(doc, e["id"])))

            # Empty layer: png null, no rectangle.
            empty = by_path["Body/Head/Empty"]
            self.assertIsNone(empty["png"])
            self.assertFalse({"x", "y", "w", "h"} & set(empty))

            # Crop rects are the opaque bounds (alpha >= 1), off-canvas kept.
            # (createDocument's Background reads as white everywhere, past the
            # canvas too: its default pixel is the fill; its bounds are the canvas.)
            self.assertEqual({k: by_path["Background"][k] for k in "xywh"},
                             {"x": 0, "y": 0, "w": W, "h": H})
            for e in layers:
                if e["type"] != "paint" or e["png"] is None or e["path"] == "Background":
                    continue
                node = nodes.node_by_id(doc, e["id"])
                ob = pixels.opaque_bounds(read_bytes(node.pixelData, WIN), WIN[2], WIN[3])
                self.assertEqual((e["x"], e["y"], e["w"], e["h"]),
                                 (WIN[0] + ob[0], WIN[1] + ob[1], ob[2], ob[3]), e["path"])
            edge = by_path["Edge"]
            self.assertLess(edge["x"], 0)
            self.assertGreater(edge["y"] + edge["h"], H)
            self.check_pixels(doc, result)

            # Masks and other layer types: skipped, with warnings.
            types = {s["path"]: (s["type"], s["reason"]) for s in result["skipped"]}
            self.assertEqual(types, {"Masked/M": ("transparencymask", "mask"),
                                     "Filter": ("filterlayer", "unsupported_type")})
            self.assertIsNotNone(by_path["Masked"]["png"])  # raw pixels, mask not applied
            self.assertEqual({w["code"]: w["paths"] for w in result["warnings"]},
                             {"masks_not_applied": ["Masked"], "not_exported": ["Filter"]})
            self.check_manifest(result)

    def test_all_opacities_reproduce_krita_values(self):
        with scratch_document(8, 8, name="kmcp-export-opacity") as doc:
            root = doc.rootNode()
            for v in range(256):
                paint(doc, root, f"o{v}", opacity=v)
            settle(doc)
            result = self.export(registry.handle_for(doc))
            got = {e["name"]: e["opacity"] for e in result["layers"] if e["name"] != "Background"}
            self.assertEqual(len(got), 256)
            for v in range(256):
                self.assertEqual(round(got[f"o{v}"] * 255), v, v)
                self.assertTrue(0.0 <= got[f"o{v}"] <= 1.0)
            self.assertEqual(result["count"], 1)  # only the white Background has pixels

    def test_include_hidden_false(self):
        with scratch_document(W, H, name="kmcp-export-hidden") as doc:
            build(doc, extras=False)
            # A hidden paint layer inside a visible group too.
            nodes.resolve_node(doc, "Body/Base").setVisible(False)
            settle(doc)
            result = self.export(registry.handle_for(doc), include_hidden=False)
            paths = [e["path"] for e in result["layers"]]
            for p in ("Hidden", "Hidden/Secret", "Body/Base"):
                self.assertNotIn(p, paths)
            self.assertEqual({s["path"]: s["reason"] for s in result["skipped"]},
                             {"Body/Base": "hidden", "Hidden": "hidden",
                              "Hidden/Secret": "inside_hidden"})
            self.assertEqual(paths, [p for p in ORDER
                                     if p not in ("Hidden", "Hidden/Secret", "Body/Base")])
            self.check_manifest(result)
            # The default exports them, hidden as they are.
            result = self.export(registry.handle_for(doc), name="all")
            by_path = {e["path"]: e for e in result["layers"]}
            self.assertIs(by_path["Body/Base"]["visible"], False)
            self.assertIsNotNone(by_path["Hidden/Secret"]["png"])

    def test_include_groups_projections(self):
        with scratch_document(W, H, name="kmcp-export-groups") as doc:
            build(doc, extras=False)
            handle = registry.handle_for(doc)
            result = self.export(handle, include_groups=True)
            layers = result["layers"]
            proj = [e for e in layers if e.get("projection")]
            self.assertEqual([e["path"] for e in proj],
                             ["Body/Head/Eyes", "Body/Head", "Body", "Hidden"])
            self.assertEqual(result["warnings"], [])
            # Krita keeps no projection of its own for a pass-through group
            # (Head) or a hidden one (Hidden); export_layers computes them in
            # a clone, leaving the document as it was.
            self.assertEqual(pixels.count_alpha(read_bytes(
                nodes.resolve_node(doc, "Body/Head").projectionPixelData, (0, 0, W, H))), 0)
            self.assertIs(nodes.resolve_node(doc, "Body/Head").passThroughMode(), True)
            self.assertIs(nodes.resolve_node(doc, "Hidden").visible(), False)
            pos = {(e["path"], bool(e.get("projection"))): i for i, e in enumerate(layers)}
            # Each projection comes right after its group's subtree.
            self.assertEqual(layers[pos[("Body/Head/Eyes", True)] - 1]["path"],
                             "Body/Head/Eyes/Eye\\/L")
            self.assertEqual(layers[pos[("Body", True)] - 1]["path"], "Body/Head")
            self.assertEqual(layers[pos[("Body", True)] + 1]["path"], "Hidden")
            groups = {e["path"]: e for e in layers if e["type"] == "group"}
            for e in proj:
                g = groups[e["path"]]
                self.assertEqual(e["type"], "paint")
                self.assertEqual((e["id"], e["name"], e["parent"], e["opacity"],
                                  e["blend_mode"], e["visible"]),
                                 (g["id"], g["name"], g["parent"], g["opacity"],
                                  g["blend_mode"], g["visible"]))
                self.assertNotEqual(e["key"], g["key"])
                self.assertIsNotNone(e["png"], e["path"])
            self.assertEqual([e["key"] for e in layers],
                             [f"L{i:04d}" for i in range(1, len(layers) + 1)])
            self.assertEqual([e["stack_index"] for e in layers], list(range(len(layers))))
            self.assertEqual(result["count"], sum(1 for e in layers if e.get("png")))
            self.check_pixels(doc, result)
            self.check_manifest(result)

            # create_layers skips the projections and builds the rest.
            with scratch_document(W, H, name="kmcp-export-groups-2") as doc2:
                h2 = registry.handle_for(doc2)
                created = self.assertOk(run("create_layers", doc=h2, manifest=result["manifest"]))
                self.assertEqual([s["key"] for s in created["skipped"]], [e["key"] for e in proj])
                self.assertEqual(len(created["created"]), len(layers) - len(proj))

    def test_crop_false_canvas_sized(self):
        with scratch_document(W, H, name="kmcp-export-nocrop") as doc:
            build(doc, extras=False)
            result = self.export(registry.handle_for(doc), crop=False, png_level=1)
            for e in result["layers"]:
                if e["type"] == "paint" and e["png"] is not None:
                    self.assertEqual((e["x"], e["y"], e["w"], e["h"]), (0, 0, W, H), e["path"])
            self.assertIsNone({e["path"]: e for e in result["layers"]}["Body/Head/Empty"]["png"])
            self.assertEqual([(w["code"], w["paths"]) for w in result["warnings"]],
                             [("off_canvas", ["Edge"])])
            self.check_pixels(doc, result)
            self.check_manifest(result)

    def test_subset_ids_include_ancestors(self):
        with scratch_document(W, H, name="kmcp-export-subset") as doc:
            build(doc, extras=False)
            handle = registry.handle_for(doc)
            result = self.export(handle, ids=["Body/Head/Eyes/Eye\\/L", "Locked"])
            self.assertEqual([(e["path"], e["type"]) for e in result["layers"]],
                             [("Body", "group"), ("Body/Head", "group"),
                              ("Body/Head/Eyes", "group"), ("Body/Head/Eyes/Eye\\/L", "paint"),
                              ("Locked", "paint")])
            self.assertEqual([e["parent"] for e in result["layers"]],
                             ["/", "$L0001", "$L0002", "$L0003", "/"])
            self.assertEqual(result["count"], 2)
            self.check_manifest(result)

            # A selected group brings its subtree; projections only for
            # selected groups, not for ancestors added for the structure.
            result = self.export(handle, name="head", ids=["Body/Head"], include_groups=True)
            self.assertEqual([(e["path"], bool(e.get("projection"))) for e in result["layers"]],
                             [("Body", False), ("Body/Head", False), ("Body/Head/Eyes", False),
                              ("Body/Head/Eyes/Eye\\/L", False), ("Body/Head/Eyes", True),
                              ("Body/Head/Empty", False), ("Body/Head", True)])
            self.check_manifest(result)
            # "/" in the list means everything.
            result = self.export(handle, name="root", ids=["/"])
            self.assertEqual([e["path"] for e in result["layers"]], ORDER)


class ExportLayersErrorsTest(ExportLayersBase):
    def test_dir_rules(self):
        with scratch_document(W, H, name="kmcp-export-dirs") as doc:
            build(doc, extras=False)
            handle = registry.handle_for(doc)
            busy = os.path.join(self.root, "busy")
            os.mkdir(busy)
            with open(os.path.join(busy, "keep.txt"), "w") as f:
                f.write("x")
            err = self.assertError(run("export_layers", doc=handle, dir=busy), "conflict")
            self.assertIn("not empty", err["message"])
            result = self.assertOk(run("export_layers", doc=handle, dir=busy, overwrite=True))
            self.assertIn("keep.txt", os.listdir(busy))
            # Overwriting the same export again replaces its files.
            again = self.assertOk(run("export_layers", doc=handle, dir=busy, overwrite=True))
            self.assertEqual(again["layers"], result["layers"])

            os.symlink(self.outside, os.path.join(self.root, "escape"))
            self.assertError(run("export_layers", doc=handle,
                                 dir=os.path.join(self.root, "escape", "out")), "forbidden_path")
            self.assertError(run("export_layers", doc=handle, dir=self.outside), "forbidden_path")
            self.assertEqual(os.listdir(self.outside), [])
            with open(os.path.join(self.root, "file.png"), "wb") as f:
                f.write(b"x")
            self.assertError(run("export_layers", doc=handle,
                                 dir=os.path.join(self.root, "file.png")), "conflict")
            self.assertError(run("export_layers", doc=handle), "bad_request")

    def test_bad_params(self):
        with scratch_document(W, H, name="kmcp-export-params") as doc:
            build(doc, extras=False)
            handle = registry.handle_for(doc)
            d = os.path.join(self.root, "p")

            def err(code, **params):
                return self.assertError(run("export_layers", doc=handle, dir=d, **params), code)

            for level in (10, -1, True, "1", 1.5):
                err("bad_request", png_level=level)
            for key in ("crop", "include_hidden", "include_groups", "overwrite"):
                err("bad_request", **{key: "yes"})
            err("bad_request", ids=[])
            err("bad_request", ids="Body")
            self.assertEqual(err("not_found", ids=["Body", "Nope"])["details"]["index"], 1)
            err("ambiguous", ids=["Glow"])
            self.assertFalse(os.path.exists(d))  # nothing created by a bad call
            for i, level in enumerate((0, 9, None)):
                self.assertOk(run("export_layers", doc=handle, dir=f"{d}{i}", png_level=level))

    def test_not_recreatable_warning(self):
        with scratch_document(8, 8, name="kmcp-export-odd") as doc:
            paint(doc, doc.rootNode(), "Odd", (0, 0, 2, 2), blend="not-a-mode")
            settle(doc)
            result = self.export(registry.handle_for(doc))
            (warning,) = result["warnings"]
            self.assertEqual(warning["code"], "not_recreatable")
            self.assertEqual([l["path"] for l in warning["layers"]], ["Odd"])
            self.assertIn("not-a-mode", warning["message"])
            self.assertEqual(result["layers"][-1]["blend_mode"], "not-a-mode")  # as stored

    def test_failure_removes_the_pngs_it_wrote(self):
        real = pixels.save_png
        calls = []

        def failing(*args, **kwargs):
            calls.append(args[1])
            if len(calls) == 3:
                raise CommandError("krita_error", "disk full (test)")
            return real(*args, **kwargs)

        with scratch_document(W, H, name="kmcp-export-fail") as doc:
            build(doc, extras=False)
            d = os.path.join(self.root, "fail")
            os.mkdir(d)
            keep = os.path.join(d, "0001_background.png")  # replaced under overwrite: stays
            with open(keep, "wb") as f:
                f.write(b"old")
            pixels.save_png = failing
            try:
                err = self.assertError(run("export_layers", doc=registry.handle_for(doc), dir=d,
                                           overwrite=True), "krita_error")
            finally:
                pixels.save_png = real
            self.assertIn("disk full", err["message"])
            self.assertEqual(len(calls), 3)
            self.assertEqual(os.listdir(d), ["0001_background.png"])

    def test_non_rgba8_document(self):
        with scratch_document(8, 8, name="kmcp-export-u16", color_depth="U16") as doc:
            handle = registry.handle_for(doc)
            d = os.path.join(self.root, "u16")
            err = self.assertError(run("export_layers", doc=handle, dir=d), "conflict")
            self.assertEqual((err["details"]["color_model"], err["details"]["color_depth"]),
                             ("RGBA", "U16"))
            self.assertFalse(os.path.exists(d))


class RoundTripTest(ExportLayersBase):
    """acceptance § 2, small variant: export, rebuild on a new transparent
    canvas, byte-identical projections and matching layer trees."""

    def _tree(self, handle, drop=None):
        layers = self.assertOk(run("list_layers", doc=handle))["layers"]
        return [tuple(l[k] for k in TREE_FIELDS) for l in layers if l["id"] != drop]

    def round_trip(self, extras=False, **export):
        handles = []
        try:
            with scratch_document(W, H, name="kmcp-roundtrip-a") as doc:
                build(doc, extras=extras)
                handle = registry.handle_for(doc)
                result = self.export(handle, name="rt", **export)
                canvas = self.assertOk(run("new_canvas", width=W, height=H,
                                           background="transparent", name="kmcp-roundtrip-b"))
                handles.append(canvas["doc"])
                created = self.assertOk(run("create_layers", doc=canvas["doc"],
                                            manifest=result["manifest"]))
                self.assertEqual(created["warnings"], [])
                doc2 = registry.lookup(canvas["doc"])
                settle(doc)
                settle(doc2)
                a = pixels.as_bytes(doc.pixelData(0, 0, W, H))
                b = pixels.as_bytes(doc2.pixelData(0, 0, W, H))
                tree_a = self._tree(handle)
                tree_b = self._tree(canvas["doc"], drop=canvas["background_layer"])
                return a, b, tree_a, tree_b, result
        finally:
            for h in handles:
                try:
                    registry.lookup(h)
                except CommandError:
                    continue
                run("close_document", doc=h, discard=True)

    def test_round_trip_identical(self):
        a, b, tree_a, tree_b, _ = self.round_trip()
        self.assertEqual(len(a), 4 * W * H)
        self.assertTrue(a == b, _first_diff(a, b))
        self.assertEqual(tree_b, tree_a)

    def test_round_trip_uncropped_with_projections(self):
        # crop=False loses Edge's pixels past the canvas (bounds differ),
        # but the canvas renders the same; projections are skipped.
        a, b, tree_a, tree_b, _ = self.round_trip(crop=False, include_groups=True)
        self.assertTrue(a == b, _first_diff(a, b))
        strip = [t[:-1] for t in tree_a]
        self.assertEqual([t[:-1] for t in tree_b], strip)

    def test_zero_alpha_colour_outside_alpha_bounds(self):
        # Colour under alpha 0 next to a layer's alpha bounds (as in PNGs
        # with coloured transparent margins) can show up in Krita's
        # projection at fully transparent pixels. crop=true cuts to alpha
        # > 0, so the rebuild has no such colour; crop=false keeps it
        # within the canvas.
        block = bytes((40, 80, 120, 255)) * (6 * 4)
        margin = bytearray(bytes((30, 28, 130, 0)) * (8 * 6))
        for r in range(4):
            o = 4 * ((r + 1) * 8 + 1)
            margin[o:o + 24] = block[24 * r:24 * (r + 1)]
        results = {}
        for crop in (True, False):
            handles = []
            try:
                canvas = self.assertOk(run("new_canvas", width=16, height=12,
                                           background="transparent", name="kmcp-margin-a"))
                handles.append(canvas["doc"])
                doc = registry.lookup(canvas["doc"])
                layer = paint(doc, doc.rootNode(), "M")
                self.assertTrue(layer.setPixelData(bytes(margin), 3, 2, 8, 6))
                settle(doc)
                b = layer.bounds()
                self.assertEqual((b.x(), b.y(), b.width(), b.height()), (4, 3, 6, 4))
                result = self.export(canvas["doc"], name=f"margin-{crop}", crop=crop)
                other = self.assertOk(run("new_canvas", width=16, height=12,
                                          background="transparent", name="kmcp-margin-b"))
                handles.append(other["doc"])
                self.assertOk(run("create_layers", doc=other["doc"],
                                  manifest=result["manifest"]))
                doc2 = registry.lookup(other["doc"])
                settle(doc2)
                a = pixels.as_bytes(doc.pixelData(0, 0, 16, 12))
                c = pixels.as_bytes(doc2.pixelData(0, 0, 16, 12))
                results[crop] = (a == c, a[3::4] == c[3::4],
                                 a.count(bytes((30, 28, 130, 0))) > 0)
            finally:
                for h in handles:
                    run("close_document", doc=h, discard=True)
        # (byte-identical, alpha identical, margin colour in the original's
        # projection): Krita keeps the colour; only crop=false reproduces it.
        self.assertEqual(results, {True: (False, True, True), False: (True, True, True)})

    def test_masks_and_filter_layers_do_not_round_trip(self):
        # Documented limit (warnings masks_not_applied / not_exported).
        a, b, _, _, result = self.round_trip(extras=True)
        self.assertEqual({w["code"] for w in result["warnings"]},
                         {"masks_not_applied", "not_exported"})
        self.assertNotEqual(a, b)


def _first_diff(a, b):
    if len(a) != len(b):
        return f"sizes differ: {len(a)} vs {len(b)}"
    for i in range(0, len(a), 4):
        if a[i:i + 4] != b[i:i + 4]:
            p = i // 4
            return f"first differing pixel ({p % W}, {p // W}): {tuple(a[i:i + 4])} vs {tuple(b[i:i + 4])}"
    return "identical"


if __name__ == "__main__":
    unittest.main()
