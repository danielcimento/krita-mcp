"""commands/analysis.py: the lint snapshot collector (P4.1), headless.

Every document is a ``scratch_document`` (closed in ``finally``) or, for
Mao, closed with ``close_document``. Stray-alpha cases run through both
dilations, ``Selection.grow`` and the big-int shift fallback, which must
agree exactly (same kernel: ``alphastats.kernel_half_widths``).

The Mao timing test is skipped unless ``KRITA_MCP_MAO_ORA`` names the Mao
``.ora``; it prints ``KMCP_LINT: ...`` lines to kritarunner's stdout
(``~/krita-mcp-output/test-krita/run.log``).
"""

import json
import os
import random
import shutil
import sys
import tempfile
import time
import types
import unittest

from krita import Krita, Selection

from helpers import add_paint_layer, scratch_document, settle, temp_config
from kritamcp import alphastats, config, registry
from kritamcp.commands import analysis
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run

NODE_KEYS = ["id", "name", "path", "type", "parent", "depth", "order", "visible", "opacity",
             "blend_mode", "inherit_alpha", "alpha_locked", "locked", "pass_through",
             "has_layer_style", "bounds", "alpha"]
DEFAULTS = {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4}

# A layer style as Krita's own ``layerStyleToAsl`` writes it (PSD-XML), with
# a drop shadow; setLayerStyleFromAsl accepts and keeps it (2026-11-25; a
# visibly rendered style is in test_lint_fixture.py, STYLE_ASL).
ASL = ('<asl><node type="Descriptor" name="" classId="null">'
       '<node type="UnitFloat" unit="#Prc" key="Scl " value="100"/>'
       '<node type="Boolean" key="masterFXSwitch" value="1"/>'
       '<node type="Descriptor" key="DrSh" name="" classId="DrSh">'
       '<node type="Boolean" key="enab" value="1"/>'
       '<node type="Enum" key="Md  " typeId="BlnM" value="Mltp"/>'
       '<node type="UnitFloat" unit="#Prc" key="Opct" value="75"/>'
       '<node type="UnitFloat" unit="#Pxl" key="Dstn" value="5"/>'
       '<node type="UnitFloat" unit="#Pxl" key="blur" value="5"/>'
       '</node></node></asl>')


def alpha_layer(doc, name, rect, points=(), blocks=(), parent=None):
    """A paint layer whose pixels in ``rect`` (x, y, w, h) are grey with
    the alpha given by ``blocks`` (``(x, y, w, h, alpha)``, canvas
    coordinates, later ones on top) and ``points`` (``(x, y, alpha)``)."""
    x0, y0, w, h = rect
    a = bytearray(w * h)
    for bx, by, bw, bh, value in blocks:
        for y in range(by, by + bh):
            a[(y - y0) * w + bx - x0:(y - y0) * w + bx - x0 + bw] = bytes([value]) * bw
    for px, py, value in points:
        a[(py - y0) * w + px - x0] = value
    data = bytearray(b"\x80" * (4 * w * h))
    data[3::4] = a
    node = doc.createNode(name, "paintlayer")
    (parent or doc.rootNode()).addChildNode(node, None)
    assert node.setPixelData(bytes(data), x0, y0, w, h)
    return node


def by_name(snapshot):
    return {n["name"]: n for n in snapshot["nodes"]}


BLOCK = (20, 20, 10, 10, 255)  # x 20..29, y 20..29
WINDOW = (0, 0, 64, 64)


