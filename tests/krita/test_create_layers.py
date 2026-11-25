"""commands/layers.py: create_layers, headless, through dispatch.execute.

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. PNGs live in a temp dir made an
allowed root through a temp config file, never the user's real dirs.
"""

import contextlib
import os
import shutil
import tempfile
import unittest

from PyQt5.QtGui import QImage

from helpers import add_paint_layer, scratch_document, temp_config
from kritamcp import batch, nodes, registry
from kritamcp.commands import layers
from test_dispatch import EnvelopeAssertions, run


def write_png(path, w, h, seed=0):
    """Write a w x h PNG with varied colors and alpha (0, partial and 255,
    colors under zero alpha too); return its BGRA bytes."""
    data = bytes((i * 37 + seed * 11 + (i // 4) * 5) % 256 for i in range(4 * w * h))
    image = QImage(data, w, h, 4 * w, QImage.Format_ARGB32).copy()
    if not image.save(path, "PNG"):
        raise RuntimeError(f"could not write {path}")
    return data


def node_ids(doc):
    return set(nodes.build_index(doc))


class CreateLayersBase(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-create-")
        self.outside = tempfile.mkdtemp(prefix="kmcp-outside-")
        self._stack.callback(shutil.rmtree, self.root, ignore_errors=True)
        self._stack.callback(shutil.rmtree, self.outside, ignore_errors=True)
        self._stack.enter_context(temp_config({"allowed_dirs": [self.root]}))

    def tearDown(self):
        self._stack.close()

    def png(self, name, w, h, seed=0, where=None):
        path = os.path.join(where or self.root, name)
        return path, write_png(path, w, h, seed)

    def create(self, handle, specs, **params):
        return run("create_layers", doc=handle, specs=specs, **params)

    def listing(self, handle):
        result = self.assertOk(run("list_layers", doc=handle))
        return {layer["id"]: layer for layer in result["layers"]}, result["layers"]


class CreateLayersTest(CreateLayersBase):
    def test_nested_groups_and_stack_order(self):
        with scratch_document(16, 16, name="kmcp-create-nested") as doc:
            handle = registry.handle_for(doc)
            specs = [
                {"key": "body", "name": "Body", "type": "group"},
                {"key": "head", "name": "Head", "type": "group", "parent": "$body"},
                {"key": "eyes", "name": "Eyes", "type": "group", "parent": "$head"},
                {"key": "eyeL", "name": "Eye L", "parent": "$eyes"},  # bottom ...
                {"name": "Eye R", "parent": "$eyes"},                 # ... to top
                {"name": "Face", "parent": "$head"},
                {"name": "Top"},
            ]
            result = self.assertOk(self.create(handle, specs))
            self.assertEqual(set(result), {"doc", "dry_run", "created", "counts"})
            self.assertEqual((result["doc"], result["dry_run"]), (handle, False))
            self.assertEqual(result["counts"], {"grouplayer": 3, "paintlayer": 4, "total": 7})
            created = result["created"]
            self.assertEqual([c["name"] for c in created], [s["name"] for s in specs])
            self.assertEqual([c["key"] for c in created],
                             ["body", "head", "eyes", "eyeL", None, None, None])
            self.assertEqual([c["path"] for c in created],
                             ["Body", "Body/Head", "Body/Head/Eyes", "Body/Head/Eyes/Eye L",
                              "Body/Head/Eyes/Eye R", "Body/Head/Face", "Top"])
            self.assertEqual([c["type"] for c in created], ["grouplayer"] * 3 + ["paintlayer"] * 4)
            ids = {c["name"]: c["id"] for c in created}

            by_id, flat = self.listing(handle)
            self.assertEqual([layer["name"] for layer in flat],
                             ["Top", "Body", "Head", "Face", "Eyes", "Eye R", "Eye L",
                              "Background"])
            self.assertEqual(by_id[ids["Eyes"]]["children"], [ids["Eye R"], ids["Eye L"]])
            self.assertEqual(by_id[ids["Head"]]["children"], [ids["Face"], ids["Eyes"]])
            self.assertEqual(by_id[ids["Eye L"]]["parent"], ids["Eyes"])
            for name in ("Body", "Head", "Eyes"):
                self.assertEqual(by_id[ids[name]]["type"], "grouplayer")
            # Defaults.
            eye = by_id[ids["Eye L"]]
            self.assertEqual((eye["visible"], eye["opacity"], eye["blend_mode"], eye["locked"],
                              eye["alpha_locked"], eye["inherit_alpha"], eye["pass_through"],
                              eye["bounds"]),
                             (True, 1.0, "normal", False, False, False, None, None))
            self.assertIs(by_id[ids["Body"]]["pass_through"], False)

    def test_above_existing_and_planned(self):
        with scratch_document(16, 16, name="kmcp-create-above") as doc:
            handle = registry.handle_for(doc)
            upper = nodes.node_id(add_paint_layer(doc, "Upper"))
            specs = [
                {"name": "Over BG", "above": "Background"},
                {"key": "g", "name": "G", "type": "group"},
                {"key": "a", "name": "A", "parent": "$g"},
                {"name": "B", "parent": "$g"},
                {"name": "Between", "parent": "$g", "above": "$a"},
                {"name": "Above Upper", "parent": "/", "above": upper},
            ]
            result = self.assertOk(self.create(handle, specs))
            ids = {c["name"]: c["id"] for c in result["created"]}
            _, flat = self.listing(handle)
            self.assertEqual([layer["name"] for layer in flat],
                             ["G", "B", "Between", "A", "Above Upper", "Upper", "Over BG",
                              "Background"])
            self.assertEqual(ids["Above Upper"], flat[4]["id"])

            # above must be a child of the parent (existing or planned).
            before = node_ids(doc)
            for specs, index in (
                ([{"name": "x", "above": "Upper", "parent": "G"}], 0),
                ([{"key": "h", "name": "H", "type": "group"},
                  {"name": "y", "above": "$h", "parent": "$h"}], 1),
                ([{"key": "h", "name": "H", "type": "group"},
                  {"name": "y", "parent": "$h", "above": "Upper"}], 1),
                ([{"name": "x", "above": "/"}], 0),
            ):
                err = self.assertError(self.create(handle, specs), "bad_request")
                self.assertEqual(err["details"]["index"], index, err)
            self.assertEqual(node_ids(doc), before)

    def test_every_property(self):
        with scratch_document(16, 16, name="kmcp-create-props") as doc:
            handle = registry.handle_for(doc)
            png, data = self.png("locked.png", 4, 3, seed=1)
            specs = [
                {"key": "g", "name": "G", "type": "group", "pass_through": True,
                 "opacity": 0.25, "visible": False},
                {"name": "Half", "parent": "$g", "opacity": 0.5, "blend_mode": "multiply"},
                {"name": "Add", "parent": "$g", "blend_mode": "add", "inherit_alpha": True},
                {"name": "Dodge", "parent": "$g", "blend_mode": "linear_dodge",
                 "alpha_locked": True},
                {"name": "Locked", "png": png, "x": 2, "y": 5, "locked": True,
                 "alpha_locked": True},
                {"name": "Plain group", "type": "group", "pass_through": False},
            ]
            result = self.assertOk(self.create(handle, specs))
            ids = {c["name"]: c["id"] for c in result["created"]}
            by_id, _ = self.listing(handle)

            g = by_id[ids["G"]]
            self.assertEqual((g["pass_through"], g["opacity"], g["visible"]), (True, 0.251, False))
            self.assertEqual(nodes.node_by_id(doc, ids["G"]).opacity(), 64)
            half = by_id[ids["Half"]]
            self.assertEqual((half["opacity"], half["blend_mode"]), (0.502, "multiply"))
            self.assertEqual(nodes.node_by_id(doc, ids["Half"]).opacity(), 128)
            add = by_id[ids["Add"]]
            self.assertEqual((add["blend_mode"], add["inherit_alpha"], add["alpha_locked"]),
                             ("add", True, False))
            dodge = by_id[ids["Dodge"]]
            self.assertEqual((dodge["blend_mode"], dodge["alpha_locked"], dodge["inherit_alpha"]),
                             ("linear_dodge", True, False))
            locked = by_id[ids["Locked"]]
            self.assertEqual((locked["locked"], locked["alpha_locked"]), (True, True))
            self.assertIs(by_id[ids["Plain group"]]["pass_through"], False)
            # Locked last: the PNG's pixels still landed.
            self.assertEqual(locked["bounds"], {"x": 2, "y": 5, "w": 4, "h": 3})
            node = nodes.node_by_id(doc, ids["Locked"])
            self.assertEqual(bytes(node.pixelData(2, 5, 4, 3)), data)

    def test_png_bit_exact_and_unclipped(self):
        with scratch_document(16, 16, name="kmcp-create-png") as doc:
            handle = registry.handle_for(doc)
            inside, inside_data = self.png("inside.png", 5, 4, seed=2)
            over, over_data = self.png("over.png", 6, 5, seed=3)
            result = self.assertOk(self.create(handle, [
                {"name": "Inside", "png": inside, "x": 3, "y": 2},
                {"name": "Over", "png": over, "x": -2, "y": 13},
                {"name": "Origin", "png": inside},  # no x/y: 0, 0
            ]))
            ids = {c["name"]: c["id"] for c in result["created"]}
            for name, data, rect in (("Inside", inside_data, (3, 2, 5, 4)),
                                     ("Over", over_data, (-2, 13, 6, 5)),
                                     ("Origin", inside_data, (0, 0, 5, 4))):
                node = nodes.node_by_id(doc, ids[name])
                self.assertEqual(bytes(node.pixelData(*rect)), data, name)
            by_id, _ = self.listing(handle)
            # Bounds cover the pixels with alpha > 0, past the canvas edge too.
            bounds = by_id[ids["Over"]]["bounds"]
            self.assertEqual(bounds, _alpha_bounds(over_data, 6, 5, -2, 13))
            self.assertLess(bounds["x"], 0)
            self.assertGreater(bounds["y"] + bounds["h"], 16)

    def test_dry_run_changes_nothing(self):
        with scratch_document(16, 16, name="kmcp-create-dry") as doc:
            handle = registry.handle_for(doc)
            png, _ = self.png("dry.png", 3, 2)
            before = node_ids(doc)
            journal_len = len(batch.JOURNALS.get(handle))
            result = self.assertOk(self.create(handle, [
                {"key": "g", "name": "G", "type": "group"},
                {"key": "a", "name": "A", "parent": "$g", "png": png, "x": 1, "y": -1},
                {"name": "B", "parent": "$g", "above": "$a"},
                {"name": "C", "above": "Background"},
            ], dry_run=True))
            bg = nodes.build_index(doc).by_name("Background")[0]
            self.assertEqual(result, {
                "doc": handle, "dry_run": True,
                "would_create": [
                    {"key": "g", "name": "G", "type": "grouplayer", "parent": "/", "above": None},
                    {"key": "a", "name": "A", "type": "paintlayer", "parent": "$g", "above": None,
                     "png": os.path.realpath(png), "x": 1, "y": -1, "w": 3, "h": 2},
                    {"key": None, "name": "B", "type": "paintlayer", "parent": "$g",
                     "above": "$a"},
                    {"key": None, "name": "C", "type": "paintlayer", "parent": "/", "above": bg},
                ],
                "counts": {"grouplayer": 1, "paintlayer": 3, "total": 4}})
            self.assertEqual(node_ids(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), journal_len)
            self.assertError(run("create_layers", doc=handle, specs=[{"name": "x"}],
                                 dry_run="yes"), "bad_request")

    def test_validation_failures_change_nothing(self):
        bad_png = os.path.join(self.root, "garbage.png")
        with open(bad_png, "wb") as f:
            f.write(b"not a png at all, just some bytes padding it out to 40+")
        outside, _ = self.png("outside.png", 2, 2, where=self.outside)
        good, _ = self.png("good.png", 2, 2)
        cases = [
            # (label, bad spec, code)
            ("blend mode", {"name": "x", "blend_mode": "not-a-mode"}, "bad_request"),
            ("pass_through on paint", {"name": "x", "pass_through": True}, "bad_request"),
            ("parent is paint", {"name": "x", "parent": "Background"}, "bad_request"),
            ("parent is planned paint", {"name": "x", "parent": "$p0"}, "bad_request"),
            ("$key before defined", {"name": "x", "parent": "$later"}, "not_found"),
            ("$key self", {"key": "me", "name": "x", "type": "group", "parent": "$me"},
             "not_found"),
            ("duplicate key", {"key": "p0", "name": "x"}, "bad_request"),
            ("unknown key", {"name": "x", "opactiy": 0.5}, "bad_request"),
            ("name missing", {"type": "group"}, "bad_request"),
            ("bad type", {"name": "x", "type": "paintlayer"}, "bad_request"),
            ("bad key", {"key": "a/b", "name": "x"}, "bad_request"),
            ("opacity 0-255", {"name": "x", "opacity": 128}, "bad_request"),
            ("int bool", {"name": "x", "visible": 1}, "bad_request"),
            ("png outside roots", {"name": "x", "png": outside}, "forbidden_path"),
            ("png missing", {"name": "x", "png": os.path.join(self.root, "nope.png")},
             "not_found"),
            ("not a png", {"name": "x", "png": bad_png}, "bad_request"),
            ("png on group", {"name": "x", "type": "group", "png": good}, "bad_request"),
            ("x without png", {"name": "x", "x": 3}, "bad_request"),
            ("y not int", {"name": "x", "png": good, "y": 1.5}, "bad_request"),
            ("x too far", {"name": "x", "png": good, "x": 10**9}, "bad_request"),
            ("unknown parent", {"name": "x", "parent": "No Such Group"}, "not_found"),
            ("not an object", "just a string", "bad_request"),
        ]
        with scratch_document(16, 16, name="kmcp-create-invalid") as doc:
            handle = registry.handle_for(doc)
            before = node_ids(doc)
            journal_len = len(batch.JOURNALS.get(handle))
            for label, spec, code in cases:
                with self.subTest(label):
                    err = self.assertError(
                        self.create(handle, [{"key": "p0", "name": "ok"}, spec]), code)
                    self.assertEqual(err["details"]["index"], 1, err)
                    self.assertEqual(err["details"]["field"], "specs", err)
                    self.assertTrue(err["message"].startswith("specs[1]: "), err)
                    self.assertEqual(node_ids(doc), before)
            self.assertEqual(len(batch.JOURNALS.get(handle)), journal_len)
            err = self.assertError(self.create(handle, [{"name": "x", "foo": 1, "bar": 2}]),
                                   "bad_request")
            self.assertEqual(err["details"]["unknown"], ["bar", "foo"])
            self.assertIn("name", err["details"]["valid"])
            for specs in ([], None, {"name": "x"}):
                self.assertError(run("create_layers", doc=handle, specs=specs), "bad_request")

    def test_too_many_specs(self):
        with scratch_document(8, 8, name="kmcp-create-many") as doc:
            handle = registry.handle_for(doc)
            err = self.assertError(self.create(handle, [{"name": f"l{i}"} for i in range(2001)]),
                                   "too_large")
            self.assertEqual(err["details"], {"count": 2001, "limit": 2000})
            self.assertEqual(len(node_ids(doc)), 1)

    def test_png_too_large(self):
        png, _ = self.png("wide.png", 70, 2)
        with scratch_document(8, 8, name="kmcp-create-wide") as doc:
            handle = registry.handle_for(doc)
            with temp_config({"allowed_dirs": [self.root],
                              "limits": {"max_canvas_dim": 64, "max_canvas_pixels": 4096}}):
                err = self.assertError(self.create(handle, [{"name": "w", "png": png}]),
                                       "too_large")
            self.assertEqual(err["details"]["limit"], "max_canvas_dim")
            self.assertEqual(err["details"]["index"], 0)

    def test_non_rgba8_document_refuses_png(self):
        png, _ = self.png("u16.png", 2, 2)
        with scratch_document(8, 8, name="kmcp-create-u16", color_depth="U16") as doc:
            handle = registry.handle_for(doc)
            before = node_ids(doc)
            err = self.assertError(self.create(handle, [{"name": "a"},
                                                        {"name": "b", "png": png}]), "conflict")
            self.assertEqual(err["details"]["index"], 1)
            self.assertEqual((err["details"]["color_model"], err["details"]["color_depth"]),
                             ("RGBA", "U16"))
            self.assertIn("RGBA/U16", err["message"])
            self.assertEqual(node_ids(doc), before)
            # Layers without pixels are fine in any color space.
            self.assertOk(self.create(handle, [{"name": "a"}, {"name": "g", "type": "group"}]))

    def test_mid_apply_failure_rolls_back(self):
        png, _ = self.png("roll.png", 3, 3)
        real_apply = layers.layerprops.apply

        def failing_apply(node, props, **kwargs):
            if node.name() == "boom":
                raise RuntimeError("injected failure")
            return real_apply(node, props, **kwargs)

        with scratch_document(16, 16, name="kmcp-create-rollback") as doc:
            handle = registry.handle_for(doc)
            before = node_ids(doc)
            journal_len = len(batch.JOURNALS.get(handle))
            layers.layerprops.apply = failing_apply
            try:
                err = self.assertError(self.create(handle, [
                    {"key": "g", "name": "G", "type": "group", "locked": True},
                    {"name": "Pix", "parent": "$g", "png": png, "locked": True},
                    {"name": "boom", "parent": "$g", "opacity": 0.5},
                    {"name": "never"},
                ]), "krita_error")
            finally:
                layers.layerprops.apply = real_apply
            details = err["details"]
            self.assertEqual((details["index"], details["applied"], details["rolled_back"],
                              details["rollback_errors"]), (2, 2, 2, []))
            self.assertIn("injected failure", err["message"])
            # Items 0-1 rolled back, item 2 removed its own half-made node.
            self.assertEqual(node_ids(doc), before)
            self.assertEqual(nodes.build_index(doc).by_name("boom"), [])
            self.assertEqual(len(batch.JOURNALS.get(handle)), journal_len)

    def test_truncated_png_fails_while_applying(self):
        # The header check passes; decoding fails, before item 1's node is
        # made, and item 0 is rolled back.
        png, _ = self.png("cut.png", 8, 8)
        with open(png, "r+b") as f:
            f.truncate(40)
        with scratch_document(16, 16, name="kmcp-create-truncated") as doc:
            handle = registry.handle_for(doc)
            before = node_ids(doc)
            err = self.assertError(self.create(handle, [{"name": "A"},
                                                        {"name": "B", "png": png}]), "bad_request")
            self.assertEqual((err["details"]["index"], err["details"]["applied"],
                              err["details"]["rolled_back"]), (1, 1, 1))
            self.assertIn("Cannot decode PNG", err["message"])
            self.assertEqual(node_ids(doc), before)

    def test_journal_entry_per_call(self):
        with scratch_document(16, 16, name="kmcp-create-journal") as doc:
            handle = registry.handle_for(doc)
            journal = batch.JOURNALS.get(handle)
            self.assertEqual(len(journal), 0)
            before = node_ids(doc)
            first = self.assertOk(self.create(handle, [
                {"key": "g", "name": "G", "type": "group"},
                {"name": "A", "parent": "$g"}]))
            self.assertEqual(len(journal), 1)
            self.assertOk(self.create(handle, [{"name": "B"}]))
            self.assertEqual(len(journal), 2)
            self.assertError(self.create(handle, [{"name": "C", "blend_mode": "nope"}]),
                             "bad_request")
            self.assertEqual(len(journal), 2)

            # Undo-style: the newest entry's inverses, given the live doc.
            entry = journal.pop()
            self.assertEqual(entry.label, "create_layers")
            self.assertEqual(batch.rollback(list(entry.inverses), doc), (1, []))
            entry = journal.pop()
            self.assertEqual([inv.node_ids[0] for inv in entry.inverses],
                             [c["id"] for c in first["created"]])
            self.assertEqual(batch.rollback(list(entry.inverses), doc), (2, []))
            self.assertEqual(node_ids(doc), before)

    def test_registry_prune_forgets_journal(self):
        with scratch_document(8, 8, name="kmcp-create-prune") as doc:
            handle = registry.handle_for(doc)
            self.assertOk(self.create(handle, [{"name": "A"}]))
            self.assertIn(handle, batch.JOURNALS)
        registry.list_open()  # prunes the closed document's handle
        self.assertNotIn(handle, batch.JOURNALS)
        self.assertError(self.create(handle, [{"name": "A"}]), "not_found")


def _alpha_bounds(data, w, h, x0, y0):
    """The bounding box of pixels with nonzero alpha, offset by (x0, y0)."""
    alpha = data[3::4]
    xs = [i % w for i, a in enumerate(alpha) if a]
    ys = [i // w for i, a in enumerate(alpha) if a]
    return {"x": x0 + min(xs), "y": y0 + min(ys), "w": max(xs) - min(xs) + 1,
            "h": max(ys) - min(ys) + 1}
