"""Parameter validators. Pure: no krita / PyQt5 imports."""

from .errors import CommandError


def int_param(params, key, default, lo, hi):
    """Read an integer parameter and check it is within [lo, hi]."""
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError("bad_request", f"'{key}' must be a number")
    value = int(value)
    if not lo <= value <= hi:
        raise CommandError("bad_request", f"'{key}' must be between {lo} and {hi}")
    return value


def float_param(params, key, default, lo, hi):
    value = params.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandError("bad_request", f"'{key}' must be a number")
    value = float(value)
    if not lo <= value <= hi:
        raise CommandError("bad_request", f"'{key}' must be between {lo} and {hi}")
    return value
