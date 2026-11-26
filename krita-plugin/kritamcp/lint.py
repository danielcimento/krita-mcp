"""Lint rules over a document snapshot (D14). Pure: stdlib only, no krita / PyQt5.

``krita_lint`` collects a snapshot inside Krita (``commands/analysis.py``,
P4.1) and hands it to ``run``. The snapshot format is in api-design.md
§ Snapshot (P4.1/P4.2); in short::

    {"document": {"width", "height", "color_model", "color_depth", "color_profile"},
     "settings": {"additive_mode", "stray_alpha_below", "stray_alpha_distance"},
     "nodes": [{"id", "name", "path", "type", "parent", "depth", "order",
                "visible", "opacity", "blend_mode", "inherit_alpha", "alpha_locked",
                "locked", "pass_through", "has_layer_style", "bounds",
                "alpha": {"pixel_count", "opaque_bounds", "stray_count"} | None}, ...]}

``nodes`` is every node except the root-level selection mask, in global
stacking order top to bottom; ``order`` is the position in that list.

Each rule is ``fn(snapshot) -> [finding]``, registered with ``@rule`` under
an id, a severity and its sets. A finding is ``{rule, severity, layer_id,
path, message}`` plus ``related_ids`` when several nodes are involved;
document-level findings have ``layer_id`` and ``path`` ``None``.

Settled here (api-design.md § Lint rules, P4.2 note):

- **Reference groups**: a ``grouplayer`` whose name, trimmed, starts with
  ``[`` and ends with ``]`` (``[ Guide ]``, ``[Refs]``). Everything inside
  one, at any depth, is skipped by every rule here. The bracket group
  itself is a normal group and is checked (its name counts for
  ``duplicate-name``). Only groups count: a bracket-named paint layer is
  linted like any other.
- **Side tokens** (CONVENTIONS § Naming): the standard token is a separate
  word ``L`` or ``R``, after the first word, placed before trailing
  variant/index words (``Arm L A``, ``Highlight R 2``). See ``side_parts``.
"""

import re
from typing import NamedTuple

from .errors import CommandError

SEVERITIES = ("info", "warning", "error")
DEFAULT_SETTINGS = {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4}
ADDITIVE_MODES = ("either", "add", "linear_dodge")
ADDITIVE = ("add", "linear_dodge")  # one mode, two IDs (D17)
RIG_BLEND_MODES = ("normal", "multiply") + ADDITIVE

PAINT = "paintlayer"
GROUP = "grouplayer"
TYPE_LABELS = {
    "filterlayer": "Filter layer", "filllayer": "Fill layer", "clonelayer": "Clone layer",
    "vectorlayer": "Vector layer", "filelayer": "File layer",
    "transparencymask": "Transparency mask", "filtermask": "Filter mask",
    "transformmask": "Transform mask", "colorizemask": "Colorize mask",
    "selectionmask": "Selection mask",
}
MASK_TYPES = frozenset(t for t in TYPE_LABELS if t.endswith("mask"))


class Rule(NamedTuple):
    id: str
    severity: str
    sets: tuple
    fn: object


RULES = {}  # id -> Rule, in registration order
SETS = ("default", "conventions", "all")  # "all" = every rule; "conventions" is filled by P5.9


def rule(rule_id, severity, sets=("default",)):
    """Register ``fn(snapshot) -> [finding]`` as rule ``rule_id``."""
    assert severity in SEVERITIES and rule_id not in RULES and rule_id not in SETS
    assert all(s in SETS and s != "all" for s in sets)

    def register(fn):
        RULES[rule_id] = Rule(rule_id, severity, tuple(sets), fn)
        return fn
    return register


def set_members(name):
    """The rule ids in set ``name``, in registration order."""
    return [r.id for r in RULES.values() if name == "all" or name in r.sets]


# --- run ----------------------------------------------------------------

