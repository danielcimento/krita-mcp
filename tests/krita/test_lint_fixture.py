"""The lint fixture (P4.3) and the ``lint`` command (P4.4), headless.

- The committed ``tests/fixtures/lint_four_findings.kra`` (acceptance
  § Fixtures, Lint fixture) is opened from a temp copy and linted:
  exactly acceptance test 4's four findings. Its snapshot must equal the
  hand-written twin ``tests/unit/snapshots/lint_four_findings.json``
  modulo ids, so the twin can't drift from the real document.
- ``build_lint_fixture.build`` into a temp dir gives the same, so the
  builder stays correct. Tests never rebuild the committed file.
- ``lint``: rule selection, ``severity_min``, sort order, settings from a
  config file, errors.
- A layer style with a visible effect (krita-api-notes § Nodes).
"""

import collections
import json
import os
import shutil
import tempfile
import unittest

from helpers import add_paint_layer, scratch_document, settle, temp_config
from kritamcp import lint, registry
from kritamcp.commands import analysis
from kritamcp.errors import CommandError
from test_dispatch import EnvelopeAssertions, run

import build_lint_fixture

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FIXTURE = os.path.join(REPO, "tests", "fixtures", "lint_four_findings.kra")
TWIN = os.path.join(REPO, "tests", "unit", "snapshots", "lint_four_findings.json")

EXPECTED = collections.Counter([("node-type", "Blur"), ("inherit-alpha", "Shade"),
                                ("duplicate-name", "Back/Hair"), ("empty-layer", "Spare")])
DEFAULT_RULES = lint.set_members("default")  # what runs with the Live2D pack on
CORE_DEFAULT = [r for r in DEFAULT_RULES if lint.RULES[r].pack == "core"]
LIVE2D = {"live2d": {"enabled": True}}  # F004: the conventions are opt-in


def live2d_settings(**knobs):
    """The full lint settings with the Live2D pack on."""
    s = lint.default_settings()
    s["live2d"].update(enabled=True, **knobs)
    return s


def without_ids(snapshot):
    """``snapshot`` with node ids replaced by paths (``parent`` too), and
    without the twin's ``description``."""
    paths = {n["id"]: n["path"] for n in snapshot["nodes"]}
    nodes = []
    for n in snapshot["nodes"]:
        n = dict(n)
        del n["id"]
        n["parent"] = paths.get(n["parent"])
        nodes.append(n)
    return {"document": snapshot["document"], "settings": snapshot["settings"], "nodes": nodes}


def pairs(findings):
    return collections.Counter((f["rule"], f["path"]) for f in findings)


class _OpenFixture:
    """Copy ``source`` into a temp dir made the only extra allowed root,
    ``open_file`` it; ``handle`` is the document. Closed and removed on
    exit."""

    def __init__(self, test, source):
        self.test, self.source = test, source

    def __enter__(self):
        self.tmp = tempfile.mkdtemp(prefix="kmcp-lint-fixture-")
        self.config = temp_config({"allowed_dirs": [self.tmp], **LIVE2D})
        self.config.__enter__()
        self.handle = None
        try:
            copy = os.path.join(self.tmp, "lint_four_findings.kra")
            shutil.copyfile(self.source, copy)
            self.handle = self.test.assertOk(run("open_file", path=copy))["doc"]
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *exc):
        try:
            if self.handle is not None:
                try:
                    registry.lookup(self.handle)
                    run("close_document", doc=self.handle, discard=True)
                except CommandError:
                    pass
        finally:
            self.config.__exit__(None, None, None)
            shutil.rmtree(self.tmp, ignore_errors=True)


