"""merge_layers (commands/merge.py), headless, through dispatch.execute (P5.7).

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. ``state`` (test_history.py)
compares the whole document: list_layers (ids included), every layer's
pixels over a window wider than the canvas, and the projection.
"""

import contextlib
import shutil
import tempfile
import unittest

from helpers import scratch_document, settle, temp_config
from kritamcp import batch, nodes, pixels, registry
from kritamcp.commands import merge as mcmd
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run
from test_history import H, W, state
from test_pixels import pattern


def opaque(w, h, seed=0):
    data = bytearray(pattern(w, h, seed))
    data[3::4] = bytes([255]) * (w * h)
    return bytes(data)


def projection(doc):
    settle(doc)
    return pixels.as_bytes(doc.pixelData(0, 0, W, H))


def group_projection(doc, nid):
    settle(doc)
    node = nodes.node_by_id(doc, nid)
    r = node.bounds()
    rect = (r.x(), r.y(), r.width(), r.height())
    return rect, pixels.as_bytes(node.projectionPixelData(*rect))


def layer_pixels(doc, nid, rect):
    return pixels.as_bytes(nodes.node_by_id(doc, nid).pixelData(*rect))


def max_diff(a, b):
    return max((abs(x - y) for x, y in zip(a, b)), default=0)


def names_top_down(doc, handle, parent=None):
    layers = run("list_layers", doc=handle)["result"]["layers"]
    return [l["name"] for l in layers if l["parent"] == parent]