def run(snapshot, rules=None, severity_min=None):
    """Run ``rules`` (rule ids and/or set names; ``["default"]`` if None) on
    ``snapshot``. Returns ``{findings, counts, rules_run}``.

    ``findings`` are sorted by the involved node's ``order`` (document-level
    first), then by rule id. ``severity_min`` (``info``/``warning``/``error``)
    drops less severe findings; ``counts`` counts the findings returned.
    Unknown names, a bad ``severity_min`` or bad ``settings`` are
    ``bad_request``.
    """
    selected = _select(["default"] if rules is None else rules)
    if severity_min is not None and severity_min not in SEVERITIES:
        raise CommandError("bad_request", f"severity_min must be one of {', '.join(SEVERITIES)}, "
                                          f"got {severity_min!r}")
    _check_snapshot(snapshot)
    floor = SEVERITIES.index(severity_min or "info")
    order = {n["id"]: n.get("order", i) for i, n in enumerate(snapshot["nodes"])}

    findings = []
    for rule_id in selected:
        for f in RULES[rule_id].fn(snapshot):
            if SEVERITIES.index(f["severity"]) >= floor:
                findings.append(f)
    findings.sort(key=lambda f: (f["layer_id"] is not None, order.get(f["layer_id"], 0), f["rule"]))
    counts = {s: 0 for s in ("error", "warning", "info")}
    for f in findings:
        counts[f["severity"]] += 1
    return {"findings": findings, "counts": counts, "rules_run": selected}


def _select(names):
    if isinstance(names, str) or not isinstance(names, (list, tuple)) or not names:
        raise CommandError("bad_request", "rules must be a non-empty list of rule ids and/or set names")
    valid = list(SETS) + list(RULES)
    unknown = [n for n in names if not isinstance(n, str) or n not in valid]
    if unknown:
        raise CommandError("bad_request",
                           f"unknown lint rule or set {', '.join(repr(n) for n in unknown)}; "
                           f"valid: {', '.join(valid)}",
                           {"unknown": unknown, "sets": list(SETS), "rules": list(RULES)})
    wanted = set()
    for n in names:
        wanted.update(set_members(n) if n in SETS else [n])
    return [r for r in RULES if r in wanted]


def _check_snapshot(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("nodes"), list) \
            or not isinstance(snapshot.get("document"), dict):
        raise CommandError("bad_request", "lint snapshot must have a 'document' object and a 'nodes' list")
    s = settings(snapshot)
    if s["additive_mode"] not in ADDITIVE_MODES:
        raise CommandError("bad_request", f"lint additive_mode must be one of {', '.join(ADDITIVE_MODES)}, "
                                          f"got {s['additive_mode']!r}")


def settings(snapshot):
    """The snapshot's ``settings`` over ``DEFAULT_SETTINGS``."""
    return {**DEFAULT_SETTINGS, **(snapshot.get("settings") or {})}


# --- snapshot helpers ---------------------------------------------------

def is_reference_group(node):
    """A group whose name is wrapped in brackets (CONVENTIONS § Naming)."""
    name = (node.get("name") or "").strip()
    return node.get("type") == GROUP and name.startswith("[") and name.endswith("]")


def stacked(snapshot):
    """``nodes`` sorted by ``order`` (they normally already are)."""
    nodes = snapshot["nodes"]
    return [n for _, n in sorted(enumerate(nodes), key=lambda p: (p[1].get("order", p[0]), p[0]))]


def checked_nodes(snapshot):
    """The nodes the rules look at, in stacking order: all of them except the
    contents of reference groups and a stray root-level selection mask."""
    skip = set()
    out = []
    for n in stacked(snapshot):  # parents come before their children (depth-first, top to bottom)
        if n.get("parent") in skip:
            skip.add(n["id"])
            continue
        if n.get("type") == "selectionmask" and n.get("parent") is None:
            continue  # the document's global selection (D4); never in a snapshot, but be safe
        if is_reference_group(n):
            skip.add(n["id"])
        out.append(n)
    return out


def finding(rule_id, node, message, related_ids=None):
    f = {"rule": rule_id, "severity": RULES[rule_id].severity,
         "layer_id": None if node is None else node["id"],
         "path": None if node is None else node.get("path"), "message": message}
    if related_ids is not None:
        f["related_ids"] = list(related_ids)
    return f


def _alpha(node):
    return node.get("alpha") if node.get("type") == PAINT else None


# --- default rules ------------------------------------------------------

@rule("node-type", "error")
def node_type(snapshot):
    by_id = {n["id"]: n for n in snapshot["nodes"]}
    out = []
    for n in checked_nodes(snapshot):
        t = n.get("type")
        if t in (PAINT, GROUP):
            continue
        label = TYPE_LABELS.get(t)
        if t in MASK_TYPES:
            parent = by_id.get(n.get("parent"))
            on = parent.get("path") if parent else (n.get("path") or "").rpartition("/")[0]
            msg = f"{label} '{n.get('path')}' on layer '{on}' must be applied or removed"
        elif label:
            msg = f"{label} '{n.get('path')}' must be baked or removed"
        else:
            msg = f"Node '{n.get('path')}' of type {t!r} must be baked or removed (only paint layers and groups)"
        out.append(finding("node-type", n, msg))
    return out