class LintFixtureTest(EnvelopeAssertions, unittest.TestCase):
    maxDiff = None

    def assertSameAsTwin(self, snapshot):
        with open(TWIN, encoding="utf-8") as f:
            twin = without_ids(json.load(f))
        got = without_ids(snapshot)
        self.assertEqual(got["document"], twin["document"])
        # The twin stores only the stray-alpha settings; the rest are defaults.
        self.assertEqual({k: got["settings"][k] for k in twin["settings"]}, twin["settings"])
        self.assertEqual([n["path"] for n in got["nodes"]], [n["path"] for n in twin["nodes"]])
        for mine, theirs in zip(got["nodes"], twin["nodes"]):
            self.assertEqual(mine, theirs, mine["path"])

    def check_fixture(self, source):
        """Lint ``source`` through the command and directly; returns the
        snapshot."""
        with _OpenFixture(self, source) as fx:
            result = self.assertOk(run("lint", doc=fx.handle))
            doc = registry.lookup(fx.handle)
            snapshot = analysis.collect_snapshot(doc)
            del doc
        direct = lint.run(snapshot)
        for got in (result, direct):
            self.assertEqual(pairs(got["findings"]), EXPECTED, got["findings"])
            self.assertEqual(got["counts"], {"error": 3, "warning": 1, "info": 0})
            self.assertEqual(got["rules_run"], DEFAULT_RULES)
        self.assertEqual(result["findings"], direct["findings"])
        hairs = [n["id"] for n in snapshot["nodes"] if n["name"] == "Hair"]
        self.assertEqual(len(hairs), 2)
        dup = next(f for f in result["findings"] if f["rule"] == "duplicate-name")
        self.assertEqual(dup["related_ids"], hairs)
        self.assertEqual(dup["layer_id"], hairs[1])  # Back/Hair, the second in stacking order
        # Sorted by stacking order: Blur, Back/Hair, Shade, Spare.
        self.assertEqual([f["path"] for f in result["findings"]],
                         ["Blur", "Back/Hair", "Shade", "Spare"])
        self.assertEqual(result["settings"], live2d_settings())
        self.assertEqual(result["rules_off"], {})
        self.assertIs(result["pixel_stats"], True)
        return snapshot

    def test_committed_fixture(self):
        self.assertTrue(os.path.isfile(FIXTURE), FIXTURE)
        self.assertSameAsTwin(self.check_fixture(FIXTURE))

    def test_fresh_build(self):
        tmp = tempfile.mkdtemp(prefix="kmcp-lint-build-")
        try:
            path = os.path.join(tmp, "fresh.kra")
            size = build_lint_fixture.build(path)
            self.assertEqual(size, os.path.getsize(path))
            self.assertEqual(os.listdir(tmp), ["fresh.kra"])  # no backup or temp files
            fresh = self.check_fixture(path)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        self.assertSameAsTwin(fresh)


