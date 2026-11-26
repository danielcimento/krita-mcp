"""What covers a layer, for ``occlusion_map`` (api-design § krita_occlusion_map).
Pure: no krita / PyQt5 imports.

"Above" is decided per *unit*: the thing that is composited onto the stack
holding the target. Walking up from the target, at each level (its parent,
then each ancestor's parent, up to the root) the siblings stacked above the
target's branch draw after it (global draw order). Among those:

- a hidden node is skipped with its subtree;
- a **pass-through group** has no composite of its own (krita-api-notes
  § Nodes): its children go straight into the parent's stack, so each of
  them is looked at in turn (its blend mode is ignored, as Krita ignores
  it; its opacity does apply, and the render keeps it);
- an **isolated group** is one unit: its composite goes onto the stack with
  the group's blend mode, so the group counts (with everything visible
  inside it, whatever their own modes) when *its* mode is in ``modes``;
- a **paint layer** is one unit, counted when its mode is in ``modes``;
- anything else (vector, fill, file, clone and filter layers, masks) is
  not counted.

Levels inside a hidden ancestor of the target are skipped (a unit must be
visible with all its ancestors visible).

The coverage render (``only`` the units, the target hidden) needs three
adjustments so the projection's alpha is what covers the target:

- ``unclip``: a unit with inherit alpha (and a pass-through group on the
  way to it) is clipped to the stack below it, which holds the target.
  Inherit alpha is alpha-locked compositing (verified 2026-11-25: an
  opaque clipped layer over alpha 128 gives its colour at alpha 128), so
  over the target's pixels it replaces a share of them equal to its own
  alpha. With the target hidden it would have nothing to clip to, so it
  is rendered unclipped. Inside a pass-through group it clips to the
  parent's stack too (verified), so the same holds there.
- ``neutral``: the target's ancestors that also hold units are composited
  at full opacity, Normal and unclipped, since their opacity, blend mode
  and clipping apply to the target and to what covers it alike. Their
  masks are hidden by the render's ``only`` (they are siblings of the
  shown nodes), for the same reason.

Masks of a pass-through group on the way to a unit are hidden by ``only``
too; that changes nothing, since Krita ignores a transparency mask on a
pass-through group (verified 2026-11-25: on an isolated group or a layer
it cuts, on a pass-through group the projection is unchanged).
"""

import math

from . import blendmodes
from .errors import CommandError
from .refs import ROOT

GROUP = "grouplayer"
PAINT = "paintlayer"
DEFAULT_MODES = ("normal",)
DEFAULT_THRESHOLD = 0.5
MAX_IDS = 50


def _bad(message, **details):
    return CommandError("bad_request", message, details or None)


def check_modes(value):
    """``modes``: a non-empty list of blend-mode IDs, repeats dropped, in
    the order given; None gives ``["normal"]``."""
    if value is None:
        return list(DEFAULT_MODES)
    if not isinstance(value, list) or not value:
        raise _bad("'modes' must be a non-empty list of blend-mode ids such as "
                   "[\"normal\", \"multiply\"], or null for [\"normal\"]", param="modes")
    out = []
    for i, mode in enumerate(value):
        if not blendmodes.is_valid(mode):
            close = blendmodes.suggest(mode)
            hint = f"; did you mean {', '.join(repr(c) for c in close)}?" if close else ""
            raise _bad(f"'modes[{i}]': {mode!r} is not a Krita blend-mode id{hint}",
                       param="modes", index=i, suggestions=close)
        if mode not in out:
            out.append(mode)
    return out


def mode_set(modes):
    """The blend modes that count: ``modes`` with ``add`` and
    ``linear_dodge`` standing for each other (D17)."""
    out = set(modes)
    if out & blendmodes.ADDITIVE:
        out |= blendmodes.ADDITIVE
    return frozenset(out)


def check_threshold(value):
    """``threshold``: a number in (0, 1], default 0.5."""
    if value is None:
        return DEFAULT_THRESHOLD
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or not 0 < value <= 1):
        raise _bad(f"'threshold' must be a number greater than 0 and at most 1 (a share of "
                   f"full coverage), got {value!r}", param="threshold")
    return float(value)


def threshold_level(threshold):
    """The 8-bit coverage a pixel needs to count as covered:
    ``round(threshold * 255)`` (halves up), at least 1. 0.5 gives 128."""
    return max(1, min(255, math.floor(threshold * 255 + 0.5)))


class Plan:
    """``units`` (ids, stacking order, top first), ``unclip`` and
    ``neutral`` (id sets); see the module docstring."""

    __slots__ = ("units", "unclip", "neutral")

    def __init__(self, units, unclip, neutral):
        self.units, self.unclip, self.neutral = units, unclip, neutral


def _visible_chain(index, props, nid):
    """True when ``nid`` (or ``ROOT``) and all its ancestors are visible."""
    if nid == ROOT:
        return True
    return props[nid]["visible"] and all(props[a]["visible"] for a in index.ancestors(nid))


def plan(index, props, target, modes):
    """The units covering ``target`` (an id in ``index``) and the render's
    adjustments. ``props``: ``{id: {"type", "visible", "blend_mode",
    "pass_through", "inherit_alpha"}}`` for every node; ``modes``: the
    blend modes that count (``mode_set``)."""
    units, unclip = [], set()

    def visit(nid, via):
        p = props[nid]
        if not p["visible"]:
            return
        if p["type"] == GROUP and p["pass_through"]:
            for child in index.children(nid):
                visit(child, via + [nid])
            return
        if p["type"] not in (GROUP, PAINT) or p["blend_mode"] not in modes:
            return
        units.append(nid)
        unclip.update(n for n in via + [nid] if props[n]["inherit_alpha"])

    branch = target
    while branch != ROOT:
        level = index.parent(branch)
        if _visible_chain(index, props, level):
            siblings = index.children(level)  # top to bottom
            for sib in siblings[:siblings.index(branch)]:
                visit(sib, [])
        branch = level
    units.sort(key=index.order)
    ancestors = set(index.ancestors(target))
    neutral = {a for u in units for a in index.ancestors(u) if a in ancestors}
    return Plan(units, unclip, neutral)


def summary_ids(units):
    """``{count, ids}``, ``ids`` only when there are at most ``MAX_IDS``."""
    out = {"count": len(units)}
    if len(units) <= MAX_IDS:
        out["ids"] = list(units)
    return out