@rule("layer-style", "error")
def layer_style(snapshot):
    return [finding("layer-style", n, f"Layer '{n.get('path')}' has a layer style; "
                                      "bake it into the pixels or remove it")
            for n in checked_nodes(snapshot) if n.get("has_layer_style")]


@rule("inherit-alpha", "error")
def inherit_alpha(snapshot):
    return [finding("inherit-alpha", n, f"Layer '{n.get('path')}' inherits alpha (clipping); bake it "
                                        "into the layer it clips to with krita_merge_layers")
            for n in checked_nodes(snapshot) if n.get("inherit_alpha")]


@rule("blend-mode", "error")
def blend_mode(snapshot):
    want = settings(snapshot)["additive_mode"]
    out = []
    for n in checked_nodes(snapshot):
        if n.get("type") != PAINT:
            continue  # group blending doesn't matter to the rig
        mode = n.get("blend_mode") or "normal"
        if mode not in RIG_BLEND_MODES:
            out.append(finding("blend-mode", n, (
                f"Layer '{n.get('path')}' uses blend mode '{mode}'; the rig supports only "
                "normal, multiply and add: bake it, or change it with krita_update_layers")))
        elif mode in ADDITIVE and want != "either" and mode != want:
            out.append(finding("blend-mode", n, (
                f"Layer '{n.get('path')}' uses '{mode}', but lint.additive_mode requires '{want}' "
                f"(same rendering): set blend_mode '{want}' with krita_update_layers")))
    return out


@rule("duplicate-name", "error")
def duplicate_name(snapshot):
    by_name = {}
    for n in checked_nodes(snapshot):
        by_name.setdefault(n.get("name"), []).append(n)
    out = []
    for name, nodes in by_name.items():
        if len(nodes) > 1:
            paths = ", ".join(f"'{n.get('path')}'" for n in nodes)
            out.append(finding("duplicate-name", nodes[1], (
                f"Name '{name}' is used by {len(nodes)} nodes ({paths}); "
                "names must be unique in the whole document"), [n["id"] for n in nodes]))
    return out


# Side tokens. Standard: a separate word "L" / "R" after the first word.
_SIDE_WORDS = {"l": "L", "r": "R", "left": "L", "right": "R"}
_BRACKETED = re.compile(r"[(\[]\s*(l|r|left|right)\s*[)\]]", re.IGNORECASE)
_JOINERS = re.compile(r"[_.\-]")
_VARIANT = re.compile(r"[A-Z]|\d+")


class SideParts(NamedTuple):
    stem: str            # the name without its side token(s), words joined by spaces
    side: object         # "L", "R" or None
    nonstandard: list    # the non-standard token spellings found, e.g. ["_L"]
    suggestion: object   # the standard spelling of the name, if non-standard


def side_parts(name):
    """Split ``name`` into its stem and side token (CONVENTIONS § Naming).

    The standard token is the word ``L`` or ``R``, not first. Non-standard
    spellings, all recognised as a side: a lowercase word ``l`` / ``r``;
    ``Left`` / ``Right`` as a word (any case); ``(L)`` / ``[R]`` (any case);
    a letter or word joined by ``_``, ``.`` or ``-`` (``Eye_L``, ``L-Eye``,
    ``Hand.r.B``); and ``L`` / ``R`` as the first word (``L Eye``). A
    name that is one plain word (``L``, ``Left``) has no side. The suggestion puts the standard token
    before trailing variant/index words (single capitals, numbers):
    ``Left Hand B`` -> ``Hand L B``.
    """
    words = name.split()
    stem, side, odd = [], None, []

    def take(token, spelling):
        nonlocal side
        side = side or _SIDE_WORDS[token.lower()]
        if spelling:
            odd.append(spelling)

    for i, word in enumerate(words):
        m = _BRACKETED.fullmatch(word)
        parts = [p for p in _JOINERS.split(word) if p]
        if m and len(words) > 1:
            take(m.group(1), word)
        elif len(parts) > 1 and any(p.lower() in _SIDE_WORDS for p in parts):
            for p in parts:
                if side is None and p.lower() in _SIDE_WORDS:
                    take(p, _joined_token(word, p))
                else:
                    stem.append(p)
        elif word.lower() in _SIDE_WORDS and len(words) > 1:
            if word in ("L", "R") and i > 0:
                take(word, None)  # the standard form
            else:
                take(word, word)
        else:
            stem.append(word)
    if side is None:
        return SideParts(" ".join(words), None, [], None)
    suggestion = None
    if odd:
        tail = len(stem)
        while tail > 1 and _VARIANT.fullmatch(stem[tail - 1]):
            tail -= 1
        suggestion = " ".join(stem[:tail] + [side] + stem[tail:])
    return SideParts(" ".join(stem), side, odd, suggestion)


