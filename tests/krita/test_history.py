"""commands/history.py: the agent journal's undo / redo / journal, and
ui_undo / ui_redo, headless, through dispatch.execute (P5.1, D6).

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. A document's state is compared
as a whole: the full ``list_layers`` dump (ids included), every node's
layer object and pixels over a window wider than the canvas, and the
projection.
"""

import contextlib
import os
import shutil
import tempfile
import unittest

from helpers import add_paint_layer, scratch_document, settle, temp_config
from kritamcp import batch, nodes, pixels, registry
from test_dispatch import EnvelopeAssertions, run
from test_pixels import pattern

W, H = 32, 24
WIN = (-8, -8, W + 16, H + 16)  # pixels past the canvas edge count too


def state(doc, handle):
    """Everything undo must restore, as plain data."""
    listed = run("list_layers", doc=handle)
    assert listed["ok"], listed
    index, by_id = nodes.tree(doc)
    per_node = {}
    for nid, node in by_id.items():
        obj = nodes.layer_object(node, index, nid=nid)
        px = pixels.as_bytes(node.pixelData(*WIN)) if obj["type"] == "paintlayer" else None
        per_node[nid] = (obj, px)
    settle(doc)
    projection = pixels.as_bytes(doc.pixelData(0, 0, W, H))
    return {"layers": listed["result"]["layers"], "nodes": per_node, "projection": projection}


