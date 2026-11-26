"""F003 K3 acceptance: every krita-mcp call of the 2026-11-25 smoke test,
made with the MCP tools' default parameters, returns at most 8 KB on the
Mao ``.kra``. Also K6 (exactly 11 duplicate-name errors, all A/B pose
pairs, fixed by one update_layers call from suggested_names), K7
(stray-alpha findings carry pixels and bounds) and the 3.2 rebuild in one
call. F004 (smoke test revision 2): K8 (exactly one reference-unbracketed
warning, for Expression References, before and after the B poses are
hidden) and K9 (the 22 suggested renames give no duplicate_name warning,
only notes for the names shared with a group). The config has the Live2D
conventions on, as the smoke test's does. Calls go through ``dispatch.execute`` with the params ``server.py``
sends by default (tests/unit/test_server_tools.py pins those); the size
is the JSON of the result, which the server returns plus a one-line
``summary``.

    KRITA_MCP_MAO_KRA=~/krita-mcp-output/mao_pro_layered.kra make test-krita KRITA_TESTS=smoke_sizes

Sizes are printed as ``KMCP_SIZE: <bytes> <call>`` lines in run.log.
"""

import json
import os
import shutil
import tempfile
import unittest

from helpers import temp_config
from kritamcp import registry
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run

MAO_KRA_ENV = "KRITA_MCP_MAO_KRA"
LIMIT = 8 * 1024
SUMMARY_ALLOWANCE = 300  # the server's one-line summary on top of the payload

# What server.py sends when the caller leaves the arguments at their defaults.
LIST_DEFAULTS = {"overview": True, "limit": 30, "offset": 0,
                 "fields": ["id", "name", "type", "visible", "blend_mode", "children"]}
LINT_DEFAULTS = {"group_by": "rule", "limit": 5}


