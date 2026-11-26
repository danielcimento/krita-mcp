"""commands/render.py: render and get_canvas, headless, through dispatch.execute.

Every call passes ``doc`` and every document is closed in ``finally``
(``scratch_document``). Renders go into a temp dir made an allowed root
through a temp config file; a second temp dir stays outside every root.

The D10 check is here: a clone's projection follows visibility changes
made in the clone, and a subset render leaves the user's document (its
projection, every node's visibility, ``modified()``) and the set of open
documents as they were.

Per-pixel Python loops appear only here, in tests, over small buffers.
"""

import contextlib
import os
import random
import shutil
import tempfile
import unittest

from krita import Krita
from PyQt5.QtCore import Qt

from helpers import add_paint_layer, scratch_document, settle, temp_config
from kritamcp import config, nodes, pixels, registry
from test_dispatch import EnvelopeAssertions, run
from test_pixels import pattern

W, H = 24, 16
RED, GREEN, YELLOW, MAGENTA = (0, 0, 255, 255), (0, 255, 0, 255), (0, 255, 255, 255), (255, 0, 255, 255)
BLUE, CYAN, GREY = (255, 0, 0, 255), (255, 255, 0, 255), (90, 90, 90, 255)
CLEAR = (0, 0, 0, 0)

# name path -> (rect, colour); the rects are disjoint, so a render of
# Normal layers over a transparent canvas is each shown layer's rectangle.
LAYERS = {
    "Top": ((0, 0, 4, 4), RED),
    "G/H/L": ((4, 0, 4, 4), GREEN),
    "G/H/L2": ((8, 0, 4, 4), YELLOW),
    "G/M": ((12, 0, 4, 4), MAGENTA),
    "K/A": ((0, 8, 4, 4), BLUE),
    "K/B": ((4, 8, 4, 4), CYAN),
    "Bottom": ((16, 8, 4, 4), GREY),
}
HIDDEN = {"G", "G/H/L2", "K/B"}  # hidden in the document
SHOWN = {"Top", "K/A", "Bottom"}  # what the document itself renders


def _remove_background(doc):
    for node in doc.rootNode().childNodes():
        if node.name() == "Background":
            node.remove()


def build(doc):
    """The test tree, top to bottom: Top, G (hidden) > [H > [L, L2 (hidden)],
    M], K > [A, B (hidden)], Bottom; no Background, so the canvas is
    transparent. Not marked modified."""
    _remove_background(doc)
    root = doc.rootNode()

    def group(parent, name):
        g = doc.createGroupLayer(name)
        parent.addChildNode(g, None)
        return g

    def paint(parent, path):
        (x, y, w, h), colour = LAYERS[path]
        node = doc.createNode(path.rsplit("/", 1)[-1], "paintlayer")
        parent.addChildNode(node, None)
        assert node.setPixelData(bytes(colour) * (w * h), x, y, w, h)
        return node

    # addChildNode(None) appends on top: add bottom to top.
    paint(root, "Bottom")
    k = group(root, "K")
    paint(k, "K/B").setVisible(False)
    paint(k, "K/A")
    g = group(root, "G")
    paint(g, "G/M")
    h = group(g, "H")
    paint(h, "G/H/L2").setVisible(False)
    paint(h, "G/H/L")
    g.setVisible(False)
    paint(root, "Top")
    settle(doc)
    doc.setModified(False)


def expected(shown, w=W, h=H):
    """BGRA bytes of a ``w`` x ``h`` canvas showing the ``shown`` layers."""
    buf = bytearray(4 * w * h)
    for path in shown:
        (x, y, rw, rh), colour = LAYERS[path]
        for r in range(y, y + rh):
            buf[4 * (r * w + x):4 * (r * w + x + rw)] = bytes(colour) * rw
    return bytes(buf)


def visibility(doc):
    return {e["id"]: bool(n.visible()) for n, e in nodes.walk(doc)}


def projection_bytes(doc, rect=(0, 0, W, H)):
    return pixels.from_qimage(doc.projection(*rect))[0]