class SnapshotStructureTest(unittest.TestCase):
    def test_tree_order_depth_parent_and_properties(self):
        with scratch_document(64, 64) as doc:
            root = doc.rootNode()
            base = alpha_layer(doc, "Base", (0, 0, 8, 8), blocks=[(0, 0, 8, 8, 255)])
            base.setBlendingMode("multiply")
            base.setOpacity(128)
            face = doc.createGroupLayer("Face")
            root.addChildNode(face, None)
            face.setPassThroughMode(True)
            eye = alpha_layer(doc, "Eye", (10, 10, 4, 4), blocks=[(10, 10, 4, 4, 255)], parent=face)
            eye.setInheritAlpha(True)
            eye.setAlphaLocked(True)
            eye.setLocked(True)
            eye.setVisible(False)
            mask = doc.createTransparencyMask("Mask")
            eye.addChildNode(mask, None)
            blur = doc.createFilterLayer("Blur", Krita.instance().filter("blur"), _all(64, 64))
            self.assertIsNotNone(blur, "createFilterLayer works headless")
            root.addChildNode(blur, None)
            sel = Selection()
            sel.select(0, 0, 8, 8, 255)
            doc.setSelection(sel)
            try:
                settle(doc)
                self.assertIn("selectionmask", [c.type() for c in root.childNodes()])
                snap = analysis.collect_snapshot(doc, DEFAULTS)
            finally:
                doc.setSelection(None)
            json.dumps(snap)  # plain JSON

        self.assertEqual(list(snap), ["document", "settings", "nodes"])
        self.assertEqual(snap["document"], {
            "width": 64, "height": 64, "color_model": "RGBA", "color_depth": "U8",
            "color_profile": snap["document"]["color_profile"], "pixel_stats": True})
        self.assertIn("sRGB", snap["document"]["color_profile"])
        self.assertEqual(snap["settings"], DEFAULTS)
        nodes = snap["nodes"]
        for n in nodes:
            self.assertEqual(list(n), NODE_KEYS)
        self.assertEqual([(n["name"], n["type"], n["depth"]) for n in nodes], [
            ("Blur", "filterlayer", 1), ("Face", "grouplayer", 1), ("Eye", "paintlayer", 2),
            ("Mask", "transparencymask", 3), ("Base", "paintlayer", 1),
            ("Background", "paintlayer", 1)])
        self.assertEqual([n["order"] for n in nodes], list(range(6)))
        named = by_name(snap)
        self.assertIsNone(named["Blur"]["parent"])
        self.assertIsNone(named["Face"]["parent"])
        self.assertEqual(named["Eye"]["parent"], named["Face"]["id"])
        self.assertEqual(named["Mask"]["parent"], named["Eye"]["id"])
        self.assertEqual(named["Mask"]["path"], "Face/Eye/Mask")
        self.assertTrue(named["Face"]["pass_through"])
        self.assertIsNone(named["Eye"]["pass_through"])
        self.assertEqual(named["Base"]["blend_mode"], "multiply")
        self.assertAlmostEqual(named["Base"]["opacity"], 128 / 255, places=2)
        eye_n = named["Eye"]
        self.assertEqual((eye_n["visible"], eye_n["inherit_alpha"], eye_n["alpha_locked"],
                          eye_n["locked"]), (False, True, True, True))
        for n in nodes:
            self.assertIs(n["has_layer_style"], False, n["name"])
            if n["type"] != "paintlayer":
                self.assertIsNone(n["alpha"], n["name"])
        self.assertEqual(eye_n["alpha"], {"pixel_count": 16, "opaque_bounds":
                                          {"x": 10, "y": 10, "w": 4, "h": 4}, "stray_count": 0})
        self.assertEqual(eye_n["bounds"], {"x": 10, "y": 10, "w": 4, "h": 4})
        self.assertEqual(named["Background"]["alpha"],
                         {"pixel_count": 64 * 64, "opaque_bounds": {"x": 0, "y": 0, "w": 64, "h": 64},
                          "stray_count": 0})

    def test_layer_style(self):
        with scratch_document(16, 16) as doc:
            styled = add_paint_layer(doc, "Styled")
            plain = add_paint_layer(doc, "Plain")
            self.assertTrue(styled.setLayerStyleFromAsl(ASL))
            self.assertNotEqual(styled.layerStyleToAsl(), "")
            mask = doc.createTransparencyMask("Mask")
            plain.addChildNode(mask, None)
            named = by_name(analysis.collect_snapshot(doc, DEFAULTS))
            del styled, plain, mask
        self.assertIs(named["Styled"]["has_layer_style"], True)
        self.assertIs(named["Plain"]["has_layer_style"], False)
        self.assertIs(named["Mask"]["has_layer_style"], False)

    def test_non_rgba8_document(self):
        with scratch_document(16, 16, color_depth="U16") as doc:
            add_paint_layer(doc, "Paint")
            snap = analysis.collect_snapshot(doc, DEFAULTS)
        self.assertEqual((snap["document"]["color_depth"], snap["document"]["pixel_stats"]),
                         ("U16", False))
        self.assertEqual([n["alpha"] for n in snap["nodes"]], [None, None])
        self.assertEqual([n["type"] for n in snap["nodes"]], ["paintlayer", "paintlayer"])


def _all(w, h):
    sel = Selection()
    sel.select(0, 0, w, h, 255)
    return sel


