"""commands/layers.py: update_layers, move_layers, delete_layers, headless,
through dispatch.execute.

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. Undo is checked by popping the
call's journal entry and running its inverses through ``batch.rollback``
with the live document; ``undo`` / ``redo`` themselves are covered in
test_history.py.
"""

import unittest

from helpers import scratch_document, temp_config
from kritamcp import batch, nodes, registry
from kritamcp.commands import layers
from test_dispatch import EnvelopeAssertions, run

# The tree ``build`` makes, top to bottom (Layers docker order):
#
#   C
#   G (group)
#     H (group)
#       Y        pixels, opacity 100/255
#     X          pixels, multiply
#   K (group, empty)
#   B
#   A            pixels, with a transparency mask M
#     M
#   Background
SPECS = [
    {"name": "A"},
    {"name": "B"},
    {"name": "K", "type": "group"},
    {"key": "g", "name": "G", "type": "group"},
    {"name": "X", "parent": "$g", "blend_mode": "multiply"},
    {"key": "h", "name": "H", "type": "group", "parent": "$g"},
    {"name": "Y", "parent": "$h", "opacity": 100 / 255},
    {"name": "C"},
]


def pattern(w, h, seed):
    return bytes((i * 31 + seed * 17 + (i // 4) * 3) % 256 for i in range(4 * w * h))


def snapshot(doc):
    """{id: (layer object, pixel bytes over its bounds or None)} for every node."""
    index, by_id = nodes.tree(doc)
    out = {}
    for nid, node in by_id.items():
        obj = nodes.layer_object(node, index, nid=nid)
        px, b = None, obj["bounds"]
        if obj["type"] == "paintlayer" and b:
            px = bytes(node.pixelData(b["x"], b["y"], b["w"], b["h"]))
        out[nid] = (obj, px)
    return out


def paint_bytes(snap, nids):
    """w * h * 4 summed over the paint layers ``nids`` (their bounds in ``snap``)."""
    total = 0
    for nid in nids:
        obj = snap[nid][0]
        if obj["type"] == "paintlayer" and obj["bounds"]:
            total += obj["bounds"]["w"] * obj["bounds"]["h"] * 4
    return total


PLACE = ("path", "parent", "index")


def without_place(entry):
    """A snapshot entry without where the node sits (for nodes that moved)."""
    obj, px = entry
    return {k: v for k, v in obj.items() if k not in PLACE}, px


def top_down(doc, parent=None):
    """Names of ``parent``'s children (an id, or None for the root), top to bottom."""
    index = nodes.build_index(doc)
    return [index.get(c)["name"] for c in index.children(parent or "/")]


class TreeBase(EnvelopeAssertions, unittest.TestCase):
    def build(self, doc):
        """Make the tree above; return (handle, {name: id})."""
        handle = registry.handle_for(doc)
        result = self.assertOk(run("create_layers", doc=handle, specs=SPECS))
        ids = {c["name"]: c["id"] for c in result["created"]}
        index = nodes.build_index(doc)
        ids["Background"] = index.by_name("Background")[0]
        for name, rect, seed in (("A", (1, 2, 3, 4), 1), ("X", (5, 0, 4, 3), 2),
                                 ("Y", (-2, 6, 5, 2), 3)):
            node = nodes.node_by_id(doc, ids[name])
            x, y, w, h = rect
            self.assertTrue(node.setPixelData(pattern(w, h, seed), x, y, w, h))
        mask = doc.createTransparencyMask("M")
        self.assertTrue(nodes.node_by_id(doc, ids["A"]).addChildNode(mask, None))
        ids["M"] = nodes.node_id(mask)
        doc.refreshProjection()
        doc.waitForDone()
        batch.JOURNALS.get(handle).clear()  # tests count only their own entries
        return handle, ids

    def undo_last(self, handle, doc, label):
        entry = batch.JOURNALS.get(handle).pop()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.label, label)
        ok, errors = batch.rollback(list(entry.inverses), doc)
        self.assertEqual((ok, errors), (len(entry.inverses), []))
        doc.refreshProjection()
        doc.waitForDone()
        return entry


# --- update_layers ---------------------------------------------------------

class UpdateLayersTest(TreeBase):
    def test_every_property_and_undo(self):
        with scratch_document(16, 16, name="kmcp-update-props") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            result = self.assertOk(run("update_layers", doc=handle, changes=[
                {"id": "A", "name": "A2", "visible": False, "opacity": 0.5,
                 "blend_mode": "screen", "locked": True, "alpha_locked": True,
                 "inherit_alpha": True},
                {"id": "G/H", "pass_through": True, "opacity": 0.25, "blend_mode": None},
                {"id": ids["Y"], "opacity": 0.0},
            ]))
            self.assertEqual(result, {
                "doc": handle, "dry_run": False, "warnings": [],
                "updated": [
                    {"id": ids["A"], "path": "A2",
                     "changed": ["name", "visible", "opacity", "blend_mode", "locked",
                                 "alpha_locked", "inherit_alpha"]},
                    {"id": ids["H"], "path": "G/H", "changed": ["opacity", "pass_through"]},
                    {"id": ids["Y"], "path": "G/H/Y", "changed": ["opacity"]}]})
            after = snapshot(doc)
            a = after[ids["A"]][0]
            self.assertEqual((a["name"], a["visible"], a["opacity"], a["blend_mode"], a["locked"],
                              a["alpha_locked"], a["inherit_alpha"]),
                             ("A2", False, 0.502, "screen", True, True, True))
            h = after[ids["H"]][0]
            self.assertEqual((h["pass_through"], h["opacity"], h["blend_mode"]),
                             (True, 0.251, "normal"))
            self.assertEqual(after[ids["Y"]][0]["opacity"], 0.0)
            # Untouched fields and other layers are unchanged.
            self.assertEqual(after[ids["A"]][1], before[ids["A"]][1])
            # M's path is now A2/M; G's bounds follow its children's pixels.
            for nid in set(before) - {ids["A"], ids["M"], ids["H"], ids["Y"], ids["G"]}:
                self.assertEqual(after[nid], before[nid])
            self.assertEqual(len(batch.JOURNALS.get(handle)), 1)

            entry = self.undo_last(handle, doc, "update_layers")
            self.assertEqual([inv.node_ids for inv in entry.inverses],
                             [(ids["A"],), (ids["H"],), (ids["Y"],)])
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(nodes.node_by_id(doc, ids["Y"]).opacity(), 100)  # exact 0-255

    def test_unlock_and_relock(self):
        with scratch_document(16, 16, name="kmcp-update-lock") as doc:
            handle, ids = self.build(doc)
            self.assertOk(run("update_layers", doc=handle, changes=[{"id": "X", "locked": True}]))
            locked = snapshot(doc)
            self.assertOk(run("update_layers", doc=handle, changes=[
                {"id": "X", "locked": False, "name": "X open", "alpha_locked": True}]))
            x = snapshot(doc)[ids["X"]][0]
            self.assertEqual((x["locked"], x["name"], x["alpha_locked"]), (False, "X open", True))
            self.undo_last(handle, doc, "update_layers")
            self.assertEqual(snapshot(doc), locked)

    def test_duplicate_rename_warns(self):
        with scratch_document(16, 16, name="kmcp-update-dup") as doc:
            handle, ids = self.build(doc)
            result = self.assertOk(run("update_layers", doc=handle, changes=[
                {"id": "C", "name": "B"}]))
            self.assertEqual(len(result["warnings"]), 1)
            w = result["warnings"][0]
            self.assertEqual((w["code"], w["id"], w["name"], w["others"]),
                             ("duplicate_name", ids["C"], "B", [ids["B"]]))
            self.assertIn("ambiguous", w["message"])
            self.assertEqual(top_down(doc)[0], "B")
            # A swap ends with unique names: no warning. Renaming to its own
            # name is not a new duplicate either.
            result = self.assertOk(run("update_layers", doc=handle, changes=[
                {"id": ids["C"], "name": "A"}, {"id": "A", "name": "B"},
                {"id": ids["B"], "name": "C"}, {"id": "K", "name": "K"}]))
            self.assertEqual(result["warnings"], [])
            self.assertNotIn("notes", result)

    def test_rename_to_a_name_of_the_other_kind_is_a_note(self):
        """F004 K9: lint's suggested_names can make a layer share its parent
        group's name (Wand A / Wand A), which the naming rules allow: a note,
        not a warning. Same-kind clashes still warn."""
        with scratch_document(16, 16, name="kmcp-update-kind") as doc:
            handle, ids = self.build(doc)
            dry = self.assertOk(run("update_layers", doc=handle, dry_run=True,
                                    changes=[{"id": "G/X", "name": "G"}]))
            result = self.assertOk(run("update_layers", doc=handle,
                                       changes=[{"id": "G/X", "name": "G"}]))
            for r in (dry, result):
                self.assertEqual(r["warnings"], [])
                (note,) = r["notes"]
                self.assertEqual((note["code"], note["id"], note["name"], note["others"]),
                                 ("name_shared_across_kinds", ids["X"], "G", [ids["G"]]))
                self.assertIn("matches group 'G': allowed", note["message"])
                self.assertIn("use an id or a path", note["message"])
            # A group renamed like another group still warns, and a layer's
            # name it also has is a note beside it.
            result = self.assertOk(run("update_layers", doc=handle, changes=[{"id": "K", "name": "G"}]))
            self.assertEqual([(w["code"], w["others"]) for w in result["warnings"]],
                             [("duplicate_name", [ids["G"]])])
            self.assertIn("like 1 other group", result["warnings"][0]["message"])
            self.assertEqual([(n["code"], n["others"]) for n in result["notes"]],
                             [("name_shared_across_kinds", [ids["X"]])])

    def test_validation_failures_change_nothing(self):
        cases = [
            ("pass_through on paint", {"id": "A", "pass_through": True}, "bad_request"),
            ("same node twice", {"id": "/B", "visible": False}, "bad_request"),
            ("unknown key", {"id": "A", "opactiy": 0.5}, "bad_request"),
            ("nothing to change", {"id": "A"}, "bad_request"),
            ("only nulls", {"id": "A", "name": None}, "bad_request"),
            ("id missing", {"name": "x"}, "bad_request"),
            ("root", {"id": "/", "visible": False}, "bad_request"),
            ("unknown layer", {"id": "Nope", "visible": False}, "not_found"),
            ("$key", {"id": "$a", "visible": False}, "bad_request"),
            ("opacity 0-255", {"id": "A", "opacity": 128}, "bad_request"),
            ("int bool", {"id": "A", "visible": 0}, "bad_request"),
            ("bad blend mode", {"id": "A", "blend_mode": "multiplly"}, "bad_request"),
            ("empty name", {"id": "A", "name": ""}, "bad_request"),
            ("not an object", ["A"], "bad_request"),
        ]
        with scratch_document(16, 16, name="kmcp-update-invalid") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            for label, change, code in cases:
                with self.subTest(label):
                    err = self.assertError(run("update_layers", doc=handle, changes=[
                        {"id": "B", "name": "renamed"}, change]), code)
                    self.assertEqual((err["details"]["index"], err["details"]["field"]),
                                     (1, "changes"), err)
                    self.assertTrue(err["message"].startswith("changes[1]: "), err)
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

            err = self.assertError(run("update_layers", doc=handle, changes=[
                {"id": "A", "visible": True}, {"id": ids["A"], "opacity": 1.0}]), "bad_request")
            self.assertEqual((err["details"]["param"], err["details"]["first"]), ("id", 0))
            err = self.assertError(run("update_layers", doc=handle, changes=[
                {"id": "A", "foo": 1, "bar": 2}]), "bad_request")
            self.assertEqual(err["details"]["unknown"], ["bar", "foo"])
            self.assertIn("pass_through", err["details"]["valid"])
            err = self.assertError(run("update_layers", doc=handle, changes=[
                {"id": "A", "pass_through": False}]), "bad_request")
            self.assertEqual(err["details"]["param"], "pass_through")
            for changes in ([], None, {"id": "A"}):
                self.assertError(run("update_layers", doc=handle, changes=changes), "bad_request")
            self.assertError(run("update_layers", doc=handle, changes=[{"id": "A", "visible": 1}],
                                 dry_run="no"), "bad_request")
            self.assertEqual(snapshot(doc), before)

    def test_dry_run_changes_nothing(self):
        with scratch_document(16, 16, name="kmcp-update-dry") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            result = self.assertOk(run("update_layers", doc=handle, dry_run=True, changes=[
                {"id": "G", "pass_through": True, "visible": False}, {"id": "K", "name": "C"}]))
            self.assertEqual(result["would_update"], [
                {"id": ids["G"], "path": "G", "set": {"visible": False, "pass_through": True}},
                {"id": ids["K"], "path": "K", "set": {"name": "C"}}])
            # K is a group, C a paint layer: a note, not a warning (F004 K9).
            self.assertEqual(result["warnings"], [])
            self.assertEqual([n["code"] for n in result["notes"]], ["name_shared_across_kinds"])
            self.assertIs(result["dry_run"], True)
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_mid_apply_failure_rolls_back(self):
        real = layers._set_props
        calls = []

        def failing(node, props):
            calls.append(props)
            if len(calls) == 3:  # changes[2]'s apply; its own restore is call 4
                raise RuntimeError("injected failure")
            return real(node, props)

        with scratch_document(16, 16, name="kmcp-update-rollback") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            layers._set_props = failing
            try:
                err = self.assertError(run("update_layers", doc=handle, changes=[
                    {"id": "A", "name": "A2", "locked": True}, {"id": "B", "opacity": 0.1},
                    {"id": "C", "visible": False}, {"id": "K", "name": "never"}]), "krita_error")
            finally:
                layers._set_props = real
            d = err["details"]
            self.assertEqual((d["index"], d["applied"], d["rolled_back"], d["rollback_errors"]),
                             (2, 2, 2, []))
            self.assertIn("injected failure", err["message"])
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)


# --- move_layers -----------------------------------------------------------

class MoveLayersTest(TreeBase):
    def move(self, handle, *moves, **params):
        return run("move_layers", doc=handle, moves=list(moves), **params)

    def test_within_parent(self):
        with scratch_document(16, 16, name="kmcp-move-within") as doc:
            handle, ids = self.build(doc)
            root = ["C", "G", "K", "B", "A", "Background"]
            self.assertEqual(top_down(doc), root)
            steps = [
                ({"id": "A", "above": "C"}, ["A", "C", "G", "K", "B", "Background"]),
                ({"id": "A", "below": "B"}, ["C", "G", "K", "B", "A", "Background"]),
                ({"id": "C", "index": 0}, ["G", "K", "B", "A", "Background", "C"]),
                ({"id": "C"}, root),                                  # default: top
                ({"id": "Background", "below": "A"}, root),           # no-op
                ({"id": "K", "index": 5}, ["K", "C", "G", "B", "A", "Background"]),  # top
                ({"id": "K", "index": 3}, ["C", "G", "K", "B", "A", "Background"]),
                ({"id": "Background", "index": 5, "parent": "/"},
                 ["Background", "C", "G", "K", "B", "A"]),
                ({"id": "Background", "below": "A"}, root),           # back to the bottom
            ]
            for move, want in steps:
                with self.subTest(move=move):
                    result = self.assertOk(self.move(handle, move))
                    self.assertEqual(top_down(doc), want)
                    (moved,) = result["moved"]
                    self.assertEqual(moved["id"], ids[move["id"]])
                    self.assertEqual(moved["parent"], "/")
                    self.assertEqual(moved["index"], len(want) - 1 - want.index(move["id"]))
                    self.assertEqual(moved["path"], move["id"])
            # A move that changes nothing records no journal entry.
            self.assertEqual(len(batch.JOURNALS.get(handle)), len(steps) - 1)

    def test_within_group_and_bottom_of_group(self):
        with scratch_document(16, 16, name="kmcp-move-group") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            self.assertOk(self.move(handle, {"id": "G/H", "index": 0}))
            self.assertEqual(top_down(doc, ids["G"]), ["X", "H"])
            self.assertEqual(top_down(doc, ids["H"]), ["Y"])  # the subtree came along
            self.assertOk(self.move(handle, {"id": "X", "below": "H"}))
            self.assertEqual(top_down(doc, ids["G"]), ["H", "X"])
            after = snapshot(doc)
            self.assertEqual(after, before)  # back where it started, all intact

    def test_across_parents_keeps_everything(self):
        with scratch_document(16, 16, name="kmcp-move-across") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            result = self.assertOk(self.move(
                handle,
                {"id": "Y", "parent": "/", "above": "B"},
                {"id": "A", "parent": "K"},             # empty group, with its mask
                {"id": "G", "parent": "/", "index": 0},  # a group, to the bottom
                {"id": "X", "parent": "H"}))  # H is empty now: Y left it
            self.assertEqual(top_down(doc), ["C", "K", "Y", "B", "Background", "G"])
            self.assertEqual(top_down(doc, ids["K"]), ["A"])
            self.assertEqual(top_down(doc, ids["A"]), ["M"])
            self.assertEqual(top_down(doc, ids["G"]), ["H"])
            self.assertEqual(top_down(doc, ids["H"]), ["X"])
            self.assertEqual(result["moved"], [
                {"id": ids["Y"], "path": "Y", "parent": "/", "index": 3},
                {"id": ids["A"], "path": "K/A", "parent": ids["K"], "index": 0},
                {"id": ids["G"], "path": "G", "parent": "/", "index": 0},
                {"id": ids["X"], "path": "G/H/X", "parent": ids["H"], "index": 0}])
            after = snapshot(doc)
            self.assertEqual(set(after), set(before))  # same UUIDs, nothing lost
            for nid in before:
                # Pixels, bounds and properties are unchanged; only places differ.
                b_obj, b_px = without_place(before[nid])
                a_obj, a_px = without_place(after[nid])
                b_obj.pop("children"), a_obj.pop("children")
                if a_obj["type"] == "grouplayer":  # a group's bounds follow its children
                    b_obj.pop("bounds"), a_obj.pop("bounds")
                self.assertEqual((a_obj, a_px), (b_obj, b_px), a_obj["name"])

            self.undo_last(handle, doc, "move_layers")
            self.assertEqual(snapshot(doc), before)

    def test_interacting_moves_validated_against_planned_state(self):
        with scratch_document(16, 16, name="kmcp-move-planned") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            # Valid only after the earlier moves: H leaves G before G goes
            # into H, and B is under G before C goes above it.
            result = self.assertOk(self.move(
                handle,
                {"id": "H", "parent": "/"},
                {"id": "G", "parent": "H", "index": 0},
                {"id": "B", "parent": "G"},
                {"id": "C", "parent": "G", "above": "B"},
                {"id": "B", "parent": "K"},               # the same layer again
                {"id": "B", "parent": "/", "index": 0}))
            self.assertEqual(top_down(doc), ["H", "K", "A", "Background", "B"])
            self.assertEqual(top_down(doc, ids["H"]), ["Y", "G"])
            self.assertEqual(top_down(doc, ids["G"]), ["C", "X"])
            self.assertEqual([m["path"] for m in result["moved"]],
                             ["H", "H/G", "B", "H/G/C", "B", "B"])
            self.undo_last(handle, doc, "move_layers")  # inverses run newest first
            self.assertEqual(snapshot(doc), before)

            # Invalid only because of an earlier move in the same call.
            for moves, index in (
                ([{"id": "K", "parent": "G"}, {"id": "G", "parent": "K"}], 1),
                ([{"id": "B", "parent": "K"}, {"id": "A", "above": "B"}], 1),
                ([{"id": "H", "parent": "/"}, {"id": "Y", "parent": "G", "above": "H"}], 1),
            ):
                err = self.assertError(self.move(handle, *moves), "bad_request")
                self.assertEqual(err["details"]["index"], index, err)
            self.assertEqual(snapshot(doc), before)

    def test_validation_failures_change_nothing(self):
        cases = [
            ("into itself", {"id": "G", "parent": "G"}, "bad_request"),
            ("into own subtree", {"id": "G", "parent": "G/H"}, "bad_request"),
            ("parent is paint", {"id": "C", "parent": "B"}, "bad_request"),
            ("parent is a mask", {"id": "C", "parent": "M"}, "bad_request"),
            ("above not a sibling", {"id": "C", "above": "X"}, "bad_request"),
            ("below not a sibling", {"id": "C", "parent": "G", "below": "B"}, "bad_request"),
            ("above itself", {"id": "C", "above": "C"}, "bad_request"),
            ("two positions", {"id": "C", "above": "B", "index": 0}, "bad_request"),
            ("index too big", {"id": "C", "index": 6}, "bad_request"),
            ("index negative", {"id": "C", "index": -1}, "bad_request"),
            ("index bool", {"id": "C", "index": True}, "bad_request"),
            ("index float", {"id": "C", "index": 1.0}, "bad_request"),
            ("root", {"id": "/", "index": 0}, "bad_request"),
            ("unknown layer", {"id": "Nope"}, "not_found"),
            ("unknown parent", {"id": "C", "parent": "Nope"}, "not_found"),
            ("unknown key", {"id": "C", "abve": "B"}, "bad_request"),
            ("id missing", {"parent": "/"}, "bad_request"),
            ("not an object", "C", "bad_request"),
        ]
        with scratch_document(16, 16, name="kmcp-move-invalid") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            for label, move, code in cases:
                with self.subTest(label):
                    err = self.assertError(self.move(handle, {"id": "A", "index": 0}, move), code)
                    self.assertEqual((err["details"]["index"], err["details"]["field"]),
                                     (1, "moves"), err)
                    self.assertTrue(err["message"].startswith("moves[1]: "), err)
            err = self.assertError(self.move(handle, {"id": "C", "above": "B", "below": "A"}),
                                   "bad_request")
            self.assertEqual(err["details"]["conflicting"], ["above", "below"])
            err = self.assertError(self.move(handle, {"id": "G", "parent": "H"}), "bad_request")
            self.assertEqual(err["details"]["param"], "parent")
            self.assertIn("own subtree", err["message"])
            self.assertOk(self.move(handle, {"id": "C", "index": 5}))  # len: on top, a no-op
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_dry_run_matches_the_real_move(self):
        moves = [{"id": "H", "parent": "/", "below": "B"}, {"id": "A", "parent": "G", "index": 0},
                 {"id": "Background", "above": "C"}]
        with scratch_document(16, 16, name="kmcp-move-dry") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            dry = self.assertOk(self.move(handle, *moves, dry_run=True))
            self.assertIs(dry["dry_run"], True)
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)
            real = self.assertOk(self.move(handle, *moves))
            self.assertEqual(dry["would_move"], real["moved"])
            self.assertEqual(top_down(doc), ["Background", "C", "G", "K", "B", "H"])
            self.assertEqual(top_down(doc, ids["G"]), ["X", "A"])

    def test_mid_apply_failure_rolls_back(self):
        real = layers._insert
        calls = []

        def failing(*args, **kwargs):
            calls.append(1)
            if len(calls) == 3:  # moves[2]'s insert, with the layer detached
                raise RuntimeError("injected failure")
            return real(*args, **kwargs)

        with scratch_document(16, 16, name="kmcp-move-rollback") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            layers._insert = failing
            try:
                err = self.assertError(self.move(
                    handle, {"id": "G", "parent": "K"}, {"id": "A", "index": 0},
                    {"id": "B", "parent": "H"}, {"id": "C", "index": 0}), "krita_error")
            finally:
                layers._insert = real
            d = err["details"]
            self.assertEqual((d["index"], d["applied"], d["rolled_back"], d["rollback_errors"]),
                             (2, 2, 2, []))
            self.assertIn("injected failure", err["message"])
            self.assertEqual(snapshot(doc), before)  # B was put back by its own item
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)