class RenderBase(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-render-")
        self.outside = tempfile.mkdtemp(prefix="kmcp-outside-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.callback(shutil.rmtree, self.outside, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))
        self.n = 0

    def tearDown(self):
        self._stack.close()

    def out(self, name=None):
        self.n += 1
        return os.path.join(self.root, name or f"r{self.n}.png")

    def render(self, handle, **params):
        params.setdefault("path", self.out())
        return self.assertOk(run("render", doc=handle, **params))

    def load(self, result):
        data, w, h = pixels.load_png_bgra(result["path"])
        self.assertEqual((w, h), (result["width"], result["height"]))
        return data


class CloneTest(RenderBase):
    """D10: subset renders use a clone; it must follow visibility changes."""

    def test_clone_projection_follows_visibility(self):
        with scratch_document(8, 8, name="kmcp-render-clone") as doc:
            _remove_background(doc)
            layer = add_paint_layer(doc, "Opaque")
            self.assertTrue(layer.setPixelData(bytes(RED) * 64, 0, 0, 8, 8))
            settle(doc)
            count = len(Krita.instance().documents())
            clone = doc.clone()
            try:
                self.assertEqual(len(Krita.instance().documents()), count)  # not registered
                _hide_all(clone)
                clone.refreshProjection()
                clone.waitForDone()
                alpha = projection_bytes(clone, (0, 0, 8, 8))[3::4]
            finally:
                clone.setModified(False)
                clone.close()
            del clone
            self.assertEqual(alpha, bytes(64))  # alpha 255 -> 0 in the clone
            self.assertEqual(projection_bytes(doc, (0, 0, 8, 8)), bytes(RED) * 64)
            self.assertTrue(layer.visible())
            self.assertFalse(doc.modified())
            self.assertEqual(len(Krita.instance().documents()), count)

    def test_subset_renders_leave_the_document_alone(self):
        with scratch_document(W, H, name="kmcp-render-alone") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            count = len(Krita.instance().documents())
            before = (visibility(doc), projection_bytes(doc))
            for params in ({"only": ["G/H/L"]}, {"hide": ["Top"]}, {"only": ["K"], "hide": ["K/A"]},
                           {"only": ["G"], "region": {"x": 2, "y": 0, "w": 30, "h": 5},
                            "scale": 0.5, "background": "#ffffff"}):
                self.render(handle, **params)
                self.assertEqual((visibility(doc), projection_bytes(doc)), before, params)
                self.assertFalse(doc.modified(), params)
                self.assertEqual(len(Krita.instance().documents()), count, params)


def _hide_all(doc):
    for node in doc.rootNode().childNodes():
        node.setVisible(False)


class RenderTest(RenderBase):
    def test_full_render_is_the_projection_exactly(self):
        with scratch_document(W, H, name="kmcp-render-full") as doc:
            _remove_background(doc)
            layer = add_paint_layer(doc, "Pattern")  # partial alpha, colour under alpha 0
            self.assertTrue(layer.setPixelData(pattern(W, H, 4), 0, 0, W, H))
            settle(doc)
            handle = registry.handle_for(doc)
            result = self.render(handle)
            self.assertEqual(set(result), {"doc", "path", "width", "height", "region", "scale",
                                           "bytes"})
            self.assertEqual(result["doc"], handle)
            self.assertEqual(result["path"], os.path.join(os.path.realpath(self.root), "r1.png"))
            self.assertEqual((result["width"], result["height"], result["scale"]), (W, H, 1.0))
            self.assertEqual(result["region"], {"x": 0, "y": 0, "w": W, "h": H})
            self.assertEqual(result["bytes"], os.path.getsize(result["path"]))
            data = self.load(result)
            self.assertEqual(data, projection_bytes(doc))
            self.assertEqual(data, pixels.as_bytes(doc.pixelData(0, 0, W, H)))
            self.assertEqual(data, pattern(W, H, 4))

    def test_region_clipped_to_the_canvas(self):
        with scratch_document(W, H, name="kmcp-render-region") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            full = expected(SHOWN)
            result = self.render(handle, region={"x": -3, "y": 10, "w": 10, "h": 20})
            self.assertEqual(result["region"], {"x": 0, "y": 10, "w": 7, "h": 6})
            self.assertEqual((result["width"], result["height"]), (7, 6))
            self.assertEqual(self.load(result), pixels.crop(full, W, H, (0, 10, 7, 6)))
            inner = self.render(handle, region={"x": 1, "y": 2, "w": 3, "h": 4})
            self.assertEqual(self.load(inner), pixels.crop(full, W, H, (1, 2, 3, 4)))

    def test_bad_regions(self):
        with scratch_document(W, H, name="kmcp-render-badregion") as doc:
            handle = registry.handle_for(doc)
            for region in ({"x": 0, "y": 0, "w": 0, "h": 4}, {"x": 0, "y": 0, "w": 4, "h": -1},
                           {"x": W, "y": 0, "w": 4, "h": 4}, {"x": -10, "y": -10, "w": 10, "h": 5},
                           {"x": 0, "y": 0, "w": 4}, {"x": 0, "y": 0, "w": 4, "h": 4, "z": 1},
                           {"x": 0.5, "y": 0, "w": 4, "h": 4}, {"x": True, "y": 0, "w": 4, "h": 4},
                           [0, 0, 4, 4], "all"):
                err = self.assertError(run("render", doc=handle, path=self.out("bad.png"),
                                           region=region), "bad_request")
                self.assertEqual(err["details"]["param"], "region", region)
            self.assertFalse(os.path.exists(self.out("bad.png")))

    def test_scale_sizes(self):
        with scratch_document(W, H, name="kmcp-render-scale") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            for scale, size in ((0.5, (12, 8)), (2, (48, 32)), (0.25, (6, 4)), (4, (96, 64)),
                                (0.001, (1, 1)), (1.01, (24, 16))):
                result = self.render(handle, scale=scale)
                self.assertEqual((result["width"], result["height"]), size, scale)
                self.assertEqual(result["scale"], float(scale))
                self.load(result)
            # A 5x3 region at 0.5 is 2.5 x 1.5: rounded half up, 3x2.
            result = self.render(handle, region={"x": 0, "y": 0, "w": 5, "h": 3}, scale=0.5)
            self.assertEqual((result["width"], result["height"]), (3, 2))
            # Upscaled 2x, the middle of Top's red 4x4 block stays red, and
            # an empty area stays fully transparent.
            data = self.load(self.render(handle, scale=2))
            self.assertEqual(_pixel(data, 2 * W, 3, 3), RED)
            self.assertEqual(_pixel(data, 2 * W, 40, 2), CLEAR)
            for bad in (0, -1, 4.5, 5, True, "1", float("nan")):
                err = self.assertError(run("render", doc=handle, path=self.out(), scale=bad),
                                       "bad_request")
                self.assertEqual(err["details"]["param"], "scale")

    def test_too_large(self):
        with scratch_document(W, H, name="kmcp-render-large") as doc:
            handle = registry.handle_for(doc)
            with temp_config({"allowed_dirs": [self.root], "limits": {"max_canvas_pixels": 100}}):
                self.assertEqual(config.current().limits["max_canvas_pixels"], 100)
                err = self.assertError(run("render", doc=handle, path=self.out("big.png")),
                                       "too_large")
                self.assertEqual(err["details"]["limit"], "max_canvas_pixels")
                self.assertEqual(err["details"]["pixels"], W * H)
                self.assertFalse(os.path.exists(self.out("big.png")))
                self.render(handle, scale=0.5)  # 12x8 = 96
            # At the default limit, 4x of 24x16 is fine.
            self.render(handle, scale=4)

    def test_background_composited_under(self):
        with scratch_document(W, H, name="kmcp-render-bg") as doc:
            build(doc)
            layer = add_paint_layer(doc, "Half")
            self.assertTrue(layer.setPixelData(bytes((0, 0, 255, 128)), 20, 0, 1, 1))  # red, alpha 128
            settle(doc)
            handle = registry.handle_for(doc)
            result = self.render(handle, background="#336699")
            self.assertEqual(result["background"], "#336699")
            data = self.load(result)
            self.assertEqual(_pixel(data, W, 22, 14), (0x99, 0x66, 0x33, 255))  # was transparent
            self.assertEqual(_pixel(data, W, 1, 1), RED)  # opaque: unchanged
            b, g, r, a = _pixel(data, W, 20, 0)  # red at 50% over the background
            self.assertEqual(a, 255)
            for got, want in zip((b, g, r), (0x99 * 127 / 255, 0x66 * 127 / 255,
                                             255 * 128 / 255 + 0x33 * 127 / 255)):
                self.assertLessEqual(abs(got - want), 1)
            # With only, the background goes under the subset.
            sub = self.load(self.render(handle, only=["K"], background="#fff"))
            self.assertEqual(_pixel(sub, W, 1, 1), (255, 255, 255, 255))
            self.assertEqual(_pixel(sub, W, 1, 9), BLUE)
            # Translucent background, and "transparent" is the same as none.
            half = self.load(self.render(handle, background="#80ffffff"))
            self.assertEqual(_pixel(half, W, 22, 14), (255, 255, 255, 128))
            plain = self.load(self.render(handle))
            self.assertEqual(self.load(self.render(handle, background="transparent")), plain)
            for bad in ("nope", 12, "#12345", ""):
                err = self.assertError(run("render", doc=handle, path=self.out(), background=bad),
                                       "bad_request")
                self.assertEqual(err["details"]["param"], "background")

    def test_png_level(self):
        with scratch_document(W, H, name="kmcp-render-level") as doc:
            _remove_background(doc)
            add_paint_layer(doc, "P").setPixelData(pattern(W, H, 2), 0, 0, W, H)
            settle(doc)
            handle = registry.handle_for(doc)
            fast = self.render(handle, png_level=0)
            small = self.render(handle, png_level=9)
            self.assertEqual((fast["png_level"], small["png_level"]), (0, 9))
            self.assertGreater(fast["bytes"], small["bytes"])
            self.assertEqual(self.load(fast), self.load(small))
            for bad in (10, -1, True, 1.5):
                self.assertError(run("render", doc=handle, path=self.out(), png_level=bad),
                                 "bad_request")

    def test_paths(self):
        with scratch_document(W, H, name="kmcp-render-paths") as doc:
            handle = registry.handle_for(doc)
            nested = os.path.join(self.root, "a", "b", "x.PNG")
            result = self.render(handle, path=nested)
            self.assertEqual(result["path"], os.path.join(os.path.realpath(self.root), "a", "b",
                                                          "x.PNG"))
            self.assertTrue(os.path.isfile(result["path"]))
            err = self.assertError(run("render", doc=handle, path=self.out("x.jpg")),
                                   "bad_request")
            self.assertIn("PNG", err["message"])
            self.assertError(run("render", doc=handle, path=self.out("x.txt")), "forbidden_path")
            self.assertError(run("render", doc=handle, path=os.path.join(self.outside, "x.png")),
                             "forbidden_path")
            self.assertError(run("render", doc=handle), "bad_request")
            self.assertError(run("render", doc="doc-999999", path=self.out()), "not_found")
            self.assertEqual(os.listdir(self.outside), [])

    def test_non_rgba8_documents_render(self):
        # Krita converts the projection to 8-bit sRGB (convertToQImage).
        for depth, profile in (("U16", "sRGB-elle-V2-srgbtrc.icc"), ("F32", "")):
            with scratch_document(8, 8, name=f"kmcp-render-{depth}", color_depth=depth,
                                  profile=profile) as doc:
                handle = registry.handle_for(doc)
                data = self.load(self.render(handle))
                self.assertEqual(data, bytes((255, 255, 255, 255)) * 64, depth)  # the white Background

    def test_smooth_scaling_precision(self):
        # Halving is a 2x2 average. Scaling a 16-bit premultiplied copy (what
        # render does) keeps colours near the exact average at every alpha;
        # Qt's own path for ARGB32 (8-bit premultiplied) does not.
        rnd = random.Random(5)
        w = h = 32
        buf = bytearray()
        for _ in range(w * h):
            a = rnd.choice([0, 1, 2, 5, 10, 40, 128, 200, 254, 255, rnd.randrange(256)])
            buf += bytes((rnd.randrange(256), rnd.randrange(256), rnd.randrange(256), a))
        data = bytes(buf)
        ref = _halved(data, w, h)
        ours = _errors(_render_scaled(data, w, h), ref)
        qt8 = pixels.to_qimage(data, w, h).scaled(w // 2, h // 2, Qt.IgnoreAspectRatio,
                                                  Qt.SmoothTransformation)
        theirs = _errors(pixels.from_qimage(qt8)[0], ref)
        print(f"\n  halving errors (alpha, colour, colour at alpha >= 32): RGBA64 {ours}, "
              f"ARGB32 {theirs}")
        self.assertLessEqual(ours[0], 1.0)
        self.assertLessEqual(ours[1], 1.5)
        self.assertGreater(theirs[1], 20)
        self.assertGreater(theirs[2], ours[2])
        # A uniform colour at alpha 3 keeps its colour.
        flat = bytes((37, 151, 229, 3)) * 64
        out = _render_scaled(flat, 8, 8)
        self.assertEqual(set(out[i:i + 4] for i in range(0, len(out), 4)),
                         {bytes((37, 151, 229, 3))})


def _render_scaled(data, w, h):
    from kritamcp.commands import render
    return pixels.from_qimage(render._scaled(pixels.to_qimage(data, w, h), w // 2, h // 2))[0]


def _halved(data, w, h):
    """The exact 2x2 average (premultiplied) of BGRA ``data``: ``[(colour, alpha)]``."""
    out = []
    for y in range(h // 2):
        for x in range(w // 2):
            acc = [0.0] * 4
            for dy in (0, 1):
                for dx in (0, 1):
                    p = data[4 * ((2 * y + dy) * w + 2 * x + dx):][:4]
                    for c in range(3):
                        acc[c] += p[c] * p[3] / 255
                    acc[3] += p[3] / 255
            out.append(([acc[c] / acc[3] if acc[3] else 0 for c in range(3)], acc[3] / 4 * 255))
    return out


def _errors(got, ref):
    """Max |error| of alpha, of colour where alpha > 0, of colour where alpha >= 32."""
    ea = ec = e32 = 0
    for i, (colour, a) in enumerate(ref):
        p = got[4 * i:4 * i + 4]
        ea = max(ea, abs(p[3] - a))
        if p[3] > 0 and a >= 0.5:
            e = max(abs(p[c] - colour[c]) for c in range(3))
            ec = max(ec, e)
            if a >= 32:
                e32 = max(e32, e)
    return round(ea, 2), round(ec, 2), round(e32, 2)


def _pixel(data, w, x, y):
    return tuple(data[4 * (y * w + x):4 * (y * w + x) + 4])


class OnlyHideTest(RenderBase):
    def check(self, handle, shown, **params):
        result = self.render(handle, **params)
        self.assertEqual(self.load(result), expected(shown), params)
        return result

    def test_no_subset_renders_the_document(self):
        with scratch_document(W, H, name="kmcp-render-doc") as doc:
            build(doc)
            self.check(registry.handle_for(doc), SHOWN)

    def test_only_nested_layer_shows_its_hidden_ancestors(self):
        with scratch_document(W, H, name="kmcp-render-only") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            index = nodes.build_index(doc)
            result = self.check(handle, {"G/H/L"}, only=["G/H/L"])  # G is hidden in the doc
            self.assertEqual(result["only"], [_id(index, "G/H/L")])
            self.assertNotIn("hide", result)
            self.check(handle, {"G/H/L2"}, only=["G/H/L2"])  # a hidden listed layer is shown
            self.check(handle, {"G/H/L", "Top"}, only=["L", "Top", "L"])  # bare names, repeats

    def test_only_group_keeps_its_descendants_state(self):
        with scratch_document(W, H, name="kmcp-render-group") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            self.check(handle, {"K/A"}, only=["K"])  # B stays hidden
            self.check(handle, {"G/H/L", "G/M"}, only=["G"])  # L2 stays hidden
            # A listed layer inside a listed group: the group's other
            # descendants keep their state too.
            self.check(handle, {"G/H/L", "G/M"}, only=["G", "G/H/L"])

    def test_hide(self):
        with scratch_document(W, H, name="kmcp-render-hide") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            count = len(Krita.instance().documents())
            result = self.check(handle, {"K/A", "Bottom"}, hide=["Top"])
            self.assertNotIn("only", result)
            self.check(handle, {"Top"}, hide=["K", "Bottom"])
            self.check(handle, SHOWN, hide=["K/B"])  # already hidden: nothing to change
            self.assertEqual(len(Krita.instance().documents()), count)

    def test_only_and_hide(self):
        with scratch_document(W, H, name="kmcp-render-both") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            self.check(handle, {"G/H/L"}, only=["K", "G/H/L"], hide=["K/A"])
            self.check(handle, {"G/M"}, only=["G"], hide=["H"])
            self.check(handle, set(), only=["Top"], hide=["Top"])  # hide wins

    def test_bad_refs(self):
        with scratch_document(W, H, name="kmcp-render-refs") as doc:
            build(doc)
            extra = add_paint_layer(doc, "Top")  # a second "Top"
            del extra
            handle = registry.handle_for(doc)
            err = self.assertError(run("render", doc=handle, path=self.out(),
                                       only=["K", "Nope"]), "not_found")
            self.assertEqual(err["details"]["index"], 1)
            err = self.assertError(run("render", doc=handle, path=self.out(), hide=["Top"]),
                                   "ambiguous")
            self.assertEqual(len(err["details"]["matches"]), 2)
            self.assertError(run("render", doc=handle, path=self.out(), only=["/"]), "bad_request")
            for key in ("only", "hide"):
                for bad in ([], "K", [3]):
                    self.assertError(run("render", doc=handle, path=self.out(), **{key: bad}),
                                     "bad_request")
            self.assertEqual(os.listdir(self.root), [])


def _id(index, path):
    from kritamcp import refs
    return refs.resolve(index, path)


class GetCanvasTest(RenderBase):
    def setUp(self):
        super().setUp()
        saved = config.CANVAS_OUTPUT_DIR  # current().output_dir follows it
        config.CANVAS_OUTPUT_DIR = self.root
        config.reset_cache()

        def restore():
            config.CANVAS_OUTPUT_DIR = saved
            config.reset_cache()
        self._stack.callback(restore)

    def test_get_canvas_is_a_full_render_into_the_output_dir(self):
        with scratch_document(W, H, name="kmcp-render-canvas") as doc:
            build(doc)
            handle = registry.handle_for(doc)
            result = self.assertOk(run("get_canvas", filename="../sub/shot", doc=handle))
            want = os.path.join(os.path.realpath(self.root), "shot.png")
            self.assertEqual(config.current().output_dir, os.path.realpath(self.root))
            self.assertEqual(result["path"], want)
            self.assertEqual(result["doc"], handle)
            self.assertEqual((result["width"], result["height"], result["scale"]), (W, H, 1.0))
            self.assertEqual(result["region"], {"x": 0, "y": 0, "w": W, "h": H})
            full = self.render(handle)
            with open(want, "rb") as a, open(full["path"], "rb") as b:
                self.assertEqual(a.read(), b.read())
            self.assertEqual(self.load(result), expected(SHOWN))
            again = self.assertOk(run("get_canvas", filename="shot.PNG", doc=handle))
            self.assertEqual(again["path"], os.path.join(os.path.realpath(self.root), "shot.PNG"))
            self.assertError(run("get_canvas", filename="", doc=handle), "bad_request")


if __name__ == "__main__":
    unittest.main()
