"""set_canvas_size (commands/canvas.py, P6.1) and diff_documents
(commands/diff.py, P6.2), headless, through dispatch.execute.

Every call passes its documents explicitly (headless there is no active
document); documents are closed by ``scratch_document``, or with
``close_document`` in ``finally`` for the ones ``open_file`` opened. Files
go into a temp dir that a temp config makes the allowed root.
"""

import contextlib
import os
import shutil
import tempfile
import unittest

from helpers import scratch_document, settle, temp_config
from kritamcp import batch, nodes, pixels, registry
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run
from test_pixels import pattern

W, H = 32, 24
# A window wide enough for every canvas size used here, past the edges too.
WIN = (-24, -24, 96, 80)

GROW, SHRINK = (41, 31), (25, 19)  # odd differences: 9 x 7 and -7 x -5
OFFSETS = {  # anchor -> (offset when growing, offset when shrinking)
    "top-left": ((0, 0), (0, 0)), "top": ((4, 0), (-4, 0)), "top-right": ((9, 0), (-7, 0)),
    "left": ((0, 3), (0, -3)), "center": ((4, 3), (-4, -3)), "right": ((9, 3), (-7, -3)),
    "bottom-left": ((0, 7), (0, -5)), "bottom": ((4, 7), (-4, -5)),
    "bottom-right": ((9, 7), (-7, -5)),
}


def solid(w, h, bgra):
    return bytes(bgra) * (w * h)


def read(node, r):
    return pixels.as_bytes(node.pixelData(*r))


def box(node):
    if not node.hasExtents():
        return None
    r = node.bounds()
    return None if r.width() * r.height() == 0 else (r.x(), r.y(), r.width(), r.height())


def state(doc, handle):
    """Everything an undo must restore: the size, list_layers, every paint
    layer's pixels over ``WIN`` (past the canvas too) and the projection."""
    listed = run("list_layers", doc=handle)
    assert listed["ok"], listed
    index, by_id = nodes.tree(doc)
    layers = {nid: read(node, WIN) for nid, node in by_id.items()
              if node.type() == "paintlayer"}
    settle(doc)
    return {"size": (doc.width(), doc.height()), "layers": listed["result"]["layers"],
            "pixels": layers, "projection": read(doc, (0, 0, doc.width(), doc.height()))}


