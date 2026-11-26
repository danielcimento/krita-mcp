"""select and selection_to_layer (commands/selection.py), headless, through
dispatch.execute (P5.4, P5.5); plus the Selection facts they rely on and
``pixels.mask_alpha``'s exactness.

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. Undo goes through the ``undo``
command; ``state`` (test_history.py) compares the whole document.
"""

import contextlib
import shutil
import tempfile
import unittest

from krita import Selection

from helpers import scratch_document, settle, temp_config
from kritamcp import batch, nodes, pixels, registry
from kritamcp.commands import selection as selcmd
from test_dispatch import EnvelopeAssertions, run
from test_history import WIN, H, W, state
from test_pixels import pattern

CANVAS = (0, 0, W, H)


def rect_set(x, y, w, h):
    return {(i, j) for i in range(x, x + w) for j in range(y, y + h)}


def bounds_of(points):
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return {"x": min(xs), "y": min(ys), "w": max(xs) - min(xs) + 1, "h": max(ys) - min(ys) + 1}


def sel_values(doc, rect=WIN):
    """The document's selection over ``rect`` (zeros when there is none)."""
    sel = doc.selection()
    if sel is None:
        return bytes(rect[2] * rect[3])
    return pixels.as_bytes(sel.pixelData(*rect))


def selected(doc, rect=WIN):
    """The set of canvas points with a selection value > 0 inside ``rect``."""
    data = sel_values(doc, rect)
    x0, y0, w, _ = rect
    return {(x0 + i % w, y0 + i // w) for i, v in enumerate(data) if v}


def root_types(doc):
    return [c.type() for c in doc.rootNode().childNodes()]


def layer_bytes(doc, nid, rect=WIN):
    return pixels.as_bytes(nodes.node_by_id(doc, nid).pixelData(*rect))


def pixel(data, rect, x, y):
    i = 4 * ((y - rect[1]) * rect[2] + (x - rect[0]))
    return data[i:i + 4]


class Base(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-selection-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def build(self, doc):
        """Paint layer S with pattern pixels (partial alpha, colour under
        alpha 0) over (-3, -2, 20, 14), past the canvas; group G with X;
        an empty paint layer E. Returns (handle, ids) with a fresh journal."""
        handle = registry.handle_for(doc)
        result = self.assertOk(run("create_layers", doc=handle, specs=[
            {"name": "E"},
            {"name": "S"},
            {"key": "g", "name": "G", "type": "group"},
            {"name": "X", "parent": "$g"},
        ]))
        ids = {c["name"]: c["id"] for c in result["created"]}
        self.assertTrue(nodes.node_by_id(doc, ids["S"]).setPixelData(
            pattern(20, 14, 4), -3, -2, 20, 14))
        self.assertTrue(nodes.node_by_id(doc, ids["X"]).setPixelData(
            bytes((10, 20, 30, 255)) * 12, 20, 10, 4, 3))
        settle(doc)
        batch.JOURNALS.forget(handle)
        return handle, ids

    def select(self, handle, **params):
        return self.assertOk(run("select", doc=handle, **params))

    def assertSelection(self, doc, handle, result, expected):
        """``result`` (select's) and the document agree with the point set
        ``expected`` (within WIN), and get_document_info reports the bounds."""
        self.assertEqual(selected(doc), expected)
        self.assertEqual(result["pixel_count"], len(expected))
        self.assertEqual(result["bounds"], bounds_of(expected))
        info = self.assertOk(run("get_document_info", doc=handle))
        self.assertEqual(info["selection"], result["bounds"])
        if expected:
            self.assertIn("selectionmask", root_types(doc))
        else:
            self.assertIsNone(doc.selection())
            self.assertNotIn("selectionmask", root_types(doc))


# --- Krita's Selection, the facts commands/selection.py relies on -----------

class SelectionFactsTest(unittest.TestCase):
    def values(self, sel, n=6):
        return list(pixels.as_bytes(sel.pixelData(0, 0, n, 1)))

    def test_combination_ops_on_partial_values(self):
        a = Selection()
        a.select(0, 0, 4, 1, 100)
        for value, op, expected in (
                (200, "add", [100, 100, 255, 255, 200, 200]),       # clamped sum
                (100, "add", [100, 100, 200, 200, 100, 100]),
                (200, "subtract", [100, 100, 0, 0, 0, 0]),          # clamped difference
                (30, "subtract", [100, 100, 70, 70, 0, 0]),
                (200, "intersect", [0, 0, 100, 100, 0, 0])):        # minimum
            b = Selection()
            b.select(2, 0, 4, 1, value)
            c = a.duplicate()
            getattr(c, op)(b)
            self.assertEqual(self.values(c), expected, (op, value))
        self.assertEqual(self.values(a), [100] * 4 + [0, 0])  # duplicate() is a deep copy

    def test_select_adds_and_document_selection_is_live(self):
        s = Selection()
        s.select(0, 0, 2, 1, 255)
        s.select(4, 0, 1, 1, 255)
        self.assertEqual(self.values(s), [255, 255, 0, 0, 255, 0])
        with scratch_document(W, H, name="kmcp-sel-live") as doc:
            mine = Selection()
            mine.select(-3, -2, 8, 6, 255)
            doc.setSelection(mine)
            got = doc.selection()
            self.assertEqual((got.x(), got.y(), got.width(), got.height()), (-3, -2, 8, 6))
            got.select(20, 20, 1, 1, 255)  # the returned wrapper is the live selection
            self.assertEqual(doc.selection().width(), 24)
            doc.setSelection(None)
            self.assertIsNone(doc.selection())

    def test_modifiers_are_not_clipped(self):
        g = Selection()
        g.select(0, 0, 4, 4, 255)
        g.grow(3, 3)
        self.assertEqual((g.x(), g.y()), (-3, -3))
        s = Selection()
        s.select(0, 0, 6, 6, 255)
        s.shrink(2, 2, False)
        self.assertEqual((s.x(), s.y(), s.width(), s.height()), (2, 2, 2, 2))


class MaskAlphaTest(unittest.TestCase):
    def test_every_alpha_and_selection_value_is_exact(self):
        """All 65536 (alpha, s) pairs: round(a * s / 255) for keep,
        round(a * (255 - s) / 255) for the cut; colours untouched."""
        w, h = 256, 256
        data = bytearray(4 * w * h)
        data[0::4] = bytes(range(256)) * 256
        data[1::4] = bytes(255 - (i % 256) for i in range(w * h))
        data[2::4] = bytes((i * 7) % 256 for i in range(w * h))
        data[3::4] = bytes(range(256)) * 256
        data = bytes(data)
        mask = b"".join(bytes([s]) * 256 for s in range(256))
        for keep in (True, False):
            out, new_alpha = pixels.mask_alpha(data, mask, w, h, keep=keep)
            expected = bytes(((a * (s if keep else 255 - s)) + 127) // 255
                             for s in range(256) for a in range(256))
            self.assertEqual(new_alpha, expected, keep)
            self.assertEqual(out[3::4], expected)
            for c in range(3):
                self.assertEqual(out[c::4], data[c::4], (keep, c))

    def test_strips(self):
        saved = pixels._STRIP_PIXELS
        pixels._STRIP_PIXELS = 7  # several strips, rows of 3
        try:
            data, mask = pattern(3, 5, 2), bytes((i * 53) % 256 for i in range(15))
            out, _ = pixels.mask_alpha(data, mask, 3, 5)
        finally:
            pixels._STRIP_PIXELS = saved
        self.assertEqual(out, pixels.mask_alpha(data, mask, 3, 5)[0])
        self.assertEqual(out[3::4], bytes((a * s + 127) // 255 for a, s in zip(data[3::4], mask)))


# --- select -------------------------------------------------------------------

class SelectTest(Base):
    def test_rect_with_every_op(self):
        with scratch_document(W, H, name="kmcp-sel-rect") as doc:
            handle, _ = self.build(doc)
            a, b = rect_set(2, 3, 6, 5), rect_set(5, 6, 6, 4)
            cases = [("replace", a), ("add", a | b), ("subtract", a - b), ("intersect", a & b)]
            for op, expected in cases:
                self.select(handle, mode="rect", x=2, y=3, w=6, h=5)
                result = self.select(handle, mode="rect", op=op, x=5, y=6, w=6, h=4)
                self.assertEqual((result["mode"], result["op"]), ("rect", op))
                self.assertSelection(doc, handle, result, b if op == "replace" else expected)
                self.assertEqual(result["warnings"], [])
            # subtract / intersect with nothing selected select nothing
            for op in ("subtract", "intersect"):
                self.select(handle, mode="none")
                result = self.select(handle, mode="rect", op=op, x=5, y=6, w=6, h=4)
                self.assertSelection(doc, handle, result, set())
                self.assertEqual([w["code"] for w in result["warnings"]], ["empty_selection"])
            self.select(handle, mode="none")
            result = self.select(handle, mode="rect", op="add", x=5, y=6, w=6, h=4)
            self.assertSelection(doc, handle, result, b)
            # Selections may extend past the canvas; Krita keeps them.
            result = self.select(handle, mode="rect", x=-3, y=-2, w=8, h=6)
            self.assertSelection(doc, handle, result, rect_set(-3, -2, 8, 6))
            self.assertEqual(result["bounds"], {"x": -3, "y": -2, "w": 8, "h": 6})
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)  # not in the agent journal

    def test_all_none_and_the_mask_node(self):
        with scratch_document(W, H, name="kmcp-sel-all") as doc:
            handle, ids = self.build(doc)
            listed = lambda: [l["type"] for l in  # noqa: E731
                              self.assertOk(run("list_layers", doc=handle))["layers"]]
            before = listed()
            result = self.select(handle, mode="all")
            self.assertSelection(doc, handle, result, rect_set(*CANVAS))
            self.assertEqual(result["bounds"], {"x": 0, "y": 0, "w": W, "h": H})
            self.assertIn("selectionmask", root_types(doc))
            self.assertEqual(listed(), before)  # list_layers never lists the mask
            self.assertEqual(nodes.build_index(doc).by_name("Selection Mask"), [])
            result = self.select(handle, mode="none")
            self.assertEqual((result["bounds"], result["pixel_count"]), (None, 0))
            self.assertSelection(doc, handle, result, set())
            # all + intersect clips a selection to the canvas
            self.select(handle, mode="rect", x=-3, y=-2, w=8, h=6)
            result = self.select(handle, mode="all", op="intersect")
            self.assertSelection(doc, handle, result, rect_set(0, 0, 5, 4))

    def test_invert(self):
        with scratch_document(W, H, name="kmcp-sel-invert") as doc:
            handle, _ = self.build(doc)
            canvas = rect_set(*CANVAS)
            self.select(handle, mode="rect", x=2, y=2, w=3, h=3)
            result = self.select(handle, mode="invert")
            self.assertSelection(doc, handle, result, canvas - rect_set(2, 2, 3, 3))
            result = self.select(handle, mode="invert")
            self.assertSelection(doc, handle, result, rect_set(2, 2, 3, 3))
            # past the canvas: only the canvas is inverted, the rest dropped
            self.select(handle, mode="rect", x=-2, y=-2, w=5, h=5)
            result = self.select(handle, mode="invert")
            self.assertSelection(doc, handle, result, canvas - rect_set(0, 0, 3, 3))
            self.select(handle, mode="none")
            result = self.select(handle, mode="invert")  # nothing inverted is everything
            self.assertSelection(doc, handle, result, canvas)
            self.select(handle, mode="all")
            result = self.select(handle, mode="invert")
            self.assertSelection(doc, handle, result, set())
            for op in ("add", "subtract", "intersect"):
                err = self.assertError(run("select", doc=handle, mode="invert", op=op),
                                       "bad_request")
                self.assertEqual(err["details"]["param"], "op")
            self.assertError(run("select", doc=handle, mode="none", op="add"), "bad_request")

    def test_polygon(self):
        with scratch_document(W, H, name="kmcp-sel-poly") as doc:
            handle, _ = self.build(doc)
            # A polygon on pixel corners is exactly the rectangle, either way.
            corners = [[2, 3], [8, 3], [8, 8], [2, 8]]
            for aa in (True, False):
                result = self.select(handle, mode="polygon", points=corners, antialias=aa)
                self.assertSelection(doc, handle, result, rect_set(2, 3, 6, 5))
                self.assertEqual(set(sel_values(doc)), {0, 255})
            # A triangle: partial edge values with antialias, binary without.
            tri = [[1, 1], [20, 2], [6, 18]]
            got = {}
            for aa in (True, False):
                result = self.select(handle, mode="polygon", points=tri, antialias=aa)
                values = sel_values(doc)
                got[aa] = values
                self.assertEqual(result["pixel_count"], len(values) - values.count(0))
                self.assertEqual(self.assertOk(run("get_document_info", doc=handle))["selection"],
                                 result["bounds"])
            partial = set(got[True]) - {0, 255}
            self.assertTrue(partial, "antialiased edges have partial values")
            self.assertEqual(set(got[False]), {0, 255})
            inside = (8 - WIN[1]) * WIN[2] + (8 - WIN[0])
            self.assertEqual((got[True][inside], got[False][inside]), (255, 255))
            # Fill rule: winding, so a pentagram's centre is selected.
            star = [[16, 2], [22, 20], [7, 9], [25, 9], [10, 20]]
            self.select(handle, mode="polygon", points=star, antialias=False)
            self.assertIn((16, 11), selected(doc))
            # Points past the canvas and fractional points are fine.
            result = self.select(handle, mode="polygon", points=[[-4, -4], [3.5, -4], [-4, 3.5]],
                                 antialias=False)
            self.assertLess(result["bounds"]["x"], 0)

    def test_alpha_of(self):
        with scratch_document(W, H, name="kmcp-sel-alpha") as doc:
            handle, ids = self.build(doc)
            rect = (-3, -2, 20, 14)
            alpha = pattern(20, 14, 4)[3::4]
            for threshold in (1, 8, 128, 255):
                result = self.select(handle, mode="alpha_of", layer="S", threshold=threshold)
                expected = {(rect[0] + i % 20, rect[1] + i // 20)
                            for i, a in enumerate(alpha) if a >= threshold}
                self.assertSelection(doc, handle, result, expected)
                self.assertEqual(set(sel_values(doc)) - {0}, {255})  # binary
                if threshold == 1:
                    self.assertLess(result["bounds"]["x"], 0)  # past the canvas too
            # default threshold 1
            result = self.select(handle, mode="alpha_of", layer=ids["S"])
            self.assertEqual(result["pixel_count"], len(alpha) - alpha.count(0))
            # a group: its projection
            result = self.select(handle, mode="alpha_of", layer="G")
            self.assertSelection(doc, handle, result, rect_set(20, 10, 4, 3))
            # an empty layer selects nothing
            result = self.select(handle, mode="alpha_of", layer="E")
            self.assertSelection(doc, handle, result, set())
            mask = doc.createTransparencyMask("M")
            self.assertTrue(nodes.node_by_id(doc, ids["S"]).addChildNode(mask, None))
            self.assertError(run("select", doc=handle, mode="alpha_of", layer="S/M"), "conflict")
            self.assertError(run("select", doc=handle, mode="alpha_of", layer="Nope"),
                             "not_found")
            self.assertError(run("select", doc=handle, mode="alpha_of"), "bad_request")
            for t in (0, 256, 1.5, True):
                self.assertError(run("select", doc=handle, mode="alpha_of", layer="S",
                                     threshold=t), "bad_request")

    def test_grow_shrink_feather(self):
        with scratch_document(W, H, name="kmcp-sel-mod") as doc:
            handle, _ = self.build(doc)
            result = self.select(handle, mode="rect", x=5, y=5, w=4, h=4, grow=2)
            self.assertEqual((result["bounds"], result["pixel_count"]),
                             ({"x": 3, "y": 3, "w": 8, "h": 8}, 60))
            result = self.select(handle, mode="rect", x=5, y=5, w=6, h=6, shrink=2)
            self.assertSelection(doc, handle, result, rect_set(7, 7, 2, 2))
            result = self.select(handle, mode="rect", x=5, y=5, w=6, h=6, feather=2)
            self.assertEqual(result["bounds"], {"x": 3, "y": 3, "w": 10, "h": 10})
            self.assertTrue(set(sel_values(doc)) - {0, 255})  # soft edges
            # Order: grow, then shrink, then feather (shrinking 2x2 first would empty it).
            result = self.select(handle, mode="rect", x=5, y=5, w=2, h=2, grow=1, shrink=1,
                                 feather=1)
            ref = Selection()
            ref.select(5, 5, 2, 2, 255)
            ref.grow(1, 1)
            ref.shrink(1, 1, False)
            ref.feather(1)
            self.assertEqual(sel_values(doc), pixels.as_bytes(ref.pixelData(*WIN)))
            self.assertGreater(result["pixel_count"], 0)
            # Modifiers apply to the combined result.
            self.select(handle, mode="rect", x=2, y=2, w=3, h=3)
            result = self.select(handle, mode="rect", op="add", x=10, y=2, w=3, h=3, grow=1)
            self.assertEqual(result["bounds"], {"x": 1, "y": 1, "w": 13, "h": 5})
            result = self.select(handle, mode="rect", x=5, y=5, w=4, h=4, shrink=5)
            self.assertSelection(doc, handle, result, set())
            for key in ("grow", "shrink", "feather"):
                for bad in (0, 501, 2.0, True):
                    err = self.assertError(run("select", doc=handle, mode="all", **{key: bad}),
                                           "bad_request")
                    self.assertEqual(err["details"]["param"], key)
                self.assertError(run("select", doc=handle, mode="none", **{key: 1}),
                                 "bad_request")

    def test_validation(self):
        with scratch_document(W, H, name="kmcp-sel-bad") as doc:
            handle, _ = self.build(doc)
            self.select(handle, mode="rect", x=1, y=1, w=2, h=2)
            before = sel_values(doc)
            bad = [
                ({}, "bad_request", "mode"),
                ({"mode": "lasso"}, "bad_request", "mode"),
                ({"mode": "rect", "x": 0, "y": 0, "w": 2}, "bad_request", "h"),
                ({"mode": "rect", "x": 0, "y": 0, "w": 0, "h": 2}, "bad_request", "w"),
                ({"mode": "rect", "x": 0.5, "y": 0, "w": 2, "h": 2}, "bad_request", "x"),
                ({"mode": "rect", "x": 0, "y": 0, "w": 2, "h": 2, "op": "xor"}, "bad_request",
                 "op"),
                ({"mode": "rect", "x": 0, "y": 0, "w": 20000, "h": 2}, "too_large", None),
                ({"mode": "rect", "x": 0, "y": 0, "w": 9000, "h": 9000}, "too_large", None),
                ({"mode": "polygon", "points": [[0, 0], [1, 1]]}, "bad_request", "points"),
                ({"mode": "polygon"}, "bad_request", "points"),
                ({"mode": "polygon", "points": [[0, 0], [1, 1], [2]]}, "bad_request", "points"),
                ({"mode": "polygon", "points": [[0, 0], [1, 1], [True, 2]]}, "bad_request",
                 "points"),
                ({"mode": "polygon", "points": [[0, 0], [5, 0], [0, 5]], "antialias": "yes"},
                 "bad_request", "antialias"),
                ({"mode": "polygon", "points": [[0, 0], [20000, 0], [0, 5]]}, "too_large", None),
            ]
            for params, code, param in bad:
                err = self.assertError(run("select", doc=handle, **params), code)
                if param is not None:
                    self.assertEqual(err["details"]["param"], param, params)
            # Strict keys, per mode.
            for params in ({"mode": "polygon", "points": [[0, 0], [5, 0], [0, 5]], "x": 1},
                           {"mode": "rect", "x": 0, "y": 0, "w": 2, "h": 2, "antialias": True},
                           {"mode": "all", "layer": "S"}, {"mode": "none", "threshold": 3},
                           {"mode": "all", "colour": 1}):
                err = self.assertError(run("select", doc=handle, **params), "bad_request")
                self.assertTrue(err["details"]["unknown"], params)
            self.assertEqual(sel_values(doc), before)  # nothing changed
            # Explicit nulls mean "not given".
            result = self.select(handle, mode="all", op=None, grow=None, points=None)
            self.assertEqual(result["pixel_count"], W * H)
            # Degenerate polygon: nothing selected.
            result = self.select(handle, mode="polygon", points=[[0, 0], [5, 0], [9, 0]])
            self.assertEqual(result["pixel_count"], 0)


# --- selection_to_layer ---------------------------------------------------------

SRC_RECT = (-3, -2, 20, 14)


class SelectionToLayerTest(Base):
    def s2l(self, handle, **params):
        return self.assertOk(run("selection_to_layer", doc=handle, **params))

    def expected(self, src, mask_of, keep):
        """The exact pixels over WIN: ``src`` (bytes over WIN) with alpha
        scaled by ``mask_of`` (bytes over WIN), all four bytes zeroed where
        the new alpha is 0 (copy) or where s > 0 and it is 0 (cut)."""
        out = bytearray(len(src))
        for i in range(len(mask_of)):
            p = src[4 * i:4 * i + 4]
            s = mask_of[i]
            a = (p[3] * (s if keep else 255 - s) + 127) // 255
            if keep:
                out[4 * i:4 * i + 4] = bytes(4) if a == 0 else p[:3] + bytes([a])
            elif s == 0:
                out[4 * i:4 * i + 4] = p
            else:
                out[4 * i:4 * i + 4] = bytes(4) if a == 0 else p[:3] + bytes([a])
        return bytes(out)

    def test_copy(self):
        with scratch_document(W, H, name="kmcp-s2l-copy") as doc:
            handle, ids = self.build(doc)
            src = layer_bytes(doc, ids["S"])
            self.select(handle, mode="rect", x=2, y=1, w=8, h=6)
            before_sel = sel_values(doc)
            result = self.s2l(handle, source="S", name="Part", cut=False)
            self.assertEqual((result["cut"], result["dry_run"], result["source"]),
                             (False, False, ids["S"]))
            self.assertEqual(result["cut_rect"], {"x": 2, "y": 1, "w": 8, "h": 6})
            layer = result["layer"]
            self.assertEqual((layer["name"], layer["type"], layer["parent"]),
                             ("Part", "paintlayer", None))
            names = [l["name"] for l in self.assertOk(run("list_layers", doc=handle))["layers"]
                     if l["parent"] is None]
            self.assertEqual(names, ["G", "Part", "S", "E", "Background"])  # above the source
            new = layer_bytes(doc, layer["id"])
            self.assertEqual(new, self.expected(src, before_sel, keep=True))
            inside = rect_set(2, 1, 8, 6)
            for x, y in inside:  # sel 255: the source's pixel, unless its alpha is 0
                p = pixel(src, WIN, x, y)
                self.assertEqual(pixel(new, WIN, x, y), p if p[3] else bytes(4))
            self.assertEqual(layer_bytes(doc, ids["S"]), src)  # source unchanged
            self.assertEqual(sel_values(doc), before_sel)  # selection unchanged
            self.assertEqual(result["copied"], pixels.count_alpha(new))
            self.assertEqual(result["warnings"], [])
            self.assertEqual(len(batch.JOURNALS.get(handle)), 1)

    def test_cut_undo_redo(self):
        with scratch_document(W, H, name="kmcp-s2l-cut") as doc:
            handle, ids = self.build(doc)
            src = layer_bytes(doc, ids["S"])
            self.select(handle, mode="rect", x=-1, y=3, w=7, h=5)
            mask = sel_values(doc)
            before = state(doc, handle)
            result = self.s2l(handle, source="S", name="Cut")
            self.assertTrue(result["cut"])
            self.assertEqual(result["cut_rect"], {"x": -1, "y": 3, "w": 7, "h": 5})
            new_id = result["layer"]["id"]
            self.assertEqual(layer_bytes(doc, new_id), self.expected(src, mask, keep=True))
            cut = layer_bytes(doc, ids["S"])
            self.assertEqual(cut, self.expected(src, mask, keep=False))
            inside = rect_set(-1, 3, 7, 5)
            for x, y in rect_set(*SRC_RECT):
                want = bytes(4) if (x, y) in inside else pixel(src, WIN, x, y)
                self.assertEqual(pixel(cut, WIN, x, y), want, (x, y))
            after = state(doc, handle)

            undone = self.assertOk(run("undo", doc=handle))
            self.assertEqual(undone["undone"]["label"], "selection_to_layer")
            self.assertEqual(state(doc, handle), before)
            self.assertIsNone(nodes.node_by_id(doc, new_id))
            self.assertEqual(layer_bytes(doc, ids["S"]), src)
            self.assertOk(run("redo", doc=handle))
            self.assertEqual(state(doc, handle), after)  # same ids, same pixels
            self.assertOk(run("undo", doc=handle))
            self.assertEqual(state(doc, handle), before)

    def test_feathered_selection(self):
        with scratch_document(W, H, name="kmcp-s2l-feather") as doc:
            handle, ids = self.build(doc)
            opaque = bytes((40, 80, 120, 255)) * (W * H)
            self.assertTrue(nodes.node_by_id(doc, ids["E"]).setPixelData(opaque, 0, 0, W, H))
            settle(doc)
            src = layer_bytes(doc, ids["E"])
            self.select(handle, mode="rect", x=8, y=6, w=12, h=10, feather=3)
            mask = sel_values(doc)
            self.assertTrue(set(mask) - {0, 255})
            result = self.s2l(handle, source="E", name="Soft")
            new = layer_bytes(doc, result["layer"]["id"])
            cut = layer_bytes(doc, ids["E"])
            # Exact, partial values included (mask_alpha is exact).
            self.assertEqual(new, self.expected(src, mask, keep=True))
            self.assertEqual(cut, self.expected(src, mask, keep=False))
            self.assertEqual(set(new[3::4]) & set(range(1, 255)),
                             set(mask) & set(range(1, 255)))  # alpha = s on an opaque source
            for i, s in enumerate(mask):
                if 0 < s < 255:
                    self.assertLessEqual(abs(new[4 * i + 3] + cut[4 * i + 3] - 255), 1)

    def test_placement_names_and_dry_run(self):
        with scratch_document(W, H, name="kmcp-s2l-place") as doc:
            handle, ids = self.build(doc)
            self.select(handle, mode="all")
            src = layer_bytes(doc, ids["S"])
            dry = self.s2l(handle, source="S", name="S", dry_run=True)
            self.assertEqual((dry["dry_run"], dry["cut_rect"]),
                             (True, {"x": 0, "y": 0, "w": 17, "h": 12}))
            self.assertEqual(dry["would_create"], {"name": "S", "type": "paintlayer",
                                                   "parent": "/", "above": ids["S"]})
            self.assertEqual([w["code"] for w in dry["warnings"]], ["duplicate_name"])
            self.assertEqual(layer_bytes(doc, ids["S"]), src)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

            result = self.s2l(handle, source="S", name="InG", parent="G", cut=False)
            self.assertEqual(result["layer"]["parent"], ids["G"])
            self.assertEqual(self.assertOk(run("list_layers", doc=handle, root="G"))
                             ["layers"][0]["children"][0], result["layer"]["id"])
            result = self.s2l(handle, source="S", name="Low", above="E", cut=False)
            names = [l["name"] for l in self.assertOk(run("list_layers", doc=handle))["layers"]
                     if l["parent"] is None]
            self.assertEqual(names, ["G", "S", "Low", "E", "Background"])
            dup = self.s2l(handle, source="S", name="Low", cut=False)
            self.assertEqual([w["code"] for w in dup["warnings"]], ["duplicate_name"])
            self.assertEqual(dup["warnings"][0]["others"], [result["layer"]["id"]])
            err = self.assertError(run("selection_to_layer", doc=handle, source="S", name="Z",
                                       above="X"), "bad_request")
            self.assertEqual(err["details"]["param"], "above")
            for params, param in (({"source": "S"}, "name"), ({"name": "Z"}, "source"),
                                  ({"source": "S", "name": ""}, "name"),
                                  ({"source": "S", "name": "Z", "cut": 1}, "cut"),
                                  ({"source": "S", "name": "Z", "dry_run": "no"}, "dry_run")):
                err = self.assertError(run("selection_to_layer", doc=handle, **params),
                                       "bad_request")
                self.assertEqual(err["details"]["param"], param, params)
            err = self.assertError(run("selection_to_layer", doc=handle, source="S", name="Z",
                                       mode="rect"), "bad_request")
            self.assertEqual(err["details"]["unknown"], ["mode"])
            self.assertError(run("selection_to_layer", doc=handle, source="Nope", name="Z"),
                             "not_found")

    def test_conflicts(self):
        with scratch_document(W, H, name="kmcp-s2l-conflict") as doc:
            handle, ids = self.build(doc)
            err = self.assertError(run("selection_to_layer", doc=handle, source="S", name="Z"),
                                   "conflict")
            self.assertIn("No selection", err["message"])
            self.select(handle, mode="rect", x=20, y=15, w=4, h=4)
            err = self.assertError(run("selection_to_layer", doc=handle, source="S", name="Z"),
                                   "conflict")
            self.assertIn("doesn't overlap", err["message"])
            self.assertError(run("selection_to_layer", doc=handle, source="E", name="Z"),
                             "conflict")  # an empty layer has no bounds
            err = self.assertError(run("selection_to_layer", doc=handle, source="G", name="Z"),
                                   "conflict")
            self.assertEqual(err["details"]["type"], "grouplayer")
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)
            names = [l["name"] for l in self.assertOk(run("list_layers", doc=handle))["layers"]]
            self.assertNotIn("Z", names)

    def test_nothing_visible_under_the_selection(self):
        with scratch_document(W, H, name="kmcp-s2l-empty") as doc:
            handle, ids = self.build(doc)
            data = bytes((9, 9, 9, 0)) * 16  # colour under alpha 0 only
            node = nodes.node_by_id(doc, ids["E"])
            self.assertTrue(node.setPixelData(data, 2, 2, 4, 4))
            # Visible pixels outside the selection, around it (Krita's bounds ignore alpha 0).
            for xy in ((1, 1), (7, 7)):
                self.assertTrue(node.setPixelData(bytes((1, 2, 3, 255)), *xy, 1, 1))
            settle(doc)
            self.select(handle, mode="rect", x=2, y=2, w=4, h=4)
            result = self.s2l(handle, source="E", name="Nothing")
            self.assertEqual(result["copied"], 0)
            self.assertEqual([w["code"] for w in result["warnings"]], ["nothing_copied"])
            self.assertEqual(set(layer_bytes(doc, result["layer"]["id"])), {0})
            self.assertEqual(set(layer_bytes(doc, ids["E"], (2, 2, 4, 4))), {0})  # colour cut too
            self.assertEqual(pixel(layer_bytes(doc, ids["E"]), WIN, 7, 7), bytes((1, 2, 3, 255)))
            self.assertOk(run("undo", doc=handle))
            self.assertEqual(layer_bytes(doc, ids["E"], (2, 2, 4, 4)), data)

    def test_split_pixels_keeps_unselected_bytes(self):
        old = pattern(5, 4, 7)
        mask = bytes([0, 255, 128, 3, 0] * 4)
        copy, cut = selcmd.split_pixels(old, mask, 5, 4)
        for i, s in enumerate(mask):
            p = old[4 * i:4 * i + 4]
            if s == 0:
                self.assertEqual(cut[4 * i:4 * i + 4], p)
                self.assertEqual(copy[4 * i:4 * i + 4], bytes(4))
            if s == 255:
                self.assertEqual(copy[4 * i:4 * i + 4], p if p[3] else bytes(4))
                self.assertEqual(cut[4 * i:4 * i + 4], bytes(4))
