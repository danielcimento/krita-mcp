"""The layer properties that ``create_layers`` and ``update_layers`` set.
Pure: no krita / PyQt5 imports.

``check(values, node_type)`` validates the property keys present in
``values`` and returns them normalized; other keys are ignored (each tool
rejects keys it doesn't know itself). ``apply(node, props)`` sets them on a
libkis ``Node`` through duck-typed calls, so this module needs no Krita.

Units (api-design § Conventions): ``opacity`` is 0.0–1.0 here and 0–255 in
Krita (``to_krita_opacity``: ``round(v * 255)``).
"""

import math
import unicodedata

from . import blendmodes
from .errors import CommandError

MAX_NAME_LEN = 255

PROPS = ("name", "visible", "opacity", "blend_mode", "locked", "alpha_locked",
         "inherit_alpha", "pass_through")
BOOL_PROPS = ("visible", "locked", "alpha_locked", "inherit_alpha", "pass_through")

GROUP = "grouplayer"


def _bad(field, message, **details):
    return CommandError("bad_request", f"'{field}' {message}", {"param": field, **details})


def check_name(value, field="name"):
    """A layer name: a non-empty string of at most ``MAX_NAME_LEN``
    characters, without control characters (newlines, tabs, NUL, …) or
    unpaired surrogates. Returned unchanged (no trimming)."""
    if not isinstance(value, str) or value == "":
        raise _bad(field, "must be a non-empty string")
    if len(value) > MAX_NAME_LEN:
        raise _bad(field, f"is {len(value)} characters long; the limit is {MAX_NAME_LEN}")
    if any(unicodedata.category(c) == "Cc" for c in value):
        raise _bad(field, "must not contain control characters (newline, tab, NUL, ...)")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _bad(field, "contains an unpaired surrogate") from None
    return value


def check_bool(value, field):
    if type(value) is not bool:
        raise _bad(field, f"must be true or false, got {value!r}")
    return value


def check_opacity(value, field="opacity"):
    """A finite number in [0, 1], returned as a float."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value)):
        raise _bad(field, f"must be a number from 0.0 to 1.0, got {value!r}")
    if not 0 <= value <= 1:
        raise _bad(field, f"must be from 0.0 to 1.0 (not 0-255), got {value!r}")
    return float(value)


def check_blend_mode(value, field="blend_mode"):
    if not isinstance(value, str):
        raise _bad(field, f"must be a Krita blend-mode id such as 'normal', got {value!r}")
    if not blendmodes.is_valid(value):
        close = blendmodes.suggest(value)
        hint = f"; did you mean {', '.join(repr(c) for c in close)}?" if close else ""
        raise _bad(field, f"{value!r} is not a Krita blend-mode id{hint}",
                   value=value, suggestions=close)
    return value


def to_krita_opacity(value):
    """0.0–1.0 → Krita's 0–255."""
    return int(round(value * 255))


def check(values, node_type=None):
    """Validate the ``PROPS`` keys present in ``values``; return them
    normalized, in ``PROPS`` order. ``node_type``: the layer's Krita type
    (``"grouplayer"``, ``"paintlayer"``, …) when known; ``pass_through`` is
    refused on anything but a group. Errors: ``bad_request`` with
    ``details.param`` naming the property."""
    out = {}
    for key in PROPS:
        if key not in values:
            continue
        v = values[key]
        if key == "name":
            out[key] = check_name(v)
        elif key == "opacity":
            out[key] = check_opacity(v)
        elif key == "blend_mode":
            out[key] = check_blend_mode(v)
        else:
            out[key] = check_bool(v, key)
    if "pass_through" in out and node_type is not None and node_type != GROUP:
        raise _bad("pass_through", f"is only valid on groups, not on a {node_type}")
    return out


# Setter per property. ``locked`` is last: a locked layer may refuse later
# writes, so ``apply`` callers set pixels between ``apply(..., locked=False)``
# and ``apply_locked``.
_SETTERS = {
    "name": lambda node, v: node.setName(v),
    "visible": lambda node, v: node.setVisible(v),
    "opacity": lambda node, v: node.setOpacity(to_krita_opacity(v)),
    "blend_mode": lambda node, v: node.setBlendingMode(v),
    "inherit_alpha": lambda node, v: node.setInheritAlpha(v),
    "alpha_locked": lambda node, v: node.setAlphaLocked(v),
    "pass_through": lambda node, v: node.setPassThroughMode(v),  # GroupLayer only
    "locked": lambda node, v: node.setLocked(v),
}


def apply(node, props, *, locked=True):
    """Set ``props`` (from ``check``) on ``node``, ``locked`` last. With
    ``locked=False`` the lock is left for ``apply_locked``."""
    for key in _SETTERS:
        if key in props and (locked or key != "locked"):
            _SETTERS[key](node, props[key])


def apply_locked(node, props):
    """Set ``locked`` if ``props`` has it."""
    if "locked" in props:
        _SETTERS["locked"](node, props["locked"])
