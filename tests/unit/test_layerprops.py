"""layerprops.py: shared layer-property validation for create/update."""

import math

import pytest

from kritamcp import layerprops
from kritamcp.errors import CommandError


def bad(fn, *args, **kwargs):
    with pytest.raises(CommandError) as cm:
        fn(*args, **kwargs)
    assert cm.value.code == "bad_request"
    return cm.value


def test_check_normalizes_in_order():
    out = layerprops.check({"locked": True, "opacity": 1, "name": "A", "blend_mode": "add",
                            "visible": False, "other": "ignored"})
    assert out == {"name": "A", "visible": False, "opacity": 1.0, "blend_mode": "add",
                   "locked": True}
    assert list(out) == ["name", "visible", "opacity", "blend_mode", "locked"]
    assert isinstance(out["opacity"], float)
    assert layerprops.check({}) == {}


@pytest.mark.parametrize("value", [0, 0.0, 0.5, 1, 1.0])
def test_opacity_ok(value):
    assert layerprops.check_opacity(value) == float(value)


@pytest.mark.parametrize("value", [-0.01, 1.01, 255, math.inf, -math.inf, math.nan, True,
                                   False, "0.5", None])
def test_opacity_bad(value):
    e = bad(layerprops.check_opacity, value)
    assert e.details["param"] == "opacity"


def test_opacity_to_krita():
    assert [layerprops.to_krita_opacity(v) for v in (0, 0.5, 1, 0.25, 0.002)] == [0, 128, 255, 64, 1]


@pytest.mark.parametrize("key", layerprops.BOOL_PROPS)
@pytest.mark.parametrize("value", [1, 0, "true", None, 1.0])
def test_bools_must_be_bools(key, value):
    e = bad(layerprops.check, {key: value}, "grouplayer")
    assert e.details["param"] == key and f"'{key}'" in e.message


def test_blend_mode():
    assert layerprops.check({"blend_mode": "linear_dodge"}) == {"blend_mode": "linear_dodge"}
    e = bad(layerprops.check, {"blend_mode": "multipy"})
    assert e.details["param"] == "blend_mode" and e.details["value"] == "multipy"
    assert "multiply" in e.details["suggestions"] and "did you mean 'multiply'" in e.message
    e = bad(layerprops.check, {"blend_mode": "not-a-mode"})
    assert "not a Krita blend-mode id" in e.message
    bad(layerprops.check, {"blend_mode": 3})


@pytest.mark.parametrize("name", ["A", "Eyeball L", "Head/Eyes", "$dollar", " spaced ", "顔",
                                  "x" * 255])
def test_name_ok(name):
    assert layerprops.check_name(name) == name


@pytest.mark.parametrize("name", ["", "x" * 256, "a\nb", "tab\there", "nul\x00", "del\x7f",
                                  "c1\x85", "\ud800", None, 3, ["a"]])
def test_name_bad(name):
    e = bad(layerprops.check_name, name)
    assert e.details["param"] == "name"


def test_pass_through_groups_only():
    assert layerprops.check({"pass_through": True}, "grouplayer") == {"pass_through": True}
    assert layerprops.check({"pass_through": False}) == {"pass_through": False}  # type unknown
    e = bad(layerprops.check, {"pass_through": True}, "paintlayer")
    assert e.details["param"] == "pass_through" and "only valid on groups" in e.message


class FakeNode:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        if not name.startswith("set"):
            raise AttributeError(name)
        return lambda v: self.calls.append((name, v))


ALL = {"name": "N", "visible": False, "opacity": 0.5, "blend_mode": "add", "locked": True,
       "alpha_locked": True, "inherit_alpha": True, "pass_through": True}


def test_apply_sets_everything_locked_last():
    node = FakeNode()
    layerprops.apply(node, ALL)
    assert node.calls == [("setName", "N"), ("setVisible", False), ("setOpacity", 128),
                          ("setBlendingMode", "add"), ("setInheritAlpha", True),
                          ("setAlphaLocked", True), ("setPassThroughMode", True),
                          ("setLocked", True)]


def test_apply_without_lock_then_lock():
    node = FakeNode()
    layerprops.apply(node, ALL, locked=False)
    assert ("setLocked", True) not in node.calls
    layerprops.apply_locked(node, ALL)
    assert node.calls[-1] == ("setLocked", True)
    node = FakeNode()
    layerprops.apply_locked(node, {"visible": True})
    assert node.calls == []
