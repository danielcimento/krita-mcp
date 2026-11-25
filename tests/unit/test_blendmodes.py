"""blendmodes.py: the valid Krita blend-mode IDs (D17)."""

import importlib.util
import os

import pytest

from kritamcp import blendmodes

_TOOL = os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, "tools", "ora_to_manifest.py")
_spec = importlib.util.spec_from_file_location("ora_to_manifest_for_blendmodes", _TOOL)
ora_to_manifest = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ora_to_manifest)


@pytest.mark.parametrize("mode", [
    "normal", "multiply", "add", "linear_dodge", "screen", "overlay", "erase",
    "soft_light_svg", "luminize", "diff", "hard mix", "linear light", "destination-in",
    "lambert_lighting_gamma2.2", "heat_glow_freeze_reflect_hybrid",
])
def test_valid(mode):
    assert blendmodes.is_valid(mode)


@pytest.mark.parametrize("mode", [
    "not-a-mode", "", "Normal", "MULTIPLY", " normal", "normal ", "pass through", "undefined",
    "plus", "svg:multiply", None, 3, ["normal"],
])
def test_invalid(mode):
    assert not blendmodes.is_valid(mode)


def test_list_shape():
    assert len(blendmodes.IDS) == 138
    assert blendmodes.DEFAULT in blendmodes.IDS
    assert blendmodes.ADDITIVE == {"add", "linear_dodge"} <= blendmodes.IDS


def test_every_ora_import_mapping_is_valid():
    # The .ora converter's targets must all be accepted by create_layers.
    assert set(ora_to_manifest.BLEND_OPS.values()) <= blendmodes.IDS


def test_suggest():
    assert blendmodes.suggest("multipy")[0] == "multiply"
    assert "linear_dodge" in blendmodes.suggest("linear-dodge")
    assert blendmodes.suggest("Normal")[0] == "normal"
    assert blendmodes.suggest("zzzzzz") == []
    assert blendmodes.suggest(None) == []