class LintCommandTest(EnvelopeAssertions, unittest.TestCase):
    def _doc(self, doc):
        """Fill ``doc`` (RGBA/U8) with one error and three warnings, top to
        bottom: an empty layer, an inherit-alpha layer ``Eye L``, ``Eye``
        (no side token next to ``Eye L``) and ``Body`` (2 px past the left
        edge)."""
        root = doc.rootNode()
        for child in root.childNodes():
            child.remove()
        body = doc.createNode("Body", "paintlayer")
        root.addChildNode(body, None)
        self.assertTrue(body.setPixelData(bytes((0, 0, 0, 255)) * 16, -2, 0, 4, 4))
        eye = doc.createNode("Eye", "paintlayer")
        root.addChildNode(eye, None)
        self.assertTrue(eye.setPixelData(bytes((0, 0, 0, 255)) * 4, 4, 4, 2, 2))
        eye_l = doc.createNode("Eye L", "paintlayer")
        root.addChildNode(eye_l, None)
        self.assertTrue(eye_l.setPixelData(bytes((0, 0, 0, 255)) * 4, 8, 4, 2, 2))
        eye_l.setInheritAlpha(True)
        add_paint_layer(doc, "Spare")
        settle(doc)

    def test_live2d_off_by_default(self):
        """F004: without live2d.enabled only the core rules run by default,
        and rules_off names the rest; a Live2D rule named by id still runs."""
        with scratch_document(16, 16) as doc:
            self._doc(doc)
            handle = registry.handle_for(doc)
            default = self.assertOk(run("lint", doc=handle))
            self.assertEqual(default["rules_run"], CORE_DEFAULT)
            self.assertEqual(default["rules_off"],
                             {"live2d": [r for r in DEFAULT_RULES if r not in CORE_DEFAULT]})
            self.assertEqual([(f["rule"], f["path"]) for f in default["findings"]],
                             [("empty-layer", "Spare"), ("out-of-canvas", "Body")])
            self.assertIs(default["settings"]["live2d"]["enabled"], False)
            by_id = self.assertOk(run("lint", doc=handle, rules=["inherit-alpha"]))
            self.assertEqual([(f["rule"], f["path"]) for f in by_id["findings"]],
                             [("inherit-alpha", "Eye L")])
            with temp_config({"lint": {"disable": ["empty-layer"]}}):
                fewer = self.assertOk(run("lint", doc=handle))
            self.assertEqual(fewer["rules_off"]["disabled"], ["empty-layer"])
            self.assertEqual([f["rule"] for f in fewer["findings"]], ["out-of-canvas"])

    def test_selection_severity_and_sorting(self):
        with scratch_document(16, 16) as doc, temp_config(LIVE2D):
            self._doc(doc)
            handle = registry.handle_for(doc)
            default = self.assertOk(run("lint", doc=handle))
            # Top to bottom: Spare, Eye L, Eye, Body; ties by rule id.
            self.assertEqual([(f["rule"], f["path"]) for f in default["findings"]], [
                ("empty-layer", "Spare"), ("inherit-alpha", "Eye L"), ("side-token", "Eye"),
                ("out-of-canvas", "Body")])
            self.assertEqual(default["counts"], {"error": 1, "warning": 3, "info": 0})
            self.assertEqual(default["rules_run"], DEFAULT_RULES)

            errors = self.assertOk(run("lint", doc=handle, severity_min="error"))
            self.assertEqual([f["rule"] for f in errors["findings"]], ["inherit-alpha"])
            self.assertEqual(errors["counts"], {"error": 1, "warning": 0, "info": 0})
            self.assertEqual(errors["rules_run"], DEFAULT_RULES)

            some = self.assertOk(run("lint", doc=handle, rules=["out-of-canvas", "empty-layer"]))
            self.assertEqual([f["rule"] for f in some["findings"]], ["empty-layer", "out-of-canvas"])
            self.assertEqual(some["rules_run"], ["empty-layer", "out-of-canvas"])  # registry order

            got = self.assertOk(run("lint", doc=handle, rules=["default", "node-type"]))
            self.assertEqual(got["rules_run"], DEFAULT_RULES)
            self.assertEqual(got["findings"], default["findings"])
            # Conventions (P5.9): Eye is the clipping base of Eye L, without " Mask".
            conventions = self.assertOk(run("lint", doc=handle, rules=["conventions"]))
            self.assertEqual(conventions["rules_run"], lint.set_members("conventions"))
            self.assertEqual([(f["rule"], f["path"]) for f in conventions["findings"]],
                             [("mask-suffix", "Eye")])
            for names in (["all"], ["default", "conventions"]):
                got = self.assertOk(run("lint", doc=handle, rules=names))
                self.assertEqual(got["rules_run"], lint.set_members("all"), names)
                self.assertEqual([(f["rule"], f["path"]) for f in got["findings"]], [
                    ("empty-layer", "Spare"), ("inherit-alpha", "Eye L"), ("mask-suffix", "Eye"),
                    ("side-token", "Eye"), ("out-of-canvas", "Body")], names)
                self.assertEqual(got["counts"], {"error": 1, "warning": 3, "info": 1}, names)

            unknown = self.assertError(run("lint", doc=handle, rules=["node-type", "bogus"]),
                                       "bad_request")
            self.assertIn("'bogus'", unknown["message"])
            self.assertEqual(unknown["details"]["unknown"], ["bogus"])
            for bad in ([], "default", [3]):
                self.assertError(run("lint", doc=handle, rules=bad), "bad_request")
            for bad in ("fatal", "Error", 2):
                self.assertError(run("lint", doc=handle, severity_min=bad), "bad_request")
        self.assertError(run("lint", doc="doc-999999"), "not_found")

    def test_bad_args_fail_before_the_document_is_resolved(self):
        self.assertError(run("lint", doc="doc-999999", rules=["bogus"]), "bad_request")

    def test_document_level_finding_sorts_first(self):
        with scratch_document(16, 16, color_depth="U16") as doc, temp_config(LIVE2D):
            add_paint_layer(doc, "Spare").setInheritAlpha(True)
            result = self.assertOk(run("lint", doc=registry.handle_for(doc)))
        self.assertIs(result["pixel_stats"], False)
        # No alpha stats on U16, so empty-layer can't fire on Spare.
        self.assertEqual([(f["rule"], f["path"]) for f in result["findings"]],
                         [("color-space", None), ("inherit-alpha", "Spare")])
        self.assertIsNone(result["findings"][0]["layer_id"])

    def test_settings_from_config(self):
        cases = (LIVE2D, {"live2d": {"enabled": True, "additive_mode": "add"}},
                 {"live2d": {"enabled": True, "additive_mode": "linear_dodge"}},
                 {"lint": {"additive_mode": "add"}, **LIVE2D})  # F003's place: still read
        with scratch_document(16, 16) as doc:
            layer = add_paint_layer(doc, "Glow")
            self.assertTrue(layer.setPixelData(bytes((0, 0, 0, 255)) * 4, 0, 0, 2, 2))
            layer.setBlendingMode("linear_dodge")
            handle = registry.handle_for(doc)
            settle(doc)
            got = {}
            for data in cases:
                with temp_config(data):
                    got[json.dumps(data)] = self.assertOk(run("lint", doc=handle))
            del layer
        plain, add, dodge, legacy = (got[json.dumps(d)] for d in cases)
        self.assertEqual(plain["settings"]["live2d"]["additive_mode"], "either")
        self.assertEqual(pairs(plain["findings"]), collections.Counter())
        self.assertEqual(add["settings"], live2d_settings(additive_mode="add"))
        self.assertEqual(legacy["settings"], add["settings"])
        self.assertEqual(legacy["findings"], add["findings"])
        self.assertEqual(pairs(add["findings"]), collections.Counter([("blend-mode", "Glow")]))
        self.assertIn("'add'", add["findings"][0]["message"])
        self.assertEqual(pairs(dodge["findings"]), collections.Counter())

    def test_stray_thresholds_from_config(self):
        with scratch_document(32, 32) as doc:
            layer = add_paint_layer(doc, "Speck")
            # An opaque 4x4 block at (0, 0) and an alpha-3 pixel at (10, 0).
            self.assertTrue(layer.setPixelData(bytes((0, 0, 0, 255)) * 16, 0, 0, 4, 4))
            self.assertTrue(layer.setPixelData(bytes((0, 0, 0, 3)), 10, 0, 1, 1))
            settle(doc)
            handle = registry.handle_for(doc)
            default = self.assertOk(run("lint", doc=handle))
            with temp_config({"lint": {"stray_alpha_distance": 8}}):
                near = self.assertOk(run("lint", doc=handle))
            with temp_config({"lint": {"stray_alpha_below": 2}}):
                strong = self.assertOk(run("lint", doc=handle))
            del layer
        self.assertEqual(pairs(default["findings"]), collections.Counter([("stray-alpha", "Speck")]))
        self.assertIn("has 1 stray speck, 1 px, at ", default["findings"][0]["message"])
        self.assertIn("at (10, 0) 1x1", default["findings"][0]["message"])
        self.assertEqual(near["settings"]["stray_alpha_distance"], 8)
        self.assertEqual(pairs(near["findings"]), collections.Counter())
        self.assertEqual(pairs(strong["findings"]), collections.Counter())


