"""kritamcp.transformplan: transform_layer's parameters and geometry (no Krita).

The geometry matches what Krita did headless (krita-api-notes § Nodes,
Transforms); tests/krita/test_transform.py checks the real calls."""

import pytest

from kritamcp import config, transformplan as tp
from kritamcp.errors import CommandError


def bad(params):
    with pytest.raises(CommandError) as e:
        tp.parse(params)
    assert e.value.code == "bad_request"
    return e.value


def test_parse_defaults_and_normalization():
    spec = tp.parse({"flip": "h"})
    assert (spec.flip, spec.pivot, spec.resample) == ("h", "center", "Bicubic")
    assert spec.pixel_steps
    spec = tp.parse({"scale": 2, "rotate": -90, "translate": [3, -2], "pivot": [1, 2.5],
                     "resample": "nearestneighbor"})
    assert spec.scale == (2.0, 2.0) and spec.rotate == -90.0 and spec.turn == 270.0
    assert spec.translate == (3, -2) and spec.pivot == (1.0, 2.5)
    assert spec.resample == "NearestNeighbor"
    assert tp.parse({"scale": [2, 0.5]}).scale == (2.0, 0.5)
    assert not tp.parse({"translate": [1, 0]}).pixel_steps
    # Steps that change nothing are dropped (the call is then a no-op).
    spec = tp.parse({"translate": [0, 0], "scale": 1, "rotate": 720})
    assert (spec.translate, spec.scale, spec.rotate) == (None, None, None)
    assert tp.parse({"flip": "v", "pivot": None, "resample": None}).pivot == "center"


@pytest.mark.parametrize("params, param", [
    ({}, "translate"),
    ({"translate": [1.5, 0]}, "translate"),
    ({"translate": [True, 0]}, "translate"),
    ({"translate": [1, 2, 3]}, "translate"),
    ({"translate": [config.COORD + 1, 0]}, "translate"),
    ({"scale": 0}, "scale"),
    ({"scale": -2}, "scale"),
    ({"scale": [2, 101]}, "scale"),
    ({"scale": "2"}, "scale"),
    ({"scale": float("nan")}, "scale"),
    ({"rotate": 3601}, "rotate"),
    ({"rotate": float("inf")}, "rotate"),
    ({"rotate": True}, "rotate"),
    ({"flip": "x"}, "flip"),
    ({"flip": "h", "pivot": "middle"}, "pivot"),
    ({"flip": "h", "pivot": [1]}, "pivot"),
    ({"flip": "h", "resample": "Box"}, "resample"),
])
def test_parse_errors_name_the_param(params, param):
    assert bad(params).details["param"] == param


def test_flip():
    r = (10, 6, 8, 4)
    assert tp.flip_axis2(r, "h", None) == 28 and tp.flip_axis2(r, "v", None) == 16
    assert tp.flipped(r, "h", 28) == r and tp.flipped(r, "v", 16) == r
    # About x = 20: the layer lands at 2 * 20 - 18 = 22.
    assert tp.flipped(r, "h", tp.flip_axis2(r, "h", (20, 0))) == (22, 6, 8, 4)
    assert tp.flip_axis2(r, "h", (20.3, 0)) == 41  # rounded to the nearest half pixel
    assert tp.flipped(r, "v", tp.flip_axis2(r, "v", (0, 0))) == (10, -10, 8, 4)


def test_scale_matches_krita():
    # Probe values (headless, 2026-11-25): (10, 6, 8, 4) x2 about its centre
    # (14, 8) -> (6, 4, 16, 8); x0.5 about the top-left -> (10, 6, 4, 2).
    r = (10, 6, 8, 4)
    assert tp.scaled_size(8, 4, 2, 2) == (16, 8)
    assert tp.scaled(r, tp.center(r), (16, 8)) == (6, 4, 16, 8)
    assert tp.scaled(r, (10, 6), tp.scaled_size(8, 4, 0.5, 0.5)) == (10, 6, 4, 2)
    # A group (28, 29, 28, 16) whose child (30, 30, 4, 4) went x2 about (32, 31) to (28, 29, 8, 8).
    assert tp.scaled((30, 30, 4, 4), (32, 31), (8, 8)) == (28, 29, 8, 8)
    assert tp.scaled_size(3, 1, 0.1, 0.1) == (1, 1)