class AlphaStatsTest(unittest.TestCase):
    def test_counts_and_bounds(self):
        with scratch_document(32, 32) as doc:
            alpha_layer(doc, "Off Canvas", (-6, -4, 12, 10),
                        blocks=[(-6, -4, 3, 2, 255), (2, 3, 4, 3, 1)])
            add_paint_layer(doc, "Empty")
            hidden = doc.createNode("Colour Under Zero", "paintlayer")
            doc.rootNode().addChildNode(hidden, None)
            self.assertTrue(hidden.setPixelData(bytes([10, 20, 30, 0]) * 16, 4, 4, 4, 4))
            settle(doc)
            timings = []
            named = by_name(analysis.collect_snapshot(doc, DEFAULTS, timings=timings))
            del hidden
        off = named["Off Canvas"]
        self.assertEqual(off["bounds"], {"x": -6, "y": -4, "w": 12, "h": 10})
        self.assertEqual(off["alpha"]["pixel_count"], 6 + 12)
        self.assertEqual(off["alpha"]["opaque_bounds"], {"x": -6, "y": -4, "w": 12, "h": 10})
        # The alpha-1 block is weak, more than 4 px from the strong one: stray.
        self.assertEqual(off["alpha"]["stray_count"], 12)
        for name in ("Empty", "Colour Under Zero"):
            self.assertIsNone(named[name]["bounds"], name)
            self.assertEqual(named[name]["alpha"],
                             {"pixel_count": 0, "opaque_bounds": None, "stray_count": 0}, name)
        self.assertEqual(len(timings), 4)  # every paint layer, Background included

    def test_opaque_bounds_tighter_than_window(self):
        # opaque_bounds is computed from the pixels; bounds() agrees here.
        with scratch_document(32, 32) as doc:
            alpha_layer(doc, "L", (0, 0, 20, 20), points=[(3, 4, 200), (15, 9, 1)])
            settle(doc)
            got = by_name(analysis.collect_snapshot(doc, DEFAULTS))["L"]
        self.assertEqual(got["alpha"]["opaque_bounds"], {"x": 3, "y": 4, "w": 13, "h": 6})
        self.assertEqual(got["bounds"], got["alpha"]["opaque_bounds"])
        self.assertEqual(got["alpha"]["pixel_count"], 2)


# name -> (blocks, points, stray count at the defaults). The block's right
# column is x = 29 and its bottom-right corner (29, 29).
STRAY_CASES = {
    # Anti-aliased edge: rings of alpha 5 and 2 around the opaque block.
    "AA Edge": ([(18, 18, 14, 14, 2), (19, 19, 12, 12, 5), BLOCK], [], 0),
    "Speck 10 Px": ([BLOCK], [(39, 25, 3)], 1),
    "Speck 3 Px": ([BLOCK], [(32, 25, 3)], 0),
    # The kernel covers the axis points at the distance, not one further.
    "Speck 4 Px": ([BLOCK], [(33, 25, 3)], 0),
    "Speck 5 Px": ([BLOCK], [(34, 25, 3)], 1),
    # Diagonals from the corner: (3, 3) and (4, 2) are inside the kernel
    # (Euclidean 4.24 and 4.47), (4, 3) is not.
    "Diagonal 3 3": ([BLOCK], [(32, 32, 3)], 0),
    "Diagonal 4 2": ([BLOCK], [(33, 31, 3)], 0),
    "Diagonal 4 3": ([BLOCK], [(33, 32, 3)], 1),
    "Only Faint": ([(5, 5, 3, 2, 3)], [], 6),
    "Strong Only": ([BLOCK, (40, 40, 2, 2, 8)], [], 0),
    # An alpha-1 ring 3 px wide (corner (17, 17) is (3, 3) from the block),
    # an alpha-7 and an alpha-1 speck far from everything, and a lone
    # alpha-255 pixel.
    "Mixed": ([(17, 17, 16, 16, 1), BLOCK], [(50, 50, 7), (55, 10, 1), (2, 60, 255)], 2),
}
SPECKS = ("Speck 10 Px", "Speck 3 Px", "Speck 4 Px", "Speck 5 Px", "Diagonal 3 3",
          "Diagonal 4 2", "Diagonal 4 3")


def expected(**changes):
    out = {name: case[2] for name, case in STRAY_CASES.items()}
    out.update({name.replace("_", " "): v for name, v in changes.items()})
    return out