# A layer style in the PSD-XML form ``layerStyleToAsl`` writes: a red colour
# overlay (SoFi) and a red drop shadow (DrSh) 4 px straight down, no blur.
# Krita keeps both and renders them (2026-11-25, krita-api-notes § Nodes).
_RED = ('<node type="Descriptor" key="Clr " name="" classId="RGBC">'
        '<node type="Double" key="Rd  " value="255"/><node type="Double" key="Grn " value="0"/>'
        '<node type="Double" key="Bl  " value="0"/></node>')
STYLE_ASL = (
    '<asl><node type="Descriptor" name="" classId="null">'
    '<node type="UnitFloat" unit="#Prc" key="Scl " value="100"/>'
    '<node type="Boolean" key="masterFXSwitch" value="1"/>'
    '<node type="Descriptor" key="SoFi" name="" classId="SoFi">'
    '<node type="Boolean" key="enab" value="1"/>'
    '<node type="Enum" key="Md  " typeId="BlnM" value="Nrml"/>' + _RED +
    '<node type="UnitFloat" unit="#Prc" key="Opct" value="100"/></node>'
    '<node type="Descriptor" key="DrSh" name="" classId="DrSh">'
    '<node type="Boolean" key="enab" value="1"/>'
    '<node type="Enum" key="Md  " typeId="BlnM" value="Nrml"/>' + _RED +
    '<node type="UnitFloat" unit="#Prc" key="Opct" value="100"/>'
    '<node type="Boolean" key="uglg" value="0"/>'
    '<node type="UnitFloat" unit="#Ang" key="lagl" value="90"/>'
    '<node type="UnitFloat" unit="#Pxl" key="Dstn" value="4"/>'
    '<node type="UnitFloat" unit="#Pxl" key="Ckmt" value="0"/>'
    '<node type="UnitFloat" unit="#Pxl" key="blur" value="0"/>'
    '</node></node></asl>')


