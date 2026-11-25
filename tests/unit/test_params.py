"""Pin the current behaviour of kritamcp.params."""

import pytest

from kritamcp import config
from kritamcp.errors import CommandError
from kritamcp.params import canvas_size, float_param, int_param


def test_int_default_used_when_missing():
    assert int_param({}, "w", 800, 1, 8192) == 800


def test_int_value_used_when_present():
    assert int_param({"w": 5}, "w", 800, 1, 8192) == 5


def test_int_truncates_float():
    assert int_param({"w": 3.9}, "w", 0, 0, 10) == 3
    assert int_param({"w": -3.9}, "w", 0, -10, 10) == -3
    assert type(int_param({"w": 3.9}, "w", 0, 0, 10)) is int


@pytest.mark.parametrize("fn", [int_param, float_param])
@pytest.mark.parametrize("bad", [True, False, "3", None, [3], {"a": 1}])
def test_non_numbers_rejected(fn, bad):
    with pytest.raises(CommandError, match="^'w' must be a number$"):
        fn({"w": bad}, "w", 1, 0, 10)


@pytest.mark.parametrize("fn", [int_param, float_param])
def test_none_default_with_missing_key_rejected(fn):
    with pytest.raises(CommandError, match="^'w' must be a number$"):
        fn({}, "w", None, 0, 10)


def test_int_bounds_inclusive():
    assert int_param({"w": 1}, "w", 0, 1, 10) == 1
    assert int_param({"w": 10}, "w", 0, 1, 10) == 10


def test_int_out_of_range_message():
    with pytest.raises(CommandError, match="^'w' must be between 1 and 10$"):
        int_param({"w": 11}, "w", 0, 1, 10)
    with pytest.raises(CommandError, match="^'w' must be between 1 and 10$"):
        int_param({"w": 0}, "w", 0, 1, 10)


def test_int_range_checked_after_truncation():
    # 10.5 truncates to 10, which is in range.
    assert int_param({"w": 10.5}, "w", 0, 1, 10) == 10
    # 0.9 truncates to 0, which is out of range.
    with pytest.raises(CommandError, match="between 1 and 10"):
        int_param({"w": 0.9}, "w", 0, 1, 10)


def test_int_default_is_range_checked():
    with pytest.raises(CommandError, match="^'w' must be between 1 and 10$"):
        int_param({}, "w", 0, 1, 10)


def test_float_default_and_value():
    assert float_param({}, "o", 1.0, 0.0, 1.0) == 1.0
    assert float_param({"o": 0.25}, "o", 1.0, 0.0, 1.0) == 0.25


def test_float_accepts_int_and_returns_float():
    got = float_param({"o": 1}, "o", 0.5, 0.0, 1.0)
    assert got == 1.0 and type(got) is float


def test_float_bounds_inclusive():
    assert float_param({"o": 0.0}, "o", 1.0, 0.0, 1.0) == 0.0
    assert float_param({"o": 1.0}, "o", 1.0, 0.0, 1.0) == 1.0


def test_float_out_of_range_message():
    with pytest.raises(CommandError, match=r"^'o' must be between 0\.0 and 1\.0$"):
        float_param({"o": 1.01}, "o", 1.0, 0.0, 1.0)
    with pytest.raises(CommandError, match=r"^'o' must be between 0\.0 and 1\.0$"):
        float_param({"o": -0.01}, "o", 1.0, 0.0, 1.0)


def test_float_nan_rejected_as_not_a_number():
    with pytest.raises(CommandError, match="must be a number"):
        float_param({"o": float("nan")}, "o", 1.0, 0.0, 1.0)


def test_command_error_is_an_exception_with_message():
    e = CommandError("bad_request", "boom")
    assert isinstance(e, Exception) and str(e) == "boom"


# --- canvas_size (new_canvas, D8) ---------------------------------------

DEFAULTS = config.DEFAULT_LIMITS
SMALL = {"max_canvas_dim": 64, "max_canvas_pixels": 2048}


def _too_large(params, limits, limit_name):
    with pytest.raises(CommandError, match="configurable under \"limits\"") as info:
        canvas_size(params, limits)
    assert info.value.code == "too_large"
    assert info.value.details["limit"] == limit_name
    assert limit_name in info.value.message
    return info.value


def test_canvas_size_defaults_and_mao():
    assert canvas_size({}, DEFAULTS) == (800, 600)
    assert canvas_size({"width": 5800, "height": 8400}, DEFAULTS) == (5800, 8400)
    assert canvas_size({"width": 16384, "height": 4096}, DEFAULTS) == (16384, 4096)
    assert canvas_size({"width": 8192, "height": 8192}, DEFAULTS) == (8192, 8192)  # exactly the cap


def test_canvas_size_per_side_limit_is_too_large():
    e = _too_large({"width": 16385, "height": 16}, DEFAULTS, "max_canvas_dim")
    assert e.details == {"limit": "max_canvas_dim", "max": 16384, "width": 16385}
    _too_large({"width": 16, "height": 16385}, DEFAULTS, "max_canvas_dim")
    _too_large({"width": float("1e300"), "height": 16}, DEFAULTS, "max_canvas_dim")


def test_canvas_size_pixel_limit_is_too_large():
    e = _too_large({"width": 9000, "height": 9000}, DEFAULTS, "max_canvas_pixels")
    assert e.details == {"limit": "max_canvas_pixels", "max": 67108864, "pixels": 81000000}
    _too_large({"width": 8193, "height": 8192}, DEFAULTS, "max_canvas_pixels")


def test_canvas_size_uses_the_given_limits():
    assert canvas_size({"width": 32, "height": 32}, SMALL) == (32, 32)
    _too_large({"width": 65, "height": 10}, SMALL, "max_canvas_dim")
    _too_large({"width": 50, "height": 50}, SMALL, "max_canvas_pixels")


@pytest.mark.parametrize("value, message", [
    (0, "'width' must be at least 1"),
    (-5, "'width' must be at least 1"),
    (0.5, "'width' must be at least 1"),
    ("800", "'width' must be a number"),
    (True, "'width' must be a number"),
    (None, "'width' must be a number"),
    (float("inf"), "'width' must be a number"),
    (float("nan"), "'width' must be a number"),
])
def test_canvas_size_bad_values_are_bad_request(value, message):
    with pytest.raises(CommandError) as info:
        canvas_size({"width": value}, DEFAULTS)
    assert (info.value.code, info.value.message) == ("bad_request", message)


def test_canvas_size_floats_truncate():
    assert canvas_size({"width": 10.9, "height": 2.0}, DEFAULTS) == (10, 2)


@pytest.mark.parametrize("fn", [int_param, float_param])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_is_bad_request(fn, bad):
    with pytest.raises(CommandError) as e:
        fn({"x": bad}, "x", 0, -10, 10)
    assert e.value.code == "bad_request"