@unittest.skipUnless(os.environ.get(MAO_KRA_ENV), f"set {MAO_KRA_ENV} to the layered Mao .kra")
class SmokeSizesTest(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self.sizes = []

    def call(self, label, action, **params):
        envelope = run(action, **params)
        size = len(json.dumps(envelope.get("result", envelope.get("error"))))
        self.sizes.append((size, label))
        print(f"KMCP_SIZE: {size} {label}", flush=True)
        return envelope

    def ok(self, label, action, **params):
        return self.assertOk(self.call(label, action, **params))

    def test_smoke_sequence(self):
        src = os.path.realpath(os.path.expanduser(os.environ[MAO_KRA_ENV]))
        tmp = tempfile.mkdtemp(prefix="kmcp-smoke-")
        handles = []
        try:
            with temp_config({"allowed_dirs": [tmp], "live2d": {"enabled": True}}):
                work = os.path.join(tmp, "work.kra")
                shutil.copyfile(src, work)
                self._sequence(tmp, work, handles)
        finally:
            for handle in handles:
                try:
                    registry.lookup(handle)
                except CommandError:
                    continue
                run("close_document", doc=handle, discard=True)
            shutil.rmtree(tmp, ignore_errors=True)
        too_big = [(s, label) for s, label in self.sizes if s + SUMMARY_ALLOWANCE > LIMIT]
        self.assertEqual(too_big, [], "results over 8 KB")

    def assertReferenceUnbracketed(self, result):
        """K8: exactly one reference-unbracketed warning, Expression References."""
        group = result["by_rule"]["reference-unbracketed"]
        self.assertEqual((group["count"], group["severity"]), (1, "warning"))
        (f,) = group["items"]
        self.assertEqual(f["path"], "Expression References")
        self.assertEqual(f["suggested_names"], [{"id": f["layer_id"], "name": "[ Expression References ]"}])

    def _ids(self, handle):
        """path -> id, from one full listing (not a measured call)."""
        listed = self.assertOk(run("list_layers", doc=handle, fields=["path", "type"]))["layers"]
        return {l["path"]: l["id"] for l in listed}, listed

    def _sequence(self, tmp, work, handles):
        self.ok("health-equivalent reload_config", "reload_config")
        doc = self.ok("open_file", "open_file", path=work)["doc"]
        handles.append(doc)

        # Stage 2: tree, lint, batch rename, save.
        listed = self.ok("list_layers", "list_layers", doc=doc, **LIST_DEFAULTS)
        self.assertEqual(listed["counts"]["total"], 222)
        self.assertEqual(listed["next_offset"], 30)
        self.assertEqual(listed["overview"]["blend_modes"]["multiply"]["count"], 7)
        self.ok("list_layers summary_only", "list_layers", doc=doc, summary_only=True)
        tree = self.ok("list_layers path", "list_layers", doc=doc, overview=True,
                       path=os.path.join(tmp, "tree.json"))
        with open(tree["path"]) as f:
            self.assertEqual(len(json.load(f)["layers"]), 222)

        lint_all = self.ok("lint all", "lint", doc=doc, rules=["all"], **LINT_DEFAULTS)
        self.assertEqual(lint_all["rules_off"], {})
        dup = lint_all["by_rule"]["duplicate-name"]
        self.assertEqual(dup["count"], 11)  # K6: A/B pose pairs only
        self.assertEqual(dup["severity"], "error")
        self.assertReferenceUnbracketed(lint_all)
        stray = lint_all["by_rule"]["stray-alpha"]
        self.assertGreater(stray["count"], 0)
        for f in stray["items"]:
            self.assertGreater(f["pixels"], 0)
            self.assertEqual(set(f["bounds"]), {"x", "y", "w", "h"})
        full = self.assertOk(run("lint", doc=doc, rules=["duplicate-name"]))
        self.assertEqual(len(full["findings"]), 11)
        changes = []
        for f in full["findings"]:
            self.assertEqual(len(f["related_ids"]), 2, f["message"])
            self.assertEqual(sorted(s["name"][-1] for s in f["suggested_names"]), ["A", "B"],
                             f["message"])
            changes += [{"id": s["id"], "name": s["name"]} for s in f["suggested_names"]]
        renamed = self.ok("update_layers 22 renames", "update_layers", doc=doc, changes=changes)
        # K9: Wand A / Wand B now match their parent groups: allowed, so notes only.
        self.assertEqual(renamed["warnings"], [])
        shared = sorted(n["name"] for n in renamed.get("notes", []))
        self.assertIn("Wand A", shared)
        self.assertIn("Wand B", shared)
        self.assertTrue(all(n["code"] == "name_shared_across_kinds" for n in renamed["notes"]))
        after = self.ok("lint duplicate-name", "lint", doc=doc, rules=["duplicate-name"],
                        **LINT_DEFAULTS)
        self.assertEqual(after["total"], 0)
        self.ok("save", "save", doc=doc)

        # Stage 5.1 (revision 2): hide the B poses. They are a pose group
        # set, so K8 still flags Expression References only.
        ids, _ = self._ids(doc)
        hide = [{"id": ids[p], "visible": False} for p in ("Arm L B", "Arm R B")]
        self.assertOk(run("update_layers", doc=doc, changes=hide))
        hidden = self.ok("lint pose-visibility, reference rules", "lint", doc=doc,
                         rules=["pose-visibility", "reference-hidden", "reference-unbracketed"],
                         **LINT_DEFAULTS)
        self.assertEqual(sorted(hidden["by_rule"]), ["reference-unbracketed"])
        self.assertReferenceUnbracketed(hidden)
        self.assertOk(run("update_layers", doc=doc,
                          changes=[{"id": c["id"], "visible": True} for c in hide]))

        # Stage 3: export, rebuild in one call, diff, render, a refused path.
        exported = self.ok("export_layers", "export_layers", doc=doc,
                           dir=os.path.join(tmp, "export"), png_level=1, verbose=False)
        self.assertEqual(exported["counts"]["png"], 184)
        rebuilt = self.ok("create_layers manifest new_document", "create_layers",
                          manifest=exported["manifest"], new_document=True, verbose="auto")
        handles.append(rebuilt["doc"])
        diff = self.ok("diff_documents", "diff_documents", doc_a=doc, doc_b=rebuilt["doc"])
        self.assertEqual(diff["document_changes"], {})
        self.assertIs(diff["projection_changed"], False)
        self.ok("render work", "render", doc=doc, path=os.path.join(tmp, "work.png"))
        self.ok("render rebuild", "render", doc=rebuilt["doc"], path=os.path.join(tmp, "b.png"))
        bad = self.call("render bad path", "render", doc=doc, path="/tmp/kmcp-elsewhere/x.png")
        self.assertError(bad, "forbidden_path")
        self.assertIn("allowed_dirs", bad["error"]["message"])
        self.ok("close rebuild", "close_document", doc=rebuilt["doc"], discard=True)

        # Stage 4: occlusion, mirror, split, undo.
        ids, layers = self._ids(doc)
        face = next(p for p, l in ((l["path"], l) for l in layers)
                    if l["type"] == "paintlayer" and p.split("/")[-1] == "Face")
        self.ok("occlusion_map Face", "occlusion_map", doc=doc, id=ids[face],
                path=os.path.join(tmp, "occ.png"))
        ear = next(p for p in ids if p.split("/")[-1] == "Ear R")
        copy = self.ok("duplicate_layer Ear R", "duplicate_layer", doc=doc, id=ids[ear],
                       name="Ear L")
        self.ok("transform_layer flip", "transform_layer", doc=doc, id=copy["layer"]["id"], flip="h",
                pivot=[2900, 0])
        pendant = next(p for p in ids if "Pendant" in p.split("/")[-1])
        stats = self.ok("layer_stats", "layer_stats", doc=doc, ids=[ids[pendant]])
        b = stats["layers"][0]["bounds"]
        self.ok("select rect", "select", doc=doc, mode="rect", x=b["x"], y=b["y"],
                w=max(1, b["w"] // 2), h=b["h"])
        self.ok("selection_to_layer cut", "selection_to_layer", doc=doc, source=ids[pendant],
                name="Pendant Half", cut=True)
        self.ok("undo", "undo", doc=doc)

        # Stage 6: rename and repaint with a colour in one call.
        bag = next(p for p in ids if p.split("/")[-1] == "Bag")
        self.ok("update_layers rename", "update_layers", doc=doc,
                changes=[{"id": ids[bag], "name": "Bag"}])
        self.ok("draw_shape with color", "draw_shape", doc=doc, layer=ids[bag], shape="ellipse",
                x=b["x"], y=b["y"], width=40, height=30, color="#3fa34d")
        self.ok("save again", "save", doc=doc)
