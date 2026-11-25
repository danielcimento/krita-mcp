"""Pin the current behaviour of kritamcp.params."""

import pytest

from kritamcp.errors import CommandError
from kritamcp.params import float_param, int_param


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


def test_float_nan_rejected_as_out_of_range():
    with pytest.raises(CommandError, match="must be between"):
        float_param({"o": float("nan")}, "o", 1.0, 0.0, 1.0)


def test_command_error_is_an_exception_with_message():
    e = CommandError("boom")
    assert isinstance(e, Exception) and str(e) == "boom"