class HistoryBase(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-history-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def start(self, doc, specs=None):
        """Some layers with pixels; a fresh journal. Returns (handle, ids)."""
        handle = registry.handle_for(doc)
        result = self.assertOk(run("create_layers", doc=handle, specs=specs or [
            {"name": "P"},
            {"key": "g", "name": "G", "type": "group"},
            {"name": "Q", "parent": "$g", "blend_mode": "multiply"},
            {"key": "h", "name": "H", "type": "group", "parent": "$g"},
            {"name": "Y", "parent": "$h", "opacity": 100 / 255},
        ]))
        ids = {c["name"]: c["id"] for c in result["created"]}
        for name, rect, seed in (("P", (0, 0, W, H), 1), ("Q", (5, 3, 9, 7), 2),
                                 ("Y", (-3, 10, 12, 6), 3)):
            if name in ids:
                x, y, w, h = rect
                node = nodes.node_by_id(doc, ids[name])
                self.assertTrue(node.setPixelData(pattern(w, h, seed), x, y, w, h))
        settle(doc)
        batch.JOURNALS.forget(handle)  # a fresh journal, with the current config
        return handle, ids

    def undo(self, handle, label=None):
        result = self.assertOk(run("undo", doc=handle))
        if label is not None:
            self.assertEqual(result["undone"]["label"], label)
        return result

    def redo(self, handle, label=None):
        result = self.assertOk(run("redo", doc=handle))
        if label is not None:
            self.assertEqual(result["redone"]["label"], label)
        return result

    def counts(self, handle):
        j = batch.JOURNALS.get(handle)
        return len(j), j.redo_count


class UndoRedoTest(HistoryBase):
    def test_five_tools_undo_five_times_then_redo_five_times(self):
        png = os.path.join(self.root, "n2.png")
        pixels.save_png(pattern(6, 5, 9), png, 6, 5)
        with scratch_document(W, H, name="kmcp-history-five") as doc:
            handle, ids = self.start(doc)
            states = [state(doc, handle)]
            created = self.assertOk(run("create_layers", doc=handle, specs=[
                {"name": "N1"},
                {"key": "g2", "name": "G2", "type": "group"},
                {"name": "N2", "parent": "$g2"}]))
            ids.update({c["name"]: c["id"] for c in created["created"]})
            states.append(state(doc, handle))
            self.assertOk(run("update_layers", doc=handle, changes=[
                {"id": "P", "name": "P2", "opacity": 0.5, "visible": False},
                {"id": "G", "pass_through": True, "blend_mode": "screen"}]))
            states.append(state(doc, handle))
            self.assertOk(run("move_layers", doc=handle, moves=[
                {"id": "N1", "parent": "G", "index": 0},
                {"id": "P2", "index": 0}]))
            states.append(state(doc, handle))
            deleted = self.assertOk(run("delete_layers", doc=handle, ids=["G"]))
            self.assertEqual(set(deleted["deleted"]),
                             {ids["G"], ids["H"], ids["Y"], ids["Q"], ids["N1"]})
            states.append(state(doc, handle))
            self.assertOk(run("import_png", doc=handle, id="G2/N2", png=png, x=30, y=-2))
            states.append(state(doc, handle))
            for i in range(1, len(states)):
                self.assertNotEqual(states[i], states[i - 1], i)
            self.assertEqual(self.counts(handle), (5, 0))

            labels = ["create_layers", "update_layers", "move_layers", "delete_layers",
                      "import_png"]
            for step in range(5, 0, -1):
                result = self.undo(handle, labels[step - 1])
                self.assertEqual(result["remaining"], {"undo": step - 1, "redo": 6 - step})
                self.assertEqual(result["warnings"], [])
                self.assertEqual(state(doc, handle), states[step - 1], labels[step - 1])
            self.assertEqual(result["undone"]["layers"], [ids["N1"], ids["G2"], ids["N2"]])
            err = self.assertError(run("undo", doc=handle), "conflict")
            self.assertEqual(err["message"], f"nothing to undo in {handle}")

            for step in range(1, 6):
                result = self.redo(handle, labels[step - 1])
                self.assertEqual(result["remaining"], {"undo": step, "redo": 5 - step})
                self.assertEqual(state(doc, handle), states[step], labels[step - 1])
            self.assertError(run("redo", doc=handle), "conflict")
            # And back again, from the redone entries.
            for step in range(5, 0, -1):
                self.undo(handle)
            self.assertEqual(state(doc, handle), states[0])

    def test_stroke_and_fill_undo_twice(self):
        with scratch_document(W, H, name="kmcp-history-paint") as doc:
            handle, ids = self.start(doc, specs=[{"name": "S"}])
            node = nodes.node_by_id(doc, ids["S"])
            self.assertTrue(node.setPixelData(pattern(20, 10, 4), 4, 4, 20, 10))
            settle(doc)
            before = state(doc, handle)
            self.assertOk(run("set_color", color="#3366cc"))
            stroke = self.assertOk(run("stroke", doc=handle, layer="S",
                                       points=[[2, 2], [20, 15], [30, 5]], size=5))
            self.assertNotIn("warnings", stroke)
            mid = state(doc, handle)
            self.assertOk(run("fill", doc=handle, layer="S", x=10, y=10, radius=6))
            after = state(doc, handle)
            self.assertNotEqual(before, mid)
            self.assertNotEqual(mid, after)
            self.assertEqual([r["label"] for r in self.assertOk(
                run("journal", doc=handle))["undo"]], ["fill", "stroke"])
            self.undo(handle, "fill")
            self.assertEqual(state(doc, handle), mid)
            self.undo(handle, "stroke")
            self.assertEqual(state(doc, handle), before)
            self.redo(handle, "stroke")
            self.redo(handle, "fill")
            self.assertEqual(state(doc, handle), after)

    def test_draw_shape_and_clear(self):
        with scratch_document(W, H, name="kmcp-history-shape") as doc:
            handle, ids = self.start(doc, specs=[{"name": "S"}])
            node = nodes.node_by_id(doc, ids["S"])
            self.assertTrue(node.setPixelData(pattern(10, 10, 5), 36, -4, 10, 10))  # off canvas
            settle(doc)
            states = [state(doc, handle)]
            for action, params in (
                    ("draw_shape", {"shape": "rectangle", "x": 2, "y": 2, "width": 9,
                                    "height": 5}),
                    ("draw_shape", {"shape": "ellipse", "x": 8, "y": 6, "width": 12,
                                    "height": 9}),
                    ("draw_shape", {"shape": "line", "x": 0, "y": 20, "x2": 31, "y2": 0}),
                    ("clear", {"color": "#ff8800"}),
                    ("clear", {"color": "transparent"})):
                self.assertOk(run(action, doc=handle, layer="S", **params))
                states.append(state(doc, handle))
                self.assertNotEqual(states[-1], states[-2], action)
            for step in range(len(states) - 1, 0, -1):
                self.undo(handle)
                self.assertEqual(state(doc, handle), states[step - 1], step)

    def test_group_subtree_delete_undo_redo(self):
        with scratch_document(W, H, name="kmcp-history-group") as doc:
            handle, ids = self.start(doc)
            mask = doc.createTransparencyMask("M")
            self.assertTrue(nodes.node_by_id(doc, ids["Q"]).addChildNode(mask, None))
            settle(doc)
            before = state(doc, handle)
            self.assertOk(run("delete_layers", doc=handle, ids=["G"]))
            gone = state(doc, handle)
            for name in ("G", "H", "Y", "Q"):
                self.assertNotIn(ids[name], gone["nodes"])
            result = self.undo(handle, "delete_layers")
            self.assertEqual(result["undone"]["layers"],
                             [ids["G"], ids["H"], ids["Y"], ids["Q"], nodes.node_id(mask)])
            self.assertEqual(state(doc, handle), before)
            self.redo(handle, "delete_layers")
            self.assertEqual(state(doc, handle), gone)
            self.undo(handle)
            self.assertEqual(state(doc, handle), before)
            index = nodes.build_index(doc)
            self.assertEqual(index.path_of(ids["Y"]), "G/H/Y")

    def test_undone_create_redoes_with_the_same_ids(self):
        png = os.path.join(self.root, "c.png")
        pixels.save_png(pattern(4, 4, 6), png, 4, 4)
        with scratch_document(W, H, name="kmcp-history-create") as doc:
            handle, ids = self.start(doc)
            before = state(doc, handle)
            created = self.assertOk(run("create_layers", doc=handle, specs=[
                {"key": "k", "name": "K", "type": "group", "parent": "G", "above": "Q"},
                {"name": "C", "parent": "$k", "png": png, "x": 1, "y": 2}]))
            after = state(doc, handle)
            self.undo(handle, "create_layers")
            self.assertEqual(state(doc, handle), before)
            self.redo(handle, "create_layers")
            self.assertEqual(state(doc, handle), after)
            self.assertEqual({c["id"] for c in created["created"]},
                             set(after["nodes"]) - set(before["nodes"]))


class RefusalTest(HistoryBase):
    def test_layer_removed_behind_the_journals_back(self):
        with scratch_document(W, H, name="kmcp-history-gone") as doc:
            handle, ids = self.start(doc)
            self.assertOk(run("update_layers", doc=handle,
                              changes=[{"id": "Q", "opacity": 0.25}]))
            node = nodes.node_by_id(doc, ids["Q"])
            self.assertTrue(node.remove())  # by hand: the journal doesn't know
            settle(doc)
            before = state(doc, handle)
            err = self.assertError(run("undo", doc=handle), "conflict")
            self.assertIn("update_layers", err["message"])
            self.assertIn(ids["Q"], err["message"])
            self.assertIn("nothing was changed", err["message"])
            self.assertEqual(err["details"]["layers"], [ids["Q"]])
            self.assertEqual(state(doc, handle), before)
            self.assertEqual(self.counts(handle), (1, 0))  # kept: fix it and retry
            parent = nodes.node_by_id(doc, ids["G"])
            self.assertTrue(parent.addChildNode(node, None))
            settle(doc)
            self.undo(handle, "update_layers")
            self.assertEqual(nodes.node_by_id(doc, ids["Q"]).opacity(), 255)

    def test_reinsert_refused_when_the_parent_is_gone(self):
        with scratch_document(W, H, name="kmcp-history-parent") as doc:
            handle, ids = self.start(doc)
            self.assertOk(run("delete_layers", doc=handle, ids=["G/H"]))
            self.assertTrue(nodes.node_by_id(doc, ids["G"]).remove())  # by hand
            settle(doc)
            err = self.assertError(run("undo", doc=handle), "conflict")
            self.assertIn(ids["G"], err["message"])
            self.assertEqual(self.counts(handle), (1, 0))

    def test_empty_journal(self):
        with scratch_document(W, H, name="kmcp-history-empty") as doc:
            handle = registry.handle_for(doc)
            batch.JOURNALS.forget(handle)
            err = self.assertError(run("undo", doc=handle), "conflict")
            self.assertEqual(err["details"], {"doc": handle, "undo": 0, "redo": 0})
            self.assertError(run("redo", doc=handle), "conflict")
            listed = self.assertOk(run("journal", doc=handle))
            self.assertEqual((listed["doc"], listed["undo"], listed["redo"]), (handle, [], []))
            self.assertError(run("undo"), "not_found")  # headless: no active document

    def test_new_edit_clears_redo(self):
        with scratch_document(W, H, name="kmcp-history-clears") as doc:
            handle, ids = self.start(doc)
            self.assertOk(run("update_layers", doc=handle, changes=[{"id": "P", "name": "A"}]))
            self.undo(handle)
            self.assertEqual(self.counts(handle), (0, 1))
            self.assertOk(run("fill", doc=handle, layer="P", x=3, y=3, radius=2))
            self.assertEqual(self.counts(handle), (1, 0))
            self.assertError(run("redo", doc=handle), "conflict")


class SpillTest(HistoryBase):
    def test_tiny_memory_cap_spills_and_undo_is_exact(self):
        png = os.path.join(self.root, "s.png")
        pixels.save_png(pattern(10, 10, 8), png, 10, 10)
        with temp_config({"allowed_dirs": [self.root], "journal": {"max_bytes": 64}}):
            with scratch_document(W, H, name="kmcp-history-spill") as doc:
                handle, ids = self.start(doc)
                journal = batch.JOURNALS.get(handle)
                self.assertEqual(journal.max_bytes, 64)
                states = [state(doc, handle)]
                self.assertOk(run("import_png", doc=handle, id="P", png=png, x=3, y=3))
                states.append(state(doc, handle))
                self.assertOk(run("stroke", doc=handle, layer="P", points=[[1, 1], [25, 20]]))
                states.append(state(doc, handle))
                self.assertOk(run("clear", doc=handle, layer="Q", color="transparent"))
                states.append(state(doc, handle))
                self.assertEqual(journal.memory_bytes, 0)  # every patch is over 64 bytes
                files = journal.spill_files()
                self.assertEqual(len(files), 3)
                spill_root = batch.process_spill_dir()
                for f in files:
                    self.assertTrue(os.path.isfile(f))
                    self.assertEqual(os.path.dirname(f), spill_root)
                listed = self.assertOk(run("journal", doc=handle))
                self.assertEqual([r["on_disk"] for r in listed["undo"]], [True] * 3)
                self.assertEqual(listed["disk_bytes"], journal.disk_bytes)
                for step in range(3, 0, -1):
                    self.undo(handle)
                    self.assertEqual(state(doc, handle), states[step - 1])
                for f in files:
                    self.assertFalse(os.path.exists(f))  # applied: deleted
                redo_files = journal.spill_files()  # the redo snapshots spilled too
                self.assertEqual(len(redo_files), 3)
                self.redo(handle)
                self.assertEqual(state(doc, handle), states[1])
                left = journal.spill_files()
                self.assertTrue(left)
            # The document closed: the next registry call forgets its
            # journal and deletes the files.
            registry.list_open()
            self.assertNotIn(handle, batch.JOURNALS)
            for f in redo_files + left:
                self.assertFalse(os.path.exists(f), f)

    def test_delete_over_the_memory_cap_is_not_undoable(self):
        with temp_config({"allowed_dirs": [self.root], "journal": {"max_bytes": 64}}):
            with scratch_document(W, H, name="kmcp-history-nospill") as doc:
                handle, ids = self.start(doc)
                self.assertOk(run("update_layers", doc=handle,
                                  changes=[{"id": "P", "visible": False}]))
                result = self.assertOk(run("delete_layers", doc=handle, ids=["P"]))
                self.assertEqual([w["code"] for w in result["warnings"]], ["not_undoable"])
                self.assertEqual(self.counts(handle), (0, 0))


class UiUndoTest(HistoryBase):
    def test_headless_has_no_view(self):
        with scratch_document(W, H, name="kmcp-history-ui") as doc:
            handle, ids = self.start(doc)
            before = state(doc, handle)
            for action in ("ui_undo", "ui_redo"):
                err = self.assertError(run(action, doc=handle), "conflict")
                self.assertIn("not shown in a Krita window", err["message"])
                self.assertEqual(err["details"], {"doc": handle})
                err = self.assertError(run(action), "conflict")
                self.assertIn("No active document", err["message"])
            self.assertEqual(state(doc, handle), before)


if __name__ == "__main__":
    unittest.main()
