"""F005 K11: the painting tools (stroke, fill, draw_shape, clear) say what
they changed and whether it shows: changed, changed_px, layer_visible,
covered_pct (occlusion_map's coverage over the changed pixels), top_cover
and notes; the config's paint.check_visibility turns the check off.

Specs are listed bottom to top, as create_layers takes them; the white
Background createDocument adds is the bottom layer of every document.
"""

import unittest

from helpers import scratch_document, temp_config
from test_dispatch import run
from test_occlusion import Base, solid

W, H = 40, 30


class PaintReportTest(Base):
    def rect(self, handle, layer, x=4, y=5, w=6, h=4, **params):
        return self.assertOk(run("draw_shape", doc=handle, layer=layer, shape="rectangle",
                                 x=x, y=y, width=w, height=h, color="#00ff00", **params))

    def test_nothing_above(self):
        with scratch_document(W, H, name="kmcp-paint-free") as doc:
            handle, _ = self.build(doc, [{"name": "T"}], {})
            r = self.rect(handle, "T")
            self.assertEqual(r["changed"], {"x": 4, "y": 5, "w": 6, "h": 4})
            self.assertEqual((r["changed_px"], r["layer_visible"], r["covered_pct"]),
                             (24, True, 0.0))
            self.assertNotIn("notes", r)
            self.assertNotIn("top_cover", r)

    def test_fully_covered_names_the_top_cover(self):
        # The run's Choker repaint: the layer is covered completely at rest.
        with scratch_document(W, H, name="kmcp-paint-covered") as doc:
            handle, ids = self.build(doc, [{"name": "Choker"}, {"name": "Shirt"}, {"name": "Neck"}],
                                     {"Choker": ((0, 0, W, H), solid(W, H)),
                                      "Shirt": ((0, 0, 3, 3), solid(3, 3)),
                                      "Neck": ((2, 2, 20, 20), solid(20, 20, bgr=(9, 9, 9)))})
            r = self.rect(handle, "Choker")
            self.assertEqual((r["covered_pct"], r["layer_visible"]), (1.0, True))
            self.assertEqual(r["top_cover"], {"id": ids["Neck"], "path": "Neck"})
            (note,) = r["notes"]
            self.assertEqual(note["code"], "change_covered")
            self.assertEqual(note["message"], "this change isn't visible in the composite: 100% "
                                              "covered by layers above (top cover: 'Neck')")

    def test_partly_covered_has_no_note(self):
        with scratch_document(W, H, name="kmcp-paint-half") as doc:
            handle, _ = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                   {"C": ((0, 0, 7, H), solid(7, H))})
            r = self.rect(handle, "T", x=4, w=6)  # columns 4-9, 4-6 covered
            self.assertEqual(r["covered_pct"], 0.5)
            self.assertNotIn("notes", r)

    def test_half_opaque_cover_hardly_shows(self):
        with scratch_document(W, H, name="kmcp-paint-alpha") as doc:
            handle, _ = self.build(doc, [{"name": "T"}, {"name": "Veil"}],
                                   {"Veil": ((0, 0, W, H), solid(W, H, a=200))})
            r = self.rect(handle, "T")
            self.assertEqual(r["covered_pct"], 1.0)
            self.assertEqual(r["notes"][0]["code"], "change_covered")
            self.assertIn("hardly shows", r["notes"][0]["message"])

    def test_multiply_layer_above_does_not_cover(self):
        with scratch_document(W, H, name="kmcp-paint-mult") as doc:
            handle, _ = self.build(doc, [{"name": "T"}, {"name": "Shade", "blend_mode": "multiply"}],
                                   {"Shade": ((0, 0, W, H), solid(W, H))})
            self.assertEqual(self.rect(handle, "T")["covered_pct"], 0.0)

    def test_hidden_layer_and_hidden_group(self):
        with scratch_document(W, H, name="kmcp-paint-hidden") as doc:
            handle, _ = self.build(doc, [{"key": "g", "name": "Arm B", "type": "group",
                                          "visible": False},
                                         {"name": "Hand", "parent": "$g"},
                                         {"name": "Ghost", "visible": False}], {})
            r = self.rect(handle, "Ghost")
            self.assertEqual((r["layer_visible"], r["covered_pct"]), (False, None))
            self.assertEqual(r["notes"], [{"code": "layer_hidden", "message":
                                           "this change isn't visible in the composite: the layer "
                                           "is hidden"}])
            r = self.rect(handle, "Arm B/Hand")
            self.assertIs(r["layer_visible"], False)
            self.assertIn("the layer's group 'Arm B' is hidden", r["notes"][0]["message"])

    def test_same_pixels_change_nothing(self):
        with scratch_document(W, H, name="kmcp-paint-same") as doc:
            handle, _ = self.build(doc, [{"name": "T"}], {})
            self.rect(handle, "T")
            r = self.rect(handle, "T")
            self.assertEqual((r["changed"], r["changed_px"], r["covered_pct"]), (None, 0, None))
            self.assertEqual(r["notes"][0]["code"], "nothing_changed")
            r = self.rect(handle, "T", x=2, w=6)  # half new
            self.assertEqual((r["changed"], r["changed_px"]), ({"x": 2, "y": 5, "w": 2, "h": 4}, 8))

    def test_check_visibility_off(self):
        with scratch_document(W, H, name="kmcp-paint-off") as doc:
            handle, _ = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                   {"C": ((0, 0, W, H), solid(W, H))})
            with temp_config({"allowed_dirs": [self.root], "paint": {"check_visibility": False}}):
                r = self.rect(handle, "T")
            self.assertEqual((r["changed_px"], r["layer_visible"], r["covered_pct"]),
                             (24, True, None))
            self.assertNotIn("notes", r)
            self.assertNotIn("top_cover", r)

    def test_stroke_fill_clear_report_too(self):
        with scratch_document(W, H, name="kmcp-paint-tools") as doc:
            handle, ids = self.build(doc, [{"name": "T"}, {"name": "C"}],
                                     {"C": ((0, 0, W, H), solid(W, H))})
            stroke = self.assertOk(run("stroke", doc=handle, layer="T", points=[[5, 5], [15, 5]],
                                       size=4, color="#ff0000"))
            fill = self.assertOk(run("fill", doc=handle, layer="T", x=20, y=15, radius=4,
                                     color="#0000ff"))
            clear = self.assertOk(run("clear", doc=handle, layer="T", color="transparent"))
            for r in (stroke, fill, clear):
                self.assertGreater(r["changed_px"], 0, r)
                self.assertEqual(r["covered_pct"], 1.0, r)
                self.assertEqual(r["top_cover"]["id"], ids["C"])
                self.assertEqual(r["notes"][0]["code"], "change_covered")
            # Erasing changes exactly what the stroke and the fill painted.
            s, f = stroke["changed"], fill["changed"]
            x0, y0 = min(s["x"], f["x"]), min(s["y"], f["y"])
            x1, y1 = max(s["x"] + s["w"], f["x"] + f["w"]), max(s["y"] + s["h"], f["y"] + f["h"])
            self.assertEqual(clear["changed"], {"x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0})
            self.assertEqual(clear["changed_px"], stroke["changed_px"] + fill["changed_px"])

    def test_undo_still_restores(self):
        with scratch_document(W, H, name="kmcp-paint-undo") as doc:
            handle, _ = self.build(doc, [{"name": "T"}], {})
            self.rect(handle, "T")
            self.assertOk(run("undo", doc=handle))
            r = self.assertOk(run("get_color_at", doc=handle, layer="T", x=5, y=6))
            self.assertEqual(r["a"], 0)


if __name__ == "__main__":
    unittest.main()