class Base(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-merge-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def build(self, doc, specs, pixels_of):
        handle = registry.handle_for(doc)
        result = self.assertOk(run("create_layers", doc=handle, specs=specs))
        ids = {c["name"]: c["id"] for c in result["created"]}
        for name, (rect, data) in pixels_of.items():
            self.assertTrue(nodes.node_by_id(doc, ids[name]).setPixelData(data, *rect))
        settle(doc)
        batch.JOURNALS.forget(handle)
        return handle, ids

    def build_group(self, doc, **group_props):
        """Below (opaque, under the group), group G (base A with partial
        alpha, a clipped opaque C, a multiply M) and Top above it."""
        return self.build(doc, [
            {"name": "Below"},
            {"key": "g", "name": "G", "type": "group", **group_props},
            {"name": "A", "parent": "$g"},
            {"name": "C", "parent": "$g", "inherit_alpha": True},
            {"name": "M", "parent": "$g", "blend_mode": "multiply"},
            {"name": "Top"},
        ], {"Below": ((0, 0, W, H), opaque(W, H, 1)),
            "A": ((3, 2, 18, 14), pattern(18, 14, 2)),
            "C": ((-2, -2, 16, 30), opaque(16, 30, 3)),
            "M": ((8, 6, 14, 12), pattern(14, 12, 4)),
            "Top": ((20, 0, 6, 6), opaque(6, 6, 5))})

    def merge(self, handle, **params):
        return self.assertOk(run("merge_layers", doc=handle, **params))

    def assertUndoRedo(self, doc, handle, before):
        after = state(doc, handle)
        self.assertNotEqual(after, before)
        undone = self.assertOk(run("undo", doc=handle))
        self.assertEqual(undone["undone"]["label"], "merge_layers")
        self.assertEqual(state(doc, handle), before)
        self.assertOk(run("redo", doc=handle))
        self.assertEqual(state(doc, handle), after)  # the same merged layer, same id
        self.assertOk(run("undo", doc=handle))
        self.assertEqual(state(doc, handle), before)

    def assertNoTempGroup(self, doc, handle):
        layers = run("list_layers", doc=handle)["result"]["layers"]
        self.assertNotIn(mcmd.TEMP_GROUP, [l["name"] for l in layers])


class FlattenGroupTest(Base):
    def test_flatten_isolated_group(self):
        with scratch_document(W, H, name="kmcp-merge-group") as doc:
            handle, ids = self.build_group(doc)
            before = state(doc, handle)
            rect, proj = group_projection(doc, ids["G"])
            self.assertTrue(any(0 < a < 255 for a in proj[3::4]))
            dry = self.merge(handle, ids=["G"], name="Flat", dry_run=True)
            self.assertEqual((dry["dry_run"], dry["mode"], dry["merged"]),
                             (True, "group", [ids["G"]]))
            self.assertEqual(dry["would_create"], {"name": "Flat", "type": "paintlayer",
                                                   "parent": "/", "above": ids["G"]})
            self.assertEqual(dry["would_remove"], [ids[n] for n in ("G", "M", "C", "A")])
            self.assertEqual(state(doc, handle), before)

            result = self.merge(handle, ids=["G"], name="Flat")
            layer = result["layer"]
            self.assertEqual((layer["name"], layer["type"], layer["parent"], layer["opacity"],
                              layer["blend_mode"], layer["visible"], layer["inherit_alpha"]),
                             ("Flat", "paintlayer", None, 1.0, "normal", True, False))
            self.assertEqual(result["removed"], [ids[n] for n in ("G", "M", "C", "A")])
            self.assertEqual(result["warnings"], [])
            self.assertEqual(names_top_down(doc, handle), ["Top", "Flat", "Below", "Background"])
            # The new layer's pixels are the group's projection, and the
            # document renders the same, byte for byte.
            self.assertEqual(layer_pixels(doc, layer["id"], rect), proj)
            self.assertEqual(layer["bounds"], {"x": rect[0], "y": rect[1], "w": rect[2],
                                               "h": rect[3]})
            self.assertEqual(projection(doc), before["projection"])
            for nid in result["removed"]:
                self.assertIsNone(nodes.node_by_id(doc, nid))
            self.assertUndoRedo(doc, handle, before)

    def test_group_opacity_and_blend_mode_are_carried(self):
        with scratch_document(W, H, name="kmcp-merge-opacity") as doc:
            handle, ids = self.build_group(doc, opacity=0.5, blend_mode="screen",
                                           inherit_alpha=True, locked=True)
            before = state(doc, handle)
            rect, proj = group_projection(doc, ids["G"])
            result = self.merge(handle, ids=["G"], name="Flat")
            layer = result["layer"]
            self.assertEqual((layer["opacity"], layer["blend_mode"], layer["inherit_alpha"],
                              layer["locked"]), (0.502, "screen", True, True))  # 128 / 255
            # The projection excludes the group's opacity (Q8), so the pixels are
            # the projection and the opacity a property: the render is unchanged.
            self.assertEqual(layer_pixels(doc, layer["id"], rect), proj)
            self.assertEqual(projection(doc), before["projection"])
            self.assertUndoRedo(doc, handle, before)

    def test_pass_through_and_hidden_groups_read_from_a_clone(self):
        with scratch_document(W, H, name="kmcp-merge-pass-through") as doc:
            handle, ids = self.build_group(doc)
            # The isolated composite, which Krita has no projection for when
            # the group is pass-through.
            rect, isolated = group_projection(doc, ids["G"])
            self.assertOk(run("update_layers", doc=handle,
                              changes=[{"id": "G", "pass_through": True}]))
            batch.JOURNALS.forget(handle)
            before = state(doc, handle)
            self.assertEqual(set(group_projection(doc, ids["G"])[1][3::4]), {0})
            result = self.merge(handle, ids=["G"], name="Flat")
            self.assertEqual([w["code"] for w in result["warnings"]], ["pass_through_flattened"])
            self.assertEqual(layer_pixels(doc, result["layer"]["id"], rect), isolated)
            self.assertNoTempGroup(doc, handle)
            self.assertUndoRedo(doc, handle, before)

            self.assertOk(run("update_layers", doc=handle, changes=[
                {"id": "G", "pass_through": False, "visible": False}]))
            hidden = state(doc, handle)
            result = self.merge(handle, ids=["G"], name="Hidden")
            self.assertEqual(result["warnings"], [])
            self.assertFalse(result["layer"]["visible"])
            self.assertEqual(layer_pixels(doc, result["layer"]["id"], rect), isolated)
            self.assertEqual(projection(doc), hidden["projection"])
            self.assertUndoRedo(doc, handle, hidden)

    def test_nested_group_inside_a_hidden_group(self):
        with scratch_document(W, H, name="kmcp-merge-nested") as doc:
            handle, ids = self.build(doc, [
                {"key": "o", "name": "Outer", "type": "group", "visible": False},
                {"key": "i", "name": "Inner", "type": "group", "parent": "$o"},
                {"name": "X", "parent": "$i", "opacity": 0.6},
                {"name": "Y", "parent": "$i", "blend_mode": "multiply"},
            ], {"X": ((2, 2, 10, 10), pattern(10, 10, 6)),
                "Y": ((6, 6, 10, 10), pattern(10, 10, 7))})
            # The projection Krita shows once the outer group is visible.
            self.assertOk(run("update_layers", doc=handle,
                              changes=[{"id": "Outer", "visible": True}]))
            rect, proj = group_projection(doc, ids["Inner"])
            self.assertOk(run("undo", doc=handle))
            batch.JOURNALS.forget(handle)
            before = state(doc, handle)
            result = self.merge(handle, ids=["Outer/Inner"], name="Inner flat")
            self.assertEqual(result["layer"]["parent"], ids["Outer"])
            self.assertEqual(layer_pixels(doc, result["layer"]["id"], rect), proj)
            self.assertEqual(names_top_down(doc, handle, ids["Outer"]), ["Inner flat"])
            self.assertUndoRedo(doc, handle, before)


class MergeLayersTest(Base):
    def build_three(self, doc, specs):
        handle, ids = self.build(doc, [{"name": "Under"}] + [
            {"name": n, "opacity": o, "blend_mode": m} for n, o, m, _ in specs] + [
            {"name": "Over"}], {
            "Under": ((0, 0, W, H), opaque(W, H, 1)),
            "Over": ((26, 18, 4, 4), opaque(4, 4, 2)),
            **{n: d for n, _, _, d in specs}})
        return handle, ids

    def test_three_siblings_render_the_same(self):
        with scratch_document(W, H, name="kmcp-merge-three") as doc:
            # The bottom one opaque under the others: their blend modes see the
            # same pixels inside the merge as outside it, so the render is exact.
            handle, ids = self.build_three(doc, [
                ("A", 1.0, "normal", ((2, 2, 24, 18), opaque(24, 18, 3))),
                ("B", 0.5, "multiply", ((6, 6, 14, 10), pattern(14, 10, 4))),
                ("C", 0.8, "screen", ((10, 4, 12, 12), pattern(12, 12, 5)))])
            before = state(doc, handle)
            result = self.merge(handle, ids=["C", "A", "B"], name="ABC")
            self.assertEqual((result["mode"], result["merged"]),
                             ("layers", [ids["C"], ids["B"], ids["A"]]))
            self.assertEqual(result["removed"], [ids["C"], ids["B"], ids["A"]])
            layer = result["layer"]
            self.assertEqual((layer["opacity"], layer["blend_mode"], layer["visible"]),
                             (1.0, "normal", True))
            self.assertEqual(layer["bounds"], {"x": 2, "y": 2, "w": 24, "h": 18})
            self.assertEqual(names_top_down(doc, handle), ["Over", "ABC", "Under", "Background"])
            self.assertEqual([w["code"] for w in result["warnings"]], ["isolated_compositing"])
            self.assertEqual(result["warnings"][0]["ids"], [ids["C"], ids["B"]])
            self.assertEqual(max_diff(projection(doc), before["projection"]), 0)
            self.assertNoTempGroup(doc, handle)
            self.assertUndoRedo(doc, handle, before)

    def test_partial_alpha_normal_layers_within_two_levels(self):
        with scratch_document(W, H, name="kmcp-merge-normal") as doc:
            handle, ids = self.build_three(doc, [
                ("A", 0.7, "normal", ((2, 2, 20, 16), pattern(20, 16, 3))),
                ("B", 0.5, "normal", ((6, 6, 14, 10), pattern(14, 10, 4))),
                ("C", 0.8, "normal", ((10, 4, 12, 12), pattern(12, 12, 5)))])
            before = state(doc, handle)
            result = self.merge(handle, ids=["A", "B", "C"], name="ABC")
            self.assertEqual(result["warnings"], [])
            # Normal compositing is associative, but the merge rounds the
            # partly transparent intermediate composite to 8 bits before it
            # goes over the layers below: measured 1 level here, 2 with other data.
            diff = max_diff(projection(doc), before["projection"])
            print(f"merge_layers: partial-alpha normal merge, max projection difference {diff}")
            self.assertLessEqual(diff, 2)
            self.assertUndoRedo(doc, handle, before)

    def test_warnings(self):
        with scratch_document(W, H, name="kmcp-merge-warnings") as doc:
            handle, ids = self.build(doc, [
                {"name": "Base"},
                {"name": "Low", "inherit_alpha": True},
                {"name": "Mid"},
                {"name": "High", "visible": False},
            ], {"Base": ((0, 0, 8, 8), opaque(8, 8, 1)),
                "Low": ((2, 2, 8, 8), opaque(8, 8, 2)),
                "Mid": ((4, 4, 4, 4), opaque(4, 4, 3)),
                "High": ((6, 6, 4, 4), opaque(4, 4, 4))})
            dry = self.merge(handle, ids=["Low", "High"], name="Mid", dry_run=True)
            codes = [w["code"] for w in dry["warnings"]]
            self.assertEqual(codes, ["not_adjacent", "clipping_base_not_merged",
                                     "hidden_layers_dropped", "duplicate_name"])
            self.assertEqual(dry["warnings"][0]["ids"], [ids["Mid"]])
            result = self.merge(handle, ids=["Low", "High"], name="LH")
            self.assertEqual([w["code"] for w in result["warnings"]],
                             ["not_adjacent", "clipping_base_not_merged",
                              "hidden_layers_dropped", "nothing_visible"])
            self.assertIsNone(result["layer"]["bounds"])
            self.assertEqual(names_top_down(doc, handle), ["LH", "Mid", "Base", "Background"])

    def test_validation(self):
        with scratch_document(W, H, name="kmcp-merge-errors") as doc:
            handle, ids = self.build_group(doc)
            mask = doc.createTransparencyMask("Mask")
            nodes.node_by_id(doc, ids["Top"]).addChildNode(mask, None)
            settle(doc)
            before = state(doc, handle)
            err = self.assertError(run("merge_layers", doc=handle, ids=["G/A", "Top"],
                                       name="X"), "bad_request")
            self.assertEqual(len(err["details"]["parents"]), 2)
            err = self.assertError(run("merge_layers", doc=handle, ids=["Top/Mask", "Below"],
                                       name="X"), "conflict")
            self.assertEqual(err["details"]["type"], "transparencymask")
            for params, param in (({"ids": ["Top"], "name": "X"}, "ids"),
                                  ({"ids": ["G", "G"], "name": "X"}, "ids"),
                                  ({"ids": ["/"], "name": "X"}, "ids"),
                                  ({"ids": [], "name": "X"}, "ids"),
                                  ({"ids": "G", "name": "X"}, "ids"),
                                  ({"ids": ["G"]}, "name"),
                                  ({"ids": ["G"], "name": ""}, "name"),
                                  ({"ids": ["G"], "name": "X", "dry_run": "yes"}, "dry_run")):
                err = self.assertError(run("merge_layers", doc=handle, **params), "bad_request")
                self.assertEqual(err["details"]["param"], param, params)
            err = self.assertError(run("merge_layers", doc=handle, ids=["G"], name="X",
                                       mode="group"), "bad_request")
            self.assertEqual(err["details"]["unknown"], ["mode"])
            self.assertError(run("merge_layers", doc=handle, ids=["Nope"], name="X"),
                             "not_found")
            self.assertEqual(state(doc, handle), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_failures_leave_the_document_unchanged(self):
        with scratch_document(W, H, name="kmcp-merge-rollback") as doc:
            handle, ids = self.build_three(doc, [
                ("A", 1.0, "normal", ((2, 2, 20, 16), opaque(20, 16, 3))),
                ("B", 0.5, "normal", ((6, 6, 14, 10), pattern(14, 10, 4))),
                ("C", 0.8, "normal", ((10, 4, 12, 12), pattern(12, 12, 5)))])
            before = state(doc, handle)
            saved_take_out, saved_read = mcmd._take_out, mcmd._temp_group_projection
            calls = []

            def take_out(node, nid):
                calls.append(nid)
                if len(calls) == 2:
                    raise CommandError("krita_error", "injected removal failure")
                return saved_take_out(node, nid)

            def read(*args):
                raise RuntimeError("injected clone failure")

            try:
                mcmd._take_out = take_out
                err = self.assertError(run("merge_layers", doc=handle, ids=["A", "B", "C"],
                                           name="ABC"), "krita_error")
                self.assertIn("injected removal failure", err["message"])
                self.assertEqual(err["details"]["rolled_back"], 2)  # the removal, the create
                self.assertEqual(state(doc, handle), before)
                self.assertNoTempGroup(doc, handle)
                mcmd._take_out = saved_take_out
                mcmd._temp_group_projection = read
                err = self.assertError(run("merge_layers", doc=handle, ids=["A", "B", "C"],
                                           name="ABC"), "krita_error")
                self.assertIn("injected clone failure", err["message"])
            finally:
                mcmd._take_out, mcmd._temp_group_projection = saved_take_out, saved_read
            self.assertEqual(state(doc, handle), before)
            self.assertNoTempGroup(doc, handle)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)
            # And a merge that works leaves no temporary group either.
            self.merge(handle, ids=["A", "B", "C"], name="ABC")
            self.assertNoTempGroup(doc, handle)
