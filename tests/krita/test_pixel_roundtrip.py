"""Lossless layer pixel round trips (F003 K1, K5).

- A synthetic layer: a 0-255 alpha ramp across saturated colours, colour
  under alpha 0 next to it, on a paint device whose origin is not (0, 0),
  survives 10 ``export_layers`` -> ``create_layers(new_document=true)``
  round trips byte for byte (layer, device origin, composite), and 10
  ``import_png`` -> ``export_layers`` round trips.
- The Mao ``.kra`` (``KRITA_MCP_MAO_KRA``, e.g.
  ``~/krita-mcp-output/mao_pro_layered.kra``): one export and one
  ``create_layers(manifest, new_document=true)`` give the same pixels in
  every layer and the same composite, alpha-0 colour included, and
  ``diff_documents`` finds no difference at all (smoke test 3.2, 3.3).

    KRITA_MCP_MAO_KRA=~/krita-mcp-output/mao_pro_layered.kra make test-krita KRITA_TESTS=roundtrip
"""

import json
import os
import shutil
import tempfile
import unittest

from helpers import temp_config
from kritamcp import nodes, pixels, registry
from kritamcp.commands.pixels import tile_window
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run

MAO_KRA_ENV = "KRITA_MCP_MAO_KRA"
W, H = 320, 200
RAMP_X, RAMP_Y = 21, 37          # where the ramp sits on the canvas
ORIGIN = {"x": 37, "y": 11}      # the paint device's origin (not tile-aligned)
SPECIAL = (12, 34, 56)           # B, G, R under alpha 0
# Saturated colours as (B, G, R), plus a soft one and black.
COLOURS = [(0, 0, 255), (0, 255, 0), (255, 0, 0), (255, 255, 0), (255, 0, 255), (0, 255, 255),
           (255, 255, 255), (37, 140, 211), (0, 0, 0)]
ROW_H = 4


def ramp():
    """``(bgra, w, h)``: one band per colour, alpha 0..255 across, then a
    column of colour under alpha 0 on the right."""
    w, h = 257, ROW_H * len(COLOURS)
    rows = []
    for b, g, r in COLOURS:
        row = b"".join(bytes((b, g, r, a)) for a in range(256)) + bytes(SPECIAL + (0,))
        rows.extend([row] * ROW_H)
    return b"".join(rows), w, h


def _close(handles):
    for handle in handles:
        try:
            registry.lookup(handle)
        except CommandError:
            continue
        run("close_document", doc=handle, discard=True)


