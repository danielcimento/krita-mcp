"""kritamcp/renderplan.py: region clipping, output size and only/hide
visibility over a fake node index."""

import pytest

from kritamcp import renderplan
from kritamcp.errors import CommandError
from kritamcp.refs import NodeIndex


def uid(n):
    return f"{{{n:08x}-0000-4000-8000-{n:012x}}}"


TOP, G, H, L, L2, M, K, A, B, BOTTOM = (uid(n) for n in range(1, 11))
# Top; G > [H > [L, L2], M]; K > [A, B]; Bottom (top to bottom).
INDEX = NodeIndex([
    {"id": TOP, "name": "Top", "parent": None, "type": "paintlayer"},
    {"id": G, "name": "G", "parent": None, "type": "grouplayer"},
    {"id": H, "name": "H", "parent": G, "type": "grouplayer"},
    {"id": L, "name": "L", "parent": H, "type": "paintlayer"},
    {"id": L2, "name": "L2", "parent": H, "type": "paintlayer"},
    {"id": M, "name": "M", "parent": G, "type": "paintlayer"},
    {"id": K, "name": "K", "parent": None, "type": "grouplayer"},
    {"id": A, "name": "A", "parent": K, "type": "paintlayer"},
    {"id": B, "name": "B", "parent": K, "type": "paintlayer"},
    {"id": BOTTOM, "name": "Bottom", "parent": None, "type": "paintlayer"},
])
HIDDEN = {G, L2, B}
VISIBLE = {nid: nid not in HIDDEN for nid in INDEX}
LIMITS = {"max_canvas_dim": 16384, "max_canvas_pixels": 1000}


def changes(only=None, hide=None, visible=VISIBLE):
    return renderplan.visibility_changes(INDEX, visible, only, hide)


def test_clip_region():
    assert renderplan.clip_region(None, 40, 30) == (0, 0, 40, 30)
    assert renderplan.clip_region({"x": 1, "y": 2, "w": 3, "h": 4}, 40, 30) == (1, 2, 3, 4)
    assert renderplan.clip_region({"x": -5, "y": 25, "w": 10, "h": 10}, 40, 30) == (0, 25, 5, 5)
    assert renderplan.clip_region({"x": -5, "y": -5, "w": 100, "h": 100}, 40, 30) == (0, 0, 40, 30)
    assert renderplan.clip_region({"x": 39, "y": 29, "w": 9, "h": 9}, 40, 30) == (39, 29, 1, 1)


@pytest.mark.parametrize("region", [
    {"x": 0, "y": 0, "w": 0, "h": 1}, {"x": 0, "y": 0, "w": 1, "h": -2},
    {"x": 40, "y": 0, "w": 5, "h": 5}, {"x": 0, "y": -5, "w": 5, "h": 5},
    {"x": 0, "y": 0, "w": 1}, {"x": 0, "y": 0, "w": 1, "h": 1, "z": 0},
    {"x": 0.0, "y": 0, "w": 1, "h": 1}, {"x": False, "y": 0, "w": 1, "h": 1},
    {"x": 10 ** 9, "y": 0, "w": 1, "h": 1}, [0, 0, 1, 1], "all"])
def test_clip_region_rejects(region):
    with pytest.raises(CommandError) as e:
        renderplan.clip_region(region, 40, 30)
    assert e.value.code == "bad_request"
    assert e.value.details["param"] == "region"


def test_scale_param():
    assert renderplan.scale_param({}) == 1.0
    assert renderplan.scale_param({"scale": None}) == 1.0
    assert renderplan.scale_param({"scale": 4}) == 4.0
    assert renderplan.scale_param({"scale": 0.001}) == 0.001
    for bad in (0, -0.5, 4.0001, True, "1", float("inf"), float("nan")):
        with pytest.raises(CommandError) as e:
            renderplan.scale_param({"scale": bad})
        assert (e.value.code, e.value.details["param"]) == ("bad_request", "scale")


def test_output_size():
    assert renderplan.output_size(20, 10, 1.0, LIMITS) == (20, 10)
    assert renderplan.output_size(20, 10, 0.5, LIMITS) == (10, 5)
    assert renderplan.output_size(5, 3, 0.5, LIMITS) == (3, 2)  # 2.5 x 1.5, halves up
    assert renderplan.output_size(5, 3, 0.001, LIMITS) == (1, 1)
    assert renderplan.output_size(10, 10, 3.1, LIMITS) == (31, 31)  # 961 pixels
    with pytest.raises(CommandError) as e:
        renderplan.output_size(40, 30, 1.0, LIMITS)
    assert e.value.code == "too_large"
    assert e.value.details == {"limit": "max_canvas_pixels", "max": 1000, "pixels": 1200,
                               "w": 40, "h": 30}


def test_output_size_limit_is_the_output_not_the_region():
    assert renderplan.output_size(40, 30, 0.5, LIMITS) == (20, 15)
    with pytest.raises(CommandError):
        renderplan.output_size(16, 16, 2, LIMITS)  # 1024 pixels


def test_no_subset_changes_nothing():
    assert changes() == {}


def test_only_nested_layer():
    # L and its ancestors shown (G was hidden); the siblings of L, H and G
    # (outside every listed subtree) hidden; nodes under hidden ones untouched.
    assert changes(only=[L]) == {G: True, TOP: False, K: False, BOTTOM: False, M: False}


def test_only_hidden_layer_is_shown():
    assert changes(only=[L2]) == {G: True, L2: True, L: False, M: False, TOP: False, K: False,
                                  BOTTOM: False}


def test_only_group_keeps_descendants():
    assert changes(only=[K]) == {TOP: False, BOTTOM: False}  # B stays hidden; G already hidden
    assert changes(only=[G]) == {G: True, TOP: False, K: False, BOTTOM: False}


def test_only_layer_inside_only_group():
    # G's other descendants keep their state even though L is listed too.
    assert changes(only=[G, L]) == {G: True, TOP: False, K: False, BOTTOM: False}
    all_hidden = {nid: False for nid in INDEX}
    assert changes(only=[G, L], visible=all_hidden) == {G: True, H: True, L: True}


def test_hide_and_both():
    assert changes(hide=[TOP]) == {TOP: False}
    assert changes(hide=[B]) == {}  # already hidden
    assert changes(only=[K, L], hide=[A]) == {G: True, TOP: False, BOTTOM: False, M: False,
                                              A: False}
    assert changes(only=[TOP], hide=[TOP]) == {K: False, BOTTOM: False, TOP: False}


def test_ancestor_hidden_by_hide():
    assert changes(only=[L], hide=[H]) == {G: True, H: False, TOP: False, K: False,
                                           BOTTOM: False, M: False}
