"""transform_layer (commands/transform.py), headless, through dispatch.execute
(P5.6), plus the Krita transform facts it relies on.

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. ``state`` (test_history.py)
compares the whole document: list_layers, every layer's pixels over a
window wider than the canvas, and the projection.
"""

import contextlib
import math
import shutil
import tempfile
import unittest

from PyQt5.QtCore import QPointF

from helpers import scratch_document, settle, temp_config
from kritamcp import batch, nodes, pixels, registry, transformplan
from kritamcp.commands import transform as tcmd
from test_dispatch import EnvelopeAssertions, run
from test_history import H, W, state
from test_pixels import pattern


def framed(w, h, seed=0):
    """``pattern`` (partial alpha, colour under alpha 0) with an opaque
    1-pixel frame, so the bounds are the whole rectangle and every colour
    under alpha 0 lies inside them."""
    data = bytearray(pattern(w, h, seed))
    for y in range(h):
        for x in range(w):
            if x in (0, w - 1) or y in (0, h - 1):
                data[4 * (y * w + x) + 3] = 255
    return bytes(data)


def px(data, w, x, y):
    return data[4 * (y * w + x):4 * (y * w + x) + 4]


def remap(data, w, h, nw, nh, src_of):
    """A new ``nw`` x ``nh`` buffer whose pixel (x, y) is ``data``'s pixel
    ``src_of(x, y)``."""
    out = bytearray(4 * nw * nh)
    for y in range(nh):
        for x in range(nw):
            sx, sy = src_of(x, y)
            out[4 * (y * nw + x):4 * (y * nw + x) + 4] = px(data, w, sx, sy)
    return bytes(out)


def mirror(data, w, h, axis):
    if axis == "h":
        return remap(data, w, h, w, h, lambda x, y: (w - 1 - x, y))
    return remap(data, w, h, w, h, lambda x, y: (x, h - 1 - y))


def turn_cw(data, w, h):
    """A clockwise quarter turn: new (x, y) is old (y, h - 1 - x)."""
    return remap(data, w, h, h, w, lambda x, y: (y, h - 1 - x))


def visible(data):
    """``data`` with every alpha-0 pixel zeroed: Krita's resampling (also
    NearestNeighbor and quarter turns) drops the colour under alpha 0."""
    out = bytearray(data)
    for i in range(3, len(out), 4):
        if out[i] == 0:
            out[i - 3:i + 1] = bytes(4)
    return bytes(out)


def xywh(r):
    return tuple(r[k] for k in "xywh")


def bounds(doc, nid):
    r = nodes.node_by_id(doc, nid).bounds()
    return r.x(), r.y(), r.width(), r.height()


def rect(r):
    return {"x": r[0], "y": r[1], "w": r[2], "h": r[3]}


def read(doc, nid, r):
    return pixels.as_bytes(nodes.node_by_id(doc, nid).pixelData(*r))