class SyntheticRoundTripTest(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kmcp-roundtrip-")
        self.handles = []
        self.cfg = temp_config({"allowed_dirs": [self.tmp]})
        self.cfg.__enter__()

    def tearDown(self):
        try:
            _close(self.handles)
        finally:
            self.cfg.__exit__(None, None, None)
            shutil.rmtree(self.tmp, ignore_errors=True)

    def _source(self):
        canvas = self.assertOk(run("new_canvas", width=W, height=H, background="none",
                                   resolution=72.0, name="ramp"))
        self.assertIsNone(canvas["background_layer"])
        self.assertEqual(canvas["background"], "none")
        handle = canvas["doc"]
        self.handles.append(handle)
        self.assertEqual(self.assertOk(run("list_layers", doc=handle))["counts"], {"total": 0})
        made = self.assertOk(run("create_layers", doc=handle, specs=[
            {"name": "Under", "key": "u"},
            {"name": "Ramp", "key": "r", "blend_mode": "normal", "device_origin": ORIGIN}]))
        ids = {c["key"]: c["id"] for c in made["created"]}
        doc = registry.lookup(handle)
        data, w, h = ramp()
        node = nodes.node_by_id(doc, ids["r"])
        self.assertEqual((node.position().x(), node.position().y()), (ORIGIN["x"], ORIGIN["y"]))
        self.assertTrue(node.setPixelData(data, RAMP_X, RAMP_Y, w, h))
        under = nodes.node_by_id(doc, ids["u"])  # something to composite over
        self.assertTrue(under.setPixelData(bytes((90, 60, 30, 77)) * (W * H), 0, 0, W, H))
        doc.refreshProjection()
        doc.waitForDone()
        return handle

    def _state(self, handle):
        """Per paint layer (by name): device origin and the pixels over the
        whole canvas plus a margin; and the composite."""
        doc = registry.lookup(handle)
        doc.refreshProjection()
        doc.waitForDone()
        layers = {}
        for node in doc.rootNode().childNodes():
            if node.type() != "paintlayer":
                continue
            p = node.position()
            layers[node.name()] = ((p.x(), p.y()),
                                   pixels.as_bytes(node.pixelData(-64, -64, W + 128, H + 128)))
        return layers, pixels.as_bytes(doc.pixelData(0, 0, W, H))

    def test_export_create_ten_times(self):
        source = self._source()
        want_layers, want_composite = self._state(source)
        self.assertIn(bytes(SPECIAL + (0,)), want_layers["Ramp"][1])
        current = source
        for i in range(10):
            out = os.path.join(self.tmp, f"export-{i}")
            exported = self.assertOk(run("export_layers", doc=current, dir=out))
            entry = next(e for e in exported["layers"] if e["name"] == "Ramp")
            self.assertEqual(entry["device_origin"], ORIGIN)
            # The PNG reaches the colour under alpha 0 (tile window, E2).
            self.assertEqual((entry["x"], entry["y"], entry["w"], entry["h"]),
                             (RAMP_X, RAMP_Y, 257, ROW_H * len(COLOURS)))
            rebuilt = self.assertOk(run("create_layers", manifest=exported["manifest"],
                                        new_document=True))
            self.handles.append(rebuilt["doc"])
            self.assertEqual(rebuilt["new_document"]["resolution"], 72)
            layers, composite = self._state(rebuilt["doc"])
            self.assertEqual(set(layers), set(want_layers))
            for name, (origin, data) in want_layers.items():
                self.assertEqual(layers[name][0], origin, f"round {i}: {name} origin")
                self.assertTrue(layers[name][1] == data, f"round {i}: {name} pixels differ")
            self.assertTrue(composite == want_composite, f"round {i}: composite differs")
            diff = self.assertOk(run("diff_documents", doc_a=source, doc_b=rebuilt["doc"]))
            self.assertEqual(diff["document_changes"], {})
            self.assertIs(diff["projection_changed"], False)
            current = rebuilt["doc"]

    def test_import_png_export_ten_times(self):
        data, w, h = ramp()
        png = os.path.join(self.tmp, "ramp-0.png")
        pixels.save_png(data, png, w, h)
        canvas = self.assertOk(run("new_canvas", width=W, height=H, background="none"))
        handle = canvas["doc"]
        self.handles.append(handle)
        for i in range(10):
            made = self.assertOk(run("create_layers", doc=handle,
                                     specs=[{"name": f"Imported {i}", "visible": False}]))
            nid = made["created"][0]["id"]
            self.assertOk(run("import_png", doc=handle, id=nid, png=png, x=RAMP_X, y=RAMP_Y))
            node = nodes.node_by_id(registry.lookup(handle), nid)
            self.assertTrue(pixels.as_bytes(node.pixelData(RAMP_X, RAMP_Y, w, h)) == data,
                            f"round {i}: import_png changed the pixels")
            out = os.path.join(self.tmp, f"import-export-{i}")
            exported = self.assertOk(run("export_layers", doc=handle, dir=out, ids=[nid]))
            (entry,) = [e for e in exported["layers"] if e["id"] == nid]
            png = os.path.join(out, entry["png"])
            back, bw, bh = pixels.load_png_bgra(png)
            self.assertEqual((bw, bh, entry["x"], entry["y"]), (w, h, RAMP_X, RAMP_Y))
            self.assertTrue(back == data, f"round {i}: exported PNG differs")

    def test_tile_window(self):
        self.assertEqual(tile_window((0, 0, 1, 1), (0, 0)), (0, 0, 64, 64))
        self.assertEqual(tile_window((64, 64, 64, 64), (0, 0)), (64, 64, 64, 64))
        self.assertEqual(tile_window((40, 20, 30, 5), (37, 11)), (37, 11, 64, 64))
        self.assertEqual(tile_window((30, 5, 10, 10), (37, 11)), (-27, -53, 128, 128))


@unittest.skipUnless(os.environ.get(MAO_KRA_ENV), f"set {MAO_KRA_ENV} to the layered Mao .kra")
class MaoKraRoundTripTest(EnvelopeAssertions, unittest.TestCase):
    """Smoke test 3.1-3.3 headless: export, rebuild in one call, compare."""

    def test_mao_kra_round_trip(self):
        src = os.path.realpath(os.path.expanduser(os.environ[MAO_KRA_ENV]))
        tmp = tempfile.mkdtemp(prefix="kmcp-mao-kra-")
        handles = []
        try:
            with temp_config({"allowed_dirs": [tmp, os.path.dirname(src)]}):
                self._round_trip(src, tmp, handles)
        finally:
            _close(handles)
            shutil.rmtree(tmp, ignore_errors=True)

    def _round_trip(self, src, tmp, handles):
        opened = self.assertOk(run("open_file", path=src))
        handles.append(opened["doc"])
        exported = self.assertOk(run("export_layers", doc=opened["doc"],
                                     dir=os.path.join(tmp, "export"), png_level=1))
        self.assertEqual((exported["skipped"], exported["warnings"]), ([], []))
        rebuilt = self.assertOk(run("create_layers", manifest=exported["manifest"],
                                    new_document=True))
        handles.append(rebuilt["doc"])
        self.assertEqual(rebuilt["counts"], {"grouplayer": 38, "paintlayer": 184, "total": 222})

        a, b = registry.lookup(opened["doc"]), registry.lookup(rebuilt["doc"])
        by_key = {c["key"]: c["id"] for c in rebuilt["created"]}
        differing = []
        for e in exported["layers"]:
            if e["type"] != "paint":
                continue
            na, nb = nodes.node_by_id(a, e["id"]), nodes.node_by_id(b, by_key[e["key"]])
            pa, pb = na.position(), nb.position()
            self.assertEqual((pa.x(), pa.y()), (pb.x(), pb.y()), e["path"])
            r = na.bounds()
            if r.width() <= 0:
                continue
            rect = tile_window((r.x(), r.y(), r.width(), r.height()), (pa.x(), pa.y()))
            if pixels.as_bytes(na.pixelData(*rect)) != pixels.as_bytes(nb.pixelData(*rect)):
                differing.append(e["path"])
        self.assertEqual(differing, [])
        for d in (a, b):
            d.refreshProjection()
            d.waitForDone()
        w, h = a.width(), a.height()
        same = pixels.as_bytes(a.pixelData(0, 0, w, h)) == pixels.as_bytes(b.pixelData(0, 0, w, h))
        self.assertTrue(same, "the rebuilt composite differs")
        del a, b
        diff = self.assertOk(run("diff_documents", doc_a=opened["doc"], doc_b=rebuilt["doc"],
                                 ignore_alpha0_rgb=False))
        self.assertEqual(diff["document_changes"], {})
        self.assertIs(diff["projection_changed"], False)
        for key in ("added", "removed", "renamed", "moved", "changed", "pixel_changes"):
            self.assertEqual(diff[key], [], key)
        self._without_origins(exported["manifest"], opened["doc"], handles)

    def _without_origins(self, manifest_path, source, handles):
        """K4 acceptance: the smoke test's 3.3 situation (a manifest without
        device origins, i.e. before the K1 fix) is quantified, not a bare
        projection_changed: 316 visible pixels off by at most 1, plus
        2,859 transparent ones whose hidden colour differs. The smoke test
        saw 2,870: the other 11 were colour under alpha 0 that the old
        export crop dropped, which E2 now keeps."""
        with open(manifest_path) as f:
            data = json.load(f)
        for entry in data["layers"]:
            entry.pop("device_origin", None)
        stripped = os.path.join(os.path.dirname(manifest_path), "manifest-no-origins.json")
        with open(stripped, "w") as f:
            json.dump(data, f)
        rebuilt = self.assertOk(run("create_layers", manifest=stripped, new_document=True))
        handles.append(rebuilt["doc"])
        diff = self.assertOk(run("diff_documents", doc_a=source, doc_b=rebuilt["doc"]))
        projection = diff["projection"]
        self.assertIs(diff["projection_changed"], True)
        self.assertEqual((projection["changed_count"], projection["max_channel_diff"],
                          projection["alpha0_rgb_count"]), (316, 1, 2859))
        self.assertIsNotNone(projection["bounds"])
        self.assertIsNotNone(projection["alpha0_rgb_bounds"])
        self.assertEqual(diff["pixel_changes"], [])  # every layer's own pixels are the same
        print(f"KMCP_K4: projection {projection}", flush=True)