def _joined_token(word, part):
    """How ``part`` appears in ``word`` with its joiner, for messages: ``_L``."""
    m = re.search(rf"[_.\-]{re.escape(part)}(?![^_.\-])", word)
    if m:
        return m.group(0)
    m = re.search(rf"(?<![^_.\-]){re.escape(part)}[_.\-]", word)
    return m.group(0) if m else part


@rule("side-token", "warning")
def side_token(snapshot):
    nodes = checked_nodes(snapshot)
    parts = {n["id"]: side_parts(n.get("name") or "") for n in nodes}
    out = []
    siblings = {}
    for n in nodes:
        siblings.setdefault((n.get("parent"), parts[n["id"]].stem), []).append(n)
        p = parts[n["id"]]
        if p.nonstandard:
            out.append(finding("side-token", n, (
                f"Layer '{n.get('path')}' uses non-standard side token "
                f"{', '.join(repr(t) for t in p.nonstandard)}; rename it to '{p.suggestion}' "
                "(' L' / ' R' as a separate word, the character's side)")))
    for group in siblings.values():
        sided = [n for n in group if parts[n["id"]].side]
        if not sided:
            continue
        for n in group:
            if not parts[n["id"]].side:
                out.append(finding("side-token", n, (
                    f"Layer '{n.get('path')}' has no side token, but its sibling "
                    f"'{sided[0].get('name')}' does; add ' L' or ' R' (the character's side) "
                    "or rename it"), [m["id"] for m in sided]))
    return out


@rule("empty-layer", "warning")
def empty_layer(snapshot):
    out = []
    for n in checked_nodes(snapshot):
        a = _alpha(n)
        if a is not None and a.get("pixel_count") == 0:
            out.append(finding("empty-layer", n, f"Paint layer '{n.get('path')}' has no pixels; "
                                                 "paint it or delete it"))
    return out


@rule("out-of-canvas", "warning")
def out_of_canvas(snapshot):
    doc = snapshot["document"]
    w, h = doc.get("width"), doc.get("height")
    out = []
    for n in checked_nodes(snapshot):
        a = _alpha(n)
        r = a.get("opaque_bounds") if a else None
        if not r or w is None or h is None:
            continue
        over = [("left", -r["x"]), ("above", -r["y"]),
                ("right", r["x"] + r["w"] - w), ("below", r["y"] + r["h"] - h)]
        over = [f"{px} px {side}" for side, px in over if px > 0]
        if over:
            out.append(finding("out-of-canvas", n, (
                f"Paint layer '{n.get('path')}' has pixels outside the {w}x{h} canvas "
                f"({', '.join(over)}); move them in or trim them")))
    return out


@rule("stray-alpha", "warning")
def stray_alpha(snapshot):
    s = settings(snapshot)
    out = []
    for n in checked_nodes(snapshot):
        a = _alpha(n)
        count = a.get("stray_count") if a else None
        if count:
            out.append(finding("stray-alpha", n, (
                f"Paint layer '{n.get('path')}' has {count} stray pixel{'s' if count != 1 else ''} "
                f"(alpha below {s['stray_alpha_below']}, more than {s['stray_alpha_distance']} px from "
                f"any pixel with alpha {s['stray_alpha_below']} or more); erase them")))
    return out


@rule("color-space", "error")
def color_space(snapshot):
    doc = snapshot["document"]
    model, depth, profile = doc.get("color_model"), doc.get("color_depth"), doc.get("color_profile")
    problems = []
    if model != "RGBA":
        problems.append(f"color model {model!r}")
    if depth != "U8":
        problems.append(f"depth {depth!r}")
    if not isinstance(profile, str) or "sRGB" not in profile:
        problems.append(f"profile {profile!r}")
    if not problems:
        return []
    return [finding("color-space", None, (
        f"Document has {', '.join(problems)}; the rig needs 8-bit RGBA sRGB (RGBA / U8, an sRGB "
        "profile): convert the image color space"))]
