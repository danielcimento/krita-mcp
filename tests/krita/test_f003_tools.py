"""F003 command behaviour not covered elsewhere: reload_config (K2),
forbidden_path through a command (K2), create_layers(new_document) error
and dry-run paths (K5), list_layers paging / overview / path (K3), lint
group_by (K3), paint colour per call (K7)."""

import json
import os
import shutil
import tempfile
import unittest

from helpers import temp_config
from kritamcp import nodes, pixels, registry
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run


def _open_handles():
    return {h for h, _ in registry.list_open()}


class F003Base(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="kmcp-f003-"))
        self.cfg = temp_config({"allowed_dirs": [self.tmp]})
        self.cfg_path = self.cfg.__enter__()
        self.handles = []

    def tearDown(self):
        try:
            for handle in self.handles:
                try:
                    registry.lookup(handle)
                except CommandError:
                    continue
                run("close_document", doc=handle, discard=True)
        finally:
            self.cfg.__exit__(None, None, None)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def canvas(self, w=32, h=24, background="none"):
        handle = self.assertOk(run("new_canvas", width=w, height=h, background=background,
                                   resolution=72.0))["doc"]
        self.handles.append(handle)
        return handle

    def manifest(self, layers, document=None):
        path = os.path.join(self.tmp, "m.json")
        with open(path, "w") as f:
            json.dump({"version": 1, "document": document or {}, "layers": layers}, f)
        return path


class ConfigTest(F003Base):
    def test_reload_config_reports_allowlist(self):
        r = self.assertOk(run("reload_config"))
        self.assertEqual(r["config_file"], self.cfg_path)
        self.assertIs(r["config_exists"], True)
        self.assertEqual(r["config_key"], "allowed_dirs")
        self.assertEqual(r["config_dirs"], [self.tmp])
        self.assertIn(self.tmp, r["allowed_dirs"])
        self.assertTrue(r["config_reload"].startswith("automatic"))

    def test_forbidden_path_names_the_fix(self):
        handle = self.canvas()
        outside = os.path.realpath(tempfile.mkdtemp(prefix="kmcp-f003-outside-"))
        try:
            err = self.assertError(run("render", doc=handle, path=os.path.join(outside, "x.png")),
                                   "forbidden_path")
            lines = err["message"].split("\n")
            self.assertTrue(lines[0].endswith("is outside the allowed directories"), lines)
            self.assertTrue(lines[1].startswith("  allowed: "), lines)
            self.assertIn(f'"{outside}" in "allowed_dirs" in {self.cfg_path}', lines[2])
            self.assertEqual(err["details"]["suggested_dir"], outside)
        finally:
            shutil.rmtree(outside, ignore_errors=True)


class NewDocumentTest(F003Base):
    LAYERS = [{"key": "g", "name": "G", "type": "group"},
              {"key": "a", "name": "A", "parent": "$g"}]

    def test_needs_a_manifest_size_and_no_doc(self):
        self.assertError(run("create_layers", specs=[{"name": "A"}], new_document=True),
                         "bad_request")
        handle = self.canvas()
        path = self.manifest(self.LAYERS, {"width": 10, "height": 8})
        self.assertError(run("create_layers", doc=handle, manifest=path, new_document=True),
                         "bad_request")
        self.assertError(run("create_layers", doc=handle, manifest=path, name="X"),
                         "bad_request")
        before = _open_handles()
        no_size = self.manifest(self.LAYERS)
        err = self.assertError(run("create_layers", manifest=no_size, new_document=True),
                               "bad_request")
        self.assertIn("no document width and height", err["message"])
        cmyk = self.manifest(self.LAYERS, {"width": 10, "height": 8, "color_model": "CMYKA"})
        self.assertError(run("create_layers", manifest=cmyk, new_document=True), "bad_request")
        self.assertEqual(_open_handles(), before)

    def test_dry_run_and_failure_leave_no_document(self):
        path = self.manifest(self.LAYERS, {"width": 10, "height": 8, "resolution": 300})
        before = _open_handles()
        dry = self.assertOk(run("create_layers", manifest=path, new_document=True, dry_run=True))
        self.assertIsNone(dry["doc"])
        self.assertEqual((dry["new_document"]["width"], dry["new_document"]["resolution"]),
                         (10, 300))
        self.assertEqual(len(dry["would_create"]), 2)
        bad = self.manifest(self.LAYERS + [{"key": "p", "name": "P", "png": "missing.png"}],
                            {"width": 10, "height": 8})
        self.assertError(run("create_layers", manifest=bad, new_document=True), "not_found")
        self.assertEqual(_open_handles(), before)

    def test_builds_with_the_manifest_size_and_name(self):
        path = self.manifest(self.LAYERS, {"width": 10, "height": 8, "resolution": 300,
                                           "name": "orig"})
        made = self.assertOk(run("create_layers", manifest=path, new_document=True,
                                 name="Rebuild", verbose="auto"))
        self.handles.append(made["doc"])
        self.assertEqual(made["new_document"]["name"], "Rebuild")
        doc = registry.lookup(made["doc"])
        self.assertEqual((doc.width(), doc.height(), doc.resolution()), (10, 8, 300))
        self.assertEqual([n.name() for n in doc.rootNode().childNodes()], ["G"])
        self.assertEqual(len(made["created"]), 2)
        del doc