def _pixel(data, w, x, y):
    return tuple(data[4 * (y * w + x):4 * (y * w + x) + 4])


class LayerStyleTest(EnvelopeAssertions, unittest.TestCase):
    """The positive layer-style case: a style Krita keeps and renders, in
    the open document and after a .kra round trip."""

    def test_visible_style_is_a_finding(self):
        from kritamcp import fileio, pixels

        tmp = tempfile.mkdtemp(prefix="kmcp-lint-style-")
        handle = None
        try:
            with scratch_document(32, 32) as doc:
                for child in doc.rootNode().childNodes():
                    child.remove()
                styled = doc.createNode("Styled", "paintlayer")
                doc.rootNode().addChildNode(styled, None)
                green = bytes((0, 255, 0, 255))
                self.assertTrue(styled.setPixelData(green * 64, 8, 8, 8, 8))  # (8, 8) to (15, 15)
                plain = add_paint_layer(doc, "Plain")
                self.assertTrue(plain.setPixelData(green, 30, 30, 1, 1))
                settle(doc)
                before = pixels.as_bytes(doc.pixelData(0, 0, 32, 32))
                self.assertTrue(styled.setLayerStyleFromAsl(STYLE_ASL))
                settle(doc)
                kept = styled.layerStyleToAsl()
                after = pixels.as_bytes(doc.pixelData(0, 0, 32, 32))
                own = pixels.as_bytes(styled.pixelData(8, 8, 8, 8))
                self.assertEqual(styled.bounds().getRect(), (8, 8, 8, 8))  # the style isn't in bounds
                with temp_config(LIVE2D):  # layer-style is a Live2D-convention rule
                    result = self.assertOk(run("lint", doc=registry.handle_for(doc)))
                kra = os.path.join(tmp, "styled.kra")
                fileio.save_as(doc, kra)
                del styled, plain
            for key in ("SoFi", "DrSh"):
                self.assertIn(f'key="{key}"', kept)
            self.assertEqual(own, green * 64)  # the layer's own pixels are unchanged
            self.assertEqual(_pixel(before, 32, 10, 10), (0, 255, 0, 255))
            self.assertEqual(_pixel(after, 32, 10, 10), (0, 0, 255, 255))  # red overlay (BGRA)
            self.assertEqual(_pixel(before, 32, 10, 18), (0, 0, 0, 0))
            self.assertEqual(_pixel(after, 32, 10, 18)[:3], (0, 0, 255))  # the shadow, 4 px down
            self.assertGreater(_pixel(after, 32, 10, 18)[3], 200)
            self.assertEqual(_pixel(after, 32, 10, 21), (0, 0, 0, 0))
            self.assertEqual(pairs(result["findings"]),
                             collections.Counter([("layer-style", "Styled")]))

            with temp_config({"allowed_dirs": [tmp], **LIVE2D}):
                handle = self.assertOk(run("open_file", path=kra))["doc"]
                reopened = self.assertOk(run("lint", doc=handle))
                self.assertOk(run("close_document", doc=handle, discard=True))
                handle = None
            self.assertEqual(pairs(reopened["findings"]),
                             collections.Counter([("layer-style", "Styled")]))
        finally:
            if handle is not None:
                try:
                    run("close_document", doc=handle, discard=True)
                except Exception:
                    pass
            shutil.rmtree(tmp, ignore_errors=True)