def test_rotate_matches_krita():
    # Probe values: quarter turns about the bounds' centre, half pixels rounded up.
    assert tp.rotated((10, 6, 8, 8), 90) == (10, 6, 8, 8)
    assert tp.rotated((10, 6, 8, 4), 90) == (12, 4, 4, 8)
    assert tp.rotated((10, 6, 9, 4), 90) == (13, 4, 4, 9)
    assert tp.rotated((100, 50, 20, 10), 90) == (105, 45, 10, 20)  # spike Q6
    assert tp.rotated((10, 6, 8, 4), 180) == (10, 6, 8, 4)
    assert tp.rotated((10, 6, 8, 4), 270) == (12, 4, 4, 8)
    assert tp.rotated((10, 6, 8, 8), 30) == (8, 4, 12, 12)


def test_rotation_shift_is_clockwise():
    r = (0, 0, 10, 10)  # centre (5, 5)
    assert tp.rotation_shift(r, (5, 5), 37) == (0, 0)
    # A quarter turn clockwise about (0, 5) instead of (5, 5): the centre
    # goes from (5, 5) to (0, 10), i.e. a shift of (-5, 5).
    assert tp.rotation_shift(r, (0, 5), 90) == (-5, 5)
    assert tp.rotation_shift(r, (0, 5), 270) == (-5, -5)
    assert tp.rotation_shift(r, (0, 5), 180) == (-10, 0)


def test_predict_chains_the_steps_in_order():
    spec = tp.parse({"flip": "h", "scale": 2, "rotate": 90, "translate": [3, -2]})
    steps, final = tp.predict((10, 6, 8, 4), spec)
    assert [s["op"] for s in steps] == ["flip", "scale", "rotate", "translate"]
    assert steps[0]["bounds"] == (10, 6, 8, 4) and steps[0]["shift"] == [0, 0]
    assert steps[1]["bounds"] == (6, 4, 16, 8)
    assert steps[1]["size"] == {"w": 16, "h": 8} and steps[1]["factor"] == [2.0, 2.0]
    assert steps[2]["bounds"] == (10, 0, 8, 16) and steps[2]["shift"] == [0, 0]
    assert final == steps[3]["bounds"] == (13, -2, 8, 16)
    # A custom pivot: the flip moves the layer, the scale keeps the pivot.
    spec = tp.parse({"flip": "h", "scale": 2, "pivot": [10, 6]})
    steps, final = tp.predict((10, 6, 8, 4), spec)
    assert steps[0]["bounds"] == (2, 6, 8, 4) and steps[0]["shift"] == [-8, 0]
    assert final == (-6, 6, 16, 8)


def test_check_size():
    tp.check_size((0, 0, 100, 100))
    limits = config.current().limits
    with pytest.raises(CommandError) as e:
        tp.check_size((0, 0, limits["max_canvas_dim"] + 1, 1))
    assert e.value.code == "too_large"


def test_rect_helpers():
    assert tp.union(None, (1, 2, 3, 4)) == (1, 2, 3, 4)
    assert tp.union((0, 0, 2, 2), (5, 5, 1, 1)) == (0, 0, 6, 6)
    assert tp.minus(None, (0, 0, 1, 1)) == []
    assert tp.minus((0, 0, 4, 4), None) == [(0, 0, 4, 4)]
    assert tp.minus((0, 0, 4, 4), (0, 0, 4, 4)) == []
    assert tp.minus((0, 0, 4, 4), (10, 10, 2, 2)) == [(0, 0, 4, 4)]
    parts = tp.minus((0, 0, 10, 10), (2, 3, 4, 5))
    assert parts == [(0, 0, 10, 3), (0, 8, 10, 2), (0, 3, 2, 5), (6, 3, 4, 5)]
    assert sum(w * h for _, _, w, h in parts) == 100 - 20