class Base(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-canvas-diff-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def open_copy(self, kra, name):
        """Open a copy of ``kra``; closed again at tearDown (before the temp
        dir goes)."""
        path = os.path.join(self.root, name)
        shutil.copyfile(kra, path)
        handle = self.assertOk(run("open_file", path=path))["doc"]
        self._stack.callback(self._close, handle)
        return handle

    @staticmethod
    def _close(handle):
        try:
            registry.lookup(handle)
        except CommandError:
            return
        run("close_document", doc=handle, discard=True)


# --- set_canvas_size -----------------------------------------------------------

class SetCanvasSizeTest(Base):
    def build(self, doc):
        """Background (opaque white), P (pattern 8x6 at (5, 3)), group G
        with A (pattern 6x5 at (20, 14)), X (solid 5x4 at (-3, -2), partly
        past the canvas). Returns (handle, ids, {name: (rect, data)})."""
        handle = registry.handle_for(doc)
        created = self.assertOk(run("create_layers", doc=handle, specs=[
            {"key": "g", "name": "G", "type": "group"},
            {"name": "A", "parent": "$g"},
            {"name": "X"},
            {"name": "P"},
        ]))["created"]
        ids = {c["name"]: c["id"] for c in created}
        data = {"P": ((5, 3, 8, 6), pattern(8, 6, 1)), "A": ((20, 14, 6, 5), pattern(6, 5, 2)),
                "X": ((-3, -2, 5, 4), solid(5, 4, (9, 99, 199, 255)))}
        for name, (r, d) in data.items():
            self.assertTrue(nodes.node_by_id(doc, ids[name]).setPixelData(d, *r))
        settle(doc)
        batch.JOURNALS.forget(handle)
        return handle, ids, data

    def resize(self, handle, width, height, **params):
        return self.assertOk(run("set_canvas_size", doc=handle, width=width, height=height,
                                 **params))

    def assertMoved(self, doc, ids, data, before_boxes, offset):
        """Every layer's content sits at its old place plus ``offset``."""
        dx, dy = offset
        for name, ((x, y, w, h), d) in data.items():
            node = nodes.node_by_id(doc, ids[name])
            self.assertEqual(read(node, (x + dx, y + dy, w, h)), d, name)
            bx, by, bw, bh = before_boxes[name]
            self.assertEqual(box(node), (bx + dx, by + dy, bw, bh), name)

    def test_every_anchor_grow_and_shrink_with_undo_redo(self):
        with scratch_document(W, H, name="kmcp-canvas-anchors") as doc:
            handle, ids, data = self.build(doc)
            start = state(doc, handle)
            boxes = {n: box(nodes.node_by_id(doc, ids[n])) for n in data}
            for anchor, offsets in OFFSETS.items():
                for size, offset in zip((GROW, SHRINK), offsets):
                    with self.subTest(anchor=anchor, size=size):
                        result = self.resize(handle, *size, anchor=anchor)
                        self.assertEqual(result, {
                            "doc": handle, "width_before": W, "height_before": H,
                            "width": size[0], "height": size[1],
                            "offset": {"x": offset[0], "y": offset[1]}, "anchor": anchor,
                            "dry_run": False, "changed": True,
                            "warnings": [{"code": "rig_breaking",
                                          "message": result["warnings"][0]["message"]}]})
                        self.assertIn("breaks existing rigs", result["warnings"][0]["message"])
                        self.assertEqual((doc.width(), doc.height()), size)
                        self.assertMoved(doc, ids, data, boxes, offset)
                        after = state(doc, handle)
                        undone = self.assertOk(run("undo", doc=handle))
                        self.assertEqual(undone["undone"], {"label": "set_canvas_size",
                                                            "layers": []})
                        self.assertEqual(state(doc, handle), start)
                        self.assertOk(run("redo", doc=handle))
                        self.assertEqual(state(doc, handle), after)
                        self.assertOk(run("undo", doc=handle))
                        self.assertEqual(state(doc, handle), start)
                        batch.JOURNALS.forget(handle)

    def test_shrink_keeps_pixels_past_the_canvas(self):
        with scratch_document(W, H, name="kmcp-canvas-shrink") as doc:
            handle, ids, data = self.build(doc)
            start = state(doc, handle)
            boxes = {n: box(nodes.node_by_id(doc, ids[n])) for n in data}
            # 4x4 at the top left: P (5, 3) and A (20, 14) end up wholly
            # past the canvas, and are kept there.
            result = self.resize(handle, 4, 4, anchor="top-left")
            self.assertEqual(result["offset"], {"x": 0, "y": 0})
            for name in ("P", "A"):
                r, d = data[name]
                node = nodes.node_by_id(doc, ids[name])
                self.assertEqual(read(node, r), d, name)
                self.assertEqual(box(node), boxes[name], name)
            # Centre, shrinking by an odd amount: 2 columns off the left.
            self.assertOk(run("undo", doc=handle))
            result = self.resize(handle, 29, 21)
            self.assertEqual(result["offset"], {"x": -2, "y": -2})
            r, d = data["P"]
            self.assertEqual(read(nodes.node_by_id(doc, ids["P"]), (r[0] - 2, r[1] - 2, r[2], r[3])),
                             d)
            self.assertOk(run("undo", doc=handle))
            self.assertEqual(state(doc, handle), start)

    def test_undo_with_earlier_pixel_entries(self):
        """Older journal entries were recorded on the old canvas: undoing the
        resize first puts them back in their coordinates."""
        with scratch_document(W, H, name="kmcp-canvas-journal") as doc:
            handle, ids, data = self.build(doc)
            start = state(doc, handle)
            png = os.path.join(self.root, "patch.png")
            pixels.save_png(solid(4, 3, (1, 2, 250, 255)), png, 4, 3)
            self.assertOk(run("import_png", doc=handle, id="P", png=png, x=6, y=4))
            self.assertOk(run("update_layers", doc=handle, changes=[{"id": "A", "opacity": 0.5}]))
            self.resize(handle, 20, 30, anchor="bottom-right")
            self.assertEqual([e["label"] for e in run("journal", doc=handle)["result"]["undo"]],
                             ["set_canvas_size", "update_layers", "import_png"])
            for _ in range(3):
                self.assertOk(run("undo", doc=handle))
            self.assertEqual(state(doc, handle), start)

    def test_undo_refused_after_a_resize_by_hand(self):
        with scratch_document(W, H, name="kmcp-canvas-check") as doc:
            handle, ids, data = self.build(doc)
            start = state(doc, handle)
            self.resize(handle, 40, 40)
            doc.resizeImage(0, 0, 50, 50)  # by hand
            doc.waitForDone()
            err = self.assertError(run("undo", doc=handle), "conflict")
            self.assertIn("50x50", err["message"])
            self.assertEqual(len(batch.JOURNALS.get(handle)), 1)  # kept for a retry
            doc.resizeImage(0, 0, 40, 40)
            doc.waitForDone()
            self.assertOk(run("undo", doc=handle))
            self.assertEqual(state(doc, handle), start)

    def test_dry_run_same_size_and_errors(self):
        with scratch_document(W, H, name="kmcp-canvas-errors") as doc:
            handle, ids, data = self.build(doc)
            start = state(doc, handle)
            dry = self.resize(handle, 40, 30, anchor="bottom", dry_run=True)
            self.assertEqual((dry["dry_run"], dry["offset"], dry["changed"]),
                             (True, {"x": 4, "y": 6}, True))
            self.assertEqual([w["code"] for w in dry["warnings"]], ["rig_breaking"])
            same = self.resize(handle, W, H, anchor="right")
            self.assertEqual((same["changed"], same["offset"]), (False, {"x": 0, "y": 0}))
            self.assertEqual([w["code"] for w in same["warnings"]], ["rig_breaking"])
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)
            self.assertEqual(state(doc, handle), start)

            def bad(code, **params):
                return self.assertError(run("set_canvas_size", doc=handle, **params), code)

            self.assertEqual(bad("too_large", width=20000, height=10)["details"]["limit"],
                             "max_canvas_dim")
            self.assertEqual(bad("too_large", width=16000, height=16000)["details"]
                             ["limit"], "max_canvas_pixels")
            err = bad("bad_request", width=40, height=40, anchor="middle")
            self.assertEqual(err["details"]["param"], "anchor")
            self.assertIn("top-left", err["details"]["valid"])
            self.assertEqual(bad("bad_request", width=40)["details"]["param"], "height")
            bad("bad_request", width=0, height=10)
            bad("bad_request", width="40", height=10)
            bad("bad_request", width=40, height=40, dry_run="yes")
            self.assertEqual(bad("bad_request", width=40, height=40, ancor="top")["details"]
                             ["unknown"], ["ancor"])
            self.assertError(run("set_canvas_size", doc="doc-999", width=4, height=4), "not_found")
            self.assertError(run("set_canvas_size", width=4, height=4), "not_found")  # no active
            self.assertEqual(state(doc, handle), start)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)


