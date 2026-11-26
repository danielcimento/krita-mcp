"""occlusion_map (commands/occlusion.py, occlusionplan.py), headless, through
dispatch.execute (P5.8).

Every call passes ``doc`` (headless there is no active document) and every
document is closed by ``scratch_document``. Specs are listed bottom to top,
as ``create_layers`` takes them; ``createDocument``'s white Background is
the bottom layer of every document (below every target, so never counted).
"""

import contextlib
import os
import shutil
import tempfile
import unittest

from krita import Krita
from PyQt5.QtGui import QImage

from helpers import scratch_document, settle, temp_config
from kritamcp import batch, nodes, pixels, registry
from test_dispatch import EnvelopeAssertions, run

W, H = 40, 30


def solid(w, h, a=255, bgr=(40, 80, 160)):
    return bytes((*bgr, a)) * (w * h)


def alpha_ramp(w, h):
    """Alpha (x * 7 + y * 13) % 256 with a fixed colour: every row differs."""
    return b"".join(bytes((10, 20, 30, (x * 7 + y * 13) % 256)) for y in range(h)
                    for x in range(w))


def read_map(path):
    image = QImage(path)
    assert image.format() == QImage.Format_Grayscale8, image.format()
    return image.width(), image.height(), pixels.alpha8_bytes(image)


class Base(EnvelopeAssertions, unittest.TestCase):
    def setUp(self):
        self._stack = contextlib.ExitStack()
        self.root = tempfile.mkdtemp(prefix="kmcp-occl-")
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

    def occl(self, handle, layer, name="map.png", **params):
        path = os.path.join(self.root, name)
        result = self.assertOk(run("occlusion_map", doc=handle, id=layer, path=path, **params))
        return result

    def assertCoverage(self, handle, layer, pct, above=None, **params):
        result = self.occl(handle, layer, **params)
        self.assertEqual(result["covered_pct"], pct, result)
        if above is not None:
            self.assertEqual(result["above"]["ids"], above, result)
            self.assertEqual(result["above"]["count"], len(above))
        return result


