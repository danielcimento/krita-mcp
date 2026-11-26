"""set_active_layer and duplicate_layer (commands/layers.py), layer_stats
(commands/analysis.py) and trim_layers (commands/pixels.py), headless,
through dispatch.execute (P5.2, P5.3).

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. Undo goes through the ``undo``
command; ``state`` (test_history.py) compares the whole document: the
``list_layers`` dump (ids included), every node's layer object and pixels
over a window wider than the canvas, and the projection.
"""

import contextlib
import shutil
import tempfile
import unittest

from helpers import scratch_document, settle, temp_config
from kritamcp import batch, nodes, pixels, registry
from test_dispatch import EnvelopeAssertions, run
from test_history import WIN, H, W, state
from test_pixels import pattern


def px(b, g, r, a):
    return bytes((b, g, r, a))


def put(doc, nid, x, y, w, h, data):
    assert nodes.node_by_id(doc, nid).setPixelData(data, x, y, w, h)


def layer_bytes(doc, nid, rect=WIN):
    return pixels.as_bytes(nodes.node_by_id(doc, nid).pixelData(*rect))


def at(data, rect, x, y):
    """The BGRA pixel at canvas (x, y) of ``data`` read over ``rect``."""
    i = 4 * ((y - rect[1]) * rect[2] + (x - rect[0]))
    return data[i:i + 4]


def top_down(doc, parent="/"):
    index = nodes.build_index(doc)
    return [index.get(c)["name"] for c in index.children(parent)]