class StrayAlphaTest(unittest.TestCase):
    def _snapshot(self, settings, method):
        with scratch_document(64, 64) as doc:
            for name, (blocks, points, _) in STRAY_CASES.items():
                alpha_layer(doc, name, WINDOW, points=points, blocks=blocks)
            settle(doc)
            return by_name(analysis.collect_snapshot(doc, settings, method=method))

    def test_grow_self_check_passes(self):
        # On Krita 5.3.4 Selection.grow matches the documented kernel, so
        # "auto" uses it.
        for r in (1, 2, 3, 4, 5, 8):
            self.assertTrue(analysis._grow_works(r), r)
            self.assertIs(analysis._dilate_for("auto", r), analysis.grow_dilate)

    def test_cases_both_methods(self):
        results = {m: self._snapshot(DEFAULTS, m) for m in ("grow", "shift", "auto")}
        for method, named in results.items():
            got = {name: named[name]["alpha"]["stray_count"] for name in STRAY_CASES}
            self.assertEqual(got, expected(), method)
        alphas = {m: {k: n["alpha"] for k, n in named.items()} for m, named in results.items()}
        self.assertEqual(alphas["grow"], alphas["shift"])  # separate documents: ids differ

    def test_custom_thresholds(self):
        zero = dict.fromkeys(STRAY_CASES, 0)
        cases = (
            # The 10 px speck is on the axis at the distance: covered.
            ({"stray_alpha_distance": 10},
             expected(**{"Speck_10_Px": 0, "Speck_5_Px": 0, "Diagonal_4_3": 0})),
            # Alpha 3 counts as strong: the specks and Only Faint are clean;
            # the alpha-7 speck is strong, the alpha-1 one still stray.
            ({"stray_alpha_below": 3},
             dict(zero, Mixed=1)),
            ({"stray_alpha_below": 6}, expected(Mixed=1)),
            ({"stray_alpha_below": 1}, zero),  # nothing is weak
            # Distance 0: every weak pixel is stray.
            ({"stray_alpha_distance": 0},
             dict(expected(**{"AA_Edge": 14 * 14 - 100, "Mixed": 16 * 16 - 100 + 2}),
                  **dict.fromkeys(SPECKS, 1))),
        )
        for settings, want in cases:
            full = dict(DEFAULTS, **settings)
            for method in ("grow", "shift"):
                named = self._snapshot(full, method)
                self.assertEqual(named["AA Edge"]["alpha"]["pixel_count"], 14 * 14)
                got = {name: named[name]["alpha"]["stray_count"] for name in STRAY_CASES}
                self.assertEqual(got, want, (settings, method))

    def test_grow_and_shift_agree_on_random_masks(self):
        rng = random.Random(41)
        sparse = bytes(1 if v < 6 else 0 for v in range(256))
        for r in (1, 2, 3, 4, 5, 8):
            for w, h in ((37, 23), (1, 9), (9, 1), (64, 64)):
                mask = rng.randbytes(w * h).translate(sparse)
                grown = analysis.grow_dilate(mask, w, h, r).translate(bytes([0] + [1] * 255))
                self.assertEqual(grown, alphastats.dilate_shift(mask, w, h, r), (r, w, h))

    def test_auto_falls_back_when_grow_fails(self):
        saved_grow, saved_checked = analysis.grow_dilate, dict(analysis._grow_checked)

        def broken(*args):
            raise RuntimeError("no grow")

        analysis.grow_dilate = broken
        analysis._grow_checked.clear()
        try:
            self.assertFalse(analysis._grow_works(4))
            named = self._snapshot(DEFAULTS, "auto")
        finally:
            analysis.grow_dilate = saved_grow
            analysis._grow_checked.clear()
            analysis._grow_checked.update(saved_checked)
        self.assertEqual(named["Speck 10 Px"]["alpha"]["stray_count"], 1)
        self.assertEqual(named["Speck 4 Px"]["alpha"]["stray_count"], 0)