class BasicTest(Base):
    def test_fully_covered_map_is_target_alpha(self):
        with scratch_document(W, H, name="kmcp-occl-full") as doc:
            ramp = alpha_ramp(12, 9)
            handle, ids = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                     {"T": ((4, 5, 12, 9), ramp),
                                      "C": ((0, 0, W, H), solid(W, H))})
            result = self.assertCoverage(handle, "T", 1.0, above=[ids["C"]])
            self.assertIs(result["fully_hidden"], True)
            self.assertEqual(result["bounds"], {"x": 4, "y": 5, "w": 12, "h": 9})
            self.assertEqual(result["pixels"], 12 * 9 - ramp[3::4].count(0))
            self.assertEqual((result["layer"], result["name"], result["doc"]),
                             (ids["T"], "T", handle))
            self.assertEqual((result["modes"], result["threshold"]), (["normal"], 0.5))
            self.assertEqual(result["path"], os.path.join(os.path.realpath(self.root), "map.png"))
            self.assertEqual(result["bytes"], os.path.getsize(result["path"]))
            self.assertEqual(read_map(result["path"]), (12, 9, ramp[3::4]))

    def test_half_covered_crisp(self):
        with scratch_document(W, H, name="kmcp-occl-half") as doc:
            handle, ids = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                     {"T": ((2, 3, 20, 10), solid(20, 10)),
                                      "C": ((2, 0, 10, H), solid(10, H, bgr=(1, 2, 3)))})
            result = self.assertCoverage(handle, "T", 0.5, above=[ids["C"]])
            self.assertEqual((result["pixels"], result["covered_px"]), (200, 100))
            self.assertIs(result["fully_hidden"], False)
            row = b"\xff" * 10 + b"\x00" * 10
            self.assertEqual(read_map(result["path"]), (20, 10, row * 10))

    def test_nothing_above(self):
        with scratch_document(W, H, name="kmcp-occl-none") as doc:
            handle, ids = self.build(doc, [{"name": "Below"}, {"name": "T"}],
                                     {"Below": ((0, 0, W, H), solid(W, H)),
                                      "T": ((2, 3, 5, 4), solid(5, 4))})
            result = self.assertCoverage(handle, "T", 0.0, above=[])
            self.assertIs(result["fully_hidden"], False)
            self.assertEqual(read_map(result["path"]), (5, 4, bytes(20)))

    def test_exact_product_all_pairs(self):
        # Target alpha x on column x, coverage y on row y: the map must be
        # round(x * y / 255) for all 65536 pairs.
        with scratch_document(256, 256, name="kmcp-occl-exact") as doc:
            target = b"".join(bytes((10, 20, 30, x)) for _ in range(256) for x in range(256))
            cover = b"".join(bytes((200, 100, 50, y)) * 256 for y in range(256))
            handle, ids = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                     {"T": ((0, 0, 256, 256), target),
                                      "C": ((0, 0, 256, 256), cover)})
            result = self.occl(handle, "T")
            self.assertEqual(result["bounds"], {"x": 1, "y": 0, "w": 255, "h": 256})
            w, h, values = read_map(result["path"])
            expected = bytes((x * y + 127) // 255 for y in range(256) for x in range(1, 256))
            self.assertEqual((w, h), (255, 256))
            bad = sum(1 for a, b in zip(values, expected) if a != b)
            self.assertEqual(bad, 0)
            # covered: target alpha > 0 (every pixel here) with coverage >= 128.
            self.assertEqual(result["covered_px"], 255 * 128)
            self.assertEqual(result["covered_pct"], 128 / 256)

    def test_partial_coverage_and_threshold(self):
        with scratch_document(W, H, name="kmcp-occl-thr") as doc:
            ramp = alpha_ramp(10, 6)
            handle, ids = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                     {"T": ((0, 0, 10, 6), ramp),
                                      "C": ((0, 0, W, H), solid(W, H, a=128))})
            for threshold, pct in ((0.5, 1.0), (128 / 255, 1.0), (0.51, 0.0), (1.0, 0.0),
                                   (0.001, 1.0)):
                result = self.assertCoverage(handle, "T", pct, threshold=threshold)
                self.assertIs(result["fully_hidden"], False)
                self.assertEqual(result["threshold"], threshold)
            expected = bytes((a * 128 + 127) // 255 for a in ramp[3::4])
            self.assertEqual(read_map(result["path"])[2], expected)
            # Two strips: alpha 255 over the left half, 100 over the right.
            node = nodes.node_by_id(doc, ids["C"])
            node.setPixelData(solid(W, H, a=0), 0, 0, W, H)
            node.setPixelData(solid(5, H), 0, 0, 5, H)
            node.setPixelData(solid(5, H, a=100), 5, 0, 5, H)
            settle(doc)
            painted = ramp[3::4]
            left = sum(1 for i, a in enumerate(painted) if a and i % 10 < 5)
            total = len(painted) - painted.count(0)
            self.assertCoverage(handle, "T", left / total, threshold=1.0)
            self.assertCoverage(handle, "T", 1.0, threshold=100 / 255)
            self.assertCoverage(handle, "T", left / total, threshold=101 / 255)


class ModesTest(Base):
    def test_multiply_counts_only_when_asked(self):
        with scratch_document(W, H, name="kmcp-occl-mul") as doc:
            handle, ids = self.build(doc, [{"name": "T"},
                                           {"name": "M", "blend_mode": "multiply"}],
                                     {"T": ((3, 3, 8, 8), solid(8, 8)),
                                      "M": ((0, 0, W, H), solid(W, H))})
            result = self.assertCoverage(handle, "T", 0.0, above=[])
            self.assertEqual(read_map(result["path"])[2], bytes(64))
            result = self.assertCoverage(handle, "T", 1.0, above=[ids["M"]],
                                         modes=["normal", "multiply", "normal"])
            self.assertEqual(result["modes"], ["normal", "multiply"])

    def test_add_and_linear_dodge_are_one_mode(self):
        with scratch_document(W, H, name="kmcp-occl-add") as doc:
            handle, ids = self.build(doc, [{"name": "T"},
                                           {"name": "D", "blend_mode": "linear_dodge"},
                                           {"name": "A", "blend_mode": "add"}],
                                     {"T": ((0, 0, 10, 4), solid(10, 4)),
                                      "D": ((0, 0, 5, 4), solid(5, 4)),
                                      "A": ((5, 0, 5, 4), solid(5, 4))})
            self.assertCoverage(handle, "T", 0.0, above=[])
            for modes in (["add"], ["linear_dodge"]):
                result = self.assertCoverage(handle, "T", 1.0, above=[ids["A"], ids["D"]],
                                             modes=modes)
                self.assertEqual(result["modes"], modes)


class TreeTest(Base):
    def test_hidden_and_below_dont_count(self):
        with scratch_document(W, H, name="kmcp-occl-hidden") as doc:
            handle, ids = self.build(doc, [
                {"name": "Below"},
                {"name": "T"},
                {"name": "Hid", "visible": False},
                {"key": "g", "name": "HG", "type": "group", "visible": False},
                {"name": "InHid", "parent": "$g"},
            ], {"Below": ((0, 0, W, H), solid(W, H)),
                "T": ((2, 2, 6, 6), solid(6, 6)),
                "Hid": ((0, 0, W, H), solid(W, H)),
                "InHid": ((0, 0, W, H), solid(W, H))})
            self.assertCoverage(handle, "T", 0.0, above=[])
            self.assertCoverage(handle, "Below", 36 / (W * H), above=[ids["T"]])

    def test_global_draw_order_across_groups(self):
        # Root, bottom to top: Low (group: L1), G (group: U0, T, U1), Mid,
        # G2 (pass-through: P1), Top. T is covered by U1, Mid, P1 and Top,
        # each over its own column band, not by L1 or U0.
        with scratch_document(W, H, name="kmcp-occl-order") as doc:
            handle, ids = self.build(doc, [
                {"key": "low", "name": "Low", "type": "group"},
                {"name": "L1", "parent": "$low"},
                {"key": "g", "name": "G", "type": "group", "pass_through": False},
                {"name": "U0", "parent": "$g"},
                {"name": "T", "parent": "$g"},
                {"name": "U1", "parent": "$g"},
                {"name": "Mid"},
                {"key": "g2", "name": "G2", "type": "group", "pass_through": True},
                {"name": "P1", "parent": "$g2"},
                {"name": "Top"},
            ], {"L1": ((0, 0, W, H), solid(W, H)),
                "U0": ((0, 0, W, H), solid(W, H)),
                "T": ((0, 0, 20, 5), solid(20, 5)),
                "U1": ((0, 0, 2, 5), solid(2, 5)),
                "Mid": ((2, 0, 2, 5), solid(2, 5)),
                "P1": ((4, 0, 2, 5), solid(2, 5)),
                "Top": ((6, 0, 2, 5), solid(2, 5))})
            result = self.assertCoverage(handle, "T", 8 / 20,
                                         above=[ids["Top"], ids["P1"], ids["Mid"], ids["U1"]])
            self.assertEqual(read_map(result["path"])[2], (b"\xff" * 8 + bytes(12)) * 5)

    def test_isolated_groups_count_as_one(self):
        # An isolated Normal group holding a Multiply layer hides what is
        # below it (its composite is the layer's pixels); an isolated
        # Multiply group holding a Normal layer only tints.
        with scratch_document(W, H, name="kmcp-occl-iso") as doc:
            handle, ids = self.build(doc, [
                {"name": "T"},
                {"key": "a", "name": "IsoN", "type": "group", "pass_through": False},
                {"name": "M", "parent": "$a", "blend_mode": "multiply"},
                {"key": "b", "name": "IsoM", "type": "group", "pass_through": False,
                 "blend_mode": "multiply"},
                {"name": "N", "parent": "$b"},
                {"key": "c", "name": "PT", "type": "group", "pass_through": True,
                 "blend_mode": "multiply"},
                {"name": "PN", "parent": "$c"},
                {"name": "PM", "parent": "$c", "blend_mode": "multiply"},
            ], {"T": ((0, 0, 10, 2), solid(10, 2)),
                "M": ((0, 0, 3, 2), solid(3, 2)),
                "N": ((3, 0, 3, 2), solid(3, 2)),
                "PN": ((6, 0, 2, 2), solid(2, 2)),
                "PM": ((8, 0, 2, 2), solid(2, 2))})
            # A pass-through group's own mode is ignored: its children count
            # one by one.
            self.assertCoverage(handle, "T", 5 / 10, above=[ids["PN"], ids["IsoN"]])
            self.assertCoverage(handle, "T", 1.0,
                                above=[ids["PM"], ids["PN"], ids["IsoM"], ids["IsoN"]],
                                modes=["normal", "multiply"])

    def test_clipped_layers(self):
        # Clipped layers directly above the target (and inside a
        # pass-through group above it) clip to a stack holding the target,
        # so they count with their own alpha. A clipped layer inside an
        # isolated group clips to that group's lower layers only.
        with scratch_document(W, H, name="kmcp-occl-clip") as doc:
            handle, ids = self.build(doc, [
                {"name": "T"},
                {"name": "X", "inherit_alpha": True},
                {"key": "p", "name": "PT", "type": "group", "pass_through": True},
                {"name": "PX", "parent": "$p", "inherit_alpha": True},
                {"key": "i", "name": "Iso", "type": "group", "pass_through": False},
                {"name": "Base", "parent": "$i"},
                {"name": "IX", "parent": "$i", "inherit_alpha": True},
            ], {"T": ((0, 0, 20, 2), solid(20, 2)),
                "X": ((0, 0, 3, 2), solid(3, 2, a=200)),
                "PX": ((3, 0, 3, 2), solid(3, 2)),
                "Base": ((6, 0, 2, 2), solid(2, 2)),
                "IX": ((6, 0, 10, 2), solid(10, 2))})
            result = self.assertCoverage(handle, "T", 8 / 20,
                                         above=[ids["Iso"], ids["PX"], ids["X"]],
                                         threshold=0.75)
            row = b"\xc8" * 3 + b"\xff" * 5 + bytes(12)
            self.assertEqual(read_map(result["path"])[2], row * 2)
            self.assertCoverage(handle, "T", 5 / 20, threshold=0.9)

    def test_pass_through_group_mask_is_ignored(self):
        # The map agrees with render(only=[PT]) when the pass-through group
        # above has a transparency mask (Krita ignores such a mask).
        with scratch_document(W, H, name="kmcp-occl-ptmask") as doc:
            handle, ids = self.build(doc, [
                {"name": "T"},
                {"key": "p", "name": "PT", "type": "group", "pass_through": True},
                {"name": "P1", "parent": "$p"},
            ], {"T": ((0, 0, 10, 4), solid(10, 4)),
                "P1": ((0, 0, 10, 4), solid(10, 4))})
            mask = doc.createTransparencyMask("Mask")
            nodes.node_by_id(doc, ids["PT"]).addChildNode(mask, None)
            mask.setPixelData((b"\xff" * 3 + b"\x40" * 3 + b"\x00" * 4) * 4, 0, 0, 10, 4)
            del mask
            settle(doc)
            path = os.path.join(self.root, "pt.png")
            self.assertOk(run("render", doc=handle, path=path, only=[ids["PT"]],
                              region={"x": 0, "y": 0, "w": 10, "h": 4}))
            image = QImage(path).convertToFormat(QImage.Format_ARGB32)
            expected = pixels.from_qimage(image)[0][3::4]
            result = self.assertCoverage(handle, "T", 1.0, above=[ids["P1"]], threshold=1.0)
            self.assertEqual(read_map(result["path"])[2], expected)

    def test_common_ancestor_props_dont_dim_coverage(self):
        # T and C share a half-opacity isolated Multiply group with inherit
        # alpha: those apply to both, so C still hides T fully.
        with scratch_document(W, H, name="kmcp-occl-anc") as doc:
            handle, ids = self.build(doc, [
                {"name": "Under"},
                {"key": "g", "name": "G", "type": "group", "pass_through": False,
                 "opacity": 0.5, "blend_mode": "multiply", "inherit_alpha": True},
                {"name": "T", "parent": "$g"},
                {"name": "C", "parent": "$g"},
            ], {"Under": ((0, 0, W, H), solid(W, H)),
                "T": ((0, 0, 6, 6), solid(6, 6)),
                "C": ((0, 0, 3, 6), solid(3, 6))})
            result = self.assertCoverage(handle, "T", 0.5, above=[ids["C"]], threshold=1.0)
            self.assertEqual(read_map(result["path"])[2], (b"\xff" * 3 + bytes(3)) * 6)


class EdgeTest(Base):
    def test_past_the_canvas_is_uncovered(self):
        with scratch_document(W, H, name="kmcp-occl-off") as doc:
            handle, ids = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                     {"T": ((-4, -1, 10, 5), solid(10, 5)),
                                      "C": ((-8, -8, W + 16, H + 16), solid(W + 16, H + 16))})
            result = self.occl(handle, "T")
            self.assertEqual(result["bounds"], {"x": -4, "y": -1, "w": 10, "h": 5})
            self.assertEqual((result["pixels"], result["off_canvas_px"], result["covered_px"]),
                             (50, 4 * 5 + 6, 24))
            self.assertEqual(result["covered_pct"], 24 / 50)
            self.assertIs(result["fully_hidden"], False)
            expected = bytes(10) + (bytes(4) + b"\xff" * 6) * 4
            self.assertEqual(read_map(result["path"])[2], expected)

    def test_empty_layer(self):
        with scratch_document(W, H, name="kmcp-occl-empty") as doc:
            handle, ids = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                     {"C": ((0, 0, W, H), solid(W, H))})
            result = self.occl(handle, "T", name="empty.png")
            self.assertEqual((result["path"], result["bounds"], result["covered_pct"],
                              result["fully_hidden"], result["pixels"]),
                             (None, None, None, False, 0))
            self.assertEqual(result["above"], {"count": 1, "ids": [ids["C"]]})
            self.assertFalse(os.path.exists(os.path.join(self.root, "empty.png")))

    def test_errors(self):
        with scratch_document(W, H, name="kmcp-occl-err") as doc:
            handle, ids = self.build(doc, [{"key": "g", "name": "G", "type": "group"},
                                           {"name": "T", "parent": "$g"}],
                                     {"T": ((0, 0, 4, 4), solid(4, 4))})
            png = os.path.join(self.root, "e.png")
            err = self.assertError(run("occlusion_map", doc=handle, id="G", path=png),
                                   "conflict")
            self.assertEqual(err["details"], {"id": ids["G"], "type": "grouplayer"})
            self.assertError(run("occlusion_map", doc=handle, id="Nope", path=png), "not_found")
            self.assertError(run("occlusion_map", doc=handle, path=png), "bad_request")
            self.assertError(run("occlusion_map", doc=handle, id="T",
                                 path=os.path.join(self.root, "e.jpg")), "bad_request")
            self.assertError(run("occlusion_map", doc=handle, id="T", path="/etc/x.png"),
                             "forbidden_path")
            for bad in ({"modes": []}, {"modes": "normal"}, {"modes": ["nrmal"]},
                        {"threshold": 0}, {"threshold": 1.5}, {"threshold": True},
                        {"png_level": 10}):
                self.assertError(run("occlusion_map", doc=handle, id="T", path=png, **bad),
                                 "bad_request")
            err = self.assertError(run("occlusion_map", doc=handle, id="T", path=png,
                                       modes=["nrmal"]), "bad_request")
            self.assertIn("normal", err["details"]["suggestions"])
            self.assertFalse(os.path.exists(png))

    def test_non_rgba8_is_conflict(self):
        with scratch_document(8, 8, name="kmcp-occl-u16", color_depth="U16") as doc:
            handle = registry.handle_for(doc)
            self.assertError(run("occlusion_map", doc=handle, id="Background",
                                 path=os.path.join(self.root, "u.png")), "conflict")

    def test_document_untouched(self):
        with scratch_document(W, H, name="kmcp-occl-same") as doc:
            handle, ids = self.build(doc, [
                {"key": "g", "name": "G", "type": "group", "opacity": 0.5,
                 "blend_mode": "multiply", "inherit_alpha": True},
                {"name": "T", "parent": "$g"},
                {"name": "C", "parent": "$g", "inherit_alpha": True},
                {"name": "M", "blend_mode": "multiply"},
                {"name": "Hid", "visible": False},
            ], {"T": ((0, 0, 6, 6), solid(6, 6)),
                "C": ((0, 0, 3, 6), solid(3, 6)),
                "M": ((0, 0, W, H), solid(W, H))})
            doc.setModified(False)
            settle(doc)
            before = run("list_layers", doc=handle)["result"]["layers"]
            proj = pixels.as_bytes(doc.pixelData(0, 0, W, H))
            open_docs = len(Krita.instance().documents())
            self.assertCoverage(handle, "T", 0.5, above=[ids["C"]])
            self.assertCoverage(handle, "T", 1.0, modes=["normal", "multiply"])
            self.assertEqual(len(Krita.instance().documents()), open_docs)
            self.assertEqual(run("list_layers", doc=handle)["result"]["layers"], before)
            self.assertIs(doc.modified(), False)
            settle(doc)
            self.assertEqual(pixels.as_bytes(doc.pixelData(0, 0, W, H)), proj)
            self.assertEqual(run("journal", doc=handle)["result"]["undo"], [])


if __name__ == "__main__":
    unittest.main()