class Base(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-layer-utils-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def build(self, doc):
        """P (pixels past the edge, multiply, clipped, with mask M), group G
        (pass-through) holding X and group H with Y, and an empty E.
        Returns (handle, {name: id}) with a fresh journal."""
        handle = registry.handle_for(doc)
        result = self.assertOk(run("create_layers", doc=handle, specs=[
            {"name": "E"},
            {"name": "P", "blend_mode": "multiply", "opacity": 100 / 255, "inherit_alpha": True},
            {"key": "g", "name": "G", "type": "group", "pass_through": True},
            {"name": "X", "parent": "$g"},
            {"key": "h", "name": "H", "type": "group", "parent": "$g"},
            {"name": "Y", "parent": "$h", "alpha_locked": True},
        ]))
        ids = {c["name"]: c["id"] for c in result["created"]}
        ids["Background"] = nodes.build_index(doc).by_name("Background")[0]
        put(doc, ids["P"], -3, 5, 10, 6, pattern(10, 6, 1))
        put(doc, ids["X"], 4, 2, 5, 3, pattern(5, 3, 2))
        put(doc, ids["Y"], 28, 20, 8, 7, pattern(8, 7, 3))
        mask = doc.createTransparencyMask("M")
        self.assertTrue(nodes.node_by_id(doc, ids["P"]).addChildNode(mask, None))
        ids["M"] = nodes.node_id(mask)
        settle(doc)
        batch.JOURNALS.forget(handle)
        return handle, ids

    def journal_len(self, handle):
        return len(batch.JOURNALS.get(handle))


# --- set_active_layer ------------------------------------------------------

class SetActiveLayerTest(Base):
    def test_sets_and_warns_headless(self):
        with scratch_document(W, H, name="kmcp-active") as doc:
            handle, ids = self.build(doc)
            for ref, nid, kind, path in (("P", ids["P"], "paintlayer", "P"),
                                         ("G/H", ids["H"], "grouplayer", "G/H"),
                                         (ids["M"], ids["M"], "transparencymask", "P/M")):
                result = self.assertOk(run("set_active_layer", doc=handle, id=ref))
                self.assertEqual({k: result[k] for k in ("doc", "layer", "path", "type")},
                                 {"doc": handle, "layer": nid, "path": path, "type": kind})
                self.assertEqual([w["code"] for w in result["warnings"]], ["not_active_document"])
                self.assertIn(handle, result["warnings"][0]["message"])
            # Headless activeNode() stays None (krita-api-notes § Nodes); nothing crashed.
            self.assertIsNone(doc.activeNode())
            self.assertEqual(self.journal_len(handle), 0)  # UI state: no journal entry

    def test_errors(self):
        with scratch_document(W, H, name="kmcp-active-err") as doc:
            handle, _ = self.build(doc)
            self.assertError(run("set_active_layer", doc=handle), "bad_request")
            self.assertError(run("set_active_layer", doc=handle, id="/"), "bad_request")
            self.assertError(run("set_active_layer", doc=handle, id="Nope"), "not_found")

    def test_no_warning_for_the_active_document(self):
        with scratch_document(W, H, name="kmcp-active-doc") as doc:
            handle, ids = self.build(doc)
            saved = registry._active_document
            registry._active_document = lambda: doc
            try:
                result = self.assertOk(run("set_active_layer", doc=handle, id="P"))
            finally:
                registry._active_document = saved
            self.assertEqual(result["warnings"], [])
            self.assertEqual(result["layer"], ids["P"])


# --- duplicate_layer -------------------------------------------------------

COMPARED = ("name", "type", "visible", "opacity", "blend_mode", "locked", "alpha_locked",
            "inherit_alpha", "pass_through", "has_layer_style", "bounds")


def props(obj):
    return {k: obj[k] for k in COMPARED}


class DuplicateLayerTest(Base):
    def shape(self, doc, nid):
        """(props, pixels or None, [child shapes, top to bottom]) of ``nid``'s subtree."""
        index, by_id = nodes.tree(doc)
        obj = nodes.layer_object(by_id[nid], index, nid=nid)
        data = layer_bytes(doc, nid) if obj["type"] == "paintlayer" else None
        return props(obj), data, [self.shape(doc, c) for c in obj["children"]]

    def test_paint_layer_copy(self):
        with scratch_document(W, H, name="kmcp-dup-paint") as doc:
            handle, ids = self.build(doc)
            result = self.assertOk(run("duplicate_layer", doc=handle, id="P"))
            copy = result["layer"]
            self.assertEqual((result["doc"], result["dry_run"], result["source"]),
                             (handle, False, ids["P"]))
            self.assertNotIn(copy["id"], ids.values())
            self.assertEqual(self.shape(doc, copy["id"]), self.shape(doc, ids["P"]))
            self.assertEqual(copy["name"], "P")  # Krita keeps the name
            self.assertEqual((copy["path"], copy["parent"]), ("P", None))
            self.assertEqual(len(result["ids"]), 2)  # the copy and its mask
            self.assertEqual(result["ids"][0], copy["id"])
            self.assertNotEqual(result["ids"][1], ids["M"])
            index = nodes.build_index(doc)
            self.assertEqual(index.get(result["ids"][1])["type"], "transparencymask")
            # Directly above the source.
            self.assertEqual(top_down(doc), ["G", "P", "P", "E", "Background"])
            kids = index.children("/")
            self.assertEqual(kids.index(copy["id"]) + 1, kids.index(ids["P"]))
            self.assertEqual(result["warnings"], [
                {"code": "duplicate_name", "message": result["warnings"][0]["message"],
                 "id": copy["id"], "name": "P", "others": [ids["P"]]},
                {"code": "duplicate_names_in_copy",
                 "message": result["warnings"][1]["message"], "count": 1, "names": ["M"]}])
            self.assertEqual(self.journal_len(handle), 1)

    def test_group_copy_has_the_subtree(self):
        with scratch_document(W, H, name="kmcp-dup-group") as doc:
            handle, ids = self.build(doc)
            result = self.assertOk(run("duplicate_layer", doc=handle, id="G"))
            copy = result["layer"]
            self.assertEqual(self.shape(doc, copy["id"]), self.shape(doc, ids["G"]))
            self.assertTrue(copy["pass_through"])
            self.assertEqual(len(result["ids"]), 4)  # G, X, H, Y
            self.assertFalse(set(result["ids"]) & set(ids.values()))  # all new ids
            index = nodes.build_index(doc)
            self.assertEqual([index.get(i)["name"] for i in result["ids"]], ["G", "H", "Y", "X"])
            self.assertEqual(top_down(doc), ["G", "G", "P", "E", "Background"])
            self.assertEqual(result["warnings"][1]["names"], ["H", "X", "Y"])
            self.assertEqual(result["warnings"][1]["count"], 3)

    def test_name_and_placement(self):
        with scratch_document(W, H, name="kmcp-dup-place") as doc:
            handle, ids = self.build(doc)
            r = self.assertOk(run("duplicate_layer", doc=handle, id="P", name="P2", parent="G",
                                  above="G/X"))
            self.assertEqual(r["layer"]["path"], "G/P2")
            self.assertEqual(top_down(doc, ids["G"]), ["H", "P2", "X"])
            self.assertEqual([w["code"] for w in r["warnings"]], ["duplicate_names_in_copy"])

            r = self.assertOk(run("duplicate_layer", doc=handle, id="E", parent="G"))
            self.assertEqual(top_down(doc, ids["G"]), ["E", "H", "P2", "X"])  # on top of G

            # Without parent, above must be a sibling of the source.
            err = self.assertError(run("duplicate_layer", doc=handle, id="G/X",
                                       above="Background"), "bad_request")
            self.assertEqual(err["details"]["param"], "above")
            r = self.assertOk(run("duplicate_layer", doc=handle, id="G/X", name="Low",
                                  parent="/", above="Background"))
            self.assertEqual(r["layer"]["path"], "Low")
            r = self.assertOk(run("duplicate_layer", doc=handle, id=ids["E"],
                                  above="Background", name="E2"))
            self.assertEqual(top_down(doc), ["G", "P", "E", "Low", "E2", "Background"])
            self.assertEqual(r["warnings"], [])

    def test_locked_source_renamed(self):
        with scratch_document(W, H, name="kmcp-dup-locked") as doc:
            handle, ids = self.build(doc)
            self.assertOk(run("update_layers", doc=handle, changes=[{"id": "X", "locked": True}]))
            r = self.assertOk(run("duplicate_layer", doc=handle, id="X", name="X copy"))
            self.assertEqual((r["layer"]["name"], r["layer"]["locked"]), ("X copy", True))
            self.assertEqual(layer_bytes(doc, r["layer"]["id"]), layer_bytes(doc, ids["X"]))

    def test_errors_change_nothing(self):
        with scratch_document(W, H, name="kmcp-dup-err") as doc:
            handle, ids = self.build(doc)
            before = state(doc, handle)
            for params, code, param in (
                    ({}, "bad_request", "id"),
                    ({"id": "/"}, "bad_request", None),
                    ({"id": "Nope"}, "not_found", None),
                    ({"id": "P/M"}, "conflict", None),
                    ({"id": "P", "parent": "E"}, "bad_request", "parent"),
                    ({"id": "P", "parent": "Nope"}, "not_found", None),
                    ({"id": "P", "above": "G/X"}, "bad_request", "above"),
                    ({"id": "P", "parent": "G", "above": "E"}, "bad_request", "above"),
                    ({"id": "P", "name": ""}, "bad_request", "name"),
                    ({"id": "P", "name": "a\nb"}, "bad_request", "name"),
                    ({"id": "P", "dry_run": "yes"}, "bad_request", "dry_run")):
                err = self.assertError(run("duplicate_layer", doc=handle, **params), code)
                if param is not None:
                    self.assertEqual(err["details"]["param"], param, params)
            self.assertEqual(state(doc, handle), before)
            self.assertEqual(self.journal_len(handle), 0)

    def test_undo_redo(self):
        with scratch_document(W, H, name="kmcp-dup-undo") as doc:
            handle, ids = self.build(doc)
            before = state(doc, handle)
            r = self.assertOk(run("duplicate_layer", doc=handle, id="G", name="G2"))
            after = state(doc, handle)
            undone = self.assertOk(run("undo", doc=handle))
            self.assertEqual(undone["undone"]["label"], "duplicate_layer")
            self.assertEqual(state(doc, handle), before)
            self.assertOk(run("redo", doc=handle))
            self.assertEqual(state(doc, handle), after)  # same ids again
            self.assertIsNotNone(nodes.node_by_id(doc, r["layer"]["id"]))

    def test_dry_run(self):
        with scratch_document(W, H, name="kmcp-dup-dry") as doc:
            handle, ids = self.build(doc)
            before = state(doc, handle)
            r = self.assertOk(run("duplicate_layer", doc=handle, id="G", dry_run=True))
            self.assertEqual(r["would_create"], {"name": "G", "type": "grouplayer", "parent": "/",
                                                 "above": ids["G"], "count": 4})
            self.assertEqual(r["warnings"][0]["others"], [ids["G"]])
            self.assertIsNone(r["warnings"][0]["id"])
            r = self.assertOk(run("duplicate_layer", doc=handle, id="P", parent="G",
                                  name="Q", dry_run=True))
            self.assertEqual(r["would_create"], {"name": "Q", "type": "paintlayer",
                                                 "parent": ids["G"], "above": None, "count": 2})
            self.assertEqual(state(doc, handle), before)
            self.assertEqual(self.journal_len(handle), 0)


# --- layer_stats -----------------------------------------------------------

class LayerStatsTest(Base):
    def make(self, doc):
        """S: a 6x6 block at (-2, 20), past the left and bottom edges, with
        known alphas; T: an opaque block, an anti-aliased edge and a far
        faint speck; Z: empty; G a group."""
        handle = registry.handle_for(doc)
        result = self.assertOk(run("create_layers", doc=handle, specs=[
            {"name": "S"}, {"name": "T"}, {"name": "Z"}, {"name": "G", "type": "group"}]))
        ids = {c["name"]: c["id"] for c in result["created"]}
        alphas = [255, 0, 1, 7, 8, 31,
                  32, 127, 128, 254, 255, 255,
                  0, 0, 3, 200, 255, 9,
                  50, 255, 255, 255, 0, 0,
                  2, 2, 2, 2, 2, 2,
                  100, 0, 0, 0, 0, 255]
        data = b"".join(px(10, 20, 30, a) for a in alphas)
        put(doc, ids["S"], -2, 20, 6, 6, data)
        t = bytearray(4 * W * H)
        def set_px(x, y, a):
            t[4 * (y * W + x):4 * (y * W + x) + 4] = px(40, 50, 60, a)
        for y in range(2, 8):
            for x in range(2, 8):
                set_px(x, y, 255)
        set_px(8, 4, 4)    # anti-aliased edge, next to the block
        set_px(20, 10, 3)  # an isolated speck, far from it
        set_px(21, 10, 3)
        put(doc, ids["T"], 0, 0, W, H, bytes(t))
        settle(doc)
        return handle, ids, alphas

    def test_exact_numbers(self):
        with scratch_document(W, H, name="kmcp-stats") as doc:
            handle, ids, alphas = self.make(doc)
            r = self.assertOk(run("layer_stats", doc=handle, ids=["S", "T", "Z", "G", "S"]))
            self.assertEqual(r["canvas"], {"w": W, "h": H})
            self.assertEqual(r["buckets"], [0, 1, 8, 32, 128, 255])
            self.assertEqual([x["id"] for x in r["layers"]], [ids["S"], ids["T"], ids["Z"]])
            self.assertEqual(r["skipped"], [{"id": ids["G"], "path": "G", "type": "grouplayer",
                                             "reason": r["skipped"][0]["reason"]}])
            s, t, z = r["layers"]

            rect = {"x": -2, "y": 20, "w": 6, "h": 6}
            self.assertEqual((s["path"], s["bounds"], s["opaque_bounds"]), ("S", rect, rect))
            nonzero = [a for a in alphas if a]
            self.assertEqual(s["pixel_count"], len(nonzero))
            self.assertEqual(s["opaque_count"], alphas.count(255))
            self.assertFalse(s["empty"])
            edges = [0, 1, 8, 32, 128, 255]
            self.assertEqual(s["alpha_histogram"], {
                "edges": [0, 1, 8, 32, 128, 255],
                "counts": [sum(1 for a in alphas if lo <= a < hi)
                           for lo, hi in zip(edges, edges[1:])] + [alphas.count(255)]})
            self.assertEqual(s["alpha_histogram"]["counts"], [9, 9, 3, 4, 3, 8])
            # Past the canvas: columns x = -2, -1 and rows y = 24, 25.
            outside = sum(1 for i, a in enumerate(alphas)
                          if a and (i % 6 < 2 or i // 6 >= 4))
            self.assertEqual(s["beyond_canvas"], {"left": 2, "top": 0, "right": 0, "bottom": 2,
                                                  "pixels": outside})

            self.assertEqual(t["bounds"], {"x": 2, "y": 2, "w": 20, "h": 9})
            self.assertEqual((t["pixel_count"], t["opaque_count"]), (39, 36))
            self.assertIsNone(t["beyond_canvas"])
            self.assertEqual(t["stray_count"], 2)
            self.assertEqual(t["stray_specks"]["count"], 1)
            self.assertEqual(t["stray_specks"]["samples"], [{"x": 20, "y": 10, "w": 2, "h": 1}])

            self.assertEqual({k: z[k] for k in ("bounds", "opaque_bounds", "pixel_count",
                                                "opaque_count", "empty", "beyond_canvas",
                                                "stray_count")},
                             {"bounds": None, "opaque_bounds": None, "pixel_count": 0,
                              "opaque_count": 0, "empty": True, "beyond_canvas": None,
                              "stray_count": 0})
            self.assertEqual(z["alpha_histogram"]["counts"], [0] * 6)

            # The stray numbers (and counts) are lint's.
            snap = self.assertOk(run("lint_snapshot", doc=handle))["snapshot"]
            self.assertEqual(r["settings"], snap["settings"])
            by_id = {n["id"]: n["alpha"] for n in snap["nodes"]}
            for layer in r["layers"]:
                for key in ("pixel_count", "opaque_bounds", "stray_count", "stray_specks"):
                    self.assertEqual(layer[key], by_id[layer["id"]][key], (layer["path"], key))

    def test_buckets(self):
        with scratch_document(W, H, name="kmcp-stats-buckets") as doc:
            handle, ids, alphas = self.make(doc)
            r = self.assertOk(run("layer_stats", doc=handle, ids=["S"], buckets=[5, 128, 255]))
            self.assertEqual(r["layers"][0]["alpha_histogram"], {
                "edges": [5, 128, 255],
                "counts": [sum(1 for a in alphas if 5 <= a < 128),
                           sum(1 for a in alphas if 128 <= a < 255), alphas.count(255)]})
            for bad in ([0], [0, 0], [3, 1], [0, 256], [0, 1.5], [0, True], "0,1",
                        list(range(17))):
                err = self.assertError(run("layer_stats", doc=handle, ids=["S"], buckets=bad),
                                       "bad_request")
                self.assertEqual(err["details"]["param"], "buckets", bad)

    def test_refs(self):
        with scratch_document(W, H, name="kmcp-stats-refs") as doc:
            handle, ids, _ = self.make(doc)
            err = self.assertError(run("layer_stats", doc=handle, ids=["S", "Nope"]), "not_found")
            self.assertEqual(err["details"]["index"], 1)
            self.assertError(run("layer_stats", doc=handle, ids=["/"]), "bad_request")
            self.assertError(run("layer_stats", doc=handle, ids=[]), "bad_request")
            self.assertError(run("layer_stats", doc=handle, ids="S"), "bad_request")


# --- trim_layers -----------------------------------------------------------

class TrimLayersTest(Base):
    def make(self, doc):
        """L: an opaque block at (4, 4, 6, 6), an alpha-3 edge pixel at
        (10, 5), a far alpha-5 speck at (25, 18), an alpha-2 pixel past the
        left edge at (-3, 1), an alpha-100 pixel at (0, 0) and a coloured
        alpha-0 pixel at (20, 2). G: a group."""
        handle = registry.handle_for(doc)
        result = self.assertOk(run("create_layers", doc=handle, specs=[
            {"name": "L"}, {"name": "K"}, {"name": "G", "type": "group"}]))
        ids = {c["name"]: c["id"] for c in result["created"]}
        put(doc, ids["L"], 4, 4, 6, 6, px(1, 2, 3, 255) * 36)
        for (x, y), p in (((10, 5), px(7, 8, 9, 3)), ((25, 18), px(10, 20, 30, 5)),
                          ((-3, 1), px(11, 12, 13, 2)), ((0, 0), px(4, 5, 6, 100)),
                          ((20, 2), px(1, 2, 3, 0))):
            put(doc, ids["L"], x, y, 1, 1, p)
        put(doc, ids["K"], 0, 0, 4, 4, px(9, 9, 9, 6) * 16)
        settle(doc)
        batch.JOURNALS.forget(handle)
        return handle, ids

    def expected(self, before, cleared):
        out = bytearray(before)
        for x, y in cleared:
            i = 4 * ((y - WIN[1]) * WIN[2] + (x - WIN[0]))
            out[i:i + 4] = bytes(4)
        return bytes(out)

    def test_threshold_only(self):
        with scratch_document(W, H, name="kmcp-trim") as doc:
            handle, ids = self.make(doc)
            before = layer_bytes(doc, ids["L"])
            r = self.assertOk(run("trim_layers", doc=handle, ids=["L"], alpha_threshold=8))
            self.assertEqual(r["trimmed"], [{
                "id": ids["L"], "path": "L", "cleared": 3,
                "bounds_before": {"x": -3, "y": 0, "w": 29, "h": 19},
                "bounds_after": {"x": 0, "y": 0, "w": 10, "h": 10}}])
            self.assertEqual(r["totals"], {"layers": 1, "changed": 1, "cleared": 3})
            self.assertEqual((r["alpha_threshold"], r["near_px"], r["dry_run"], r["warnings"]),
                             (8, None, False, []))
            after = layer_bytes(doc, ids["L"])
            self.assertEqual(after, self.expected(before, [(10, 5), (25, 18), (-3, 1)]))
            self.assertEqual(at(after, WIN, 25, 18), bytes(4))     # colour zeroed too
            self.assertEqual(at(after, WIN, 20, 2), px(1, 2, 3, 0))  # alpha 0: untouched
            self.assertEqual(at(after, WIN, 0, 0), px(4, 5, 6, 100))
            listed = self.assertOk(run("list_layers", doc=handle, root="L"))["layers"][0]
            self.assertEqual(listed["bounds"], r["trimmed"][0]["bounds_after"])

    def test_near_px_keeps_the_edge(self):
        with scratch_document(W, H, name="kmcp-trim-near") as doc:
            handle, ids = self.make(doc)
            before = layer_bytes(doc, ids["L"])
            r = self.assertOk(run("trim_layers", doc=handle, ids=["L", "K"], alpha_threshold=8,
                                  near_px=2))
            # The edge at (10, 5) is 1 px from the block; the speck and the
            # off-canvas pixel are far from every strong pixel. K has none.
            self.assertEqual([e["cleared"] for e in r["trimmed"]], [2, 16])
            self.assertEqual(r["trimmed"][1]["bounds_after"], None)
            self.assertEqual(r["totals"], {"layers": 2, "changed": 2, "cleared": 18})
            self.assertEqual(layer_bytes(doc, ids["L"]),
                             self.expected(before, [(25, 18), (-3, 1)]))
            self.assertEqual(at(layer_bytes(doc, ids["L"]), WIN, 10, 5), px(7, 8, 9, 3))

    def test_non_paint_target_is_a_conflict(self):
        with scratch_document(W, H, name="kmcp-trim-conflict") as doc:
            handle, ids = self.make(doc)
            before = state(doc, handle)
            err = self.assertError(run("trim_layers", doc=handle, ids=["L", "G"],
                                       alpha_threshold=8), "conflict")
            self.assertEqual((err["details"]["index"], err["details"]["type"]), (1, "grouplayer"))
            self.assertEqual(state(doc, handle), before)  # L untouched: all checked first
            self.assertEqual(self.journal_len(handle), 0)

    def test_bad_params(self):
        with scratch_document(W, H, name="kmcp-trim-params") as doc:
            handle, ids = self.make(doc)
            for params, param in (({"alpha_threshold": 0}, "alpha_threshold"),
                                  ({"alpha_threshold": 256}, "alpha_threshold"),
                                  ({"alpha_threshold": True}, "alpha_threshold"),
                                  ({"alpha_threshold": 7.5}, "alpha_threshold"),
                                  ({"near_px": 0}, "near_px"), ({"near_px": 65}, "near_px"),
                                  ({"near_px": 2.0}, "near_px")):
                err = self.assertError(run("trim_layers", doc=handle, ids=["L"], **params),
                                       "bad_request")
                self.assertEqual(err["details"]["param"], param, params)
            self.assertError(run("trim_layers", doc=handle, ids=["Nope"]), "not_found")
            self.assertError(run("trim_layers", doc=handle, ids=["/"]), "bad_request")

    def test_default_threshold_is_8(self):
        # The default was 1 until 2026-11-25, which cleared nothing (alpha-0
        # pixels are left alone); it is now 8, lint's stray_alpha_below.
        with scratch_document(W, H, name="kmcp-trim-default") as doc:
            handle, ids = self.make(doc)
            dflt = self.assertOk(run("trim_layers", doc=handle, ids=["L", "K"], dry_run=True))
            eight = self.assertOk(run("trim_layers", doc=handle, ids=["L", "K"],
                                      alpha_threshold=8, dry_run=True))
            self.assertEqual(dflt["alpha_threshold"], 8)
            self.assertEqual(dflt["would_trim"], eight["would_trim"])
            self.assertEqual(dflt["totals"], eight["totals"])

    def test_undo_redo_exact(self):
        with scratch_document(W, H, name="kmcp-trim-undo") as doc:
            handle, ids = self.make(doc)
            before = state(doc, handle)
            self.assertOk(run("trim_layers", doc=handle, ids=["L", "K"], alpha_threshold=8))
            after = state(doc, handle)
            self.assertNotEqual(after, before)
            undone = self.assertOk(run("undo", doc=handle))
            self.assertEqual(undone["undone"]["label"], "trim_layers")
            self.assertEqual(state(doc, handle), before)
            self.assertOk(run("redo", doc=handle))
            self.assertEqual(state(doc, handle), after)

    def test_dry_run(self):
        with scratch_document(W, H, name="kmcp-trim-dry") as doc:
            handle, ids = self.make(doc)
            before = state(doc, handle)
            dry = self.assertOk(run("trim_layers", doc=handle, ids=["L", "K"], alpha_threshold=8,
                                    near_px=2, dry_run=True))
            self.assertEqual(state(doc, handle), before)
            self.assertEqual(self.journal_len(handle), 0)
            real = self.assertOk(run("trim_layers", doc=handle, ids=["L", "K"],
                                     alpha_threshold=8, near_px=2))
            self.assertEqual(dry["would_trim"], real["trimmed"])
            self.assertEqual(dry["totals"], real["totals"])