class BulkResultsTest(F003Base):
    def _many(self, handle, n=45):
        specs = [{"key": "g", "name": "Group", "type": "group"}]
        specs += [{"name": f"Layer {i}", "parent": "$g", "blend_mode": "multiply" if i < 3
                   else "normal", "visible": i != 5} for i in range(n)]
        return self.assertOk(run("create_layers", doc=handle, specs=specs, verbose="auto"))

    def test_create_layers_auto_lists_small_batches_only(self):
        handle = self.canvas()
        made = self._many(handle)
        self.assertNotIn("created", made)
        self.assertIs(made["created_listed"], False)
        self.assertEqual(made["counts"]["total"], 46)
        dry = self.assertOk(run("create_layers", doc=handle, specs=[{"name": "x"}] * 30,
                                dry_run=True, verbose="auto"))
        self.assertEqual((len(dry["would_create"]), dry["would_create_listed"]), (20, 20))
        self.assertError(run("create_layers", doc=handle, specs=[{"name": "x"}], verbose="yes"),
                         "bad_request")

    def test_list_layers_pages_and_overview(self):
        handle = self.canvas()
        self._many(handle)
        first = self.assertOk(run("list_layers", doc=handle, limit=30, overview=True,
                                  fields=["type"]))
        self.assertEqual((len(first["layers"]), first["next_offset"]), (30, 30))
        self.assertEqual(first["counts"]["total"], 46)
        o = first["overview"]
        self.assertEqual(o["blend_modes"]["multiply"]["count"], 3)
        self.assertEqual(o["hidden"], {"count": 1, "paths": ["Group/Layer 5"]})
        self.assertEqual(o["max_depth"], 2)
        rest = self.assertOk(run("list_layers", doc=handle, limit=30, offset=30))
        self.assertEqual((len(rest["layers"]), rest["next_offset"]), (16, None))
        only = self.assertOk(run("list_layers", doc=handle, summary_only=True))
        self.assertNotIn("layers", only)
        out = os.path.join(self.tmp, "tree.json")
        written = self.assertOk(run("list_layers", doc=handle, path=out))
        with open(written["path"]) as f:
            self.assertEqual(len(json.load(f)["layers"]), 46)
        self.assertError(run("list_layers", doc=handle, path=out, limit=3), "bad_request")
        self.assertError(run("list_layers", doc=handle, path=os.path.join(self.tmp, "t.txt")),
                         "forbidden_path")

    def test_lint_group_by_rule(self):
        handle = self.canvas()
        self.assertOk(run("create_layers", doc=handle, specs=[{"name": "Same"}] * 4
                          + [{"name": "Empty"}]))
        flat = self.assertOk(run("lint", doc=handle))
        grouped = self.assertOk(run("lint", doc=handle, group_by="rule", limit=1))
        self.assertNotIn("findings", grouped)
        self.assertEqual(grouped["total"], len(flat["findings"]))
        empty = grouped["by_rule"]["empty-layer"]
        self.assertEqual((empty["count"], len(empty["items"]), empty["next_offset"]), (5, 1, 1))
        page = self.assertOk(run("lint", doc=handle, limit=2, offset=1))
        self.assertEqual(page["findings"], flat["findings"][1:3])
        self.assertError(run("lint", doc=handle, group_by="layer"), "bad_request")


class PaintColourTest(F003Base):
    def test_color_for_one_call(self):
        handle = self.canvas(background="transparent")
        self.assertOk(run("set_color", color="#000000"))
        layer = self.assertOk(run("list_layers", doc=handle))["layers"][0]["id"]
        r = self.assertOk(run("draw_shape", doc=handle, layer=layer, shape="rectangle", x=2, y=2,
                              width=4, height=4, color="#3fa34d"))
        self.assertEqual(r["color"], "#3fa34d")
        node = nodes.node_by_id(registry.lookup(handle), layer)
        self.assertEqual(tuple(pixels.as_bytes(node.pixelData(3, 3, 1, 1))), (0x4d, 0xa3, 0x3f, 255))
        del node
        r = self.assertOk(run("fill", doc=handle, layer=layer, x=20, y=12, radius=2))
        self.assertEqual(r["color"], "#000000")  # the stored colour is unchanged
        self.assertError(run("stroke", doc=handle, layer=layer, points=[[1, 1], [5, 5]],
                             color="nope"), "bad_request")
