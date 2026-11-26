"""Lint rules over a document snapshot (D14). Pure: stdlib only, no krita / PyQt5.

``krita_lint`` collects a snapshot inside Krita (``commands/analysis.py``,
P4.1) and hands it to ``run``. The snapshot format is in api-design.md
§ Snapshot (P4.1/P4.2); in short::

    {"document": {"width", "height", "color_model", "color_depth", "color_profile"},
     "settings": {"additive_mode", "stray_alpha_below", "stray_alpha_distance",
                  "stray_speck_max_px"},
     "nodes": [{"id", "name", "path", "type", "parent", "depth", "order",
                "visible", "opacity", "blend_mode", "inherit_alpha", "alpha_locked",
                "locked", "pass_through", "has_layer_style", "bounds",
                "alpha": {"pixel_count", "opaque_bounds", "stray_count",
                          "stray_specks": {"count", "pixels", "regions", "samples",
                                           "truncated"}} | None}, ...]}

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
  ``duplicate-name`` among groups). Only groups count: a bracket-named paint layer is
  linted like any other.
- **Side tokens** (CONVENTIONS § Naming): the standard token is a separate
  word ``L`` or ``R``, after the first word, placed before trailing
  variant/index words (``Arm L A``, ``Highlight R 2``). See ``side_parts``.
- **Specks** (P5.10): ``stray-alpha`` fires only for layers with stray
  specks, 8-connected groups of at most ``stray_speck_max_px`` stray
  pixels (``alphastats.components``); larger stray regions are soft glows.
  A snapshot without ``stray_specks`` (an older collector) falls back to
  ``stray_count > 0``.
- **The conventions set** (P5.9, CONVENTIONS § Structure, § Naming): see
  the rules' docstrings and api-design.md § Lint rules.
"""

import re
from typing import NamedTuple

from .errors import CommandError

SEVERITIES = ("info", "warning", "error")
DEFAULT_SETTINGS = {"additive_mode": "either", "stray_alpha_below": 8, "stray_alpha_distance": 4,
                    "stray_speck_max_px": 64}
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
SETS = ("default", "conventions", "all")  # "all" = every rule


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
    selected = check_args(rules, severity_min)
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


def check_args(rules=None, severity_min=None):
    """Validate ``run``'s ``rules`` and ``severity_min`` without a snapshot
    (``krita_lint`` checks them before collecting one). Returns the rule
    ids to run, in registry order; bad values are ``bad_request``."""
    selected = _select(["default"] if rules is None else rules)
    if severity_min is not None and severity_min not in SEVERITIES:
        raise CommandError("bad_request", f"severity_min must be one of {', '.join(SEVERITIES)}, "
                                          f"got {severity_min!r}")
    return selected


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


def _pose_letter(node, by_id):
    """The pose letter of the nearest enclosing pose group (``<X> A``,
    ``<X> B``, …; ``pose_parts``) of ``node``, or None."""
    parent = by_id.get(node.get("parent"))
    while parent is not None:
        p = pose_parts(parent.get("name") or "") if parent.get("type") == GROUP else None
        if p:
            return p[1]
        parent = by_id.get(parent.get("parent"))
    return None


def _rename_suggestion(name, nodes, by_id):
    """``(text, [{id, name}])``: how CONVENTIONS § Naming would rename the
    copies of ``name``. When each copy sits in a different pose group, the
    pose suffix (``Hand L A`` / ``Hand L B``), as data too; otherwise an
    index suffix, as text only (which copy is which is the artist's call)."""
    letters = [_pose_letter(n, by_id) for n in nodes]
    if all(letters) and len(set(letters)) == len(letters):
        names = [f"{name} {letter}" for letter in letters]
        shown = " / ".join(f"'{n}'" for n in names)
        return (f"rename to {shown} (pose suffix, CONVENTIONS § Naming)",
                [{"id": n["id"], "name": new} for n, new in zip(nodes, names)])
    shown = " / ".join(f"'{name} {i}'" for i in range(1, min(len(nodes), 3) + 1))
    return (f"rename them to say what differs, or add an index: {shown}"
            f"{' / …' if len(nodes) > 3 else ''}", None)