class SettingsAndCommandTest(EnvelopeAssertions, unittest.TestCase):
    def test_settings_from_config_lint_section(self):
        self.assertEqual(analysis.lint_settings(),
                         alphastats.normalize_settings(getattr(config.current(), "lint", None)))
        saved = config.current
        fake = types.SimpleNamespace(lint={"stray_alpha_below": 4, "additive_mode": "add"})
        config.current = lambda: fake
        try:
            self.assertEqual(analysis.lint_settings(), {
                "additive_mode": "add", "stray_alpha_below": 4, "stray_alpha_distance": 4})
            with scratch_document(64, 64) as doc:
                alpha_layer(doc, "Faint", WINDOW, blocks=[(5, 5, 2, 2, 3)])
                settle(doc)
                snap = analysis.collect_snapshot(doc)
        finally:
            config.current = saved
        self.assertEqual(snap["settings"]["stray_alpha_below"], 4)
        self.assertEqual(by_name(snap)["Faint"]["alpha"]["stray_count"], 4)

    def test_settings_without_lint_section_are_defaults(self):
        saved = config.current
        config.current = lambda: types.SimpleNamespace()
        try:
            self.assertEqual(analysis.lint_settings(), DEFAULTS)
        finally:
            config.current = saved

    def test_lint_snapshot_command(self):
        with scratch_document(16, 16) as doc:
            alpha_layer(doc, "L", (0, 0, 4, 4), blocks=[(0, 0, 4, 4, 255)])
            settle(doc)
            handle = registry.handle_for(doc)
            result = self.assertOk(run("lint_snapshot", doc=handle))
            direct = analysis.collect_snapshot(doc)
        self.assertEqual(list(result), ["doc", "snapshot"])
        self.assertEqual(result["doc"], handle)
        self.assertEqual(result["snapshot"], direct)
        self.assertEqual([n["name"] for n in result["snapshot"]["nodes"]], ["L", "Background"])
        self.assertError(run("lint_snapshot", doc="doc-999999"), "not_found")


MAO_ENV = "KRITA_MCP_MAO_ORA"
TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "tools")


def _log(msg):
    print(f"KMCP_LINT: {msg}", flush=True)


@unittest.skipUnless(os.environ.get(MAO_ENV), f"set {MAO_ENV} to the Mao .ora (acceptance § 1)")
class MaoSnapshotTimingTest(EnvelopeAssertions, unittest.TestCase):
    """The snapshot of the headless Mao rebuild (as test_mao_headless):
    total time, and the slowest paint layer, with each dilation."""

    def test_mao_snapshot_timing(self):
        ora = os.path.expanduser(os.environ[MAO_ENV])
        self.assertTrue(os.path.isfile(ora), ora)
        if TOOLS not in sys.path:
            sys.path.insert(0, TOOLS)
        import ora_to_manifest

        tmp = tempfile.mkdtemp(prefix="kmcp-lint-mao-")
        handle = None
        try:
            with temp_config({"allowed_dirs": [tmp]}):
                out = os.path.join(tmp, "mao")
                ora_to_manifest.convert(ora, out)
                canvas = self.assertOk(run("new_canvas", width=5800, height=8400,
                                           background="transparent", name="kmcp-lint-mao"))
                handle = canvas["doc"]
                self.assertOk(run("create_layers", doc=handle,
                                  manifest=os.path.join(out, "manifest.json")))
                envelope = run("lint_snapshot", doc=handle)
                snap = self.assertOk(envelope)["snapshot"]
                _log(f"lint_snapshot elapsed_ms={envelope['elapsed_ms']}")
                doc = registry.lookup(handle)
                for method in ("grow", "shift"):
                    timings = []
                    t = time.perf_counter()
                    again = analysis.collect_snapshot(doc, method=method, timings=timings)
                    total = time.perf_counter() - t
                    worst = max(timings, key=lambda item: item[1])
                    worst_name = next(n["path"] for n in again["nodes"] if n["id"] == worst[0])
                    _log(f"{method}: total {total:.2f} s over {len(timings)} paint layers; "
                         f"slowest {worst[1] * 1000:.0f} ms ({worst_name})")
                    self.assertEqual(again, snap, method)
                del doc
            paint = [n for n in snap["nodes"] if n["type"] == "paintlayer"]
            self.assertEqual(len(paint), 185)  # 184 + new_canvas's Background
            for n in paint:
                ob = n["alpha"]["opaque_bounds"]
                self.assertEqual(ob, n["bounds"], n["path"])
            strays = [(n["path"], n["alpha"]["stray_count"]) for n in paint
                      if n["alpha"]["stray_count"]]
            empty = [n["path"] for n in paint if n["alpha"]["pixel_count"] == 0]
            _log(f"layers with stray alpha: {len(strays)}, total "
                 f"{sum(c for _, c in strays)} pixels; top {sorted(strays, key=lambda s: -s[1])[:5]}; "
                 f"empty: {empty}")
        finally:
            if handle is not None:
                try:
                    registry.lookup(handle)
                    run("close_document", doc=handle, discard=True)
                except CommandError:
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertFalse(os.path.exists(tmp))