# --- diff_documents ------------------------------------------------------------

def _source_specs():
    return [
        {"name": "Body"},
        {"key": "head", "name": "Head", "type": "group"},
        {"key": "right", "name": "Right", "type": "group", "parent": "$head"},
        {"name": "Line", "parent": "$right"},
        {"key": "left", "name": "Left", "type": "group", "parent": "$head"},
        {"name": "Line", "parent": "$left"},
        {"name": "Eye L", "parent": "$head"},
        {"name": "Eye R", "parent": "$head"},
        {"name": "Hat"},
    ]


class DiffDocumentsTest(Base):
    def build_source(self, doc):
        """32x24: Hat, Head (group: Eye R, Eye L, Left (group: Line), Right
        (group: Line)), Body, Background (white), top to bottom: 10 nodes.
        Returns (handle, ids by path)."""
        handle = registry.handle_for(doc)
        created = self.assertOk(run("create_layers", doc=handle,
                                    specs=_source_specs()))["created"]
        ids = {c["path"]: c["id"] for c in created}
        fills = {"Body": ((4, 4, 10, 8), solid(10, 8, (40, 50, 60, 255))),
                 "Head/Eye L": ((16, 2, 3, 3), pattern(3, 3, 1)),
                 "Head/Eye R": ((22, 2, 3, 3), pattern(3, 3, 2)),
                 "Head/Left/Line": ((15, 8, 4, 1), solid(4, 1, (0, 0, 0, 255))),
                 "Head/Right/Line": ((21, 8, 4, 1), solid(4, 1, (0, 0, 0, 200))),
                 "Hat": ((14, 0, 12, 2), pattern(12, 2, 3))}
        for path, (r, d) in fills.items():
            self.assertTrue(nodes.node_by_id(doc, ids[path]).setPixelData(d, *r))
        settle(doc)
        ids["Background"] = nodes.node_id(nodes.resolve_node(doc, "Background"))
        return handle, ids

    def diff(self, a, b, **params):
        return self.assertOk(run("diff_documents", doc_a=a, doc_b=b, **params))

    def assertSame(self, result, matched):
        for key in ("added", "removed", "renamed", "moved", "changed", "pixel_changes"):
            self.assertEqual(result[key], [], key)
        self.assertIs(result["projection_changed"], False)
        self.assertEqual(result["document_changes"], {})
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["counts"], {
            "added": 0, "removed": 0, "renamed": 0, "moved": 0, "changed": 0,
            "pixel_changes": 0, "alpha0_rgb_only_layers": 0, "matched": matched})
        self.assertEqual(result["projection"], {"changed_count": 0, "bounds": None,
                                                "max_channel_diff": 0, "alpha0_rgb_count": 0,
                                                "alpha0_rgb_bounds": None})

    def saved_copy(self, handle, name="copy.kra"):
        kra = self.assertOk(run("save", doc=handle, path=os.path.join(self.root, "a.kra")))["path"]
        return self.open_copy(kra, name)

    def test_kra_copy_matches_by_uuid_and_reports_each_kind(self):
        with scratch_document(W, H, name="kmcp-diff-a") as doc:
            ha, ids = self.build_source(doc)
            hb = self.saved_copy(ha)
            result = self.diff(ha, hb)
            self.assertEqual((result["doc_a"], result["doc_b"]), (ha, hb))
            self.assertSame(result, {"uuid": 10, "path": 0, "name": 0, "total": 10})

            b = registry.lookup(hb)
            self.assertOk(run("update_layers", doc=hb, changes=[
                {"id": ids["Body"], "name": "Torso"},
                {"id": ids["Head/Eye L"], "opacity": 0.5, "blend_mode": "multiply"}]))
            self.assertOk(run("move_layers", doc=hb, moves=[{"id": ids["Head/Eye R"],
                                                             "parent": "/", "above": "Hat"}]))
            self.assertOk(run("delete_layers", doc=hb, ids=[ids["Hat"]]))
            new = self.assertOk(run("create_layers", doc=hb, specs=[{"name": "New"}]))
            new_id = new["created"][0]["id"]
            body = nodes.node_by_id(b, ids["Body"])
            self.assertTrue(body.setPixelData(solid(3, 2, (0, 0, 255, 255)), 10, 10, 3, 2))
            # Colour under alpha 0 where Body is empty: not a change.
            self.assertTrue(body.setPixelData(bytes((1, 2, 3, 0)), 20, 20, 1, 1))
            eye_l = nodes.node_by_id(b, ids["Head/Eye L"])
            self.assertTrue(eye_l.setPixelData(solid(2, 1, (5, 5, 5, 255)), 30, 0, 2, 1))
            settle(b)
            del b, body, eye_l

            result = self.diff(ha, hb)
            self.assertEqual(result["added"], [{"id": new_id, "name": "New", "path": "New",
                                                "type": "paintlayer"}])
            self.assertEqual(result["removed"], [{"id": ids["Hat"], "name": "Hat", "path": "Hat",
                                                  "type": "paintlayer"}])
            self.assertEqual(result["renamed"], [{"id_a": ids["Body"], "id_b": ids["Body"],
                                                  "path": "Torso", "path_a": "Body",
                                                  "match": "uuid", "name": ["Body", "Torso"]}])
            self.assertEqual(result["moved"], [{
                "id_a": ids["Head/Eye R"], "id_b": ids["Head/Eye R"], "path": "Eye R",
                "path_a": "Head/Eye R", "match": "uuid", "parent": [ids["Head"], None],
                "index": [3, 3], "parent_changed": True}])
            self.assertEqual(result["changed"], [{
                "id_a": ids["Head/Eye L"], "id_b": ids["Head/Eye L"], "path": "Head/Eye L",
                "match": "uuid", "fields": {"opacity": [1.0, 0.502],
                                            "blend_mode": ["normal", "multiply"]}}])
            stats = ("max_channel_diff", "alpha0_rgb_count", "alpha0_rgb_bounds")
            self.assertEqual([{k: v for k, v in e.items() if k not in stats}
                              for e in result["pixel_changes"]], [
                {"id_a": ids["Head/Eye L"], "id_b": ids["Head/Eye L"], "path": "Head/Eye L",
                 "match": "uuid", "changed_count": 2, "bounds": {"x": 30, "y": 0, "w": 2, "h": 1}},
                {"id_a": ids["Body"], "id_b": ids["Body"], "path": "Torso", "path_a": "Body",
                 "match": "uuid", "changed_count": 6,
                 "bounds": {"x": 10, "y": 10, "w": 3, "h": 2}}])
            eye_change, body_change = result["pixel_changes"]
            self.assertEqual(eye_change["max_channel_diff"], 255)  # alpha 0 -> 255
            # The colour under alpha 0 at (20, 20) is counted apart (F003 K4).
            self.assertEqual((body_change["alpha0_rgb_count"], body_change["alpha0_rgb_bounds"]),
                             (1, {"x": 20, "y": 20, "w": 1, "h": 1}))
            self.assertIs(result["projection_changed"], True)
            self.assertEqual(result["counts"], {
                "added": 1, "removed": 1, "renamed": 1, "moved": 1, "changed": 1,
                "pixel_changes": 2, "alpha0_rgb_only_layers": 0,
                "matched": {"uuid": 9, "path": 0, "name": 0, "total": 9}})
            strict = self.diff(ha, hb, ignore_alpha0_rgb=False)
            self.assertEqual(len(strict["pixel_changes"]), 2)
            self.assertIs(strict["ignore_alpha0_rgb"], False)
            # The other way round: added and removed swap.
            back = self.diff(hb, ha)
            self.assertEqual([e["id"] for e in back["added"]], [ids["Hat"]])
            self.assertEqual([e["id"] for e in back["removed"]], [new_id])
            self.assertEqual(back["renamed"][0]["name"], ["Torso", "Body"])

    def rebuilt_copy(self, handle, doc2):
        """``doc2`` (a fresh scratch document) rebuilt from ``handle``'s
        export_layers manifest: new ids throughout."""
        out = os.path.join(self.root, "export")
        exported = self.assertOk(run("export_layers", doc=handle, dir=out))
        h2 = registry.handle_for(doc2)
        self.assertOk(run("delete_layers", doc=h2, ids=["Background"]))
        self.assertOk(run("create_layers", doc=h2, manifest=exported["manifest"]))
        settle(doc2)
        return h2

    def test_rebuilt_copy_matches_by_path_then_unique_name(self):
        with scratch_document(W, H, name="kmcp-diff-src") as doc, \
                scratch_document(W, H, name="kmcp-diff-rebuilt") as doc2:
            ha, ids = self.build_source(doc)
            hb = self.rebuilt_copy(ha, doc2)
            result = self.diff(ha, hb)
            self.assertSame(result, {"uuid": 0, "path": 10, "name": 0, "total": 10})

            new_ids = {e["path"]: e["id"] for e in run("list_layers", doc=hb)["result"]["layers"]}
            # Eye R to the top level: its path changes, its unique name
            # still matches. One Line (a repeated name) to the top level:
            # neither its path nor its name can match.
            self.assertOk(run("move_layers", doc=hb, moves=[
                {"id": new_ids["Head/Eye R"], "parent": "/", "above": "Hat"},
                {"id": new_ids["Head/Left/Line"], "parent": "/", "above": "Eye R"}]))
            result = self.diff(ha, hb)
            self.assertEqual(result["moved"], [{
                "id_a": ids["Head/Eye R"], "id_b": new_ids["Head/Eye R"], "path": "Eye R",
                "path_a": "Head/Eye R", "match": "name", "parent": [ids["Head"], None],
                "index": [3, 4], "parent_changed": True}])
            self.assertEqual(result["added"], [{"id": new_ids["Head/Left/Line"], "name": "Line",
                                                "path": "Line", "type": "paintlayer"}])
            self.assertEqual(result["removed"], [{"id": ids["Head/Left/Line"], "name": "Line",
                                                  "path": "Head/Left/Line",
                                                  "type": "paintlayer"}])
            self.assertEqual(result["counts"]["matched"],
                             {"uuid": 0, "path": 8, "name": 1, "total": 9})
            self.assertEqual(result["pixel_changes"], [])
            self.assertIs(result["projection_changed"], False)

    def test_size_mismatch_color_space_and_no_pixels(self):
        with scratch_document(W, H, name="kmcp-diff-a") as doc, \
                scratch_document(W + 1, H, name="kmcp-diff-b") as doc2:
            ha, hb = registry.handle_for(doc), registry.handle_for(doc2)
            result = self.diff(ha, hb)
            self.assertIsNone(result["pixel_changes"])
            self.assertIsNone(result["projection_changed"])
            self.assertIsNone(result["counts"]["pixel_changes"])
            self.assertEqual([w["code"] for w in result["warnings"]], ["size_mismatch"])
            self.assertEqual(result["document_changes"], {"width": [W, W + 1]})
            # Both Backgrounds: different ids, same path.
            self.assertEqual(result["counts"]["matched"],
                             {"uuid": 0, "path": 1, "name": 0, "total": 1})
            off = self.diff(ha, hb, pixels=False)
            self.assertEqual((off["pixel_changes"], off["projection_changed"], off["warnings"]),
                             (None, None, []))
        with scratch_document(W, H, name="kmcp-diff-16a", color_depth="U16") as doc, \
                scratch_document(W, H, name="kmcp-diff-16b", color_depth="U16") as doc2:
            result = self.diff(registry.handle_for(doc), registry.handle_for(doc2))
            self.assertIsNone(result["pixel_changes"])
            self.assertEqual([w["code"] for w in result["warnings"]], ["not_rgba8"])

    def test_errors(self):
        with scratch_document(W, H, name="kmcp-diff-err") as doc:
            ha = registry.handle_for(doc)
            err = self.assertError(run("diff_documents", doc_a=ha, doc_b=ha), "bad_request")
            self.assertEqual(err["details"]["param"], "doc_b")
            self.assertError(run("diff_documents", doc_a=ha), "bad_request")
            self.assertError(run("diff_documents", doc_b=ha), "bad_request")
            self.assertError(run("diff_documents", doc_a=ha, doc_b="doc-999"), "not_found")
            self.assertError(run("diff_documents", doc_a=ha, doc_b="x"), "bad_request")
            with scratch_document(W, H, name="kmcp-diff-err2") as doc2:
                hb = registry.handle_for(doc2)
                self.assertError(run("diff_documents", doc_a=ha, doc_b=hb, pixels="yes"),
                                 "bad_request")
                err = self.assertError(run("diff_documents", doc_a=ha, doc_b=hb, doc=ha),
                                       "bad_request")
                self.assertEqual(err["details"]["unknown"], ["doc"])


if __name__ == "__main__":
    unittest.main()