# --- delete_layers ---------------------------------------------------------

class DeleteLayersTest(TreeBase):
    def delete(self, handle, ids, **params):
        return run("delete_layers", doc=handle, ids=ids, **params)

    def test_delete_paint_with_mask_and_undo(self):
        with scratch_document(16, 16, name="kmcp-delete-paint") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            result = self.assertOk(self.delete(handle, ["A"]))
            self.assertEqual(result, {"doc": handle, "dry_run": False,
                                      "deleted": [ids["A"], ids["M"]], "count": 2,
                                      "warnings": []})
            self.assertEqual(top_down(doc), ["C", "G", "K", "B", "Background"])
            self.assertIsNone(nodes.node_by_id(doc, ids["M"]))
            entry = self.undo_last(handle, doc, "delete_layers")
            (inv,) = entry.inverses
            self.assertEqual(inv.node_ids, (ids["A"], ids["M"]))
            self.assertEqual(inv.nbytes, paint_bytes(before, [ids["A"]]))
            self.assertGreater(inv.nbytes, 0)
            self.assertEqual(entry.nbytes, inv.nbytes)
            self.assertEqual(snapshot(doc), before)

    def test_delete_group_subtree_and_fold(self):
        with scratch_document(16, 16, name="kmcp-delete-group") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            result = self.assertOk(self.delete(handle, ["G/H/Y", "G", ids["G"], "H", "C"]))
            self.assertEqual(result["deleted"],
                             [ids["C"], ids["G"], ids["H"], ids["Y"], ids["X"]])
            self.assertEqual(result["count"], 5)
            self.assertEqual(top_down(doc), ["K", "B", "A", "Background"])
            entry = self.undo_last(handle, doc, "delete_layers")
            self.assertEqual([inv.node_ids for inv in entry.inverses],
                             [(ids["C"],), (ids["G"], ids["H"], ids["Y"], ids["X"])])
            self.assertEqual(entry.nbytes, paint_bytes(before, [ids["X"], ids["Y"]]))
            self.assertEqual([inv.nbytes for inv in entry.inverses], [0, entry.nbytes])
            self.assertEqual(snapshot(doc), before)

    def test_delete_neighbours_and_the_bottom(self):
        with scratch_document(16, 16, name="kmcp-delete-bottom") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            for refs in (["Background", "B", "A"], ["X", "H"], ["K", "Background"],
                         ["C", "G", "K", "B", "A", "Background"]):
                with self.subTest(refs=refs):
                    self.assertOk(self.delete(handle, refs))
                    self.undo_last(handle, doc, "delete_layers")
                    self.assertEqual(snapshot(doc), before)
            self.assertOk(self.delete(handle, ["C", "G", "K", "B", "A", "Background"]))
            self.assertEqual(top_down(doc), [])

    def test_delete_active_node(self):
        with scratch_document(16, 16, name="kmcp-delete-active") as doc:
            handle, ids = self.build(doc)
            doc.setActiveNode(nodes.node_by_id(doc, ids["B"]))
            self.assertOk(self.delete(handle, ["B"]))
            self.assertOk(run("list_layers", doc=handle))
            self.assertOk(run("update_layers", doc=handle, changes=[{"id": "A", "visible": False}]))
            self.assertNotIn("B", top_down(doc))

    def test_too_big_to_undo_warns(self):
        with scratch_document(16, 16, name="kmcp-delete-cap") as doc:
            handle, ids = self.build(doc)
            nbytes = paint_bytes(snapshot(doc), [ids["A"]])
            with temp_config({"journal": {"max_bytes": 8}}):
                batch.JOURNALS.forget(handle)  # re-created with the small cap
                journal = batch.JOURNALS.get(handle)
                self.assertEqual(journal.max_bytes, 8)
                self.assertOk(run("update_layers", doc=handle, changes=[{"id": "C", "name": "C2"}]))
                self.assertEqual(len(journal), 1)
                result = self.assertOk(self.delete(handle, ["A"]))
                (w,) = result["warnings"]
                self.assertEqual((w["code"], w["nbytes"]), ("not_undoable", nbytes))
                self.assertGreater(nbytes, 8)
                self.assertEqual(len(journal), 0)  # cleared: older entries can't be undone
                self.assertEqual(result["deleted"], [ids["A"], ids["M"]])
            batch.JOURNALS.forget(handle)

    def test_validation_failures_change_nothing(self):
        with scratch_document(16, 16, name="kmcp-delete-invalid") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            for ref, code in (("/", "bad_request"), ("Nope", "not_found"), ("", "bad_request"),
                              (3, "bad_request"), ("$k", "bad_request"),
                              ("{00000000-0000-0000-0000-000000000000}", "not_found")):
                with self.subTest(ref=ref):
                    err = self.assertError(self.delete(handle, ["A", ref]), code)
                    self.assertEqual((err["details"]["index"], err["details"]["field"]),
                                     (1, "ids"), err)
            for value in ([], None, "A"):
                self.assertError(self.delete(handle, value), "bad_request")
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_dry_run_changes_nothing(self):
        with scratch_document(16, 16, name="kmcp-delete-dry") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            result = self.assertOk(self.delete(handle, ["H", "A"], dry_run=True))
            self.assertEqual(result, {"doc": handle, "dry_run": True,
                                      "would_delete": [ids["H"], ids["Y"], ids["A"], ids["M"]],
                                      "count": 4})
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)

    def test_mid_apply_failure_rolls_back(self):
        real = layers._paint_bytes
        calls = []

        def failing(node, layout):
            calls.append(1)
            if len(calls) == 2:  # ids' second deletion, before its remove()
                raise RuntimeError("injected failure")
            return real(node, layout)

        with scratch_document(16, 16, name="kmcp-delete-rollback") as doc:
            handle, ids = self.build(doc)
            before = snapshot(doc)
            layers._paint_bytes = failing
            try:
                err = self.assertError(self.delete(handle, ["G", "A", "B"]), "krita_error")
            finally:
                layers._paint_bytes = real
            d = err["details"]
            # Applied top first (G, then B): index is B's position in ids.
            self.assertEqual((d["index"], d["applied"], d["rolled_back"], d["rollback_errors"]),
                             (2, 1, 1, []))
            self.assertTrue(err["message"].startswith("delete_layers: ids[2] failed: "), err)
            self.assertEqual(snapshot(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), 0)


class BatchLimitsTest(TreeBase):
    def test_too_many_items(self):
        with scratch_document(8, 8, name="kmcp-umd-many") as doc:
            handle = registry.handle_for(doc)
            before = snapshot(doc)
            for action, field, item in (("update_layers", "changes", {"id": "Background",
                                                                      "visible": False}),
                                        ("move_layers", "moves", {"id": "Background"}),
                                        ("delete_layers", "ids", "Background")):
                with self.subTest(action):
                    err = self.assertError(run(action, doc=handle, **{field: [item] * 2001}),
                                           "too_large")
                    self.assertEqual(err["details"], {"count": 2001, "limit": 2000})
            self.assertEqual(snapshot(doc), before)