@rule("duplicate-name", "error")
def duplicate_name(snapshot):
    """Names must be unique among groups and, separately, among layers
    (paint layers and the other layer types): a group may share its name
    with a layer, e.g. the Face group holding the Face layer (CONVENTIONS
    § Naming, F003 K6). One finding per clashing name and kind; the
    message lists the copies' paths and a fix, ``suggested_names`` the
    new names when they follow from pose groups."""
    by_id = {n["id"]: n for n in snapshot["nodes"]}
    by_name = {}
    for n in checked_nodes(snapshot):
        kind = "group" if n.get("type") == GROUP else "layer"
        by_name.setdefault((kind, n.get("name")), []).append(n)
    out = []
    for (kind, name), nodes in by_name.items():
        if len(nodes) < 2:
            continue
        if kind == "group":
            what, rule_text = "groups", "group names must be unique (a layer may share one)"
        else:
            what = "paint layers" if all(n.get("type") == PAINT for n in nodes) else "layers"
            rule_text = f"{what[:-1]} names must be unique (a group may share one)"
        paths = ", ".join(f"'{n.get('path')}'" for n in nodes)
        fix, suggested = _rename_suggestion(name, nodes, by_id)
        f = finding("duplicate-name", nodes[1], (
            f"Name '{name}' is used by {len(nodes)} {what} ({paths}); {rule_text}: {fix}"),
            [n["id"] for n in nodes])
        if suggested:
            f["suggested_names"] = suggested
        out.append(f)
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


def _plural(n, word):
    return f"{n} {word}{'' if n == 1 else 's'}"


@rule("stray-alpha", "warning")
def stray_alpha(snapshot):
    """Stray specks (P5.10): ``alpha.stray_specks.count > 0``. Larger stray
    regions (soft glows) are not flagged. ``truncated`` (too many runs to
    label) is a finding of its own. Without ``stray_specks`` (an older
    collector): ``stray_count > 0``."""
    s = settings(snapshot)
    below, dist = s["stray_alpha_below"], s["stray_alpha_distance"]
    stray = f"alpha below {below}, more than {dist} px from any pixel with alpha {below} or more"
    out = []
    for n in checked_nodes(snapshot):
        a = _alpha(n)
        if not a:
            continue
        path = n.get("path")
        specks = a.get("stray_specks")
        if specks is None:
            count = a.get("stray_count")
            if count:
                out.append(finding("stray-alpha", n, (
                    f"Paint layer '{path}' has {_plural(count, 'stray pixel')} ({stray}); erase them")))
        elif specks.get("truncated"):
            out.append(finding("stray-alpha", n, (
                f"Paint layer '{path}' has {_plural(a.get('stray_count') or 0, 'stray pixel')} ({stray}), "
                "too fragmented to analyse for specks; look for noise and erase it")))
        elif specks.get("count"):
            count, shown = specks["count"], (specks.get("samples") or [])[:3]
            where = ", ".join(f"({r['x']}, {r['y']}) {r['w']}x{r['h']}" for r in shown)
            if where:
                where = f", {'e.g. ' if count > len(shown) else ''}at {where}"
            box = specks.get("bounds")
            within = (f", all within ({box['x']}, {box['y']}) {box['w']}x{box['h']}"
                      if box and count > len(shown) else "")
            f = finding("stray-alpha", n, (
                f"Paint layer '{path}' has {_plural(count, 'stray speck')}, {specks.get('pixels', 0)} px "
                f"(groups of up to {s['stray_speck_max_px']} px with {stray}){where}{within}; "
                f"erase them, or clear them with krita_trim_layers(ids=[...], "
                f"alpha_threshold={below}, near_px={dist})"))
            # K7: enough to decide on a trim without rendering.
            f["pixels"] = specks.get("pixels", 0)
            f["bounds"] = box
            out.append(f)
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


# --- conventions set (P5.9; CONVENTIONS § Structure, § Naming) -----------

MAX_NESTING = 3  # groups above a paint layer (CONVENTIONS: Head/Eyes/Eyeballs)


@rule("nesting-depth", "warning", sets=("conventions",))
def nesting_depth(snapshot):
    """A paint layer inside more than ``MAX_NESTING`` groups (``depth`` > 4)."""
    out = []
    for n in checked_nodes(snapshot):
        groups = (n.get("depth") or 1) - 1
        if n.get("type") == PAINT and groups > MAX_NESTING:
            out.append(finding("nesting-depth", n, (
                f"Paint layer '{n.get('path')}' is {groups} groups deep; nest at most {MAX_NESTING} "
                "(move it up or merge groups)")))
    return out


# Title Case (name-case). Lowercase minor words are fine inside a name,
# never first or last (so a variant "a" at the end is still flagged).
MINOR_WORDS = frozenset("a an and as at but by for from in into nor of on onto or per the to via vs with".split())
_OPENERS = "([{\"'"
_CLOSERS = ")]}\"'.,:;!?"


def _part_problem(part, edge):
    """None if the hyphen-separated word part is fine, else "lowercase" or
    "uncased" (a letter without case, such as Japanese)."""
    core = part.lstrip(_OPENERS).rstrip(_CLOSERS)
    first = next((c for c in core if c.isalnum()), None)
    if first is None or first.isdigit() or first.isupper():
        return None  # symbols ("&", "[", "]"), numbers ("2", "3D"), capitalised words, acronyms
    if first.islower():
        return None if (not edge and core in MINOR_WORDS) else "lowercase"
    return "uncased"