class Base(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-transform-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def build(self, doc):
        """P (framed 8x6 at (5, 3)), S (framed 8x8 at (14, 8)), group G with
        A (framed 4x4 at (2, 14)) and B (framed 6x3 at (9, 18), multiply),
        an empty layer E. Returns (handle, ids, {name: (rect, data)}) with a
        fresh journal."""
        handle = registry.handle_for(doc)
        result = self.assertOk(run("create_layers", doc=handle, specs=[
            {"name": "E"},
            {"key": "g", "name": "G", "type": "group"},
            {"name": "A", "parent": "$g"},
            {"name": "B", "parent": "$g", "blend_mode": "multiply"},
            {"name": "S"},
            {"name": "P"},
        ]))
        ids = {c["name"]: c["id"] for c in result["created"]}
        data = {"P": ((5, 3, 8, 6), framed(8, 6, 1)), "S": ((14, 8, 8, 8), framed(8, 8, 2)),
                "A": ((2, 14, 4, 4), framed(4, 4, 3)), "B": ((9, 18, 6, 3), framed(6, 3, 4))}
        for name, (r, d) in data.items():
            self.assertTrue(nodes.node_by_id(doc, ids[name]).setPixelData(d, *r))
        settle(doc)
        batch.JOURNALS.forget(handle)
        return handle, ids, data

    def transform(self, handle, **params):
        return self.assertOk(run("transform_layer", doc=handle, **params))

    def assertUndoRedo(self, doc, handle, before):
        """Undo the newest transform: the state is ``before`` again; redo it:
        the state after the call again (same ids, pixels and bounds)."""
        after = state(doc, handle)
        self.assertNotEqual(after, before)
        undone = self.assertOk(run("undo", doc=handle))
        self.assertEqual(undone["undone"]["label"], "transform_layer")
        self.assertEqual(state(doc, handle), before)
        self.assertOk(run("redo", doc=handle))
        self.assertEqual(state(doc, handle), after)
        self.assertOk(run("undo", doc=handle))
        self.assertEqual(state(doc, handle), before)

    def assertNear(self, got, want, tol=1):
        for a, b in zip(got, want):
            self.assertLessEqual(abs(a - b), tol, (got, want))


# --- Krita's transforms, the facts commands/transform.py relies on ----------

class TransformFactsTest(unittest.TestCase):
    def test_native_transforms(self):
        with scratch_document(64, 48, name="kmcp-transform-facts") as doc:
            n = doc.createNode("A", "paintlayer")
            doc.rootNode().addChildNode(n, None)
            sq = framed(8, 8, 5)
            self.assertTrue(n.setPixelData(sq, 10, 6, 8, 8))
            settle(doc)
            nid = nodes.node_id(n)
            # A quarter turn is an exact clockwise permutation about the centre.
            n.rotateNode(math.pi / 2)
            settle(doc)
            self.assertEqual(bounds(doc, nid), (10, 6, 8, 8))
            self.assertEqual(visible(read(doc, nid, (10, 6, 8, 8))), visible(turn_cw(sq, 8, 8)))
            # scaleNode keeps the origin fixed; NearestNeighbor doubles pixels.
            n.scaleNode(QPointF(10, 6), 16, 16, "NearestNeighbor")
            settle(doc)
            self.assertEqual(bounds(doc, nid), (10, 6, 16, 16))
            big = read(doc, nid, (10, 6, 16, 16))
            turned = turn_cw(sq, 8, 8)
            self.assertEqual(px(big, 16, 0, 0), px(turned, 8, 0, 0))
            self.assertEqual(px(big, 16, 15, 15), px(turned, 8, 7, 7))
            # move is relative to position(), which a rotate changes.
            p = n.position()
            n.move(p.x() + 3, p.y() - 2)
            settle(doc)
            self.assertEqual(bounds(doc, nid), (13, 4, 16, 16))

    def test_group_transforms_and_move(self):
        with scratch_document(64, 48, name="kmcp-transform-group-facts") as doc:
            g = doc.createGroupLayer("G")
            doc.rootNode().addChildNode(g, None)
            c1, c2 = doc.createNode("c1", "paintlayer"), doc.createNode("c2", "paintlayer")
            g.addChildNode(c1, None)
            g.addChildNode(c2, None)
            self.assertTrue(c1.setPixelData(framed(4, 4, 1), 30, 30, 4, 4))
            self.assertTrue(c2.setPixelData(framed(4, 4, 2), 40, 34, 4, 4))
            settle(doc)
            i1, i2, gid = nodes.node_id(c1), nodes.node_id(c2), nodes.node_id(g)
            # Node.move on a group changes nothing visible.
            g.move(g.position().x() + 2, g.position().y() + 1)
            settle(doc)
            self.assertEqual((bounds(doc, i1), bounds(doc, i2)),
                             ((30, 30, 4, 4), (40, 34, 4, 4)))
            # scaleNode on a group scales every child about the same origin.
            g.scaleNode(QPointF(32, 31), 28, 16, "NearestNeighbor")
            settle(doc)
            self.assertEqual((bounds(doc, i1), bounds(doc, i2), bounds(doc, gid)),
                             ((28, 29, 8, 8), (48, 37, 8, 8), (28, 29, 28, 16)))
            # rotateNode on a group turns every child about the group's centre.
            g.rotateNode(math.pi / 2)
            settle(doc)
            self.assertEqual((bounds(doc, i1), bounds(doc, i2)), ((42, 23, 8, 8), (34, 43, 8, 8)))
            # A hidden pass-through group reports no bounds.
            g.setPassThroughMode(True)
            g.setVisible(False)
            c2.setPixelData(framed(2, 2, 3), 0, 0, 2, 2)
            settle(doc)
            self.assertEqual(bounds(doc, gid)[2:], (0, 0))
            g.setPassThroughMode(False)
            settle(doc)
            self.assertNotEqual(bounds(doc, gid)[2:], (0, 0))


# --- transform_layer ----------------------------------------------------------

class TransformLayerTest(Base):
    def test_translate_is_exact(self):
        with scratch_document(W, H, name="kmcp-transform-translate") as doc:
            handle, ids, data = self.build(doc)
            r, src = data["P"]
            before = state(doc, handle)
            result = self.transform(handle, id="P", translate=[3, -2])
            self.assertEqual((result["layer"], result["type"], result["dry_run"]),
                             (ids["P"], "paintlayer", False))
            self.assertEqual(result["bounds_before"], rect(r))
            self.assertEqual(result["bounds_after"], rect((8, 1, 8, 6)))
            self.assertEqual(result["applied"], [{"op": "translate", "by": [3, -2]}])
            self.assertEqual(read(doc, ids["P"], (8, 1, 8, 6)), src)  # bit for bit
            self.assertEqual(batch.JOURNALS.get(handle).peek().nbytes, 0)  # no snapshot
            self.assertUndoRedo(doc, handle, before)

    def test_flip_is_exact_and_involutive(self):
        with scratch_document(W, H, name="kmcp-transform-flip") as doc:
            handle, ids, data = self.build(doc)
            r, src = data["S"]
            before = state(doc, handle)
            for axis in ("h", "v"):
                result = self.transform(handle, id="S", flip=axis)
                self.assertEqual(result["bounds_after"], rect(r))
                self.assertEqual(result["applied"],
                                 [{"op": "flip", "axis": axis, "shift": [0, 0]}])
                self.assertEqual(read(doc, ids["S"], r), mirror(src, 8, 8, axis))
                self.assertUndoRedo(doc, handle, before)
                self.transform(handle, id="S", flip=axis)
                self.transform(handle, id="S", flip=axis)
                self.assertEqual(state(doc, handle), before)  # involutive, bit for bit
            # About a pivot: mirrored about x = 12, the layer lands at 2 * 12 - 22 = 2.
            result = self.transform(handle, id="S", flip="h", pivot=[12, 0])
            self.assertEqual(result["bounds_after"], rect((2, 8, 8, 8)))
            self.assertEqual(result["applied"][0]["shift"], [-12, 0])
            self.assertEqual(read(doc, ids["S"], (2, 8, 8, 8)), mirror(src, 8, 8, "h"))
            self.assertEqual(set(read(doc, ids["S"], (10, 8, 12, 8))), {0})  # cleared
            self.assertUndoRedo(doc, handle, before)

    def test_scale(self):
        with scratch_document(W, H, name="kmcp-transform-scale") as doc:
            handle, ids, data = self.build(doc)
            r, src = data["P"]
            before = state(doc, handle)
            dry = self.transform(handle, id="P", scale=2, resample="NearestNeighbor",
                                 dry_run=True)
            self.assertEqual(dry["bounds_predicted"], rect((1, 0, 16, 12)))
            self.assertEqual(dry["would_apply"][0]["size"], {"w": 16, "h": 12})
            self.assertEqual(state(doc, handle), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)
            result = self.transform(handle, id="P", scale=2, resample="NearestNeighbor")
            self.assertEqual(result["bounds_after"], dry["bounds_predicted"])
            self.assertEqual(result["applied"], [{"op": "scale", "factor": [2.0, 2.0],
                                                  "size": {"w": 16, "h": 12},
                                                  "resample": "NearestNeighbor"}])
            big = read(doc, ids["P"], (1, 0, 16, 12))
            self.assertEqual(visible(big), visible(remap(src, 8, 6, 16, 12,
                                                         lambda x, y: (x // 2, y // 2))))
            self.assertUndoRedo(doc, handle, before)
            # About a custom pivot (it stays fixed), and about the centre, Bicubic.
            for factor, pivot in ((0.5, [14, 8]), ((0.5, 2), [22, 16]), (3, "center"),
                                  (0.5, [5, 3])):
                result = self.transform(handle, id="S", scale=factor, pivot=pivot)
                spec = transformplan.parse({"scale": factor, "pivot": pivot})
                self.assertNear(xywh(result["bounds_after"]),
                                transformplan.predict(data["S"][0], spec)[1])
                self.assertOk(run("undo", doc=handle))
            self.assertEqual(state(doc, handle), before)
            result = self.transform(handle, id="S", scale=0.5, pivot=[14, 8])
            self.assertEqual(result["bounds_after"], rect((14, 8, 4, 4)))  # top-left kept

    def test_rotate(self):
        with scratch_document(W, H, name="kmcp-transform-rotate") as doc:
            handle, ids, data = self.build(doc)
            r, src = data["S"]
            before = state(doc, handle)
            # Positive degrees turn clockwise; a quarter turn permutes the pixels exactly.
            result = self.transform(handle, id="S", rotate=90)
            self.assertEqual(result["bounds_after"], rect(r))
            self.assertEqual(result["applied"], [{"op": "rotate", "degrees": 90.0,
                                                  "shift": [0, 0]}])
            turned = read(doc, ids["S"], r)
            self.assertEqual(visible(turned), visible(turn_cw(src, 8, 8)))
            self.assertEqual(px(turned, 8, 7, 0), px(src, 8, 0, 0))  # top-left -> top-right
            self.assertUndoRedo(doc, handle, before)
            for degrees, turns in ((180, 2), (270, 3), (-90, 3), (450, 1)):
                self.transform(handle, id="S", rotate=degrees)
                want = src
                for _ in range(turns):
                    want = turn_cw(want, 8, 8)
                self.assertEqual(visible(read(doc, ids["S"], r)), visible(want), degrees)
                self.assertOk(run("undo", doc=handle))
            self.assertEqual(state(doc, handle), before)
            # A non-square layer about its top-left corner: turned there, then shifted.
            # (5, 3, 8, 6) turned clockwise about (5, 3) covers x -1..5, y 3..11.
            result = self.transform(handle, id="P", rotate=90, pivot=[5, 3])
            self.assertEqual(result["applied"][0]["shift"], [-7, 1])
            self.assertEqual(result["bounds_after"], rect((-1, 3, 6, 8)))
            self.assertEqual(visible(read(doc, ids["P"], (-1, 3, 6, 8))),
                             visible(turn_cw(data["P"][1], 8, 6)))
            self.assertUndoRedo(doc, handle, before)
            # Any angle: the bounds as predicted, within a pixel.
            result = self.transform(handle, id="P", rotate=30)
            want = transformplan.predict(data["P"][0], transformplan.parse({"rotate": 30}))[1]
            self.assertNear(xywh(result["bounds_after"]), want)
            self.assertUndoRedo(doc, handle, before)

    def test_combined_order(self):
        with scratch_document(W, H, name="kmcp-transform-combined") as doc:
            handle, ids, data = self.build(doc)
            r, src = data["P"]
            before = state(doc, handle)
            params = {"flip": "v", "scale": 2, "rotate": 90, "translate": [2, 1],
                      "resample": "NearestNeighbor"}
            dry = self.transform(handle, id="P", dry_run=True, **params)
            self.assertEqual([s["op"] for s in dry["would_apply"]],
                             ["flip", "scale", "rotate", "translate"])
            self.assertEqual(dry["bounds_predicted"], rect((5, -1, 12, 16)))
            result = self.transform(handle, id="P", **params)
            self.assertEqual([s["op"] for s in result["applied"]],
                             ["flip", "scale", "rotate", "translate"])
            self.assertEqual(result["bounds_after"], dry["bounds_predicted"])
            got = read(doc, ids["P"], xywh(result["bounds_after"]))
            doubled = remap(mirror(src, 8, 6, "v"), 8, 6, 16, 12, lambda x, y: (x // 2, y // 2))
            self.assertEqual(visible(got), visible(turn_cw(doubled, 16, 12)))
            self.assertUndoRedo(doc, handle, before)

    def test_group(self):
        with scratch_document(W, H, name="kmcp-transform-group") as doc:
            handle, ids, data = self.build(doc)
            before = state(doc, handle)
            gbox = (2, 14, 13, 7)  # A (2, 14, 4, 4) and B (9, 18, 6, 3)
            self.assertEqual(bounds(doc, ids["G"]), gbox)
            group = nodes.node_by_id(doc, ids["G"])
            proj = pixels.as_bytes(group.projectionPixelData(*gbox))
            # Translate: every layer in it moves.
            result = self.transform(handle, id="G", translate=[4, -3])
            self.assertEqual(result["type"], "grouplayer")
            self.assertEqual(result["bounds_after"], rect((6, 11, 13, 7)))
            self.assertEqual((bounds(doc, ids["A"]), bounds(doc, ids["B"])),
                             ((6, 11, 4, 4), (13, 15, 6, 3)))
            self.assertUndoRedo(doc, handle, before)
            # Flip: the group's composite is mirrored exactly.
            result = self.transform(handle, id="G", flip="h")
            self.assertEqual(result["bounds_after"], rect(gbox))
            self.assertEqual((bounds(doc, ids["A"]), bounds(doc, ids["B"])),
                             ((11, 14, 4, 4), (2, 18, 6, 3)))
            got = pixels.as_bytes(nodes.node_by_id(doc, ids["G"]).projectionPixelData(*gbox))
            self.assertEqual(got, mirror(proj, 13, 7, "h"))
            self.assertUndoRedo(doc, handle, before)
            # Scale x2 about its top-left: the layers keep their layout.
            result = self.transform(handle, id="G", scale=2, pivot=[2, 14],
                                    resample="NearestNeighbor")
            self.assertEqual((bounds(doc, ids["A"]), bounds(doc, ids["B"])),
                             ((2, 14, 8, 8), (16, 22, 12, 6)))
            # A group's bounds are its projection's, clipped to the canvas (H = 24).
            self.assertEqual(result["bounds_after"], rect((2, 14, 26, H - 14)))
            self.assertUndoRedo(doc, handle, before)
            # A quarter turn about its centre, then about its top-left corner.
            result = self.transform(handle, id="G", rotate=90)
            self.assertNear(xywh(result["bounds_after"]), transformplan.rotated(gbox, 90))
            self.assertUndoRedo(doc, handle, before)
            result = self.transform(handle, id="G", rotate=90, pivot=[2, 14])
            # Turned clockwise about (2, 14): A covers x -2..2, y 14..18, B x -5..-2,
            # y 21..27 (the group's own bounds stop at the canvas edge).
            self.assertNear(bounds(doc, ids["A"]), (-2, 14, 4, 4))
            self.assertNear(bounds(doc, ids["B"]), (-5, 21, 3, 6))
            self.assertEqual(result["bounds_after"], rect((0, 14, 2, 4)))
            self.assertUndoRedo(doc, handle, before)

    def test_hidden_pass_through_group(self):
        with scratch_document(W, H, name="kmcp-transform-hidden-pt") as doc:
            handle, ids, data = self.build(doc)
            self.assertOk(run("update_layers", doc=handle, changes=[
                {"id": "G", "pass_through": True, "visible": False}]))
            batch.JOURNALS.forget(handle)
            before = state(doc, handle)
            result = self.transform(handle, id="G", scale=2, pivot=[2, 14],
                                    resample="NearestNeighbor")
            self.assertEqual(result["bounds_before"], rect((2, 14, 13, 7)))  # from its layers
            self.assertEqual((bounds(doc, ids["A"]), bounds(doc, ids["B"])),
                             ((2, 14, 8, 8), (16, 22, 12, 6)))
            listed = {l["id"]: l for l in self.assertOk(run("list_layers", doc=handle))["layers"]}
            self.assertEqual((listed[ids["G"]]["pass_through"], listed[ids["G"]]["visible"]),
                             (True, False))  # put back
            self.assertUndoRedo(doc, handle, before)

    def test_conflicts_and_validation(self):
        with scratch_document(W, H, name="kmcp-transform-errors") as doc:
            handle, ids, data = self.build(doc)
            mask = doc.createTransparencyMask("M")
            nodes.node_by_id(doc, ids["S"]).addChildNode(mask, None)
            settle(doc)
            before = state(doc, handle)
            err = self.assertError(run("transform_layer", doc=handle, id="S/M", flip="h"),
                                   "conflict")
            self.assertEqual(err["details"]["type"], "transparencymask")
            err = self.assertError(run("transform_layer", doc=handle, id="S", flip="h"),
                                   "conflict")
            self.assertIn("masks", err["message"])
            self.assertError(run("transform_layer", doc=handle, id="E", flip="h"), "conflict")
            self.assertOk(run("move_layers", doc=handle, moves=[{"id": "S", "parent": "G"}]))
            err = self.assertError(run("transform_layer", doc=handle, id="G", rotate=90),
                                   "conflict")
            self.assertEqual(err["details"]["types"], ["transparencymask"])
            self.assertOk(run("undo", doc=handle))
            for params, param in (({"flip": "h"}, "id"), ({"id": "P"}, "translate"),
                                  ({"id": "/", "flip": "h"}, "id"),
                                  ({"id": "P", "flip": "x"}, "flip"),
                                  ({"id": "P", "scale": 0}, "scale"),
                                  ({"id": "P", "rotate": "90"}, "rotate"),
                                  ({"id": "P", "translate": [1]}, "translate"),
                                  ({"id": "P", "flip": "h", "pivot": [1, "a"]}, "pivot"),
                                  ({"id": "P", "scale": 2, "resample": "Box"}, "resample"),
                                  ({"id": "P", "flip": "h", "dry_run": 1}, "dry_run")):
                err = self.assertError(run("transform_layer", doc=handle, **params),
                                       "bad_request")
                self.assertEqual(err["details"]["param"], param, params)
            err = self.assertError(run("transform_layer", doc=handle, id="P", skew=1),
                                   "bad_request")
            self.assertEqual(err["details"]["unknown"], ["skew"])
            self.assertError(run("transform_layer", doc=handle, id="Nope", flip="h"),
                             "not_found")
            with temp_config({"allowed_dirs": [self.root], "limits": {"max_canvas_dim": 64}}):
                self.assertError(run("transform_layer", doc=handle, id="P", scale=10),
                                 "too_large")
            self.assertEqual(state(doc, handle), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_failure_mid_apply_rolls_back(self):
        with scratch_document(W, H, name="kmcp-transform-rollback") as doc:
            handle, ids, data = self.build(doc)
            before = state(doc, handle)
            saved = tcmd._rotate

            def boom(*args):
                raise RuntimeError("injected")

            tcmd._rotate = boom
            try:
                err = self.assertError(run("transform_layer", doc=handle, id="G", flip="h",
                                           scale=2, rotate=30, translate=[1, 1]), "krita_error")
            finally:
                tcmd._rotate = saved
            self.assertIn("injected", err["message"])
            self.assertEqual(state(doc, handle), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_sixteen_bit_translate_only(self):
        with scratch_document(W, H, name="kmcp-transform-u16", color_depth="U16") as doc:
            handle = registry.handle_for(doc)
            created = self.assertOk(run("create_layers", doc=handle, specs=[{"name": "L"}]))
            nid = created["created"][0]["id"]
            node = nodes.node_by_id(doc, nid)
            self.assertTrue(node.setPixelData(bytes([255]) * (8 * 4 * 4), 2, 2, 4, 4))
            settle(doc)
            self.assertError(run("transform_layer", doc=handle, id="L", flip="h"), "conflict")
            result = self.transform(handle, id="L", translate=[1, 2])
            self.assertEqual(result["bounds_after"], rect((3, 4, 4, 4)))
            self.assertOk(run("undo", doc=handle))
            self.assertEqual(bounds(doc, nid), (2, 2, 4, 4))