def _word_parts(name):
    """``(word, [(part, edge)])`` per space-separated word; ``edge`` marks
    the very first and very last part of the name."""
    words = name.split()
    out = []
    for i, word in enumerate(words):
        parts = word.split("-")
        out.append((word, [(p, (i == 0 and j == 0) or (i == len(words) - 1 and j == len(parts) - 1))
                           for j, p in enumerate(parts)]))
    return out


def name_case_problems(name):
    """Why ``name`` is not Title Case with space-separated words (CONVENTIONS
    § Naming), as short phrases; ``[]`` when it is.

    Words are separated by single spaces, with no leading or trailing
    space and no ``_``. A word may be hyphenated (each part is checked).
    A part is fine when its first letter or digit (after an opening
    bracket or quote) is a digit (``2``, ``3D``) or an uppercase letter
    (``Hair``, ``L``, ``A``, ``FX``, ``McKay``), when it has no letter or
    digit at all (``&``, ``[``, ``]``), or when it is a lowercase minor word
    (``MINOR_WORDS``: ``of``, ``and``, ``the``, ...) that is neither the
    first nor the last word. Letters without case (Japanese) are not
    English Title Case.
    """
    if not name.strip():
        return ["empty name"]
    problems = []
    if name != name.strip():
        problems.append("leading or trailing space")
    if re.search(r"\s{2,}|[^\S ]", name.strip()):
        problems.append("words not separated by single spaces")
    if "_" in name:
        problems.append("underscores instead of spaces")
    bad = {"lowercase": [], "uncased": []}
    for word, parts in _word_parts(name.replace("_", " ")):
        kinds = {_part_problem(p, edge) for p, edge in parts} - {None}
        for kind in sorted(kinds):
            if word not in bad[kind]:
                bad[kind].append(word)
    if bad["lowercase"]:
        problems.append("lowercase " + ", ".join(f"'{w}'" for w in bad["lowercase"]))
    if bad["uncased"]:
        problems.append("not English: " + ", ".join(f"'{w}'" for w in bad["uncased"]))
    return problems


def title_case(name):
    """The Title Case spelling of ``name`` (spaces for ``_`` and runs of
    whitespace, first letters raised), or None if that doesn't fix it."""
    words = []
    for word, parts in _word_parts(name.replace("_", " ")):
        fixed = []
        for p, edge in parts:
            if _part_problem(p, edge) == "lowercase":
                i = next(i for i, c in enumerate(p) if c.isalnum())
                p = p[:i] + p[i].upper() + p[i + 1:]
            fixed.append(p)
        words.append("-".join(fixed))
    fixed = " ".join(words)
    return fixed if fixed != name and not name_case_problems(fixed) else None


@rule("name-case", "info", sets=("conventions",))
def name_case(snapshot):
    """A paint layer's or group's name that ``name_case_problems`` objects
    to. Other node types are ``node-type`` findings."""
    out = []
    for n in checked_nodes(snapshot):
        if n.get("type") not in (PAINT, GROUP):
            continue
        name = n.get("name") or ""
        problems = name_case_problems(name)
        if problems:
            fix = title_case(name)
            out.append(finding("name-case", n, (
                f"{'Group' if n.get('type') == GROUP else 'Layer'} '{n.get('path')}' is not Title Case "
                f"with space-separated words ({'; '.join(problems)})"
                + (f"; rename it to '{fix}'" if fix else ""))))
    return out


_BLEND_SUFFIX = {"multiply": "Multiply", "add": "Add", "linear_dodge": "Add"}  # D17: one Add


@rule("blend-suffix", "warning", sets=("conventions",))
def blend_suffix(snapshot):
    """A paint layer in Multiply or Add (``add`` or ``linear_dodge``, D17)
    whose last word isn't ``Multiply`` / ``Add``, or whose last word is one
    of them (any case) but the mode doesn't match. A one-word name has no
    suffix. Groups are exempt (group blending doesn't matter to the rig)."""
    want_add = settings(snapshot)["additive_mode"]
    want_add = "add" if want_add == "either" else want_add
    out = []
    for n in checked_nodes(snapshot):
        if n.get("type") != PAINT:
            continue
        name, path = n.get("name") or "", n.get("path")
        mode = n.get("blend_mode") or "normal"
        words = name.split()
        last = words[-1] if len(words) > 1 else ""
        has = last.capitalize() if last.lower() in ("multiply", "add") else None
        want = _BLEND_SUFFIX.get(mode)
        stem = " ".join(words[:-1]) if has else name.strip()
        if want and has == want:
            if last != want:
                msg = f"ends in ' {last}'; spell the suffix ' {want}': rename it to '{stem} {want}'"
            else:
                continue
        elif want and not has:
            msg = f"uses blend mode '{mode}' but its name doesn't end in ' {want}'; rename it to '{stem} {want}'"
        elif want:
            msg = (f"uses blend mode '{mode}' but its name ends in ' {last}'; rename it to '{stem} {want}' "
                   f"or change its blend mode with krita_update_layers")
        elif has:
            mode_for = "multiply" if has == "Multiply" else want_add
            msg = (f"ends in ' {last}' but uses blend mode '{mode}'; set blend_mode '{mode_for}' with "
                   f"krita_update_layers, or rename it to '{stem}'")
        else:
            continue
        out.append(finding("blend-suffix", n, f"Layer '{path}' {msg}"))
    return out


_POSE = re.compile(r"(.*\S) ([A-Z])")


def pose_parts(name):
    """``(stem, letter)`` for a pose name ``<X> A`` (the last word one
    capital other than the side tokens ``L`` / ``R``), else None."""
    m = _POSE.fullmatch(name)
    if not m or m.group(2) in ("L", "R"):
        return None
    return m.group(1), m.group(2)


@rule("pose-visibility", "warning", sets=("conventions",))
def pose_visibility(snapshot):
    """Sibling groups ``<X> A``, ``<X> B``, … (two or more, one of them
    ``A``): pose ``A`` hidden, or another pose visible. Own ``visible``
    flags only (siblings share their ancestors). A family without ``A`` is
    not checked."""
    families = {}
    for n in checked_nodes(snapshot):
        p = pose_parts(n.get("name") or "") if n.get("type") == GROUP else None
        if p:
            families.setdefault((n.get("parent"), p[0]), []).append((p[1], n))
    out = []
    for members in families.values():
        if len(members) < 2 or not any(letter == "A" for letter, _ in members):
            continue
        a_name = next(n.get("name") for letter, n in members if letter == "A")
        for letter, n in members:
            others = [m["id"] for _, m in members if m is not n]
            if letter == "A" and not n.get("visible", True):
                out.append(finding("pose-visibility", n, (
                    f"Pose group '{n.get('path')}' is hidden; keep pose A visible and the others hidden"),
                    others))
            elif letter != "A" and n.get("visible", True):
                out.append(finding("pose-visibility", n, (
                    f"Pose group '{n.get('path')}' is visible; hide it (only pose A, '{a_name}', "
                    "stays visible)"), others))
    return out


@rule("reference-hidden", "warning", sets=("conventions",))
def reference_hidden(snapshot):
    """A reference group (``is_reference_group``) whose own ``visible`` is
    true. Its contents stay skipped."""
    return [finding("reference-hidden", n, (
                f"Reference group '{n.get('path')}' is visible; hide it (reference material "
                "stays hidden)"))
            for n in checked_nodes(snapshot) if is_reference_group(n) and n.get("visible", True)]


def clipping_bases(snapshot):
    """``{base_id: [clipped ids]}``: for each ``inherit_alpha`` node, its
    clipping base, the nearest sibling below it (same parent, later in the
    stack) that doesn't inherit alpha itself, so a stack of clipped layers
    shares one base (the clipping-mask idiom; Krita clips to everything
    below in the group, which is the base in that idiom). Masks are not
    siblings here. Only ``checked_nodes``."""
    siblings = {}
    for n in checked_nodes(snapshot):
        if n.get("type") not in MASK_TYPES:
            siblings.setdefault(n.get("parent"), []).append(n)
    bases = {}
    for group in siblings.values():
        for i, n in enumerate(group):
            if not n.get("inherit_alpha"):
                continue
            base = next((m for m in group[i + 1:] if not m.get("inherit_alpha")), None)
            if base is not None:
                bases.setdefault(base["id"], []).append(n["id"])
    return bases


@rule("mask-suffix", "info", sets=("conventions",))
def mask_suffix(snapshot):
    """A clipping base (``clipping_bases``) whose last word isn't ``Mask``.
    Fires only where ``inherit-alpha`` does: it names the layer the clipped
    ones would be merged into."""
    by_id = {n["id"]: n for n in snapshot["nodes"]}
    out = []
    for base_id, clipped in clipping_bases(snapshot).items():
        base = by_id[base_id]
        name = (base.get("name") or "").strip()
        if name.split()[-1:] == ["Mask"]:
            continue
        names = ", ".join(f"'{by_id[c].get('name')}'" for c in clipped)
        kind = "Group" if base.get("type") == GROUP else "Layer"
        out.append(finding("mask-suffix", base, (
            f"{kind} '{base.get('path')}' is the clipping base of {names}; if it exists only to clip, "
            f"rename it to '{name} Mask'"), clipped))
    return out
